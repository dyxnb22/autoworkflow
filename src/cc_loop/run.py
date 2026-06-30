"""Run-path orchestration for cc-loop task execution."""

from __future__ import annotations

import json
from pathlib import Path

from cc_loop.config import LoopConfig
from cc_loop.git import GitError, add_worktree
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


class RunError(Exception):
    """Raised when a run cannot proceed."""


class PlanningError(Exception):
    """Raised when planner execution or parsing fails."""


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


def execute_run(state: TaskState, state_root: Path) -> tuple[TaskState, AttemptRecord, dict[str, Path]]:
    """Run preflight, worktree creation, and the planner phase for one iteration."""
    state, attempt, artifact_paths = prepare_run(state, state_root)

    try:
        state = run_planning_phase(state, state_root, artifact_paths)
    except PlanningError:
        return state, _current_attempt(state), artifact_paths

    state.status = TaskStatus.STOPPED
    save_state(state, state_root)
    return state, _current_attempt(state), artifact_paths


def run_planning_phase(
    state: TaskState,
    state_root: Path,
    artifact_paths: dict[str, Path],
) -> TaskState:
    """Create the worktree, run the planner provider, and persist plan_json."""
    attempt = _current_attempt(state)
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
    save_state(state, state_root)
    return state


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


def _planner_timeout_seconds(config: LoopConfig, provider_name: str) -> int:
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
        cursor_prompt_path=str(artifact_paths["cursor_prompt"]),
        cursor_raw_path=str(artifact_paths["cursor_raw"]),
        test_raw_path=str(artifact_paths["test_output"]),
        diff_stat_path=str(artifact_paths["diff_stat"]),
        review_raw_path=str(artifact_paths["review_raw"]),
    )

    attempt.phase = AttemptPhase.PLANNING
    return attempt
