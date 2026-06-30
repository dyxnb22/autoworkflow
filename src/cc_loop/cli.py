"""cc-loop command-line interface."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from cc_loop import __version__
from cc_loop.config import merge_config
from cc_loop.git import resolve_base_commit_if_possible
from cc_loop.preflight import PreflightError
from cc_loop.providers import codex, cursor  # noqa: F401 — register built-in providers
from cc_loop.run import RunError, execute_run
from cc_loop.state import (
    DEFAULT_STATE_ROOT,
    TaskStatus,
    create_initial_state,
    load_state,
    save_state,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cc-loop", description="Local coding agent orchestrator")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help=f"Root directory for task state (default: {DEFAULT_STATE_ROOT})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize a new cc-loop task")
    init_parser.add_argument("--goal", required=True, help="High-level task goal")
    init_parser.add_argument("--repo", required=True, type=Path, help="Target git repository")
    init_parser.add_argument("--task-id", help="Optional task identifier")
    init_parser.add_argument("--base-branch", default="main", help="Base branch name")

    subparsers.add_parser("run", help="Run or continue the active task loop")
    subparsers.add_parser("resume", help="Resume an interrupted task")
    subparsers.add_parser("status", help="Show current task status")

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(f"error: target repo does not exist: {repo}", file=sys.stderr)
        return 1

    task_id = args.task_id or uuid.uuid4().hex[:12]
    config = merge_config({"base_branch": args.base_branch})
    base_commit = resolve_base_commit_if_possible(repo, args.base_branch)
    state = create_initial_state(
        task_id=task_id,
        goal=args.goal,
        target_repo=str(repo),
        base_branch=args.base_branch,
        base_commit=base_commit,
        config=config,
    )
    path = save_state(state, args.state_root)
    print(f"initialized task {task_id}")
    print(f"state: {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    task_id = _resolve_task_id(args.state_root)
    if task_id is None:
        print("error: no task found; run `cc-loop init` first", file=sys.stderr)
        return 1

    state = load_state(task_id, args.state_root)
    print(f"task_id: {state.task_id}")
    print(f"status: {state.status.value}")
    print(f"goal: {state.goal}")
    print(f"target_repo: {state.target_repo}")
    print(f"iteration: {state.iteration}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    task_id = _resolve_task_id(args.state_root)
    if task_id is None:
        print("error: no task found; run `cc-loop init` first", file=sys.stderr)
        return 1

    state = load_state(task_id, args.state_root)
    try:
        state, attempt, artifact_paths = execute_run(state, args.state_root)
    except RunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PreflightError as exc:
        print(f"error: preflight failed: {exc}", file=sys.stderr)
        return 1

    print(f"task_id: {state.task_id}")
    print(f"status: {state.status.value}")
    print(f"iteration: {state.iteration}")
    print(f"phase: {attempt.phase.value}")
    print(f"base_commit: {state.base_commit}")
    print(f"worktree_path: {attempt.worktree_path}")
    print(f"branch: {attempt.branch}")
    print(f"artifacts: {artifact_paths['plan_prompt'].parent}")

    if state.status == TaskStatus.FAILED:
        if attempt.implementer_exit_code is not None:
            print(
                "error: implementer phase failed; artifacts preserved in state history",
                file=sys.stderr,
            )
        else:
            print(
                "error: planner phase failed; artifacts preserved in state history",
                file=sys.stderr,
            )
        return 2

    if attempt.plan_json is None:
        print("error: planner phase did not produce plan_json", file=sys.stderr)
        return 2

    if attempt.implementer_exit_code is None:
        print("error: implementer phase did not record implementer_exit_code", file=sys.stderr)
        return 2

    print(f"implementer_exit_code: {attempt.implementer_exit_code}")
    print(f"implementer_provider: {attempt.implementer_provider}")
    print(f"head_commit: {attempt.head_commit}")
    print(f"diff_stat: {artifact_paths['diff_stat']}")
    print(f"diff_files: {artifact_paths['diff_files']}")

    print(
        "stopped after implementer phase "
        "(tests, reviewer, and merge are not implemented yet)",
        file=sys.stderr,
    )
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    print("error: `cc-loop resume` is not implemented yet", file=sys.stderr)
    return 2


def _resolve_task_id(state_root: Path) -> str | None:
    tasks_dir = state_root / "tasks"
    if not tasks_dir.is_dir():
        return None
    candidates = sorted(tasks_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if (candidate / "state.json").is_file():
            return candidate.name
    return None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "init": cmd_init,
        "run": cmd_run,
        "resume": cmd_resume,
        "status": cmd_status,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
