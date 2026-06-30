"""Unit tests for auto recovery dispatch."""

from __future__ import annotations

import unittest

from cc_loop.config import merge_config
from cc_loop.failure import FailureReport, FailureType, RecoveryDisposition
from cc_loop.recovery import AutoStep, decide_auto_step, recovery_budget_remaining
from cc_loop.state import AttemptPhase, AttemptRecord, TaskState, TaskStatus


def _attempt(**kwargs) -> AttemptRecord:
    base = {
        "iteration": 1,
        "retry": 0,
        "created_at": "t",
        "base_commit": "abc",
        "phase": AttemptPhase.APPROVED,
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
        config=merge_config(),
        history=[attempt] if attempt is not None else [],
    )


class RecoveryDispatchTests(unittest.TestCase):
    def test_merge_conflict_requests_repair(self) -> None:
        attempt = _attempt(merge_error="conflict", failure_type="merge_conflict")
        state = _state(attempt)
        report = FailureReport(
            failure_type=FailureType.MERGE_CONFLICT,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="conflict",
        )
        step, _ = decide_auto_step(
            state,
            attempt,
            state.config,
            artifact_paths={"plan_prompt": type("P", (), {"parent": __import__('pathlib').Path('/tmp')})()},
        )
        self.assertIn(step, {AutoStep.REPAIR, AutoStep.MERGE_RETRY, AutoStep.RESUME})

    def test_merge_retry_budget(self) -> None:
        attempt = _attempt(merge_retry_count=2)
        report = FailureReport(
            failure_type=FailureType.MERGE_WORKTREE_BUSY,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="busy",
        )
        self.assertFalse(recovery_budget_remaining(attempt, merge_config(), report))

    def test_terminal_reviewer_stop(self) -> None:
        attempt = _attempt(
            decision="stop",
            review_json={"stop_reason": "permission denied", "issues": []},
        )
        state = _state(attempt)
        step, report = decide_auto_step(state, attempt, state.config)
        self.assertEqual(step, AutoStep.TERMINAL)
        assert report is not None
        self.assertEqual(report.failure_type, FailureType.REVIEWER_STOP_TERMINAL)

    def test_done_with_more_steps_runs_again(self) -> None:
        attempt = _attempt(
            phase=AttemptPhase.MERGED,
            plan_json={"is_final_step": False, "prompt": "x"},
        )
        state = _state(attempt, status=TaskStatus.DONE)
        step, _ = decide_auto_step(state, attempt, state.config)
        self.assertEqual(step, AutoStep.RUN)


if __name__ == "__main__":
    unittest.main()
