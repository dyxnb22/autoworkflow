"""Reviewer prompt context mode resolution for cache-friendly review payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from cc_loop.config import LoopConfig

ReviewContextMode = Literal["hybrid", "inline", "artifact_refs"]


def resolve_review_context_mode(
    config: LoopConfig,
    patch_body_chars: int,
) -> tuple[ReviewContextMode, bool]:
    """Return (resolved_mode, inline_patch) for the reviewer prompt."""
    mode = str(config.get("review_context_mode", "hybrid") or "hybrid").strip().lower()
    threshold = int(config.get("review_inline_patch_threshold", 8000) or 8000)

    if mode == "inline":
        return "inline", True
    if mode == "artifact_refs":
        return "artifact_refs", False
    if patch_body_chars <= threshold:
        return "hybrid", True
    return "hybrid", False


def format_patch_path_list(patch_paths: list[Path]) -> str:
    if not patch_paths:
        return "(no patch files selected)"
    return "\n".join(f"- {path}" for path in patch_paths)
