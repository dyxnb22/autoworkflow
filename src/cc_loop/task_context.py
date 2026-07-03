"""Shared task context payload artifact and prompt section formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cc_loop.config import LoopConfig
from cc_loop.prompts import load_prompt_fragment
from cc_loop.state import AttemptRecord, TaskState

TASK_CONTEXT_SCHEMA_VERSION = 1
TASK_CONTEXT_ARTIFACT_NAME = "task.context.json"

TaskContextRole = Literal["implementer", "reviewer"]
TaskContextMode = Literal["inline", "artifact_ref"]

FILE_ARTIFACT_REF_PROVIDERS = frozenset({"codex", "claude-code"})


@dataclass(frozen=True)
class PreparedTaskContext:
    mode: TaskContextMode
    path: Path | None
    payload: dict[str, Any]
    inline_body: str
    prompt_section: str


def resolve_task_context_mode(
    config: LoopConfig,
    *,
    provider: str,
) -> TaskContextMode:
    """Resolve whether task context is inlined or referenced via artifact path."""
    mode = str(config.get("task_context_mode", "inline") or "inline")
    if mode == "auto":
        if provider in FILE_ARTIFACT_REF_PROVIDERS:
            return "artifact_ref"
        return "inline"
    if mode == "artifact_ref":
        return "artifact_ref"
    return "inline"


def build_task_context_payload(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    plan_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared task context payload for one attempt."""
    from cc_loop.prompt_metadata import resolve_implementer_layout
    from cc_loop.task_graph import completed_dependency_labels, ensure_task_graph, get_node

    graph = ensure_task_graph(state)
    layout = resolve_implementer_layout(state, attempt)
    payload: dict[str, Any] = {
        "schema_version": TASK_CONTEXT_SCHEMA_VERSION,
        "task_id": state.task_id,
        "goal": state.goal,
        "layout": layout,
        "graph_node_id": attempt.graph_node_id or "",
        "iteration": state.iteration,
    }

    if graph is not None and attempt.graph_node_id:
        node = get_node(graph, attempt.graph_node_id)
        payload["graph_summary"] = graph.summary or ""
        payload["completed_dependencies"] = completed_dependency_labels(graph, attempt.graph_node_id)
        if node is not None:
            payload["graph_node"] = {
                "id": node.id,
                "title": node.title,
                "description": node.description or "",
                "kind": node.kind.value,
                "owner": node.owner,
                "acceptance_criteria": list(node.acceptance_criteria or []),
                "files_scope": list(node.files_scope or []),
            }
        else:
            payload["graph_node"] = {
                "id": attempt.graph_node_id,
                "title": "(unknown node)",
                "description": "",
                "kind": "",
                "owner": "",
                "acceptance_criteria": [],
                "files_scope": [],
            }
    elif plan_json is not None:
        payload["plan"] = {
            "prompt": str(plan_json.get("prompt", "")).strip(),
            "expected_changes": str(plan_json.get("expected_changes", "")).strip(),
            "acceptance_criteria": str(plan_json.get("acceptance_criteria", "")).strip(),
        }

    return payload


def write_task_context_artifact(path: Path, payload: dict[str, Any]) -> Path:
    """Persist task context JSON and return the written path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def render_inline_task_context(
    *,
    role: TaskContextRole,
    payload: dict[str, Any],
    config: LoopConfig | None = None,
    marker: str,
) -> str:
    """Render the inline task context body for implementer or reviewer prompts."""
    if role == "implementer":
        return _render_implementer_inline(payload, config=config, marker=marker)
    return _render_reviewer_inline(payload, config=config, marker=marker)


def format_task_context_prompt_section(
    *,
    role: TaskContextRole,
    mode: TaskContextMode,
    marker: str,
    context_path: Path | None,
    inline_body: str,
    config: LoopConfig | None = None,
    layout: str = "",
) -> str:
    """Return the task context section for a prompt, inline or artifact-ref."""
    if mode == "artifact_ref" and context_path is not None:
        if role == "reviewer":
            intro_key = "reviewer/task_context_intro.txt"
        elif layout == "node-scoped-v1":
            intro_key = "implementer/task_context_node_intro.txt"
        else:
            intro_key = "implementer/task_context_legacy_intro.txt"
        intro = load_prompt_fragment(intro_key, config=config).strip()
        return (
            f"{marker}\n"
            f"{intro}\n"
            f"Task context file: {context_path}\n"
            "Read that JSON file for goal, graph_node, plan, acceptance_criteria, "
            "files_scope, and completed_dependencies before continuing.\n\n"
        )
    return inline_body


def prepare_task_context(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    role: TaskContextRole,
    provider: str,
    config: LoopConfig,
    artifact_path: Path,
    marker: str,
    plan_json: dict[str, Any] | None = None,
) -> PreparedTaskContext:
    """Build payload, optionally persist artifact, and format the prompt section."""
    payload = build_task_context_payload(state=state, attempt=attempt, plan_json=plan_json)
    inline_body = render_inline_task_context(
        role=role,
        payload=payload,
        config=config,
        marker=marker,
    )
    mode = resolve_task_context_mode(config, provider=provider)
    context_path: Path | None = None
    if mode == "artifact_ref":
        context_path = write_task_context_artifact(artifact_path, payload)
    prompt_section = format_task_context_prompt_section(
        role=role,
        mode=mode,
        marker=marker,
        context_path=context_path,
        inline_body=inline_body,
        config=config,
        layout=str(payload.get("layout", "")),
    )
    return PreparedTaskContext(
        mode=mode,
        path=context_path,
        payload=payload,
        inline_body=inline_body,
        prompt_section=prompt_section,
    )


def _render_implementer_inline(
    payload: dict[str, Any],
    *,
    config: LoopConfig | None,
    marker: str,
) -> str:
    graph_node = payload.get("graph_node")
    if isinstance(graph_node, dict) and graph_node.get("id"):
        criteria = graph_node.get("acceptance_criteria") or ["(none specified)"]
        files_scope = graph_node.get("files_scope") or ["(not restricted)"]
        lines = [
            marker,
            load_prompt_fragment("implementer/task_context_node_intro.txt", config=config).strip(),
            f"Project goal: {payload.get('goal', '')}",
            f"Graph summary: {payload.get('graph_summary') or '(none)'}",
            "",
            f"Current node: {graph_node.get('id')} — {graph_node.get('title', '')}",
            f"Node description: {graph_node.get('description') or '(none)'}",
            f"Node kind: {graph_node.get('kind') or '(unknown)'}",
            f"Node owner: {graph_node.get('owner') or '(unknown)'}",
            "",
            "Acceptance criteria:",
            *[f"- {item}" for item in criteria],
            "",
            "Files scope:",
            *[f"- {item}" for item in files_scope],
        ]
        deps = payload.get("completed_dependencies") or []
        if deps:
            lines.extend(["", "Completed dependencies:", *[f"- {line}" for line in deps]])
        lines.extend(["", load_prompt_fragment("implementer/node_instructions.txt", config=config).strip()])
        return "\n".join(lines).strip() + "\n\n"

    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    lines = [
        marker,
        load_prompt_fragment("implementer/task_context_legacy_intro.txt", config=config).strip(),
        f"Goal: {payload.get('goal', '')}",
        "",
        "Implementation prompt:",
        str(plan.get("prompt", "")).strip(),
    ]
    expected_changes = str(plan.get("expected_changes", "")).strip()
    if expected_changes:
        lines.extend(["", "Expected changes:", expected_changes])
    acceptance_criteria = str(plan.get("acceptance_criteria", "")).strip()
    if acceptance_criteria:
        lines.extend(["", "Acceptance criteria:", acceptance_criteria])
    return "\n".join(lines).strip() + "\n\n"


def _render_reviewer_inline(
    payload: dict[str, Any],
    *,
    config: LoopConfig | None,
    marker: str,
) -> str:
    lines = [
        marker,
        load_prompt_fragment("reviewer/task_context_intro.txt", config=config).strip(),
        f"Task goal: {payload.get('goal', '')}",
    ]
    graph_node = payload.get("graph_node")
    if isinstance(graph_node, dict) and graph_node.get("id"):
        criteria = graph_node.get("acceptance_criteria") or ["(none specified)"]
        files_scope = graph_node.get("files_scope") or ["(none specified)"]
        lines.extend(
            [
                f"Graph node: {graph_node.get('id')}",
                f"Node title: {graph_node.get('title', '')}",
                f"Node kind: {graph_node.get('kind') or '(unknown)'}",
                f"Node description: {graph_node.get('description') or '(none)'}",
                "Acceptance criteria:",
                *[f"- {item}" for item in criteria],
                "Files scope:",
                *[f"- {item}" for item in files_scope],
            ]
        )
        deps = payload.get("completed_dependencies") or []
        if deps:
            lines.append("Completed dependencies:")
            lines.extend(f"- {line}" for line in deps)
        lines.append(
            "Review whether this graph node is complete — not whether the entire project is complete."
        )
    elif payload.get("graph_node_id"):
        lines.extend(
            [
                f"Graph node: {payload.get('graph_node_id')}",
                "Node title: (unknown node)",
            ]
        )
    else:
        lines.append("Graph node: legacy single-step")
    return "\n".join(lines) + "\n\n"
