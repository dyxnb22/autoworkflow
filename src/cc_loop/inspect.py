"""Machine-readable task inspection for integration consumers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cc_loop import __version__
from cc_loop.failure import (
    FailureReport,
    FailureType,
    RecoveryDisposition,
    merge_blocked_by_test_gate,
    read_failure_report,
    reviewer_gate_passed,
)
from cc_loop.recovery import AutoStep, decide_auto_step, derive_next_action_from_step
from cc_loop.prompt_cache import prompt_cache_snapshot
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    plan_artifact_paths,
    task_dir,
)
from cc_loop.budgets import wall_clock_elapsed_seconds
from cc_loop.runner_heartbeat import RunnerHeartbeat, is_heartbeat_stale, read_heartbeat
from cc_loop.task_graph import build_graph_snapshot, ensure_task_graph, graph_status_summary, sync_graph_node_with_attempt

INTEGRATION_SCHEMA_VERSION = 1


def _safe_read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _reviewer_prompt_metrics_snapshot(
    state: TaskState,
    attempt: AttemptRecord | None,
    state_root: Path,
) -> dict | None:
    if attempt is None:
        return None
    paths = plan_artifact_paths(
        artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
    )
    metrics = _safe_read_json(paths["review_prompt_metrics"])
    if metrics is None:
        return None
    return {
        "layout": metrics.get("layout"),
        "stable_prefix_ratio": metrics.get("stable_prefix_ratio"),
        "contract_prefix_ratio": metrics.get("contract_prefix_ratio"),
        "cache_health": metrics.get("cache_health"),
        "total_prompt_cache_health": metrics.get("total_prompt_cache_health"),
        "estimated_prompt_tokens": metrics.get("estimated_prompt_tokens"),
        "omitted_patch_chars": metrics.get("omitted_patch_chars"),
        "estimated_avoidable_miss_tokens": metrics.get("estimated_avoidable_miss_tokens"),
        "context_mode": metrics.get("context_mode"),
        "inline_patch": metrics.get("inline_patch"),
    }


def _prompt_cache_snapshot(
    state: TaskState,
    attempt: AttemptRecord | None,
    state_root: Path,
) -> dict | None:
    if attempt is None:
        return None
    paths = plan_artifact_paths(
        artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
    )
    return prompt_cache_snapshot(paths["prompt_cache"])


_ACTIVE_PROVIDER_PHASES = frozenset(
    {
        AttemptPhase.PLANNING.value,
        AttemptPhase.EXECUTING.value,
        AttemptPhase.REVIEWING.value,
        AttemptPhase.TESTING.value,
    }
)


def _resolve_live_attempt_fields(
    attempt: AttemptRecord | None,
    *,
    running: bool,
    hb: RunnerHeartbeat | None,
    stale_seconds: int,
) -> tuple[str, str]:
    """Merge state.json attempt fields with a fresh runner heartbeat when available."""
    if attempt is None:
        return "", ""
    phase = attempt.phase.value
    running_provider = attempt.running_provider or ""
    if (
        not running
        or hb is None
        or hb.status not in {"running", "replanning"}
        or is_heartbeat_stale(hb, stale_seconds=stale_seconds)
    ):
        return phase, running_provider
    if hb.running_provider:
        running_provider = hb.running_provider
    if hb.phase in _ACTIVE_PROVIDER_PHASES:
        if phase == AttemptPhase.WORKTREE_CREATED.value or hb.running_provider:
            phase = hb.phase
    return phase, running_provider


def _provider_phase_message(phase: str, running_provider: str) -> str:
    if phase == AttemptPhase.REVIEWING.value:
        if running_provider:
            return f"Reviewer running ({running_provider})"
        return "Review in progress"
    if phase == AttemptPhase.TESTING.value:
        return "Running tests"
    if phase == AttemptPhase.EXECUTING.value:
        if running_provider:
            return f"Implementer running ({running_provider})"
        return "Implementer running"
    if phase == AttemptPhase.PLANNING.value:
        if running_provider:
            return f"Planner running ({running_provider})"
        return "Planning"
    return ""


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
    from cc_loop.runner_control import validate_pid_ownership

    pid = read_runner_pid(state_root, task_id)
    if pid is not None and is_process_alive(pid):
        return True, pid

    hb = read_heartbeat(state_root, task_id)
    if hb is not None and hb.pid and is_process_alive(hb.pid):
        if validate_pid_ownership(hb.pid, task_id, state_root=state_root):
            return True, hb.pid
    if pid is not None:
        return False, pid
    if hb is not None and hb.pid:
        return False, hb.pid
    return False, None


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
    runner_state: str = "",
) -> str:
    if runner_state == "stale_heartbeat":
        return _derive_stale_heartbeat_next_action(state, attempt, running=running)
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


def _derive_stale_heartbeat_next_action(
    state: TaskState,
    attempt: AttemptRecord | None,
    *,
    running: bool,
) -> str:
    if running:
        return "cancel"
    if state.status == TaskStatus.CANCELLED:
        return "cleanup"
    if attempt is not None and attempt.phase in {AttemptPhase.EXECUTING, AttemptPhase.PLANNING, AttemptPhase.REVIEWING}:
        return "resume"
    return "resume"


def derive_stale_heartbeat_guidance(
    *,
    running: bool,
    runner_pid: int | None,
) -> dict[str, str]:
    if running:
        return {
            "resume": "risk: may start a second provider while the existing runner is still alive",
            "cancel": "recommended: stop the hung runner and mark the task cancelled",
            "cleanup": "risk: removes runtime files but may leave a live runner process",
        }
    return {
        "resume": "recommended: no live runner detected; resume from saved phase",
        "cancel": "mark task cancelled without starting new work",
        "cleanup": "remove runner pid/heartbeat/worktrees after confirming no live process",
    }


def _empty_failure_snapshot(attempt: AttemptRecord | None = None) -> dict:
    base = {
        "failure_type": "",
        "disposition": "",
        "stop_reason": "",
        "recovery_retry_count": 0,
        "merge_retry_count": 0,
        "attempted_repairs": [],
        "suggested_actions": [],
        "details": {},
    }
    if attempt is not None:
        base["recovery_retry_count"] = attempt.recovery_retry_count
        base["merge_retry_count"] = attempt.merge_retry_count
        base["attempted_repairs"] = list(attempt.attempted_repairs)
    return base


def _attempt_indicates_failure(attempt: AttemptRecord, state: TaskState) -> bool:
    if attempt.merge_error:
        return True
    if attempt.phase == AttemptPhase.FAILED:
        return True
    if state.status == TaskStatus.FAILED:
        return True
    if attempt.decision in {"reject", "stop"}:
        return True
    if attempt.phase == AttemptPhase.REJECTED:
        return True
    if reviewer_gate_passed(attempt) and attempt.phase == AttemptPhase.APPROVED:
        if merge_blocked_by_test_gate(attempt, state.config, state=state):
            return True
        return False
    if attempt.failure_type:
        return True
    return False


def build_failure_snapshot(
    attempt: AttemptRecord | None,
    state_root: Path,
    task_id: str,
    *,
    state: TaskState | None = None,
) -> dict:
    if attempt is None:
        return _empty_failure_snapshot()

    if state is not None:
        if state.status == TaskStatus.DONE and attempt.phase == AttemptPhase.MERGED and not attempt.merge_error:
            return _empty_failure_snapshot(attempt)
        if attempt.phase == AttemptPhase.MERGED and not attempt.merge_error and not attempt.failure_type:
            return _empty_failure_snapshot(attempt)
        if (
            reviewer_gate_passed(attempt)
            and attempt.phase == AttemptPhase.APPROVED
            and not attempt.merge_error
            and not merge_blocked_by_test_gate(attempt, state.config, state=state)
        ):
            return _empty_failure_snapshot(attempt)
        if not _attempt_indicates_failure(attempt, state):
            return _empty_failure_snapshot(attempt)

    artifact_root = artifacts_dir(task_id, attempt.iteration, attempt.retry, state_root)
    report = read_failure_report(artifact_root)
    if report is not None and state is not None:
        if (
            reviewer_gate_passed(attempt)
            and attempt.phase == AttemptPhase.APPROVED
            and not attempt.merge_error
            and report.failure_type == FailureType.PATCH_NOT_CAPTURED
        ):
            report = None
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
        "graph_node_id": attempt.graph_node_id or "",
        "running_provider": attempt.running_provider or "",
    }


def derive_current_message(
    state: TaskState,
    attempt: AttemptRecord | None,
    running: bool,
    *,
    runner_state: str = "",
    live_phase: str = "",
    live_running_provider: str = "",
) -> str:
    if runner_state == "stale_heartbeat":
        if running:
            return "Runner heartbeat is stale but process is still alive — cancel or wait"
        return "Runner heartbeat is stale with no live runner — safe to resume or cleanup"
    phase = live_phase or (attempt.phase.value if attempt is not None else "")
    running_provider = live_running_provider or (
        attempt.running_provider if attempt is not None else ""
    )
    if running:
        provider_message = _provider_phase_message(phase, running_provider)
        if provider_message:
            return provider_message
        if attempt is not None and attempt.graph_node_id:
            return f"Running node {attempt.graph_node_id}"
        return "Auto runner active"
    if state.status == TaskStatus.DONE:
        return "Task completed"
    if state.status == TaskStatus.CANCELLED:
        return "Task cancelled"
    if state.status == TaskStatus.REPLANNING:
        return "Replanning task graph"
    if attempt is None:
        return "Ready to run"
    if attempt.merge_error:
        return "Merge failed — recovery available"
    if attempt.phase == AttemptPhase.MERGED:
        graph = ensure_task_graph(state)
        if graph is not None and graph_status_summary(graph)["passed"] < graph_status_summary(graph)["total"]:
            return "Node merged — more graph nodes remain"
        return "Merged successfully"
    if attempt.decision == "stop":
        return "Reviewer requested stop"
    if attempt.phase == AttemptPhase.REJECTED:
        return "Reviewer rejected — retry available"
    if attempt.phase == AttemptPhase.APPROVED:
        if state is not None and merge_blocked_by_test_gate(attempt, state.config, state=state):
            return "Approved — merge blocked by test gate"
        return "Approved — pending merge"
    provider_message = _provider_phase_message(phase, running_provider)
    if provider_message:
        return provider_message
    return f"Phase: {phase or attempt.phase.value}"


def _runner_capability_flags(state: TaskState, state_root: Path, running: bool) -> dict[str, bool]:
    return {
        "can_stop": running,
        "can_resume": state.status
        in {
            TaskStatus.STOPPED,
            TaskStatus.INTERRUPTED,
            TaskStatus.RUNNING,
            TaskStatus.REPLANNING,
        },
        "can_cleanup": state.status
        in {
            TaskStatus.STOPPED,
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INITIALIZED,
        }
        and not running,
    }


def build_status_snapshot(state: TaskState, state_root: Path) -> dict:
    attempt = state.history[-1] if state.history else None
    running, runner_pid = is_runner_alive(state_root, state.task_id)
    graph = ensure_task_graph(state)
    if graph is not None and attempt is not None:
        sync_graph_node_with_attempt(graph, attempt, max_retries=int(state.config.get("max_retries_per_step", 2)))

    stale_seconds = int(state.config.get("stale_heartbeat_seconds", 120) or 120)
    hb = read_heartbeat(state_root, state.task_id)
    caps = _runner_capability_flags(state, state_root, running)
    from cc_loop.runner_control import runner_state_label
    from cc_loop.test_command import format_test_command_display

    runner_state = runner_state_label(state_root, state.task_id, stale_heartbeat_seconds=stale_seconds)
    live_phase, live_running_provider = _resolve_live_attempt_fields(
        attempt,
        running=running,
        hb=hb,
        stale_seconds=stale_seconds,
    )
    next_action = derive_next_action(
        state,
        attempt,
        running=running,
        state_root=state_root,
        runner_state=runner_state,
    )

    snapshot = {
        "schema_version": INTEGRATION_SCHEMA_VERSION,
        "cc_loop_version": __version__,
        "task_id": state.task_id,
        "goal": state.goal,
        "target_repo": str(Path(state.target_repo).resolve()),
        "base_branch": state.base_branch,
        "base_commit": state.base_commit,
        "status": state.status.value,
        "iteration": state.iteration,
        "test_command_argv": list(state.config.get("test_command") or []),
        "test_command_display": format_test_command_display(state.config.get("test_command")),
        "attempt": build_attempt_snapshot(state, attempt, state_root),
        "failure": build_failure_snapshot(attempt, state_root, state.task_id, state=state),
        "next_action": next_action,
        "running": running,
        "runner_pid": runner_pid,
        "runner_state": runner_state,
        "last_heartbeat_at": hb.updated_at if hb else "",
        "runner_started_at": hb.started_at if hb else "",
        "elapsed_seconds": int(wall_clock_elapsed_seconds(state)),
        "log_path": str(runner_log_path(state_root, state.task_id)),
        "current_message": derive_current_message(
            state,
            attempt,
            running,
            runner_state=runner_state,
            live_phase=live_phase,
            live_running_provider=live_running_provider,
        ),
        **caps,
    }
    if attempt is not None:
        snapshot["attempt"]["phase"] = live_phase or snapshot["attempt"]["phase"]
        snapshot["attempt"]["running_provider"] = live_running_provider
    if hb is not None and not is_heartbeat_stale(hb, stale_seconds=stale_seconds):
        snapshot["heartbeat"] = {
            "phase": hb.phase,
            "running_provider": hb.running_provider,
            "updated_at": hb.updated_at,
        }
        if hb.provider_progress:
            snapshot["heartbeat"]["provider_progress"] = hb.provider_progress
    if runner_state == "stale_heartbeat":
        snapshot["stale_heartbeat_guidance"] = derive_stale_heartbeat_guidance(
            running=running,
            runner_pid=runner_pid,
        )
    if graph is not None:
        snapshot["task_graph"] = build_graph_snapshot(
            graph,
            state_providers=state.providers,
            config=dict(state.config),
        )
        if len(getattr(state, "running_attempts", None) or {}) > 1:
            snapshot["running_node_ids"] = list(state.running_attempts.keys())
    reviewer_metrics = _reviewer_prompt_metrics_snapshot(state, attempt, state_root)
    if reviewer_metrics is not None:
        snapshot["reviewer_prompt_metrics"] = reviewer_metrics
    prompt_cache = _prompt_cache_snapshot(state, attempt, state_root)
    if prompt_cache is not None:
        snapshot["prompt_cache"] = prompt_cache
    return snapshot


def format_task_graph_human(state: TaskState) -> str:
    """Human-readable task graph progress for CLI output."""
    graph = ensure_task_graph(state)
    if graph is None:
        return "No task graph for this task."

    summary = graph_status_summary(graph)
    passed = summary["passed"]
    total = summary["total"]
    lines = [
        f"Task graph: {state.task_id}",
        f"Progress: {passed}/{total} passed",
        "",
    ]
    for node in graph.nodes:
        lines.append(f"{node.id} {node.status.value:<8} {node.title}")
    return "\n".join(lines)


def state_mtime_iso(state_root: Path, task_id: str) -> str:
    path = task_dir(task_id, state_root) / "state.json"
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).replace(microsecond=0).isoformat()
