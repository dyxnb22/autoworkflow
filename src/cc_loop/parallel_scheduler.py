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
    plan_artifact_paths,
    save_state,
    worktree_path,
    DEFAULT_WORKTREE_ROOT,
)
from cc_loop.task_graph import (
    GraphNode,
    ensure_task_graph,
    mark_node_running,
    next_runnable_nodes,
)


def _max_parallel(state: TaskState) -> int:
    return max(1, int(state.config.get("max_parallel_nodes", 1) or 1))


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
) -> tuple[TaskState, AttemptRecord]:
    """Run implementer through review for one parallel node (no merge)."""
    from cc_loop.run import (
        run_implementer_phase,
        run_review_phase,
        run_test_phase,
        _run_graph_node_setup,
    )
    from cc_loop.task_graph import get_node, mark_node_running

    graph = ensure_task_graph(state)
    if graph is None:
        raise RuntimeError("parallel execution requires a task graph")
    node = get_node(graph, node_id)
    if node is None:
        raise RuntimeError(f"unknown node {node_id}")

    mark_node_running(graph, node_id)
    attempt.graph_node_id = node_id
    state.status = TaskStatus.RUNNING
    save_state(state, state_root)

    state = _run_graph_node_setup(
        state,
        state_root,
        artifact_paths,
        graph=graph,
        max_retries=int(state.config.get("max_retries_per_step", 2)),
    )
    state = run_implementer_phase(state, state_root, artifact_paths)
    state = run_test_phase(state, state_root, artifact_paths)
    state = run_review_phase(state, state_root, artifact_paths)
    return state, state.history[-1]


def execute_parallel_batch(state: TaskState, state_root: Path) -> TaskState:
    """Start runnable nodes concurrently up to max_parallel_nodes."""
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
    )

    futures_map: dict[str, tuple[AttemptRecord, dict]] = {}
    for node in nodes:
        attempt, paths = prepare_parallel_attempt(state, state_root, node, preflight=preflight)
        state.iteration = attempt.iteration
        state.history.append(attempt)
        running = dict(getattr(state, "running_attempts", None) or {})
        running[node.id] = len(state.history) - 1
        state.running_attempts = running
        futures_map[node.id] = (attempt, paths)
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
            ): node_id
            for node_id, (attempt, paths) in futures_map.items()
        }
        for future in concurrent.futures.as_completed(future_to_node):
            node_id = future_to_node[future]
            try:
                updated_state, attempt = future.result()
                state = updated_state
                if attempt.decision == "approve" and attempt.phase == AttemptPhase.APPROVED:
                    enqueue_merge(state, node_id)
                save_state(state, state_root)
            except Exception:
                from cc_loop.task_graph import mark_node_failed

                graph = ensure_task_graph(state)
                if graph is not None:
                    mark_node_failed(graph, node_id, "parallel execution failed")
                running = dict(state.running_attempts)
                running.pop(node_id, None)
                state.running_attempts = running
                save_state(state, state_root)

    while pending_merge_count(state) > 0:
        from cc_loop.merge_queue import dequeue_merge

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

    return state
