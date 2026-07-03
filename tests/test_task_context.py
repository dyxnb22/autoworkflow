"""Tests for shared task.context.json artifact and prompt references."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cc_loop.config import merge_config
from cc_loop.run import (
    TASK_IMPLEMENTER_CONTEXT_MARKER,
    TASK_REVIEW_CONTEXT_MARKER,
    build_implementer_prompt,
    build_reviewer_prompt,
)
from cc_loop.state import AttemptRecord, TaskState, TaskStatus, utc_now_iso
from cc_loop.task_context import (
    build_task_context_payload,
    prepare_task_context,
    resolve_task_context_mode,
    write_task_context_artifact,
)


def _state(*, config: dict | None = None) -> TaskState:
    return TaskState(
        task_id="ctx-task",
        goal="Implement shared task context",
        target_repo="/repo",
        base_branch="main",
        base_commit="abc",
        status=TaskStatus.RUNNING,
        iteration=1,
        config=merge_config(config or {}),
        providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
    )


def _attempt() -> AttemptRecord:
    return AttemptRecord(
        iteration=1,
        retry=0,
        created_at=utc_now_iso(),
        base_commit="abc",
        implementer_exit_code=0,
    )


class TaskContextArtifactTests(unittest.TestCase):
    def test_payload_includes_plan_for_legacy_step(self) -> None:
        state = _state()
        attempt = _attempt()
        plan_json = {
            "prompt": "Do the thing",
            "expected_changes": "thing.py",
            "acceptance_criteria": "thing works",
        }
        payload = build_task_context_payload(state=state, attempt=attempt, plan_json=plan_json)
        self.assertEqual(payload["goal"], "Implement shared task context")
        self.assertEqual(payload["plan"]["prompt"], "Do the thing")

    def test_auto_mode_uses_artifact_ref_for_codex(self) -> None:
        config = merge_config({"task_context_mode": "auto"})
        self.assertEqual(resolve_task_context_mode(config, provider="codex"), "artifact_ref")
        self.assertEqual(resolve_task_context_mode(config, provider="cursor"), "inline")

    def test_artifact_ref_prompt_references_task_context_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "task.context.json"
            state = _state(config={"task_context_mode": "artifact_ref"})
            attempt = _attempt()
            plan_json = {"prompt": "Build feature", "expected_changes": "f.py", "acceptance_criteria": "ok"}
            prepared = prepare_task_context(
                state=state,
                attempt=attempt,
                role="implementer",
                provider="codex",
                config=state.config,
                artifact_path=artifact_path,
                marker=TASK_IMPLEMENTER_CONTEXT_MARKER,
                plan_json=plan_json,
            )
            self.assertEqual(prepared.mode, "artifact_ref")
            self.assertTrue(artifact_path.is_file())
            saved = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["plan"]["prompt"], "Build feature")
            prompt = build_implementer_prompt(
                state,
                plan_json,
                attempt=attempt,
                task_context_section=prepared.prompt_section,
                config=state.config,
            )
            self.assertIn(str(artifact_path), prompt)
            self.assertNotIn("Build feature", prompt)

    def test_reviewer_artifact_ref_shares_payload_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "task.context.json"
            state = _state(config={"task_context_mode": "artifact_ref"})
            attempt = _attempt()
            payload = build_task_context_payload(state=state, attempt=attempt)
            write_task_context_artifact(artifact_path, payload)
            prepared = prepare_task_context(
                state=state,
                attempt=attempt,
                role="reviewer",
                provider="codex",
                config=state.config,
                artifact_path=artifact_path,
                marker=TASK_REVIEW_CONTEXT_MARKER,
            )
            prompt = build_reviewer_prompt(
                state=state,
                attempt=attempt,
                diff_stat=" README.md | 1 +",
                patch_body="patch",
                test_status="passed",
                config=state.config,
                task_context_section=prepared.prompt_section,
            )
            self.assertIn(str(artifact_path), prompt)
            self.assertNotIn("Implement shared task context", prompt)
