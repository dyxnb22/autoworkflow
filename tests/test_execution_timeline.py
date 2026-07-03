"""Tests for execution timeline, terminal events, and cursor progress."""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.events import EventType, append_event, emit_terminal_task_event, read_events
from cc_loop.execution_timeline import (
    EXECUTION_TIMELINE_FILENAME,
    build_execution_timeline,
    write_execution_timeline_if_terminal,
)
from cc_loop.provider_runtime import run_provider_with_heartbeat
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider
from cc_loop.runner_heartbeat import read_heartbeat
from cc_loop.state import AttemptPhase, AttemptRecord, TaskState, TaskStatus, save_state, utc_now_iso
from cc_loop.config import merge_config
from cc_loop.summary import finalize_terminal_task
from tests.helpers import TempEnv, make_task


class ExecutionTimelineTests(unittest.TestCase):
    def test_build_execution_timeline_from_events(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="timeline")
            append_event(
                env.state_root(),
                task_id="timeline",
                event_type=EventType.PLANNER_STARTED,
                iteration=1,
                retry=0,
                phase="planning",
            )
            append_event(
                env.state_root(),
                task_id="timeline",
                event_type=EventType.PLANNER_COMPLETED,
                iteration=1,
                retry=0,
                phase="planning",
            )
            from cc_loop.state import load_state

            state = load_state("timeline", env.state_root())
            payload = build_execution_timeline(state, env.state_root())
            self.assertEqual(payload["schema_version"], 1)
            self.assertGreaterEqual(payload["entry_count"], 2)
            phases = [entry["phase"] for entry in payload["entries"]]
            self.assertIn("planning", phases)
        finally:
            env.close()

    def test_execution_timeline_written_on_terminal_done(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="timeline-done")
            from cc_loop.state import load_state

            state = load_state("timeline-done", env.state_root())
            state.status = TaskStatus.DONE
            path = write_execution_timeline_if_terminal(state, env.state_root())
            self.assertIsNotNone(path)
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "done")
            self.assertEqual(path.name, EXECUTION_TIMELINE_FILENAME)
        finally:
            env.close()


class TerminalEventTests(unittest.TestCase):
    def test_emit_terminal_task_event_once(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="terminal-evt")
            from cc_loop.state import load_state

            state = load_state("terminal-evt", env.state_root())
            state.status = TaskStatus.DONE
            first = emit_terminal_task_event(env.state_root(), state)
            second = emit_terminal_task_event(env.state_root(), state)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            events = read_events(env.state_root(), "terminal-evt")
            terminal = [event for event in events if event.type == EventType.TASK_COMPLETED.value]
            self.assertEqual(len(terminal), 1)
        finally:
            env.close()

    def test_finalize_terminal_task_writes_summary_and_timeline(self) -> None:
        env = TempEnv()
        try:
            make_task(repo=env.repo(), state_root=env.state_root(), task_id="finalize")
            from cc_loop.state import load_state

            state = load_state("finalize", env.state_root())
            state.status = TaskStatus.FAILED
            finalize_terminal_task(state, env.state_root())
            task_dir = env.state_root() / "tasks" / "finalize"
            self.assertTrue((task_dir / "run.summary.json").is_file())
            self.assertTrue((task_dir / EXECUTION_TIMELINE_FILENAME).is_file())
            events = read_events(env.state_root(), "finalize")
            self.assertTrue(any(event.type == EventType.TASK_FAILED.value for event in events))
        finally:
            env.close()


@register_provider
class SlowCursorLikeProvider(ProviderAdapter):
    name = "slow-cursor-like"

    release: threading.Event | None = None

    def build_args(self, *, worktree_path, prompt, output_path, config) -> list[str]:
        return ["sleep", "1"]

    def run(self, **kwargs) -> ProviderRunResult:
        raw_path = kwargs.get("raw_output_path")
        if raw_path is not None:
            Path(raw_path).write_text("", encoding="utf-8")
        if self.release is not None:
            self.release.wait(timeout=5.0)
        kwargs["output_path"].write_text('{"result":"ok"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=kwargs["output_path"])

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


class CursorProgressTests(unittest.TestCase):
    def test_cursor_like_provider_sets_provider_progress_in_heartbeat(self) -> None:
        env = TempEnv()
        try:
            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="cursor-progress",
                config={"stale_heartbeat_seconds": 120},
            )
            release = threading.Event()
            SlowCursorLikeProvider.release = release

            state = TaskState(
                task_id="cursor-progress",
                goal="g",
                target_repo=str(env.repo()),
                base_branch="main",
                base_commit="abc",
                status=TaskStatus.RUNNING,
                iteration=1,
                config=merge_config({"stale_heartbeat_seconds": 5}),
                history=[
                    AttemptRecord(
                        iteration=1,
                        retry=0,
                        created_at=utc_now_iso(),
                        base_commit="abc",
                        phase=AttemptPhase.EXECUTING,
                        running_provider="cursor",
                    )
                ],
                providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
            )
            save_state(state, env.state_root())
            attempt = state.history[-1]
            raw_path = env.state_root() / "raw.json"
            output_path = env.state_root() / "out.json"

            @register_provider
            class CursorAlias(SlowCursorLikeProvider):
                name = "cursor"

            provider = CursorAlias()

            def _run() -> None:
                with (
                    mock.patch("cc_loop.provider_runtime.CURSOR_OUTPUT_WARN_SECONDS", 0),
                    mock.patch("cc_loop.provider_runtime.CURSOR_OUTPUT_POLL_SECONDS", 0.2),
                    mock.patch("cc_loop.provider_runtime._heartbeat_interval_seconds", return_value=0.2),
                ):
                    run_provider_with_heartbeat(
                        provider,
                        state_root=env.state_root(),
                        state=state,
                        attempt=attempt,
                        worktree_path=env.repo(),
                        prompt="implement",
                        output_path=output_path,
                        config=state.config,
                        timeout_seconds=10,
                        raw_output_path=raw_path,
                    )

            worker = threading.Thread(target=_run)
            worker.start()
            deadline = time.time() + 5.0
            progress = ""
            while time.time() < deadline:
                hb = read_heartbeat(env.state_root(), "cursor-progress")
                if hb is not None and hb.provider_progress:
                    progress = hb.provider_progress
                    break
                time.sleep(0.2)
            release.set()
            worker.join(timeout=5.0)
            self.assertIn("waiting for output", progress)
        finally:
            SlowCursorLikeProvider.release = None
            env.close()


if __name__ == "__main__":
    unittest.main()
