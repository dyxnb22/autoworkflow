"""Tests for git command timeouts."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from cc_loop.config import merge_config
from cc_loop.git import DEFAULT_GIT_TIMEOUT_SECONDS, GitError, _run_git, _run_git_detailed, resolve_git_timeout_seconds
from cc_loop.run import _run_finalize_phase
from cc_loop.state import AttemptPhase, AttemptRecord, TaskStatus, load_state, plan_artifact_paths, save_state, utc_now_iso
from tests.helpers import TempEnv


class GitTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()

    def tearDown(self) -> None:
        self.env.close()

    def test_default_timeout_constant(self) -> None:
        self.assertEqual(DEFAULT_GIT_TIMEOUT_SECONDS, 60)

    def test_resolve_git_timeout_from_config(self) -> None:
        config = merge_config({"git_timeout_seconds": 90})
        self.assertEqual(resolve_git_timeout_seconds(config), 90)
        self.assertEqual(resolve_git_timeout_seconds(None), DEFAULT_GIT_TIMEOUT_SECONDS)

    def test_run_git_timeout_raises_git_error_with_args(self) -> None:
        repo = self.env.repo()
        with mock.patch("cc_loop.git.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=1)):
            with self.assertRaises(GitError) as ctx:
                _run_git(repo, "status", timeout_seconds=1)
        message = str(ctx.exception)
        self.assertIn("timed out after 1s", message)
        self.assertIn("git status", message)

    def test_run_git_detailed_timeout_raises_git_error(self) -> None:
        repo = self.env.repo()
        with mock.patch("cc_loop.git.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=2)):
            with self.assertRaises(GitError) as ctx:
                _run_git_detailed(repo, "rev-parse", "HEAD")
        self.assertIn("timed out after", str(ctx.exception))

    def test_successful_git_command_still_works(self) -> None:
        repo = self.env.repo()
        completed = _run_git(repo, "rev-parse", "HEAD")
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.strip())

    def test_finalize_uses_configured_git_timeout_for_commit_and_merge(self) -> None:
        from tests.helpers import make_task

        repo = self.env.repo()
        state_root = self.env.state_root()
        make_task(
            repo=repo,
            state_root=state_root,
            task_id="git-timeout-finalize",
            config={"git_timeout_seconds": 7},
        )
        state = load_state("git-timeout-finalize", state_root)
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at=utc_now_iso(),
            base_commit=state.base_commit,
            head_commit=state.base_commit,
            branch="cc-loop/test-branch",
            worktree_path=str(repo),
            phase=AttemptPhase.APPROVED,
            implementer_exit_code=0,
            test_status="passed",
            decision="approve",
            review_json={"decision": "approve", "issues": [], "retry_prompt": ""},
        )
        state.history.append(attempt)
        state.status = TaskStatus.RUNNING
        save_state(state, state_root)
        artifact_paths = plan_artifact_paths(state_root / "git-timeout-finalize" / "artifacts" / "001-00")
        artifact_paths["plan_prompt"].parent.mkdir(parents=True, exist_ok=True)

        with mock.patch("cc_loop.run.commit_worktree_changes", return_value="impl-head") as commit_mock:
            with mock.patch("cc_loop.run.merge_branch_into_base", return_value="merged-head") as merge_mock:
                _run_finalize_phase(state, state_root, attempt, artifact_paths)

        self.assertEqual(commit_mock.call_args.kwargs["timeout_seconds"], 7)
        self.assertEqual(merge_mock.call_args.kwargs["timeout_seconds"], 7)


if __name__ == "__main__":
    unittest.main()
