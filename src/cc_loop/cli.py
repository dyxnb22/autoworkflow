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
from cc_loop.report import build_report, format_report_human
from cc_loop.summary import build_task_summary, format_task_summary_human
from cc_loop.runner_control import cancel_task, cleanup_task, stop_runner
from cc_loop.runner_heartbeat import mark_heartbeat_terminal, refresh_heartbeat, remove_heartbeat
from cc_loop.evals import format_eval_human, run_eval_suite
from cc_loop.export import write_jsonl_export
from cc_loop.events import EventType, append_event, read_events
from cc_loop.inspect import (
    build_status_snapshot,
    clear_runner_pid_if_matches,
    format_task_graph_human,
    runner_log_path,
    runner_pid_path,
)
from cc_loop.task_graph import build_graph_snapshot, ensure_task_graph
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
    execute_replan,
    execute_resume,
    execute_run,
    soft_reset_provider_failure,
    summarize_attempt,
)
from cc_loop.parallel_scheduler import discover_parallel_runnable, execute_parallel_batch
from cc_loop.budgets import check_budgets
from cc_loop.test_command import (
    TEST_COMMAND_HINT,
    expand_test_command_in_argv,
    format_test_command_argv,
    format_test_command_display,
    normalize_test_command,
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


class CcLoopArgumentParser(argparse.ArgumentParser):
    """Argument parser with clearer errors for misplaced test-command flags."""

    def error(self, message: str) -> None:
        if message.startswith("unrecognized arguments:"):
            tail = message.split("unrecognized arguments:", 1)[1].strip()
            if tail.startswith("-"):
                self.print_usage(sys.stderr)
                print(f"error: {message}", file=sys.stderr)
                print(f"hint: {TEST_COMMAND_HINT}", file=sys.stderr)
                self.exit(2)
        super().error(message)


def _task_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", help="Explicit task identifier")


def _build_parser() -> argparse.ArgumentParser:
    parser = CcLoopArgumentParser(prog="cc-loop", description="Local coding agent orchestrator")
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
        "--test-command",
        nargs="+",
        metavar="ARG",
        default=None,
        help="Test command argv; use `--` before flags (e.g. --test-command -- pytest tests -q)",
    )
    init_parser.add_argument(
        "--planner-granularity",
        choices=["single", "auto", "graph"],
        default=None,
        help="Planner decomposition: single node, auto (default), or multi-node graph",
    )
    init_parser.add_argument(
        "--planner-mode",
        choices=["auto", "graph", "single", "direct"],
        default=None,
        help="Planner execution mode; direct skips planner provider and synthesizes a single-node plan",
    )
    init_parser.add_argument(
        "--review-context-mode",
        choices=["hybrid", "inline", "artifact_refs"],
        default=None,
        help="Reviewer prompt context: inline patches, artifact refs only, or hybrid threshold",
    )
    init_parser.add_argument(
        "--review-inline-patch-threshold",
        type=int,
        default=None,
        help="Hybrid reviewer mode inlines patches up to this many characters (default: 8000)",
    )
    init_parser.add_argument(
        "--provider-watchdog-grace-seconds",
        type=int,
        default=None,
        help="Seconds after provider timeout before force-kill (default: 5)",
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
    init_parser.add_argument("--max-parallel-nodes", type=int, default=None, help="Max concurrent graph nodes (default: 1)")
    init_parser.add_argument(
        "--allow-parallel-execution",
        action="store_true",
        default=False,
        help="Enable experimental parallel graph node execution (requires max_parallel_nodes > 1)",
    )
    init_parser.add_argument(
        "--max-wall-clock-seconds",
        type=int,
        default=None,
        help="Stop after this many wall-clock seconds (0 disables)",
    )
    init_parser.add_argument(
        "--max-changed-files-per-attempt",
        type=int,
        default=None,
        help="Stop when changed file count exceeds this limit (0 disables)",
    )
    init_parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=None,
        help="Stop after this many consecutive failures (0 disables)",
    )
    init_parser.add_argument(
        "--max-artifact-log-bytes",
        type=int,
        default=None,
        help="Stop when artifact log size exceeds this limit (0 disables)",
    )
    init_parser.add_argument(
        "--allow-node-policy-weakening",
        action="store_true",
        default=False,
        help="Allow per-node policy to weaken task-level safety defaults",
    )

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
        "--test-command",
        nargs="+",
        metavar="ARG",
        default=None,
        help="Test command argv to validate; use `--` before flags",
    )
    doctor_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON")

    graph_parser = subparsers.add_parser("graph", help="Show task graph progress")
    _task_id_arg(graph_parser)
    graph_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON")
    graph_parser.add_argument("--history", action="store_true", default=False, help="Show graph mutation history")

    stop_parser = subparsers.add_parser("stop", help="Stop detached auto runner")
    _task_id_arg(stop_parser)
    stop_parser.add_argument("--json", action="store_true", default=False)

    cancel_parser = subparsers.add_parser("cancel", help="Stop runner and mark task cancelled")
    _task_id_arg(cancel_parser)
    cancel_parser.add_argument("--json", action="store_true", default=False)

    cleanup_parser = subparsers.add_parser("cleanup", help="Remove task-owned runtime artifacts")
    _task_id_arg(cleanup_parser)
    cleanup_parser.add_argument("--json", action="store_true", default=False)

    report_parser = subparsers.add_parser("report", help="Show task report")
    report_parser.add_argument(
        "report_task_id",
        nargs="?",
        metavar="TASK_ID",
        help="Task identifier (positional alternative to --task-id)",
    )
    _task_id_arg(report_parser)
    report_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON")
    report_parser.add_argument(
        "--format",
        choices=["json", "human"],
        default=None,
        help="Output format (alias for --json when set to json)",
    )

    eval_parser = subparsers.add_parser("eval", help="Run a local eval suite against task artifacts")
    _task_id_arg(eval_parser)
    eval_parser.add_argument("--suite", required=True, type=Path, help="Path to eval suite JSON file")
    eval_parser.add_argument("--json", action="store_true", default=False)

    export_parser = subparsers.add_parser("export", help="Export observability rows for analytics tools")
    _task_id_arg(export_parser)
    export_parser.add_argument("--format", choices=["jsonl"], default="jsonl", help="Export format")
    export_parser.add_argument("--output", required=True, type=Path, help="Output file path")

    summary_parser = subparsers.add_parser("summary", help="Show Luma-oriented task summary")
    _task_id_arg(summary_parser)
    summary_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON")

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
        try:
            overrides["test_command"] = normalize_test_command(args.test_command)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(f"hint: {TEST_COMMAND_HINT}", file=sys.stderr)
            return 1
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
    if args.max_parallel_nodes is not None:
        overrides["max_parallel_nodes"] = args.max_parallel_nodes
    if args.allow_parallel_execution:
        overrides["allow_parallel_execution"] = True
    if args.max_wall_clock_seconds is not None:
        overrides["max_wall_clock_seconds"] = args.max_wall_clock_seconds
    if args.max_changed_files_per_attempt is not None:
        overrides["max_changed_files_per_attempt"] = args.max_changed_files_per_attempt
    if args.max_consecutive_failures is not None:
        overrides["max_consecutive_failures"] = args.max_consecutive_failures
    if args.max_artifact_log_bytes is not None:
        overrides["max_artifact_log_bytes"] = args.max_artifact_log_bytes
    if args.allow_node_policy_weakening:
        overrides["allow_node_policy_weakening"] = True
    if args.planner_granularity is not None:
        overrides["planner_granularity"] = args.planner_granularity
    if args.planner_mode is not None:
        overrides["planner_mode"] = args.planner_mode
    if args.review_context_mode is not None:
        overrides["review_context_mode"] = args.review_context_mode
    if args.review_inline_patch_threshold is not None:
        overrides["review_inline_patch_threshold"] = args.review_inline_patch_threshold

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
    append_event(
        args.state_root,
        task_id=task_id,
        event_type=EventType.TASK_INITIALIZED,
        message=goal[:200],
    )
    print(f"initialized task {task_id}")
    print(f"state: {path}")
    print(
        f"planner: {config['planner_provider']}  "
        f"reviewer: {config['reviewer_provider']}  "
        f"implementer: {config['implementer_provider']}"
    )
    if config.get("test_command"):
        print(f"test_command: {format_test_command_argv(config['test_command'])}")
        print(f"test_command_argv: {format_test_command_display(config['test_command'])}")
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
    if state.config.get("test_command"):
        print(f"test_command_argv: {format_test_command_display(state.config.get('test_command'))}")
    print(f"goal: {state.goal}")
    print(f"target_repo: {state.target_repo}")
    print(f"iteration: {state.iteration}")
    graph = ensure_task_graph(state)
    if graph is not None:
        from cc_loop.task_graph import graph_status_summary

        summary = graph_status_summary(graph)
        current = graph.current_node_id or "(none)"
        print(f"task_graph: {summary['passed']}/{summary['total']} passed (current node: {current})")
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
        print(f"next: {summarize_attempt(attempt, state)}")
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


def cmd_graph(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        if args.task_id:
            return 1
        print("error: no task found; run `cc-loop init` first", file=sys.stderr)
        return 1

    state = load_state(task_id, args.state_root)
    graph = ensure_task_graph(state)
    if graph is None:
        if args.json:
            print(json.dumps({"task_graph": None}))
        else:
            print("No task graph for this task.")
        return 0

    if args.history:
        events = read_events(args.state_root, task_id, stream="graph")
        if args.json:
            print(json.dumps({"graph_events": [e.to_dict() for e in events]}, indent=2))
        else:
            for event in events:
                print(f"{event.timestamp} {event.type} {event.message}")
        return 0

    if args.json:
        print(json.dumps({"task_graph": build_graph_snapshot(graph, state_providers=state.providers, config=dict(state.config))}, indent=2))
        return 0

    print(format_task_graph_human(state))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        return 1
    result = stop_runner(args.state_root, task_id)
    append_event(
        args.state_root,
        task_id=task_id,
        event_type=EventType.RUNNER_STOPPED,
        message=result.message,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.message)
    return 0 if result.ok else 1


def cmd_cancel(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        return 1
    result = cancel_task(args.state_root, task_id)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.message)
    return 0 if result.ok else 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        return 1
    result = cleanup_task(args.state_root, task_id)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.message)
    return 0 if result.ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    task_id_arg = args.task_id or getattr(args, "report_task_id", None)
    if args.task_id and getattr(args, "report_task_id", None) and args.task_id != args.report_task_id:
        print("error: conflicting task id between --task-id and positional TASK_ID", file=sys.stderr)
        return 1
    task_id = resolve_task_id(args.state_root, task_id_arg)
    if task_id is None:
        return 1
    state = load_state(task_id, args.state_root)
    report = build_report(state, args.state_root)
    output_format = args.format
    if output_format is None:
        output_format = "json" if args.json else "human"
    if output_format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_report_human(report))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        return 1
    state = load_state(task_id, args.state_root)
    summary = build_task_summary(state, args.state_root)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_task_summary_human(summary))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        return 1
    state = load_state(task_id, args.state_root)
    try:
        result = run_eval_suite(state, args.state_root, args.suite.expanduser())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_eval_human(result), end="")
    return 0 if result.get("passed") else 1


def cmd_export(args: argparse.Namespace) -> int:
    task_id = resolve_task_id(args.state_root, args.task_id)
    if task_id is None:
        return 1
    if args.format != "jsonl":
        print(f"error: unsupported export format: {args.format}", file=sys.stderr)
        return 2
    state = load_state(task_id, args.state_root)
    if not state.history:
        print("error: task has no attempts to export", file=sys.stderr)
        return 2
    output_path = args.output.expanduser()
    row_count = write_jsonl_export(state, args.state_root, output_path)
    print(f"exported {row_count} rows to {output_path}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    test_command = args.test_command
    if test_command is not None:
        try:
            test_command = normalize_test_command(test_command)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(f"hint: {TEST_COMMAND_HINT}", file=sys.stderr)
            return 1
    try:
        run_doctor_preflight(
            target_repo=repo,
            base_branch=args.base_branch,
            planner=args.planner,
            reviewer=args.reviewer,
            implementer=args.implementer,
            test_command=test_command,
        )
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {"ok": True}
        if test_command is not None:
            payload["test_command_argv"] = test_command
        print(json.dumps(payload))
    else:
        print("ok")
        if test_command is not None:
            print(f"test_command_argv: {format_test_command_display(test_command)}")
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
    print(f"next: {summarize_attempt(attempt, state)}")


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

    append_event(
        state_root,
        task_id=task_id,
        event_type=EventType.RUNNER_STARTED,
        message="auto loop started",
    )
    runner_pid_path(state_root, task_id).write_text(f"{os.getpid()}\n", encoding="utf-8")

    while True:
        state = load_state(task_id, state_root)
        if args.max_iterations is not None:
            state.config["max_iterations"] = args.max_iterations

        attempt = state.history[-1] if state.history else None
        artifact_paths = (
            _artifact_paths_for_attempt(state, attempt, state_root) if attempt is not None else None
        )

        phase = attempt.phase.value if attempt is not None else ""
        refresh_heartbeat(
            state_root,
            task_id=task_id,
            pid=os.getpid(),
            status=state.status.value,
            phase=phase,
            iteration=state.iteration,
            graph_node_id=attempt.graph_node_id if attempt else "",
            running_provider=attempt.running_provider if attempt else "",
        )

        if attempt is not None and artifact_paths is not None:
            budget_report = check_budgets(
                state,
                attempt,
                state.config,
                artifact_dir=artifact_paths["plan_prompt"].parent,
            )
            if budget_report is not None:
                return _handle_terminal_auto_stop(state, attempt, state_root, budget_report)

        step, report = decide_auto_step(
            state,
            attempt,
            state.config,
            artifact_paths=artifact_paths,
            running=False,
        )

        if step == AutoStep.DONE:
            from cc_loop.summary import finalize_terminal_task

            finalize_terminal_task(state, state_root)
            remove_heartbeat(state_root, task_id)
            print(f"task {state.task_id} completed successfully")
            _notify(f"task {task_id} done", state.goal)
            return 0

        if step == AutoStep.TERMINAL:
            remove_heartbeat(state_root, task_id)
            return _handle_terminal_auto_stop(state, attempt, state_root, report)

        if step == AutoStep.WAIT:
            return 0

        if step == AutoStep.RUN and state.status == TaskStatus.DONE:
            state.status = TaskStatus.STOPPED
            save_state(state, state_root)

        maybe_backoff(state.config)

        try:
            if step == AutoStep.REPLAN:
                state, attempt, artifact_paths = execute_replan(state, state_root, report)
            elif step == AutoStep.REPAIR:
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
            elif (
                step == AutoStep.RUN
                and int(state.config.get("max_parallel_nodes", 1) or 1) > 1
                and state.config.get("allow_parallel_execution", False)
                and discover_parallel_runnable(state)
            ):
                state = execute_parallel_batch(state, state_root)
                attempt = state.history[-1] if state.history else None
                artifact_paths = (
                    _artifact_paths_for_attempt(state, attempt, state_root) if attempt else None
                )
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
    mark_heartbeat_terminal(state_root, task_id, status="stopped", phase=phase.value)
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
        args = ["--state-root", env_root, *args]
    return expand_test_command_in_argv(args)


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
        "graph": cmd_graph,
        "stop": cmd_stop,
        "cancel": cmd_cancel,
        "cleanup": cmd_cleanup,
        "report": cmd_report,
        "summary": cmd_summary,
        "eval": cmd_eval,
        "export": cmd_export,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
