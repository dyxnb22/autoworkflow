"""Runner heartbeat tests."""

from __future__ import annotations

import time
import unittest

from cc_loop.runner_heartbeat import (
    is_heartbeat_stale,
    read_heartbeat,
    refresh_heartbeat,
    remove_heartbeat,
)
from tests.helpers import TempEnv, make_task


class RunnerHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        make_task(repo=self.repo, state_root=self.state_root, task_id="hb-task")

    def tearDown(self) -> None:
        self.env.close()

    def test_write_and_read_heartbeat(self) -> None:
        hb = refresh_heartbeat(
            self.state_root,
            task_id="hb-task",
            pid=12345,
            status="running",
            phase="executing",
            iteration=1,
            graph_node_id="T1",
        )
        loaded = read_heartbeat(self.state_root, "hb-task")
        assert loaded is not None
        self.assertEqual(loaded.pid, 12345)
        self.assertEqual(loaded.graph_node_id, "T1")
        self.assertEqual(loaded.task_id, "hb-task")

    def test_stale_heartbeat_detection(self) -> None:
        hb = refresh_heartbeat(
            self.state_root,
            task_id="hb-task",
            pid=1,
            status="running",
            phase="x",
            iteration=1,
        )
        self.assertFalse(is_heartbeat_stale(hb, stale_seconds=120))
        hb.updated_at = "2020-01-01T00:00:00+00:00"
        self.assertTrue(is_heartbeat_stale(hb, stale_seconds=60))

    def test_remove_heartbeat(self) -> None:
        refresh_heartbeat(
            self.state_root,
            task_id="hb-task",
            pid=1,
            status="running",
            phase="x",
            iteration=1,
        )
        remove_heartbeat(self.state_root, "hb-task")
        self.assertIsNone(read_heartbeat(self.state_root, "hb-task"))
