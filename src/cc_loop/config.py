"""Task configuration defaults and helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict


class LoopConfig(TypedDict, total=False):
    max_iterations: int
    max_retries_per_step: int
    codex_timeout_seconds: int
    cursor_timeout_seconds: int
    claude_code_timeout_seconds: int
    test_timeout_seconds: int
    planner_provider: str
    reviewer_provider: str
    implementer_provider: str
    codex_model: str
    cursor_model: str
    claude_code_model: str
    base_branch: str
    auto_merge: bool
    allow_merge_without_tests: bool
    max_review_patch_bytes: int
    cursor_force: bool
    cursor_sandbox: str
    test_command: list[str]
    max_merge_retries: int
    max_merge_recovery_attempts: int
    max_recovery_attempts_per_iteration: int
    auto_recover_merge: bool
    auto_recover_tests: bool
    auto_recover_provider_errors: bool
    recovery_retry_backoff_seconds: int


DEFAULT_CONFIG: LoopConfig = {
    "max_iterations": 10,
    "max_retries_per_step": 2,
    "codex_timeout_seconds": 300,
    "cursor_timeout_seconds": 900,
    "claude_code_timeout_seconds": 600,
    "test_timeout_seconds": 600,
    "planner_provider": "codex",
    "reviewer_provider": "codex",
    "implementer_provider": "cursor",
    "codex_model": "",
    "cursor_model": "",
    "claude_code_model": "",
    "base_branch": "main",
    "auto_merge": True,
    "allow_merge_without_tests": False,
    "max_review_patch_bytes": 60000,
    "cursor_force": False,
    "cursor_sandbox": "",
    "max_merge_retries": 2,
    "max_merge_recovery_attempts": 2,
    "max_recovery_attempts_per_iteration": 3,
    "auto_recover_merge": True,
    "auto_recover_tests": True,
    "auto_recover_provider_errors": True,
    "recovery_retry_backoff_seconds": 0,
}


def merge_config(overrides: dict[str, Any] | None = None) -> LoopConfig:
    """Return a copy of the default config with optional overrides applied."""
    config = deepcopy(DEFAULT_CONFIG)
    if overrides:
        config.update(overrides)
    return config
