"""Repair prompts for recoverable failure handling."""

from __future__ import annotations

from cc_loop.failure import FailureReport, FailureType
from cc_loop.state import AttemptRecord, TaskState
from cc_loop.task_graph import ensure_task_graph, get_node


def build_repair_prompt(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    report: FailureReport,
) -> str:
    builders = {
        FailureType.MERGE_CONFLICT: _merge_repair_prompt,
        FailureType.TEST_IMPLEMENTATION: _test_repair_prompt,
        FailureType.TEST_GATE_BLOCKED: _test_repair_prompt,
        FailureType.PROVIDER_EXIT_ERROR: _provider_repair_prompt,
        FailureType.PROVIDER_TIMEOUT: _provider_repair_prompt,
        FailureType.REVIEWER_STOP_FIXABLE: _reviewer_stop_repair_prompt,
        FailureType.REVIEWER_REJECT: _reviewer_stop_repair_prompt,
    }
    builder = builders.get(report.failure_type, _generic_repair_prompt)
    return builder(state=state, attempt=attempt, report=report)


def _graph_node_context(state: TaskState, attempt: AttemptRecord) -> str:
    graph = ensure_task_graph(state)
    if graph is None or not attempt.graph_node_id:
        return ""
    node = get_node(graph, attempt.graph_node_id)
    if node is None:
        return f"Graph node: {attempt.graph_node_id}\n"
    criteria = node.acceptance_criteria or ["(none specified)"]
    return (
        f"Graph node: {node.id} — {node.title}\n"
        f"Node description: {node.description or '(none)'}\n"
        "Acceptance criteria:\n"
        + "\n".join(f"- {item}" for item in criteria)
        + "\n"
    )


def _safety_rules() -> str:
    return (
        "Safety rules:\n"
        "- Edit only files in this worktree.\n"
        "- Do not run destructive git commands (no reset --hard, checkout -- ., clean -fd).\n"
        "- Do not delete or weaken tests unless they are objectively wrong.\n"
        "- Prefer fixing implementation code over changing tests.\n"
    )


def _merge_repair_prompt(*, state: TaskState, attempt: AttemptRecord, report: FailureReport) -> str:
    conflict_files = report.details.get("conflict_files") or []
    stderr_tail = report.details.get("stderr_tail", "")
    return (
        "You are the cc-loop implementer running a merge-conflict repair.\n"
        f"{_safety_rules()}\n"
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"Iteration: {attempt.iteration} retry: {attempt.retry}\n"
        f"{_graph_node_context(state, attempt)}"
        f"Failure: {report.message}\n"
        f"Conflict files: {', '.join(conflict_files) or '(see merge output)'}\n"
        f"Merge stderr tail:\n{stderr_tail}\n"
        "Resolve the implementation issues that caused the merge conflict.\n"
        "Keep changes minimal and aligned with the task goal.\n"
    )


def _test_repair_prompt(*, state: TaskState, attempt: AttemptRecord, report: FailureReport) -> str:
    failed_tests = report.details.get("failed_tests") or []
    stderr_tail = report.details.get("stderr_tail", "")
    plan_prompt = ""
    if attempt.plan_json:
        plan_prompt = str(attempt.plan_json.get("prompt", "")).strip()
    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is not None:
            plan_prompt = node.description or plan_prompt
    return (
        "You are the cc-loop implementer running a test-failure repair.\n"
        f"{_safety_rules()}\n"
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"Iteration: {attempt.iteration} retry: {attempt.retry}\n"
        f"{_graph_node_context(state, attempt)}"
        f"Failed tests: {', '.join(failed_tests) or '(see test.output.txt)'}\n"
        f"Test output tail:\n{stderr_tail}\n"
        f"Original implementation prompt:\n{plan_prompt}\n"
        "Fix the implementation so the configured tests pass.\n"
    )


def _provider_repair_prompt(*, state: TaskState, attempt: AttemptRecord, report: FailureReport) -> str:
    return (
        "You are the cc-loop implementer running a provider-failure repair.\n"
        f"{_safety_rules()}\n"
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"{_graph_node_context(state, attempt)}"
        f"Phase: {report.details.get('phase', attempt.phase.value)}\n"
        f"Provider: {report.details.get('provider', '')}\n"
        f"Failure: {report.message}\n"
        "Continue the planned implementation and resolve the issue that caused the provider failure.\n"
    )


def _reviewer_stop_repair_prompt(*, state: TaskState, attempt: AttemptRecord, report: FailureReport) -> str:
    retry_prompt = str(report.details.get("retry_prompt", "")).strip()
    issues = report.details.get("issues") or []
    return (
        "You are the cc-loop implementer applying reviewer-requested fixes.\n"
        f"{_safety_rules()}\n"
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"{_graph_node_context(state, attempt)}"
        f"Reviewer stop reason: {report.stop_reason}\n"
        f"Issues: {issues}\n"
        f"Required changes:\n{retry_prompt or report.message}\n"
    )


def _generic_repair_prompt(*, state: TaskState, attempt: AttemptRecord, report: FailureReport) -> str:
    return (
        "You are the cc-loop implementer running a recovery repair.\n"
        f"{_safety_rules()}\n"
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"{_graph_node_context(state, attempt)}"
        f"Failure: {report.message}\n"
    )
