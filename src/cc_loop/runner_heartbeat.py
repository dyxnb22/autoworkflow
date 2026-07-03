"""Detached runner heartbeat read/write and staleness detection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from cc_loop.state import task_dir, utc_now_iso

DEFAULT_STALE_HEARTBEAT_SECONDS = 120


def heartbeat_path(state_root: Path, task_id: str) -> Path:
    return task_dir(task_id, state_root) / "runner.heartbeat.json"


@dataclass
class RunnerHeartbeat:
    task_id: str
    pid: int
    started_at: str
    updated_at: str
    status: str
    phase: str
    iteration: int
    graph_node_id: str = ""
    running_provider: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RunnerHeartbeat:
        return cls(
            task_id=str(data.get("task_id", "")),
            pid=int(data.get("pid", 0)),
            started_at=str(data.get("started_at", "")),
            updated_at=str(data.get("updated_at", "")),
            status=str(data.get("status", "")),
            phase=str(data.get("phase", "")),
            iteration=int(data.get("iteration", 0)),
            graph_node_id=str(data.get("graph_node_id", "")),
            running_provider=str(data.get("running_provider", "")),
        )


def read_heartbeat(state_root: Path, task_id: str) -> RunnerHeartbeat | None:
    path = heartbeat_path(state_root, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunnerHeartbeat.from_dict(data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def write_heartbeat(state_root: Path, heartbeat: RunnerHeartbeat) -> Path:
    from cc_loop.state_lock import atomic_write_json

    path = heartbeat_path(state_root, task_id=heartbeat.task_id)
    atomic_write_json(path, heartbeat.to_dict())
    return path


def remove_heartbeat(state_root: Path, task_id: str) -> None:
    path = heartbeat_path(state_root, task_id)
    try:
        path.unlink()
    except OSError:
        pass


def is_heartbeat_stale(
    heartbeat: RunnerHeartbeat | None,
    *,
    stale_seconds: int = DEFAULT_STALE_HEARTBEAT_SECONDS,
) -> bool:
    if heartbeat is None:
        return True
    if stale_seconds <= 0:
        return False
    try:
        updated = datetime.fromisoformat(heartbeat.updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age > stale_seconds
    except (TypeError, ValueError):
        return True


def refresh_heartbeat(
    state_root: Path,
    *,
    task_id: str,
    pid: int,
    status: str,
    phase: str,
    iteration: int,
    graph_node_id: str = "",
    running_provider: str = "",
    started_at: str | None = None,
) -> RunnerHeartbeat:
    existing = read_heartbeat(state_root, task_id)
    hb = RunnerHeartbeat(
        task_id=task_id,
        pid=pid,
        started_at=started_at or (existing.started_at if existing else utc_now_iso()),
        updated_at=utc_now_iso(),
        status=status,
        phase=phase,
        iteration=iteration,
        graph_node_id=graph_node_id,
        running_provider=running_provider,
    )
    write_heartbeat(state_root, hb)
    return hb


def mark_heartbeat_terminal(
    state_root: Path,
    task_id: str,
    *,
    status: str = "stopped",
    phase: str = "",
) -> None:
    existing = read_heartbeat(state_root, task_id)
    if existing is None:
        return
    existing.updated_at = utc_now_iso()
    existing.status = status
    if phase:
        existing.phase = phase
    write_heartbeat(state_root, existing)
