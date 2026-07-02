"""Parallel execution and state lock tests."""

from __future__ import annotations

import json
import threading
import unittest

import tests.fake_providers  # noqa: F401
from cc_loop.parallel_scheduler import discover_parallel_runnable, parallel_execution_enabled
from cc_loop.state import load_state, save_state
from cc_loop.state_lock import atomic_write_json, task_state_lock
from cc_loop.task_graph import graph_from_planner_json
from tests.helpers import TempEnv, make_task


class StateLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        make_task(repo=self.repo, state_root=self.state_root, task_id="lock-task")

    def tearDown(self) -> None:
        self.env.close()

    def test_atomic_write_and_lock(self) -> None:
        path = self.state_root / "tasks" / "lock-task" / "test.json"
        atomic_write_json(path, {"ok": True})
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])

        errors: list[str] = []

        def writer() -> None:
            try:
                with task_state_lock(self.state_root, "lock-task"):
                    state = load_state("lock-task", self.state_root)
                    state.iteration += 1
                    save_state(state, self.state_root)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        state = load_state("lock-task", self.state_root)
        self.assertGreaterEqual(state.iteration, 1)


class ParallelSchedulerTests(unittest.TestCase):
    def test_discover_independent_runnable_nodes(self) -> None:
        env = TempEnv()
        try:
            repo = env.repo()
            state_root = env.state_root()
            make_task(
                repo=repo,
                state_root=state_root,
                task_id="par",
                config={"max_parallel_nodes": 2},
            )
            state = load_state("par", state_root)
            state.task_graph = graph_from_planner_json(
                {
                    "mode": "task_graph",
                    "nodes": [
                        {"id": "T1", "title": "A", "description": "", "dependencies": []},
                        {"id": "T2", "title": "B", "description": "", "dependencies": []},
                    ],
                }
            )
            save_state(state, state_root)
            state = load_state("par", state_root)
            nodes = discover_parallel_runnable(state)
            self.assertEqual(len(nodes), 2)
            ids = {n.id for n in nodes}
            self.assertEqual(ids, {"T1", "T2"})
        finally:
            env.close()

    def test_parallel_execution_requires_explicit_opt_in(self) -> None:
        env = TempEnv()
        try:
            repo = env.repo()
            state_root = env.state_root()
            make_task(
                repo=repo,
                state_root=state_root,
                task_id="par-gate",
                config={"max_parallel_nodes": 2},
            )
            state = load_state("par-gate", state_root)
            self.assertFalse(parallel_execution_enabled(state))
            state.config["allow_parallel_execution"] = True
            self.assertTrue(parallel_execution_enabled(state))
        finally:
            env.close()

    def test_failed_node_blocks_dependents_only(self) -> None:
        from cc_loop.task_graph import mark_node_failed, next_runnable_nodes

        graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "nodes": [
                    {"id": "T1", "title": "A", "description": "", "dependencies": []},
                    {"id": "T2", "title": "B", "description": "", "dependencies": ["T1"]},
                    {"id": "T3", "title": "C", "description": "", "dependencies": []},
                ],
            }
        )
        mark_node_failed(graph, "T1", "failed")
        runnable = next_runnable_nodes(graph, limit=10)
        ids = {n.id for n in runnable}
        self.assertIn("T3", ids)
        self.assertNotIn("T2", ids)
