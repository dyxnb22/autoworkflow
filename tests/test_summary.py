"""Tests for cc-loop summary command and run.summary.json artifact."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.run import execute_run
from cc_loop.state import AttemptPhase, TaskStatus, load_state
from cc_loop.summary import RUN_SUMMARY_FILENAME, build_task_summary, format_task_summary_human, write_run_summary_if_terminal
from tests.helpers import TempEnv, make_task


def _cli(*args: str, state_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cc_loop.cli", "--state-root", str(state_root), *args],
        capture_output=True,
        text=True,
        env={**os.environ},
    )


class SummaryCommandTests(unittest.TestCase):
    def test_summary_json_output(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="summary-json")
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("summary-json", env.state_root())
                execute_run(state, env.state_root())

            result = _cli("summary", "--task-id", "summary-json", "--json", state_root=env.state_root())
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["task_id"], "summary-json")
            self.assertIn("prompt_cache", payload)
            self.assertIn("reviewer_prompt_metrics", payload)
            self.assertIn("latest_attempt", payload)
            self.assertIn("artifact_paths", payload)
            self.assertIn("schema_version", payload)
            self.assertIn("execution_timeline", payload)
        finally:
            env.close()

    def test_summary_human_output(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="summary-human")
            summary = build_task_summary(load_state("summary-human", env.state_root()), env.state_root())
            text = format_task_summary_human(summary)
            self.assertIn("summary-human", text)
            self.assertIn("Status:", text)
        finally:
            env.close()

    def test_run_summary_written_after_successful_merge(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="run-summary")
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("run-summary", env.state_root())
                state, attempt, _paths = execute_run(state, env.state_root())

            self.assertIn(state.status, {TaskStatus.DONE, TaskStatus.STOPPED})
            if attempt.phase == AttemptPhase.MERGED:
                run_summary_path = env.state_root() / "tasks" / "run-summary" / RUN_SUMMARY_FILENAME
                self.assertTrue(run_summary_path.is_file())
                payload = json.loads(run_summary_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["task_id"], "run-summary")
                self.assertIn("latest_attempt", payload)
        finally:
            env.close()

    def test_write_run_summary_if_terminal_done(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="terminal-summary")
            state = load_state("terminal-summary", env.state_root())
            state.status = TaskStatus.DONE
            path = write_run_summary_if_terminal(state, env.state_root())
            self.assertIsNotNone(path)
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "done")
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
