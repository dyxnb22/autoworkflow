"""Prompt cache budget artifact for multi-agent cost observability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig

PROMPT_CACHE_SCHEMA_VERSION = 1

DYNAMIC_PLANNER_MARKER = "## Dynamic Planner Payload"
DYNAMIC_IMPLEMENTER_MARKER = "## Dynamic Implementer Payload"
DYNAMIC_REVIEW_MARKER = "## Dynamic Review Payload"


def estimated_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return (char_count + 3) // 4


def _classify_cache_health(prefix_ratio: float) -> str:
    if prefix_ratio >= 0.60:
        return "good"
    if prefix_ratio >= 0.40:
        return "warning"
    return "poor"


def _split_prompt_at_marker(prompt: str, marker: str) -> tuple[int, int]:
    marker_index = prompt.find(marker)
    if marker_index < 0:
        return len(prompt), 0
    return marker_index, len(prompt) - marker_index


def _phase_layout_metrics(
    *,
    prompt: str,
    marker: str,
    layout: str,
) -> dict[str, Any]:
    stable_prefix_chars, dynamic_payload_chars = _split_prompt_at_marker(prompt, marker)
    prompt_chars = len(prompt)
    stable_prefix_ratio = round(stable_prefix_chars / prompt_chars, 6) if prompt_chars else 0.0
    contract_denominator = stable_prefix_chars + max(
        0, dynamic_payload_chars - _estimate_evidence_chars(prompt, marker)
    )
    contract_prefix_ratio = (
        round(stable_prefix_chars / contract_denominator, 6) if contract_denominator > 0 else 0.0
    )
    return {
        "layout": layout,
        "prompt_chars": prompt_chars,
        "stable_prefix_chars": stable_prefix_chars,
        "dynamic_payload_chars": dynamic_payload_chars,
        "stable_prefix_ratio": stable_prefix_ratio,
        "contract_prefix_ratio": contract_prefix_ratio,
        "cache_health": _classify_cache_health(contract_prefix_ratio),
        "total_prompt_cache_health": _classify_cache_health(stable_prefix_ratio),
        "estimated_prompt_tokens": estimated_tokens_from_chars(prompt_chars),
        "estimated_provider_prompt_tokens": estimated_tokens_from_chars(prompt_chars),
        "estimated_stable_prefix_tokens": estimated_tokens_from_chars(stable_prefix_chars),
        "estimated_dynamic_payload_tokens": estimated_tokens_from_chars(dynamic_payload_chars),
        "estimated_provider_dynamic_payload_tokens": estimated_tokens_from_chars(dynamic_payload_chars),
    }


def _estimate_evidence_chars(prompt: str, marker: str) -> int:
    if marker not in prompt:
        return 0
    dynamic = prompt[prompt.index(marker) :]
    evidence = 0
    for section in ("### Diff stat", "### Selected patches", "### Patch artifact references"):
        start = dynamic.find(section)
        if start >= 0:
            next_heading = dynamic.find("\n### ", start + len(section))
            section_text = dynamic[start : next_heading if next_heading >= 0 else len(dynamic)]
            evidence += len(section_text)
    return evidence


def build_planner_phase_cache(*, prompt: str, skipped: bool = False) -> dict[str, Any]:
    metrics = _phase_layout_metrics(
        prompt=prompt,
        marker=DYNAMIC_PLANNER_MARKER,
        layout="stable-prefix-v1",
    )
    metrics["skipped"] = skipped
    metrics["recommendations"] = []
    if skipped:
        metrics["estimated_provider_prompt_tokens"] = 0
        metrics["estimated_provider_dynamic_payload_tokens"] = 0
        metrics["recommendations"].append("Planner provider skipped via planner_mode=direct.")
    elif metrics["total_prompt_cache_health"] != "good":
        metrics["recommendations"].append(
            "Keep planner stable contract before Dynamic Planner Payload marker."
        )
    return metrics


def build_implementer_phase_cache(*, prompt: str) -> dict[str, Any]:
    metrics = _phase_layout_metrics(
        prompt=prompt,
        marker=DYNAMIC_IMPLEMENTER_MARKER,
        layout="stable-prefix-v1",
    )
    metrics["recommendations"] = []
    if metrics["total_prompt_cache_health"] != "good":
        metrics["recommendations"].append(
            "Keep implementer stable contract before Dynamic Implementer Payload marker."
        )
    return metrics


def build_reviewer_phase_cache(*, metrics: dict[str, Any]) -> dict[str, Any]:
    phase = dict(metrics)
    recommendations: list[str] = list(phase.pop("recommendations", []) or [])
    context_mode = phase.get("context_mode", "inline")
    if context_mode in {"artifact_refs", "hybrid"} and not phase.get("inline_patch", True):
        recommendations.append(
            "Patch content omitted from reviewer prompt; inspect artifact paths or worktree git diff."
        )
    if phase.get("estimated_avoidable_miss_tokens", 0) > 0:
        recommendations.append(
            "Large patch omitted from inline prompt; cache miss tokens reduced via artifact refs."
        )
    if phase.get("cache_health") != "good":
        recommendations.append("Increase stable reviewer prefix ratio before dynamic payload.")
    phase["recommendations"] = recommendations
    return phase


def compute_prompt_cache_totals(phases: dict[str, Any]) -> dict[str, int]:
    estimated_prompt_tokens = 0
    estimated_provider_prompt_tokens = 0
    estimated_dynamic_payload_tokens = 0
    estimated_provider_dynamic_payload_tokens = 0
    estimated_avoidable_miss_tokens = 0
    for phase_data in phases.values():
        if not isinstance(phase_data, dict):
            continue
        estimated_prompt_tokens += int(phase_data.get("estimated_prompt_tokens", 0) or 0)
        estimated_provider_prompt_tokens += int(
            phase_data.get(
                "estimated_provider_prompt_tokens",
                phase_data.get("estimated_prompt_tokens", 0),
            )
            or 0
        )
        estimated_dynamic_payload_tokens += int(
            phase_data.get("estimated_dynamic_payload_tokens", 0) or 0
        )
        estimated_provider_dynamic_payload_tokens += int(
            phase_data.get(
                "estimated_provider_dynamic_payload_tokens",
                phase_data.get("estimated_dynamic_payload_tokens", 0),
            )
            or 0
        )
        estimated_avoidable_miss_tokens += int(
            phase_data.get("estimated_avoidable_miss_tokens", 0) or 0
        )
    return {
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "estimated_provider_prompt_tokens": estimated_provider_prompt_tokens,
        "estimated_dynamic_payload_tokens": estimated_dynamic_payload_tokens,
        "estimated_provider_dynamic_payload_tokens": estimated_provider_dynamic_payload_tokens,
        "estimated_avoidable_miss_tokens": estimated_avoidable_miss_tokens,
    }


def load_prompt_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": PROMPT_CACHE_SCHEMA_VERSION, "phases": {}, "totals": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": PROMPT_CACHE_SCHEMA_VERSION, "phases": {}, "totals": {}}
    if not isinstance(data, dict):
        return {"schema_version": PROMPT_CACHE_SCHEMA_VERSION, "phases": {}, "totals": {}}
    return data


def update_prompt_cache_artifact(
    cache_path: Path,
    *,
    phase: str,
    phase_data: dict[str, Any],
) -> Path:
    payload = load_prompt_cache(cache_path)
    phases = dict(payload.get("phases") or {})
    phases[phase] = phase_data
    payload["schema_version"] = PROMPT_CACHE_SCHEMA_VERSION
    payload["phases"] = phases
    payload["totals"] = compute_prompt_cache_totals(phases)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return cache_path


def prompt_cache_snapshot(cache_path: Path) -> dict[str, Any] | None:
    payload = load_prompt_cache(cache_path)
    totals = payload.get("totals") or {}
    if not totals and not payload.get("phases"):
        return None
    reviewer = (payload.get("phases") or {}).get("reviewer") or {}
    return {
        "path": str(cache_path),
        "estimated_prompt_tokens": totals.get("estimated_prompt_tokens", 0),
        "estimated_provider_prompt_tokens": totals.get("estimated_provider_prompt_tokens", 0),
        "estimated_dynamic_payload_tokens": totals.get("estimated_dynamic_payload_tokens", 0),
        "estimated_provider_dynamic_payload_tokens": totals.get(
            "estimated_provider_dynamic_payload_tokens", 0
        ),
        "estimated_avoidable_miss_tokens": totals.get("estimated_avoidable_miss_tokens", 0),
        "reviewer_context_mode": reviewer.get("context_mode"),
        "reviewer_inline_patch": reviewer.get("inline_patch"),
        "reviewer_omitted_patch_chars": reviewer.get("omitted_patch_chars", 0),
    }
