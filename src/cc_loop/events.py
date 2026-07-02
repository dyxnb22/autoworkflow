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
