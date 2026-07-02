"""Multi-role routing tests."""

from __future__ import annotations

import unittest

from cc_loop.run import _aggregate_reviewer_decisions, _resolve_implementer_provider, _resolve_reviewer_chain
from cc_loop.state import AttemptRecord, TaskState, TaskStatus
from cc_loop.task_graph import GraphNode, GraphNodeKind, GraphNodeStatus, TaskGraph, effective_node_policy


class MultiRoleTests(unittest.TestCase):
    def _state_with_node(self, **node_overrides) -> tuple[TaskState, AttemptRecord]:
        node = GraphNode(
            id="T1",
            title="Docs",
            description="",
            kind=GraphNodeKind.DOCS,
            owner="implementer",
            dependencies=[],
            acceptance_criteria=[],
            files_scope=[],
            status=GraphNodeStatus.PENDING,
            attempt_iterations=[],
            retry_count=0,
            created_at="t",
            updated_at="t",
            **node_overrides,
        )
        state = TaskState(
            task_id="t",
            goal="g",
            target_repo="/r",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.RUNNING,
            iteration=1,
            config={"implementer_provider": "cursor", "reviewer_provider": "codex", "allow_merge_without_tests": False},
            providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
            task_graph=TaskGraph(1, [node], "T1"),
        )
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at="t",
            base_commit="abc",
            graph_node_id="T1",
        )
        return state, attempt

    def test_per_node_implementer_provider(self) -> None:
        state, attempt = self._state_with_node(implementer_provider="claude-code")
        self.assertEqual(_resolve_implementer_provider(state, attempt), "claude-code")

    def test_per_node_reviewer_chain(self) -> None:
        state, attempt = self._state_with_node(reviewer_providers=["fake-reviewer", "codex"])
        chain = _resolve_reviewer_chain(state, attempt)
        self.assertEqual(chain, ["fake-reviewer", "codex"])

    def test_policy_cannot_weaken_safety(self) -> None:
        state, attempt = self._state_with_node(allow_merge_without_tests=True)
        node = state.task_graph.nodes[0]
        policy = effective_node_policy(node, state.config)
        self.assertFalse(policy["allow_merge_without_tests"])

    def test_multi_reviewer_aggregation(self) -> None:
        reviews = [
            {"decision": "approve", "reason": "ok"},
            {"decision": "reject", "reason": "not ok"},
        ]
        agg = _aggregate_reviewer_decisions(reviews)
        self.assertEqual(agg["decision"], "reject")

    def test_multi_reviewer_stop_wins(self) -> None:
        reviews = [
            {"decision": "approve"},
            {"decision": "stop", "stop_reason": "halt"},
        ]
        agg = _aggregate_reviewer_decisions(reviews)
        self.assertEqual(agg["decision"], "stop")
