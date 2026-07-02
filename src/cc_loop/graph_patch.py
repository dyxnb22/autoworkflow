"""Graph patch model, validation, and application for dynamic replanning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from cc_loop.state import utc_now_iso
from cc_loop.task_graph import (
    GraphNode,
    GraphNodeKind,
    GraphNodeStatus,
    TaskGraph,
    _node_index,
    get_node,
)


class PatchOpType(StrEnum):
    ADD_NODE = "add_node"
    UPDATE_NODE = "update_node"
    ADD_DEPENDENCY = "add_dependency"
    REMOVE_DEPENDENCY = "remove_dependency"
    SKIP_NODE = "skip_node"
    UNBLOCK_NODE = "unblock_node"


@dataclass
class GraphPatchOp:
    op: str
    node_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphPatchOp:
        return cls(
            op=str(data.get("op", "")),
            node_id=str(data.get("node_id", "")),
            data=dict(data.get("data") or {}),
        )


@dataclass
class GraphPatch:
    reason: str
    operations: list[GraphPatchOp]

    @classmethod
    def from_planner_json(cls, data: dict[str, Any]) -> GraphPatch:
        ops = [GraphPatchOp.from_dict(item) for item in data.get("operations", [])]
        return cls(reason=str(data.get("reason", "")).strip(), operations=ops)


class GraphPatchError(Exception):
    """Raised when a graph patch is invalid."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def _would_create_cycle(graph: TaskGraph, from_id: str, to_id: str) -> bool:
    """True if adding edge from_id -> to_id creates a cycle."""
    deps: dict[str, set[str]] = {n.id: set(n.dependencies) for n in graph.nodes}
    if to_id not in deps:
        deps[to_id] = set()
    deps[to_id].add(from_id)

    def has_path(start: str, target: str, visited: set[str]) -> bool:
        if start == target:
            return True
        if start in visited:
            return False
        visited.add(start)
        for dep in deps.get(start, set()):
            if has_path(dep, target, visited):
                return True
        return False

    return has_path(to_id, from_id, set())


def validate_patch(graph: TaskGraph, patch: GraphPatch) -> list[str]:
    errors: list[str] = []
    index = _node_index(graph)

    for op in patch.operations:
        try:
            op_type = PatchOpType(op.op)
        except ValueError:
            errors.append(f"unknown patch op: {op.op}")
            continue

        if op_type == PatchOpType.ADD_NODE:
            node_id = str(op.data.get("id", op.node_id)).strip()
            if not node_id:
                errors.append("add_node requires id")
            elif node_id in index:
                errors.append(f"add_node: node {node_id} already exists")

        elif op_type == PatchOpType.UPDATE_NODE:
            node = index.get(op.node_id)
            if node is None:
                errors.append(f"update_node: unknown node {op.node_id}")
            elif node.status == GraphNodeStatus.PASSED:
                if "acceptance_criteria" in op.data:
                    errors.append(f"cannot change acceptance_criteria on passed node {op.node_id}")

        elif op_type in {PatchOpType.ADD_DEPENDENCY, PatchOpType.REMOVE_DEPENDENCY}:
            node = index.get(op.node_id)
            if node is None:
                errors.append(f"{op.op}: unknown node {op.node_id}")
            elif node.status == GraphNodeStatus.PASSED:
                errors.append(f"cannot modify dependencies on passed node {op.node_id}")
            dep_id = str(op.data.get("dependency", "")).strip()
            if op_type == PatchOpType.ADD_DEPENDENCY:
                if dep_id not in index:
                    errors.append(f"add_dependency: unknown dependency {dep_id}")
                elif dep_id == op.node_id:
                    errors.append("add_dependency: self-dependency not allowed")
                elif dep_id and _would_create_cycle(graph, dep_id, op.node_id):
                    errors.append(f"add_dependency: cycle detected for {op.node_id} -> {dep_id}")

        elif op_type == PatchOpType.SKIP_NODE:
            node = index.get(op.node_id)
            if node is None:
                errors.append(f"skip_node: unknown node {op.node_id}")
            elif node.status == GraphNodeStatus.PASSED:
                errors.append(f"cannot skip passed node {op.node_id}")

        elif op_type == PatchOpType.UNBLOCK_NODE:
            node = index.get(op.node_id)
            if node is None:
                errors.append(f"unblock_node: unknown node {op.node_id}")
            elif node.status != GraphNodeStatus.BLOCKED:
                errors.append(f"unblock_node: node {op.node_id} is not blocked")
            else:
                for dep_id in node.dependencies:
                    dep = index.get(dep_id)
                    if dep is not None and dep.status in {
                        GraphNodeStatus.FAILED,
                        GraphNodeStatus.BLOCKED,
                    }:
                        errors.append(
                            f"unblock_node: dependency {dep_id} is terminally failed"
                        )

    return errors


def apply_patch(graph: TaskGraph, patch: GraphPatch) -> TaskGraph:
    errors = validate_patch(graph, patch)
    if errors:
        raise GraphPatchError("; ".join(errors), details={"errors": errors})

    now = utc_now_iso()
    for op in patch.operations:
        op_type = PatchOpType(op.op)

        if op_type == PatchOpType.ADD_NODE:
            node_id = str(op.data.get("id", op.node_id)).strip()
            graph.nodes.append(
                GraphNode(
                    id=node_id,
                    title=str(op.data.get("title", node_id)),
                    description=str(op.data.get("description", "")),
                    kind=GraphNodeKind(str(op.data.get("kind", GraphNodeKind.IMPLEMENTATION.value))),
                    owner=str(op.data.get("owner", "implementer")),
                    dependencies=list(op.data.get("dependencies") or []),
                    acceptance_criteria=list(op.data.get("acceptance_criteria") or []),
                    files_scope=list(op.data.get("files_scope") or []),
                    status=GraphNodeStatus.PENDING,
                    attempt_iterations=[],
                    retry_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )

        elif op_type == PatchOpType.UPDATE_NODE:
            node = get_node(graph, op.node_id)
            assert node is not None
            for key in ("title", "description", "owner", "notes"):
                if key in op.data:
                    setattr(node, key, str(op.data[key]))
            if "kind" in op.data:
                node.kind = GraphNodeKind(str(op.data["kind"]))
            if "acceptance_criteria" in op.data and node.status != GraphNodeStatus.PASSED:
                node.acceptance_criteria = list(op.data["acceptance_criteria"])
            if "files_scope" in op.data:
                node.files_scope = list(op.data["files_scope"])
            node.updated_at = now

        elif op_type == PatchOpType.ADD_DEPENDENCY:
            node = get_node(graph, op.node_id)
            assert node is not None
            dep_id = str(op.data.get("dependency", "")).strip()
            if dep_id and dep_id not in node.dependencies:
                node.dependencies.append(dep_id)
            node.updated_at = now

        elif op_type == PatchOpType.REMOVE_DEPENDENCY:
            node = get_node(graph, op.node_id)
            assert node is not None
            dep_id = str(op.data.get("dependency", "")).strip()
            if dep_id in node.dependencies:
                node.dependencies.remove(dep_id)
            node.updated_at = now

        elif op_type == PatchOpType.SKIP_NODE:
            node = get_node(graph, op.node_id)
            assert node is not None
            node.status = GraphNodeStatus.SKIPPED
            node.notes = str(op.data.get("reason", op.data.get("notes", "skipped")))
            node.updated_at = now

        elif op_type == PatchOpType.UNBLOCK_NODE:
            node = get_node(graph, op.node_id)
            assert node is not None
            node.status = GraphNodeStatus.PENDING
            node.notes = ""
            node.updated_at = now

    if graph.current_node_id and get_node(graph, graph.current_node_id) is None:
        from cc_loop.task_graph import next_runnable_node

        nxt = next_runnable_node(graph)
        graph.current_node_id = nxt.id if nxt else ""

    return graph
