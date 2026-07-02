"""Parallel graph node scheduling and execution."""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

from cc_loop.merge_queue import enqueue_merge, find_attempt_for_node, pending_merge_count
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    branch_name,
    load_state,
    plan_artifact_paths,
    save_state,
    worktree_path,
    DEFAULT_WORKTREE_ROOT,
)
from cc_loop.state_lock import task_state_lock
from cc_loop.task_graph import (
    GraphNode,
    ensure_task_graph,
    mark_node_running,
    next_runnable_nodes,
)


class ParallelExecutionError(RuntimeError):
    """Raised when parallel execution is requested but not enabled or safe."""


def _max_parallel(state: TaskState) -> int:
    return max(1, int(state.config.get("max_parallel_nodes", 1) or 1))


def parallel_execution_enabled(state: TaskState) -> bool:
    """Return True when parallel node execution is explicitly enabled."""
    return (
        _max_parallel(state) > 1
        and bool(state.config.get("allow_parallel_execution", False))
    )


def discover_parallel_runnable(state: TaskState) -> list[GraphNode]:
    graph = ensure_task_graph(state)
    if graph is None:
        return []
    running = set(getattr(state, "running_attempts", None) or {})
    max_retries = int(state.config.get("max_retries_per_step", 2))
    limit = _max_parallel(state) - len(running)
    if limit <= 0:
        return []
    return next_runnable_nodes(graph, max_retries=max_retries, limit=limit, exclude=running)


def prepare_parallel_attempt(
    state: TaskState,
    state_root: Path,
    node: GraphNode,
    *,
    preflight,
) -> tuple[AttemptRecord, dict]:
    from cc_loop.run import _begin_attempt

    iteration = state.iteration + 1
    retry = 0
    artifact_root = artifacts_dir(state.task_id, iteration, retry, state_root)
    wt = worktree_path(state.task_id, preflight.target_repo, iteration, retry, DEFAULT_WORKTREE_ROOT)
    br = branch_name(state.task_id, iteration, retry)
    paths = plan_artifact_paths(artifact_root)
    attempt = _begin_attempt(
        state=state,
        preflight=preflight,
        iteration=iteration,
        retry=retry,
        worktree=wt,
        branch=br,
        artifact_paths=paths,
        graph_node_id=node.id,
    )
    return attempt, paths


def run_single_node_pipeline(
    state: TaskState,
    state_root: Path,
    node_id: str,
    attempt: AttemptRecord,
    artifact_paths: dict,
    *,
    history_index: int,
) -> tuple[TaskState, AttemptRecord]:
    """Run implementer through review for one parallel node (no merge)."""
    from cc_loop.run import (
        run_implementer_phase,
        run_review_phase,
        run_test_phase,
        _run_graph_node_setup,
    )
    from cc_loop.task_graph import get_node, mark_node_running

    with task_state_lock(state_root, state.task_id):
        state = load_state(state.task_id, state_root)
        graph = ensure_task_graph(state)
        if graph is None:
            raise RuntimeError("parallel execution requires a task graph")
        node = get_node(graph, node_id)
        if node is None:
            raise RuntimeError(f"unknown node {node_id}")
        if history_index >= len(state.history):
            raise RuntimeError(f"missing attempt history index {history_index} for node {node_id}")
        attempt = state.history[history_index]
        mark_node_running(graph, node_id)
        attempt.graph_node_id = node_id
        state.status = TaskStatus.RUNNING
        save_state(state, state_root)

    state = _run_graph_node_setup(
        state,
        state_root,
        artifact_paths,
        graph=ensure_task_graph(state),
        max_retries=int(state.config.get("max_retries_per_step", 2)),
        attempt=attempt,
    )
    state = run_implementer_phase(state, state_root, artifact_paths, attempt=attempt)
    state = run_test_phase(state, state_root, artifact_paths, attempt=attempt)
    state = run_review_phase(state, state_root, artifact_paths, attempt=attempt)

    with task_state_lock(state_root, state.task_id):
        state = load_state(state.task_id, state_root)
        if history_index < len(state.history):
            attempt = state.history[history_index]
        return state, attempt


def execute_parallel_batch(state: TaskState, state_root: Path) -> TaskState:
    """Start runnable nodes concurrently up to max_parallel_nodes."""
    if not parallel_execution_enabled(state):
        raise ParallelExecutionError(
            "parallel execution requires allow_parallel_execution=True and max_parallel_nodes > 1"
        )

    from cc_loop.preflight import run_preflight
    from cc_loop.run import _run_finalize_phase

    nodes = discover_parallel_runnable(state)
    if not nodes:
        return state

    preflight = run_preflight(
        target_repo=state.target_repo,
        base_branch=state.base_branch,
        providers=state.providers,
        config=state.config,
        task_graph=ensure_task_graph(state),
    )

    futures_map: dict[str, tuple[AttemptRecord, dict, int]] = {}
    with task_state_lock(state_root, state.task_id):
        for node in nodes:
            attempt, paths = prepare_parallel_attempt(state, state_root, node, preflight=preflight)
            state.iteration = attempt.iteration
            state.history.append(attempt)
            history_index = len(state.history) - 1
            running = dict(getattr(state, "running_attempts", None) or {})
            running[node.id] = history_index
            state.running_attempts = running
            futures_map[node.id] = (attempt, paths, history_index)
        save_state(state, state_root)

    max_workers = min(len(nodes), _max_parallel(state))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_node = {
            pool.submit(
                run_single_node_pipeline,
                state,
                state_root,
                node_id,
                attempt,
                paths,
                history_index=history_index,
            ): node_id
            for node_id, (attempt, paths, history_index) in futures_map.items()
        }
        for future in concurrent.futures.as_completed(future_to_node):
            node_id = future_to_node[future]
            try:
                _, attempt = future.result()
                with task_state_lock(state_root, state.task_id):
                    state = load_state(state.task_id, state_root)
                    if attempt.decision == "approve" and attempt.phase == AttemptPhase.APPROVED:
                        enqueue_merge(state, node_id)
                    running = dict(getattr(state, "running_attempts", None) or {})
                    running.pop(node_id, None)
                    state.running_attempts = running
                    save_state(state, state_root)
            except Exception:
                from cc_loop.task_graph import mark_node_failed

                with task_state_lock(state_root, state.task_id):
                    state = load_state(state.task_id, state_root)
                    graph = ensure_task_graph(state)
                    if graph is not None:
                        mark_node_failed(graph, node_id, "parallel execution failed")
                    running = dict(getattr(state, "running_attempts", None) or {})
                    running.pop(node_id, None)
                    state.running_attempts = running
                    save_state(state, state_root)

    while pending_merge_count(state) > 0:
        from cc_loop.merge_queue import dequeue_merge

        with task_state_lock(state_root, state.task_id):
            state = load_state(state.task_id, state_root)
            node_id = dequeue_merge(state)
            if node_id is None:
                break
            attempt = find_attempt_for_node(state, node_id)
            if attempt is None:
                continue
            paths = plan_artifact_paths(
                artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
            )
            state, _, _ = _run_finalize_phase(state, state_root, attempt, paths)
            save_state(state, state_root)

    return state
