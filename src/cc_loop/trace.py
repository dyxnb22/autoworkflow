"""Per-attempt trace artifact for observability and cost analytics."""

from __future__ import annotations

import json
from math import ceil
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.prompt_metadata import resolve_provider_model
from cc_loop.state import AttemptRecord, TaskState, plan_artifact_paths

TRACE_SCHEMA_VERSION = 1


def estimate_tokens_from_text(text: str) -> int:
    """Heuristic token estimate: ceil(chars / 4)."""
    if not text:
        return 0
    return ceil(len(text) / 4)


def estimate_tokens_from_path(path: Path | None) -> int:
    if path is None or not path.is_file():
        return 0
    try:
        return estimate_tokens_from_text(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def trace_file_path(artifact_root: Path) -> Path:
    return artifact_root / "attempt.trace.json"


def load_trace(artifact_root: Path) -> dict[str, Any] | None:
    path = trace_file_path(artifact_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_trace(artifact_root: Path, trace: dict[str, Any]) -> Path:
    path = trace_file_path(artifact_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")
    return path


def _role_providers(state: TaskState, attempt: AttemptRecord) -> dict[str, str]:
    return {
        "planner": state.config.get("planner_provider", state.providers.get("planner", "")),
        "implementer": attempt.implementer_provider or state.config.get(
            "implementer_provider", state.providers.get("implementer", "")
        ),
        "reviewer": attempt.review_provider or state.config.get(
            "reviewer_provider", state.providers.get("reviewer", "")
        ),
    }


def _role_models(providers: dict[str, str], config: LoopConfig) -> dict[str, str]:
    return {role: resolve_provider_model(provider, config) for role, provider in providers.items()}


def init_trace(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    config: LoopConfig,
) -> dict[str, Any]:
    providers = _role_providers(state, attempt)
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "task_id": state.task_id,
        "iteration": attempt.iteration,
        "retry": attempt.retry,
        "graph_node_id": attempt.graph_node_id or "",
        "providers": providers,
        "models": _role_models(providers, config),
        "phases": {},
        "artifacts": {key: str(path) for key, path in artifact_paths.items()},
    }


def update_trace_phase(
    *,
    state: TaskState,
    attempt: AttemptRecord,
    artifact_paths: dict[str, Path],
    config: LoopConfig,
    phase: str,
    status: str,
    **fields: Any,
) -> Path:
    """Incrementally update attempt.trace.json for one phase."""
    artifact_root = artifact_paths["plan_prompt"].parent
    trace = load_trace(artifact_root) or init_trace(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
    )
    providers = _role_providers(state, attempt)
    trace["providers"] = providers
    trace["models"] = _role_models(providers, config)
    trace["artifacts"] = {key: str(path) for key, path in artifact_paths.items()}

    phases = trace.setdefault("phases", {})
    phase_data = dict(phases.get(phase, {}))
    phase_data["status"] = status
    phase_data.update(fields)
    phases[phase] = phase_data
    return save_trace(artifact_root, trace)


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def build_trace_snapshot(
    state: TaskState,
    attempt: AttemptRecord,
    artifact_root: Path,
    config: LoopConfig,
) -> dict[str, Any]:
    """Build or read a resilient trace snapshot from existing artifacts."""
    existing = load_trace(artifact_root)
    if existing is not None:
        return existing

    artifact_paths = plan_artifact_paths(artifact_root)
    trace = init_trace(
        state=state,
        attempt=attempt,
        artifact_paths=artifact_paths,
        config=config,
    )

    plan_prompt = artifact_paths["plan_prompt"]
    if plan_prompt.is_file():
        trace["phases"]["planning"] = {
            "status": "completed",
            "prompt_path": str(artifact_paths["plan_prompt"]),
            "prompt_meta_path": str(artifact_paths["plan_prompt_meta"]),
            "raw_path": str(artifact_paths["plan_raw"]),
            "estimated_prompt_tokens": estimate_tokens_from_path(plan_prompt),
        }

    impl_prompt = artifact_paths["implementer_prompt"]
    impl_metrics = _safe_read_json(artifact_paths["implementer_prompt_metrics"])
    if impl_prompt.is_file():
        implementation_phase: dict[str, Any] = {
            "status": "completed" if attempt.implementer_exit_code == 0 else "failed",
            "prompt_path": str(artifact_paths["implementer_prompt"]),
            "prompt_meta_path": str(artifact_paths["implementer_prompt_meta"]),
            "metrics_path": str(artifact_paths["implementer_prompt_metrics"]),
            "raw_path": str(artifact_paths["implementer_raw"]),
            "estimated_prompt_tokens": estimate_tokens_from_path(impl_prompt),
        }
        if impl_metrics is not None:
            implementation_phase["stable_prefix_ratio"] = impl_metrics.get("stable_prefix_ratio")
            implementation_phase["contract_prefix_ratio"] = impl_metrics.get("contract_prefix_ratio")
            implementation_phase["cache_health"] = impl_metrics.get("cache_health")
            implementation_phase["total_prompt_cache_health"] = impl_metrics.get("total_prompt_cache_health")
        trace["phases"]["implementation"] = implementation_phase

    if artifact_paths["test_output"].is_file() or attempt.test_status:
        trace["phases"]["testing"] = {
            "status": attempt.test_status or "unknown",
            "output_path": str(artifact_paths["test_output"]),
            "exit_code": attempt.test_exit_code,
        }

    review_prompt = artifact_paths["review_prompt"]
    metrics = _safe_read_json(artifact_paths["review_prompt_metrics"])
    if review_prompt.is_file():
        review_phase: dict[str, Any] = {
            "status": "completed" if attempt.decision else "unknown",
            "prompt_path": str(artifact_paths["review_prompt"]),
            "prompt_meta_path": str(artifact_paths["review_prompt_meta"]),
            "raw_path": str(artifact_paths["review_raw"]),
            "metrics_path": str(artifact_paths["review_prompt_metrics"]),
            "decision": attempt.decision,
            "estimated_prompt_tokens": estimate_tokens_from_path(review_prompt),
        }
        if metrics is not None:
            review_phase["stable_prefix_ratio"] = metrics.get("stable_prefix_ratio")
            review_phase["contract_prefix_ratio"] = metrics.get("contract_prefix_ratio")
            review_phase["cache_health"] = metrics.get("cache_health")
            review_phase["total_prompt_cache_health"] = metrics.get("total_prompt_cache_health")
        trace["phases"]["review"] = review_phase

    if artifact_paths["merge_output"].is_file() or attempt.phase.value == "merged":
        trace["phases"]["merge"] = {
            "status": "merged" if attempt.phase.value == "merged" else "failed",
            "output_path": str(artifact_paths["merge_output"]),
            "error": attempt.merge_error,
        }

    return trace
