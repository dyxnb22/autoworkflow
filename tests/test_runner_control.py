"""Runner control command tests."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from cc_loop.inspect import runner_pid_path
from cc_loop.runner_control import cancel_task, cleanup_task, stop_runner, validate_pid_ownership
from cc_loop.state import TaskStatus, load_state, save_state, task_dir
from tests.helpers import TempEnv, make_task


class RunnerControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        make_task(repo=self.repo, state_root=self.state_root, task_id="ctrl-task")

    def tearDown(self) -> None:
        self.env.close()

    def test_stop_cleans_stale_pid(self) -> None:
        runner_pid_path(self.state_root, "ctrl-task").write_text("999999\n", encoding="utf-8")
        result = stop_runner(self.state_root, "ctrl-task")
        self.assertTrue(result.ok)
        self.assertFalse(runner_pid_path(self.state_root, "ctrl-task").is_file())

    def test_cancel_preserves_artifacts(self) -> None:
        artifact_dir = task_dir("ctrl-task", self.state_root) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        marker = artifact_dir / "marker.txt"
        marker.write_text("keep\n", encoding="utf-8")
        result = cancel_task(self.state_root, "ctrl-task")
        self.assertTrue(result.ok)
        state = load_state("ctrl-task", self.state_root)
        self.assertEqual(state.status, TaskStatus.CANCELLED)
        self.assertTrue(marker.is_file())

    def test_cleanup_does_not_delete_unrelated_worktree(self) -> None:
        unrelated = self.env.worktree_root() / "other-repo" / "other-task"
        unrelated.mkdir(parents=True)
        (unrelated / "keep.txt").write_text("x\n", encoding="utf-8")
        result = cleanup_task(self.state_root, "ctrl-task")
        self.assertTrue(result.ok)
        self.assertTrue((unrelated / "keep.txt").is_file())

    def test_validate_pid_ownership_rejects_unrelated(self) -> None:
        with mock.patch("cc_loop.runner_control._read_proc_cmdline", return_value="bash -c sleep"):
            self.assertFalse(validate_pid_ownership(os.getpid(), "ctrl-task"))
