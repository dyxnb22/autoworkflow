"""cc-loop command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from cc_loop import __version__
from cc_loop.config import merge_config
from cc_loop.detach import spawn_detached_auto
from cc_loop.git import resolve_base_commit_if_possible
from cc_loop.failure import FailureReport, FailureType, RecoveryDisposition, failure_report_path
from cc_loop.inspect import build_status_snapshot, clear_runner_pid_if_matches, runner_log_path
from cc_loop.recovery import (
    AutoStep,
    decide_auto_step,
    increment_recovery_counter,
    maybe_backoff,
    persist_failure_state,
)
from cc_loop.list_tasks import format_task_line, iter_tasks
from cc_loop.preflight import PreflightError, run_doctor_preflight
from cc_loop.providers import claude_code, codex, cursor  # noqa: F401 — register built-in providers
from cc_loop.run import (
    ImplementingError,
    PlanningError,
    ResumeError,
    ReviewError,
    RunError,
    classify_provider_exception,
    execute_repair_recovery,
    execute_resume,
    execute_run,
    soft_reset_provider_failure,
    summarize_attempt,
)
from cc_loop.state import (
    DEFAULT_STATE_ROOT,
    AttemptPhase,
    TaskStatus,
    artifacts_dir,
    create_initial_state,
    load_state,
    plan_artifact_paths,
    save_state,
    state_path,
)


def _task_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", help="Explicit task identifier")


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
    goal_group = init_parser.add_mutually_exclusive_group(required=True)
    goal_group.add_argument("--goal", help="High-level task goal")
    goal_group.add_argument("--goal-file", type=Path, help="Read goal from file")
    init_parser.add_argument("--repo", required=True, type=Path, help="Target git repository")
    init_parser.add_argument("--task-id", help="Optional task identifier")
    init_parser.add_argument("--base-branch", default="main", help="Base branch name")
    init_parser.add_argument(
        "--test-command", nargs="+", metavar="ARG", default=None, help="Test command argv (e.g. pytest tests/)"
    )
    init_parser.add_argument("--planner", default=None, help="Planner provider name (default: codex)")
    init_parser.add_argument("--reviewer", default=None, help="Reviewer provider name (default: codex)")
    init_parser.add_argument("--implementer", default=None, help="Implementer provider name (default: cursor)")
    init_parser.add_argument(
        "--allow-merge-without-tests", action="store_true", default=False,
        help="Allow auto-merge when no test command is configured",
    )
    init_parser.add_argument("--max-iterations", type=int, default=None, help="Override max_iterations")
    init_parser.add_argument("--max-retries", type=int, default=None, help="Override max_retries_per_step")
    init_parser.add_argument("--codex-model", default=None, help="Codex model override")
    init_parser.add_argument("--cursor-model", default=None, help="Cursor model override")
    init_parser.add_argument("--claude-code-model", default=None, help="Claude Code model override")
    init_parser.add_argument("--cursor-force", action="store_true", default=False, help="Pass --force to cursor agent")
    init_parser.add_argument("--cursor-sandbox", default=None, help="Cursor sandbox mode override")

    run_parser = subparsers.add_parser("run", help="Start the task loop from an initialized task")
    _task_id_arg(run_parser)

    resume_parser = subparsers.add_parser("resume", help="Resume an interrupted or stopped task")
    _task_id_arg(resume_parser)

    auto_parser = subparsers.add_parser("auto", help="Run the full task loop until completion or failure")
    _task_id_arg(auto_parser)
    auto_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Override max_iterations from config",
    )
    auto_parser.add_argument(
        "--detach",
        action="store_true",
        default=False,
        help="Spawn detached background auto runner and exit immediately",
    )

    status_parser = subparsers.add_parser("status", help="Show current task status")
    _task_id_arg(status_parser)
    status_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON")

    list_parser = subparsers.add_parser("list", help="List tasks under the state root")
    list_parser.add_argument("--repo", type=Path, default=None, help="Filter by target repository path")
    list_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON")

    doctor_parser = subparsers.add_parser("doctor", help="Run preflight checks without creating a task")
    doctor_parser.add_argument("--repo", required=True, type=Path, help="Target git repository")
    doctor_parser.add_argument("--base-branch", default="main", help="Base branch name")
    doctor_parser.add_argument("--planner", default=None, help="Planner provider name")
    doctor_parser.add_argument("--reviewer", default=None, help="Reviewer provider name")
    doctor_parser.add_argument("--implementer", default=None, help="Implementer provider name")
    doctor_parser.add_argument(
        "--test-command", nargs="+", metavar="ARG", default=None, help="Test command argv to validate",
    )
    doctor_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON")

    return parser


def resolve_task_id(state_root: Path, task_id: str | None) -> str | None:
    if task_id:
        path = state_path(task_id, state_root)
        if not path.is_file():
            print(f"error: task not found: {task_id}", file=sys.stderr)
            return None
        return task_id

    tasks_dir = state_root / "tasks"
    if not tasks_dir.is_dir():
        return None
    candidates = sorted(tasks_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if (candidate / "state.json").is_file():
            return candidate.name
    return None


def _read_goal(args: argparse.Namespace) -> str | None:
    if args.goal is not None:
        return args.goal
    if args.goal_file is None:
        return None
    path = args.goal_file.expanduser()
    if not path.is_file():
        print(f"error: goal file does not exist: {path}", file=sys.stderr)
        return None
    text = path.read_text(encoding="utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return text


def cmd_init(args: argparse.Namespace) -> int:
    goal = _read_goal(args)
    if goal is None:
        return 1

    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(f"error: target repo does not exist: {repo}", file=sys.stderr)
        return 1

    task_id = args.task_id or uuid.uuid4().hex[:12]
    overrides: dict = {"base_branch": args.base_branch}
    if args.test_command is not None:
        overrides["test_command"] = args.test_command
    if args.planner is not None:
        overrides["planner_provider"] = args.planner
    if args.reviewer is not None:
        overrides["reviewer_provider"] = args.reviewer
    if args.implementer is not None:
        overrides["implementer_provider"] = args.implementer
    if args.allow_merge_without_tests:
        overrides["allow_merge_without_tests"] = True
    if args.max_iterations is not None:
        overrides["max_iterations"] = args.max_iterations
    if args.max_retries is not None:
        overrides["max_retries_per_step"] = args.max_retries
    if args.codex_model is not None:
        overrides["codex_model"] = args.codex_model
    if args.cursor_model is not None:
        overrides["cursor_model"] = args.cursor_model
    if args.claude_code_model is not None:
        overrides["claude_code_model"] = args.claude_code_model
    if args.cursor_force:
        overrides["cursor_force"] = True
    if args.cursor_sandbox is not None:
        overrides["cursor_sandbox"] = args.cursor_sandbox

    config = merge_config(overrides)
    base_commit = resolve_base_commit_if_possible(repo, args.base_branch)
    state = create_initial_state(
        task_id=task_id,
        goal=goal,
        target_repo=str(repo),
        base_branch=args.base_branch,
        base_commit=base_commit,
        config=config,
    )
    path = save_state(state, args.state_root)
    print(f"initialized task {task_id}")
    print(f"state: {path}")
    print(
        f"planner: {config['planner_provider']}  "
        f"reviewer: {config['reviewer_provider']}  "
        f"implementer: {config['implementer_provider']}"
    )
    if config.get("test_command"):
        print(f"test_command: {' '.join(config['test_command'])}")
    print("next: cc-loop run")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        if args.task_id:
            return 1
        print("error: no task found; run `cc-loop init` first", file=sys.stderr)
        return 1

    state = load_state(task_id, args.state_root)
    if args.json:
        print(json.dumps(build_status_snapshot(state, args.state_root), indent=2))
        return 0

    attempt = state.history[-1] if state.history else None
    print(f"task_id: {state.task_id}")
    print(f"status: {state.status.value}")
    print(f"goal: {state.goal}")
    print(f"target_repo: {state.target_repo}")
    print(f"iteration: {state.iteration}")
    if attempt is not None:
        artifact_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, args.state_root)
        print(f"attempt: iter-{attempt.iteration:03d} retry-{attempt.retry:02d}")
        print(f"phase: {attempt.phase.value}")
        if attempt.decision:
            print(f"decision: {attempt.decision}")
        if attempt.test_status:
            print(f"test_status: {attempt.test_status}")
        if attempt.merge_error:
            print(f"merge_error: {attempt.merge_error}")
        print(f"worktree_path: {attempt.worktree_path}")
        print(f"artifacts: {artifact_root}")
        print(f"next: {summarize_attempt(attempt)}")
    else:
        print("next: cc-loop run")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve() if args.repo is not None else None
    items = iter_tasks(args.state_root, repo=repo)
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    for item in items:
        print(format_task_line(item))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    try:
        run_doctor_preflight(
            target_repo=repo,
            base_branch=args.base_branch,
            planner=args.planner,
            reviewer=args.reviewer,
            implementer=args.implementer,
            test_command=args.test_command,
        )
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True}))
    else:
        print("ok")
    return 0


def _print_run_summary(
    state,
    attempt,
    artifact_paths: dict[str, Path],
) -> None:
    print(f"task_id: {state.task_id}")
    print(f"status: {state.status.value}")
    print(f"iteration: {state.iteration}")
    print(f"phase: {attempt.phase.value}")
    print(f"base_commit: {state.base_commit}")
    print(f"worktree_path: {attempt.worktree_path}")
    print(f"branch: {attempt.branch}")
    print(f"artifacts: {artifact_paths['plan_prompt'].parent}")

    if attempt.plan_json is not None:
        print(f"plan_parsed: {artifact_paths['plan_parsed']}")
    if attempt.implementer_exit_code is not None:
        print(f"implementer_exit_code: {attempt.implementer_exit_code}")
        print(f"implementer_provider: {attempt.implementer_provider}")
        print(f"head_commit: {attempt.head_commit}")
        print(f"diff_stat: {artifact_paths['diff_stat']}")
    if attempt.test_status:
        print(f"test_status: {attempt.test_status}")
        print(f"test_output: {artifact_paths['test_output']}")
    if attempt.decision:
        print(f"decision: {attempt.decision}")
        print(f"review_parsed: {artifact_paths['review_parsed']}")
    if attempt.merge_output_path:
        print(f"merge_output: {artifact_paths['merge_output']}")
    if attempt.merge_error:
        print(f"merge_error: {attempt.merge_error}")
    print(f"next: {summarize_attempt(attempt)}")


def cmd_run(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        if args.task_id:
            return 1
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
    except (PlanningError, ImplementingError, ReviewError) as exc:
        attempt = state.history[-1] if state.history else None
        print(f"error: {exc}", file=sys.stderr)
        if attempt is not None:
            artifact_paths = _artifact_paths_for_attempt(state, attempt, args.state_root)
            _print_run_summary(state, attempt, artifact_paths)
        return 2

    _print_run_summary(state, attempt, artifact_paths)

    if state.status == TaskStatus.FAILED:
        return 2
    if state.status == TaskStatus.DONE:
        return 0
    if attempt.phase == AttemptPhase.REJECTED and attempt.decision == "reject":
        return 0
    if attempt.decision == "stop":
        return 0
    if state.status == TaskStatus.STOPPED:
        return 0
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        if args.task_id:
            return 1
        print("error: no task found; run `cc-loop init` first", file=sys.stderr)
        return 1

    if args.detach:
        pid = spawn_detached_auto(
            state_root=args.state_root,
            task_id=task_id,
            max_iterations=args.max_iterations,
        )
        log_path = runner_log_path(args.state_root, task_id)
        print(f"detached pid={pid} task_id={task_id} log={log_path}")
        return 0

    current_pid = os.getpid()
    try:
        return _run_auto_loop(args, task_id)
    finally:
        clear_runner_pid_if_matches(args.state_root, task_id, expected_pid=current_pid)


def _run_auto_loop(args: argparse.Namespace, task_id: str) -> int:
    state_root = args.state_root
    if args.max_iterations is not None:
        initial = load_state(task_id, state_root)
        initial.config["max_iterations"] = args.max_iterations
        save_state(initial, state_root)

    while True:
        state = load_state(task_id, state_root)
        if args.max_iterations is not None:
            state.config["max_iterations"] = args.max_iterations

        attempt = state.history[-1] if state.history else None
        artifact_paths = (
            _artifact_paths_for_attempt(state, attempt, state_root) if attempt is not None else None
        )

        step, report = decide_auto_step(
            state,
            attempt,
            state.config,
            artifact_paths=artifact_paths,
            running=False,
        )

        if step == AutoStep.DONE:
            print(f"task {state.task_id} completed successfully")
            _notify(f"task {task_id} done", state.goal)
            return 0

        if step == AutoStep.TERMINAL:
            return _handle_terminal_auto_stop(state, attempt, state_root, report)

        if step == AutoStep.WAIT:
            return 0

        if step == AutoStep.RUN and state.status == TaskStatus.DONE:
            state.status = TaskStatus.STOPPED
            save_state(state, state_root)

        maybe_backoff(state.config)

        try:
            if step == AutoStep.REPAIR:
                if report is None or attempt is None:
                    print("error: repair step without failure report", file=sys.stderr)
                    return 1
                increment_recovery_counter(attempt, report)
                save_state(state, state_root)
                state, attempt, artifact_paths = execute_repair_recovery(state, state_root, report)
            elif step in {AutoStep.RESUME, AutoStep.MERGE_RETRY}:
                if report is not None and step == AutoStep.MERGE_RETRY:
                    increment_recovery_counter(attempt, report)
                    save_state(state, state_root)
                state, attempt, artifact_paths = execute_resume(state, state_root)
            else:
                state, attempt, artifact_paths = execute_run(state, state_root)
        except ResumeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except RunError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except PreflightError as exc:
            print(f"error: preflight: {exc}", file=sys.stderr)
            return 1
        except PlanningError as exc:
            code = _handle_provider_auto_failure(
                task_id, state_root, exc, phase=AttemptPhase.PLANNING, provider=state.config["planner_provider"]
            )
            if code is not None:
                return code
            continue
        except ImplementingError as exc:
            code = _handle_provider_auto_failure(
                task_id,
                state_root,
                exc,
                phase=AttemptPhase.EXECUTING,
                provider=state.config["implementer_provider"],
            )
            if code is not None:
                return code
            continue
        except ReviewError as exc:
            code = _handle_provider_auto_failure(
                task_id,
                state_root,
                exc,
                phase=AttemptPhase.REVIEWING,
                provider=state.config["reviewer_provider"],
            )
            if code is not None:
                return code
            continue

        _print_run_summary(state, attempt, artifact_paths)


def _handle_provider_auto_failure(
    task_id: str,
    state_root: Path,
    exc: BaseException,
    *,
    phase: AttemptPhase,
    provider: str,
) -> int | None:
    state = load_state(task_id, state_root)
    if not state.history:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    attempt = state.history[-1]
    artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
    report = classify_provider_exception(exc, phase=phase.value, provider=provider)
    soft_reset_provider_failure(state, state_root, phase=phase)
    state = load_state(task_id, state_root)
    attempt = state.history[-1]
    if report.disposition == RecoveryDisposition.RECOVERABLE and state.config.get("auto_recover_provider_errors", True):
        from cc_loop.recovery import recovery_budget_remaining

        if recovery_budget_remaining(attempt, state.config, report):
            persist_failure_state(state, attempt, report, artifact_paths)
            save_state(state, state_root)
            return None
    persist_failure_state(state, attempt, report, artifact_paths)
    save_state(state, state_root)
    return _handle_terminal_auto_stop(state, attempt, state_root, report)


def _handle_terminal_auto_stop(
    state,
    attempt,
    state_root: Path,
    report: FailureReport | None,
) -> int:
    if report is not None and attempt is not None:
        artifact_paths = _artifact_paths_for_attempt(state, attempt, state_root)
        persist_failure_state(state, attempt, report, artifact_paths)
        save_state(state, state_root)
        artifact_dir = artifact_paths["plan_prompt"].parent
        print(
            f"error: task stopped (terminal): failure_type={report.failure_type.value}",
            file=sys.stderr,
        )
        if report.stop_reason:
            print(f"stop_reason: {report.stop_reason}", file=sys.stderr)
        for action in report.suggested_actions:
            print(f"suggested_action: {action}", file=sys.stderr)
        print(f"artifacts: {artifact_dir}", file=sys.stderr)
        print(f"failure_report: {failure_report_path(artifact_dir)}", file=sys.stderr)
        _notify(f"task {state.task_id} stopped", report.failure_type.value)
        if report.failure_type == FailureType.RECOVERY_BUDGET_EXHAUSTED:
            return 1
        if report.stop_reason in {"max_iterations", "retry_exhausted"}:
            return 1
        return 1
    print(f"task {state.task_id} failed", file=sys.stderr)
    _notify(f"task {state.task_id} failed", "check artifacts")
    return 2


def cmd_resume(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        if args.task_id:
            return 1
        print("error: no task found; run `cc-loop init` first", file=sys.stderr)
        return 1

    state = load_state(task_id, args.state_root)
    try:
        state, attempt, artifact_paths = execute_resume(state, args.state_root)
    except ResumeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PreflightError as exc:
        print(f"error: preflight failed: {exc}", file=sys.stderr)
        return 1
    except (PlanningError, ImplementingError, ReviewError) as exc:
        attempt = state.history[-1] if state.history else None
        print(f"error: {exc}", file=sys.stderr)
        if attempt is not None:
            artifact_paths = _artifact_paths_for_attempt(state, attempt, args.state_root)
            _print_run_summary(state, attempt, artifact_paths)
        return 2

    _print_run_summary(state, attempt, artifact_paths)

    if state.status == TaskStatus.FAILED:
        return 2
    return 0


def _artifact_paths_for_attempt(state, attempt, state_root: Path) -> dict[str, Path]:
    artifact_root = artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)
    return plan_artifact_paths(artifact_root)


def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        pass


def _apply_state_root_default(argv: list[str] | None) -> list[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    env_root = os.environ.get("CC_LOOP_STATE_ROOT")
    if env_root and "--state-root" not in args:
        return ["--state-root", env_root, *args]
    return args


def main(argv: list[str] | None = None) -> int:
    argv = _apply_state_root_default(argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "init": cmd_init,
        "run": cmd_run,
        "resume": cmd_resume,
        "auto": cmd_auto,
        "status": cmd_status,
        "list": cmd_list,
        "doctor": cmd_doctor,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
