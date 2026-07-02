"""Budget enforcement tests."""

from __future__ import annotations

import unittest

from cc_loop.budgets import check_budgets, count_consecutive_failures
from cc_loop.config import merge_config
from cc_loop.state import AttemptPhase, AttemptRecord, TaskState, TaskStatus


class BudgetTests(unittest.TestCase):
    def test_consecutive_failures_budget(self) -> None:
        attempts = [
            AttemptRecord(1, 0, "t", "abc", phase=AttemptPhase.REJECTED, decision="reject"),
            AttemptRecord(1, 1, "t", "abc", phase=AttemptPhase.REJECTED, decision="reject"),
        ]
        state = TaskState(
            task_id="t",
            goal="g",
            target_repo="/r",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.STOPPED,
            iteration=1,
            config=merge_config({"max_consecutive_failures": 2}),
            history=attempts,
        )
        self.assertEqual(count_consecutive_failures(state), 2)
        report = check_budgets(state, attempts[-1], state.config)
        assert report is not None
        self.assertEqual(report.stop_reason, "max_consecutive_failures")
