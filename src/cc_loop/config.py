"""Task configuration defaults and helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict


class LoopConfig(TypedDict, total=False):
    max_iterations: int
    max_retries_per_step: int
    codex_timeout_seconds: int
    cursor_timeout_seconds: int
    test_timeout_seconds: int
    planner_provider: str
    reviewer_provider: str
    implementer_provider: str
    codex_model: str
    cursor_model: str
    base_branch: str
    auto_merge: bool
    allow_merge_without_tests: bool
    max_review_patch_bytes: int
    cursor_force: bool
    cursor_sandbox: str
    test_command: list[str]


DEFAULT_CONFIG: LoopConfig = {
    "max_iterations": 10,
    "max_retries_per_step": 2,
    "codex_timeout_seconds": 300,
    "cursor_timeout_seconds": 900,
    "test_timeout_seconds": 600,
    "planner_provider": "codex",
    "reviewer_provider": "codex",
    "implementer_provider": "cursor",
    "codex_model": "",
    "cursor_model": "",
    "base_branch": "main",
    "auto_merge": True,
    "allow_merge_without_tests": False,
    "max_review_patch_bytes": 60000,
    "cursor_force": False,
    "cursor_sandbox": "",
}


def merge_config(overrides: dict[str, Any] | None = None) -> LoopConfig:
    """Return a copy of the default config with optional overrides applied."""
    config = deepcopy(DEFAULT_CONFIG)
    if overrides:
        config.update(overrides)
    return config
