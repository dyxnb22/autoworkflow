"""Tests for reliability and CLI usability fixes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cc_loop.cli import CcLoopArgumentParser, main
from cc_loop.inspect import build_status_snapshot, runner_pid_path
from cc_loop.planner_granularity import resolve_planner_granularity
from cc_loop.run import build_planner_prompt
from cc_loop.runner_heartbeat import RunnerHeartbeat, write_heartbeat
from cc_loop.provider_runtime import run_provider_with_heartbeat
from cc_loop.subprocess_util import run_with_timeout
from cc_loop.state import AttemptPhase, AttemptRecord, TaskStatus, load_state, save_state, utc_now_iso
from cc_loop.test_command import expand_test_command_in_argv, normalize_test_command, register_test_command_subcommand
from tests.helpers import TempEnv, make_task


def _cli(*args: str, state_root: Path | None = None) -> subprocess.CompletedProcess:
    argv = list(args)
    if state_root is not None:
        argv = ["--state-root", str(state_root), *argv]
    return subprocess.run(
        [sys.executable, "-m", "cc_loop.cli", *argv],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


class TestCommandParsingTests(unittest.TestCase):
    def test_normalize_with_double_dash_separator(self) -> None:
        self.assertEqual(
            normalize_test_command(["--", "python", "-m", "pytest", "tests", "-q"]),
            ["python", "-m", "pytest", "tests", "-q"],
        )

    def test_normalize_legacy_argv(self) -> None:
        self.assertEqual(normalize_test_command(["pytest", "tests"]), ["pytest", "tests"])

    def test_normalize_single_string_shlex_split(self) -> None:
        self.assertEqual(
            normalize_test_command(["python -m pytest tests/ -q"]),
            ["python", "-m", "pytest", "tests/", "-q"],
        )

    def test_normalize_rejects_shell_operators(self) -> None:
        with self.assertRaises(ValueError):
            normalize_test_command(["pytest tests | tee out.log"])

    def test_expand_separator_consumes_remaining_test_command_tokens(self) -> None:
        expanded = expand_test_command_in_argv(
            [
                "init",
                "--goal",
                "g",
                "--repo",
                "/tmp/r",
                "--test-command",
                "--",
                "python",
                "-m",
                "pytest",
                "tests",
                "-q",
                "--max-iterations",
                "3",
            ]
        )
        self.assertEqual(
            expanded,
            [
                "init",
                "--goal",
                "g",
                "--repo",
                "/tmp/r",
                "--test-command",
                "python -m pytest tests -q --max-iterations 3",
            ],
        )

    def test_expand_flags_before_separator_remain_cc_loop_flags(self) -> None:
        expanded = expand_test_command_in_argv(
            [
                "init",
                "--goal",
                "g",
                "--repo",
                "/tmp/r",
                "--max-iterations",
                "3",
                "--test-command",
                "--",
                "python",
                "-m",
                "pytest",
                "tests",
                "-q",
            ]
        )
        self.assertEqual(
            expanded,
            [
                "init",
                "--goal",
                "g",
                "--repo",
                "/tmp/r",
                "--max-iterations",
                "3",
                "--test-command",
                "python -m pytest tests -q",
            ],
        )

    def test_expand_separator_preserves_test_command_long_flags(self) -> None:
        expanded = expand_test_command_in_argv(
            [
                "doctor",
                "--repo",
                "/tmp/r",
                "--test-command",
                "--",
                "pytest",
                "--json",
                "tests",
            ]
        )
        idx = expanded.index("--test-command")
        self.assertEqual(expanded[idx + 1], "pytest --json tests")

        expanded = expand_test_command_in_argv(
            [
                "init",
                "--goal",
                "g",
                "--repo",
                "/tmp/r",
                "--test-command",
                "--",
                "pytest",
                "--maxfail",
                "1",
                "tests",
            ]
        )
        idx = expanded.index("--test-command")
        self.assertEqual(expanded[idx + 1], "pytest --maxfail 1 tests")

    def test_expand_legacy_argv_includes_dash_q(self) -> None:
        expanded = expand_test_command_in_argv(
            [
                "init",
                "--goal",
                "g",
                "--repo",
                "/tmp/r",
                "--test-command",
                "pytest",
                "tests",
                "-q",
                "--planner",
                "codex",
            ]
        )
        self.assertIn("--test-command", expanded)
        idx = expanded.index("--test-command")
        self.assertEqual(expanded[idx + 1], "pytest tests -q")
        self.assertEqual(expanded[idx + 2], "--planner")

    def test_init_test_command_after_other_flags(self) -> None:
        env = TempEnv()
        try:
            result = _cli(
                "init",
                "--goal",
                "goal",
                "--repo",
                str(env.repo()),
                "--task-id",
                "tc-mixed",
                "--max-iterations",
                "7",
                "--test-command",
                "--",
                "python",
                "-m",
                "pytest",
                "tests",
                "-q",
                state_root=env.state_root(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = load_state("tc-mixed", env.state_root())
            self.assertEqual(state.config["test_command"], ["python", "-m", "pytest", "tests", "-q"])
            self.assertEqual(state.config["max_iterations"], 7)
        finally:
            env.close()

    def test_init_legacy_dash_q_without_separator(self) -> None:
        env = TempEnv()
        try:
            result = _cli(
                "init",
                "--goal",
                "goal",
                "--repo",
                str(env.repo()),
                "--task-id",
                "tc-legacy-q",
                "--test-command",
                "pytest",
                "tests",
                "-q",
                state_root=env.state_root(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = load_state("tc-legacy-q", env.state_root())
            self.assertEqual(state.config["test_command"], ["pytest", "tests", "-q"])
        finally:
            env.close()

    def test_init_persists_parsed_test_command(self) -> None:
        env = TempEnv()
        try:
            result = _cli(
                "init",
                "--goal",
                "goal",
                "--repo",
                str(env.repo()),
                "--task-id",
                "tc-parse",
                "--test-command",
                "--",
                "python",
                "-m",
                "pytest",
                "tests",
                "-q",
                state_root=env.state_root(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = load_state("tc-parse", env.state_root())
            self.assertEqual(state.config["test_command"], ["python", "-m", "pytest", "tests", "-q"])
        finally:
            env.close()

    def test_argparse_hint_for_misplaced_test_flag(self) -> None:
        parser = CcLoopArgumentParser(prog="cc-loop")
        parser.add_argument("--test-command", nargs="+")
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--test-command", "pytest", "tests", "-q"])
        self.assertEqual(ctx.exception.code, 2)


class ProviderTimeoutTests(unittest.TestCase):
    def test_timeout_kills_sleeping_child(self) -> None:
        script = "import time; time.sleep(30)"
        result = run_with_timeout([sys.executable, "-c", script], timeout_seconds=1)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.killed)
        self.assertGreater(result.duration_seconds, 0.0)

    def test_timeout_marks_hung_when_sigkill_required(self) -> None:
        script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: None)\n"
            "time.sleep(30)\n"
        )
        result = run_with_timeout([sys.executable, "-c", script], timeout_seconds=1, kill_grace_seconds=0.2)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.hung)


class StaleHeartbeatStatusTests(unittest.TestCase):
    def test_stale_heartbeat_guidance_in_status_json(self) -> None:
        env = TempEnv()
        try:
            state_root = env.state_root()
            make_task(repo=env.repo(), state_root=state_root, task_id="stale-hb")
            state = load_state("stale-hb", state_root)
            state.status = TaskStatus.RUNNING
            state.history = [
                AttemptRecord(
                    iteration=1,
                    retry=0,
                    created_at=utc_now_iso(),
                    base_commit=state.base_commit,
                    phase=AttemptPhase.EXECUTING,
                )
            ]
            save_state(state, state_root)
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0).isoformat()
            write_heartbeat(
                state_root,
                RunnerHeartbeat(
                    task_id="stale-hb",
                    pid=999999,
                    started_at=old,
                    updated_at=old,
                    status="running",
                    phase="executing",
                    iteration=1,
                ),
            )
            snapshot = build_status_snapshot(load_state("stale-hb", state_root), state_root)
            self.assertEqual(snapshot["runner_state"], "stale_heartbeat")
            self.assertIn("stale_heartbeat_guidance", snapshot)
            self.assertIn(snapshot["next_action"], {"resume", "cancel", "cleanup"})
        finally:
            env.close()


class ReportFormatTests(unittest.TestCase):
    def test_report_format_json_flag(self) -> None:
        env = TempEnv()
        try:
            state_root = env.state_root()
            make_task(repo=env.repo(), state_root=state_root, task_id="report-fmt")
            result = _cli("report", "report-fmt", "--format", "json", state_root=state_root)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["task_id"], "report-fmt")
            self.assertIn("attempts", payload)
            self.assertIn("runner_state", payload)
        finally:
            env.close()


class PlannerGranularityTests(unittest.TestCase):
    def test_focused_goal_resolves_single(self) -> None:
        config = {"planner_granularity": "auto"}
        self.assertEqual(resolve_planner_granularity("Focused fix: wire CLI flag only", config), "single")

    def test_planner_prompt_includes_single_node_guidance(self) -> None:
        env = TempEnv()
        try:
            state_root = env.state_root()
            make_task(repo=env.repo(), state_root=state_root, task_id="granularity")
            state = load_state("granularity", state_root)
            state.goal = "Small CLI wiring fix only"
            state.config["planner_granularity"] = "single"
            prompt = build_planner_prompt(state)
            self.assertIn("SINGLE NODE", prompt)
            self.assertIn("Preferred Single-Loop Shape", prompt)
            self.assertNotIn("Prefer task_graph mode when decomposition", prompt)
        finally:
            env.close()


class ProviderWatchdogTests(unittest.TestCase):
    def test_provider_watchdog_kills_blocked_subprocess(self) -> None:
        env = TempEnv()
        try:
            state_root = env.state_root()
            repo = env.repo()
            make_task(repo=repo, state_root=state_root, task_id="watchdog")
            state = load_state("watchdog", state_root)
            state.config["provider_watchdog_grace_seconds"] = 1
            attempt = AttemptRecord(
                iteration=1,
                retry=0,
                created_at=utc_now_iso(),
                base_commit=state.base_commit,
                phase=AttemptPhase.EXECUTING,
            )

            class HangProvider:
                name = "hang-provider"

                def run(self, **kwargs):
                    from cc_loop.providers.base import ProviderRunResult
                    from cc_loop.subprocess_util import run_with_timeout
                    from pathlib import Path

                    output_path = Path(kwargs["output_path"])
                    result = run_with_timeout(
                        [sys.executable, "-c", "import time; time.sleep(120)"],
                        timeout_seconds=9999,
                    )
                    return ProviderRunResult(
                        provider=self.name,
                        exit_code=result.returncode,
                        raw_artifact_path=output_path,
                        timed_out=result.timed_out,
                        hung=result.hung,
                        killed=result.killed,
                    )

            started = time.monotonic()
            result = run_provider_with_heartbeat(
                HangProvider(),
                state_root=state_root,
                state=state,
                attempt=attempt,
                worktree_path=repo,
                prompt="hang",
                output_path=env.root / "out.txt",
                config=state.config,
                timeout_seconds=1,
            )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 10.0)
            self.assertTrue(result.timed_out or result.hung)
        finally:
            env.close()


class TestCommandRegistryTests(unittest.TestCase):
    def test_register_test_command_subcommand(self) -> None:
        register_test_command_subcommand("demo", {"--demo-flag"})
        expanded = expand_test_command_in_argv(
            [
                "demo",
                "--test-command",
                "pytest",
                "tests",
                "-q",
                "--demo-flag",
                "1",
            ]
        )
        idx = expanded.index("--test-command")
        self.assertEqual(expanded[idx + 1], "pytest tests -q")
        self.assertEqual(expanded[idx + 2], "--demo-flag")


if __name__ == "__main__":
    unittest.main()
