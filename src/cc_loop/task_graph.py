"""Task graph models and dispatcher for cc-loop v0.4."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from cc_loop.state import TaskState, utc_now_iso

TASK_GRAPH_SCHEMA_VERSION = 1


class GraphNodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class GraphNodeKind(StrEnum):
    IMPLEMENTATION = "implementation"
    TEST = "test"
    DOCS = "docs"
    REFACTOR = "refactor"
    REVIEW = "review"
    INTEGRATION = "integration"


_TERMINAL_FAILURE_STATUSES = {
    GraphNodeStatus.FAILED,
    GraphNodeStatus.BLOCKED,
}


@dataclass
class GraphNode:
    id: str
    title: str
    description: str
    kind: GraphNodeKind
    owner: str
    dependencies: list[str]
    acceptance_criteria: list[str]
    files_scope: list[str]
    status: GraphNodeStatus
    attempt_iterations: list[int]
    retry_count: int
    created_at: str
    updated_at: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphNode:
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            kind=GraphNodeKind(data.get("kind", GraphNodeKind.IMPLEMENTATION.value)),
            owner=data.get("owner", "implementer"),
            dependencies=list(data.get("dependencies") or []),
            acceptance_criteria=_coerce_str_list(data.get("acceptance_criteria")),
            files_scope=_coerce_str_list(data.get("files_scope")),
            status=GraphNodeStatus(data.get("status", GraphNodeStatus.PENDING.value)),
            attempt_iterations=list(data.get("attempt_iterations") or []),
            retry_count=int(data.get("retry_count", 0)),
            created_at=data.get("created_at", utc_now_iso()),
            updated_at=data.get("updated_at", utc_now_iso()),
            notes=data.get("notes", ""),
        )


@dataclass
class TaskGraph:
    schema_version: int
    nodes: list[GraphNode]
    current_node_id: str
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "nodes": [node.to_dict() for node in self.nodes],
            "current_node_id": self.current_node_id,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskGraph:
        return cls(
            schema_version=int(data.get("schema_version", TASK_GRAPH_SCHEMA_VERSION)),
            nodes=[GraphNode.from_dict(item) for item in data.get("nodes", [])],
            current_node_id=data.get("current_node_id", ""),
            summary=data.get("summary", ""),
        )


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()] or [text]


def _node_index(graph: TaskGraph) -> dict[str, GraphNode]:
    return {node.id: node for node in graph.nodes}


def _unknown_dependencies(graph: TaskGraph, node: GraphNode) -> list[str]:
    known = set(_node_index(graph))
    return [dep for dep in node.dependencies if dep not in known]


def _dependency_statuses(graph: TaskGraph, node: GraphNode) -> list[GraphNodeStatus]:
    index = _node_index(graph)
    return [index[dep].status for dep in node.dependencies if dep in index]


def _has_terminal_dependency_failure(graph: TaskGraph, node: GraphNode) -> bool:
    return any(status in _TERMINAL_FAILURE_STATUSES for status in _dependency_statuses(graph, node))


def _dependencies_passed(graph: TaskGraph, node: GraphNode) -> bool:
    unknown = _unknown_dependencies(graph, node)
    if unknown:
        return False
    if not node.dependencies:
        return True
    return all(status == GraphNodeStatus.PASSED for status in _dependency_statuses(graph, node))


def _is_runnable_status(status: GraphNodeStatus, retry_count: int, max_retries: int) -> bool:
    if status == GraphNodeStatus.PENDING:
        return True
    if status == GraphNodeStatus.REJECTED:
        return retry_count < max_retries
    return False


def ensure_task_graph(state: TaskState) -> TaskGraph | None:
    """Return the task graph from state, or None if absent."""
    graph = getattr(state, "task_graph", None)
    if graph is None:
        return None
    if isinstance(graph, TaskGraph):
        return graph
    if isinstance(graph, dict):
        return TaskGraph.from_dict(graph)
    return None


def graph_from_planner_json(plan_json: dict[str, Any]) -> TaskGraph:
    """Build a TaskGraph from planner JSON (task_graph or legacy shape)."""
    if plan_json.get("mode") == "task_graph" or isinstance(plan_json.get("nodes"), list):
        return _graph_from_task_graph_json(plan_json)
    return _wrap_legacy_planner_json(plan_json)


def _graph_from_task_graph_json(plan_json: dict[str, Any]) -> TaskGraph:
    now = utc_now_iso()
    nodes: list[GraphNode] = []
    for item in plan_json.get("nodes", []):
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id", "")).strip()
        if not node_id:
            continue
        nodes.append(
            GraphNode(
                id=node_id,
                title=str(item.get("title", node_id)).strip(),
                description=str(item.get("description", "")).strip(),
                kind=GraphNodeKind(str(item.get("kind", GraphNodeKind.IMPLEMENTATION.value))),
                owner=str(item.get("owner", "implementer")).strip(),
                dependencies=[str(dep).strip() for dep in item.get("dependencies") or [] if str(dep).strip()],
                acceptance_criteria=_coerce_str_list(item.get("acceptance_criteria")),
                files_scope=_coerce_str_list(item.get("files_scope")),
                status=GraphNodeStatus.PENDING,
                attempt_iterations=[],
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
        )
    if not nodes:
        raise ValueError("task graph planner output contained no valid nodes")

    _apply_invalid_dependency_blocking(nodes)
    first = next_runnable_node(TaskGraph(TASK_GRAPH_SCHEMA_VERSION, nodes, "")) or nodes[0]
    return TaskGraph(
        schema_version=TASK_GRAPH_SCHEMA_VERSION,
        nodes=nodes,
        current_node_id=first.id,
        summary=str(plan_json.get("summary", "")).strip(),
    )


def _wrap_legacy_planner_json(plan_json: dict[str, Any]) -> TaskGraph:
    now = utc_now_iso()
    acceptance = _coerce_str_list(plan_json.get("acceptance_criteria"))
    files_scope = _coerce_str_list(plan_json.get("expected_changes"))
    node = GraphNode(
        id="T1",
        title="Implementation step",
        description=str(plan_json.get("prompt", "")).strip(),
        kind=GraphNodeKind.IMPLEMENTATION,
        owner="implementer",
        dependencies=[],
        acceptance_criteria=acceptance,
        files_scope=files_scope,
        status=GraphNodeStatus.PENDING,
        attempt_iterations=[],
        retry_count=0,
        created_at=now,
        updated_at=now,
    )
    return TaskGraph(
        schema_version=TASK_GRAPH_SCHEMA_VERSION,
        nodes=[node],
        current_node_id="T1",
        summary=str(plan_json.get("expected_changes", "")).strip(),
    )


def _apply_invalid_dependency_blocking(nodes: list[GraphNode]) -> None:
    known = {node.id for node in nodes}
    for node in nodes:
        unknown = [dep for dep in node.dependencies if dep not in known]
        if unknown:
            node.status = GraphNodeStatus.BLOCKED
            node.notes = f"unknown dependencies: {', '.join(unknown)}"
            node.updated_at = utc_now_iso()


def get_node(graph: TaskGraph, node_id: str) -> GraphNode | None:
    for node in graph.nodes:
        if node.id == node_id:
            return node
    return None


def next_runnable_node(graph: TaskGraph, *, max_retries: int = 2) -> GraphNode | None:
    """Return the first runnable node in graph order, or None."""
    for node in graph.nodes:
        if _unknown_dependencies(graph, node):
            if node.status == GraphNodeStatus.PENDING:
                node.status = GraphNodeStatus.BLOCKED
                node.notes = f"unknown dependencies: {', '.join(_unknown_dependencies(graph, node))}"
                node.updated_at = utc_now_iso()
            continue
        if _has_terminal_dependency_failure(graph, node):
            if node.status == GraphNodeStatus.PENDING:
                node.status = GraphNodeStatus.BLOCKED
                node.notes = "blocked by failed dependency"
                node.updated_at = utc_now_iso()
            continue
        if not _dependencies_passed(graph, node):
            continue
        if _is_runnable_status(node.status, node.retry_count, max_retries):
            return node
    return None


def mark_node_running(graph: TaskGraph, node_id: str) -> None:
    node = get_node(graph, node_id)
    if node is None:
        raise KeyError(f"graph node not found: {node_id}")
    node.status = GraphNodeStatus.RUNNING
    node.updated_at = utc_now_iso()
    graph.current_node_id = node_id


def mark_node_passed(graph: TaskGraph, node_id: str, attempt_iteration: int) -> None:
    node = get_node(graph, node_id)
    if node is None:
        raise KeyError(f"graph node not found: {node_id}")
    node.status = GraphNodeStatus.PASSED
    if attempt_iteration not in node.attempt_iterations:
        node.attempt_iterations.append(attempt_iteration)
    node.updated_at = utc_now_iso()
    graph.current_node_id = node_id


def mark_node_rejected(graph: TaskGraph, node_id: str, reason: str) -> None:
    node = get_node(graph, node_id)
    if node is None:
        raise KeyError(f"graph node not found: {node_id}")
    node.status = GraphNodeStatus.REJECTED
    node.retry_count += 1
    if reason.strip():
        node.notes = reason.strip()
    node.updated_at = utc_now_iso()
    graph.current_node_id = node_id


def mark_node_failed(graph: TaskGraph, node_id: str, reason: str) -> None:
    node = get_node(graph, node_id)
    if node is None:
        raise KeyError(f"graph node not found: {node_id}")
    node.status = GraphNodeStatus.FAILED
    if reason.strip():
        node.notes = reason.strip()
    node.updated_at = utc_now_iso()
    graph.current_node_id = node_id


def graph_complete(graph: TaskGraph) -> bool:
    """True when all required nodes have passed (skipped nodes are allowed)."""
    if not graph.nodes:
        return False
    for node in graph.nodes:
        if node.status == GraphNodeStatus.SKIPPED:
            continue
        if node.status != GraphNodeStatus.PASSED:
            return False
    return True


def graph_status_summary(graph: TaskGraph) -> dict[str, int]:
    counts = {
        "total": len(graph.nodes),
        "pending": 0,
        "running": 0,
        "passed": 0,
        "failed": 0,
        "rejected": 0,
        "blocked": 0,
        "skipped": 0,
    }
    for node in graph.nodes:
        key = node.status.value
        if key in counts:
            counts[key] += 1
    return counts


def build_graph_snapshot(graph: TaskGraph) -> dict[str, Any]:
    """Build the integration/status JSON task_graph block."""
    return {
        "schema_version": graph.schema_version,
        "current_node_id": graph.current_node_id,
        "summary": graph_status_summary(graph),
        "nodes": [
            {
                "id": node.id,
                "title": node.title,
                "kind": node.kind.value,
                "owner": node.owner,
                "dependencies": list(node.dependencies),
                "status": node.status.value,
                "retry_count": node.retry_count,
            }
            for node in graph.nodes
        ],
    }


def completed_dependency_labels(graph: TaskGraph, node_id: str) -> list[str]:
    """Human-readable labels for passed dependencies of a node."""
    node = get_node(graph, node_id)
    if node is None:
        return []
    index = _node_index(graph)
    labels = []
    for dep_id in node.dependencies:
        dep = index.get(dep_id)
        if dep is not None and dep.status == GraphNodeStatus.PASSED:
            labels.append(f"{dep.id}: {dep.title}")
    return labels


def legacy_is_final_step(plan_json: dict[str, Any] | None, graph: TaskGraph | None) -> bool:
    """Whether the task should stop after the current node (legacy or single-node graph)."""
    if graph is not None:
        if len(graph.nodes) == 1 and graph_complete(graph):
            return True
        if graph_complete(graph):
            return True
        return False
    if plan_json is None:
        return True
    return bool(plan_json.get("is_final_step", True))
