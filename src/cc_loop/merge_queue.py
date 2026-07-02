"""Serial merge queue for parallel graph node execution."""

from __future__ import annotations

from cc_loop.state import AttemptRecord, TaskState, TaskStatus, save_state
from cc_loop.task_graph import ensure_task_graph, get_node, graph_complete, mark_node_passed


def enqueue_merge(state: TaskState, node_id: str) -> None:
    queue = list(getattr(state, "merge_queue", None) or [])
    if node_id not in queue:
        queue.append(node_id)
    state.merge_queue = queue


def dequeue_merge(state: TaskState) -> str | None:
    queue = list(getattr(state, "merge_queue", None) or [])
    if not queue:
        return None
    node_id = queue.pop(0)
    state.merge_queue = queue
    return node_id


def pending_merge_count(state: TaskState) -> int:
    return len(getattr(state, "merge_queue", None) or [])


def find_attempt_for_node(state: TaskState, node_id: str) -> AttemptRecord | None:
    running = getattr(state, "running_attempts", None) or {}
    idx = running.get(node_id)
    if idx is not None and 0 <= idx < len(state.history):
        return state.history[idx]
    for attempt in reversed(state.history):
        if attempt.graph_node_id == node_id and attempt.phase.value in {
            "approved",
            "merged",
        }:
            return attempt
    return None


def complete_merge_for_node(
    state: TaskState,
    state_root,
    node_id: str,
    attempt: AttemptRecord,
) -> TaskState:
    graph = ensure_task_graph(state)
    if graph is not None:
        mark_node_passed(graph, node_id, attempt.iteration)
        running = dict(getattr(state, "running_attempts", None) or {})
        running.pop(node_id, None)
        state.running_attempts = running
        if graph_complete(graph):
            state.status = TaskStatus.DONE
        else:
            state.status = TaskStatus.STOPPED
    save_state(state, state_root)
    return state
