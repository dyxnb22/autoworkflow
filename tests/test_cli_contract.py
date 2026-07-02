"""CLI integration contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.cli import main, resolve_task_id
from cc_loop.inspect import build_status_snapshot, runner_pid_path
from cc_loop.task_graph import graph_from_planner_json
from cc_loop.detach import spawn_detached_auto
from cc_loop.providers.claude_code import ClaudeCodeAdapter
from cc_loop.providers.claude_code import _extract_json
from cc_loop.state import load_state, save_state, state_path
from tests.helpers import TempEnv, init_git_repo, make_task


def _cli(*args: str, env: dict | None = None, state_root: Path | None = None) -> subprocess.CompletedProcess:
    argv = list(args)
    if state_root is not None:
        argv = ["--state-root", str(state_root), *argv]
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "cc_loop.cli", *argv],
        capture_output=True,
        text=True,
        env=merged,
    )


class ResolveTaskIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_explicit_task_id(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="older")
        make_task(repo=self.repo, state_root=self.state_root, task_id="newer")
        self.assertEqual(resolve_task_id(self.state_root, "older"), "older")
        self.assertIsNone(resolve_task_id(self.state_root, "missing"))

    def test_mtime_fallback_prefers_newer(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="older")
        time.sleep(0.05)
        make_task(repo=self.repo, state_root=self.state_root, task_id="newer")
        self.assertEqual(resolve_task_id(self.state_root, None), "newer")


class ListAndStatusJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_list_json_includes_tasks(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="task-a")
        make_task(repo=self.repo, state_root=self.state_root, task_id="task-b")
        result = _cli("list", "--json", state_root=self.state_root)
        self.assertEqual(result.returncode, 0)
        items = json.loads(result.stdout)
        ids = {item["task_id"] for item in items}
        self.assertEqual(ids, {"task-a", "task-b"})
        for item in items:
            self.assertIn("status", item)
            self.assertIn("target_repo", item)
            self.assertIn("phase", item)
            self.assertIn("updated_at", item)

    def test_status_json_initialized_task(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="snap-task")
        result = _cli("status", "--task-id", "snap-task", "--json", state_root=self.state_root)
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["cc_loop_version"], "0.4.0")
        self.assertEqual(payload["task_id"], "snap-task")
        self.assertEqual(payload["next_action"], "run")
        self.assertFalse(payload["running"])
        self.assertIn("attempt", payload)

    def test_status_json_mid_run_state(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="mid-task")
        state = load_state("mid-task", self.state_root)
        from cc_loop.state import AttemptPhase, AttemptRecord, TaskStatus, utc_now_iso

        state.status = TaskStatus.RUNNING
        state.iteration = 1
        state.history = [
            AttemptRecord(
                iteration=1,
                retry=0,
                created_at=utc_now_iso(),
                base_commit=state.base_commit,
                phase=AttemptPhase.EXECUTING,
            )
        ]
        save_state(state, self.state_root)
        snapshot = build_status_snapshot(load_state("mid-task", self.state_root), self.state_root)
        self.assertEqual(snapshot["attempt"]["phase"], "executing")
        self.assertEqual(snapshot["next_action"], "resume")

    def test_status_json_includes_task_graph_when_present(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="graph-status")
        state = load_state("graph-status", self.state_root)
        state.task_graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "summary": "demo",
                "nodes": [
                    {"id": "T1", "title": "One", "description": "", "dependencies": []},
                ],
            }
        )
        save_state(state, self.state_root)
        snapshot = build_status_snapshot(load_state("graph-status", self.state_root), self.state_root)
        self.assertIn("task_graph", snapshot)
        self.assertEqual(snapshot["task_graph"]["summary"]["total"], 1)

    def test_graph_command_human_and_json(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="graph-cli")
        state = load_state("graph-cli", self.state_root)
        state.task_graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "summary": "demo",
                "nodes": [
                    {"id": "T1", "title": "Set up project", "description": "", "dependencies": []},
                    {"id": "T2", "title": "Implement parser", "description": "", "dependencies": ["T1"]},
                ],
            }
        )
        save_state(state, self.state_root)

        human = _cli("graph", "--task-id", "graph-cli", state_root=self.state_root)
        self.assertEqual(human.returncode, 0)
        self.assertIn("Progress:", human.stdout)
        self.assertIn("T1", human.stdout)

        payload = _cli("graph", "--task-id", "graph-cli", "--json", state_root=self.state_root)
        self.assertEqual(payload.returncode, 0)
        data = json.loads(payload.stdout)
        self.assertIn("task_graph", data)
        self.assertEqual(data["task_graph"]["summary"]["total"], 2)


class SchemaVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_saved_state_has_schema_version(self) -> None:
        path = make_task(repo=self.repo, state_root=self.state_root, task_id="schema-task")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)

    def test_init_persists_schema_version(self) -> None:
        result = _cli(
            "init",
            "--goal",
            "test goal",
            "--repo",
            str(self.repo),
            "--task-id",
            "init-schema",
            state_root=self.state_root,
        )
        self.assertEqual(result.returncode, 0)
        data = json.loads(state_path("init-schema", self.state_root).read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_doctor_clean_repo(self) -> None:
        code = main(
            [
                "--state-root",
                str(self.state_root),
                "doctor",
                "--repo",
                str(self.repo),
                "--planner",
                "fake-planner",
                "--reviewer",
                "fake-reviewer",
                "--implementer",
                "fake-implementer",
            ]
        )
        self.assertEqual(code, 0)

    def test_doctor_dirty_repo_fails(self) -> None:
        (self.repo / "dirty.txt").write_text("x\n", encoding="utf-8")
        code = main(
            [
                "--state-root",
                str(self.state_root),
                "doctor",
                "--repo",
                str(self.repo),
                "--planner",
                "fake-planner",
            ]
        )
        self.assertEqual(code, 1)


class InitFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_init_model_and_cursor_flags(self) -> None:
        result = _cli(
            "init",
            "--goal",
            "flags",
            "--repo",
            str(self.repo),
            "--task-id",
            "flag-task",
            "--codex-model",
            "gpt-test",
            "--cursor-model",
            "cursor-test",
            "--claude-code-model",
            "claude-test",
            "--cursor-force",
            "--cursor-sandbox",
            "strict",
            state_root=self.state_root,
        )
        self.assertEqual(result.returncode, 0)
        state = load_state("flag-task", self.state_root)
        self.assertEqual(state.config["codex_model"], "gpt-test")
        self.assertEqual(state.config["cursor_model"], "cursor-test")
        self.assertEqual(state.config["claude_code_model"], "claude-test")
        self.assertTrue(state.config["cursor_force"])
        self.assertEqual(state.config["cursor_sandbox"], "strict")

    def test_init_goal_file(self) -> None:
        goal_file = self.env.root / "goal.txt"
        goal_file.write_text("from file\n", encoding="utf-8")
        result = _cli(
            "init",
            "--goal-file",
            str(goal_file),
            "--repo",
            str(self.repo),
            "--task-id",
            "goal-file-task",
            state_root=self.state_root,
        )
        self.assertEqual(result.returncode, 0)
        state = load_state("goal-file-task", self.state_root)
        self.assertEqual(state.goal, "from file")


class ClaudeCodePrintTests(unittest.TestCase):
    def test_planner_build_args_includes_print(self) -> None:
        adapter = ClaudeCodeAdapter()
        args = adapter.build_args(
            worktree_path=Path("/tmp/wt"),
            prompt="plan",
            output_path=Path("/tmp/out"),
            config={},
            print_only=True,
        )
        self.assertIn("--print", args)

    def test_extract_json_handles_nested_markdown_fences_in_string(self) -> None:
        text = '''```json
{
  "prompt": "Create files:\\n```\\nsrc/app.py\\n```",
  "expected_changes": "files",
  "acceptance_criteria": "tests pass",
  "is_final_step": true
}
```'''
        data = _extract_json(text)
        self.assertEqual(data["prompt"], "Create files:\n```\nsrc/app.py\n```")
        self.assertTrue(data["is_final_step"])

    def test_planner_parse_falls_back_for_unescaped_multiline_prompt(self) -> None:
        adapter = ClaudeCodeAdapter()
        env = TempEnv()
        try:
            output = env.root / "planner.txt"
            output.write_text(
                '''Here is the plan:
```json
{
  "prompt": "Build the project.

Create files:
```toml
[project]
name = "demo"
```
Use stable IDs like "T1".",

  "expected_changes": "pyproject.toml and src files",

  "acceptance_criteria": "1. tests pass
2. CLI works",

  "is_final_step": true
}
```''',
                encoding="utf-8",
            )
            data = adapter.parse_planner_output(output)
        finally:
            env.close()

        self.assertIn("Build the project.", data["prompt"])
        self.assertIn("[project]", data["prompt"])
        self.assertEqual(data["expected_changes"], "pyproject.toml and src files")
        self.assertIn("CLI works", data["acceptance_criteria"])
        self.assertTrue(data["is_final_step"])


    def test_planner_parse_task_graph_json(self) -> None:
        adapter = ClaudeCodeAdapter()
        env = TempEnv()
        try:
            output = env.root / "planner-graph.txt"
            output.write_text(
                '''```json
{
  "mode": "task_graph",
  "summary": "Two step plan",
  "nodes": [
    {
      "id": "T1",
      "title": "Scaffold",
      "description": "Create package skeleton",
      "kind": "implementation",
      "owner": "implementer",
      "dependencies": [],
      "acceptance_criteria": ["imports work"],
      "files_scope": ["src/"]
    }
  ],
  "is_final_step": false
}
```''',
                encoding="utf-8",
            )
            data = adapter.parse_planner_output(output)
        finally:
            env.close()

        self.assertEqual(data["mode"], "task_graph")
        self.assertEqual(len(data["nodes"]), 1)
        self.assertEqual(data["nodes"][0]["id"], "T1")


class DetachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_auto_detach_creates_pid_file(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="detach-task")

        def fake_spawn(**kwargs):
            pid_path = runner_pid_path(kwargs["state_root"], kwargs["task_id"])
            pid_path.write_text("99999\n", encoding="utf-8")
            return 99999

        with mock.patch("cc_loop.cli.spawn_detached_auto", side_effect=fake_spawn):
            code = main(
                [
                    "--state-root",
                    str(self.state_root),
                    "auto",
                    "--detach",
                    "--task-id",
                    "detach-task",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(runner_pid_path(self.state_root, "detach-task").is_file())

    def test_detached_child_puts_state_root_before_subcommand(self) -> None:
        popen_calls = []

        class FakeProcess:
            pid = 99998

        def fake_popen(argv, **kwargs):
            popen_calls.append((argv, kwargs))
            return FakeProcess()

        with mock.patch("cc_loop.detach.subprocess.Popen", side_effect=fake_popen):
            pid = spawn_detached_auto(state_root=self.state_root, task_id="detach-task")

        self.assertEqual(pid, 99998)
        argv = popen_calls[0][0]
        state_root_index = argv.index("--state-root")
        auto_index = argv.index("auto")
        self.assertLess(state_root_index, auto_index)
        self.assertEqual(argv[state_root_index + 1], str(self.state_root))
        self.assertTrue(runner_pid_path(self.state_root, "detach-task").is_file())


class EnvStateRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()

    def tearDown(self) -> None:
        self.env.close()

    def test_cc_loop_state_root_used_by_list(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="env-task")
        result = _cli("list", "--json", env={"CC_LOOP_STATE_ROOT": str(self.state_root)})
        self.assertEqual(result.returncode, 0)
        items = json.loads(result.stdout)
        self.assertEqual([item["task_id"] for item in items], ["env-task"])


if __name__ == "__main__":
    unittest.main()
