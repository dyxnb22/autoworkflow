"""Task report generation for human and JSON output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.events import events_path, read_events
from cc_loop.failure import read_failure_report
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

    failure_summary = build_failure_snapshot(attempt, state_root, state.task_id)
    if attempt is not None and failure_summary.get("failure_type"):
        art_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
        report_obj = read_failure_report(art_root)
        if report_obj is not None:
            failure_summary["message"] = report_obj.message

    return {
        "task_summary": summary,
        "graph": graph_section,
        "latest_attempt": build_attempt_snapshot(state, attempt, state_root) if attempt else None,
        "test_result": test_result,
        "review_decision": review_decision,
        "merge_result": merge_result,
        "failure_summary": failure_summary,
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
    if failure.get("failure_type"):
        lines.append(f"Failure: {failure['failure_type']} ({failure.get('disposition', '')})")
        if failure.get("stop_reason"):
            lines.append(f"  Reason: {failure['stop_reason']}")
        lines.append("")

    lines.append(f"Suggested next action: {report.get('suggested_next_action', 'unknown')}")
    lines.append(f"Events: {report.get('event_count', 0)}")
    lines.append(f"Log: {report.get('log_path', '')}")
    return "\n".join(lines)
