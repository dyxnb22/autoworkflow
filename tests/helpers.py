"""Shared helpers for cc-loop tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from cc_loop.config import merge_config
from cc_loop.state import create_initial_state, save_state


def init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, text=True)
    readme = path / "README.md"
    readme.write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )


def make_task(
    *,
    repo: Path,
    state_root: Path,
    task_id: str = "test-task",
    config: dict | None = None,
) -> Path:
    merged = merge_config(
        {
            "planner_provider": "fake-planner",
            "reviewer_provider": "fake-reviewer",
            "implementer_provider": "fake-implementer",
            "test_command": ["true"],
            "auto_merge": True,
            "allow_merge_without_tests": False,
            **(config or {}),
        }
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    state = create_initial_state(
        task_id=task_id,
        goal="add hello.txt",
        target_repo=str(repo),
        base_branch="main",
        base_commit=base_commit,
        config=merged,
    )
    return save_state(state, state_root)


class TempEnv:
    def __init__(self) -> None:
        self._cm = tempfile.TemporaryDirectory()
        self.root = Path(self._cm.__enter__())

    def close(self) -> None:
        self._cm.__exit__(None, None, None)

    def repo(self) -> Path:
        path = self.root / "repo"
        path.mkdir()
        init_git_repo(path)
        return path

    def state_root(self) -> Path:
        path = self.root / "state"
        path.mkdir()
        return path

    def worktree_root(self) -> Path:
        path = self.root / "worktrees"
        path.mkdir()
        return path
