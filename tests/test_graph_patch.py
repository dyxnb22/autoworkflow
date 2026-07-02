"""Graph patch and replanning tests."""

from __future__ import annotations

import unittest

from cc_loop.graph_patch import GraphPatch, GraphPatchError, GraphPatchOp, apply_patch, validate_patch
from cc_loop.recovery import AutoStep, decide_auto_step
from cc_loop.state import AttemptPhase, AttemptRecord, TaskState, TaskStatus
from cc_loop.task_graph import GraphNodeStatus, graph_from_planner_json, mark_node_passed


class GraphPatchTests(unittest.TestCase):
    def test_valid_patch_applies(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                ],
            }
        )
        patch = GraphPatch(
            reason="add follow-up",
            operations=[
                GraphPatchOp(
                    op="add_node",
                    data={
                        "id": "T2",
                        "title": "B",
                        "description": "next",
                        "dependencies": ["T1"],
                    },
                )
            ],
        )
        apply_patch(graph, patch)
        self.assertEqual(len(graph.nodes), 2)

    def test_invalid_patch_rejected(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                ],
            }
        )
        mark_node_passed(graph, "T1", 1)
        patch = GraphPatch(
            reason="bad",
            operations=[
                GraphPatchOp(op="update_node", node_id="T1", data={"acceptance_criteria": ["changed"]}),
            ],
        )
        errors = validate_patch(graph, patch)
        self.assertTrue(errors)
        with self.assertRaises(GraphPatchError):
            apply_patch(graph, patch)

    def test_reviewer_replan_triggers_replan_step(self) -> None:
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at="t",
            base_commit="abc",
            phase=AttemptPhase.REJECTED,
            decision="replan",
            review_json={"decision": "replan", "replan_reason": "plan wrong"},
        )
        state = TaskState(
            task_id="t",
            goal="g",
            target_repo="/r",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.STOPPED,
            iteration=1,
            config={},
            history=[attempt],
        )
        step, report = decide_auto_step(state, attempt, state.config)
        self.assertEqual(step, AutoStep.REPLAN)
        assert report is not None
        self.assertEqual(report.stop_reason, "replan_requested")
