"""Run-path orchestration for cc-loop task execution."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from cc_loop.budgets import count_changed_files
from cc_loop.config import LoopConfig
from cc_loop.diff import collect_bounded_review_patches, has_mergeable_patches, read_diff_stat_summary
from cc_loop.failure import (
    FailureReport,
    FailureType,
    RecoveryDisposition,
    apply_report_to_attempt,
    classify_merge_failure,
    classify_provider_failure,
    classify_uncaptured_patch,
    clear_failure_artifact,
    clear_report_from_attempt,
    failure_report_path,
    is_patch_capture_resolved,
    merge_blocked_by_test_gate,
    repair_uncaptured_patch_report,
    reviewer_gate_passed,
    test_gate_blocked_report,
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
from cc_loop.prompt_cache import (
    build_implementer_phase_cache,
    build_planner_phase_cache,
    build_reviewer_phase_cache,
    update_prompt_cache_artifact,
)
from cc_loop.review_context import format_patch_path_list, resolve_review_context_mode
from cc_loop.prompt_metadata import build_prompt_metadata, write_prompt_metadata
from cc_loop.providers.base import ProviderRunResult, get_provider
from cc_loop.repair_prompts import build_repair_prompt
from cc_loop.recovery import persist_failure_state
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
from cc_loop.subprocess_util import RunResult, run_with_timeout
from cc_loop.planner_granularity import planner_granularity_prompt_section, resolve_planner_granularity
from cc_loop.provider_runtime import (
    provider_argv_from_result,
    run_provider_with_heartbeat,
    write_command_argv_artifact,
    write_subprocess_result_artifact,
)
from cc_loop.runner_heartbeat import mark_heartbeat_terminal, read_heartbeat, refresh_heartbeat, write_heartbeat
from cc_loop.trace import estimate_tokens_from_path, update_trace_phase
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
    _ensure_safe_stale_resume(state, state_root, attempt)
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
    if attempt.failure_type == FailureType.PATCH_NOT_CAPTURED.value:
        if reconcile_patch_not_captured(
            state,
            attempt,
            artifact_paths,
            state_root,
            reason="resume_recheck",
        ):
            state.status = TaskStatus.RUNNING
            save_state(state, state_root)
            return _run_from_phase(
                state,
                state_root,
                attempt,
                artifact_paths,
                start_phase=AttemptPhase.TESTING,
            )

    state.status = TaskStatus.RUNNING
    save_state(state, state_root)
    return _run_from_phase(state, state_root, attempt, artifact_paths, start_phase=attempt.phase)


def _ensure_safe_stale_resume(state: TaskState, state_root: Path, attempt: AttemptRecord) -> None:
    from cc_loop.inspect import is_runner_alive
    from cc_loop.runner_control import runner_state_label

    stale_seconds = int(state.config.get("stale_heartbeat_seconds", 120) or 120)
    runner_state = runner_state_label(state_root, state.task_id, stale_heartbeat_seconds=stale_seconds)
    if runner_state != "stale_heartbeat":
        return
    if attempt.phase not in {
        AttemptPhase.EXECUTING,
        AttemptPhase.PLANNING,
        AttemptPhase.REVIEWING,
        AttemptPhase.TESTING,
    }:
        return

    running, pid = is_runner_alive(state_root, state.task_id)
    if running and pid is not None:
        raise ResumeError(
            f"stale heartbeat while runner pid {pid} is still alive at phase {attempt.phase.value}; "
            "run `cc-loop cancel` or `cc-loop stop` before resuming to avoid duplicate providers"
        )

    append_event(
        state_root,
        task_id=state.task_id,
        event_type=EventType.FAILURE_RECORDED,
        iteration=attempt.iteration,
        retry=attempt.retry,
        graph_node_id=attempt.graph_node_id,
        message="stale heartbeat recovery: no live runner detected; resume allowed",
    )


_PROVIDER_PHASE_BY_KEY = {
    "planner": AttemptPhase.PLANNING,
    "implementer": AttemptPhase.EXECUTING,
    "reviewer": AttemptPhase.REVIEWING,
}


def _write_provider_startup_failure_artifacts(
    *,
    artifact_paths: dict[str, Path],
    phase_key: str,
    provider_name: str,
    error: str,
) -> None:
    """Persist argv/result/failure artifacts when a provider cannot be resolved or started."""
    artifact_root = artifact_paths["plan_prompt"].parent
    argv = ["(provider-resolution-failed)", provider_name]
    write_command_argv_artifact(artifact_root, phase=phase_key, argv=argv)
    write_subprocess_result_artifact(
        artifact_root,
        phase=phase_key,
        result=RunResult(
            args=argv,
            returncode=-1,
            stdout="",
            stderr=error,
            duration_seconds=0.0,
        ),
        stderr_path="",
        stdout_path="",
    )
    write_failure_report(
        artifact_root,
        FailureReport(
            failure_type=FailureType.PROVIDER_EXIT_ERROR,
            disposition=RecoveryDisposition.TERMINAL,
            message=error,
            stop_reason="provider_resolution_failed",
            details={"provider": provider_name, "phase": phase_key},
            suggested_actions=[f"Configure or install provider: {provider_name}"],
        ),
    )


def _invoke_provider(
    *,
    provider,
    provider_name: str,
    phase_key: str,
    state: TaskState,
    state_root: Path,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    worktree: Path,
    prompt: str,
    output_path: Path,
    config: LoopConfig,
    timeout_seconds: int,
    raw_output_path: Path | None = None,
    print_only: bool = False,
) -> ProviderRunResult:
    argv = provider_argv_from_result(
        provider,
        worktree_path=worktree,
        prompt=prompt,
        output_path=output_path,
        config=config,
        print_only=print_only,
    )
    artifact_root = artifact_paths["plan_prompt"].parent
    write_command_argv_artifact(artifact_root, phase=phase_key, argv=argv)

    provider_phase = _PROVIDER_PHASE_BY_KEY.get(phase_key)
    if provider_phase is not None:
        attempt.phase = provider_phase
    attempt.running_provider = provider_name
    save_state(state, state_root)

    try:
        run_result = run_provider_with_heartbeat(
            provider,
            state_root=state_root,
            state=state,
            attempt=attempt,
            worktree_path=worktree,
            prompt=prompt,
            output_path=output_path,
            config=config,
            timeout_seconds=timeout_seconds,
            raw_output_path=raw_output_path,
            print_only=print_only,
        )
        write_subprocess_result_artifact(
            artifact_root,
            phase=phase_key,
            result=run_result,
            stdout_path=str(run_result.raw_artifact_path),
        )
        return run_result
    finally:
        attempt.running_provider = ""
        save_state(state, state_root)


def _provider_failure_report(
    *,
    provider_name: str,
    phase: str,
    run_result: ProviderRunResult,
) -> FailureReport:
    hung = bool(
        getattr(run_result, "hung", False)
        or (
            run_result.killed
            and not run_result.timed_out
            and not run_result.interrupted
            and run_result.exit_code != 0
        )
    )
    return classify_provider_failure(
        phase=phase,
        provider=provider_name,
        exit_code=run_result.exit_code,
        timed_out=run_result.timed_out,
        interrupted=run_result.interrupted,
        hung=hung,
    )


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
    run_result = _invoke_provider(
        provider=provider,
        provider_name=provider_name,
        phase_key="planner",
        state=state,
        state_root=state_root,
        attempt=attempt,
        artifact_paths=artifact_paths,
        worktree=worktree,
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


def _clear_failure_state(attempt: AttemptRecord, artifact_paths: dict[str, Path]) -> None:
    clear_report_from_attempt(attempt)
    clear_failure_artifact(artifact_paths["plan_prompt"].parent)


def _refresh_worktree_diff_artifacts(
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    worktree: Path,
) -> dict[str, object]:
    diff_metadata = capture_worktree_diff_metadata(
        worktree,
        attempt.base_commit,
        diff_stat_path=artifact_paths["diff_stat"],
        diff_files_path=artifact_paths["diff_files"],
    )
    attempt.head_commit = str(diff_metadata["head_commit"])
    attempt.diff_stat_path = str(artifact_paths["diff_stat"])
    return diff_metadata


def reconcile_patch_not_captured(
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    state_root: Path,
    *,
    reason: str,
) -> bool:
    """Clear stale patch_not_captured when the worktree already has captured changes."""
    if attempt.failure_type != FailureType.PATCH_NOT_CAPTURED.value:
        return False
    worktree_path = str(attempt.worktree_path or "").strip()
    if not worktree_path:
        return False
    worktree = Path(worktree_path)
    if not worktree.is_dir():
        return False

    diff_metadata = _refresh_worktree_diff_artifacts(attempt, artifact_paths, worktree)
    if not is_patch_capture_resolved(
        worktree=worktree,
        base_commit=attempt.base_commit,
        diff_metadata=diff_metadata,
    ):
        return False

    _clear_failure_state(attempt, artifact_paths)
    attempt.running_provider = ""
    if attempt.implementer_exit_code is None:
        attempt.implementer_exit_code = 0
    append_event(
        state_root,
        task_id=state.task_id,
        event_type=EventType.PATCH_CAPTURE_RECOVERED,
        iteration=attempt.iteration,
        retry=attempt.retry,
        graph_node_id=attempt.graph_node_id,
        phase=attempt.phase.value,
        message="patch capture recovered from worktree recheck",
        details={"reason": reason, "head_commit": attempt.head_commit},
    )
    return True


def _stop_for_patch_not_captured(
    state: TaskState,
    state_root: Path,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    *,
    diff_metadata: dict[str, object],
    worktree: Path,
    after_repair: bool = False,
) -> None:
    attempt.running_provider = ""
    if after_repair:
        report = repair_uncaptured_patch_report(
            porcelain=list(diff_metadata.get("porcelain") or []),
            base_commit=attempt.base_commit,
            head_commit=str(diff_metadata.get("head_commit", attempt.head_commit)),
            has_committed_changes=bool(diff_metadata.get("has_committed_changes")),
            has_mergeable_patch=has_mergeable_patches(worktree, attempt.base_commit),
        )
        persist_failure_state(state, attempt, report, artifact_paths)
    state.status = TaskStatus.STOPPED
    mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
    save_state(state, state_root)


def _clear_resolved_patch_not_captured(
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    *,
    diff_metadata: dict[str, object],
    worktree: Path,
) -> None:
    if attempt.failure_type != FailureType.PATCH_NOT_CAPTURED.value:
        return
    if classify_uncaptured_patch(
        porcelain=list(diff_metadata.get("porcelain") or []),
        base_commit=attempt.base_commit,
        head_commit=str(diff_metadata.get("head_commit", attempt.head_commit)),
        has_committed_changes=bool(diff_metadata.get("has_committed_changes")),
        has_mergeable_patch=has_mergeable_patches(worktree, attempt.base_commit),
    ) is None:
        _clear_failure_state(attempt, artifact_paths)


def execute_repair_recovery(
    state: TaskState,
    state_root: Path,
    report: FailureReport,
) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    """Run implementer repair and continue test → review → finalize on the same attempt."""
    attempt = _current_attempt(state)
    if (
        attempt.phase == AttemptPhase.APPROVED
        and attempt.decision == "approve"
        and report.failure_type != FailureType.MERGE_CONFLICT
    ):
        raise RunError("cannot repair an attempt that already passed reviewer approval")
    artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
    worktree = Path(attempt.worktree_path)

    if report.failure_type == FailureType.PATCH_NOT_CAPTURED:
        if reconcile_patch_not_captured(
            state,
            attempt,
            artifact_paths,
            state_root,
            reason="pre_repair_recheck",
        ):
            append_event(
                state_root,
                task_id=state.task_id,
                event_type=EventType.RECOVERY_SKIPPED,
                iteration=attempt.iteration,
                retry=attempt.retry,
                graph_node_id=attempt.graph_node_id,
                phase=attempt.phase.value,
                message="implementer repair skipped: patch already captured",
                details={"failure_type": report.failure_type.value, "reason": "pre_repair_recheck"},
            )
            state.status = TaskStatus.RUNNING
            save_state(state, state_root)
            return _run_from_phase(
                state,
                state_root,
                attempt,
                artifact_paths,
                start_phase=AttemptPhase.TESTING,
            )

    repair_label = f"implementer_repair:{report.failure_type.value}"
    if repair_label not in report.attempted_repairs:
        report.attempted_repairs.append(repair_label)
    attempt.attempted_repairs = list(report.attempted_repairs)
    apply_report_to_attempt(attempt, report)
    write_failure_report(artifact_paths["plan_prompt"].parent, report)
    _reset_attempt_downstream_for_repair(attempt)
    state.status = TaskStatus.RUNNING
    save_state(state, state_root)

    append_event(
        state_root,
        task_id=state.task_id,
        event_type=EventType.REPAIR_STARTED,
        iteration=attempt.iteration,
        retry=attempt.retry,
        graph_node_id=attempt.graph_node_id,
        phase=attempt.phase.value,
        message=repair_label,
        details={"failure_type": report.failure_type.value},
    )

    prompt = build_repair_prompt(state=state, attempt=attempt, report=report)
    state = _run_implementer_with_prompt(state, state_root, artifact_paths, prompt)
    attempt = _current_attempt(state)

    append_event(
        state_root,
        task_id=state.task_id,
        event_type=EventType.REPAIR_COMPLETED,
        iteration=attempt.iteration,
        retry=attempt.retry,
        graph_node_id=attempt.graph_node_id,
        phase=attempt.phase.value,
        message=repair_label,
        details={"failure_type": report.failure_type.value},
    )

    if report.failure_type == FailureType.PATCH_NOT_CAPTURED:
        diff_metadata = _refresh_worktree_diff_artifacts(attempt, artifact_paths, worktree)
        if is_patch_capture_resolved(
            worktree=worktree,
            base_commit=attempt.base_commit,
            diff_metadata=diff_metadata,
        ):
            _clear_failure_state(attempt, artifact_paths)
            attempt.running_provider = ""
            append_event(
                state_root,
                task_id=state.task_id,
                event_type=EventType.PATCH_CAPTURE_RECOVERED,
                iteration=attempt.iteration,
                retry=attempt.retry,
                graph_node_id=attempt.graph_node_id,
                phase=attempt.phase.value,
                message="patch capture recovered after implementer repair",
                details={"reason": "post_repair_recheck", "head_commit": attempt.head_commit},
            )
            state.status = TaskStatus.RUNNING
            save_state(state, state_root)
            return _run_from_phase(
                state,
                state_root,
                attempt,
                artifact_paths,
                start_phase=AttemptPhase.TESTING,
            )
        if attempt.failure_type == FailureType.PATCH_NOT_CAPTURED.value:
            _stop_for_patch_not_captured(
                state,
                state_root,
                attempt,
                artifact_paths,
                diff_metadata=diff_metadata,
                worktree=worktree,
                after_repair=True,
            )
            return state, attempt, artifact_paths

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
        if attempt.failure_type == FailureType.PATCH_NOT_CAPTURED.value:
            worktree = Path(attempt.worktree_path)
            diff_metadata = _refresh_worktree_diff_artifacts(attempt, artifact_paths, worktree)
            _stop_for_patch_not_captured(
                state,
                state_root,
                attempt,
                artifact_paths,
                diff_metadata=diff_metadata,
                worktree=worktree,
            )
            return state, attempt, artifact_paths

    if start_index <= phase_order.index(AttemptPhase.TESTING):
        state = run_test_phase(state, state_root, artifact_paths)
        attempt = _current_attempt(state)

    if start_index <= phase_order.index(AttemptPhase.REVIEWING):
        state = run_review_phase(state, state_root, artifact_paths)
        attempt = _current_attempt(state)

    return _run_finalize_phase(state, state_root, attempt, artifact_paths)


def _planner_mode(config: LoopConfig) -> str:
    return str(config.get("planner_mode", "auto") or "auto").strip().lower()


def _planner_granularity_for_prompt(state: TaskState) -> str:
    mode = _planner_mode(state.config)
    if mode in {"single", "graph"}:
        return mode
    return resolve_planner_granularity(state.goal, state.config)


_AUTO_DIRECT_SIMPLE_KEYWORDS = (
    "fix",
    "bug",
    "cli",
    "docs",
    "documentation",
    "typo",
    "test",
    "failing test",
    "small",
    "narrow",
    "single file",
)
_AUTO_DIRECT_SIMPLE_PHRASES = ("do not refactor", "minimal change")
_AUTO_DIRECT_COMPLEX_KEYWORDS = (
    "architecture",
    "redesign",
    "migration",
    "multi-service",
    "distributed",
    "database schema",
    "security review",
    "benchmark suite",
    "framework",
    "roadmap",
)


def should_use_direct_planner(state: TaskState) -> tuple[bool, str]:
    """Return whether the planner provider should be skipped for this task."""
    config = state.config
    mode = _planner_mode(config)
    if mode == "direct":
        return True, "planner_mode=direct"
    if mode != "auto":
        return False, f"planner_mode={mode}"

    if not bool(config.get("auto_direct_planner", True)):
        return False, "auto_direct_planner disabled"

    goal = state.goal.strip()
    goal_lower = goal.lower()
    max_chars = int(config.get("auto_direct_max_goal_chars", 500) or 500)
    if len(goal) > max_chars:
        return False, f"goal length {len(goal)} exceeds auto_direct_max_goal_chars={max_chars}"

    for keyword in _AUTO_DIRECT_COMPLEX_KEYWORDS:
        if keyword in goal_lower:
            return False, f"complex keyword: {keyword}"

    for phrase in _AUTO_DIRECT_SIMPLE_PHRASES:
        if phrase in goal_lower:
            return True, f"minimal-change phrase: {phrase}"

    for keyword in _AUTO_DIRECT_SIMPLE_KEYWORDS:
        if keyword in goal_lower:
            return True, f"simple keyword: {keyword}"

    return False, "no auto-direct heuristic match"


def _resolved_planner_mode(state: TaskState) -> tuple[str, str, bool]:
    """Return (resolved_mode, reason, use_direct)."""
    use_direct, reason = should_use_direct_planner(state)
    if use_direct:
        return "direct", reason, True
    return "provider", reason, False


def build_direct_plan_json(goal: str) -> dict[str, Any]:
    """Build a single-node task graph plan from the task goal without a planner provider."""
    title = goal.strip()
    if len(title) > 80:
        title = title[:77] + "..."
    return {
        "mode": "task_graph",
        "summary": title,
        "nodes": [
            {
                "id": "T1",
                "title": title,
                "description": goal,
                "kind": "implementation",
                "owner": "implementer",
                "dependencies": [],
                "acceptance_criteria": [
                    "Implementation satisfies the requested goal",
                    "Configured tests pass or are explicitly skipped",
                ],
                "files_scope": [],
            }
        ],
        "is_final_step": True,
    }


def _run_direct_planning(
    *,
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
    attempt: AttemptRecord,
    config: LoopConfig,
    max_retries: int,
    prompt: str,
    planner_direct_reason: str = "planner_mode=direct",
) -> TaskState:
    """Skip planner provider and synthesize a single-node plan from the task goal."""
    plan_json = build_direct_plan_json(state.goal)
    artifact_root = artifact_paths["plan_prompt"].parent
    artifact_paths["plan_provider"].write_text("(direct)\n", encoding="utf-8")
    _write_planning_prompt_metadata(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        provider_name="(direct)",
    )
    artifact_paths["plan_raw"].write_text(
        json.dumps(plan_json, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_paths["plan_last_message"].write_text(
        json.dumps(plan_json, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_paths["plan_parsed"].write_text(
        json.dumps(plan_json, indent=2) + "\n",
        encoding="utf-8",
    )
    write_command_argv_artifact(
        artifact_root,
        phase="planner",
        argv=["(planner-skipped-direct)"],
    )

    try:
        graph = graph_from_planner_json(plan_json)
    except (KeyError, TypeError, ValueError) as exc:
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"direct plan parse failed: {exc}") from exc

    state.task_graph = graph
    node = next_runnable_node(graph, max_retries=max_retries)
    if node is None:
        _mark_planning_failed(state, state_root)
        raise PlanningError("direct plan produced a task graph with no runnable nodes")

    mark_node_running(graph, node.id)
    attempt.graph_node_id = node.id
    attempt.plan_json = plan_json
    clear_report_from_attempt(attempt)
    failure_report_path(artifact_root).unlink(missing_ok=True)
    attempt.phase = AttemptPhase.WORKTREE_CREATED
    _emit_run_event(
        state_root,
        state,
        attempt,
        EventType.PLANNER_COMPLETED,
        message="direct plan (planner provider skipped)",
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
    update_trace_phase(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        phase="planning",
        status="completed",
        prompt_path=str(artifact_paths["plan_prompt"]),
        prompt_meta_path=str(artifact_paths["plan_prompt_meta"]),
        raw_path=str(artifact_paths["plan_raw"]),
        estimated_prompt_tokens=estimate_tokens_from_path(artifact_paths["plan_prompt"]),
        planner_mode="direct",
        planner_mode_resolved="direct",
        planner_direct_reason=planner_direct_reason,
        provider_skipped=True,
    )
    return state


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
    _write_planning_prompt_metadata(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        provider_name=provider_name,
    )
    planner_mode_resolved, planner_direct_reason, use_direct_planner = _resolved_planner_mode(state)
    update_prompt_cache_artifact(
        artifact_paths["prompt_cache"],
        phase="planner",
        phase_data=build_planner_phase_cache(
            prompt=prompt,
            skipped=use_direct_planner,
            planner_mode_resolved=planner_mode_resolved,
            planner_direct_reason=planner_direct_reason,
            provider_skipped=use_direct_planner,
        ),
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

    if use_direct_planner:
        return _run_direct_planning(
            state=state,
            state_root=state_root,
            artifact_paths=artifact_paths,
            attempt=attempt,
            config=config,
            max_retries=max_retries,
            prompt=prompt,
            planner_direct_reason=planner_direct_reason,
        )

    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        _write_provider_startup_failure_artifacts(
            artifact_paths=artifact_paths,
            phase_key="planner",
            provider_name=provider_name,
            error=str(exc),
        )
        _mark_planning_failed(state, state_root)
        raise PlanningError(str(exc)) from exc

    timeout_seconds = _planner_timeout_seconds(config, provider_name)
    print_only = provider_name == "claude-code"
    try:
        run_result = _invoke_provider(
            provider=provider,
            provider_name=provider_name,
            phase_key="planner",
            state=state,
            state_root=state_root,
            attempt=attempt,
            artifact_paths=artifact_paths,
            worktree=worktree,
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

    if run_result.timed_out or run_result.interrupted or run_result.exit_code != 0:
        report = _provider_failure_report(
            provider_name=provider_name,
            phase=AttemptPhase.PLANNING.value,
            run_result=run_result,
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
        save_state(state, state_root)
        if run_result.timed_out:
            _mark_planning_failed(state, state_root)
            raise PlanningError(f"{provider_name} planner timed out")
        if run_result.interrupted:
            _mark_planning_failed(state, state_root)
            raise PlanningError(f"{provider_name} planner interrupted")
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"{provider_name} planner exited with code {run_result.exit_code}")

    last_message_path = artifact_paths["plan_last_message"]
    if not last_message_path.is_file():
        _mark_planning_failed(state, state_root)
        raise PlanningError(f"planner last-message artifact missing: {last_message_path}")

    try:
        plan_json = provider.parse_planner_output(last_message_path)
    except (json.JSONDecodeError, KeyError, TypeError, NotImplementedError) as exc:
        parse_error = f"planner output parse failed: {exc}"
        report = classify_provider_failure(
            phase=AttemptPhase.PLANNING.value,
            provider=provider_name,
            parse_error=parse_error,
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
        save_state(state, state_root)
        _mark_planning_failed(state, state_root)
        raise PlanningError(parse_error) from exc

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
    update_trace_phase(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        phase="planning",
        status="completed",
        prompt_path=str(artifact_paths["plan_prompt"]),
        prompt_meta_path=str(artifact_paths["plan_prompt_meta"]),
        raw_path=str(artifact_paths["plan_raw"]),
        estimated_prompt_tokens=estimate_tokens_from_path(artifact_paths["plan_prompt"]),
    )
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
    _write_planning_prompt_metadata(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        provider_name=provider_name,
    )
    update_prompt_cache_artifact(
        artifact_paths["prompt_cache"],
        phase="planner",
        phase_data=build_planner_phase_cache(prompt=prompt, skipped=False),
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
    update_trace_phase(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        phase="planning",
        status="completed",
        prompt_path=str(artifact_paths["plan_prompt"]),
        prompt_meta_path=str(artifact_paths["plan_prompt_meta"]),
        raw_path=str(artifact_paths["plan_raw"]),
        estimated_prompt_tokens=estimate_tokens_from_path(artifact_paths["plan_prompt"]),
    )
    return state


def _record_uncaptured_patch_if_needed(
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    *,
    diff_metadata: dict[str, object],
    worktree: Path,
) -> None:
    """Persist a recoverable failure when implementer changes are not mergeable."""
    if attempt.implementer_exit_code not in {0, None}:
        return
    report = classify_uncaptured_patch(
        porcelain=list(diff_metadata.get("porcelain") or []),
        base_commit=attempt.base_commit,
        head_commit=str(diff_metadata.get("head_commit", attempt.head_commit)),
        has_committed_changes=bool(diff_metadata.get("has_committed_changes")),
        has_mergeable_patch=has_mergeable_patches(worktree, attempt.base_commit),
    )
    if report is None:
        return
    persist_failure_state(state, attempt, report, artifact_paths)


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
    attempt.phase = AttemptPhase.EXECUTING
    save_state(state, state_root)

    prompt = build_implementer_prompt(state, attempt.plan_json, attempt=attempt)

    _write_implementer_artifacts_before(
        artifact_paths=artifact_paths,
        prompt=prompt,
        provider_name=provider_name,
    )
    _write_implementer_prompt_metadata(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        provider_name=provider_name,
    )
    update_prompt_cache_artifact(
        artifact_paths["prompt_cache"],
        phase="implementer",
        phase_data=build_implementer_phase_cache(prompt=prompt),
    )

    _emit_run_event(state_root, state, attempt, EventType.IMPLEMENTER_STARTED, message=provider_name)

    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        _write_provider_startup_failure_artifacts(
            artifact_paths=artifact_paths,
            phase_key="implementer",
            provider_name=provider_name,
            error=str(exc),
        )
        _update_implementer_trace(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            status="failed",
            error=str(exc),
        )
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(str(exc)) from exc

    timeout_seconds = _implementer_timeout_seconds(config, provider_name)
    try:
        run_result = _invoke_provider(
            provider=provider,
            provider_name=provider_name,
            phase_key="implementer",
            state=state,
            state_root=state_root,
            attempt=attempt,
            artifact_paths=artifact_paths,
            worktree=worktree,
            prompt=prompt,
            output_path=artifact_paths["implementer_raw"],
            config=config,
            timeout_seconds=timeout_seconds,
        )
    except NotImplementedError as exc:
        _update_implementer_trace(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            status="failed",
            error=str(exc),
        )
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
    _record_uncaptured_patch_if_needed(
        state,
        attempt,
        artifact_paths,
        diff_metadata=diff_metadata,
        worktree=worktree,
    )
    _clear_resolved_patch_not_captured(
        attempt,
        artifact_paths,
        diff_metadata=diff_metadata,
        worktree=worktree,
    )
    save_state(state, state_root)

    if run_result.timed_out:
        report = _provider_failure_report(
            provider_name=provider_name,
            phase=AttemptPhase.EXECUTING.value,
            run_result=run_result,
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
        save_state(state, state_root)
        _update_implementer_trace(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            status="timed_out",
            exit_code=run_result.exit_code,
            error=f"{provider_name} implementer timed out",
        )
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(f"{provider_name} implementer timed out")

    hung = bool(
        getattr(run_result, "hung", False)
        or (
            run_result.killed
            and not run_result.timed_out
            and not run_result.interrupted
            and run_result.exit_code != 0
        )
    )
    if hung:
        report = _provider_failure_report(
            provider_name=provider_name,
            phase=AttemptPhase.EXECUTING.value,
            run_result=run_result,
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
        save_state(state, state_root)
        _update_implementer_trace(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            status="hung",
            exit_code=run_result.exit_code,
            error=f"{provider_name} implementer hung",
        )
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(f"{provider_name} implementer hung and was force-killed")

    if run_result.interrupted:
        report = _provider_failure_report(
            provider_name=provider_name,
            phase=AttemptPhase.EXECUTING.value,
            run_result=run_result,
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
        save_state(state, state_root)
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(f"{provider_name} implementer interrupted")

    if run_result.exit_code != 0:
        report = _provider_failure_report(
            provider_name=provider_name,
            phase=AttemptPhase.EXECUTING.value,
            run_result=run_result,
        )
        persist_failure_state(state, attempt, report, artifact_paths)
        mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
        save_state(state, state_root)
        _update_implementer_trace(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            status="failed",
            exit_code=run_result.exit_code,
            error=f"{provider_name} implementer exited with code {run_result.exit_code}",
        )
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
    _update_implementer_trace(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        status="completed",
        exit_code=run_result.exit_code,
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
        update_trace_phase(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            phase="testing",
            status="skipped",
            output_path=str(artifact_paths["test_output"]),
            exit_code=None,
        )
        save_state(state, state_root)
        return state

    timeout_seconds = config["test_timeout_seconds"]
    artifact_root = artifact_paths["plan_prompt"].parent
    write_command_argv_artifact(artifact_root, phase="test", argv=test_command)
    result = run_with_timeout(
        test_command,
        cwd=str(worktree),
        timeout_seconds=timeout_seconds,
        capture_output=True,
    )
    test_stdout_path = str(artifact_paths["test_output"])
    write_subprocess_result_artifact(
        artifact_root,
        phase="test",
        result=result,
        stdout_path=test_stdout_path,
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
    update_trace_phase(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        phase="testing",
        status=attempt.test_status,
        output_path=str(artifact_paths["test_output"]),
        exit_code=attempt.test_exit_code,
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
    context_mode, inline_patch = resolve_review_context_mode(config, len(patch_body))
    prompt = build_reviewer_prompt(
        state=state,
        attempt=attempt,
        diff_stat=diff_stat,
        patch_body=patch_body,
        test_status=attempt.test_status,
        config=config,
        artifact_paths=artifact_paths,
        patch_paths=patch_paths,
        inline_patch=inline_patch,
        context_mode=context_mode,
    )
    artifact_paths["review_prompt"].write_text(prompt, encoding="utf-8")
    review_prompt_metrics = build_reviewer_prompt_metrics(
        prompt=prompt,
        diff_stat=diff_stat,
        patch_body=patch_body,
        inline_patch=inline_patch,
        context_mode=context_mode,
    )
    artifact_paths["review_prompt_metrics"].write_text(
        json.dumps(review_prompt_metrics, indent=2) + "\n",
        encoding="utf-8",
    )
    update_prompt_cache_artifact(
        artifact_paths["prompt_cache"],
        phase="reviewer",
        phase_data=build_reviewer_phase_cache(metrics=review_prompt_metrics),
    )
    _write_reviewer_prompt_metadata(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        provider_name=reviewer_chain[0],
    )

    all_reviews: list[dict[str, Any]] = []
    review_raw_paths: list[str] = []
    review_last_message_paths: list[str] = []
    for idx, provider_name in enumerate(reviewer_chain):
        provider_path = _review_artifact_path(artifact_paths["review_provider"], idx)
        raw_path = _review_artifact_path(artifact_paths["review_raw"], idx)
        last_message_path = _review_artifact_path(artifact_paths["review_last_message"], idx)
        current_raw_paths = review_raw_paths + [str(raw_path)]
        current_last_message_paths = review_last_message_paths + [str(last_message_path)]
        provider_path.write_text(provider_name + "\n", encoding="utf-8")
        raw_path.write_text("", encoding="utf-8")
        last_message_path.write_text("", encoding="utf-8")

        try:
            provider = get_provider(provider_name)
        except ValueError as exc:
            _write_provider_startup_failure_artifacts(
                artifact_paths=artifact_paths,
                phase_key="reviewer",
                provider_name=provider_name,
                error=str(exc),
            )
            _update_review_trace(
                state=state,
                attempt=attempt,
                artifact_paths=artifact_paths,
                config=config,
                status="failed",
                raw_paths=current_raw_paths,
                last_message_paths=current_last_message_paths,
                metrics=review_prompt_metrics,
                error=str(exc),
            )
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(str(exc)) from exc

        timeout_seconds = _reviewer_timeout_seconds(config, provider_name)
        print_only = provider_name == "claude-code"
        try:
            run_result = _invoke_provider(
                provider=provider,
                provider_name=provider_name,
                phase_key="reviewer",
                state=state,
                state_root=state_root,
                attempt=attempt,
                artifact_paths=artifact_paths,
                worktree=worktree,
                prompt=prompt,
                output_path=last_message_path,
                config=config,
                timeout_seconds=timeout_seconds,
                raw_output_path=raw_path,
                print_only=print_only,
            )
        except NotImplementedError as exc:
            _update_review_trace(
                state=state,
                attempt=attempt,
                artifact_paths=artifact_paths,
                config=config,
                status="failed",
                raw_paths=current_raw_paths,
                last_message_paths=current_last_message_paths,
                metrics=review_prompt_metrics,
                error=str(exc),
            )
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(str(exc)) from exc

        provider_path.write_text(run_result.provider + "\n", encoding="utf-8")
        review_raw_paths.append(str(raw_path))
        review_last_message_paths.append(str(last_message_path))

        if run_result.timed_out:
            report = _provider_failure_report(
                provider_name=provider_name,
                phase=AttemptPhase.REVIEWING.value,
                run_result=run_result,
            )
            persist_failure_state(state, attempt, report, artifact_paths)
            mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
            save_state(state, state_root)
            _update_review_trace(
                state=state,
                attempt=attempt,
                artifact_paths=artifact_paths,
                config=config,
                status="timed_out",
                raw_paths=review_raw_paths,
                last_message_paths=review_last_message_paths,
                metrics=review_prompt_metrics,
                error=f"{provider_name} reviewer timed out",
            )
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"{provider_name} reviewer timed out")

        hung = bool(
            getattr(run_result, "hung", False)
            or (
                run_result.killed
                and not run_result.timed_out
                and not run_result.interrupted
                and run_result.exit_code != 0
            )
        )
        if hung:
            report = _provider_failure_report(
                provider_name=provider_name,
                phase=AttemptPhase.REVIEWING.value,
                run_result=run_result,
            )
            persist_failure_state(state, attempt, report, artifact_paths)
            mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
            save_state(state, state_root)
            _update_review_trace(
                state=state,
                attempt=attempt,
                artifact_paths=artifact_paths,
                config=config,
                status="hung",
                raw_paths=review_raw_paths,
                last_message_paths=review_last_message_paths,
                metrics=review_prompt_metrics,
                error=f"{provider_name} reviewer hung",
            )
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"{provider_name} reviewer hung and was force-killed")

        if run_result.exit_code != 0:
            report = _provider_failure_report(
                provider_name=provider_name,
                phase=AttemptPhase.REVIEWING.value,
                run_result=run_result,
            )
            persist_failure_state(state, attempt, report, artifact_paths)
            mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
            save_state(state, state_root)
            _update_review_trace(
                state=state,
                attempt=attempt,
                artifact_paths=artifact_paths,
                config=config,
                status="failed",
                raw_paths=review_raw_paths,
                last_message_paths=review_last_message_paths,
                metrics=review_prompt_metrics,
                error=f"{provider_name} reviewer exited with code {run_result.exit_code}",
            )
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"{provider_name} reviewer exited with code {run_result.exit_code}")

        if not last_message_path.is_file():
            _update_review_trace(
                state=state,
                attempt=attempt,
                artifact_paths=artifact_paths,
                config=config,
                status="failed",
                raw_paths=review_raw_paths,
                last_message_paths=review_last_message_paths,
                metrics=review_prompt_metrics,
                error=f"reviewer last-message artifact missing: {last_message_path}",
            )
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(f"reviewer last-message artifact missing: {last_message_path}")

        try:
            review_json = provider.parse_reviewer_output(last_message_path)
        except (json.JSONDecodeError, KeyError, TypeError, NotImplementedError) as exc:
            parse_error = f"reviewer output parse failed: {exc}"
            report = classify_provider_failure(
                phase=AttemptPhase.REVIEWING.value,
                provider=provider_name,
                parse_error=parse_error,
            )
            persist_failure_state(state, attempt, report, artifact_paths)
            mark_heartbeat_terminal(state_root, state.task_id, status="stopped", phase=attempt.phase.value)
            save_state(state, state_root)
            _update_review_trace(
                state=state,
                attempt=attempt,
                artifact_paths=artifact_paths,
                config=config,
                status="failed",
                raw_paths=review_raw_paths,
                last_message_paths=review_last_message_paths,
                metrics=review_prompt_metrics,
                error=parse_error,
            )
            _mark_review_failed(state, state_root, attempt=attempt)
            raise ReviewError(parse_error) from exc

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
        _clear_failure_state(attempt, artifact_paths)
        existing_hb = read_heartbeat(state_root, state.task_id)
        refresh_heartbeat(
            state_root,
            task_id=state.task_id,
            pid=existing_hb.pid if existing_hb else os.getpid(),
            status=TaskStatus.STOPPED.value,
            phase=AttemptPhase.APPROVED.value,
            iteration=state.iteration,
            graph_node_id=attempt.graph_node_id,
            running_provider="",
        )
    else:
        attempt.phase = AttemptPhase.REJECTED

    _emit_run_event(
        state_root,
        state,
        attempt,
        EventType.REVIEWER_COMPLETED,
        message=attempt.decision,
        details={
            "prompt_chars": review_prompt_metrics["prompt_chars"],
            "stable_prefix_chars": review_prompt_metrics["stable_prefix_chars"],
            "dynamic_payload_chars": review_prompt_metrics["dynamic_payload_chars"],
            "estimated_prompt_tokens": review_prompt_metrics["estimated_prompt_tokens"],
            "estimated_dynamic_payload_tokens": review_prompt_metrics["estimated_dynamic_payload_tokens"],
        },
    )
    _update_review_trace(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        status="completed",
        raw_paths=review_raw_paths,
        last_message_paths=review_last_message_paths,
        metrics=review_prompt_metrics,
        decision=attempt.decision,
    )
    save_state(state, state_root)
    return state


def _persist_state(state: TaskState, state_root: Path) -> None:
    save_state(state, state_root)
    from cc_loop.summary import finalize_terminal_task

    finalize_terminal_task(state, state_root)


def _run_finalize_phase(
    state: TaskState,
    state_root: Path,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    config: LoopConfig = state.config

    if attempt.decision == "stop":
        state.status = TaskStatus.STOPPED
        _persist_state(state, state_root)
        return state, attempt, artifact_paths

    if attempt.decision == "replan":
        state.status = TaskStatus.STOPPED
        _persist_state(state, state_root)
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
            _persist_state(state, state_root)
            return state, attempt, artifact_paths
        state.status = TaskStatus.STOPPED
        _persist_state(state, state_root)
        return state, attempt, artifact_paths

    if not _can_auto_merge(attempt, config, state=state):
        state.status = TaskStatus.STOPPED
        if reviewer_gate_passed(attempt) and merge_blocked_by_test_gate(attempt, config, state=state):
            report = test_gate_blocked_report(attempt)
            apply_report_to_attempt(attempt, report)
            write_failure_report(artifact_paths["plan_prompt"].parent, report)
        mark_heartbeat_terminal(
            state_root,
            state.task_id,
            status=TaskStatus.STOPPED.value,
            phase=attempt.phase.value,
        )
        existing_hb = read_heartbeat(state_root, state.task_id)
        if existing_hb is not None:
            existing_hb.running_provider = ""
            write_heartbeat(state_root, existing_hb)
        _persist_state(state, state_root)
        return state, attempt, artifact_paths

    worktree = Path(attempt.worktree_path)
    target_repo = Path(state.target_repo)
    attempt.merge_output_path = str(artifact_paths["merge_output"])
    _emit_run_event(state_root, state, attempt, EventType.MERGE_STARTED)
    try:
        head = commit_worktree_changes(
            worktree,
            f"cc-loop: {state.task_id} {attempt.iteration:03d} retry {attempt.retry:02d}".strip(),
            staging_report_path=artifact_paths["plan_prompt"].parent / "commit.staging.json",
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
        update_trace_phase(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            phase="merge",
            status="failed",
            output_path=str(artifact_paths["merge_output"]),
            error=attempt.merge_error,
        )
        state.status = TaskStatus.STOPPED
        _persist_state(state, state_root)
        return state, attempt, artifact_paths
    except GitError as exc:
        report = classify_merge_failure(exc)
        apply_report_to_attempt(attempt, report)
        write_failure_report(artifact_paths["plan_prompt"].parent, report)
        attempt.merge_error = report.message
        artifact_paths["merge_output"].write_text(str(exc) + "\n", encoding="utf-8")
        update_trace_phase(
            state=state,
            attempt=attempt,
            artifact_paths=artifact_paths,
            config=config,
            phase="merge",
            status="failed",
            output_path=str(artifact_paths["merge_output"]),
            error=attempt.merge_error,
        )
        state.status = TaskStatus.STOPPED
        _persist_state(state, state_root)
        return state, attempt, artifact_paths

    attempt.phase = AttemptPhase.MERGED
    clear_report_from_attempt(attempt)
    failure_report_path(artifact_paths["plan_prompt"].parent).unlink(missing_ok=True)
    _emit_run_event(state_root, state, attempt, EventType.MERGE_COMPLETED)
    update_trace_phase(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        phase="merge",
        status="merged",
        output_path=str(artifact_paths["merge_output"]),
        error="",
    )

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
    _persist_state(state, state_root)
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
        completed_section = "\nCompleted steps so far:\n" + "\n".join(completed_steps)

    retry_feedback = ""
    for prev in reversed(state.history):
        if prev.phase == AttemptPhase.REJECTED and prev.review_json:
            rp = str(prev.review_json.get("retry_prompt", "")).strip()
            if rp:
                retry_feedback = f"Prior attempt was rejected. Reviewer's required changes:\n{rp}\n"
            break

    granularity = _planner_granularity_for_prompt(state)
    granularity_section = planner_granularity_prompt_section(granularity).strip()
    dynamic_marker = "## Dynamic Planner Payload"

    stable_prefix = (
        "You are the cc-loop planner. Analyze the task goal and repository checkout.\n"
        "\n"
        "## Output Format\n"
        "Return raw JSON only. Do not wrap in markdown fences. Do not add commentary.\n"
        "\n"
        "## Preferred Task Graph Shape\n"
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
        "}\n"
        "\n"
        "## Legacy Single-Step Shape\n"
        "{\n"
        '  "prompt": "Detailed implementation prompt for the implementer provider",\n'
        '  "expected_changes": "Expected files or areas",\n'
        '  "acceptance_criteria": "How this step will be judged",\n'
        '  "is_final_step": false\n'
        "}\n"
        "\n"
        "## Task Graph Rules\n"
        "- Prefer task_graph mode when decomposition reduces risk or clarifies ownership.\n"
        "- Each node must have a unique id, title, description, and acceptance_criteria.\n"
        "- Use dependencies to order work; do not duplicate completed steps or existing repo functionality.\n"
        "- Set is_final_step true only when the entire goal is complete after this plan.\n"
        "\n"
        "## Planner Granularity\n"
        f"{granularity_section}\n"
        "\n"
        f"{dynamic_marker}\n"
        "Everything below this line is task-specific. Use it as context for planning.\n"
    )

    dynamic_payload = (
        f"\nTask ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"Target repo: {state.target_repo}\n"
        f"Base branch: {state.base_branch}\n"
        f"Base commit: {state.base_commit}\n"
        f"Iteration: {state.iteration}\n"
        f"{retry_feedback}"
        f"{completed_section}"
    )
    return stable_prefix + dynamic_payload


_IMPLEMENTER_STABLE_CONTRACT = (
    "You are the cc-loop implementer. Apply the planned changes in this worktree checkout.\n"
    "\n"
    "## Implementer Contract\n"
    "- Keep changes scoped to the assigned work; do not expand scope.\n"
    "- Do not perform unrelated refactors or drive-by edits.\n"
    "- Do not undo user changes or completed dependency work.\n"
    "- Preserve existing code patterns and conventions in this repository.\n"
    "\n"
    "## Execution Rules\n"
    "- Work only inside the assigned worktree checkout.\n"
    "- Implement the requested behavior completely before finishing.\n"
    "- Focus on the assigned node or plan step; avoid expanding scope.\n"
    "\n"
    "## Git Commit Requirements (mandatory)\n"
    "- You MUST stage all implementation changes with `git add`.\n"
    "- You MUST create a git commit before finishing (`git commit -m \"...\"`).\n"
    "- Do NOT only write files without committing; uncommitted changes fail patch capture.\n"
    "- When finished, the worktree should be clean (`git status --porcelain` empty).\n"
    "- Implementation changes must appear in base..HEAD commits.\n"
    "- Use a concise commit message describing what was implemented.\n"
    "- If you cannot commit, explain why before exiting.\n"
    "\n"
    "## Testing Requirements\n"
    "- Add or update tests when the change warrants them.\n"
    "- Do not weaken existing tests or documented contracts.\n"
    "- Ensure new modules are tracked files included in your commit.\n"
    "\n"
    "## Patch Capture Requirements\n"
    "- cc-loop captures implementation via git commits between base and HEAD.\n"
    "- Untracked or uncommitted files are not mergeable and trigger patch_not_captured failure.\n"
    "- Include generated files with `git add` before committing.\n"
    "\n"
    "## Output Requirements\n"
    "- Write files in the worktree; do not substitute partial file dumps for real edits.\n"
    "- Finish only after changes are committed and the worktree is clean.\n"
    "\n"
    "## Dynamic Implementer Payload\n"
    "Everything below this line is task-specific context for this attempt.\n"
)


def _implementer_dynamic_payload(
    state: TaskState,
    *,
    attempt: AttemptRecord | None = None,
    plan_json: dict[str, Any] | None = None,
    node_sections: list[str] | None = None,
) -> str:
    sections: list[str] = [
        f"Task ID: {state.task_id}",
        f"Goal: {state.goal}",
        f"Target repo: {state.target_repo}",
        f"Base branch: {state.base_branch}",
        f"Iteration: {state.iteration}",
    ]
    if attempt is not None:
        if attempt.branch:
            sections.append(f"Branch: {attempt.branch}")
        if attempt.worktree_path:
            sections.append(f"Worktree path: {attempt.worktree_path}")
        if attempt.graph_node_id:
            sections.append(f"Graph node: {attempt.graph_node_id}")

    retry_feedback = ""
    for prev in reversed(state.history):
        if prev.phase == AttemptPhase.REJECTED and prev.review_json:
            rp = str(prev.review_json.get("retry_prompt", "")).strip()
            if rp:
                retry_feedback = f"Prior attempt was rejected. Reviewer's required changes:\n{rp}\n"
                break
    if retry_feedback:
        sections.extend(["", retry_feedback.strip()])

    if node_sections:
        sections.extend(node_sections)
    elif plan_json is not None:
        sections.extend(
            [
                "",
                "Implementation prompt:",
                str(plan_json.get("prompt", "")).strip(),
            ]
        )
        expected_changes = str(plan_json.get("expected_changes", "")).strip()
        if expected_changes:
            sections.extend(["", "Expected changes:", expected_changes])
        acceptance_criteria = str(plan_json.get("acceptance_criteria", "")).strip()
        if acceptance_criteria:
            sections.extend(["", "Acceptance criteria:", acceptance_criteria])

    return "\n".join(sections).strip() + "\n"


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
            return _build_node_implementer_prompt(state, graph, node, attempt=attempt)

    sections = [
        _IMPLEMENTER_STABLE_CONTRACT,
        _implementer_dynamic_payload(state, attempt=attempt, plan_json=plan_json),
    ]
    return "\n".join(section for section in sections if section).strip() + "\n"


def _build_node_implementer_prompt(
    state: TaskState,
    graph,
    node,
    *,
    attempt: AttemptRecord | None = None,
) -> str:
    dep_lines = completed_dependency_labels(graph, node.id)
    criteria = node.acceptance_criteria or ["(none specified)"]
    files_scope = node.files_scope or ["(not restricted)"]

    node_sections = [
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
        node_sections.extend(["", "Completed dependencies:", *[f"- {line}" for line in dep_lines]])

    node_sections.extend(
        [
            "",
            "Node instructions:",
            "- Focus on this node.",
            "- Implement only this node's scope unless a small adjacent change is necessary.",
        ]
    )

    return (
        _IMPLEMENTER_STABLE_CONTRACT
        + _implementer_dynamic_payload(state, attempt=attempt, node_sections=node_sections)
    )


TASK_REVIEW_CONTEXT_MARKER = "## Task Review Context"
DYNAMIC_REVIEW_MARKER = "## Dynamic Review Payload"


def summarize_diff_stat(diff_stat: str, *, max_lines: int = 20) -> dict[str, Any]:
    """Summarize a git diff --stat block for artifact-ref reviewer prompts."""
    lines = [line for line in diff_stat.splitlines() if line.strip()]
    preview_lines = lines[:max_lines]
    truncated = len(lines) > max_lines

    changed_file_count = 0
    inserted_lines: int | None = None
    deleted_lines: int | None = None

    for line in lines:
        if "|" in line and "changed" not in line.lower():
            changed_file_count += 1

    for line in reversed(lines):
        lower = line.lower()
        if "changed" not in lower:
            continue
        files_match = re.search(r"(\d+)\s+files?\s+changed", line)
        if files_match:
            changed_file_count = int(files_match.group(1))
        insert_match = re.search(r"(\d+)\s+insertions?\(\+\)", line)
        if insert_match:
            inserted_lines = int(insert_match.group(1))
        delete_match = re.search(r"(\d+)\s+deletions?\(-\)", line)
        if delete_match:
            deleted_lines = int(delete_match.group(1))
        break

    return {
        "changed_file_count": changed_file_count,
        "inserted_lines": inserted_lines,
        "deleted_lines": deleted_lines,
        "preview_lines": preview_lines,
        "truncated": truncated,
    }


def _should_inline_diff_stat(*, context_mode: str, inline_patch: bool) -> bool:
    if context_mode == "inline":
        return True
    if context_mode == "artifact_refs":
        return False
    return inline_patch


def _build_task_review_context_section(
    *,
    state: TaskState,
    attempt: AttemptRecord,
) -> str:
    graph = ensure_task_graph(state)
    lines = [
        TASK_REVIEW_CONTEXT_MARKER,
        "Everything in this section is task/node-scoped and should remain stable across retries of the same node.",
        f"Task goal: {state.goal}",
    ]
    if graph is not None and attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        if node is not None:
            criteria = node.acceptance_criteria or ["(none specified)"]
            files_scope = node.files_scope or ["(none specified)"]
            dep_lines = completed_dependency_labels(graph, attempt.graph_node_id)
            lines.extend(
                [
                    f"Graph node: {node.id}",
                    f"Node title: {node.title}",
                    f"Node kind: {node.kind.value}",
                    f"Node description: {node.description or '(none)'}",
                    "Acceptance criteria:",
                    *[f"- {item}" for item in criteria],
                    "Files scope:",
                    *[f"- {item}" for item in files_scope],
                ]
            )
            if dep_lines:
                lines.append("Completed dependencies:")
                lines.extend(f"- {line}" for line in dep_lines)
            lines.append(
                "Review whether this graph node is complete — not whether the entire project is complete."
            )
        else:
            lines.extend(
                [
                    f"Graph node: {attempt.graph_node_id}",
                    "Node title: (unknown node)",
                ]
            )
    else:
        lines.append("Graph node: legacy single-step")

    return "\n".join(lines) + "\n\n"


def _format_diff_stat_section(
    *,
    diff_stat: str,
    inline_diff_stat: bool,
    diff_stat_path: str,
    diff_files_path: str,
) -> str:
    if inline_diff_stat:
        return f"### Diff stat\n{diff_stat}\n\n"

    summary = summarize_diff_stat(diff_stat)
    preview = "\n".join(summary["preview_lines"]) or "(empty)"
    if summary["truncated"]:
        preview += "\n...(truncated)"

    lines = [
        "### Diff stat summary",
        f"Changed files: {summary['changed_file_count']}",
    ]
    if summary["inserted_lines"] is not None:
        lines.append(f"Insertions: {summary['inserted_lines']}")
    if summary["deleted_lines"] is not None:
        lines.append(f"Deletions: {summary['deleted_lines']}")
    lines.extend(
        [
            "Preview:",
            preview,
            f"Full diff stat: {diff_stat_path or '(unknown)'}",
            f"Changed files list: {diff_files_path or '(unknown)'}",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def build_reviewer_prompt(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    diff_stat: str,
    patch_body: str,
    test_status: str,
    config: LoopConfig | None = None,
    artifact_paths: dict[str, Path] | None = None,
    patch_paths: list[Path] | None = None,
    inline_patch: bool | None = None,
    context_mode: str | None = None,
) -> str:
    """Construct the reviewer prompt with bounded diff context.

    Stable contract and task context precede the dynamic payload marker so
    provider prefix caches can reuse rubric, JSON contract, and node-scoped
    context across retries of the same node.
    """
    config = config or state.config
    patch_body_chars = len(patch_body)
    if context_mode is None or inline_patch is None:
        resolved_mode, resolved_inline = resolve_review_context_mode(config, patch_body_chars)
        context_mode = context_mode or resolved_mode
        inline_patch = resolved_inline if inline_patch is None else inline_patch

    inline_diff_stat = _should_inline_diff_stat(context_mode=context_mode, inline_patch=inline_patch)
    artifact_root = ""
    diff_stat_path = ""
    diff_files_path = ""
    test_output_path = ""
    patches_dir = ""
    selected_patch_paths = ""
    omitted_patch_chars = 0 if inline_patch else patch_body_chars
    omitted_diff_stat_chars = 0 if inline_diff_stat else len(diff_stat)
    estimated_avoided_tokens = _estimated_tokens_from_chars(
        omitted_patch_chars + omitted_diff_stat_chars
    )

    if artifact_paths is not None:
        artifact_root = str(artifact_paths["plan_prompt"].parent)
        diff_stat_path = str(artifact_paths["diff_stat"])
        diff_files_path = str(artifact_paths["diff_files"])
        test_output_path = str(artifact_paths["test_output"])
        patches_dir = str(artifact_paths["patches_dir"])
        selected_patch_paths = format_patch_path_list(list(patch_paths or []))

    allow_merge_without_tests = bool(config.get("allow_merge_without_tests", False))
    contract_prefix = (
        "You are the cc-loop reviewer.\n"
        "\n"
        "## Stable Review Contract\n"
        "Review one implementation attempt and decide whether it is safe to merge.\n"
        "Judge only the current graph node when a graph node is provided.\n"
        "Prefer precise, actionable feedback over broad commentary.\n"
        "Do not request unrelated refactors, style churn, or work outside the scoped node.\n"
        "Treat generated caches, build artifacts, and unrelated file churn as review findings.\n"
        "Before approving, you must inspect test status and diff/patch evidence.\n"
        "When patch or diff stat content is not inlined, read the listed artifact paths or inspect the worktree git diff.\n"
        "Do not approve solely because patch text is omitted from the prompt.\n"
        f"Default to reject when tests failed or timed_out unless allow_merge_without_tests is enabled "
        f"(currently {allow_merge_without_tests}).\n"
        "\n"
        "## Stable Review Rubric\n"
        "- Verify the implementation satisfies the stated acceptance criteria.\n"
        "- Verify tests passed, or explain why the attempt must not merge.\n"
        "- Verify the changed files match the declared scope and do not undo completed dependency work.\n"
        "- Verify the patch does not weaken existing behavior, tests, safety checks, or documented contracts.\n"
        "- Reject if this node re-implements functionality already delivered by completed dependency nodes.\n"
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
        '  "stop_reason": "",\n'
        '  "inspected_artifacts": [],\n'
        '  "inspected_files": [],\n'
        '  "test_status_checked": true\n'
        "}\n"
        'Allowed decisions: "approve", "reject", "stop", "replan".\n'
        'For "approve", keep issues empty and retry_prompt empty.\n'
        'For "reject", include concise issues and a retry_prompt the implementer can execute.\n'
        'For "stop", include stop_reason.\n'
        'For "replan", include replan_reason and replan_prompt or suggested_changes.\n'
        "Set inspected_artifacts to artifact paths you reviewed.\n"
        "Set inspected_files to changed file paths you verified.\n"
        "Set test_status_checked true only after reading test status/output evidence.\n"
        "\n"
    )
    task_context = _build_task_review_context_section(state=state, attempt=attempt)
    dynamic_payload = (
        f"{DYNAMIC_REVIEW_MARKER}\n"
        "Everything below this line may change on every attempt.\n"
        "\n"
        "### Attempt metadata\n"
        f"- Task ID: {state.task_id}\n"
        f"- Iteration: {attempt.iteration}\n"
        f"- Retry: {attempt.retry}\n"
        f"- Implementer exit code: {attempt.implementer_exit_code}\n"
        f"- Test status: {test_status}\n"
        f"- Base commit: {attempt.base_commit}\n"
        f"- Head commit: {attempt.head_commit}\n"
        f"- Graph node: {attempt.graph_node_id or '(legacy single-step)'}\n"
        "\n"
        "### Review context\n"
        f"- Context mode: {context_mode}\n"
        f"- Inline patch: {inline_patch}\n"
        f"- Inline diff stat: {inline_diff_stat}\n"
        f"- Artifact root: {artifact_root or '(unknown)'}\n"
        f"- diff.stat.txt: {diff_stat_path or '(unknown)'}\n"
        f"- diff.files.txt: {diff_files_path or '(unknown)'}\n"
        f"- test.output.txt: {test_output_path or '(unknown)'}\n"
        f"- Patches directory: {patches_dir or '(unknown)'}\n"
        f"- Selected patch paths:\n{selected_patch_paths or '(unknown)'}\n"
        f"- Omitted patch chars: {omitted_patch_chars}\n"
        f"- Omitted diff stat chars: {omitted_diff_stat_chars}\n"
        f"- Estimated avoided cache-miss tokens: {estimated_avoided_tokens}\n"
        "\n"
        "### Test result\n"
        f"Status: {test_status}\n"
        f"Output path: {test_output_path or '(unknown)'}\n\n"
    )
    dynamic_payload += _format_diff_stat_section(
        diff_stat=diff_stat,
        inline_diff_stat=inline_diff_stat,
        diff_stat_path=diff_stat_path,
        diff_files_path=diff_files_path,
    )

    if inline_patch:
        dynamic_payload += (
            "### Selected patches\n"
            f"{patch_body or '(no patch content selected)'}\n"
        )
    else:
        dynamic_payload += (
            "### Patch artifact references\n"
            "Patch content is not inlined. Inspect the selected patch paths above, diff.files.txt, "
            "or run `git diff` in the worktree before approving.\n"
        )

    return (contract_prefix + task_context + dynamic_payload).strip() + "\n"


def _estimated_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return (char_count + 3) // 4


def _classify_cache_health(prefix_ratio: float) -> str:
    if prefix_ratio >= 0.60:
        return "good"
    if prefix_ratio >= 0.40:
        return "warning"
    return "poor"


def build_reviewer_prompt_metrics(
    *,
    prompt: str,
    diff_stat: str,
    patch_body: str,
    inline_patch: bool = True,
    context_mode: str = "inline",
    inline_diff_stat: bool | None = None,
) -> dict[str, Any]:
    """Return cache/cost layout metrics for a reviewer prompt artifact."""
    if inline_diff_stat is None:
        inline_diff_stat = _should_inline_diff_stat(context_mode=context_mode, inline_patch=inline_patch)

    task_context_index = prompt.find(TASK_REVIEW_CONTEXT_MARKER)
    dynamic_index = prompt.find(DYNAMIC_REVIEW_MARKER)
    contract_prefix_chars = (
        task_context_index if task_context_index >= 0 else (dynamic_index if dynamic_index >= 0 else len(prompt))
    )
    stable_prefix_chars = dynamic_index if dynamic_index >= 0 else len(prompt)
    task_context_chars = max(0, stable_prefix_chars - contract_prefix_chars)
    dynamic_payload_chars = len(prompt) - stable_prefix_chars
    patch_chars = len(patch_body)
    inline_patch_chars = patch_chars if inline_patch else 0
    omitted_patch_chars = 0 if inline_patch else patch_chars
    diff_stat_chars = len(diff_stat)
    inline_diff_stat_chars = diff_stat_chars if inline_diff_stat else 0
    omitted_diff_stat_chars = 0 if inline_diff_stat else diff_stat_chars
    evidence_payload_chars = inline_patch_chars + inline_diff_stat_chars
    contract_dynamic_chars = max(0, dynamic_payload_chars - evidence_payload_chars)
    contract_denominator = contract_prefix_chars + contract_dynamic_chars
    stable_prefix_ratio = round(stable_prefix_chars / len(prompt), 6) if prompt else 0.0
    dynamic_payload_ratio = round(dynamic_payload_chars / len(prompt), 6) if prompt else 0.0
    contract_prefix_ratio = (
        round(contract_prefix_chars / contract_denominator, 6) if contract_denominator > 0 else 0.0
    )
    task_context_ratio = round(task_context_chars / len(prompt), 6) if prompt else 0.0
    estimated_omitted_patch_tokens = _estimated_tokens_from_chars(omitted_patch_chars)
    estimated_omitted_diff_stat_tokens = _estimated_tokens_from_chars(omitted_diff_stat_chars)
    estimated_avoidable_miss_tokens = _estimated_tokens_from_chars(
        omitted_patch_chars + omitted_diff_stat_chars
    )
    return {
        "schema_version": 1,
        "layout": "stable-prefix-v1",
        "context_mode": context_mode,
        "inline_patch": inline_patch,
        "inline_diff_stat": inline_diff_stat,
        "dynamic_payload_marker": DYNAMIC_REVIEW_MARKER,
        "task_review_context_marker": TASK_REVIEW_CONTEXT_MARKER,
        "prompt_chars": len(prompt),
        "contract_prefix_chars": contract_prefix_chars,
        "contract_prefix_ratio": contract_prefix_ratio,
        "task_context_chars": task_context_chars,
        "task_context_ratio": task_context_ratio,
        "stable_prefix_chars": stable_prefix_chars,
        "dynamic_payload_chars": dynamic_payload_chars,
        "stable_prefix_ratio": stable_prefix_ratio,
        "dynamic_payload_ratio": dynamic_payload_ratio,
        "evidence_payload_chars": evidence_payload_chars,
        "cache_health": _classify_cache_health(contract_prefix_ratio),
        "total_prompt_cache_health": _classify_cache_health(stable_prefix_ratio),
        "diff_stat_chars": diff_stat_chars,
        "inline_diff_stat_chars": inline_diff_stat_chars,
        "omitted_diff_stat_chars": omitted_diff_stat_chars,
        "patch_body_chars": patch_chars,
        "inline_patch_chars": inline_patch_chars,
        "omitted_patch_chars": omitted_patch_chars,
        "estimated_prompt_tokens": _estimated_tokens_from_chars(len(prompt)),
        "estimated_stable_prefix_tokens": _estimated_tokens_from_chars(stable_prefix_chars),
        "estimated_dynamic_payload_tokens": _estimated_tokens_from_chars(dynamic_payload_chars),
        "estimated_diff_stat_tokens": _estimated_tokens_from_chars(diff_stat_chars),
        "estimated_patch_body_tokens": _estimated_tokens_from_chars(patch_chars),
        "estimated_inline_patch_tokens": _estimated_tokens_from_chars(inline_patch_chars),
        "estimated_omitted_patch_tokens": estimated_omitted_patch_tokens,
        "estimated_omitted_diff_stat_tokens": estimated_omitted_diff_stat_tokens,
        "estimated_avoidable_miss_tokens": estimated_avoidable_miss_tokens,
        "estimated_provider_prompt_tokens": _estimated_tokens_from_chars(len(prompt)),
        "estimated_provider_dynamic_payload_tokens": _estimated_tokens_from_chars(dynamic_payload_chars),
        "recommendations": [],
    }


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
    _write_implementer_prompt_metadata(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        provider_name=provider_name,
    )
    attempt.phase = AttemptPhase.EXECUTING
    save_state(state, state_root)

    provider = get_provider(provider_name)
    timeout_seconds = _implementer_timeout_seconds(config, provider_name)
    run_result = _invoke_provider(
        provider=provider,
        provider_name=provider_name,
        phase_key="implementer",
        state=state,
        state_root=state_root,
        attempt=attempt,
        artifact_paths=artifact_paths,
        worktree=worktree,
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
    _record_uncaptured_patch_if_needed(
        state,
        attempt,
        artifact_paths,
        diff_metadata=diff_metadata,
        worktree=worktree,
    )
    _clear_resolved_patch_not_captured(
        attempt,
        artifact_paths,
        diff_metadata=diff_metadata,
        worktree=worktree,
    )
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
    lower = message.lower()
    timed_out = "timed out" in lower
    interrupted = "interrupted" in lower
    hung = "hung" in lower
    return classify_provider_failure(
        phase=phase,
        provider=provider,
        exit_code=None,
        timed_out=timed_out,
        interrupted=interrupted,
        hung=hung,
        parse_error=message if "parse failed" in lower else "",
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


def _write_planning_prompt_metadata(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    provider_name: str,
) -> None:
    metadata = build_prompt_metadata(
        role="planner",
        provider=provider_name,
        config=state.config,
        state=state,
        attempt=attempt,
        prompt_path=artifact_paths["plan_prompt"],
    )
    write_prompt_metadata(artifact_paths["plan_prompt_meta"], metadata)


def _write_implementer_prompt_metadata(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    provider_name: str,
) -> None:
    metadata = build_prompt_metadata(
        role="implementer",
        provider=provider_name,
        config=state.config,
        state=state,
        attempt=attempt,
        prompt_path=artifact_paths["implementer_prompt"],
    )
    write_prompt_metadata(artifact_paths["implementer_prompt_meta"], metadata)


def _write_reviewer_prompt_metadata(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    provider_name: str,
) -> None:
    metadata = build_prompt_metadata(
        role="reviewer",
        provider=provider_name,
        config=state.config,
        state=state,
        attempt=attempt,
        prompt_path=artifact_paths["review_prompt"],
    )
    write_prompt_metadata(artifact_paths["review_prompt_meta"], metadata)


def _update_implementer_trace(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    config: LoopConfig,
    status: str,
    exit_code: int | None = None,
    error: str = "",
) -> None:
    fields: dict[str, Any] = {
        "prompt_path": str(artifact_paths["implementer_prompt"]),
        "prompt_meta_path": str(artifact_paths["implementer_prompt_meta"]),
        "raw_path": str(artifact_paths["implementer_raw"]),
        "estimated_prompt_tokens": estimate_tokens_from_path(artifact_paths["implementer_prompt"]),
    }
    if exit_code is not None:
        fields["exit_code"] = exit_code
    if error:
        fields["error"] = error
    update_trace_phase(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        phase="implementation",
        status=status,
        **fields,
    )


def _update_review_trace(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    config: LoopConfig,
    status: str,
    raw_paths: list[str],
    last_message_paths: list[str],
    metrics: dict[str, Any],
    decision: str = "",
    error: str = "",
) -> None:
    fields: dict[str, Any] = {
        "prompt_path": str(artifact_paths["review_prompt"]),
        "prompt_meta_path": str(artifact_paths["review_prompt_meta"]),
        "raw_path": str(artifact_paths["review_raw"]),
        "raw_paths": raw_paths,
        "last_message_paths": last_message_paths,
        "metrics_path": str(artifact_paths["review_prompt_metrics"]),
        "estimated_prompt_tokens": metrics["estimated_prompt_tokens"],
        "stable_prefix_ratio": metrics["stable_prefix_ratio"],
        "contract_prefix_ratio": metrics.get("contract_prefix_ratio", ""),
        "cache_health": metrics.get("cache_health", ""),
        "total_prompt_cache_health": metrics.get("total_prompt_cache_health", ""),
    }
    if decision:
        fields["decision"] = decision
    if error:
        fields["error"] = error
    update_trace_phase(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
        phase="review",
        status=status,
        **fields,
    )


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
    _persist_state(state, state_root)


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
    _persist_state(state, state_root)


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
    _persist_state(state, state_root)


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
        if state is not None and merge_blocked_by_test_gate(attempt, state.config, state=state):
            return (
                "reviewer approved but merge is blocked by the test gate; "
                "inspect artifacts, fix tests through a repair path, or allow merge without tests"
            )
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
