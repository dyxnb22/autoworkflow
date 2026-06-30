"""Unit tests for failure classification."""

from __future__ import annotations

import unittest

from cc_loop.failure import (
    FailureType,
    RecoveryDisposition,
    classify_merge_failure,
    classify_reviewer_outcome,
    classify_test_failure,
)
from cc_loop.git import GitCommandError, GitCommandResult
from cc_loop.state import AttemptRecord


class FailureClassificationTests(unittest.TestCase):
    def test_merge_conflict_is_recoverable(self) -> None:
        result = GitCommandResult(
            returncode=1,
            stdout="",
            stderr="CONFLICT (add/add): Merge conflict in hello.txt",
            args=("merge",),
        )
        report = classify_merge_failure(GitCommandError("merge failed", result=result))
        self.assertEqual(report.failure_type, FailureType.MERGE_CONFLICT)
        self.assertEqual(report.disposition, RecoveryDisposition.RECOVERABLE)
        self.assertIn("hello.txt", report.details["conflict_files"])

    def test_merge_worktree_busy(self) -> None:
        report = classify_merge_failure(Exception("base branch 'main' is already checked out in another worktree"))
        self.assertEqual(report.failure_type, FailureType.MERGE_WORKTREE_BUSY)
        self.assertEqual(report.disposition, RecoveryDisposition.RECOVERABLE)

    def test_test_environment_is_terminal(self) -> None:
        output = "E   ModuleNotFoundError: No module named 'missing_pkg'"
        report = classify_test_failure(output, "failed")
        self.assertEqual(report.failure_type, FailureType.TEST_ENVIRONMENT)
        self.assertEqual(report.disposition, RecoveryDisposition.TERMINAL)

    def test_test_implementation_is_recoverable(self) -> None:
        output = "FAILED tests/test_app.py::test_add - AssertionError"
        report = classify_test_failure(output, "failed")
        self.assertEqual(report.failure_type, FailureType.TEST_IMPLEMENTATION)
        self.assertEqual(report.disposition, RecoveryDisposition.RECOVERABLE)
        self.assertIn("tests/test_app.py::test_add", report.details["failed_tests"])

    def test_reviewer_stop_terminal_on_requirement(self) -> None:
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at="t",
            base_commit="abc",
            decision="stop",
            review_json={"stop_reason": "requirement unclear", "issues": [], "retry_prompt": ""},
        )
        report = classify_reviewer_outcome(attempt)
        assert report is not None
        self.assertEqual(report.failure_type, FailureType.REVIEWER_STOP_TERMINAL)
        self.assertEqual(report.disposition, RecoveryDisposition.TERMINAL)

    def test_reviewer_stop_fixable_with_retry_prompt(self) -> None:
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at="t",
            base_commit="abc",
            decision="stop",
            review_json={
                "stop_reason": "tests still failing",
                "issues": ["hello.txt wrong"],
                "retry_prompt": "Fix hello.txt contents",
            },
        )
        report = classify_reviewer_outcome(attempt)
        assert report is not None
        self.assertEqual(report.failure_type, FailureType.REVIEWER_STOP_FIXABLE)
        self.assertEqual(report.disposition, RecoveryDisposition.RECOVERABLE)


if __name__ == "__main__":
    unittest.main()
