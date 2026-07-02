"""Unit tests for task graph models and dispatcher."""

from __future__ import annotations

import json
import unittest

from cc_loop.config import merge_config as merge_loop_config
from cc_loop.state import TaskState
from cc_loop.task_graph import (
    GraphNodeStatus,
    TaskGraph,
    build_graph_snapshot,
    ensure_task_graph,
    graph_complete,
    graph_from_planner_json,
    graph_status_summary,
    mark_node_failed,
    mark_node_passed,
    mark_node_rejected,
    mark_node_running,
    next_runnable_node,
)


class TaskGraphTests(unittest.TestCase):
    def test_graph_from_task_graph_planner_json(self) -> None:
        plan = {
            "mode": "task_graph",
            "summary": "Two-step plan",
            "nodes": [
                {
                    "id": "T1",
                    "title": "First",
                    "description": "Do first",
                    "kind": "implementation",
                    "owner": "implementer",
                    "dependencies": [],
                    "acceptance_criteria": ["a"],
                    "files_scope": ["a.py"],
                },
                {
                    "id": "T2",
                    "title": "Second",
                    "description": "Do second",
                    "kind": "implementation",
                    "owner": "implementer",
                    "dependencies": ["T1"],
                    "acceptance_criteria": ["b"],
                    "files_scope": ["b.py"],
                },
            ],
        }
        graph = graph_from_planner_json(plan)
        self.assertEqual(graph.summary, "Two-step plan")
        self.assertEqual(len(graph.nodes), 2)
        self.assertEqual(graph.current_node_id, "T1")
        self.assertEqual(graph.nodes[0].status, GraphNodeStatus.PENDING)

    def test_wrap_legacy_planner_json(self) -> None:
        plan = {
            "prompt": "Build feature",
            "expected_changes": "src/feature.py",
            "acceptance_criteria": "tests pass",
            "is_final_step": True,
        }
        graph = graph_from_planner_json(plan)
        self.assertEqual(len(graph.nodes), 1)
        self.assertEqual(graph.nodes[0].id, "T1")
        self.assertIn("Build feature", graph.nodes[0].description)
        self.assertEqual(graph.nodes[0].files_scope, ["src/feature.py"])

    def test_next_runnable_node_respects_dependencies(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                    {"id": "T2", "title": "B", "description": "", "dependencies": ["T1"]},
                ],
            }
        )
        first = next_runnable_node(graph)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.id, "T1")
        mark_node_running(graph, "T1")
        mark_node_passed(graph, "T1", 1)
        second = next_runnable_node(graph)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.id, "T2")

    def test_failed_dependency_blocks_downstream(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                    {"id": "T2", "title": "B", "description": "", "dependencies": ["T1"]},
                ],
            }
        )
        mark_node_failed(graph, "T1", "boom")
        self.assertIsNone(next_runnable_node(graph))
        self.assertEqual(graph.nodes[1].status, GraphNodeStatus.BLOCKED)

    def test_graph_complete_detection(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                ],
            }
        )
        self.assertFalse(graph_complete(graph))
        mark_node_passed(graph, "T1", 1)
        self.assertTrue(graph_complete(graph))

    def test_status_summary(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                    {"id": "T2", "title": "B", "description": "", "dependencies": ["T1"]},
                ],
            }
        )
        mark_node_passed(graph, "T1", 1)
        summary = graph_status_summary(graph)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["pending"], 1)

    def test_invalid_dependency_handling(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": ["MISSING"]},
                ],
            }
        )
        self.assertEqual(graph.nodes[0].status, GraphNodeStatus.BLOCKED)
        self.assertIsNone(next_runnable_node(graph))

    def test_rejected_node_retryable(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                ],
            }
        )
        mark_node_rejected(graph, "T1", "needs work")
        self.assertIsNotNone(next_runnable_node(graph, max_retries=2))
        mark_node_rejected(graph, "T1", "still bad")
        self.assertIsNone(next_runnable_node(graph, max_retries=2))

    def test_build_graph_snapshot(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "summary": "snap",
                "nodes": [{"id": "T1", "title": "A", "description": "", "dependencies": []}],
            }
        )
        snap = build_graph_snapshot(graph)
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["summary"]["total"], 1)
        self.assertEqual(snap["nodes"][0]["id"], "T1")

    def test_state_without_task_graph_loads(self) -> None:
        raw = {
            "task_id": "legacy",
            "goal": "g",
            "target_repo": "/tmp/r",
            "base_branch": "main",
            "base_commit": "abc",
            "status": "initialized",
            "iteration": 0,
            "config": merge_loop_config({}),
            "history": [],
            "providers": {},
            "schema_version": 1,
        }
        state = TaskState.from_dict(raw)
        self.assertIsNone(ensure_task_graph(state))

    def test_task_graph_round_trip_json(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [{"id": "T1", "title": "A", "description": "d", "dependencies": []}],
            }
        )
        restored = TaskGraph.from_dict(json.loads(json.dumps(graph.to_dict())))
        self.assertEqual(restored.nodes[0].title, "A")


if __name__ == "__main__":
    unittest.main()
