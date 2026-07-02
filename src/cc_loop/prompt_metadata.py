"""Prompt version, label, and deployment metadata for cc-loop artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.state import AttemptRecord, TaskState, utc_now_iso

PROMPT_META_SCHEMA_VERSION = 1

ROLE_DEFAULTS: dict[str, dict[str, str]] = {
    "planner": {
        "prompt_name": "cc-loop-planner",
        "prompt_version": "0.10.0",
        "label": "production",
        "layout": "task-graph-v1",
    },
    "implementer": {
        "prompt_name": "cc-loop-implementer",
        "prompt_version": "0.10.0",
        "label": "production",
        "layout": "node-scoped-v1",
    },
    "reviewer": {
        "prompt_name": "cc-loop-reviewer",
        "prompt_version": "0.10.0",
        "label": "production",
        "layout": "stable-prefix-v1",
    },
}


def resolve_provider_model(provider: str, config: LoopConfig) -> str:
    """Return configured model name for a provider when available."""
    key_by_provider = {
        "codex": "codex_model",
        "cursor": "cursor_model",
        "claude-code": "claude_code_model",
    }
    key = key_by_provider.get(provider)
    if not key:
        return ""
    return str(config.get(key, "") or "")


def resolve_implementer_layout(state: TaskState, attempt: AttemptRecord) -> str:
    """Choose implementer layout based on graph vs legacy single-step mode."""
    from cc_loop.task_graph import ensure_task_graph

    graph = ensure_task_graph(state)
    if graph is not None and attempt.graph_node_id:
        return "node-scoped-v1"
    return "legacy-single-step-v1"


def build_prompt_metadata(
    *,
    role: str,
    provider: str,
    config: LoopConfig,
    state: TaskState,
    attempt: AttemptRecord,
    prompt_path: Path,
    layout: str | None = None,
) -> dict[str, Any]:
    """Build prompt metadata payload for one role."""
    defaults = ROLE_DEFAULTS[role]
    resolved_layout = layout
    if resolved_layout is None:
        if role == "implementer":
            resolved_layout = resolve_implementer_layout(state, attempt)
        else:
            resolved_layout = defaults["layout"]

    return {
        "schema_version": PROMPT_META_SCHEMA_VERSION,
        "role": role,
        "prompt_name": defaults["prompt_name"],
        "prompt_version": defaults["prompt_version"],
        "label": defaults["label"],
        "layout": resolved_layout,
        "provider": provider,
        "model": resolve_provider_model(provider, config),
        "task_id": state.task_id,
        "iteration": attempt.iteration,
        "retry": attempt.retry,
        "graph_node_id": attempt.graph_node_id or "",
        "created_at": utc_now_iso(),
        "prompt_path": str(prompt_path),
    }


def write_prompt_metadata(path: Path, metadata: dict[str, Any]) -> Path:
    """Persist prompt metadata adjacent to the prompt artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return path
