"""Run-path orchestration for cc-loop task execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.budgets import count_changed_files
from cc_loop.config import LoopConfig
from cc_loop.diff import collect_bounded_review_patches, read_diff_stat_summary
from cc_loop.failure import (
    FailureReport,
    FailureType,
    RecoveryDisposition,
    apply_report_to_attempt,
    classify_merge_failure,
    classify_provider_failure,
    clear_report_from_attempt,
    failure_report_path,
    write_failure_report,
)
from cc_loop.git import (
    GitError,
    GitCommandError,
    add_worktree,
    capture_worktree_diff_metadata,
    commit_worktree_changes,
    merge_branch_into_base,
)
from cc_loop.preflight import PreflightResult, run_preflight
from cc_loop.providers.base import ProviderRunResult, get_provider
from cc_loop.repair_prompts import build_repair_prompt
from cc_loop.events import EventType, append_event
from cc_loop.graph_patch import GraphPatch, GraphPatchError, apply_patch
from cc_loop.task_graph import effective_node_providers
from cc_loop.state import (
    DEFAULT_WORKTREE_ROOT,
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    branch_name,
    plan_artifact_paths,
    save_state,
    utc_now_iso,
    worktree_path,
)
from cc_loop.subprocess_util import run_with_timeout
from cc_loop.task_graph import (
    completed_dependency_labels,
    effective_node_policy,
    ensure_task_graph,
    get_node,
    graph_complete,
    graph_from_planner_json,
    mark_node_failed,
    mark_node_passed,
    mark_node_rejected,
    mark_node_running,
    next_runnable_node,
)


class RunError(Exception):
    """Raised when a run cannot proceed."""


class PlanningError(Exception):
    """Raised when planner execution or parsing fails."""


class ImplementingError(Exception):
    """Raised when implementer execution fails."""


class ReviewError(Exception):
    """Raised when reviewer execution or parsing fails."""


class ResumeError(Exception):
    """Raised when a task cannot be resumed."""


RESUMABLE_TASK_STATUSES = {
    TaskStatus.STOPPED,
    TaskStatus.INTERRUPTED,
    TaskStatus.RUNNING,
}


def execute_run(state: TaskState, state_root: Path) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    """Start a new iteration from an initialized task."""
    if state.status == TaskStatus.STOPPED and state.history:
        attempt = _current_attempt(state)
        if _attempt_needs_continuation(attempt):
            raise RunError(
                f"task {state.task_id} has an incomplete attempt at phase {attempt.phase.value}; "
                "use `cc-loop resume` to continue"
            )

    state, attempt, artifact_paths = prepare_run(state, state_root)
    return _run_from_phase(state, state_root, attempt, artifact_paths, start_phase=AttemptPhase.PLANNING)


def execute_resume(state: TaskState, state_root: Path) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    """Continue an interrupted or stopped task without corrupting history."""
    if state.status == TaskStatus.DONE:
        raise ResumeError(f"task {state.task_id} is already done")
    if state.status == TaskStatus.FAILED:
        raise ResumeError(
            f"task {state.task_id} is failed; inspect artifacts and re-init or fix state before resuming"
        )
    if state.status == TaskStatus.INITIALIZED:
        raise ResumeError(f"task {state.task_id} is initialized; use `cc-loop run` to start")
    if state.status not in RESUMABLE_TASK_STATUSES:
        raise ResumeError(f"task {state.task_id} status {state.status.value} is not resumable")

    if not state.history:
        raise ResumeError(f"task {state.task_id} has no attempt history to resume")

    attempt = _current_attempt(state)
    if attempt.phase == AttemptPhase.REJECTED:
        if _retry_remaining(state, attempt):
            state, attempt, artifact_paths = _begin_retry_attempt(state, state_root, attempt)
            return _run_from_phase(state, state_root, attempt, artifact_paths, start_phase=AttemptPhase.PLANNING)
        raise ResumeError(
            f"task {state.task_id} was rejected and max_retries_per_step "
            f"({state.config['max_retries_per_step']}) is exhausted"
        )

    if attempt.phase == AttemptPhase.APPROVED:
        artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
        return _run_finalize_phase(state, state_root, attempt, artifact_paths)

    if not _attempt_needs_continuation(attempt):
        raise ResumeError(
            f"task {state.task_id} attempt phase {attempt.phase.value} has no automatic next step; "
            "inspect artifacts or state.json"
        )

    artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
    state.status = TaskStatus.RUNNING
    save_state(state, state_root)
    return _run_from_phase(state, state_root, attempt, artifact_paths, start_phase=attempt.phase)


def execute_replan(
    state: TaskState,
    state_root: Path,
    report: FailureReport | None,
) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    """Run planner to produce and apply a graph patch after reviewer replan."""
    attempt = _current_attempt(state)
    artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
    append_event(
        state_root,
        task_id=state.task_id,
        event_type=EventType.REPLAN_STARTED,
        iteration=attempt.iteration,
        retry=attempt.retry,
        graph_node_id=attempt.graph_node_id,
        message="replanning task graph",
    )
    state.status = TaskStatus.REPLANNING
    attempt.phase = AttemptPhase.REPLANNING
    save_state(state, state_root)

    graph = ensure_task_graph(state)
    if graph is None:
        raise PlanningError("replan requested but task has no graph")

    config: LoopConfig = state.config
    provider_name = config["planner_provider"]
    worktree = Path(attempt.worktree_path)
    prompt = _build_replan_planner_prompt(state, attempt, report)
    artifact_paths["plan_prompt"].write_text(prompt, encoding="utf-8")

    provider = get_provider(provider_name)
    timeout_seconds = _planner_timeout_seconds(config, provider_name)
    print_only = provider_name == "claude-code"
    run_result = provider.run(
        worktree_path=worktree,
        prompt=prompt,
        output_path=artifact_paths["plan_last_message"],
        config=config,
        timeout_seconds=timeout_seconds,
        raw_output_path=artifact_paths["plan_raw"],
        print_only=print_only,
    )
    if run_result.timed_out or run_result.exit_code != 0:
        raise PlanningError(f"replan planner failed: exit={run_result.exit_code}")

    patch_json = provider.parse_planner_output(artifact_paths["plan_last_message"])
    if patch_json.get("mode") != "graph_patch":
        raise PlanningError("planner replan output must use mode=graph_patch")

    patch = GraphPatch.from_planner_json(patch_json)
    try:
        apply_patch(graph, patch)
    except GraphPatchError as exc:
        from cc_loop.failure import write_failure_report

        fail_report = FailureReport(
            failure_type=FailureType.PROVIDER_PARSE_ERROR,
            disposition=RecoveryDisposition.TERMINAL,
            message=str(exc),
            stop_reason="invalid_graph_patch",
            details=getattr(exc, "details", {}),
            suggested_actions=["Fix planner graph patch output"],
        )
        write_failure_report(artifact_paths["plan_prompt"].parent, fail_report)
        raise PlanningError(str(exc)) from exc

    append_event(
        state_root,
        task_id=state.task_id,
        event_type=EventType.REPLAN_COMPLETED,
        iteration=attempt.iteration,
        message=patch.reason,
        details={"operations": len(patch.operations)},
        stream="graph",
    )
    for op in patch.operations:
        append_event(
            state_root,
            task_id=state.task_id,
            event_type="graph.patch_applied",
            message=f"{op.op} {op.node_id}",
            details=op.to_dict(),
            stream="graph",
        )

    state.task_graph = graph
    state.status = TaskStatus.STOPPED
    attempt.phase = AttemptPhase.WORKTREE_CREATED
    save_state(state, state_root)
    return state, attempt, artifact_paths


def _build_replan_planner_prompt(
    state: TaskState,
    attempt: AttemptRecord,
    report: FailureReport | None,
) -> str:
    graph = ensure_task_graph(state)
    context = ""
    if report is not None:
        context = (
            f"\nReplan reason: {report.message}\n"
            f"Details: {json.dumps(report.details)}\n"
        )
    return (
        "You are the cc-loop planner. The reviewer requested a task graph revision.\n"
        "Respond with JSON only using mode=graph_patch:\n"
        "{\n"
        '  "mode": "graph_patch",\n'
        '  "reason": "why the graph is being changed",\n'
        '  "operations": [\n'
        '    {"op": "add_node", "data": {"id": "T3", "title": "...", ...}},\n'
        '    {"op": "update_node", "node_id": "T2", "data": {"description": "..."}},\n'
        '    {"op": "add_dependency", "node_id": "T3", "data": {"dependency": "T1"}},\n'
        '    {"op": "skip_node", "node_id": "T4", "data": {"reason": "..."}}\n'
        "  ]\n"
        "}\n"
        f"Task goal: {state.goal}\n"
        f"Current graph: {json.dumps(graph.to_dict() if graph else {})}\n"
        f"Attempt summary: phase={attempt.phase.value} test={attempt.test_status} decision={attempt.decision}\n"
        f"{context}"
    )


def _resolve_implementer_provider(state: TaskState, attempt: AttemptRecord) -> str:
    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is not None:
            return effective_node_providers(node, state.providers)["implementer"]  # type: ignore[return-value]
    return state.config["implementer_provider"]


def _resolve_reviewer_chain(state: TaskState, attempt: AttemptRecord) -> list[str]:
    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is not None:
            chain = effective_node_providers(node, state.providers)["reviewer_chain"]
            return [str(p) for p in chain if p]
    return [state.config["reviewer_provider"]]


def _aggregate_reviewer_decisions(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = [str(r.get("decision", "reject")) for r in reviews]
    if any(d == "stop" for d in decisions):
        stop_review = next(r for r in reviews if r.get("decision") == "stop")
        return dict(stop_review)
    if any(d == "replan" for d in decisions):
        replan_review = next(r for r in reviews if r.get("decision") == "replan")
        return dict(replan_review)
    if any(d == "reject" for d in decisions):
        reject_review = next(r for r in reviews if r.get("decision") == "reject")
        return dict(reject_review)
    return dict(reviews[-1]) if reviews else {"decision": "reject", "reason": "no reviewer output"}


def execute_repair_recovery(
    state: TaskState,
    state_root: Path,
    report: FailureReport,
) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    """Run implementer repair and continue test → review → finalize on the same attempt."""
    attempt = _current_attempt(state)
    artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
    repair_label = f"implementer_repair:{report.failure_type.value}"
    if repair_label not in report.attempted_repairs:
        report.attempted_repairs.append(repair_label)
    attempt.attempted_repairs = list(report.attempted_repairs)
    apply_report_to_attempt(attempt, report)
    write_failure_report(artifact_paths["plan_prompt"].parent, report)
    _reset_attempt_downstream_for_repair(attempt)
    state.status = TaskStatus.RUNNING
    save_state(state, state_root)

    prompt = build_repair_prompt(state=state, attempt=attempt, report=report)
    state = _run_implementer_with_prompt(state, state_root, artifact_paths, prompt)
    attempt = _current_attempt(state)
    return _run_from_phase(
        state,
        state_root,
        attempt,
        artifact_paths,
        start_phase=AttemptPhase.TESTING,
    )


def soft_reset_provider_failure(
    state: TaskState,
    state_root: Path,
    *,
    phase: AttemptPhase,
) -> TaskState:
    """Move a failed provider attempt back to STOPPED for auto recovery."""
    attempt = _current_attempt(state)
    state.status = TaskStatus.STOPPED
    attempt.phase = phase
    save_state(state, state_root)
    return state


def prepare_run(state: TaskState, state_root: Path) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    """Load task state, run preflight, and plan the first-iteration paths."""
    if state.status not in {TaskStatus.INITIALIZED, TaskStatus.STOPPED}:
        raise RunError(
            f"task {state.task_id} is {state.status.value}; "
            "only initialized or stopped tasks can be started with `cc-loop run`"
        )

    if state.iteration >= state.config["max_iterations"]:
        raise RunError(
            f"task {state.task_id} reached max_iterations ({state.config['max_iterations']})"
        )

    preflight = run_preflight(
        target_repo=state.target_repo,
        base_branch=state.base_branch,
        providers=state.providers,
        config=state.config,
        task_graph=ensure_task_graph(state),
    )

    iteration = state.iteration + 1
    retry = 0
    artifact_root = artifacts_dir(state.task_id, iteration, retry, state_root)
    worktree = worktree_path(
        state.task_id,
        preflight.target_repo,
        iteration,
        retry,
        DEFAULT_WORKTREE_ROOT,
    )
    branch = branch_name(state.task_id, iteration, retry)
    artifact_paths = plan_artifact_paths(artifact_root)

    attempt = _begin_attempt(
        state=state,
        preflight=preflight,
        iteration=iteration,
        retry=retry,
        worktree=worktree,
        branch=branch,
        artifact_paths=artifact_paths,
    )

    state.base_commit = preflight.base_commit
    state.base_branch = preflight.base_branch
    state.status = TaskStatus.RUNNING
    state.iteration = iteration
    state.history.append(attempt)
    save_state(state, state_root)

    return state, attempt, artifact_paths


def _run_from_phase(
    state: TaskState,
    state_root: Path,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    *,
    start_phase: AttemptPhase,
) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    phase_order = [
        AttemptPhase.PLANNING,
        AttemptPhase.WORKTREE_CREATED,
        AttemptPhase.EXECUTING,
        AttemptPhase.TESTING,
        AttemptPhase.REVIEWING,
        AttemptPhase.APPROVED,
    ]
    try:
        start_index = phase_order.index(start_phase)
    except ValueError:
        if start_phase == AttemptPhase.PREFLIGHT:
            start_index = 0
        else:
            raise RunError(f"cannot continue from attempt phase {start_phase.value}")

    if start_index <= phase_order.index(AttemptPhase.PLANNING):
        state = run_planning_phase(state, state_root, artifact_paths)
        attempt = _current_attempt(state)

    if start_index <= phase_order.index(AttemptPhase.EXECUTING):
        state = run_implementer_phase(state, state_root, artifact_paths)
        attempt = _current_attempt(state)

    if start_index <= phase_order.index(AttemptPhase.TESTING):
        state = run_test_phase(state, state_root, artifact_paths)
        attempt = _current_attempt(state)

    if start_index <= phase_order.index(AttemptPhase.REVIEWING):
        state = run_review_phase(state, state_root, artifact_paths)
        attempt = _current_attempt(state)

    return _run_finalize_phase(state, state_root, attempt, artifact_paths)


def run_planning_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
) -> TaskState:
    """Create the worktree, run the planner provider, and persist plan_json."""
    attempt = _current_attempt(state)
    if attempt.plan_json is not None and attempt.phase not in {
        AttemptPhase.PLANNING,
        AttemptPhase.PREFLIGHT,
        AttemptPhase.WORKTREE_CREATED,
    }:
        return state

    config: LoopConfig = state.config
    provider_name = config["planner_provider"]
    worktree = Path(attempt.worktree_path)
    target_repo = Path(state.target_repo)
    graph = ensure_task_graph(state)
    max_retries = int(config.get("max_retries_per_step", 2))

    if graph is not None:
        return _run_graph_node_setup(
            state,
            state_root,
            artifact_paths,
            graph=graph,
            max_retries=max_retries,
            attempt=attempt,
        )

    _emit_run_event(state_root, state, attempt, EventType.PLANNER_STARTED)
    prompt = build_planner_prompt(state)
    _write_planning_artifacts_before(
        artifact_paths=artifact_paths,
        prompt=prompt,
        provider_name=provider_name,
    )

    if not worktree.is_dir():
        try:
            add_worktree(
                target_repo,
                path=worktree,
                branch=attempt.branch,
                base_commit=attempt.base_commit,
            )
        except GitError as exc:
            _mark_planning_failed(state, state_root)
            raise PlanningError(str(exc)) from exc

    attempt.phase = AttemptPhase.WORKTREE_CREATED
    save_state(state, state_root)

    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        _mark_planning_failed(state, state_root)
        raise PlanningError(str(exc)) from exc

    timeout_seconds = _planner_timeout_seconds(config, provider_name)
    print_only = provider_name == "claude-code"
    try:
        run_result = provider.run(
            worktree_path=worktree,
            prompt=prompt,
            output_path=artifact_paths["plan_last_message"],
            config=config,
            timeout_seconds=timeout_seconds,
            raw_output_path=artifact_paths["plan_raw"],
            print_only=print_only,
        )
    except NotImplementedError as exc:
        _mark_planning_failed(state, state_root)
        raise PlanningError(str(exc)) from exc

    _write_planning_artifacts_after(artifact_paths=artifact_paths, run_result=run_result)

    if run_result.timed_out:
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"{provider_name} planner timed out")

    if run_result.exit_code != 0:
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"{provider_name} planner exited with code {run_result.exit_code}")

    last_message_path = artifact_paths["plan_last_message"]
    if not last_message_path.is_file():
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"planner last-message artifact missing: {last_message_path}")

    try:
        plan_json = provider.parse_planner_output(last_message_path)
    except (json.JSONDecodeError, KeyError, TypeError, NotImplementedError) as exc:
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"planner output parse failed: {exc}") from exc

    artifact_paths["plan_parsed"].write_text(
        json.dumps(plan_json, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        graph = graph_from_planner_json(plan_json)
    except (KeyError, TypeError, ValueError) as exc:
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"task graph parse failed: {exc}") from exc

    state.task_graph = graph
    node = next_runnable_node(graph, max_retries=max_retries)
    if node is None:
        _mark_planning_failed(state, state_root)
        raise PlanningError("planner produced a task graph with no runnable nodes")

    mark_node_running(graph, node.id)
    attempt.graph_node_id = node.id
    attempt.plan_json = plan_json
    clear_report_from_attempt(attempt)
    failure_report_path(artifact_paths["plan_prompt"].parent).unlink(missing_ok=True)
    attempt.phase = AttemptPhase.WORKTREE_CREATED
    _emit_run_event(
        state_root,
        state,
        attempt,
        EventType.PLANNER_COMPLETED,
        message=graph.summary or "planner produced task graph",
    )
    if attempt.graph_node_id:
        _emit_run_event(
            state_root,
            state,
            attempt,
            EventType.GRAPH_NODE_STARTED,
            message=node.title,
        )
    save_state(state, state_root)
    return state


def _run_graph_node_setup(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
    *,
    graph,
    max_retries: int,
    attempt: AttemptRecord | None = None,
) -> TaskState:
    """Prepare worktree and current graph node without re-running the planner."""
    attempt = _resolve_attempt(state, attempt)
    config: LoopConfig = state.config
    provider_name = config["planner_provider"]
    worktree = Path(attempt.worktree_path)
    target_repo = Path(state.target_repo)

    if attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is None:
            _mark_planning_failed(state, state_root, attempt=attempt)
            raise PlanningError(f"unknown graph node {attempt.graph_node_id}")
    else:
        node = next_runnable_node(graph, max_retries=max_retries)
        if node is None:
            _mark_planning_failed(state, state_root, attempt=attempt)
            raise PlanningError("no runnable graph nodes remain")

    mark_node_running(graph, node.id)
    attempt.graph_node_id = node.id
    if attempt.plan_json is None:
        attempt.plan_json = {
            "mode": "task_graph",
            "summary": graph.summary,
            "nodes": [n.to_dict() for n in graph.nodes],
            "is_final_step": False,
        }

    prompt = build_planner_prompt(state)
    _write_planning_artifacts_before(
        artifact_paths=artifact_paths,
        prompt=prompt,
        provider_name=provider_name,
    )
    artifact_paths["plan_parsed"].write_text(
        json.dumps(attempt.plan_json, indent=2) + "\n",
        encoding="utf-8",
    )

    if not worktree.is_dir():
        try:
            add_worktree(
                target_repo,
                path=worktree,
                branch=attempt.branch,
                base_commit=attempt.base_commit,
            )
        except GitError as exc:
            if attempt.graph_node_id:
                mark_node_failed(graph, attempt.graph_node_id, str(exc))
            _mark_planning_failed(state, state_root, attempt=attempt)
            raise PlanningError(str(exc)) from exc

    attempt.phase = AttemptPhase.WORKTREE_CREATED
    if attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is not None:
            _emit_run_event(
                state_root,
                state,
                attempt,
                EventType.GRAPH_NODE_STARTED,
                message=node.title,
            )
    save_state(state, state_root)
    return state


def run_implementer_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
    *,
    attempt: AttemptRecord | None = None,
) -> TaskState:
    """Run the configured implementer provider and capture worktree diff metadata."""
    attempt = _resolve_attempt(state, attempt)
    if attempt.implementer_exit_code is not None:
        if attempt.implementer_exit_code == 0:
            return state
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(
            f"implementer already failed with exit code {attempt.implementer_exit_code}"
        )

    if attempt.plan_json is None:
        _mark_implementer_failed(state, state_root)
        raise ImplementingError("plan_json is missing; planner must succeed before implementer")

    config: LoopConfig = state.config
    provider_name = _resolve_implementer_provider(state, attempt)
    worktree = Path(attempt.worktree_path)
    prompt = build_implementer_prompt(state, attempt.plan_json, attempt=attempt)

    _write_implementer_artifacts_before(
        artifact_paths=artifact_paths,
        prompt=prompt,
        provider_name=provider_name,
    )

    attempt.phase = AttemptPhase.EXECUTING
    save_state(state, state_root)
    _emit_run_event(state_root, state, attempt, EventType.IMPLEMENTER_STARTED, message=provider_name)

    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(str(exc)) from exc

    timeout_seconds = _implementer_timeout_seconds(config, provider_name)
    try:
        run_result = provider.run(
            worktree_path=worktree,
            prompt=prompt,
            output_path=artifact_paths["implementer_raw"],
            config=config,
            timeout_seconds=timeout_seconds,
        )
    except NotImplementedError as exc:
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(str(exc)) from exc

    _write_implementer_artifacts_after(artifact_paths=artifact_paths, run_result=run_result)
    attempt.implementer_exit_code = run_result.exit_code
    attempt.implementer_provider = provider_name

    diff_metadata = capture_worktree_diff_metadata(
        worktree,
        attempt.base_commit,
        diff_stat_path=artifact_paths["diff_stat"],
        diff_files_path=artifact_paths["diff_files"],
    )
    attempt.head_commit = str(diff_metadata["head_commit"])
    attempt.diff_stat_path = str(artifact_paths["diff_stat"])
    save_state(state, state_root)

    if run_result.timed_out:
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(f"{provider_name} implementer timed out")

    if run_result.exit_code != 0:
        _mark_implementer_failed(state, state_root, attempt=attempt)
        raise ImplementingError(f"{provider_name} implementer exited with code {run_result.exit_code}")

    _emit_run_event(
        state_root,
        state,
        attempt,
        EventType.IMPLEMENTER_COMPLETED,
        message=provider_name,
        details={"exit_code": run_result.exit_code},
    )
    return state


def run_test_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
    *,
    attempt: AttemptRecord | None = None,
) -> TaskState:
    """Run the configured test command in the worktree."""
    attempt = _resolve_attempt(state, attempt)
    if attempt.test_status in {"passed", "failed", "skipped", "timed_out"}:
        return state

    config: LoopConfig = state.config
    worktree = Path(attempt.worktree_path)
    test_command = list(config.get("test_command") or [])
    attempt.test_command = test_command
    attempt.phase = AttemptPhase.TESTING
    save_state(state, state_root)
    _emit_run_event(state_root, state, attempt, EventType.TESTS_STARTED)

    if not test_command:
        attempt.test_status = "skipped"
        attempt.test_exit_code = None
        artifact_paths["test_output"].write_text("(tests skipped: test_command not configured)\n", encoding="utf-8")
        _emit_run_event(
            state_root,
            state,
            attempt,
            EventType.TESTS_COMPLETED,
            message="skipped",
        )
        save_state(state, state_root)
        return state

    timeout_seconds = config["test_timeout_seconds"]
    result = run_with_timeout(
        test_command,
        cwd=str(worktree),
        timeout_seconds=timeout_seconds,
        capture_output=True,
    )
    output_lines = [
        f"command: {' '.join(test_command)}",
        f"cwd: {worktree}",
        f"exit_code: {result.returncode}",
        f"timed_out: {result.timed_out}",
        "",
        "## stdout",
        result.stdout.rstrip(),
        "",
        "## stderr",
        result.stderr.rstrip(),
        "",
    ]
    artifact_paths["test_output"].write_text("\n".join(output_lines), encoding="utf-8")
    attempt.test_raw_path = str(artifact_paths["test_output"])

    if result.timed_out:
        attempt.test_status = "timed_out"
        attempt.test_exit_code = result.returncode
    elif result.returncode == 0:
        attempt.test_status = "passed"
        attempt.test_exit_code = 0
    else:
        attempt.test_status = "failed"
        attempt.test_exit_code = result.returncode

    _emit_run_event(
        state_root,
        state,
        attempt,
        EventType.TESTS_COMPLETED,
        message=attempt.test_status,
        details={"exit_code": attempt.test_exit_code},
    )
    save_state(state, state_root)
    return state


def _review_artifact_path(base: Path, idx: int) -> Path:
    if idx == 0:
        return base
    return base.parent / f"{base.stem}.{idx}{base.suffix}"


def run_review_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
    *,
    attempt: AttemptRecord | None = None,
) -> TaskState:
    """Build bounded review context and run the configured reviewer provider(s)."""
    attempt = _resolve_attempt(state, attempt)
    if attempt.review_json is not None and attempt.decision:
        return state

    config: LoopConfig = state.config
    reviewer_chain = _resolve_reviewer_chain(state, attempt)
    worktree = Path(attempt.worktree_path)
    attempt.phase = AttemptPhase.REVIEWING
    save_state(state, state_root)
    _emit_run_event(state_root, state, attempt, EventType.REVIEWER_STARTED)

    patch_paths, patch_body, _used_bytes = collect_bounded_review_patches(
        worktree,
        attempt.base_commit,
        patches_dir=artifact_paths["patches_dir"],
        max_bytes=config["max_review_patch_bytes"],
    )
    attempt.diff_patch_paths = [str(path) for path in patch_paths]

    diff_stat = read_diff_stat_summary(worktree, attempt.base_commit)
    prompt = build_reviewer_prompt(
        state=state,
        attempt=attempt,
        diff_stat=diff_stat,
        patch_body=patch_body,
        test_status=attempt.test_status,
    )
    artifact_paths["review_prompt"].write_text(prompt, encoding="utf-8")

    all_reviews: list[dict[str, Any]] = []
    review_raw_paths: list[str] = []
    for idx, provider_name in enumerate(reviewer_chain):
        provider_path = _review_artifact_path(artifact_paths["review_provider"], idx)
        raw_path = _review_artifact_path(artifact_paths["review_raw"], idx)
        last_message_path = _review_artifact_path(artifact_paths["review_last_message"], idx)
        provider_path.write_text(provider_name + "\n", encoding="utf-8")
        raw_path.write_text("", encoding="utf-8")
        last_message_path.write_text("", encoding="utf-8")

        try:
            provider = get_provider(provider_name)
        except ValueError as exc:
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(str(exc)) from exc

        timeout_seconds = _reviewer_timeout_seconds(config, provider_name)
        print_only = provider_name == "claude-code"
        try:
            run_result = provider.run(
                worktree_path=worktree,
                prompt=prompt,
                output_path=last_message_path,
                config=config,
                timeout_seconds=timeout_seconds,
                raw_output_path=raw_path,
                print_only=print_only,
            )
        except NotImplementedError as exc:
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(str(exc)) from exc

        provider_path.write_text(run_result.provider + "\n", encoding="utf-8")
        review_raw_paths.append(str(raw_path))

        if run_result.timed_out:
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"{provider_name} reviewer timed out")

        if run_result.exit_code != 0:
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"{provider_name} reviewer exited with code {run_result.exit_code}")

        if not last_message_path.is_file():
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"reviewer last-message artifact missing: {last_message_path}")

        try:
            review_json = provider.parse_reviewer_output(last_message_path)
        except (json.JSONDecodeError, KeyError, TypeError, NotImplementedError) as exc:
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"reviewer output parse failed: {exc}") from exc

        review_artifact = artifact_paths["review_parsed"].parent / f"review.parsed.{idx}.json"
        review_artifact.write_text(json.dumps(review_json, indent=2) + "\n", encoding="utf-8")
        all_reviews.append(review_json)

    review_json = _aggregate_reviewer_decisions(all_reviews)
    artifact_paths["review_parsed"].write_text(
        json.dumps(review_json, indent=2) + "\n",
        encoding="utf-8",
    )
    attempt.review_json = review_json
    attempt.decision = str(review_json.get("decision", "reject"))
    attempt.review_provider = ",".join(reviewer_chain)
    attempt.review_raw_path = ",".join(review_raw_paths)

    if attempt.decision == "approve":
        attempt.phase = AttemptPhase.APPROVED
    else:
        attempt.phase = AttemptPhase.REJECTED

    _emit_run_event(
        state_root,
        state,
        attempt,
        EventType.REVIEWER_COMPLETED,
        message=attempt.decision,
    )
    save_state(state, state_root)
    return state


def _run_finalize_phase(
    state: TaskState,
    state_root: Path,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    config: LoopConfig = state.config

    if attempt.decision == "stop":
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    if attempt.decision == "replan":
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    if attempt.decision == "reject":
        graph = ensure_task_graph(state)
        if graph is not None and attempt.graph_node_id:
            reason = ""
            if attempt.review_json:
                reason = str(attempt.review_json.get("reason", "")).strip()
            mark_node_rejected(graph, attempt.graph_node_id, reason)
        if _retry_remaining(state, attempt):
            state.status = TaskStatus.STOPPED
            save_state(state, state_root)
            return state, attempt, artifact_paths
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    if not _can_auto_merge(attempt, config, state=state):
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    worktree = Path(attempt.worktree_path)
    target_repo = Path(state.target_repo)
    attempt.merge_output_path = str(artifact_paths["merge_output"])
    _emit_run_event(state_root, state, attempt, EventType.MERGE_STARTED)
    try:
        head = commit_worktree_changes(
            worktree,
            f"cc-loop: {state.task_id} {attempt.iteration:03d} retry {attempt.retry:02d}".strip(),
        )
        if head:
            attempt.head_commit = head
        merged_head = merge_branch_into_base(
            target_repo,
            attempt.branch,
            resolved_base_branch=state.base_branch,
            configured_base_branch=str(config.get("base_branch", state.base_branch)),
            message=f"cc-loop: merge {attempt.branch}",
            merge_worktree_path=_merge_worktree_path(state, attempt),
        )
        attempt.merge_error = ""
        artifact_paths["merge_output"].write_text(
            (
                f"merge_target: {state.base_branch}\n"
                f"source_branch: {attempt.branch}\n"
                f"target_head: {merged_head}\n"
                "result: merged\n"
            ),
            encoding="utf-8",
        )
    except GitCommandError as exc:
        report = classify_merge_failure(exc)
        apply_report_to_attempt(attempt, report)
        write_failure_report(artifact_paths["plan_prompt"].parent, report)
        attempt.merge_error = report.message
        artifact_paths["merge_output"].write_text(str(exc) + "\n", encoding="utf-8")
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths
    except GitError as exc:
        report = classify_merge_failure(exc)
        apply_report_to_attempt(attempt, report)
        write_failure_report(artifact_paths["plan_prompt"].parent, report)
        attempt.merge_error = report.message
        artifact_paths["merge_output"].write_text(str(exc) + "\n", encoding="utf-8")
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    attempt.phase = AttemptPhase.MERGED
    clear_report_from_attempt(attempt)
    failure_report_path(artifact_paths["plan_prompt"].parent).unlink(missing_ok=True)
    _emit_run_event(state_root, state, attempt, EventType.MERGE_COMPLETED)

    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        mark_node_passed(graph, attempt.graph_node_id, attempt.iteration)
        _emit_run_event(
            state_root,
            state,
            attempt,
            EventType.GRAPH_NODE_COMPLETED,
            message=attempt.graph_node_id,
        )
        if graph_complete(graph):
            state.status = TaskStatus.DONE
        else:
            state.status = TaskStatus.STOPPED
    elif attempt.plan_json and not attempt.plan_json.get("is_final_step", True):
        state.status = TaskStatus.STOPPED
    else:
        state.status = TaskStatus.DONE
    save_state(state, state_root)
    return state, attempt, artifact_paths


def build_planner_prompt(state: TaskState) -> str:
    """Construct the stdin prompt for the configured planner provider."""
    completed_steps = []
    graph = ensure_task_graph(state)
    if graph is not None:
        for node in graph.nodes:
            if node.status.value == "passed":
                completed_steps.append(f"  - {node.id}: {node.title}")
    else:
        for prev in state.history:
            if prev.phase == AttemptPhase.MERGED and prev.plan_json:
                completed_steps.append(
                    f"  - iter {prev.iteration}: {prev.plan_json.get('expected_changes', '').strip()}"
                )

    completed_section = ""
    if completed_steps:
        completed_section = "\n\nCompleted steps so far:\n" + "\n".join(completed_steps)

    retry_feedback = ""
    for prev in reversed(state.history):
        if prev.phase == AttemptPhase.REJECTED and prev.review_json:
            rp = str(prev.review_json.get("retry_prompt", "")).strip()
            if rp:
                retry_feedback = f"\n\nPrior attempt was rejected. Reviewer's required changes:\n{rp}"
            break

    return (
        "You are the cc-loop planner. Analyze the task goal and repository checkout.\n"
        "Respond with JSON only. Prefer task graph mode using this shape:\n"
        "{\n"
        '  "mode": "task_graph",\n'
        '  "summary": "Short summary of the implementation strategy",\n'
        '  "nodes": [\n'
        "    {\n"
        '      "id": "T1",\n'
        '      "title": "Set up project structure",\n'
        '      "description": "Create the package skeleton and baseline docs.",\n'
        '      "kind": "implementation",\n'
        '      "owner": "implementer",\n'
        '      "dependencies": [],\n'
        '      "acceptance_criteria": ["pyproject.toml exists"],\n'
        '      "files_scope": ["pyproject.toml", "src/"]\n'
        "    }\n"
        "  ],\n"
        '  "is_final_step": false\n'
        "}\n\n"
        "Legacy single-step JSON is also accepted:\n"
        "{\n"
        '  "prompt": "Detailed implementation prompt for the implementer provider",\n'
        '  "expected_changes": "Expected files or areas",\n'
        '  "acceptance_criteria": "How this step will be judged",\n'
        '  "is_final_step": false\n'
        "}\n\n"
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"Target repo: {state.target_repo}\n"
        f"Base branch: {state.base_branch}\n"
        f"Base commit: {state.base_commit}\n"
        f"{retry_feedback}"
        f"Iteration: {state.iteration}\n"
        f"{completed_section}"
    )


def build_implementer_prompt(
    state: TaskState,
    plan_json: dict[str, Any],
    *,
    attempt: AttemptRecord | None = None,
) -> str:
    """Construct the prompt for the configured implementer provider."""
    graph = ensure_task_graph(state)
    if graph is not None and attempt is not None and attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is not None:
            return _build_node_implementer_prompt(state, graph, node)

    sections = [
        "You are the cc-loop implementer. Apply the planned changes in this worktree checkout.",
        "",
        f"Task ID: {state.task_id}",
        f"Goal: {state.goal}",
        f"Iteration: {state.iteration}",
        "",
        "Implementation prompt:",
        str(plan_json.get("prompt", "")).strip(),
    ]

    expected_changes = str(plan_json.get("expected_changes", "")).strip()
    if expected_changes:
        sections.extend(["", "Expected changes:", expected_changes])

    acceptance_criteria = str(plan_json.get("acceptance_criteria", "")).strip()
    if acceptance_criteria:
        sections.extend(["", "Acceptance criteria:", acceptance_criteria])

    return "\n".join(sections).strip() + "\n"


def _build_node_implementer_prompt(state: TaskState, graph, node) -> str:
    dep_lines = completed_dependency_labels(graph, node.id)
    criteria = node.acceptance_criteria or ["(none specified)"]
    files_scope = node.files_scope or ["(not restricted)"]

    sections = [
        "You are the cc-loop implementer.",
        "",
        "You are implementing one node from a task graph.",
        "",
        "Project goal:",
        state.goal,
        "",
        f"Graph summary: {graph.summary or '(none)'}",
        "",
        f"Current node:",
        f"{node.id} — {node.title}",
        "",
        "Node description:",
        node.description or "(none)",
        "",
        f"Node kind: {node.kind.value}",
        f"Node owner: {node.owner}",
        "",
        "Acceptance criteria:",
        *[f"- {item}" for item in criteria],
        "",
        "Files scope:",
        *[f"- {item}" for item in files_scope],
    ]

    if dep_lines:
        sections.extend(["", "Completed dependencies:", *[f"- {line}" for line in dep_lines]])

    sections.extend(
        [
            "",
            "Instructions:",
            "- Focus on this node.",
            "- Keep changes scoped.",
            "- Do not undo completed dependency work.",
            "- Add or update tests for this node.",
            "- Do not weaken existing tests.",
            "- Implement only this node's scope unless a small adjacent change is necessary.",
        ]
    )
    return "\n".join(sections).strip() + "\n"


def build_reviewer_prompt(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    diff_stat: str,
    patch_body: str,
    test_status: str,
) -> str:
    """Construct the reviewer prompt with bounded diff context.

    Keep stable reviewer rules before dynamic task/diff payload so provider
    prefix caches can reuse the rubric and JSON contract across iterations.
    """
    graph = ensure_task_graph(state)
    node_section = ""
    if graph is not None and attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is not None:
            criteria = node.acceptance_criteria or ["(none specified)"]
            dep_lines = completed_dependency_labels(graph, attempt.graph_node_id)
            node_section = (
                "\n### Current graph node\n"
                f"Node: {node.id} — {node.title}\n"
                f"Kind: {node.kind.value}\n"
                f"Description: {node.description or '(none)'}\n"
                "Acceptance criteria:\n"
                + "\n".join(f"- {item}" for item in criteria)
                + "\n"
            )
            if dep_lines:
                node_section += "Completed dependencies:\n" + "\n".join(f"- {line}" for line in dep_lines) + "\n"
            node_section += (
                "\nReview whether this graph node is complete — not whether the entire project is complete.\n"
            )

    return (
        "You are the cc-loop reviewer.\n"
        "\n"
        "## Stable Review Contract\n"
        "Review one implementation attempt and decide whether it is safe to merge.\n"
        "Judge only the current graph node when a graph node is provided.\n"
        "Prefer precise, actionable feedback over broad commentary.\n"
        "Do not request unrelated refactors, style churn, or work outside the scoped node.\n"
        "Treat generated caches, build artifacts, and unrelated file churn as review findings.\n"
        "\n"
        "## Stable Review Rubric\n"
        "- Verify the implementation satisfies the stated acceptance criteria.\n"
        "- Verify tests passed, or explain why the attempt must not merge.\n"
        "- Verify the changed files match the declared scope and do not undo completed dependency work.\n"
        "- Verify the patch does not weaken existing behavior, tests, safety checks, or documented contracts.\n"
        "- Approve only when the attempt is complete, scoped, tested, and merge-ready.\n"
        "- Reject when the attempt is fixable by another implementation pass.\n"
        "- Stop only when the task is blocked by missing requirements or external input.\n"
        "- Replan when the task graph or decomposition must change before implementation can continue.\n"
        "\n"
        "## Stable JSON Output Contract\n"
        "Respond with JSON only using this exact shape:\n"
        "{\n"
        '  "decision": "approve",\n'
        '  "reason": "Why this attempt is acceptable or not",\n'
        '  "issues": [],\n'
        '  "retry_prompt": "",\n'
        '  "stop_reason": ""\n'
        "}\n"
        'Allowed decisions: "approve", "reject", "stop", "replan".\n'
        'For "approve", keep issues empty and retry_prompt empty.\n'
        'For "reject", include concise issues and a retry_prompt the implementer can execute.\n'
        'For "stop", include stop_reason.\n'
        'For "replan", include replan_reason and replan_prompt or suggested_changes.\n'
        "\n"
        "## Dynamic Review Payload\n"
        "Everything below this line may change on every attempt. Use it as evidence, but keep the stable contract above authoritative.\n"
        "\n"
        "### Attempt metadata\n"
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"Iteration: {attempt.iteration}\n"
        f"Retry: {attempt.retry}\n"
        f"Graph node: {attempt.graph_node_id or '(legacy single-step)'}\n"
        f"Implementer exit code: {attempt.implementer_exit_code}\n"
        f"Test status: {test_status}\n"
        f"Base commit: {attempt.base_commit}\n"
        f"Head commit: {attempt.head_commit}\n"
        f"{node_section}\n"
        "### Diff stat\n"
        f"{diff_stat}\n\n"
        "### Selected patches\n"
        f"{patch_body or '(no patch content selected)'}\n"
    )


def _can_auto_merge(
    attempt: AttemptRecord,
    config: LoopConfig,
    *,
    state: TaskState | None = None,
) -> bool:
    if not config.get("auto_merge", True):
        return False
    if attempt.implementer_exit_code != 0:
        return False
    if attempt.decision != "approve":
        return False
    if attempt.test_status == "failed":
        return False
    if attempt.test_status == "timed_out":
        return False

    allow_merge_without_tests = bool(config.get("allow_merge_without_tests", False))
    max_changed_files = int(config.get("max_changed_files_per_attempt", 0) or 0)
    requires_manual_review = False

    if state is not None and attempt.graph_node_id:
        graph = ensure_task_graph(state)
        if graph is not None:
            node = get_node(graph, attempt.graph_node_id)
            if node is not None:
                policy = effective_node_policy(node, config)
                requires_manual_review = bool(policy["requires_manual_review"])
                allow_merge_without_tests = bool(policy["allow_merge_without_tests"])
                node_max_files = int(policy["max_changed_files"] or 0)
                if node_max_files > 0:
                    max_changed_files = node_max_files

    if requires_manual_review:
        return False
    if attempt.test_status == "skipped" and not allow_merge_without_tests:
        return False
    if max_changed_files > 0 and attempt.diff_stat_path:
        diff_files_path = Path(attempt.diff_stat_path).parent / "diff.files.txt"
        if count_changed_files(diff_files_path) > max_changed_files:
            return False
    return True


def _retry_remaining(state: TaskState, attempt: AttemptRecord) -> bool:
    return attempt.retry < state.config["max_retries_per_step"]


def _begin_retry_attempt(
    state: TaskState,
    state_root: Path,
    rejected_attempt: AttemptRecord,
) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    preflight = run_preflight(
        target_repo=state.target_repo,
        base_branch=state.base_branch,
        providers=state.providers,
        config=state.config,
        task_graph=ensure_task_graph(state),
    )
    retry = rejected_attempt.retry + 1
    iteration = rejected_attempt.iteration
    artifact_root = artifacts_dir(state.task_id, iteration, retry, state_root)
    worktree = worktree_path(
        state.task_id,
        preflight.target_repo,
        iteration,
        retry,
        DEFAULT_WORKTREE_ROOT,
    )
    branch = branch_name(state.task_id, iteration, retry)
    artifact_paths = plan_artifact_paths(artifact_root)
    attempt = _begin_attempt(
        state=state,
        preflight=preflight,
        iteration=iteration,
        retry=retry,
        worktree=worktree,
        branch=branch,
        artifact_paths=artifact_paths,
        graph_node_id=rejected_attempt.graph_node_id,
    )
    state.base_commit = preflight.base_commit
    state.status = TaskStatus.RUNNING
    state.history.append(attempt)
    save_state(state, state_root)
    return state, attempt, artifact_paths


def _artifact_paths_for_attempt(
    state: TaskState,
    attempt: AttemptRecord,
    state_root: Path,
) -> dict[str, Path]:
    artifact_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
    return plan_artifact_paths(artifact_root)


def _merge_worktree_path(state: TaskState, attempt: AttemptRecord) -> Path:
    target_repo = Path(state.target_repo)
    repo_name = target_repo.resolve().name
    return (
        DEFAULT_WORKTREE_ROOT
        / repo_name
        / state.task_id
        / f"merge-iter-{attempt.iteration:03d}-retry-{attempt.retry:02d}"
    )


def _attempt_needs_continuation(attempt: AttemptRecord) -> bool:
    if attempt.phase in {AttemptPhase.MERGED, AttemptPhase.FAILED}:
        return False
    if attempt.phase == AttemptPhase.REJECTED:
        return False
    if attempt.phase == AttemptPhase.APPROVED:
        return True
    if attempt.implementer_exit_code == 0 and attempt.test_status:
        return attempt.decision == ""
    if attempt.implementer_exit_code == 0:
        return True
    if attempt.plan_json is not None and attempt.implementer_exit_code is None:
        return True
    if attempt.phase in {AttemptPhase.PLANNING, AttemptPhase.WORKTREE_CREATED, AttemptPhase.PREFLIGHT}:
        return attempt.plan_json is None
    return attempt.phase in {
        AttemptPhase.EXECUTING,
        AttemptPhase.TESTING,
        AttemptPhase.REVIEWING,
    }


def _reset_attempt_downstream_for_repair(attempt: AttemptRecord) -> None:
    attempt.implementer_exit_code = None
    attempt.test_status = ""
    attempt.test_exit_code = None
    attempt.decision = ""
    attempt.review_json = None
    attempt.merge_error = ""


def _run_implementer_with_prompt(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
    prompt: str,
) -> TaskState:
    attempt = _current_attempt(state)
    config: LoopConfig = state.config
    provider_name = config["implementer_provider"]
    worktree = Path(attempt.worktree_path)

    _write_implementer_artifacts_before(
        artifact_paths=artifact_paths,
        prompt=prompt,
        provider_name=provider_name,
    )
    attempt.phase = AttemptPhase.EXECUTING
    save_state(state, state_root)

    provider = get_provider(provider_name)
    timeout_seconds = _implementer_timeout_seconds(config, provider_name)
    run_result = provider.run(
        worktree_path=worktree,
        prompt=prompt,
        output_path=artifact_paths["implementer_raw"],
        config=config,
        timeout_seconds=timeout_seconds,
    )
    _write_implementer_artifacts_after(artifact_paths=artifact_paths, run_result=run_result)
    attempt.implementer_exit_code = run_result.exit_code
    attempt.implementer_provider = provider_name

    diff_metadata = capture_worktree_diff_metadata(
        worktree,
        attempt.base_commit,
        diff_stat_path=artifact_paths["diff_stat"],
        diff_files_path=artifact_paths["diff_files"],
    )
    attempt.head_commit = str(diff_metadata["head_commit"])
    attempt.diff_stat_path = str(artifact_paths["diff_stat"])
    save_state(state, state_root)

    if run_result.timed_out:
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(f"{provider_name} implementer timed out during repair")

    if run_result.exit_code != 0:
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(
            f"{provider_name} implementer exited with code {run_result.exit_code} during repair"
        )
    return state


def classify_provider_exception(
    exc: BaseException,
    *,
    phase: str,
    provider: str,
) -> FailureReport:
    message = str(exc)
    timed_out = "timed out" in message.lower()
    return classify_provider_failure(
        phase=phase,
        provider=provider,
        exit_code=None,
        timed_out=timed_out,
        parse_error=message if "parse failed" in message.lower() else "",
    )


def _write_planning_artifacts_before(
    *,
    artifact_paths: dict[str, Path],
    prompt: str,
    provider_name: str,
) -> None:
    artifact_root = artifact_paths["plan_prompt"].parent
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_paths["plan_prompt"].write_text(prompt, encoding="utf-8")
    artifact_paths["plan_provider"].write_text(provider_name + "\n", encoding="utf-8")
    artifact_paths["plan_raw"].write_text("", encoding="utf-8")
    artifact_paths["plan_last_message"].write_text("", encoding="utf-8")


def _write_planning_artifacts_after(
    *,
    artifact_paths: dict[str, Path],
    run_result: ProviderRunResult,
) -> None:
    artifact_paths["plan_provider"].write_text(run_result.provider + "\n", encoding="utf-8")
    if not artifact_paths["plan_raw"].exists():
        artifact_paths["plan_raw"].write_text("", encoding="utf-8")
    if not artifact_paths["plan_last_message"].exists():
        artifact_paths["plan_last_message"].write_text("", encoding="utf-8")


def _write_implementer_artifacts_before(
    *,
    artifact_paths: dict[str, Path],
    prompt: str,
    provider_name: str,
) -> None:
    artifact_root = artifact_paths["implementer_prompt"].parent
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_paths["implementer_prompt"].write_text(prompt, encoding="utf-8")
    artifact_paths["implementer_provider"].write_text(provider_name + "\n", encoding="utf-8")
    artifact_paths["implementer_raw"].write_text("", encoding="utf-8")


def _write_implementer_artifacts_after(
    *,
    artifact_paths: dict[str, Path],
    run_result: ProviderRunResult,
) -> None:
    artifact_paths["implementer_provider"].write_text(run_result.provider + "\n", encoding="utf-8")
    if not artifact_paths["implementer_raw"].exists():
        artifact_paths["implementer_raw"].write_text("", encoding="utf-8")


def _provider_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
    if provider_name == "cursor":
        return config["cursor_timeout_seconds"]
    if provider_name == "claude-code":
        return config["claude_code_timeout_seconds"]
    return config["codex_timeout_seconds"]


def _planner_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
    return _provider_timeout_seconds(config, provider_name)


def _implementer_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
    return _provider_timeout_seconds(config, provider_name)


def _reviewer_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
    return _provider_timeout_seconds(config, provider_name)


def _current_attempt(state: TaskState) -> AttemptRecord:
    if not state.history:
        raise RunError("task has no attempt history")
    return state.history[-1]


def _resolve_attempt(state: TaskState, attempt: AttemptRecord | None = None) -> AttemptRecord:
    if attempt is not None:
        return attempt
    return _current_attempt(state)


def _emit_run_event(
    state_root: Path,
    state: TaskState,
    attempt: AttemptRecord,
    event_type: EventType,
    *,
    message: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    append_event(
        state_root,
        task_id=state.task_id,
        event_type=event_type,
        iteration=attempt.iteration,
        retry=attempt.retry,
        graph_node_id=attempt.graph_node_id or "",
        phase=attempt.phase.value if hasattr(attempt.phase, "value") else str(attempt.phase),
        message=message,
        details=details,
    )


def _mark_planning_failed(
    state: TaskState,
    state_root: Path,
    *,
    attempt: AttemptRecord | None = None,
) -> None:
    attempt = _resolve_attempt(state, attempt)
    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        mark_node_failed(graph, attempt.graph_node_id, "planning failed")
    attempt.phase = AttemptPhase.FAILED
    state.status = TaskStatus.FAILED
    save_state(state, state_root)


def _mark_implementer_failed(
    state: TaskState,
    state_root: Path,
    *,
    attempt: AttemptRecord | None = None,
) -> None:
    attempt = _resolve_attempt(state, attempt)
    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        mark_node_failed(graph, attempt.graph_node_id, "implementer failed")
    attempt.phase = AttemptPhase.FAILED
    state.status = TaskStatus.FAILED
    save_state(state, state_root)


def _mark_review_failed(
    state: TaskState,
    state_root: Path,
    *,
    attempt: AttemptRecord | None = None,
) -> None:
    attempt = _resolve_attempt(state, attempt)
    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        mark_node_failed(graph, attempt.graph_node_id, "review failed")
    attempt.phase = AttemptPhase.FAILED
    state.status = TaskStatus.FAILED
    save_state(state, state_root)


def _begin_attempt(
    *,
    state: TaskState,
    preflight: PreflightResult,
    iteration: int,
    retry: int,
    worktree: Path,
    branch: str,
    artifact_paths: dict[str, Path],
    graph_node_id: str = "",
) -> AttemptRecord:
    config: LoopConfig = state.config
    attempt = AttemptRecord(
        iteration=iteration,
        retry=retry,
        created_at=utc_now_iso(),
        base_commit=preflight.base_commit,
        graph_node_id=graph_node_id,
        branch=branch,
        worktree_path=str(worktree),
        phase=AttemptPhase.PREFLIGHT,
        plan_provider=config["planner_provider"],
        implementer_provider=config["implementer_provider"],
        review_provider=config["reviewer_provider"],
        test_command=list(config.get("test_command") or []),
        plan_raw_path=str(artifact_paths["plan_raw"]),
        implementer_prompt_path=str(artifact_paths["implementer_prompt"]),
        implementer_raw_path=str(artifact_paths["implementer_raw"]),
        test_raw_path=str(artifact_paths["test_output"]),
        diff_stat_path=str(artifact_paths["diff_stat"]),
        review_raw_path=str(artifact_paths["review_raw"]),
    )

    attempt.phase = AttemptPhase.PLANNING
    return attempt


def summarize_attempt(attempt: AttemptRecord, state: TaskState | None = None) -> str:
    """Human-readable next-action hint for CLI output."""
    if attempt.phase == AttemptPhase.MERGED:
        graph = ensure_task_graph(state) if state is not None else None
        if graph is not None and not graph_complete(graph):
            node = attempt.graph_node_id or graph.current_node_id or "?"
            return f"node {node} merged; run `cc-loop auto` or `cc-loop run` for next graph node"
        return "attempt merged; task complete"
    if attempt.phase == AttemptPhase.FAILED:
        return "attempt failed; inspect artifacts before resuming"
    if attempt.merge_error:
        return "merge failed; inspect merge.output.txt and run `cc-loop resume` after fixing the branch state"
    if attempt.decision == "stop":
        return "reviewer requested stop; inspect artifacts"
    if attempt.phase == AttemptPhase.REJECTED:
        return "reviewer rejected; run `cc-loop resume` to retry from base commit if retries remain"
    if attempt.phase == AttemptPhase.APPROVED:
        return "reviewer approved but merge did not complete; run `cc-loop resume` to retry merge"
    if attempt.test_status and not attempt.decision:
        return "tests finished; resume to run reviewer"
    if attempt.implementer_exit_code == 0 and not attempt.test_status:
        return "implementer finished; resume to run tests"
    if attempt.plan_json and attempt.implementer_exit_code is None:
        return "plan ready; resume to run implementer"
    if attempt.phase in {AttemptPhase.PLANNING, AttemptPhase.WORKTREE_CREATED}:
        return "planning incomplete; resume to continue planner phase"
    return "resume to continue the current attempt"
