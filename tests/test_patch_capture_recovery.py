"""Tests for patch_not_captured recovery, resume idempotency, and implementer prompt layout."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.config import LoopConfig, merge_config
from cc_loop.failure import (
    FailureReport,
    FailureType,
    RecoveryDisposition,
    failure_report_path,
    write_failure_report,
)
from cc_loop.recovery import persist_failure_state
from cc_loop.git import capture_worktree_diff_metadata, commit_worktree_changes
from cc_loop.inspect import build_status_snapshot, derive_current_message
from cc_loop.prompt_cache import DYNAMIC_IMPLEMENTER_MARKER, build_implementer_phase_cache
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider
from cc_loop.recovery import AutoStep, decide_auto_step
from cc_loop.run import (
    build_implementer_prompt,
    execute_repair_recovery,
    execute_resume,
    prepare_run,
    run_planning_phase,
)
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    load_state,
    save_state,
)
from tests.helpers import TempEnv, make_task


def _setup_worktree_attempt(env: TempEnv, task_id: str = "patch-recovery") -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    repo = env.repo()
    state_root = env.state_root()
    worktree_root = env.worktree_root()
    make_task(repo=repo, state_root=state_root, task_id=task_id)
    with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", worktree_root):
        state = load_state(task_id, state_root)
        state, attempt, artifact_paths = prepare_run(state, state_root)
        state = run_planning_phase(state, state_root, artifact_paths)
        attempt = state.history[-1]
    return state, attempt, artifact_paths


@register_provider
class CommittingRepairImplementer(ProviderAdapter):
    name = "committing-repair-implementer"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        (worktree_path / "generated.py").write_text("value = 1\n", encoding="utf-8")
        commit_worktree_changes(worktree_path, "repair commit")
        output_path.write_text('{"result":"ok"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class NoCommitRepairImplementer(ProviderAdapter):
    name = "no-commit-repair-implementer"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        (worktree_path / "generated.py").write_text("value = 1\n", encoding="utf-8")
        output_path.write_text('{"result":"ok"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


class PatchCaptureRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()

    def tearDown(self) -> None:
        self.env.close()

    def _seed_patch_not_captured_with_commit(self) -> tuple[TaskState, AttemptRecord, dict[str, Path], Path]:
        state, attempt, artifact_paths = _setup_worktree_attempt(self.env, "resume-skip")
        worktree = Path(attempt.worktree_path)
        (worktree / "hello.txt").write_text("hello\n", encoding="utf-8")
        commit_worktree_changes(worktree, "already committed implementation")
        capture_worktree_diff_metadata(
            worktree,
            attempt.base_commit,
            diff_stat_path=artifact_paths["diff_stat"],
            diff_files_path=artifact_paths["diff_files"],
        )

        report = FailureReport(
            failure_type=FailureType.PATCH_NOT_CAPTURED,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="stale uncaptured patch",
            details={"porcelain": ["?? generated.py"]},
        )
        attempt.failure_type = FailureType.PATCH_NOT_CAPTURED.value
        attempt.failure_details = dict(report.details)
        attempt.stop_reason = report.message
        attempt.phase = AttemptPhase.EXECUTING
        attempt.implementer_exit_code = 0
        state.status = TaskStatus.STOPPED
        write_failure_report(artifact_paths["plan_prompt"].parent, report)
        save_state(state, self.env.state_root())
        return state, attempt, artifact_paths, worktree

    def test_resume_skips_repair_when_commit_already_exists(self) -> None:
        state, attempt, artifact_paths, worktree = self._seed_patch_not_captured_with_commit()
        state_root = self.env.state_root()

        with mock.patch("cc_loop.run._invoke_provider") as invoke_mock, mock.patch(
            "cc_loop.run.DEFAULT_WORKTREE_ROOT", self.env.worktree_root()
        ), mock.patch("cc_loop.run.run_test_phase") as test_mock, mock.patch(
            "cc_loop.run.run_review_phase", side_effect=lambda s, *_a, **_k: s
        ), mock.patch(
            "cc_loop.run._run_finalize_phase",
            return_value=(state, attempt, artifact_paths),
        ):
            test_mock.side_effect = lambda s, *_args, **_kwargs: s
            state, attempt, _paths = execute_resume(state, state_root)

        invoke_mock.assert_not_called()
        self.assertEqual(attempt.failure_type, "")
        self.assertFalse(failure_report_path(artifact_paths["plan_prompt"].parent).is_file())
        test_mock.assert_called_once()
        self.assertNotEqual(attempt.head_commit, attempt.base_commit)
        self.assertFalse(list(worktree.iterdir()) == [])

    def test_auto_dispatch_skips_repair_when_commit_already_exists(self) -> None:
        state, attempt, artifact_paths, _worktree = self._seed_patch_not_captured_with_commit()
        from cc_loop.run import reconcile_patch_not_captured

        with mock.patch("cc_loop.run._invoke_provider") as invoke_mock:
            recovered = reconcile_patch_not_captured(
                state,
                attempt,
                artifact_paths,
                self.env.state_root(),
                reason="test_auto_dispatch",
            )
            step, report = decide_auto_step(
                state,
                attempt,
                state.config,
                artifact_paths=artifact_paths,
                running=False,
            )

        self.assertTrue(recovered)
        invoke_mock.assert_not_called()
        self.assertEqual(attempt.failure_type, "")
        self.assertNotEqual(step, AutoStep.REPAIR)
        self.assertIsNone(report)

    def test_repair_success_rechecks_patch_capture_and_continues_to_tests(self) -> None:
        state, attempt, artifact_paths = _setup_worktree_attempt(self.env, "repair-success")
        worktree = Path(attempt.worktree_path)
        (worktree / "generated.py").write_text("value = 1\n", encoding="utf-8")
        report = FailureReport(
            failure_type=FailureType.PATCH_NOT_CAPTURED,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="uncaptured patch",
            details={"porcelain": ["?? generated.py"]},
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        attempt.phase = AttemptPhase.EXECUTING
        state.status = TaskStatus.STOPPED
        save_state(state, self.env.state_root())

        state.config = merge_config(
            {
                **dict(state.config),
                "implementer_provider": "committing-repair-implementer",
            }
        )

        with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.env.worktree_root()), mock.patch(
            "cc_loop.run.run_test_phase"
        ) as test_mock:
            test_mock.side_effect = lambda s, *_args, **_kwargs: s
            state, attempt, _paths = execute_repair_recovery(state, self.env.state_root(), report)

        self.assertEqual(attempt.failure_type, "")
        test_mock.assert_called_once()
        self.assertNotEqual(attempt.head_commit, attempt.base_commit)

    def test_repair_still_uncaptured_stops_cleanly(self) -> None:
        state, attempt, artifact_paths = _setup_worktree_attempt(self.env, "repair-fail")
        worktree = Path(attempt.worktree_path)
        (worktree / "generated.py").write_text("value = 1\n", encoding="utf-8")
        report = FailureReport(
            failure_type=FailureType.PATCH_NOT_CAPTURED,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="uncaptured patch",
            details={"porcelain": ["?? generated.py"]},
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        state.config = merge_config(
            {
                **dict(state.config),
                "implementer_provider": "no-commit-repair-implementer",
            }
        )

        with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.env.worktree_root()), mock.patch(
            "cc_loop.run.run_test_phase"
        ) as test_mock:
            state, attempt, _paths = execute_repair_recovery(state, self.env.state_root(), report)

        test_mock.assert_not_called()
        self.assertEqual(state.status, TaskStatus.STOPPED)
        self.assertEqual(attempt.failure_type, FailureType.PATCH_NOT_CAPTURED.value)
        self.assertEqual(attempt.stop_reason, "repair_did_not_capture_patch")
        self.assertEqual(attempt.running_provider, "")
        message = derive_current_message(state, attempt, running=False)
        self.assertEqual(message, "Repair failed: generated files remain uncommitted")
        failure = json.loads(failure_report_path(artifact_paths["plan_prompt"].parent).read_text(encoding="utf-8"))
        self.assertIn("commit", failure["suggested_actions"][0].lower())

    def test_implementer_prompt_cache_metric_improved(self) -> None:
        state = TaskState(
            task_id="impl-metrics",
            goal="Unique implementer goal for metrics test with enough dynamic text to measure ratio",
            target_repo="/repo",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.RUNNING,
            iteration=1,
            config=merge_config({}),
            providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
        )
        plan_json = {
            "prompt": "Create widget module with CLI and tests for the metrics layout check",
            "expected_changes": "widget.py, tests/test_widget.py",
            "acceptance_criteria": "widget exists and tests pass",
        }
        prompt = build_implementer_prompt(state, plan_json)
        marker_index = prompt.index(DYNAMIC_IMPLEMENTER_MARKER)
        self.assertIn("git add", prompt[:marker_index])
        self.assertIn("git commit", prompt[:marker_index])
        self.assertGreater(len(prompt[:marker_index]), 800)
        self.assertLess(marker_index, prompt.index("Unique implementer goal"))
        self.assertLess(marker_index, prompt.index("Create widget module"))

        metrics = build_implementer_phase_cache(prompt=prompt)
        self.assertGreaterEqual(metrics["stable_prefix_ratio"], 0.45)
        self.assertIn(metrics["total_prompt_cache_health"], {"good", "warning"})

    def test_status_does_not_show_provider_running_after_patch_failure_stop(self) -> None:
        state, attempt, artifact_paths, _worktree = self._seed_patch_not_captured_with_commit()
        attempt.running_provider = "cursor"
        attempt.failure_type = FailureType.PATCH_NOT_CAPTURED.value
        attempt.stop_reason = "repair_did_not_capture_patch"
        state.status = TaskStatus.STOPPED
        save_state(state, self.env.state_root())

        snapshot = build_status_snapshot(state, self.env.state_root())
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["attempt"]["running_provider"], "cursor")
        self.assertNotEqual(snapshot["current_message"], "Implementer running (cursor)")
        self.assertEqual(
            snapshot["current_message"],
            "Repair failed: generated files remain uncommitted",
        )


if __name__ == "__main__":
    unittest.main()
