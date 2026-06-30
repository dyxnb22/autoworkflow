"""Persistent task state and artifact paths."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig, merge_config


class TaskStatus(StrEnum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    WAITING_MANUAL_REVIEW = "waiting_manual_review"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class AttemptPhase(StrEnum):
    PREFLIGHT = "preflight"
    PLANNING = "planning"
    WORKTREE_CREATED = "worktree_created"
    EXECUTING = "executing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    FAILED = "failed"


DEFAULT_STATE_ROOT = Path.home() / ".cc-loop"
DEFAULT_WORKTREE_ROOT = DEFAULT_STATE_ROOT / "worktrees"


def task_dir(task_id: str, state_root: Path | None = None) -> Path:
    root = state_root or DEFAULT_STATE_ROOT
    return root / "tasks" / task_id


def artifacts_dir(task_id: str, iteration: int, retry: int = 0, state_root: Path | None = None) -> Path:
    task_path = task_dir(task_id, state_root)
    suffix = f"iter-{iteration:03d}" if retry == 0 else f"iter-{iteration:03d}-retry-{retry:02d}"
    return task_path / "artifacts" / suffix


@dataclass
class AttemptRecord:
    iteration: int
    retry: int
    created_at: str
    base_commit: str
    head_commit: str = ""
    branch: str = ""
    worktree_path: str = ""
    phase: AttemptPhase = AttemptPhase.PREFLIGHT
    plan_raw_path: str = ""
    plan_json: dict[str, Any] | None = None
    plan_provider: str = ""
    cursor_prompt_path: str = ""
    cursor_raw_path: str = ""
    cursor_exit_code: int | None = None
    implementer_provider: str = ""
    test_command: list[str] = field(default_factory=list)
    test_exit_code: int | None = None
    test_raw_path: str = ""
    diff_stat_path: str = ""
    diff_patch_paths: list[str] = field(default_factory=list)
    review_raw_path: str = ""
    review_json: dict[str, Any] | None = None
    review_provider: str = ""
    decision: str = ""


@dataclass
class TaskState:
    task_id: str
    goal: str
    target_repo: str
    base_branch: str
    base_commit: str
    status: TaskStatus
    iteration: int
    config: LoopConfig
    history: list[AttemptRecord] = field(default_factory=list)
    providers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["history"] = [
            {**asdict(attempt), "phase": attempt.phase.value} for attempt in self.history
        ]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        history = []
        for item in data.get("history", []):
            phase = AttemptPhase(item.get("phase", AttemptPhase.PREFLIGHT.value))
            history.append(AttemptRecord(**{**item, "phase": phase}))
        return cls(
            task_id=data["task_id"],
            goal=data["goal"],
            target_repo=data["target_repo"],
            base_branch=data["base_branch"],
            base_commit=data["base_commit"],
            status=TaskStatus(data["status"]),
            iteration=data.get("iteration", 0),
            config=merge_config(data.get("config")),
            history=history,
            providers=data.get("providers", {}),
        )


def state_path(task_id: str, state_root: Path | None = None) -> Path:
    return task_dir(task_id, state_root) / "state.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_state(state: TaskState, state_root: Path | None = None) -> Path:
    path = state_path(state.task_id, state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def load_state(task_id: str, state_root: Path | None = None) -> TaskState:
    path = state_path(task_id, state_root)
    data = json.loads(path.read_text(encoding="utf-8"))
    return TaskState.from_dict(data)


def create_initial_state(
    *,
    task_id: str,
    goal: str,
    target_repo: str,
    base_branch: str,
    base_commit: str,
    config: LoopConfig | None = None,
) -> TaskState:
    merged = merge_config(config)
    return TaskState(
        task_id=task_id,
        goal=goal,
        target_repo=str(Path(target_repo).resolve()),
        base_branch=base_branch,
        base_commit=base_commit,
        status=TaskStatus.INITIALIZED,
        iteration=0,
        config=merged,
        providers={
            "planner": merged["planner_provider"],
            "reviewer": merged["reviewer_provider"],
            "implementer": merged["implementer_provider"],
        },
    )
