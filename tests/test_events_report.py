"""Event stream and report command tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from cc_loop.events import EventType, append_event, read_events
from cc_loop.failure import FailureReport, FailureType, RecoveryDisposition, write_failure_report
from cc_loop.report import build_report, format_report_human
from cc_loop.state import AttemptPhase, AttemptRecord, TaskStatus, artifacts_dir, load_state, save_state
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
        self.assertIn("diagnostics", report)

    def test_report_diagnostics_for_failed_attempt(self) -> None:
        state = load_state("evt-task", self.state_root)
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at="t",
            base_commit="abc",
            phase=AttemptPhase.REJECTED,
            decision="reject",
            test_status="failed",
            test_exit_code=2,
            review_json={"reason": "no mergeable diff"},
            worktree_path=str(self.repo),
        )
        state.history = [attempt]
        state.status = TaskStatus.STOPPED
        save_state(state, self.state_root)
        artifact_root = artifacts_dir("evt-task", 1, 0, self.state_root)
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "test.output.txt").write_text("ERROR collecting tests\n", encoding="utf-8")
        write_failure_report(
            artifact_root,
            FailureReport(
                failure_type=FailureType.TEST_IMPLEMENTATION,
                disposition=RecoveryDisposition.RECOVERABLE,
                message="collection failed",
                suggested_actions=["Fix imports", "Commit generated files"],
            ),
        )
        report = build_report(state, self.state_root)
        diagnostics = report.get("diagnostics") or {}
        self.assertEqual(diagnostics.get("latest_failed_phase"), "rejected")
        self.assertEqual(diagnostics.get("failure_type"), "test_implementation")
        self.assertEqual(diagnostics.get("reviewer_decision"), "reject")
        self.assertEqual(diagnostics.get("test_status"), "failed")
        self.assertGreaterEqual(len(diagnostics.get("suggested_actions") or []), 1)
        human = format_report_human(report)
        self.assertIn("Diagnosis:", human)
        self.assertIn("test.output.txt", human)

    def test_report_infers_failure_summary_without_failure_report(self) -> None:
        state = load_state("evt-task", self.state_root)
        attempt = AttemptRecord(
            iteration=1,
            retry=1,
            created_at="t",
            base_commit="abc",
            phase=AttemptPhase.REJECTED,
            decision="reject",
            review_json={"reason": "missing generated file", "retry_prompt": "add the file"},
            worktree_path=str(self.repo),
        )
        state.history = [attempt]
        state.status = TaskStatus.STOPPED
        state.config["max_retries_per_step"] = 1
        save_state(state, self.state_root)
        artifacts_dir("evt-task", 1, 1, self.state_root).mkdir(parents=True, exist_ok=True)

        report = build_report(state, self.state_root)

        self.assertEqual(report["failure_summary"]["failure_type"], "reviewer_reject")
        self.assertEqual(report["failure_summary"]["disposition"], "terminal")
        self.assertEqual(report["failure_summary"]["stop_reason"], "retry_exhausted")
        self.assertEqual(report["diagnostics"]["failure_type"], "reviewer_reject")
        self.assertIn("Inspect", report["diagnostics"]["suggested_actions"][0])

    def test_report_cli_human_smoke(self) -> None:
        result = _cli("report", "--task-id", "evt-task", state_root=self.state_root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Task report:", result.stdout)

    def test_report_cli_json(self) -> None:
        result = _cli("report", "--task-id", "evt-task", "--json", state_root=self.state_root)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_summary"]["task_id"], "evt-task")

    def test_report_cli_positional_task_id(self) -> None:
        result = _cli("report", "evt-task", "--json", state_root=self.state_root)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["task_summary"]["task_id"], "evt-task")

    def test_status_includes_current_message(self) -> None:
        result = _cli("status", "--task-id", "evt-task", "--json", state_root=self.state_root)
        payload = json.loads(result.stdout)
        self.assertIn("current_message", payload)
        self.assertIn("runner_state", payload)
        self.assertIn("can_stop", payload)
