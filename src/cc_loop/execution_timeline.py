"""Build task-level execution timeline from events and attempt artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.events import EventType, TaskEvent, read_events
from cc_loop.state import TaskState, artifacts_dir, plan_artifact_paths, task_dir

EXECUTION_TIMELINE_SCHEMA_VERSION = 1
EXECUTION_TIMELINE_FILENAME = "execution.timeline.json"

_EVENT_PHASE: dict[str, str] = {
    EventType.PLANNER_STARTED.value: "planning",
    EventType.PLANNER_COMPLETED.value: "planning",
    EventType.IMPLEMENTER_STARTED.value: "executing",
    EventType.IMPLEMENTER_COMPLETED.value: "executing",
    EventType.TESTS_STARTED.value: "testing",
    EventType.TESTS_COMPLETED.value: "testing",
    EventType.REVIEWER_STARTED.value: "reviewing",
    EventType.REVIEWER_COMPLETED.value: "reviewing",
    EventType.MERGE_STARTED.value: "merge",
    EventType.MERGE_COMPLETED.value: "merge",
    EventType.REPAIR_STARTED.value: "repair",
    EventType.REPAIR_COMPLETED.value: "repair",
    EventType.REPLAN_STARTED.value: "replanning",
    EventType.REPLAN_COMPLETED.value: "replanning",
    EventType.FAILURE_RECORDED.value: "failure",
}

_EVENT_ACTION: dict[str, str] = {
    EventType.PLANNER_STARTED.value: "started",
    EventType.PLANNER_COMPLETED.value: "completed",
    EventType.IMPLEMENTER_STARTED.value: "started",
    EventType.IMPLEMENTER_COMPLETED.value: "completed",
    EventType.TESTS_STARTED.value: "started",
    EventType.TESTS_COMPLETED.value: "completed",
    EventType.REVIEWER_STARTED.value: "started",
    EventType.REVIEWER_COMPLETED.value: "completed",
    EventType.MERGE_STARTED.value: "started",
    EventType.MERGE_COMPLETED.value: "completed",
    EventType.REPAIR_STARTED.value: "started",
    EventType.REPAIR_COMPLETED.value: "completed",
    EventType.REPLAN_STARTED.value: "started",
    EventType.REPLAN_COMPLETED.value: "completed",
    EventType.FAILURE_RECORDED.value: "recorded",
    EventType.TASK_COMPLETED.value: "completed",
    EventType.TASK_FAILED.value: "failed",
    EventType.TASK_CANCELLED.value: "cancelled",
    EventType.TASK_INITIALIZED.value: "initialized",
    EventType.RUNNER_STARTED.value: "started",
    EventType.RUNNER_STOPPED.value: "stopped",
}

_SUBPROCESS_PHASE_BY_EVENT: dict[str, str] = {
    EventType.PLANNER_COMPLETED.value: "planner",
    EventType.IMPLEMENTER_COMPLETED.value: "implementer",
    EventType.TESTS_COMPLETED.value: "test",
    EventType.REVIEWER_COMPLETED.value: "reviewer",
}


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _duration_ms_from_subprocess(
    state_root: Path,
    task_id: str,
    event: TaskEvent,
    *,
    subprocess_phase: str,
) -> int | None:
    artifact_root = artifacts_dir(task_id, event.iteration, event.retry, state_root)
    payload = _safe_read_json(artifact_root / "subprocess.result.json")
    if payload is None:
        return None
    entry = payload.get(subprocess_phase)
    if not isinstance(entry, dict):
        return None
    duration_seconds = entry.get("duration_seconds")
    if duration_seconds is None:
        return None
    try:
        return int(round(float(duration_seconds) * 1000))
    except (TypeError, ValueError):
        return None


def _provider_from_trace(
    state_root: Path,
    task_id: str,
    event: TaskEvent,
    *,
    provider_role: str,
) -> str:
    artifact_root = artifacts_dir(task_id, event.iteration, event.retry, state_root)
    trace = _safe_read_json(artifact_root / "attempt.trace.json")
    if trace is None:
        return ""
    providers = trace.get("providers") or {}
    if isinstance(providers, dict):
        return str(providers.get(provider_role, "") or "")
    return ""


def _timeline_entry_from_event(state_root: Path, task_id: str, event: TaskEvent) -> dict[str, Any]:
    phase = _EVENT_PHASE.get(event.type, event.phase or "")
    action = _EVENT_ACTION.get(event.type, event.type.split(".")[-1] if "." in event.type else event.type)
    entry: dict[str, Any] = {
        "timestamp": event.timestamp,
        "phase": phase,
        "event": action,
        "type": event.type,
        "iteration": event.iteration,
        "retry": event.retry,
    }
    if event.graph_node_id:
        entry["graph_node_id"] = event.graph_node_id
    if event.message:
        entry["message"] = event.message

    subprocess_phase = _SUBPROCESS_PHASE_BY_EVENT.get(event.type)
    if subprocess_phase:
        duration_ms = _duration_ms_from_subprocess(
            state_root,
            task_id,
            event,
            subprocess_phase=subprocess_phase,
        )
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms

    if event.type.endswith(".started"):
        provider_role = {
            EventType.PLANNER_STARTED.value: "planner",
            EventType.IMPLEMENTER_STARTED.value: "implementer",
            EventType.REVIEWER_STARTED.value: "reviewer",
        }.get(event.type, "")
        if provider_role:
            provider = _provider_from_trace(state_root, task_id, event, provider_role=provider_role)
            if provider:
                entry["provider"] = provider
        elif event.details.get("provider"):
            entry["provider"] = event.details["provider"]

    return entry


def build_execution_timeline(state: TaskState, state_root: Path) -> dict[str, Any]:
    """Build a task-level execution timeline from events.jsonl and attempt artifacts."""
    events = read_events(state_root, state.task_id)
    entries = [_timeline_entry_from_event(state_root, state.task_id, event) for event in events]
    return {
        "schema_version": EXECUTION_TIMELINE_SCHEMA_VERSION,
        "task_id": state.task_id,
        "status": state.status.value,
        "entry_count": len(entries),
        "entries": entries,
    }


def execution_timeline_path(state_root: Path, task_id: str) -> Path:
    return task_dir(task_id, state_root) / EXECUTION_TIMELINE_FILENAME


def write_execution_timeline_if_terminal(state: TaskState, state_root: Path) -> Path | None:
    from cc_loop.summary import should_write_run_summary

    if not should_write_run_summary(state, state_root):
        return None
    payload = build_execution_timeline(state, state_root)
    path = execution_timeline_path(state_root, state.task_id)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
