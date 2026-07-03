"""Integration tests for auto recovery behavior."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.cli import _run_auto_loop
from cc_loop.config import LoopConfig
from cc_loop.git import GitCommandError, GitCommandResult
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider
from cc_loop.state import TaskStatus, artifacts_dir, load_state
from tests.helpers import TempEnv, make_task


@register_provider
class UntrackedOnlyImplementer(ProviderAdapter):
    name = "untracked-only-implementer"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        (worktree_path / "generated.py").write_text("value = 1\n", encoding="utf-8")
        output_path.write_text('{"result":"ok"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


class AutoRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.repo = self.env.repo()
        self.state_root = self.env.state_root()
        self.worktree_root = self.env.worktree_root()

    def tearDown(self) -> None:
        self.env.close()

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(state_root=self.state_root, max_iterations=None)

    def _patch_worktree_root(self):
        return mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.worktree_root)

    def test_auto_recovers_from_merge_conflict(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="merge-auto")
        git_result = GitCommandResult(
            returncode=1,
            stdout="",
            stderr="CONFLICT (add/add): Merge conflict in hello.txt",
            args=("merge",),
        )
        calls = {"count": 0}

        def merge_side_effect(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise GitCommandError("merge into base branch failed", result=git_result)
            return "merged-sha"

        with mock.patch("cc_loop.run.merge_branch_into_base", side_effect=merge_side_effect), self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "merge-auto")

        self.assertEqual(code, 0)
        state = load_state("merge-auto", self.state_root)
        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertGreaterEqual(calls["count"], 2)

    def test_auto_test_failure_with_reviewer_approve_stops_at_test_gate(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="test-auto",
            config={"test_command": ["false"]},
        )
        with self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "test-auto")

        self.assertEqual(code, 1)
        state = load_state("test-auto", self.state_root)
        attempt = state.history[-1]
        self.assertEqual(attempt.decision, "approve")
        self.assertEqual(attempt.failure_type, "test_gate_blocked")
        self.assertEqual(attempt.recovery_retry_count, 0)
        artifact_root = artifacts_dir("test-auto", attempt.iteration, attempt.retry, self.state_root)
        self.assertTrue((artifact_root / "failure.report.json").is_file())

    def test_auto_recovers_from_uncaptured_patch(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="patch-auto",
            config={
                "implementer_provider": "untracked-only-implementer",
                "max_recovery_attempts_per_iteration": 2,
            },
        )
        with self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "patch-auto")

        self.assertEqual(code, 0)
        state = load_state("patch-auto", self.state_root)
        attempt = state.history[-1]
        self.assertGreater(attempt.recovery_retry_count, 0)
        artifact_root = artifacts_dir("patch-auto", attempt.iteration, attempt.retry, self.state_root)
        diff_files = (artifact_root / "diff.files.txt").read_text(encoding="utf-8")
        self.assertIn("generated.py", diff_files)
        self.assertNotEqual(attempt.head_commit, attempt.base_commit)

    def test_graph_node_test_failure_with_reviewer_approve_stops_at_test_gate(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="graph-recovery",
            config={
                "planner_provider": "fake-graph-planner",
                "implementer_provider": "fake-graph-implementer",
                "test_command": ["false"],
                "max_recovery_attempts_per_iteration": 1,
            },
        )
        with self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "graph-recovery")

        self.assertEqual(code, 1)
        state = load_state("graph-recovery", self.state_root)
        attempt = state.history[-1]
        self.assertEqual(attempt.graph_node_id, "T1")
        self.assertEqual(attempt.decision, "approve")
        self.assertEqual(attempt.failure_type, "test_gate_blocked")
        graph = state.task_graph
        self.assertIsNotNone(graph)
        self.assertEqual(graph.nodes[0].status.value, "running")


if __name__ == "__main__":
    unittest.main()
