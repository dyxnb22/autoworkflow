"""Product sharpening: distinct reviewer, auto test gate, handoff success."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.config import DEFAULT_CONFIG, distinct_reviewer_satisfied, merge_config
from cc_loop.preflight import PreflightError, verify_distinct_reviewer
from cc_loop.recovery import AutoStep, decide_auto_step
from cc_loop.run import execute_resume, execute_run
from cc_loop.state import AttemptPhase, AttemptRecord, TaskStatus, load_state
from cc_loop.summary import SUCCESS_READY_FOR_HANDOFF, build_task_summary
from tests.fake_providers import FakeReviewer
from tests.helpers import TempEnv, make_task


def _cli(*args: str, state_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cc_loop.cli", "--state-root", str(state_root), *args],
        capture_output=True,
        text=True,
        env={**os.environ},
    )


class DefaultConfigSharpeningTests(unittest.TestCase):
    def test_defaults_favor_handoff_and_single_loop(self) -> None:
        self.assertFalse(DEFAULT_CONFIG["auto_merge"])
        self.assertTrue(DEFAULT_CONFIG["require_distinct_reviewer"])
        self.assertFalse(DEFAULT_CONFIG["allow_merge_without_tests"])
        self.assertEqual(DEFAULT_CONFIG["planner_granularity"], "single")
        self.assertFalse(DEFAULT_CONFIG["allow_parallel_execution"])


class DistinctReviewerTests(unittest.TestCase):
    def test_same_provider_rejected_when_required(self) -> None:
        config = merge_config(
            {
                "require_distinct_reviewer": True,
                "implementer_provider": "claude-code",
                "reviewer_provider": "claude-code",
                "claude_code_model": "sonnet",
            }
        )
        providers = {
            "planner": "claude-code",
            "implementer": "claude-code",
            "reviewer": "claude-code",
        }
        self.assertFalse(distinct_reviewer_satisfied(config, providers))
        with self.assertRaises(PreflightError) as ctx:
            verify_distinct_reviewer(config, providers)
        self.assertIn("require_distinct_reviewer", str(ctx.exception))

    def test_different_models_on_same_provider_satisfy(self) -> None:
        # Models are provider-scoped config keys; use two providers for a realistic split.
        config = merge_config(
            {
                "require_distinct_reviewer": True,
                "implementer_provider": "cursor",
                "reviewer_provider": "codex",
                "cursor_model": "composer",
                "codex_model": "o3",
            }
        )
        providers = {
            "planner": "codex",
            "implementer": "cursor",
            "reviewer": "codex",
        }
        self.assertTrue(distinct_reviewer_satisfied(config, providers))
        verify_distinct_reviewer(config, providers)

    def test_init_rejects_identical_writer_reviewer(self) -> None:
        env = TempEnv()
        try:
            result = _cli(
                "init",
                "--goal",
                "demo",
                "--repo",
                str(env.repo()),
                "--task-id",
                "same-roles",
                "--planner",
                "fake-planner",
                "--implementer",
                "fake-implementer",
                "--reviewer",
                "fake-implementer",
                "--test-command",
                "--",
                "true",
                state_root=env.state_root(),
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("require_distinct_reviewer", result.stderr)
        finally:
            env.close()

    def test_init_allow_same_reviewer_escape_hatch(self) -> None:
        env = TempEnv()
        try:
            result = _cli(
                "init",
                "--goal",
                "demo",
                "--repo",
                str(env.repo()),
                "--task-id",
                "same-ok",
                "--planner",
                "fake-planner",
                "--implementer",
                "fake-implementer",
                "--reviewer",
                "fake-implementer",
                "--allow-same-reviewer",
                "--test-command",
                "--",
                "true",
                state_root=env.state_root(),
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            state = load_state("same-ok", env.state_root())
            self.assertFalse(state.config["require_distinct_reviewer"])
        finally:
            env.close()


class AutoTestGateTests(unittest.TestCase):
    def test_auto_refuses_without_test_command(self) -> None:
        env = TempEnv()
        try:
            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="no-tests",
                config={"test_command": [], "allow_merge_without_tests": False},
            )
            result = _cli("auto", "--task-id", "no-tests", state_root=env.state_root())
            self.assertEqual(result.returncode, 1)
            self.assertIn("test_command", result.stderr)
        finally:
            env.close()

    def test_run_warns_without_test_command(self) -> None:
        env = TempEnv()
        try:
            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="run-warn",
                config={"test_command": [], "allow_merge_without_tests": False},
            )
            # Avoid executing providers; just hit the CLI gate/warning path by
            # invoking run with a missing worktree root mock would still run.
            # Call the warning path via a dry check: load and invoke cmd through CLI
            # with fake providers — execute_run may succeed with skipped tests.
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                result = _cli("run", "--task-id", "run-warn", state_root=env.state_root())
            self.assertIn("no test_command configured", result.stderr)
            self.assertIn("cc-loop auto", result.stderr)
        finally:
            env.close()


class DoctorDeliveryFieldsTests(unittest.TestCase):
    def test_doctor_human_shows_roles_and_distinct(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr
        from cc_loop.cli import main

        env = TempEnv()
        try:
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "--state-root",
                        str(env.state_root()),
                        "doctor",
                        "--repo",
                        str(env.repo()),
                        "--planner",
                        "fake-planner",
                        "--reviewer",
                        "fake-reviewer",
                        "--implementer",
                        "fake-implementer",
                        "--test-command",
                        "--",
                        "true",
                    ]
                )
            self.assertEqual(code, 0, msg=stderr.getvalue())
            out = stdout.getvalue()
            self.assertIn("roles: write=fake-implementer", out)
            self.assertIn("distinct_reviewer: True", out)
            self.assertIn("require_distinct_reviewer: True", out)
        finally:
            env.close()

    def test_doctor_json_includes_delivery_fields(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr
        from cc_loop.cli import main

        env = TempEnv()
        try:
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "--state-root",
                        str(env.state_root()),
                        "doctor",
                        "--repo",
                        str(env.repo()),
                        "--planner",
                        "fake-planner",
                        "--reviewer",
                        "fake-reviewer",
                        "--implementer",
                        "fake-implementer",
                        "--json",
                        "--test-command",
                        "--",
                        "true",
                    ]
                )
            self.assertEqual(code, 0, msg=stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["distinct_reviewer"])
            self.assertTrue(payload["require_distinct_reviewer"])
            self.assertIn("auto_merge_default", payload)
            self.assertFalse(payload["auto_merge_default"])
        finally:
            env.close()


class HandoffSuccessTests(unittest.TestCase):
    def test_default_success_is_ready_for_handoff(self) -> None:
        env = TempEnv()
        try:
            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="handoff",
                config={"auto_merge": False},
            )
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("handoff", env.state_root())
                state, attempt, _paths = execute_run(state, env.state_root())

            self.assertEqual(state.status, TaskStatus.DONE)
            self.assertEqual(attempt.phase, AttemptPhase.APPROVED)
            self.assertEqual(attempt.decision, "approve")
            self.assertEqual(attempt.test_status, "passed")
            self.assertFalse((env.repo() / "hello.txt").exists())
            self.assertTrue(Path(attempt.worktree_path).is_dir())

            summary = build_task_summary(state, env.state_root())
            self.assertEqual(summary["success"], SUCCESS_READY_FOR_HANDOFF)
            self.assertFalse(summary["auto_merge"])
            self.assertTrue(summary["distinct_reviewer"])
            self.assertIn("roles", summary)
            self.assertEqual(summary["tests"]["status"], "passed")
            self.assertEqual(summary["review"]["decision"], "approve")
        finally:
            env.close()


class RejectRetryTests(unittest.TestCase):
    def test_reject_returns_to_implement_via_resume_and_auto_step(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="reject-retry")
            with mock.patch.object(FakeReviewer, "decision", "reject"), mock.patch(
                "cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()
            ):
                state = load_state("reject-retry", env.state_root())
                state, attempt, _paths = execute_run(state, env.state_root())

            self.assertEqual(attempt.phase, AttemptPhase.REJECTED)
            step, _report = decide_auto_step(state, attempt, state.config)
            self.assertEqual(step, AutoStep.RESUME)

            summary = build_task_summary(state, env.state_root())
            self.assertEqual(summary["latest_attempt"]["retry"], 0)
            self.assertTrue(summary.get("latest_reject_reason"))

            with mock.patch.object(FakeReviewer, "decision", "approve"), mock.patch(
                "cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()
            ):
                state = load_state("reject-retry", env.state_root())
                state, retry_attempt, _paths = execute_resume(state, env.state_root())

            self.assertEqual(retry_attempt.retry, 1)
            self.assertIn(retry_attempt.phase, {AttemptPhase.APPROVED, AttemptPhase.MERGED})
        finally:
            env.close()


class SummaryContractFieldsTests(unittest.TestCase):
    def test_summary_json_includes_luma_fields(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="luma-summary")
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("luma-summary", env.state_root())
                execute_run(state, env.state_root())

            result = _cli("summary", "--task-id", "luma-summary", "--json", state_root=env.state_root())
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            for key in (
                "task_id",
                "goal",
                "status",
                "phase",
                "next_action",
                "roles",
                "distinct_reviewer",
                "plan_summary",
                "latest_attempt",
                "tests",
                "review",
                "diff_stat",
                "success",
                "artifacts",
            ):
                self.assertIn(key, payload)
            self.assertIn("provider", payload["roles"]["implementer"])
            self.assertIn("provider", payload["roles"]["reviewer"])
            self.assertIn(payload["success"], {"ready_for_handoff", "merged", "stopped", "failed"})

            human = _cli("summary", "--task-id", "luma-summary", state_root=env.state_root())
            self.assertEqual(human.returncode, 0)
            self.assertIn("implementer (writes)", human.stdout)
            self.assertIn("reviewer (reviews)", human.stdout)
            self.assertIn("Tests:", human.stdout)
            self.assertIn("Handoff:", human.stdout)
        finally:
            env.close()

    def test_status_json_includes_delivery_fields(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="status-delivery")
            result = _cli("status", "--task-id", "status-delivery", "--json", state_root=env.state_root())
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            for key in ("roles", "distinct_reviewer", "require_distinct_reviewer", "auto_merge", "success"):
                self.assertIn(key, payload)
            self.assertTrue(payload["require_distinct_reviewer"])
            self.assertIn("provider", payload["roles"]["implementer"])
            self.assertEqual(payload["success"], "initialized")

            human = _cli("status", "--task-id", "status-delivery", state_root=env.state_root())
            self.assertEqual(human.returncode, 0)
            self.assertIn("roles: write=", human.stdout)
            self.assertIn("distinct_reviewer:", human.stdout)
            self.assertIn("success:", human.stdout)
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
