"""Runner stop, cancel, and cleanup for detached auto runs."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cc_loop.inspect import (
    clear_runner_pid_if_matches,
    is_process_alive,
    is_runner_alive,
    read_runner_pid,
    runner_log_path,
    runner_pid_path,
)
from cc_loop.runner_heartbeat import is_heartbeat_stale, read_heartbeat, remove_heartbeat
from cc_loop.state import DEFAULT_WORKTREE_ROOT, TaskState, TaskStatus, load_state, save_state, task_dir


@dataclass
class RunnerControlResult:
    action: str
    task_id: str
    ok: bool
    message: str
    pid: int | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["details"] is None:
            data["details"] = {}
        return data


def _read_proc_cmdline(pid: int) -> str:
    proc_path = Path(f"/proc/{pid}/cmdline")
    if not proc_path.is_file():
        return ""
    try:
        raw = proc_path.read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def validate_pid_ownership(pid: int, task_id: str, *, state_root: Path | None = None) -> bool:
    """Return True when PID appears to belong to this task's cc-loop runner."""
    if not is_process_alive(pid):
        return False
    cmdline = _read_proc_cmdline(pid)
    if not cmdline:
        if state_root is not None:
            hb = read_heartbeat(state_root, task_id)
            if hb is not None and hb.pid == pid and hb.task_id == task_id:
                return True
        return False
    markers = ("cc_loop.cli", "cc-loop", "cc_loop")
    if not any(marker in cmdline for marker in markers):
        return False
    return task_id in cmdline or "auto" in cmdline


def _terminate_pid(pid: int, *, grace_seconds: float = 5.0) -> bool:
    if not is_process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.1)

    if not is_process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    time.sleep(0.1)
    return not is_process_alive(pid)


def stop_runner(
    state_root: Path,
    task_id: str,
    *,
    grace_seconds: float = 5.0,
) -> RunnerControlResult:
    running, pid = is_runner_alive(state_root, task_id)
    if not running or pid is None:
        clear_runner_pid_if_matches(state_root, task_id)
        remove_heartbeat(state_root, task_id)
        return RunnerControlResult(
            action="stop",
            task_id=task_id,
            ok=True,
            message="no active runner",
            pid=None,
        )

    if not validate_pid_ownership(pid, task_id, state_root=state_root):
        clear_runner_pid_if_matches(state_root, task_id)
        return RunnerControlResult(
            action="stop",
            task_id=task_id,
            ok=False,
            message=f"pid {pid} does not belong to cc-loop task runner",
            pid=pid,
        )

    stopped = _terminate_pid(pid, grace_seconds=grace_seconds)
    clear_runner_pid_if_matches(state_root, task_id, expected_pid=pid)
    remove_heartbeat(state_root, task_id)

    if stopped:
        return RunnerControlResult(
            action="stop",
            task_id=task_id,
            ok=True,
            message=f"stopped runner pid={pid}",
            pid=pid,
        )
    return RunnerControlResult(
        action="stop",
        task_id=task_id,
        ok=False,
        message=f"failed to stop runner pid={pid}",
        pid=pid,
    )


def cancel_task(state_root: Path, task_id: str) -> RunnerControlResult:
    stop_result = stop_runner(state_root, task_id)
    state = load_state(task_id, state_root)
    from cc_loop.task_graph import GraphNodeStatus, ensure_task_graph

    graph = ensure_task_graph(state)
    if graph is not None:
        for node in graph.nodes:
            if node.status == GraphNodeStatus.RUNNING:
                node.status = GraphNodeStatus.SKIPPED
                node.notes = (node.notes + "; " if node.notes else "") + "cancelled by user"
    state.status = TaskStatus.CANCELLED
    save_state(state, state_root)
    from cc_loop.summary import write_run_summary_if_terminal

    write_run_summary_if_terminal(state, state_root)

    from cc_loop.events import EventType, append_event

    append_event(
        state_root,
        task_id=task_id,
        event_type=EventType.TASK_CANCELLED,
        message="task cancelled by user",
    )

    return RunnerControlResult(
        action="cancel",
        task_id=task_id,
        ok=True,
        message="task cancelled; artifacts preserved",
        pid=stop_result.pid,
        details={"stop": stop_result.to_dict()},
    )


def _task_worktree_root(state: TaskState) -> Path:
    from cc_loop.git import repo_label

    repo_name = repo_label(Path(state.target_repo))
    return DEFAULT_WORKTREE_ROOT / repo_name / state.task_id


def cleanup_task(state_root: Path, task_id: str) -> RunnerControlResult:
    """Remove task-owned runtime artifacts without touching the user's main repo."""
    removed: list[str] = []
    errors: list[str] = []

    pid_path = runner_pid_path(state_root, task_id)
    if pid_path.is_file():
        pid = read_runner_pid(state_root, task_id)
        if pid is not None and is_process_alive(pid) and validate_pid_ownership(pid, task_id, state_root=state_root):
            return RunnerControlResult(
                action="cleanup",
                task_id=task_id,
                ok=False,
                message="runner is still active; stop or cancel first",
                pid=pid,
            )
        try:
            pid_path.unlink()
            removed.append(str(pid_path))
        except OSError as exc:
            errors.append(str(exc))

    hb_path = task_dir(task_id, state_root) / "runner.heartbeat.json"
    if hb_path.is_file():
        try:
            hb_path.unlink()
            removed.append(str(hb_path))
        except OSError as exc:
            errors.append(str(exc))

    lock_path = task_dir(task_id, state_root) / "state.lock"
    if lock_path.is_file():
        try:
            lock_path.unlink()
            removed.append(str(lock_path))
        except OSError as exc:
            errors.append(str(exc))

    try:
        state = load_state(task_id, state_root)
        target_repo = Path(state.target_repo)
        wt_root = _task_worktree_root(state)
        if wt_root.is_dir():
            from cc_loop.git import GitError, prune_worktrees, remove_worktree

            for child in sorted(wt_root.iterdir()):
                if not child.is_dir():
                    continue
                try:
                    remove_worktree(target_repo, child, force=True)
                    removed.append(str(child))
                except GitError:
                    import shutil

                    shutil.rmtree(child, ignore_errors=True)
                    removed.append(str(child))
            try:
                wt_root.rmdir()
            except OSError:
                pass
            try:
                prune_worktrees(target_repo)
            except GitError as exc:
                errors.append(str(exc))
    except (OSError, FileNotFoundError) as exc:
        errors.append(str(exc))

    ok = not errors
    message = "cleaned task-owned runtime artifacts" if ok else "; ".join(errors)
    return RunnerControlResult(
        action="cleanup",
        task_id=task_id,
        ok=ok,
        message=message,
        details={"removed": removed, "errors": errors},
    )


def runner_state_label(
    state_root: Path,
    task_id: str,
    *,
    stale_heartbeat_seconds: int = 120,
) -> str:
    running, pid = is_runner_alive(state_root, task_id)
    if running:
        return "running"
    hb = read_heartbeat(state_root, task_id)
    if hb is not None and is_heartbeat_stale(hb, stale_seconds=stale_heartbeat_seconds):
        return "stale_heartbeat"
    if pid is not None:
        return "stale_pid"
    if hb is not None:
        return "stopped"
    if runner_log_path(state_root, task_id).is_file():
        return "stopped"
    return "idle"
