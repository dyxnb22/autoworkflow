"""Regression tests for v0.7-v0.9 closure gaps."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cc_loop.config import merge_config
from cc_loop.graph_patch import GraphPatch, GraphPatchOp, apply_patch
from cc_loop.providers.claude_code import ClaudeCodeAdapter
from cc_loop.providers.codex import CodexAdapter
from cc_loop.run import _can_auto_merge
from cc_loop.runner_control import validate_pid_ownership
from cc_loop.runner_heartbeat import RunnerHeartbeat, write_heartbeat
from cc_loop.state import AttemptPhase, AttemptRecord, TaskState, TaskStatus
from cc_loop.task_graph import graph_from_planner_json


class ClosureGapTests(unittest.TestCase):
    def test_codex_reviewer_parses_replan(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            path.write_text(
                json.dumps({"decision": "replan", "replan_reason": "bad graph"}),
                encoding="utf-8",
            )
            parsed = CodexAdapter().parse_reviewer_output(path)
            self.assertEqual(parsed["decision"], "replan")

    def test_claude_code_reviewer_parses_replan(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            path.write_text(
                json.dumps({"decision": "replan", "replan_reason": "bad graph"}),
                encoding="utf-8",
            )
            parsed = ClaudeCodeAdapter().parse_reviewer_output(path)
            self.assertEqual(parsed["decision"], "replan")

    def test_graph_patch_add_node_preserves_policy_fields(self) -> None:
        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [{"id": "T1", "title": "A", "description": "", "dependencies": []}],
            }
        )
        patch = GraphPatch(
            reason="add manual node",
            operations=[
                GraphPatchOp(
                    op="add_node",
                    data={
                        "id": "T2",
                        "title": "Manual",
                        "description": "review me",
                        "requires_manual_review": True,
                        "implementer_provider": "claude-code",
                    },
                )
            ],
        )
        apply_patch(graph, patch)
        node = graph.nodes[1]
        self.assertTrue(node.requires_manual_review)
        self.assertEqual(node.implementer_provider, "claude-code")

    def test_manual_review_blocks_auto_merge(self) -> None:
        state = TaskState(
            task_id="t",
            goal="g",
            target_repo="/r",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.RUNNING,
            iteration=1,
            config=merge_config(),
            task_graph=graph_from_planner_json(
                {
                    "mode": "task_graph",
                    "nodes": [
                        {
                            "id": "T1",
                            "title": "Manual",
                            "description": "",
                            "requires_manual_review": True,
                        }
                    ],
                }
            ),
        )
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at="t",
            base_commit="abc",
            graph_node_id="T1",
            implementer_exit_code=0,
            test_status="passed",
            decision="approve",
            phase=AttemptPhase.APPROVED,
        )
        self.assertFalse(_can_auto_merge(attempt, state.config, state=state))

    def test_validate_pid_ownership_uses_heartbeat_without_proc(self) -> None:
        with TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            task_id = "hb-task"
            (state_root / "tasks" / task_id).mkdir(parents=True)
            write_heartbeat(
                state_root,
                RunnerHeartbeat(
                    task_id=task_id,
                    pid=os.getpid(),
                    started_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                    status="running",
                    phase="running",
                    iteration=1,
                ),
            )
            with mock.patch("cc_loop.runner_control._read_proc_cmdline", return_value=""):
                self.assertTrue(
                    validate_pid_ownership(os.getpid(), task_id, state_root=state_root)
                )
                self.assertFalse(
                    validate_pid_ownership(os.getpid(), "other-task", state_root=state_root)
                )
