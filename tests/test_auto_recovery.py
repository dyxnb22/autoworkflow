"""Integration tests for auto recovery behavior."""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.cli import _run_auto_loop
from cc_loop.git import GitCommandError, GitCommandResult
from cc_loop.state import AttemptPhase, TaskStatus, load_state
from tests.helpers import TempEnv, make_task


class AutoRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.repo = self.env.repo()
        self.state_root = self.env.state_root()
        self.worktree_root = self.env.worktree_root()

    def tearDown(self) -> None:
        self.env.close()

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(state_root=self.state_root, max_iterations=None)

    def _patch_worktree_root(self):
        return mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.worktree_root)

    def test_auto_recovers_from_merge_conflict(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="merge-auto")
        git_result = GitCommandResult(
            returncode=1,
            stdout="",
            stderr="CONFLICT (add/add): Merge conflict in hello.txt",
            args=("merge",),
        )
        calls = {"count": 0}

        def merge_side_effect(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise GitCommandError("merge into base branch failed", result=git_result)
            return "merged-sha"

        with mock.patch("cc_loop.run.merge_branch_into_base", side_effect=merge_side_effect), self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "merge-auto")

        self.assertEqual(code, 0)
        state = load_state("merge-auto", self.state_root)
        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertGreaterEqual(calls["count"], 2)

    def test_auto_test_failure_enters_repair_instead_of_run_error(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="test-auto",
            config={"test_command": ["false"]},
        )
        with self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "test-auto")

        state = load_state("test-auto", self.state_root)
        attempt = state.history[-1]
        self.assertIn(attempt.failure_type, {"", "test_implementation", "recovery_budget_exhausted"})
        self.assertIn(code, {0, 1})

    def test_auto_terminal_on_environment_test_failure(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="env-auto",
            config={"test_command": ["python", "-c", "import definitely_missing_pkg_xyz"]},
        )
        with self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "env-auto")

        self.assertEqual(code, 1)
        state = load_state("env-auto", self.state_root)
        attempt = state.history[-1]
        self.assertIn(attempt.failure_type, {"test_environment", "recovery_budget_exhausted", ""})


if __name__ == "__main__":
    unittest.main()
