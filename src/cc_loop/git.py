"""Git helpers for target repository validation and base resolution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    """Raised when a git operation fails or preconditions are not met."""


@dataclass(frozen=True)
class GitCommandResult:
    returncode: int
    stdout: str
    stderr: str
    args: tuple[str, ...]


class GitCommandError(GitError):
    """Raised when a git command fails with captured stdout/stderr."""

    def __init__(self, message: str, *, result: GitCommandResult) -> None:
        super().__init__(message)
        self.result = result


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")
    return completed


def _run_git_detailed(repo: Path, *args: str) -> GitCommandResult:
    completed = _run_git(repo, *args, check=False)
    return GitCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        args=args,
    )


def resolve_repo_path(path: Path) -> Path:
    return path.expanduser().resolve()


def is_git_repo(repo: Path) -> bool:
    completed = _run_git(repo, "rev-parse", "--show-toplevel", check=False)
    return completed.returncode == 0


def git_toplevel(repo: Path) -> Path:
    completed = _run_git(repo, "rev-parse", "--show-toplevel")
    return Path(completed.stdout.strip()).resolve()


def dirty_files(repo: Path) -> list[str]:
    completed = _run_git(repo, "status", "--porcelain")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return lines


def is_clean(repo: Path) -> bool:
    return not dirty_files(repo)


def resolve_base_branch(repo: Path, branch: str) -> str:
    completed = _run_git(repo, "rev-parse", "--verify", branch, check=False)
    if completed.returncode == 0:
        return branch

    remote_branch = f"origin/{branch}"
    completed = _run_git(repo, "rev-parse", "--verify", remote_branch, check=False)
    if completed.returncode == 0:
        return remote_branch

    raise GitError(f"base branch not found: {branch}")


def resolve_base_commit(repo: Path, branch: str) -> str:
    resolved_branch = resolve_base_branch(repo, branch)
    completed = _run_git(repo, "rev-parse", resolved_branch)
    return completed.stdout.strip()


def resolve_base_commit_if_possible(repo: Path, branch: str) -> str:
    """Return the base commit when resolvable, otherwise an empty string."""
    if not is_git_repo(repo):
        return ""
    try:
        return resolve_base_commit(repo, branch)
    except GitError:
        return ""


def repo_label(repo: Path) -> str:
    """Stable short name for worktree directory layout."""
    if is_git_repo(repo):
        return git_toplevel(repo).name
    return repo.name


def add_worktree(
    repo: Path,
    *,
    path: Path,
    branch: str,
    base_commit: str,
) -> Path:
    """Create an isolated worktree branch at ``base_commit``.

    Equivalent to::

        git -C <repo> worktree add -b <branch> <path> <base_commit>
    """
    if path.exists():
        raise GitError(f"worktree path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "worktree", "add", "-b", branch, str(path), base_commit)
    return path


def remove_worktree(repo: Path, path: Path, *, force: bool = False) -> None:
    """Remove a worktree registered against ``repo``."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    _run_git(repo, *args)


def prune_worktrees(repo: Path) -> None:
    """Prune stale worktree administrative data."""
    _run_git(repo, "worktree", "prune")


def rev_parse(repo: Path, ref: str = "HEAD") -> str:
    completed = _run_git(repo, "rev-parse", ref)
    return completed.stdout.strip()


def _git_output(repo: Path, *args: str) -> str:
    completed = _run_git(repo, *args, check=False)
    return completed.stdout


def capture_worktree_diff_metadata(
    repo: Path,
    base_commit: str,
    *,
    diff_stat_path: Path,
    diff_files_path: Path,
) -> dict[str, object]:
    """Capture porcelain status and diff summaries for later review phases."""
    porcelain = dirty_files(repo)
    head_commit = rev_parse(repo, "HEAD")

    status_section = "\n".join(porcelain) if porcelain else "(clean)"
    stat_sections: list[str] = []
    file_sections: list[str] = []

    for label, stat_args, name_args in (
        ("base_commit...HEAD", ("diff", "--stat", f"{base_commit}...HEAD"), ("diff", "--name-only", f"{base_commit}...HEAD")),
        ("working tree vs HEAD", ("diff", "--stat", "HEAD"), ("diff", "--name-only", "HEAD")),
        ("staged vs HEAD", ("diff", "--cached", "--stat"), ("diff", "--cached", "--name-only")),
    ):
        stat_text = _git_output(repo, *stat_args).strip()
        if stat_text:
            stat_sections.append(f"## {label}\n{stat_text}")
        name_text = _git_output(repo, *name_args).strip()
        if name_text:
            file_sections.append(f"## {label}\n{name_text}")

    diff_stat_path.parent.mkdir(parents=True, exist_ok=True)
    diff_stat_path.write_text(
        f"## status --porcelain\n{status_section}\n\n" + "\n\n".join(stat_sections) + ("\n" if stat_sections else ""),
        encoding="utf-8",
    )
    diff_files_path.write_text(
        f"## status --porcelain\n{status_section}\n\n" + "\n\n".join(file_sections) + ("\n" if file_sections else ""),
        encoding="utf-8",
    )

    has_committed_changes = bool(_git_output(repo, "rev-list", "--count", f"{base_commit}..HEAD").strip() not in {"", "0"})
    has_changes = bool(porcelain) or has_committed_changes or bool(stat_sections)

    return {
        "has_changes": has_changes,
        "head_commit": head_commit,
        "porcelain": porcelain,
    }


def commit_worktree_changes(worktree: Path, message: str) -> str | None:
    """Stage and commit all worktree changes when dirty; return new HEAD or None."""
    if is_clean(worktree):
        return rev_parse(worktree, "HEAD")
    _run_git(worktree, "add", "-A")
    _run_git(worktree, "commit", "-m", message)
    return rev_parse(worktree, "HEAD")


def current_branch(repo: Path) -> str:
    """Return the short branch name for ``HEAD``, or ``HEAD`` when detached."""
    completed = _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return completed.stdout.strip()


def branch_ref_exists(repo: Path, ref: str) -> bool:
    completed = _run_git(repo, "rev-parse", "--verify", ref, check=False)
    return completed.returncode == 0


def resolve_merge_target_branch(
    repo: Path,
    *,
    resolved_base_branch: str,
    configured_base_branch: str,
) -> str:
    """Return the local branch ref that should receive an auto-merge."""
    if not resolved_base_branch.startswith("origin/"):
        return resolved_base_branch

    local_name = configured_base_branch
    if branch_ref_exists(repo, local_name):
        return local_name

    raise GitError(
        f"cannot merge into remote-tracking branch {resolved_base_branch!r} without a local branch; "
        f"create local branch {local_name!r} tracking {resolved_base_branch!r}, then run `cc-loop resume`"
    )


def _format_git_command_error(action: str, result: GitCommandResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    command = " ".join(("git", *result.args))
    if detail:
        return f"{action}: {command}\n{detail}"
    return f"{action}: {command} failed with exit code {result.returncode}"


def _merge_in_repo(
    repo: Path,
    source_branch: str,
    *,
    message: str,
) -> GitCommandResult:
    return _run_git_detailed(repo, "merge", "--no-ff", "-m", message, source_branch)


def merge_branch_into_base(
    target_repo: Path,
    source_branch: str,
    *,
    resolved_base_branch: str,
    configured_base_branch: str,
    message: str,
    merge_worktree_path: Path,
) -> str:
    """Merge ``source_branch`` into the task's base branch without switching main checkout.

    When the main repository checkout is already on the merge target branch, merge there.
    Otherwise merge through an ephemeral worktree checked out to the base branch so the
    user's current branch in the main checkout is left unchanged.
    """
    merge_target = resolve_merge_target_branch(
        target_repo,
        resolved_base_branch=resolved_base_branch,
        configured_base_branch=configured_base_branch,
    )
    checkout_branch = current_branch(target_repo)

    if checkout_branch == merge_target:
        result = _merge_in_repo(target_repo, source_branch, message=message)
        if result.returncode != 0:
            raise GitCommandError(
                _format_git_command_error("merge into base branch failed", result),
                result=result,
            )
        return rev_parse(target_repo, merge_target)

    if merge_worktree_path.exists():
        raise GitError(f"merge worktree path already exists: {merge_worktree_path}")

    merge_worktree_path.parent.mkdir(parents=True, exist_ok=True)
    add_result = _run_git_detailed(
        target_repo,
        "worktree",
        "add",
        str(merge_worktree_path),
        merge_target,
    )
    if add_result.returncode != 0:
        detail = add_result.stderr.strip() or add_result.stdout.strip()
        if "already checked out" in detail.lower():
            raise GitError(
                f"base branch {merge_target!r} is already checked out in another worktree; "
                f"switch the main checkout to {merge_target!r} or remove the conflicting worktree, "
                f"then run `cc-loop resume`"
            )
        raise GitCommandError(
            _format_git_command_error("failed to create merge worktree", add_result),
            result=add_result,
        )

    try:
        result = _merge_in_repo(merge_worktree_path, source_branch, message=message)
        if result.returncode != 0:
            raise GitCommandError(
                _format_git_command_error("merge into base branch failed", result),
                result=result,
            )
        return rev_parse(target_repo, merge_target)
    finally:
        remove_result = _run_git_detailed(
            target_repo,
            "worktree",
            "remove",
            "--force",
            str(merge_worktree_path),
        )
        if remove_result.returncode != 0:
            prune_worktrees(target_repo)
