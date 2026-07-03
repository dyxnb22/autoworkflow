"""State machine tests for reviewer approve + recovery cleanup."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cc_loop.failure import (
    FailureReport,
    FailureType,
    RecoveryDisposition,
    failure_report_path,
    write_failure_report,
)
from cc_loop.inspect import build_failure_snapshot, build_status_snapshot, derive_current_message
from cc_loop.recovery import AutoStep, decide_auto_step
from cc_loop.run import _run_finalize_phase
from cc_loop.runner_heartbeat import read_heartbeat, write_heartbeat, RunnerHeartbeat
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    plan_artifact_paths,
    save_state,
)
from cc_loop.config import merge_config
from tests.helpers import TempEnv, make_task


def _attempt(**kwargs) -> AttemptRecord:
    base = {
        "iteration": 1,
        "retry": 0,
        "created_at": "t",
        "base_commit": "abc",
        "worktree_path": "/tmp/wt",
        "branch": "cc-loop/t1/iter-001",
    }
    base.update(kwargs)
    return AttemptRecord(**base)


def _state(attempt: AttemptRecord | None, **kwargs) -> TaskState:
    return TaskState(
        task_id="t1",
        goal="g",
        target_repo="/repo",
        base_branch="main",
        base_commit="abc",
        status=kwargs.pop("status", TaskStatus.STOPPED),
        iteration=1,
        config=merge_config(kwargs.pop("config", None) or {}),
        history=[attempt] if attempt is not None else [],
    )


class ReviewerApproveClearsStaleFailureTests(unittest.TestCase):
    def test_approve_clears_patch_not_captured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp)
            artifact_paths = plan_artifact_paths(artifact_root)
            attempt = _attempt(
                phase=AttemptPhase.REVIEWING,
                failure_type=FailureType.PATCH_NOT_CAPTURED.value,
                recovery_disposition=RecoveryDisposition.RECOVERABLE.value,
                stop_reason="uncaptured",
                attempted_repairs=["implementer_repair:patch_not_captured"],
                test_status="failed",
            )
            state = _state(attempt)
            write_failure_report(
                artifact_root,
                FailureReport(
                    failure_type=FailureType.PATCH_NOT_CAPTURED,
                    disposition=RecoveryDisposition.RECOVERABLE,
                    message="uncaptured patch",
                ),
            )
            attempt.review_json = {"decision": "approve", "reason": "skeleton node"}
            attempt.decision = "approve"
            attempt.phase = AttemptPhase.APPROVED
            from cc_loop.run import _clear_failure_state

            _clear_failure_state(attempt, artifact_paths)

            self.assertEqual(attempt.decision, "approve")
            self.assertEqual(attempt.phase, AttemptPhase.APPROVED)
            self.assertEqual(attempt.failure_type, "")
            self.assertEqual(attempt.attempted_repairs, [])
            self.assertFalse(failure_report_path(artifact_root).is_file())

    def test_decide_auto_step_ignores_stale_patch_after_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp)
            (artifact_root / "plan.prompt.txt").write_text("plan", encoding="utf-8")
            attempt = _attempt(
                phase=AttemptPhase.APPROVED,
                decision="approve",
                review_json={"decision": "approve"},
                test_status="passed",
                failure_type="",
            )
            state = _state(attempt)
            write_failure_report(
                artifact_root,
                FailureReport(
                    failure_type=FailureType.PATCH_NOT_CAPTURED,
                    disposition=RecoveryDisposition.RECOVERABLE,
                    message="stale",
                ),
            )
            step, report = decide_auto_step(
                state,
                attempt,
                state.config,
                artifact_paths={"plan_prompt": artifact_root / "plan.prompt.txt"},
            )
            self.assertEqual(step, AutoStep.MERGE_RETRY)
            self.assertIsNone(report)


class FailedTestsWithoutApproveTests(unittest.TestCase):
    def test_failed_tests_without_reviewer_approve_requests_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp)
            (artifact_root / "test.output.txt").write_text(
                "ERROR collecting tests/test_foo.py",
                encoding="utf-8",
            )
            (artifact_root / "plan.prompt.txt").write_text("plan", encoding="utf-8")
            attempt = _attempt(
                phase=AttemptPhase.TESTING,
                test_status="failed",
                decision="",
            )
            state = _state(attempt, status=TaskStatus.STOPPED)
            step, report = decide_auto_step(
                state,
                attempt,
                state.config,
                artifact_paths={
                    "plan_prompt": artifact_root / "plan.prompt.txt",
                    "test_output": artifact_root / "test.output.txt",
                },
            )
            self.assertEqual(step, AutoStep.REPAIR)
            assert report is not None
            self.assertIn(
                report.failure_type,
                {FailureType.TEST_ENVIRONMENT, FailureType.TEST_IMPLEMENTATION},
            )


class ApproveFailedTestsNoLoopTests(unittest.TestCase):
    def test_decide_auto_step_returns_terminal_not_resume(self) -> None:
        attempt = _attempt(
            phase=AttemptPhase.APPROVED,
            decision="approve",
            review_json={"decision": "approve", "reason": "skeleton ok"},
            test_status="failed",
            graph_node_id="T1",
        )
        state = _state(attempt, config={"allow_merge_without_tests": False})
        step, report = decide_auto_step(state, attempt, state.config)
        self.assertNotEqual(step, AutoStep.RESUME)
        self.assertNotEqual(step, AutoStep.REPAIR)
        self.assertEqual(step, AutoStep.TERMINAL)
        assert report is not None
        self.assertEqual(report.failure_type, FailureType.TEST_GATE_BLOCKED)

    def test_finalize_writes_test_gate_blocked_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp)
            artifact_paths = plan_artifact_paths(artifact_root)
            attempt = _attempt(
                phase=AttemptPhase.APPROVED,
                decision="approve",
                review_json={"decision": "approve"},
                test_status="failed",
                implementer_exit_code=0,
            )
            state = _state(attempt)
            state, attempt, _paths = _run_finalize_phase(state, Path(tmp) / "state", attempt, artifact_paths)
            self.assertEqual(state.status, TaskStatus.STOPPED)
            self.assertEqual(attempt.phase, AttemptPhase.APPROVED)
            self.assertEqual(attempt.failure_type, FailureType.TEST_GATE_BLOCKED.value)
            self.assertTrue(failure_report_path(artifact_root).is_file())


class StaleFailureReportIgnoredTests(unittest.TestCase):
    def test_build_failure_snapshot_ignores_stale_patch_after_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            attempt = _attempt(
                phase=AttemptPhase.APPROVED,
                decision="approve",
                review_json={"decision": "approve"},
                test_status="passed",
            )
            state = _state(attempt)
            artifact_root = artifacts_dir("t1", 1, 0, state_root)
            artifact_root.mkdir(parents=True, exist_ok=True)
            write_failure_report(
                artifact_root,
                FailureReport(
                    failure_type=FailureType.PATCH_NOT_CAPTURED,
                    disposition=RecoveryDisposition.RECOVERABLE,
                    message="stale",
                ),
            )
            snapshot = build_failure_snapshot(attempt, state_root, "t1", state=state)
            self.assertEqual(snapshot["failure_type"], "")


class HeartbeatStatusTransitionTests(unittest.TestCase):
    def test_finalize_test_gate_blocked_message_not_reviewer_running(self) -> None:
        attempt = _attempt(
            phase=AttemptPhase.APPROVED,
            decision="approve",
            review_json={"decision": "approve"},
            test_status="failed",
        )
        state = _state(attempt)
        message = derive_current_message(
            state,
            attempt,
            running=False,
            live_phase="reviewing",
            live_running_provider="claude-code",
        )
        self.assertNotIn("Reviewer running", message)
        self.assertIn("test gate", message.lower())

    def test_heartbeat_cleared_after_finalize_test_gate_blocked(self) -> None:
        env = TempEnv()
        try:
            state_root = env.state_root()
            attempt = _attempt(
                phase=AttemptPhase.APPROVED,
                decision="approve",
                review_json={"decision": "approve"},
                test_status="failed",
                implementer_exit_code=0,
            )
            state = _state(attempt)
            state.task_id = "hb-task"
            write_heartbeat(
                state_root,
                RunnerHeartbeat(
                    task_id="hb-task",
                    pid=os.getpid(),
                    started_at="t",
                    updated_at="t",
                    status="running",
                    phase="reviewing",
                    iteration=1,
                    running_provider="claude-code",
                ),
            )
            artifact_root = artifacts_dir("hb-task", 1, 0, state_root)
            artifact_root.mkdir(parents=True, exist_ok=True)
            artifact_paths = plan_artifact_paths(artifact_root)
            _run_finalize_phase(state, state_root, attempt, artifact_paths)
            hb = read_heartbeat(state_root, "hb-task")
            assert hb is not None
            self.assertEqual(hb.phase, AttemptPhase.APPROVED.value)
            self.assertEqual(hb.running_provider, "")
        finally:
            env.close()


class ApprovePassedTestsRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        self.worktree_root = self.env.worktree_root()

    def tearDown(self) -> None:
        self.env.close()

    def _patch_worktree_root(self):
        return mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.worktree_root)

    def test_approve_with_passed_tests_merges(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root)
        with self._patch_worktree_root():
            from cc_loop.run import execute_run
            from cc_loop.state import load_state

            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertEqual(attempt.phase, AttemptPhase.MERGED)
        self.assertEqual(attempt.decision, "approve")
        self.assertEqual(attempt.test_status, "passed")
        self.assertTrue((self.repo / "hello.txt").is_file())


if __name__ == "__main__":
    unittest.main()
