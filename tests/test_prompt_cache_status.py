"""Tests for prompt cache layout, early phase persistence, and CLI warnings."""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.cli import main
from cc_loop.config import merge_config
from cc_loop.inspect import build_status_snapshot
from cc_loop.failure import failure_report_path
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider
from cc_loop.run import (
    ImplementingError,
    build_implementer_prompt,
    build_planner_prompt,
    build_reviewer_prompt,
    build_reviewer_prompt_metrics,
    prepare_run,
    run_implementer_phase,
    run_planning_phase,
)
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    load_state,
    plan_artifact_paths,
    save_state,
    utc_now_iso,
)
from cc_loop.task_graph import graph_from_planner_json
from tests.helpers import TempEnv, make_task


class PlannerPromptLayoutTests(unittest.TestCase):
    def test_planner_prompt_puts_stable_contract_before_dynamic_goal(self) -> None:
        state = TaskState(
            task_id="plan-layout",
            goal="Unique goal text for planner layout test",
            target_repo="/repo",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.INITIALIZED,
            iteration=1,
            config=merge_config({"planner_granularity": "single"}),
            providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
        )
        prompt = build_planner_prompt(state)
        marker = "## Dynamic Planner Payload"

        assert "Return raw JSON only. Do not wrap in markdown fences. Do not add commentary." in prompt
        assert prompt.index("SINGLE NODE") < prompt.index(marker)
        assert prompt.index(marker) < prompt.index("Unique goal text for planner layout test")
        assert prompt.index(marker) < prompt.index("Target repo:")


class ImplementerPromptLayoutTests(unittest.TestCase):
    def test_implementer_prompt_puts_stable_contract_before_dynamic_goal(self) -> None:
        state = TaskState(
            task_id="impl-layout",
            goal="Unique implementer goal",
            target_repo="/repo",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.RUNNING,
            iteration=1,
            config=merge_config({}),
            providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
        )
        plan_json = {
            "prompt": "Create widget module",
            "expected_changes": "widget.py",
            "acceptance_criteria": "widget exists",
        }
        prompt = build_implementer_prompt(state, plan_json)
        marker = "## Dynamic Implementer Payload"

        assert "Do not perform unrelated refactors" in prompt
        assert prompt.index(marker) < prompt.index("Unique implementer goal")
        assert prompt.index(marker) < prompt.index("Create widget module")

    def test_node_implementer_prompt_keeps_stable_prefix_across_nodes(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "summary": "demo",
                "nodes": [
                    {
                        "id": "T1",
                        "title": "First",
                        "description": "first node",
                        "dependencies": [],
                        "acceptance_criteria": ["a"],
                        "files_scope": ["a.py"],
                    },
                    {
                        "id": "T2",
                        "title": "Second",
                        "description": "second node",
                        "dependencies": ["T1"],
                        "acceptance_criteria": ["b"],
                        "files_scope": ["b.py"],
                    },
                ],
            }
        )
        state = TaskState(
            task_id="node-layout",
            goal="Graph goal alpha",
            target_repo="/repo",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.RUNNING,
            iteration=1,
            config=merge_config({}),
            history=[],
            providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
            task_graph=graph,
        )
        attempt_a = AttemptRecord(
            iteration=1,
            retry=0,
            created_at=utc_now_iso(),
            base_commit="abc",
            graph_node_id="T1",
            plan_json={"mode": "task_graph"},
        )
        attempt_b = AttemptRecord(
            iteration=1,
            retry=0,
            created_at=utc_now_iso(),
            base_commit="abc",
            graph_node_id="T2",
            plan_json={"mode": "task_graph"},
        )
        prompt_a = build_implementer_prompt(state, attempt_a.plan_json, attempt=attempt_a)
        state.goal = "Graph goal beta"
        prompt_b = build_implementer_prompt(state, attempt_b.plan_json, attempt=attempt_b)
        marker = "## Dynamic Implementer Payload"
        prefix_a = prompt_a[: prompt_a.index(marker)]
        prefix_b = prompt_b[: prompt_b.index(marker)]
        self.assertEqual(prefix_a, prefix_b)


class ReviewerPromptMetricsTests(unittest.TestCase):
    def test_reviewer_metrics_include_cache_health(self) -> None:
        diff_stat = " README.md | 1 +"
        patch_body = "diff --git a/README.md b/README.md\n"
        prompt = build_reviewer_prompt(
            state=TaskState(
                task_id="metrics",
                goal="goal",
                target_repo="/repo",
                base_branch="main",
                base_commit="abc",
                status=TaskStatus.RUNNING,
                iteration=1,
                config=merge_config({}),
                providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
            ),
            attempt=AttemptRecord(
                iteration=1,
                retry=0,
                created_at=utc_now_iso(),
                base_commit="abc",
                implementer_exit_code=0,
            ),
            diff_stat=diff_stat,
            patch_body=patch_body,
            test_status="passed",
        )
        metrics = build_reviewer_prompt_metrics(
            prompt=prompt,
            diff_stat=diff_stat,
            patch_body=patch_body,
        )
        self.assertIn(metrics["cache_health"], {"good", "warning", "poor"})
        self.assertEqual(metrics["cache_health"], "good")
        marker = "## Dynamic Review Payload"
        self.assertNotIn("diff --git", prompt[: prompt.index(marker)])

    def test_large_patch_marks_cache_health_poor(self) -> None:
        diff_stat = " big.txt | 999 +"
        patch_body = "x" * 5000
        prompt = build_reviewer_prompt(
            state=TaskState(
                task_id="metrics-large",
                goal="goal",
                target_repo="/repo",
                base_branch="main",
                base_commit="abc",
                status=TaskStatus.RUNNING,
                iteration=1,
                config=merge_config({}),
                providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
            ),
            attempt=AttemptRecord(
                iteration=1,
                retry=0,
                created_at=utc_now_iso(),
                base_commit="abc",
                implementer_exit_code=0,
            ),
            diff_stat=diff_stat,
            patch_body=patch_body,
            test_status="passed",
        )
        metrics = build_reviewer_prompt_metrics(
            prompt=prompt,
            diff_stat=diff_stat,
            patch_body=patch_body,
        )
        self.assertEqual(metrics["cache_health"], "poor")


@register_provider
class PhaseCheckingImplementer(ProviderAdapter):
    """Implementer that asserts persisted phase while provider is running."""

    name = "phase-check-implementer"

    state_root: Path | None = None
    task_id: str = ""
    observed: dict[str, str] | None = None
    release: threading.Event | None = None

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        assert self.state_root is not None
        assert self.task_id
        state = load_state(self.task_id, self.state_root)
        attempt = state.history[-1]
        if self.observed is not None:
            self.observed["phase"] = attempt.phase.value
            self.observed["running_provider"] = attempt.running_provider
        if self.release is not None:
            self.release.wait(timeout=2.0)
        output_path.write_text('{"result":"ok"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


class ProviderPhasePersistenceTests(unittest.TestCase):
    def test_implementer_phase_persisted_before_provider_run(self) -> None:
        env = TempEnv()
        try:
            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="phase-check",
                config={"implementer_provider": "phase-check-implementer"},
            )
            observed: dict[str, str] = {}
            release = threading.Event()
            PhaseCheckingImplementer.state_root = env.state_root()
            PhaseCheckingImplementer.task_id = "phase-check"
            PhaseCheckingImplementer.observed = observed
            PhaseCheckingImplementer.release = release

            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("phase-check", env.state_root())
                state, attempt, artifact_paths = prepare_run(state, env.state_root())
                state = run_planning_phase(state, env.state_root(), artifact_paths)

                def _run_implementer() -> None:
                    run_implementer_phase(state, env.state_root(), artifact_paths)

                worker = threading.Thread(target=_run_implementer)
                worker.start()
                deadline = time.time() + 5.0
                snapshot = None
                while time.time() < deadline:
                    snapshot = build_status_snapshot(
                        load_state("phase-check", env.state_root()),
                        env.state_root(),
                    )
                    if snapshot["attempt"]["phase"] == "executing":
                        break
                    time.sleep(0.05)
                release.set()
                worker.join(timeout=5.0)

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot["attempt"]["phase"], "executing")
            self.assertNotEqual(snapshot["attempt"]["phase"], "worktree_created")
            self.assertEqual(observed.get("phase"), "executing")
            self.assertEqual(observed.get("running_provider"), "phase-check-implementer")
        finally:
            PhaseCheckingImplementer.observed = None
            PhaseCheckingImplementer.release = None
            env.close()


class PlannerPhasePersistenceTests(unittest.TestCase):
    def test_planner_phase_persisted_before_provider_run(self) -> None:
        env = TempEnv()
        try:
            observed: dict[str, str] = {}
            release = threading.Event()

            @register_provider
            class PhaseCheckingPlanner(ProviderAdapter):
                name = "phase-check-planner"

                def build_args(self, *, worktree_path, prompt, output_path, config) -> list[str]:
                    return ["true"]

                def run(self, **kwargs) -> ProviderRunResult:
                    state = load_state("plan-phase", env.state_root())
                    attempt = state.history[-1]
                    observed["phase"] = attempt.phase.value
                    observed["running_provider"] = attempt.running_provider
                    release.wait(timeout=2.0)
                    payload = {
                        "prompt": "Create hello.txt",
                        "expected_changes": "hello.txt",
                        "acceptance_criteria": "hello.txt exists",
                        "is_final_step": True,
                    }
                    kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                    return ProviderRunResult(
                        provider=self.name,
                        exit_code=0,
                        raw_artifact_path=kwargs["output_path"],
                    )

                def parse_planner_output(self, last_message_path: Path):
                    return json.loads(last_message_path.read_text(encoding="utf-8"))

                def parse_reviewer_output(self, last_message_path: Path):
                    raise NotImplementedError

                def preflight_check_argv(self) -> list[str]:
                    return ["true"]

            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="plan-phase",
                config={"planner_provider": "phase-check-planner"},
            )

            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("plan-phase", env.state_root())
                state, _attempt, artifact_paths = prepare_run(state, env.state_root())

                def _run_planning() -> None:
                    run_planning_phase(state, env.state_root(), artifact_paths)

                worker = threading.Thread(target=_run_planning)
                worker.start()
                deadline = time.time() + 5.0
                snapshot = None
                while time.time() < deadline:
                    snapshot = build_status_snapshot(
                        load_state("plan-phase", env.state_root()),
                        env.state_root(),
                    )
                    if snapshot["attempt"]["phase"] == "planning":
                        break
                    time.sleep(0.05)
                release.set()
                worker.join(timeout=5.0)

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot["attempt"]["phase"], "planning")
            self.assertNotEqual(snapshot["attempt"]["phase"], "worktree_created")
            self.assertEqual(observed.get("phase"), "planning")
            self.assertEqual(observed.get("running_provider"), "phase-check-planner")
        finally:
            env.close()


class ProviderStartupFailureTests(unittest.TestCase):
    def test_unknown_implementer_writes_startup_failure_artifacts(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="startup-fail")
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("startup-fail", env.state_root())
                state, _attempt, artifact_paths = prepare_run(state, env.state_root())
                state = run_planning_phase(state, env.state_root(), artifact_paths)
                with mock.patch(
                    "cc_loop.run.get_provider",
                    side_effect=ValueError("unknown provider: missing-implementer-xyz"),
                ):
                    with self.assertRaises(ImplementingError):
                        run_implementer_phase(state, env.state_root(), artifact_paths)

            artifact_root = artifact_paths["plan_prompt"].parent
            argv_payload = json.loads((artifact_root / "command.argv.json").read_text(encoding="utf-8"))
            result_payload = json.loads((artifact_root / "subprocess.result.json").read_text(encoding="utf-8"))
            self.assertIn("implementer", argv_payload)
            self.assertEqual(result_payload["implementer"]["exit_code"], -1)
            self.assertTrue(failure_report_path(artifact_root).is_file())
            failure = json.loads(failure_report_path(artifact_root).read_text(encoding="utf-8"))
            self.assertEqual(failure["stop_reason"], "provider_resolution_failed")
        finally:
            env.close()


class StatusReviewerMetricsTests(unittest.TestCase):
    def test_status_json_includes_reviewer_prompt_metrics_when_present(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="status-metrics")
            state = load_state("status-metrics", env.state_root())
            state.status = TaskStatus.RUNNING
            state.history = [
                AttemptRecord(
                    iteration=1,
                    retry=0,
                    created_at=utc_now_iso(),
                    base_commit=state.base_commit,
                    phase=AttemptPhase.REVIEWING,
                )
            ]
            save_state(state, env.state_root())
            artifact_root = artifacts_dir("status-metrics", 1, 0, env.state_root())
            artifact_root.mkdir(parents=True, exist_ok=True)
            paths = plan_artifact_paths(artifact_root)
            paths["review_prompt_metrics"].write_text(
                json.dumps(
                    {
                        "layout": "stable-prefix-v1",
                        "stable_prefix_ratio": 0.82,
                        "cache_health": "good",
                        "estimated_prompt_tokens": 120,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot = build_status_snapshot(load_state("status-metrics", env.state_root()), env.state_root())
            self.assertIn("reviewer_prompt_metrics", snapshot)
            self.assertEqual(snapshot["reviewer_prompt_metrics"]["cache_health"], "good")
        finally:
            env.close()


class TestCommandWarningTests(unittest.TestCase):
    def test_doctor_json_before_separator_has_no_warning(self) -> None:
        env = TempEnv()
        try:
            stderr = StringIO()
            with redirect_stderr(stderr):
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
                        "pytest",
                        "-q",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertNotIn("warning:", stderr.getvalue())
        finally:
            env.close()

    def test_doctor_json_after_separator_warns_and_passes_to_test_command(self) -> None:
        env = TempEnv()
        try:
            stderr = StringIO()
            with redirect_stderr(stderr):
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
                        "pytest",
                        "-q",
                        "--json",
                    ]
                )
            err = stderr.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("warning: --json appears after --test-command --", err)
        finally:
            env.close()

    def test_init_misplaced_max_iterations_warns_and_does_not_apply(self) -> None:
        env = TempEnv()
        try:
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "--state-root",
                        str(env.state_root()),
                        "init",
                        "--goal",
                        "goal",
                        "--repo",
                        str(env.repo()),
                        "--task-id",
                        "warn-init",
                        "--test-command",
                        "--",
                        "pytest",
                        "-q",
                        "--max-iterations",
                        "2",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("warning: --max-iterations appears after --test-command --", stderr.getvalue())
            state = load_state("warn-init", env.state_root())
            self.assertNotEqual(state.config["max_iterations"], 2)
            self.assertEqual(state.config["test_command"], ["pytest", "-q", "--max-iterations", "2"])
        finally:
            env.close()


class SubprocessDiagnosticsTests(unittest.TestCase):
    def test_run_writes_command_argv_and_subprocess_result_artifacts(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="diag-artifacts")
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                from cc_loop.run import execute_run

                state = load_state("diag-artifacts", env.state_root())
                state, attempt, _paths = execute_run(state, env.state_root())

            artifact_root = artifacts_dir("diag-artifacts", attempt.iteration, attempt.retry, env.state_root())
            argv_path = artifact_root / "command.argv.json"
            result_path = artifact_root / "subprocess.result.json"
            self.assertTrue(argv_path.is_file())
            self.assertTrue(result_path.is_file())

            argv_payload = json.loads(argv_path.read_text(encoding="utf-8"))
            result_payload = json.loads(result_path.read_text(encoding="utf-8"))
            for phase in ("planner", "implementer", "reviewer", "test"):
                self.assertIn(phase, argv_payload, msg=f"missing {phase} in command.argv.json")
                self.assertIn(phase, result_payload, msg=f"missing {phase} in subprocess.result.json")
                entry = result_payload[phase]
                for key in (
                    "exit_code",
                    "timed_out",
                    "duration_seconds",
                    "killed",
                    "hung",
                    "stdout_path",
                ):
                    self.assertIn(key, entry, msg=f"{phase} missing {key}")
        finally:
            env.close()
