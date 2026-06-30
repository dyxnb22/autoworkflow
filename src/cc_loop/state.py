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


def iteration_suffix(iteration: int, retry: int = 0) -> str:
    if retry == 0:
        return f"iter-{iteration:03d}"
    return f"iter-{iteration:03d}-retry-{retry:02d}"


def artifacts_dir(task_id: str, iteration: int, retry: int = 0, state_root: Path | None = None) -> Path:
    task_path = task_dir(task_id, state_root)
    return task_path / "artifacts" / iteration_suffix(iteration, retry)


def worktree_path(
    task_id: str,
    target_repo: Path,
    iteration: int,
    retry: int = 0,
    worktree_root: Path | None = None,
) -> Path:
    from cc_loop.git import repo_label

    root = worktree_root or DEFAULT_WORKTREE_ROOT
    repo_name = repo_label(target_repo)
    return root / repo_name / task_id / iteration_suffix(iteration, retry)


def branch_name(task_id: str, iteration: int, retry: int = 0) -> str:
    return f"cc-loop/{task_id}/{iteration_suffix(iteration, retry)}"


def plan_artifact_paths(artifact_root: Path) -> dict[str, Path]:
    """Return deterministic artifact paths for one attempt."""
    return {
        "plan_prompt": artifact_root / "plan.prompt.txt",
        "plan_raw": artifact_root / "plan.raw.jsonl",
        "plan_last_message": artifact_root / "plan.last-message.txt",
        "plan_parsed": artifact_root / "plan.parsed.json",
        "plan_provider": artifact_root / "plan.provider.txt",
        "implementer_prompt": artifact_root / "implementer.prompt.txt",
        "implementer_raw": artifact_root / "implementer.raw.json",
        "implementer_provider": artifact_root / "implementer.provider.txt",
        "test_output": artifact_root / "test.output.txt",
        "diff_stat": artifact_root / "diff.stat.txt",
        "diff_files": artifact_root / "diff.files.txt",
        "patches_dir": artifact_root / "patches",
        "review_prompt": artifact_root / "review.prompt.txt",
        "review_raw": artifact_root / "review.raw.jsonl",
        "review_last_message": artifact_root / "review.last-message.txt",
        "review_parsed": artifact_root / "review.parsed.json",
        "review_provider": artifact_root / "review.provider.txt",
        "merge_output": artifact_root / "merge.output.txt",
    }


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
    implementer_prompt_path: str = ""
    implementer_raw_path: str = ""
    implementer_exit_code: int | None = None
    implementer_provider: str = ""
    test_command: list[str] = field(default_factory=list)
    test_exit_code: int | None = None
    test_status: str = ""
    test_raw_path: str = ""
    diff_stat_path: str = ""
    diff_patch_paths: list[str] = field(default_factory=list)
    review_raw_path: str = ""
    review_json: dict[str, Any] | None = None
    review_provider: str = ""
    decision: str = ""
    merge_error: str = ""
    merge_output_path: str = ""
    failure_type: str = ""
    recovery_disposition: str = ""
    stop_reason: str = ""
    attempted_repairs: list[str] = field(default_factory=list)
    recovery_retry_count: int = 0
    failure_details: dict[str, Any] = field(default_factory=dict)
    merge_retry_count: int = 0


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
    schema_version: int = 1

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
            legacy_compat = dict(item)
            if "implementer_prompt_path" not in legacy_compat and "cursor_prompt_path" in legacy_compat:
                legacy_compat["implementer_prompt_path"] = legacy_compat["cursor_prompt_path"]
            if "implementer_raw_path" not in legacy_compat and "cursor_raw_path" in legacy_compat:
                legacy_compat["implementer_raw_path"] = legacy_compat["cursor_raw_path"]
            if "implementer_exit_code" not in legacy_compat and "cursor_exit_code" in legacy_compat:
                legacy_compat["implementer_exit_code"] = legacy_compat["cursor_exit_code"]
            legacy_compat.pop("cursor_prompt_path", None)
            legacy_compat.pop("cursor_raw_path", None)
            legacy_compat.pop("cursor_exit_code", None)
            history.append(AttemptRecord(**{**legacy_compat, "phase": phase}))
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
            schema_version=data.get("schema_version", 1),
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
        schema_version=1,
    )
