"""Luma-oriented task summary generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop import __version__
from cc_loop.config import distinct_reviewer_satisfied
from cc_loop.inspect import (
    INTEGRATION_SCHEMA_VERSION,
    SUCCESS_READY_FOR_HANDOFF,
    build_attempt_snapshot,
    build_failure_snapshot,
    build_roles_snapshot,
    derive_next_action,
    derive_success_outcome,
    is_runner_alive,
)
from cc_loop.prompt_cache import prompt_cache_snapshot
from cc_loop.recovery import AutoStep, decide_auto_step
from cc_loop.report import build_report
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    plan_artifact_paths,
    task_dir,
)
from cc_loop.execution_timeline import (
    build_execution_timeline,
    execution_timeline_path,
)
from cc_loop.task_graph import ensure_task_graph
from cc_loop.trace import trace_file_path


SUMMARY_SCHEMA_VERSION = 1
RUN_SUMMARY_FILENAME = "run.summary.json"

# Re-export for tests/importers that previously imported from summary.
__all__ = [
    "RUN_SUMMARY_FILENAME",
    "SUMMARY_SCHEMA_VERSION",
    "SUCCESS_READY_FOR_HANDOFF",
    "build_task_summary",
    "format_task_summary_human",
    "should_write_run_summary",
    "write_run_summary_if_terminal",
    "finalize_terminal_task",
    "derive_success_outcome",
]


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _subprocess_result_summary(artifact_root: Path) -> dict[str, Any] | None:
    payload = _safe_read_json(artifact_root / "subprocess.result.json")
    if payload is None:
        return None
    summary: dict[str, Any] = {}
    for phase in ("planner", "implementer", "reviewer", "test"):
        entry = payload.get(phase)
        if isinstance(entry, dict):
            summary[phase] = {
                "exit_code": entry.get("exit_code"),
                "timed_out": entry.get("timed_out"),
                "duration_seconds": entry.get("duration_seconds"),
                "killed": entry.get("killed"),
                "hung": entry.get("hung"),
            }
    return summary or None


def _reviewer_metrics_summary(metrics: dict[str, Any] | None) -> dict[str, Any] | None:
    if metrics is None:
        return None
    return {
        "layout": metrics.get("layout"),
        "stable_prefix_ratio": metrics.get("stable_prefix_ratio"),
        "contract_prefix_ratio": metrics.get("contract_prefix_ratio"),
        "task_context_ratio": metrics.get("task_context_ratio"),
        "cache_health": metrics.get("cache_health"),
        "total_prompt_cache_health": metrics.get("total_prompt_cache_health"),
        "context_mode": metrics.get("context_mode"),
        "inline_patch": metrics.get("inline_patch"),
        "inline_diff_stat": metrics.get("inline_diff_stat"),
        "omitted_patch_chars": metrics.get("omitted_patch_chars"),
        "omitted_diff_stat_chars": metrics.get("omitted_diff_stat_chars"),
        "estimated_prompt_tokens": metrics.get("estimated_prompt_tokens"),
        "estimated_avoidable_miss_tokens": metrics.get("estimated_avoidable_miss_tokens"),
    }


def _plan_summary(state: TaskState, attempt: AttemptRecord | None) -> str:
    graph = ensure_task_graph(state)
    if graph is not None and str(graph.summary or "").strip():
        return str(graph.summary).strip()[:240]
    if attempt is not None and isinstance(attempt.plan_json, dict):
        for key in ("summary", "expected_changes", "title"):
            value = str(attempt.plan_json.get(key, "") or "").strip()
            if value:
                return value[:240]
    return ""


def _diff_stat_summary(attempt: AttemptRecord | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    path = ""
    text = ""
    if attempt.diff_stat_path:
        path = attempt.diff_stat_path
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            text = ""
    preview = "\n".join(text.splitlines()[:12]).strip()
    if not path and not preview:
        return None
    return {
        "path": path or None,
        "preview": preview[:800] if preview else "",
        "branch": attempt.branch or "",
        "worktree_path": attempt.worktree_path or "",
        "head_commit": attempt.head_commit or "",
    }


def _tests_summary(attempt: AttemptRecord | None, report: dict[str, Any]) -> dict[str, Any]:
    status = ""
    reason = ""
    if attempt is not None:
        status = attempt.test_status or ""
        if status == "skipped":
            reason = "test_command not configured"
        elif status == "failed":
            reason = "test_command exited non-zero"
        elif status == "timed_out":
            reason = "test_command timed out"
        elif status == "passed":
            reason = "test_command passed"
    failure = report.get("failure_summary") or {}
    if not reason and failure.get("failure_type", "").startswith("test"):
        reason = str(failure.get("message", "") or "")[:200]
    return {
        "status": status,
        "pass": status == "passed",
        "fail": status in {"failed", "timed_out"},
        "skipped": status == "skipped",
        "reason": reason,
        "exit_code": attempt.test_exit_code if attempt is not None else None,
    }


def _latest_reject_reason(state: TaskState) -> str:
    for prev in reversed(state.history):
        if prev.phase == AttemptPhase.REJECTED and prev.review_json:
            reason = str(prev.review_json.get("reason", "") or "").strip()
            if reason:
                return reason[:400]
            retry_prompt = str(prev.review_json.get("retry_prompt", "") or "").strip()
            if retry_prompt:
                return retry_prompt[:400]
    return ""


def _artifact_key_paths(artifact_paths: dict[str, str]) -> dict[str, str]:
    preferred = (
        "plan_parsed",
        "diff_stat",
        "test_output",
        "review_parsed",
        "merge_output",
        "implementer_prompt",
        "review_prompt",
    )
    out: dict[str, str] = {}
    for key in preferred:
        value = artifact_paths.get(key)
        if value:
            out[key] = value
    return out


def build_task_summary(state: TaskState, state_root: Path) -> dict[str, Any]:
    """Build a single JSON summary for Luma and run.summary.json."""
    report = build_report(state, state_root)
    attempt = state.history[-1] if state.history else None
    artifact_paths: dict[str, str] = report.get("artifact_paths") or {}
    observability = report.get("observability") or {}

    artifact_root = Path(artifact_paths["plan_prompt"]).parent if artifact_paths.get("plan_prompt") else None
    command_argv_path = str(artifact_root / "command.argv.json") if artifact_root else ""
    attempt_trace_path = str(trace_file_path(artifact_root)) if artifact_root else ""

    review_json = (attempt.review_json or {}) if attempt is not None else {}
    latest_attempt = build_attempt_snapshot(state, attempt, state_root) if attempt else None
    if latest_attempt is not None:
        latest_attempt = {
            "iteration": latest_attempt.get("iteration", 0),
            "retry": latest_attempt.get("retry", 0),
            "phase": latest_attempt.get("phase", ""),
            "decision": latest_attempt.get("decision", ""),
            "test_status": latest_attempt.get("test_status", ""),
            "implementer_exit_code": latest_attempt.get("implementer_exit_code", 0),
            "graph_node_id": latest_attempt.get("graph_node_id", ""),
            "artifact_dir": latest_attempt.get("artifact_dir", ""),
            "branch": attempt.branch if attempt is not None else "",
            "worktree_path": attempt.worktree_path if attempt is not None else "",
        }

    metrics_path = Path(artifact_paths["review_prompt_metrics"]) if artifact_paths.get("review_prompt_metrics") else None
    reviewer_metrics = _reviewer_metrics_summary(
        _safe_read_json(metrics_path) if metrics_path is not None else None
    )
    if reviewer_metrics is None:
        reviewer_metrics = observability.get("reviewer_prompt_metrics")

    prompt_cache = observability.get("prompt_cache")
    if prompt_cache is None and artifact_paths.get("prompt_cache"):
        prompt_cache = prompt_cache_snapshot(Path(artifact_paths["prompt_cache"]))

    roles = build_roles_snapshot(state)
    running, _ = is_runner_alive(state_root, state.task_id)
    next_action = derive_next_action(
        state,
        attempt,
        running=running,
        state_root=state_root,
    )
    phase = attempt.phase.value if attempt is not None else ""
    success = derive_success_outcome(state, attempt)
    reject_reason = _latest_reject_reason(state)

    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "cc_loop_version": __version__,
        "integration_schema_version": INTEGRATION_SCHEMA_VERSION,
        "task_id": state.task_id,
        "goal": state.goal,
        "status": state.status.value,
        "phase": phase,
        "next_action": next_action,
        "base_branch": state.base_branch,
        "target_repo": state.target_repo,
        "providers": dict(state.providers),
        "roles": roles,
        "distinct_reviewer": distinct_reviewer_satisfied(state.config, state.providers),
        "require_distinct_reviewer": bool(state.config.get("require_distinct_reviewer", True)),
        "auto_merge": bool(state.config.get("auto_merge", False)),
        "plan_summary": _plan_summary(state, attempt),
        "latest_attempt": latest_attempt,
        "latest_reject_reason": reject_reason or None,
        "tests": _tests_summary(attempt, report),
        "prompt_cache": prompt_cache,
        "reviewer_prompt_metrics": reviewer_metrics,
        "subprocess_result": _subprocess_result_summary(artifact_root) if artifact_root else None,
        "command_argv_path": command_argv_path or None,
        "attempt_trace_path": attempt_trace_path or None,
        "review": {
            "decision": (report.get("review_decision") or {}).get("decision", ""),
            "reason": (report.get("review_decision") or {}).get("reason", ""),
            "issues": list(review_json.get("issues") or []),
        },
        "diff_stat": _diff_stat_summary(attempt),
        "success": success,
        "failure": report.get("failure_summary") or build_failure_snapshot(
            attempt, state_root, state.task_id, state=state
        ),
        "artifact_paths": artifact_paths or None,
        "artifacts": _artifact_key_paths(artifact_paths),
        "suggested_next_action": report.get("suggested_next_action", "") or next_action,
        "task_dir": str(task_dir(state.task_id, state_root)),
        "events_path": report.get("events_path"),
        "log_path": report.get("log_path"),
        "execution_timeline": build_execution_timeline(state, state_root),
        "execution_timeline_path": str(execution_timeline_path(state_root, state.task_id)),
    }


def format_task_summary_human(summary: dict[str, Any]) -> str:
    roles = summary.get("roles") or {}
    implementer = roles.get("implementer") or {}
    reviewer = roles.get("reviewer") or {}
    planner = roles.get("planner") or {}
    tests = summary.get("tests") or {}
    review = summary.get("review") or {}
    latest = summary.get("latest_attempt") or {}
    diff = summary.get("diff_stat") or {}

    def _role_line(label: str, role: dict[str, Any]) -> str:
        provider = role.get("provider") or "-"
        model = role.get("model") or ""
        return f"{label}: {provider}" + (f" ({model})" if model else "")

    lines = [
        f"Task summary: {summary.get('task_id', '')}",
        f"Status: {summary.get('status', '')}  success={summary.get('success', '')}",
        f"Goal: {summary.get('goal', '')}",
        "",
        "Roles",
        f"  {_role_line('planner', planner)}",
        f"  {_role_line('implementer (writes)', implementer)}",
        f"  {_role_line('reviewer (reviews)', reviewer)}",
        f"  distinct_reviewer: {summary.get('distinct_reviewer', False)}",
    ]
    if summary.get("plan_summary"):
        lines.append(f"Plan: {summary.get('plan_summary')}")
    if latest:
        lines.extend(
            [
                "",
                f"Latest attempt: iter-{latest.get('iteration', 0):03d} retry-{latest.get('retry', 0):02d}",
                f"Phase: {latest.get('phase', '')}",
                f"Tests: {tests.get('status', latest.get('test_status', ''))}"
                + (f" ({tests.get('reason')})" if tests.get("reason") else ""),
            ]
        )
        if latest.get("branch"):
            lines.append(f"Branch: {latest.get('branch')}")
    if review.get("decision"):
        lines.append(
            f"Review: {review.get('decision')}"
            + (f" — {review.get('reason')}" if review.get("reason") else "")
        )
    if summary.get("latest_reject_reason"):
        lines.append(f"Latest reject reason: {summary.get('latest_reject_reason')}")
    if diff.get("preview"):
        lines.append("Diff stat:")
        lines.extend(f"  {line}" for line in str(diff.get("preview")).splitlines()[:6])
    elif diff.get("path"):
        lines.append(f"Diff stat: {diff.get('path')}")
    lines.append(f"Handoff: {summary.get('success', '')} (auto_merge={summary.get('auto_merge', False)})")
    cache = summary.get("prompt_cache") or {}
    if cache:
        lines.append(
            "Prompt cache: "
            f"{cache.get('estimated_prompt_tokens', 0)} est. tokens, "
            f"avoidable miss {cache.get('estimated_avoidable_miss_tokens', 0)}"
        )
    if latest.get("artifact_dir"):
        lines.append(f"Artifacts: {latest.get('artifact_dir')}")
    run_summary_path = Path(summary.get("task_dir", "")) / RUN_SUMMARY_FILENAME
    if run_summary_path.is_file():
        lines.append(f"Run summary: {run_summary_path}")
    lines.append(f"Next action: {summary.get('next_action') or summary.get('suggested_next_action', '')}")
    return "\n".join(lines)


def should_write_run_summary(state: TaskState, state_root: Path) -> bool:
    if state.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return True
    if state.status != TaskStatus.STOPPED:
        return False
    attempt = state.history[-1] if state.history else None
    running, _ = is_runner_alive(state_root, state.task_id)
    artifact_paths = None
    if attempt is not None:
        artifact_paths = plan_artifact_paths(
            artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
        )
    step, _report = decide_auto_step(
        state,
        attempt,
        state.config,
        artifact_paths=artifact_paths,
        running=running,
    )
    return step in {AutoStep.TERMINAL, AutoStep.DONE}


def write_run_summary_if_terminal(state: TaskState, state_root: Path) -> Path | None:
    if not should_write_run_summary(state, state_root):
        return None
    summary = build_task_summary(state, state_root)
    path = task_dir(state.task_id, state_root) / RUN_SUMMARY_FILENAME
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def finalize_terminal_task(state: TaskState, state_root: Path) -> None:
    """Emit terminal events and write terminal artifacts when appropriate."""
    from cc_loop.events import emit_terminal_task_event
    from cc_loop.execution_timeline import write_execution_timeline_if_terminal

    emit_terminal_task_event(state_root, state)
    write_run_summary_if_terminal(state, state_root)
    write_execution_timeline_if_terminal(state, state_root)
