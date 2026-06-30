"""Run-path orchestration for cc-loop task execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.diff import collect_bounded_review_patches, read_diff_stat_summary
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
    if attempt.plan_json is not None and attempt.phase not in {AttemptPhase.PLANNING, AttemptPhase.PREFLIGHT}:
        return state

    config: LoopConfig = state.config
    provider_name = config["planner_provider"]
    worktree = Path(attempt.worktree_path)
    target_repo = Path(state.target_repo)
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
    try:
        run_result = provider.run(
            worktree_path=worktree,
            prompt=prompt,
            output_path=artifact_paths["plan_last_message"],
            config=config,
            timeout_seconds=timeout_seconds,
            raw_output_path=artifact_paths["plan_raw"],
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
    attempt.plan_json = plan_json
    attempt.phase = AttemptPhase.WORKTREE_CREATED
    save_state(state, state_root)
    return state


def run_implementer_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
) -> TaskState:
    """Run the configured implementer provider and capture worktree diff metadata."""
    attempt = _current_attempt(state)
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
    provider_name = config["implementer_provider"]
    worktree = Path(attempt.worktree_path)
    prompt = build_implementer_prompt(state, attempt.plan_json)

    _write_implementer_artifacts_before(
        artifact_paths=artifact_paths,
        prompt=prompt,
        provider_name=provider_name,
    )

    attempt.phase = AttemptPhase.EXECUTING
    save_state(state, state_root)

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
        _mark_implementer_failed(state, state_root)
        raise ImplementingError(f"{provider_name} implementer exited with code {run_result.exit_code}")

    return state


def run_test_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
) -> TaskState:
    """Run the configured test command in the worktree."""
    attempt = _current_attempt(state)
    if attempt.test_status in {"passed", "failed", "skipped", "timed_out"}:
        return state

    config: LoopConfig = state.config
    worktree = Path(attempt.worktree_path)
    test_command = list(config.get("test_command") or [])
    attempt.test_command = test_command
    attempt.phase = AttemptPhase.TESTING
    save_state(state, state_root)

    if not test_command:
        attempt.test_status = "skipped"
        attempt.test_exit_code = None
        artifact_paths["test_output"].write_text("(tests skipped: test_command not configured)\n", encoding="utf-8")
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

    save_state(state, state_root)
    return state


def run_review_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
) -> TaskState:
    """Build bounded review context and run the configured reviewer provider."""
    attempt = _current_attempt(state)
    if attempt.review_json is not None and attempt.decision:
        return state

    config: LoopConfig = state.config
    provider_name = config["reviewer_provider"]
    worktree = Path(attempt.worktree_path)
    attempt.phase = AttemptPhase.REVIEWING
    save_state(state, state_root)

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
    artifact_paths["review_provider"].write_text(provider_name + "\n", encoding="utf-8")
    artifact_paths["review_raw"].write_text("", encoding="utf-8")
    artifact_paths["review_last_message"].write_text("", encoding="utf-8")

    try:
        provider = get_provider(provider_name)
    except ValueError as exc:
        _mark_review_failed(state, state_root)
        raise ReviewError(str(exc)) from exc

    timeout_seconds = _reviewer_timeout_seconds(config, provider_name)
    try:
        run_result = provider.run(
            worktree_path=worktree,
            prompt=prompt,
            output_path=artifact_paths["review_last_message"],
            config=config,
            timeout_seconds=timeout_seconds,
            raw_output_path=artifact_paths["review_raw"],
        )
    except NotImplementedError as exc:
        _mark_review_failed(state, state_root)
        raise ReviewError(str(exc)) from exc

    artifact_paths["review_provider"].write_text(run_result.provider + "\n", encoding="utf-8")
    attempt.review_raw_path = str(artifact_paths["review_raw"])

    if run_result.timed_out:
        _mark_review_failed(state, state_root)
        raise ReviewError(f"{provider_name} reviewer timed out")

    if run_result.exit_code != 0:
        _mark_review_failed(state, state_root)
        raise ReviewError(f"{provider_name} reviewer exited with code {run_result.exit_code}")

    last_message_path = artifact_paths["review_last_message"]
    if not last_message_path.is_file():
        _mark_review_failed(state, state_root)
        raise ReviewError(f"reviewer last-message artifact missing: {last_message_path}")

    try:
        review_json = provider.parse_reviewer_output(last_message_path)
    except (json.JSONDecodeError, KeyError, TypeError, NotImplementedError) as exc:
        _mark_review_failed(state, state_root)
        raise ReviewError(f"reviewer output parse failed: {exc}") from exc

    artifact_paths["review_parsed"].write_text(
        json.dumps(review_json, indent=2) + "\n",
        encoding="utf-8",
    )
    attempt.review_json = review_json
    attempt.decision = str(review_json.get("decision", "reject"))
    attempt.review_provider = run_result.provider

    if attempt.decision == "approve":
        attempt.phase = AttemptPhase.APPROVED
    else:
        attempt.phase = AttemptPhase.REJECTED

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

    if attempt.decision == "reject":
        if _retry_remaining(state, attempt):
            state.status = TaskStatus.STOPPED
            save_state(state, state_root)
            return state, attempt, artifact_paths
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    if not _can_auto_merge(attempt, config):
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    worktree = Path(attempt.worktree_path)
    target_repo = Path(state.target_repo)
    attempt.merge_output_path = str(artifact_paths["merge_output"])
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
        attempt.merge_error = str(exc)
        artifact_paths["merge_output"].write_text(str(exc) + "\n", encoding="utf-8")
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths
    except GitError as exc:
        attempt.merge_error = str(exc)
        artifact_paths["merge_output"].write_text(str(exc) + "\n", encoding="utf-8")
        state.status = TaskStatus.STOPPED
        save_state(state, state_root)
        return state, attempt, artifact_paths

    attempt.phase = AttemptPhase.MERGED
    state.status = TaskStatus.DONE
    save_state(state, state_root)
    return state, attempt, artifact_paths


def build_planner_prompt(state: TaskState) -> str:
    """Construct the stdin prompt for the configured planner provider."""
    return (
        "You are the cc-loop planner. Analyze the task goal and repository checkout.\n"
        "Respond with JSON only using this exact shape:\n"
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
        f"Iteration: {state.iteration}\n"
    )


def build_implementer_prompt(state: TaskState, plan_json: dict[str, Any]) -> str:
    """Construct the prompt for the configured implementer provider."""
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


def build_reviewer_prompt(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    diff_stat: str,
    patch_body: str,
    test_status: str,
) -> str:
    """Construct the reviewer prompt with bounded diff context."""
    return (
        "You are the cc-loop reviewer. Review the implementation attempt.\n"
        "Respond with JSON only using this exact shape:\n"
        "{\n"
        '  "decision": "approve",\n'
        '  "reason": "Why this attempt is acceptable or not",\n'
        '  "issues": [],\n'
        '  "retry_prompt": "",\n'
        '  "stop_reason": ""\n'
        "}\n"
        'Allowed decisions: "approve", "reject", "stop".\n\n'
        f"Task ID: {state.task_id}\n"
        f"Goal: {state.goal}\n"
        f"Iteration: {attempt.iteration}\n"
        f"Retry: {attempt.retry}\n"
        f"Implementer exit code: {attempt.implementer_exit_code}\n"
        f"Test status: {test_status}\n"
        f"Base commit: {attempt.base_commit}\n"
        f"Head commit: {attempt.head_commit}\n\n"
        "## Diff stat\n"
        f"{diff_stat}\n\n"
        "## Selected patches\n"
        f"{patch_body or '(no patch content selected)'}\n"
    )


def _can_auto_merge(attempt: AttemptRecord, config: LoopConfig) -> bool:
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
    if attempt.test_status == "skipped" and not config.get("allow_merge_without_tests", False):
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


def _planner_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
    if provider_name == "cursor":
        return config["cursor_timeout_seconds"]
    return config["codex_timeout_seconds"]


def _implementer_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
    if provider_name == "cursor":
        return config["cursor_timeout_seconds"]
    return config["codex_timeout_seconds"]


def _reviewer_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
    if provider_name == "cursor":
        return config["cursor_timeout_seconds"]
    return config["codex_timeout_seconds"]


def _current_attempt(state: TaskState) -> AttemptRecord:
    if not state.history:
        raise RunError("task has no attempt history")
    return state.history[-1]


def _mark_planning_failed(state: TaskState, state_root: Path) -> None:
    attempt = _current_attempt(state)
    attempt.phase = AttemptPhase.FAILED
    state.status = TaskStatus.FAILED
    save_state(state, state_root)


def _mark_implementer_failed(state: TaskState, state_root: Path) -> None:
    attempt = _current_attempt(state)
    attempt.phase = AttemptPhase.FAILED
    state.status = TaskStatus.FAILED
    save_state(state, state_root)


def _mark_review_failed(state: TaskState, state_root: Path) -> None:
    attempt = _current_attempt(state)
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
) -> AttemptRecord:
    config: LoopConfig = state.config
    attempt = AttemptRecord(
        iteration=iteration,
        retry=retry,
        created_at=utc_now_iso(),
        base_commit=preflight.base_commit,
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


def summarize_attempt(attempt: AttemptRecord) -> str:
    """Human-readable next-action hint for CLI output."""
    if attempt.phase == AttemptPhase.MERGED:
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
