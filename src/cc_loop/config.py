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
    require_distinct_reviewer: bool
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
    max_wall_clock_seconds: int
    max_consecutive_failures: int
    max_artifact_log_bytes: int
    max_changed_files_per_attempt: int
    stale_heartbeat_seconds: int
    max_parallel_nodes: int
    allow_parallel_execution: bool
    allow_node_policy_weakening: bool
    planner_granularity: str
    planner_mode: str
    auto_direct_planner: bool
    auto_direct_max_goal_chars: int
    review_context_mode: str
    review_inline_patch_threshold: int
    provider_watchdog_grace_seconds: int
    git_timeout_seconds: int


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
    # Default success = tests green + review approve + branch ready for handoff.
    # Merging into the user's base branch is opt-in.
    "auto_merge": False,
    "allow_merge_without_tests": False,
    # Strongly recommended product gate; default true (escape hatch: allow_same_reviewer).
    "require_distinct_reviewer": True,
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
    "max_wall_clock_seconds": 0,
    "max_consecutive_failures": 0,
    "max_artifact_log_bytes": 0,
    "max_changed_files_per_attempt": 0,
    "stale_heartbeat_seconds": 120,
    "max_parallel_nodes": 1,
    "allow_parallel_execution": False,
    "allow_node_policy_weakening": False,
    # Default path is a single closed loop; multi-node graphs are advanced.
    "planner_granularity": "single",
    "planner_mode": "auto",
    "auto_direct_planner": True,
    "auto_direct_max_goal_chars": 500,
    "review_context_mode": "hybrid",
    "review_inline_patch_threshold": 8000,
    "provider_watchdog_grace_seconds": 5,
    "git_timeout_seconds": 60,
}


def merge_config(overrides: dict[str, Any] | None = None) -> LoopConfig:
    """Return a copy of the default config with optional overrides applied."""
    config = deepcopy(DEFAULT_CONFIG)
    if overrides:
        config.update(overrides)
    return config


def role_provider_identity(provider: str, config: LoopConfig) -> tuple[str, str]:
    """Return (provider, model) identity used for distinct-reviewer checks."""
    from cc_loop.prompt_metadata import resolve_provider_model

    name = str(provider or "").strip()
    model = resolve_provider_model(name, config) if name else ""
    return name, model


def distinct_reviewer_satisfied(config: LoopConfig, providers: dict[str, str] | None = None) -> bool:
    """True when implementer and reviewer identities differ (provider and/or model)."""
    roles = providers or {
        "implementer": str(config.get("implementer_provider", "")),
        "reviewer": str(config.get("reviewer_provider", "")),
    }
    implementer = role_provider_identity(str(roles.get("implementer", "")), config)
    reviewer = role_provider_identity(str(roles.get("reviewer", "")), config)
    if not implementer[0] or not reviewer[0]:
        return False
    return implementer != reviewer


def format_distinct_reviewer_error(config: LoopConfig, providers: dict[str, str] | None = None) -> str:
    roles = providers or {
        "implementer": str(config.get("implementer_provider", "")),
        "reviewer": str(config.get("reviewer_provider", "")),
    }
    impl_name, impl_model = role_provider_identity(str(roles.get("implementer", "")), config)
    rev_name, rev_model = role_provider_identity(str(roles.get("reviewer", "")), config)
    impl_label = impl_name + (f" model={impl_model}" if impl_model else "")
    rev_label = rev_name + (f" model={rev_model}" if rev_model else "")
    return (
        "require_distinct_reviewer=true but implementer and reviewer are the same "
        f"({impl_label}); configure a different reviewer provider (and/or model) so the "
        "writer cannot review their own work"
    )
