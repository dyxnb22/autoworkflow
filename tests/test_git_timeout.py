"""Tests for git command timeouts."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from cc_loop.config import merge_config
from cc_loop.git import DEFAULT_GIT_TIMEOUT_SECONDS, GitError, _run_git, _run_git_detailed, resolve_git_timeout_seconds
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


if __name__ == "__main__":
    unittest.main()
