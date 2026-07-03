"""Append-only JSONL event stream for task audit."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from cc_loop.state import task_dir, utc_now_iso


class EventType(StrEnum):
    TASK_INITIALIZED = "task.initialized"
    RUNNER_STARTED = "runner.started"
    RUNNER_STOPPED = "runner.stopped"
    PLANNER_STARTED = "planner.started"
    PLANNER_COMPLETED = "planner.completed"
    GRAPH_NODE_STARTED = "graph.node_started"
    GRAPH_NODE_COMPLETED = "graph.node_completed"
    IMPLEMENTER_STARTED = "implementer.started"
    IMPLEMENTER_COMPLETED = "implementer.completed"
    TESTS_STARTED = "tests.started"
    TESTS_COMPLETED = "tests.completed"
    REVIEWER_STARTED = "reviewer.started"
    REVIEWER_COMPLETED = "reviewer.completed"
    REPAIR_STARTED = "repair.started"
    REPAIR_COMPLETED = "repair.completed"
    MERGE_STARTED = "merge.started"
    MERGE_COMPLETED = "merge.completed"
    FAILURE_RECORDED = "failure.recorded"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    REPLAN_STARTED = "replan.started"
    REPLAN_COMPLETED = "replan.completed"


def events_path(state_root: Path, task_id: str) -> Path:
    return task_dir(task_id, state_root) / "events.jsonl"


def graph_events_path(state_root: Path, task_id: str) -> Path:
    return task_dir(task_id, state_root) / "graph_events.jsonl"


@dataclass
class TaskEvent:
    event_id: str
    task_id: str
    timestamp: str
    type: str
    iteration: int = 0
    retry: int = 0
    graph_node_id: str = ""
    phase: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskEvent:
        return cls(
            event_id=str(data.get("event_id", "")),
            task_id=str(data.get("task_id", "")),
            timestamp=str(data.get("timestamp", "")),
            type=str(data.get("type", "")),
            iteration=int(data.get("iteration", 0)),
            retry=int(data.get("retry", 0)),
            graph_node_id=str(data.get("graph_node_id", "")),
            phase=str(data.get("phase", "")),
            message=str(data.get("message", "")),
            details=dict(data.get("details") or {}),
        )


def append_event(
    state_root: Path,
    *,
    task_id: str,
    event_type: EventType | str,
    iteration: int = 0,
    retry: int = 0,
    graph_node_id: str = "",
    phase: str = "",
    message: str = "",
    details: dict[str, Any] | None = None,
    stream: str = "events",
) -> TaskEvent:
    event = TaskEvent(
        event_id=uuid.uuid4().hex[:16],
        task_id=task_id,
        timestamp=utc_now_iso(),
        type=str(event_type),
        iteration=iteration,
        retry=retry,
        graph_node_id=graph_node_id,
        phase=phase,
        message=message,
        details=dict(details or {}),
    )
    path = graph_events_path(state_root, task_id) if stream == "graph" else events_path(state_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event.to_dict()) + "\n")
    return event


def read_events(state_root: Path, task_id: str, *, stream: str = "events") -> list[TaskEvent]:
    path = graph_events_path(state_root, task_id) if stream == "graph" else events_path(state_root, task_id)
    if not path.is_file():
        return []
    events: list[TaskEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(TaskEvent.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return events


_TERMINAL_EVENT_TYPES = frozenset(
    {
        EventType.TASK_COMPLETED.value,
        EventType.TASK_FAILED.value,
        EventType.TASK_CANCELLED.value,
    }
)


def latest_terminal_event_type(state_root: Path, task_id: str) -> str | None:
    for event in reversed(read_events(state_root, task_id)):
        if event.type in _TERMINAL_EVENT_TYPES:
            return event.type
    return None


def emit_terminal_task_event(state_root: Path, state: TaskState) -> TaskEvent | None:
    """Emit a terminal task event once when the task reaches a terminal disposition."""
    from cc_loop.summary import should_write_run_summary
    from cc_loop.state import TaskStatus

    if latest_terminal_event_type(state_root, state.task_id):
        return None

    if state.status == TaskStatus.DONE:
        event_type = EventType.TASK_COMPLETED
        message = "task completed"
    elif state.status == TaskStatus.CANCELLED:
        event_type = EventType.TASK_CANCELLED
        message = "task cancelled"
    elif state.status == TaskStatus.FAILED:
        event_type = EventType.TASK_FAILED
        message = "task failed"
    elif state.status == TaskStatus.STOPPED and should_write_run_summary(state, state_root):
        event_type = EventType.TASK_FAILED
        message = "task stopped (terminal)"
    else:
        return None

    attempt = state.history[-1] if state.history else None
    return append_event(
        state_root,
        task_id=state.task_id,
        event_type=event_type,
        iteration=attempt.iteration if attempt else state.iteration,
        retry=attempt.retry if attempt else 0,
        graph_node_id=attempt.graph_node_id if attempt else "",
        phase=attempt.phase.value if attempt is not None else "",
        message=message,
        details={"status": state.status.value},
    )
