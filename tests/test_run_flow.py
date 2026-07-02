"""Integration tests for run, resume, review, merge, and test gating."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401 — register fake providers
from cc_loop.git import GitCommandError, GitCommandResult
from cc_loop.run import execute_resume, execute_run, prepare_run, run_implementer_phase, run_planning_phase
from cc_loop.state import AttemptPhase, TaskStatus, load_state, save_state
from cc_loop.task_graph import ensure_task_graph, graph_complete
from tests.fake_providers import FakeReviewer
from tests.helpers import TempEnv, make_task


class RunFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.repo = self.env.repo()
        self.state_root = self.env.state_root()
        self.worktree_root = self.env.worktree_root()

    def tearDown(self) -> None:
        self.env.close()

    def _patch_worktree_root(self):
        return mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.worktree_root)

    def test_success_path_merges(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root)
        with self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertEqual(attempt.phase, AttemptPhase.MERGED)
        self.assertEqual(attempt.test_status, "passed")
        self.assertEqual(attempt.decision, "approve")
        self.assertTrue((self.repo / "hello.txt").is_file())

    def test_failed_tests_block_merge_even_when_approved(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            config={"test_command": ["false"]},
        )
        with self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        self.assertEqual(state.status, TaskStatus.STOPPED)
        self.assertEqual(attempt.test_status, "failed")
        self.assertEqual(attempt.decision, "approve")
        self.assertEqual(attempt.phase, AttemptPhase.APPROVED)
        self.assertFalse((self.repo / "hello.txt").exists())

    def test_reviewer_reject_schedules_retry(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root)
        with mock.patch.object(FakeReviewer, "decision", "reject"), self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        self.assertEqual(state.status, TaskStatus.STOPPED)
        self.assertEqual(attempt.decision, "reject")
        self.assertEqual(attempt.phase, AttemptPhase.REJECTED)
        self.assertFalse((self.repo / "hello.txt").exists())

        with self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, retry_attempt, _paths = execute_resume(state, self.state_root)

        self.assertEqual(retry_attempt.retry, 1)
        self.assertEqual(retry_attempt.iteration, 1)
        self.assertEqual(retry_attempt.base_commit, state.base_commit)

    def test_reviewer_stop_leaves_inspectable_state(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root)
        with mock.patch.object(FakeReviewer, "decision", "stop"), self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, paths = execute_run(state, self.state_root)

        self.assertEqual(state.status, TaskStatus.STOPPED)
        self.assertEqual(attempt.decision, "stop")
        self.assertTrue(Path(attempt.worktree_path).is_dir())
        self.assertTrue(paths["review_parsed"].is_file())

    def test_resume_continues_after_implementer(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root)
        with self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, _attempt, paths = prepare_run(state, self.state_root)
            state = run_planning_phase(state, self.state_root, paths)
            state = run_implementer_phase(state, self.state_root, paths)
            state.status = TaskStatus.STOPPED
            save_state(state, self.state_root)

            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_resume(state, self.state_root)

        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertEqual(attempt.test_status, "passed")
        self.assertEqual(attempt.decision, "approve")
        self.assertTrue((self.repo / "hello.txt").is_file())

    def test_skipped_tests_block_merge_by_default(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            config={"test_command": []},
        )
        with self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        self.assertEqual(attempt.test_status, "skipped")
        self.assertEqual(state.status, TaskStatus.STOPPED)
        self.assertFalse((self.repo / "hello.txt").exists())

    def test_allow_merge_without_tests(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            config={"test_command": [], "allow_merge_without_tests": True},
        )
        with self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        self.assertEqual(attempt.test_status, "skipped")
        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertTrue((self.repo / "hello.txt").is_file())

    def test_merge_uses_base_branch_without_switching_main_checkout(self) -> None:
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=self.repo, check=True)
        make_task(repo=self.repo, state_root=self.state_root)

        with self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        current_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        main_file = subprocess.run(
            ["git", "show", "main:hello.txt"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertEqual(attempt.phase, AttemptPhase.MERGED)
        self.assertEqual(current_branch, "feature")
        self.assertEqual(main_file, "hello\n")
        self.assertFalse((self.repo / "hello.txt").exists())

    def test_merge_failure_is_persisted_for_resume(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root)
        git_result = GitCommandResult(
            returncode=1,
            stdout="",
            stderr="CONFLICT (add/add): Merge conflict in hello.txt\nAutomatic merge failed",
            args=("merge", "--no-ff", "-m", "cc-loop: merge branch", "cc-loop/test-task/iter-001"),
        )

        with mock.patch(
            "cc_loop.run.merge_branch_into_base",
            side_effect=GitCommandError("merge into base branch failed", result=git_result),
        ), self._patch_worktree_root():
            state = load_state("test-task", self.state_root)
            state, attempt, paths = execute_run(state, self.state_root)

        self.assertEqual(state.status, TaskStatus.STOPPED)
        self.assertEqual(attempt.phase, AttemptPhase.APPROVED)
        self.assertEqual(attempt.failure_type, "merge_conflict")
        self.assertEqual(attempt.recovery_disposition, "recoverable")
        self.assertTrue(paths["merge_output"].is_file())
        self.assertTrue((paths["plan_prompt"].parent / "failure.report.json").is_file())

    def test_task_graph_two_node_auto_flow(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="graph-task",
            config={
                "planner_provider": "fake-graph-planner",
                "implementer_provider": "fake-graph-implementer",
            },
        )
        with self._patch_worktree_root():
            from cc_loop.cli import _run_auto_loop
            import argparse

            args = argparse.Namespace(state_root=self.state_root, max_iterations=None)
            code = _run_auto_loop(args, "graph-task")

        self.assertEqual(code, 0)
        state = load_state("graph-task", self.state_root)
        graph = ensure_task_graph(state)
        self.assertIsNotNone(graph)
        assert graph is not None
        self.assertTrue(graph_complete(graph))
        self.assertEqual(state.status, TaskStatus.DONE)
        self.assertTrue((self.repo / "hello.txt").is_file())
        self.assertTrue((self.repo / "world.txt").is_file())

        node_ids = {attempt.graph_node_id for attempt in state.history if attempt.phase == AttemptPhase.MERGED}
        self.assertEqual(node_ids, {"T1", "T2"})

    def test_task_graph_first_node_passed_makes_second_runnable(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="graph-partial",
            config={
                "planner_provider": "fake-graph-planner",
                "implementer_provider": "fake-graph-implementer",
            },
        )
        with self._patch_worktree_root():
            state = load_state("graph-partial", self.state_root)
            state, attempt, _paths = execute_run(state, self.state_root)

        graph = ensure_task_graph(state)
        self.assertIsNotNone(graph)
        assert graph is not None
        self.assertEqual(attempt.graph_node_id, "T1")
        self.assertEqual(graph.nodes[0].status.value, "passed")
        self.assertEqual(state.status, TaskStatus.STOPPED)
        self.assertTrue((self.repo / "hello.txt").is_file())
        self.assertFalse((self.repo / "world.txt").exists())


if __name__ == "__main__":
    unittest.main()
