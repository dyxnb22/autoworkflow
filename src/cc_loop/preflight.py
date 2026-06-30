"""Preflight checks before starting or resuming a cc-loop attempt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cc_loop.config import LoopConfig
from cc_loop.git import (
    GitError,
    dirty_files,
    is_clean,
    is_git_repo,
    resolve_base_branch,
    resolve_base_commit,
    resolve_repo_path,
)
from cc_loop.providers.base import get_provider


class PreflightError(Exception):
    """Raised when preflight checks fail."""


@dataclass(frozen=True)
class PreflightResult:
    target_repo: Path
    base_branch: str
    base_commit: str


def _verify_test_command(config: LoopConfig) -> None:
    test_command = config.get("test_command")
    if not test_command:
        return
    if not isinstance(test_command, list) or not test_command:
        raise PreflightError("test_command must be a non-empty argv list")
    if not all(isinstance(part, str) and part for part in test_command):
        raise PreflightError("test_command must contain only non-empty strings")


def _configured_provider_names(providers: dict[str, str]) -> set[str]:
    return {name for name in providers.values() if name}


def verify_providers(providers: dict[str, str]) -> None:
    """Verify only providers referenced by configured roles are installed."""
    for provider_name in sorted(_configured_provider_names(providers)):
        try:
            provider = get_provider(provider_name)
            provider.preflight_check()
        except ValueError as exc:
            raise PreflightError(str(exc)) from exc
        except RuntimeError as exc:
            raise PreflightError(str(exc)) from exc


def run_preflight(
    *,
    target_repo: str | Path,
    base_branch: str,
    providers: dict[str, str],
    config: LoopConfig,
) -> PreflightResult:
    repo = resolve_repo_path(Path(target_repo))
    if not repo.is_dir():
        raise PreflightError(f"target repo does not exist: {repo}")

    if not is_git_repo(repo):
        raise PreflightError(f"target repo is not a git repository: {repo}")

    if not is_clean(repo):
        lines = dirty_files(repo)
        preview = "\n".join(lines[:10])
        suffix = "\n..." if len(lines) > 10 else ""
        raise PreflightError(
            "target repo has uncommitted changes; commit or stash before running cc-loop\n"
            f"{preview}{suffix}"
        )

    try:
        resolved_branch = resolve_base_branch(repo, base_branch)
        resolved_commit = resolve_base_commit(repo, base_branch)
    except GitError as exc:
        raise PreflightError(str(exc)) from exc

    _verify_test_command(config)
    verify_providers(providers)

    return PreflightResult(
        target_repo=repo,
        base_branch=resolved_branch,
        base_commit=resolved_commit,
    )
