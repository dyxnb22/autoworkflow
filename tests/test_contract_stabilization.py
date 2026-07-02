"""v0.4.x contract stabilization tests."""

from __future__ import annotations

import json
import unittest

from cc_loop.failure import FailureReport, FailureType, RecoveryDisposition, write_failure_report
from cc_loop.inspect import build_failure_snapshot, build_status_snapshot, derive_next_action
from cc_loop.recovery import decide_auto_step
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    load_state,
    save_state,
)
from cc_loop.task_graph import graph_from_planner_json
from tests.helpers import TempEnv, make_task


class StaleFailureReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_stale_failure_report_not_shown_after_merge(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="stale-fail")
        state = load_state("stale-fail", self.state_root)
        state.status = TaskStatus.DONE
        state.history = [
            AttemptRecord(
                iteration=1,
                retry=0,
                created_at="2026-01-01T00:00:00+00:00",
                base_commit="abc",
                phase=AttemptPhase.MERGED,
                decision="approve",
                test_status="passed",
            )
        ]
        save_state(state, self.state_root)
        artifact_root = artifacts_dir("stale-fail", 1, 0, self.state_root)
        artifact_root.mkdir(parents=True, exist_ok=True)
        write_failure_report(
            artifact_root,
            FailureReport(
                failure_type=FailureType.MERGE_CONFLICT,
                disposition=RecoveryDisposition.RECOVERABLE,
                message="old conflict",
            ),
        )
        snapshot = build_status_snapshot(load_state("stale-fail", self.state_root), self.state_root)
        self.assertEqual(snapshot["failure"]["failure_type"], "")
        self.assertEqual(snapshot["next_action"], "done")


class LegacyStateLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_old_state_without_task_graph_loads(self) -> None:
        path = make_task(repo=self.repo, state_root=self.state_root, task_id="legacy")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["task_graph"]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        state = load_state("legacy", self.state_root)
        self.assertIsNone(state.task_graph)

    def test_legacy_planner_json_wraps_to_graph(self) -> None:
        plan = {
            "prompt": "do work",
            "expected_changes": "file.py",
            "acceptance_criteria": "tests pass",
            "is_final_step": True,
        }
        graph = graph_from_planner_json(plan)
        self.assertEqual(len(graph.nodes), 1)
        self.assertEqual(graph.nodes[0].id, "T1")


class NextActionFromRecoveryTests(unittest.TestCase):
    def test_next_action_uses_recovery_dispatcher(self) -> None:
        state = TaskState(
            task_id="t",
            goal="g",
            target_repo="/r",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.INITIALIZED,
            iteration=0,
            config={},
            history=[],
        )
        step, _ = decide_auto_step(state, None, state.config)
        action = derive_next_action(state, None, running=False, state_root=None)
        self.assertEqual(action, "run")
