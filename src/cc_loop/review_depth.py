"""Two-stage reviewer depth resolution and escalation heuristics."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from cc_loop.config import LoopConfig

ReviewDepthMode = Literal["standard", "fast", "deep", "auto"]

_SECURITY_PATH_MARKERS = (
    "auth",
    "crypto",
    "password",
    "permission",
    "security",
    "token",
    "secret",
)


def resolve_review_depth_mode(config: LoopConfig) -> ReviewDepthMode:
    mode = str(config.get("review_depth", "standard") or "standard").strip().lower()
    if mode in {"standard", "fast", "deep", "auto"}:
        return mode  # type: ignore[return-value]
    return "standard"


def should_pre_escalate_to_deep(
    *,
    test_status: str,
    changed_file_count: int,
    diff_stat_chars: int,
    patch_paths: list[Path],
    config: LoopConfig,
) -> tuple[bool, list[str]]:
    """Return True when auto mode should skip fast review and run deep review."""
    reasons: list[str] = []
    if test_status in {"failed", "timed_out"}:
        reasons.append("test_not_passed")
    max_files = int(config.get("review_fast_max_changed_files", 10) or 10)
    if changed_file_count > max_files:
        reasons.append(f"changed_files>{max_files}")
    max_diff_stat = int(config.get("review_escalate_diff_stat_chars", 8000) or 8000)
    if diff_stat_chars > max_diff_stat:
        reasons.append(f"diff_stat_chars>{max_diff_stat}")
    for patch_path in patch_paths:
        lowered = str(patch_path).lower()
        if any(marker in lowered for marker in _SECURITY_PATH_MARKERS):
            reasons.append("security_sensitive_path")
            break
    return bool(reasons), reasons
