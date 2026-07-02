"""Event stream and report command tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from cc_loop.events import EventType, append_event, read_events
from cc_loop.report import build_report, format_report_human
from cc_loop.state import load_state
from tests.helpers import TempEnv, make_task


def _cli(*args: str, state_root) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cc_loop.cli", "--state-root", str(state_root), *args],
        capture_output=True,
        text=True,
    )


class EventsReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        make_task(repo=self.repo, state_root=self.state_root, task_id="evt-task")

    def tearDown(self) -> None:
        self.env.close()

    def test_events_schema_and_order(self) -> None:
        append_event(self.state_root, task_id="evt-task", event_type=EventType.TASK_INITIALIZED, message="init")
        append_event(self.state_root, task_id="evt-task", event_type=EventType.PLANNER_STARTED, message="plan")
        events = read_events(self.state_root, "evt-task")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].type, EventType.TASK_INITIALIZED.value)
        self.assertTrue(events[0].event_id)
        self.assertTrue(events[0].timestamp)

    def test_report_json_contract(self) -> None:
        state = load_state("evt-task", self.state_root)
        report = build_report(state, self.state_root)
        self.assertIn("task_summary", report)
        self.assertIn("suggested_next_action", report)
        self.assertIn("artifact_paths", report)

    def test_report_cli_human_smoke(self) -> None:
        result = _cli("report", "--task-id", "evt-task", state_root=self.state_root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Task report:", result.stdout)

    def test_report_cli_json(self) -> None:
        result = _cli("report", "--task-id", "evt-task", "--json", state_root=self.state_root)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_summary"]["task_id"], "evt-task")

    def test_status_includes_current_message(self) -> None:
        result = _cli("status", "--task-id", "evt-task", "--json", state_root=self.state_root)
        payload = json.loads(result.stdout)
        self.assertIn("current_message", payload)
        self.assertIn("runner_state", payload)
        self.assertIn("can_stop", payload)
