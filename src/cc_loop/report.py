"""Task report generation for human and JSON output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.events import events_path, read_events
from cc_loop.failure import FailureReport, classify_attempt_outcome, read_failure_report
from cc_loop.inspect import build_attempt_snapshot, build_failure_snapshot, derive_next_action
from cc_loop.recovery import decide_auto_step, derive_next_action_from_step
from cc_loop.runner_control import runner_log_path
from cc_loop.state import (
    AttemptPhase,
    TaskState,
    TaskStatus,
    artifacts_dir,
    plan_artifact_paths,
    task_dir,
)
from cc_loop.task_graph import build_graph_snapshot, ensure_task_graph, graph_status_summary
from cc_loop.trace import build_trace_snapshot, trace_file_path


def _latest_attempt(state: TaskState):
    return state.history[-1] if state.history else None


def _completed_nodes(graph) -> list[dict[str, str]]:
    return [
        {"id": n.id, "title": n.title, "status": n.status.value}
        for n in graph.nodes
        if n.status.value == "passed"
    ]


def _problem_nodes(graph) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {
        "failed": [],
        "blocked": [],
        "skipped": [],
        "rejected": [],
    }
    for n in graph.nodes:
        if n.status.value in result:
            result[n.status.value].append({"id": n.id, "title": n.title, "notes": n.notes})
    return result


def _diagnostics_section(
    state: TaskState,
    attempt,
    failure_summary: dict[str, Any],
    artifact_paths: dict[str, str],
    *,
    test_result: dict[str, Any] | None,
    review_decision: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if attempt is None:
        return None

    failure_type = failure_summary.get("failure_type", "")
    disposition = failure_summary.get("disposition", "")
    is_problem = bool(
        failure_type
        or state.status.value in {"stopped", "failed", "cancelled"}
        or (attempt.decision in {"reject", "stop"})
        or (test_result and test_result.get("status") in {"failed", "timed_out"})
    )
    if not is_problem:
        return None

    suggested = list(failure_summary.get("suggested_actions") or [])[:3]
    key_artifacts: dict[str, str] = {}
    for key in ("test_output", "review_parsed", "diff_files", "attempt_trace"):
        path = artifact_paths.get(key, "")
        if path and Path(path).is_file():
            key_artifacts[key] = path

    return {
        "latest_failed_phase": attempt.phase.value if hasattr(attempt.phase, "value") else str(attempt.phase),
        "failure_type": failure_type,
        "disposition": disposition,
        "reviewer_decision": (review_decision or {}).get("decision", ""),
        "reviewer_reason": (review_decision or {}).get("reason", ""),
        "test_status": (test_result or {}).get("status", ""),
        "test_exit_code": (test_result or {}).get("exit_code"),
        "suggested_actions": suggested,
        "key_artifact_paths": key_artifacts,
    }


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _observability_section(
    state: TaskState,
    attempt,
    state_root: Path,
    artifact_paths: dict[str, str],
) -> dict[str, Any]:
    if attempt is None:
        return {}

    artifact_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
    paths = plan_artifact_paths(artifact_root)
    _ = build_trace_snapshot(state, attempt, artifact_root, state.config)
    metrics = _safe_read_json(paths["review_prompt_metrics"])

    reviewer_metrics_summary = None
    if metrics is not None:
        reviewer_metrics_summary = {
            "layout": metrics.get("layout"),
            "stable_prefix_ratio": metrics.get("stable_prefix_ratio"),
            "estimated_prompt_tokens": metrics.get("estimated_prompt_tokens"),
        }

    return {
        "trace_path": str(trace_file_path(artifact_root)),
        "reviewer_prompt_metrics": reviewer_metrics_summary,
        "prompt_metadata_paths": {
            "planner": artifact_paths.get("plan_prompt_meta", str(paths["plan_prompt_meta"])),
            "implementer": artifact_paths.get(
                "implementer_prompt_meta", str(paths["implementer_prompt_meta"])
            ),
            "reviewer": artifact_paths.get(
                "review_prompt_meta", str(paths["review_prompt_meta"])
            ),
        },
    }


def _failure_summary_from_report(report: FailureReport, failure_summary: dict[str, Any]) -> dict[str, Any]:
    summary = dict(failure_summary)
    summary.update(
        {
            "failure_type": report.failure_type.value,
            "disposition": report.disposition.value,
            "stop_reason": report.stop_reason,
            "suggested_actions": list(report.suggested_actions),
            "details": dict(report.details),
        }
    )
    summary.setdefault("recovery_retry_count", failure_summary.get("recovery_retry_count", 0))
    summary.setdefault("merge_retry_count", failure_summary.get("merge_retry_count", 0))
    summary.setdefault("attempted_repairs", failure_summary.get("attempted_repairs", []))
    if report.message:
        summary["message"] = report.message
    return summary


def build_report(state: TaskState, state_root: Path) -> dict[str, Any]:
    attempt = _latest_attempt(state)
    running = False
    from cc_loop.inspect import is_runner_alive

    running, _ = is_runner_alive(state_root, state.task_id)
    graph = ensure_task_graph(state)
    artifact_paths: dict[str, str] = {}
    if attempt is not None:
        root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
        artifact_paths = {k: str(v) for k, v in plan_artifact_paths(root).items()}

    step, report = decide_auto_step(
        state,
        attempt,
        state.config,
        artifact_paths=plan_artifact_paths(
            artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
        )
        if attempt is not None
        else None,
        running=running,
    )
    next_action = derive_next_action_from_step(step, report)

    summary = {
        "task_id": state.task_id,
        "goal": state.goal,
        "status": state.status.value,
        "iteration": state.iteration,
        "target_repo": state.target_repo,
        "base_branch": state.base_branch,
        "base_commit": state.base_commit,
    }

    graph_section: dict[str, Any] | None = None
    if graph is not None:
        graph_section = {
            "snapshot": build_graph_snapshot(graph),
            "completed_nodes": _completed_nodes(graph),
            "problem_nodes": _problem_nodes(graph),
            "progress": graph_status_summary(graph),
        }

    test_result = None
    review_decision = None
    merge_result = None
    if attempt is not None:
        test_result = {
            "status": attempt.test_status,
            "exit_code": attempt.test_exit_code,
        }
        review_decision = {
            "decision": attempt.decision,
            "reason": (attempt.review_json or {}).get("reason", ""),
        }
        if attempt.phase == AttemptPhase.MERGED:
            merge_result = {
                "result": "merged",
                "branch": attempt.branch,
                "head_commit": attempt.head_commit,
            }
        elif attempt.merge_error:
            merge_result = {"result": "failed", "error": attempt.merge_error}

    failure_summary = build_failure_snapshot(attempt, state_root, state.task_id, state=state)
    if attempt is not None and failure_summary.get("failure_type"):
        art_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
        report_obj = read_failure_report(art_root)
        if report_obj is not None:
            failure_summary["message"] = report_obj.message
            if report_obj.suggested_actions:
                failure_summary["suggested_actions"] = list(report_obj.suggested_actions)
    elif attempt is not None:
        inferred_report = classify_attempt_outcome(
            state,
            attempt,
            plan_artifact_paths(artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)),
        )
        if inferred_report is not None:
            failure_summary = _failure_summary_from_report(inferred_report, failure_summary)
            if report is not None and report.failure_type == inferred_report.failure_type:
                failure_summary["disposition"] = report.disposition.value
                if report.stop_reason:
                    failure_summary["stop_reason"] = report.stop_reason
                if report.suggested_actions:
                    failure_summary["suggested_actions"] = list(report.suggested_actions)
                elif report.stop_reason == "retry_exhausted":
                    failure_summary["suggested_actions"] = [
                        "Inspect review.parsed.json and test.output.txt before restarting",
                        "Start a new run after fixing the rejected attempt manually",
                    ]

    diagnostics = _diagnostics_section(
        state,
        attempt,
        failure_summary,
        artifact_paths,
        test_result=test_result,
        review_decision=review_decision,
    )

    return {
        "task_summary": summary,
        "graph": graph_section,
        "latest_attempt": build_attempt_snapshot(state, attempt, state_root) if attempt else None,
        "test_result": test_result,
        "review_decision": review_decision,
        "merge_result": merge_result,
        "failure_summary": failure_summary,
        "diagnostics": diagnostics,
        "artifact_paths": artifact_paths,
        "observability": _observability_section(state, attempt, state_root, artifact_paths),
        "events_path": str(events_path(state_root, state.task_id)),
        "task_dir": str(task_dir(state.task_id, state_root)),
        "log_path": str(runner_log_path(state_root, state.task_id)),
        "suggested_next_action": next_action,
        "event_count": len(read_events(state_root, state.task_id)),
    }


def format_report_human(report: dict[str, Any]) -> str:
    summary = report["task_summary"]
    lines = [
        f"Task report: {summary['task_id']}",
        f"Status: {summary['status']}",
        f"Goal: {summary['goal']}",
        f"Iteration: {summary['iteration']}",
        "",
    ]

    graph = report.get("graph")
    if graph is not None:
        progress = graph["progress"]
        lines.append(f"Graph progress: {progress['passed']}/{progress['total']} passed")
        for node in graph["completed_nodes"]:
            lines.append(f"  ✓ {node['id']}: {node['title']}")
        problems = graph["problem_nodes"]
        for kind, nodes in problems.items():
            for node in nodes:
                lines.append(f"  {kind}: {node['id']} — {node['title']}")
        lines.append("")

    attempt = report.get("latest_attempt")
    if attempt:
        lines.append(f"Latest attempt: iter-{attempt['iteration']:03d} retry-{attempt['retry']:02d}")
        lines.append(f"  Phase: {attempt['phase']}")
        if attempt.get("graph_node_id"):
            lines.append(f"  Node: {attempt['graph_node_id']}")
        lines.append("")

    observability = report.get("observability") or {}
    reviewer_metrics = observability.get("reviewer_prompt_metrics") or {}
    if reviewer_metrics:
        ratio = reviewer_metrics.get("stable_prefix_ratio")
        tokens = reviewer_metrics.get("estimated_prompt_tokens")
        if ratio is not None:
            lines.append(f"Reviewer stable prefix ratio: {ratio}")
        if tokens is not None:
            lines.append(f"Reviewer estimated prompt tokens: {tokens}")
        lines.append("")

    failure = report.get("failure_summary") or {}
    diagnostics = report.get("diagnostics") or {}
    if diagnostics:
        lines.append("Diagnosis:")
        lines.append(f"  Phase: {diagnostics.get('latest_failed_phase', '')}")
        if diagnostics.get("failure_type"):
            lines.append(
                f"  Failure: {diagnostics['failure_type']} ({diagnostics.get('disposition', '')})"
            )
        if diagnostics.get("reviewer_decision"):
            lines.append(f"  Reviewer: {diagnostics['reviewer_decision']}")
            if diagnostics.get("reviewer_reason"):
                lines.append(f"  Reviewer reason: {diagnostics['reviewer_reason']}")
        if diagnostics.get("test_status"):
            lines.append(
                f"  Tests: {diagnostics['test_status']} (exit {diagnostics.get('test_exit_code')})"
            )
        for action in diagnostics.get("suggested_actions") or []:
            lines.append(f"  → {action}")
        key_paths = diagnostics.get("key_artifact_paths") or {}
        if key_paths:
            lines.append("  Artifacts:")
            for name, path in key_paths.items():
                lines.append(f"    {name}: {path}")
        lines.append("")
    elif failure.get("failure_type"):
        lines.append(f"Failure: {failure['failure_type']} ({failure.get('disposition', '')})")
        if failure.get("stop_reason"):
            lines.append(f"  Reason: {failure['stop_reason']}")
        lines.append("")

    lines.append(f"Suggested next action: {report.get('suggested_next_action', 'unknown')}")
    lines.append(f"Events: {report.get('event_count', 0)}")
    lines.append(f"Log: {report.get('log_path', '')}")
    return "\n".join(lines)
