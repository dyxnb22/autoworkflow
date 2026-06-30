"""Machine-readable task inspection for integration consumers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from cc_loop import __version__
from cc_loop.failure import FailureReport, FailureType, RecoveryDisposition, read_failure_report
from cc_loop.recovery import AutoStep, decide_auto_step, derive_next_action_from_step
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    plan_artifact_paths,
    task_dir,
)

INTEGRATION_SCHEMA_VERSION = 1


def runner_pid_path(state_root: Path, task_id: str) -> Path:
    return task_dir(task_id, state_root) / "runner.pid"


def runner_log_path(state_root: Path, task_id: str) -> Path:
    return task_dir(task_id, state_root) / "runner.log"


def read_runner_pid(state_root: Path, task_id: str) -> int | None:
    path = runner_pid_path(state_root, task_id)
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_runner_alive(state_root: Path, task_id: str) -> tuple[bool, int | None]:
    pid = read_runner_pid(state_root, task_id)
    if pid is None:
        return False, None
    if not is_process_alive(pid):
        return False, pid
    return True, pid


def clear_runner_pid_if_matches(state_root: Path, task_id: str, expected_pid: int | None = None) -> None:
    path = runner_pid_path(state_root, task_id)
    if not path.is_file():
        return
    if expected_pid is not None:
        current = read_runner_pid(state_root, task_id)
        if current != expected_pid:
            return
    try:
        path.unlink()
    except OSError:
        pass


def _artifact_paths_for_attempt(state: TaskState, attempt: AttemptRecord, state_root: Path) -> dict:
    artifact_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
    return plan_artifact_paths(artifact_root)


def derive_next_action(
    state: TaskState,
    attempt: AttemptRecord | None,
    *,
    running: bool,
    state_root: Path | None = None,
) -> str:
    artifact_paths = None
    if attempt is not None and state_root is not None:
        artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
    step, report = decide_auto_step(
        state,
        attempt,
        state.config,
        artifact_paths=artifact_paths,
        running=running,
    )
    return derive_next_action_from_step(step, report)


def build_failure_snapshot(attempt: AttemptRecord | None, state_root: Path, task_id: str) -> dict:
    if attempt is None:
        return {
            "failure_type": "",
            "disposition": "",
            "stop_reason": "",
            "recovery_retry_count": 0,
            "merge_retry_count": 0,
            "attempted_repairs": [],
            "suggested_actions": [],
            "details": {},
        }

    artifact_root = artifacts_dir(task_id, attempt.iteration, attempt.retry, state_root)
    report = read_failure_report(artifact_root)
    if report is None and attempt.failure_type:
        try:
            failure_type = FailureType(attempt.failure_type)
        except ValueError:
            failure_type = FailureType.NONE
        try:
            disposition = RecoveryDisposition(attempt.recovery_disposition)
        except ValueError:
            disposition = RecoveryDisposition.TERMINAL
        report = FailureReport(
            failure_type=failure_type,
            disposition=disposition,
            message=attempt.stop_reason,
            stop_reason=attempt.stop_reason,
            details=dict(attempt.failure_details),
            suggested_actions=[],
            attempted_repairs=list(attempt.attempted_repairs),
        )
    if report is None:
        return {
            "failure_type": "",
            "disposition": "",
            "stop_reason": "",
            "recovery_retry_count": attempt.recovery_retry_count,
            "merge_retry_count": attempt.merge_retry_count,
            "attempted_repairs": list(attempt.attempted_repairs),
            "suggested_actions": [],
            "details": {},
        }
    return {
        "failure_type": report.failure_type.value,
        "disposition": report.disposition.value,
        "stop_reason": report.stop_reason,
        "recovery_retry_count": attempt.recovery_retry_count,
        "merge_retry_count": attempt.merge_retry_count,
        "attempted_repairs": list(report.attempted_repairs),
        "suggested_actions": list(report.suggested_actions),
        "details": dict(report.details),
    }


def build_attempt_snapshot(
    state: TaskState,
    attempt: AttemptRecord | None,
    state_root: Path,
) -> dict:
    if attempt is None:
        return {
            "iteration": 0,
            "retry": 0,
            "phase": "",
            "decision": "",
            "test_status": "",
            "implementer_exit_code": 0,
            "worktree_path": "",
            "merge_error": "",
            "artifact_dir": "",
            "created_at": "",
        }

    artifact_path = str(
        artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root).resolve()
    )
    return {
        "iteration": attempt.iteration,
        "retry": attempt.retry,
        "phase": attempt.phase.value,
        "decision": attempt.decision or "",
        "test_status": attempt.test_status or "",
        "implementer_exit_code": attempt.implementer_exit_code if attempt.implementer_exit_code is not None else 0,
        "worktree_path": attempt.worktree_path or "",
        "merge_error": attempt.merge_error or "",
        "artifact_dir": artifact_path,
        "created_at": attempt.created_at or "",
    }


def build_status_snapshot(state: TaskState, state_root: Path) -> dict:
    attempt = state.history[-1] if state.history else None
    running, runner_pid = is_runner_alive(state_root, state.task_id)
    return {
        "schema_version": INTEGRATION_SCHEMA_VERSION,
        "cc_loop_version": __version__,
        "task_id": state.task_id,
        "goal": state.goal,
        "target_repo": str(Path(state.target_repo).resolve()),
        "base_branch": state.base_branch,
        "base_commit": state.base_commit,
        "status": state.status.value,
        "iteration": state.iteration,
        "attempt": build_attempt_snapshot(state, attempt, state_root),
        "failure": build_failure_snapshot(attempt, state_root, state.task_id),
        "next_action": derive_next_action(state, attempt, running=running, state_root=state_root),
        "running": running,
        "runner_pid": runner_pid,
    }


def state_mtime_iso(state_root: Path, task_id: str) -> str:
    path = task_dir(task_id, state_root) / "state.json"
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).replace(microsecond=0).isoformat()
