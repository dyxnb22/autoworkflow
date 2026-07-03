"""LLM gateway and cost analytics export formats."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.state import TaskState, artifacts_dir, plan_artifact_paths, utc_now_iso
from cc_loop.trace import build_trace_snapshot, estimate_tokens_from_path, load_trace

EXPORT_ROW_SCHEMA_VERSION = 1


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _coerce_path_list(value: Any) -> list[Path]:
    if not isinstance(value, list):
        return []
    return [Path(item) for item in value if isinstance(item, str) and item]


def _estimate_tokens_from_paths(paths: list[Path]) -> int:
    return sum(estimate_tokens_from_path(path) for path in paths)


def _base_row(
    *,
    state: TaskState,
    attempt,
    graph_node_id: str,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "schema_version": EXPORT_ROW_SCHEMA_VERSION,
        "task_id": state.task_id,
        "iteration": attempt.iteration,
        "retry": attempt.retry,
        "graph_node_id": graph_node_id,
        "timestamp": timestamp,
    }


def build_export_rows(state: TaskState, state_root: Path) -> list[dict[str, Any]]:
    """Build JSONL export rows from existing state and artifacts."""
    if not state.history:
        return []

    attempt = state.history[-1]
    artifact_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
    artifact_paths = plan_artifact_paths(artifact_root)
    trace = load_trace(artifact_root) or build_trace_snapshot(
        state, attempt, artifact_root, state.config
    )
    providers = trace.get("providers", {})
    models = trace.get("models", {})
    phases = trace.get("phases", {})
    timestamp = utc_now_iso()
    rows: list[dict[str, Any]] = []

    planning = phases.get("planning", {})
    if planning or artifact_paths["plan_prompt"].is_file():
        rows.append(
            {
                **_base_row(
                    state=state,
                    attempt=attempt,
                    graph_node_id=attempt.graph_node_id or "",
                    timestamp=timestamp,
                ),
                "role": "planner",
                "provider": providers.get("planner", state.config.get("planner_provider", "")),
                "model": models.get("planner", ""),
                "prompt_path": planning.get("prompt_path", str(artifact_paths["plan_prompt"])),
                "prompt_meta_path": planning.get(
                    "prompt_meta_path", str(artifact_paths["plan_prompt_meta"])
                ),
                "raw_output_path": planning.get("raw_path", str(artifact_paths["plan_raw"])),
                "estimated_prompt_tokens": planning.get(
                    "estimated_prompt_tokens",
                    estimate_tokens_from_path(artifact_paths["plan_prompt"]),
                ),
                "estimated_output_tokens": estimate_tokens_from_path(
                    artifact_paths["plan_last_message"]
                ),
            }
        )

    implementation = phases.get("implementation", {})
    if implementation or artifact_paths["implementer_prompt"].is_file():
        rows.append(
            {
                **_base_row(
                    state=state,
                    attempt=attempt,
                    graph_node_id=attempt.graph_node_id or "",
                    timestamp=timestamp,
                ),
                "role": "implementer",
                "provider": providers.get(
                    "implementer", attempt.implementer_provider or state.config.get("implementer_provider", "")
                ),
                "model": models.get("implementer", ""),
                "prompt_path": implementation.get(
                    "prompt_path", str(artifact_paths["implementer_prompt"])
                ),
                "prompt_meta_path": implementation.get(
                    "prompt_meta_path", str(artifact_paths["implementer_prompt_meta"])
                ),
                "raw_output_path": implementation.get(
                    "raw_path", str(artifact_paths["implementer_raw"])
                ),
                "estimated_prompt_tokens": implementation.get(
                    "estimated_prompt_tokens",
                    estimate_tokens_from_path(artifact_paths["implementer_prompt"]),
                ),
                "estimated_output_tokens": estimate_tokens_from_path(
                    artifact_paths["implementer_raw"]
                ),
            }
        )

    testing = phases.get("testing", {})
    if testing or artifact_paths["test_output"].is_file() or attempt.test_status:
        rows.append(
            {
                **_base_row(
                    state=state,
                    attempt=attempt,
                    graph_node_id=attempt.graph_node_id or "",
                    timestamp=timestamp,
                ),
                "role": "testing",
                "provider": "",
                "model": "",
                "prompt_path": "",
                "prompt_meta_path": "",
                "raw_output_path": testing.get("output_path", str(artifact_paths["test_output"])),
                "estimated_prompt_tokens": 0,
                "estimated_output_tokens": estimate_tokens_from_path(artifact_paths["test_output"]),
                "test_status": testing.get("status", attempt.test_status),
            }
        )

    review = phases.get("review", {})
    metrics = _safe_read_json(artifact_paths["review_prompt_metrics"])
    if review or artifact_paths["review_prompt"].is_file():
        review_raw_paths = _coerce_path_list(review.get("raw_paths"))
        review_last_message_paths = _coerce_path_list(review.get("last_message_paths"))
        row = {
            **_base_row(
                state=state,
                attempt=attempt,
                graph_node_id=attempt.graph_node_id or "",
                timestamp=timestamp,
            ),
            "role": "reviewer",
            "provider": providers.get(
                "reviewer", attempt.review_provider or state.config.get("reviewer_provider", "")
            ),
            "model": models.get("reviewer", ""),
            "prompt_path": review.get("prompt_path", str(artifact_paths["review_prompt"])),
            "prompt_meta_path": review.get(
                "prompt_meta_path", str(artifact_paths["review_prompt_meta"])
            ),
            "raw_output_path": review.get("raw_path", str(artifact_paths["review_raw"])),
            "estimated_prompt_tokens": review.get(
                "estimated_prompt_tokens",
                estimate_tokens_from_path(artifact_paths["review_prompt"]),
            ),
            "estimated_output_tokens": (
                _estimate_tokens_from_paths(review_last_message_paths)
                if review_last_message_paths
                else estimate_tokens_from_path(artifact_paths["review_last_message"])
            ),
            "decision": review.get("decision", attempt.decision),
        }
        if review_raw_paths:
            row["raw_output_paths"] = [str(path) for path in review_raw_paths]
        if review_last_message_paths:
            row["last_message_paths"] = [str(path) for path in review_last_message_paths]
        stable_prefix_ratio = review.get("stable_prefix_ratio")
        if stable_prefix_ratio is None and metrics is not None:
            stable_prefix_ratio = metrics.get("stable_prefix_ratio")
        if stable_prefix_ratio is not None:
            row["stable_prefix_ratio"] = stable_prefix_ratio
        contract_prefix_ratio = review.get("contract_prefix_ratio")
        if contract_prefix_ratio is None and metrics is not None:
            contract_prefix_ratio = metrics.get("contract_prefix_ratio")
        if contract_prefix_ratio is not None:
            row["contract_prefix_ratio"] = contract_prefix_ratio
        cache_health = review.get("cache_health")
        if cache_health is None and metrics is not None:
            cache_health = metrics.get("cache_health")
        if cache_health:
            row["cache_health"] = cache_health
        rows.append(row)

    merge = phases.get("merge", {})
    if merge or artifact_paths["merge_output"].is_file():
        rows.append(
            {
                **_base_row(
                    state=state,
                    attempt=attempt,
                    graph_node_id=attempt.graph_node_id or "",
                    timestamp=timestamp,
                ),
                "role": "merge",
                "provider": "",
                "model": "",
                "prompt_path": "",
                "prompt_meta_path": "",
                "raw_output_path": merge.get("output_path", str(artifact_paths["merge_output"])),
                "estimated_prompt_tokens": 0,
                "estimated_output_tokens": estimate_tokens_from_path(artifact_paths["merge_output"]),
            }
        )

    return rows


def write_jsonl_export(state: TaskState, state_root: Path, output_path: Path) -> int:
    """Write JSONL export rows for the latest attempt."""
    rows = build_export_rows(state, state_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return len(rows)
