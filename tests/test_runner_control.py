"""Runner control command tests."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from cc_loop.inspect import runner_pid_path
from cc_loop.runner_control import (
    _read_proc_cmdline,
    cancel_task,
    cleanup_task,
    stop_runner,
    validate_pid_ownership,
)
from cc_loop.runner_heartbeat import RunnerHeartbeat, write_heartbeat
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

    def test_validate_pid_ownership_uses_ps_when_proc_missing(self) -> None:
        cmdline = "python -m cc_loop.cli auto --task-id ctrl-task"
        with mock.patch("cc_loop.runner_control._read_proc_cmdline", return_value=cmdline):
            self.assertTrue(validate_pid_ownership(os.getpid(), "ctrl-task"))

    def test_validate_pid_ownership_rejects_unrelated_ps_output(self) -> None:
        with mock.patch("cc_loop.runner_control._read_proc_cmdline", return_value="sleep 999"):
            self.assertFalse(validate_pid_ownership(os.getpid(), "ctrl-task"))

    def test_validate_pid_ownership_falls_back_to_heartbeat_when_ps_fails(self) -> None:
        write_heartbeat(
            self.state_root,
            RunnerHeartbeat(
                task_id="ctrl-task",
                pid=os.getpid(),
                started_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                status="running",
                phase="executing",
                iteration=1,
            ),
        )
        with mock.patch("cc_loop.runner_control._read_proc_cmdline", return_value=""):
            self.assertTrue(validate_pid_ownership(os.getpid(), "ctrl-task", state_root=self.state_root))

    def test_read_proc_cmdline_falls_back_to_ps_when_proc_unavailable(self) -> None:
        with mock.patch("cc_loop.runner_control.Path.is_file", return_value=False):
            with mock.patch("cc_loop.runner_control._read_ps_cmdline", return_value="python -m cc_loop.cli"):
                self.assertIn("cc_loop.cli", _read_proc_cmdline(os.getpid()))

    def test_read_proc_cmdline_prefers_proc_when_available(self) -> None:
        with mock.patch("cc_loop.runner_control.Path.is_file", return_value=True):
            with mock.patch(
                "cc_loop.runner_control.Path.read_bytes",
                return_value=b"python\x00-m\x00cc_loop.cli\x00",
            ):
                self.assertIn("cc_loop.cli", _read_proc_cmdline(os.getpid()))
