"""Timeout-safe subprocess execution with dedicated process groups."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

DEFAULT_KILL_GRACE_SECONDS = 0.5
COMMUNICATE_DRAIN_SECONDS = 5.0


@dataclass
class RunResult:
    """Result of a timeout-aware subprocess invocation."""

    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    killed: bool = False
    interrupted: bool = False
    hung: bool = False
    duration_seconds: float = 0.0


def _process_group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _terminate_process_group(pid: int, *, grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS) -> bool:
    """Send SIGTERM then SIGKILL to a process group.

    Returns ``True`` when SIGKILL was required.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False

    if grace_seconds <= 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        return True

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _process_group_alive(pid):
            return False
        time.sleep(0.05)

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


def _drain_communicate(proc: subprocess.Popen, *, timeout: float) -> tuple[str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return stdout or "", stderr or ""


def run_with_timeout(
    args: list[str],
    *,
    cwd: str | None = None,
    input: str | None = None,
    timeout_seconds: int | None = None,
    capture_output: bool = True,
    stdout_file=None,
    stderr_file=None,
    text: bool = True,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> RunResult:
    """Run ``args`` in a new process group with optional timeout enforcement."""
    popen_kwargs: dict = {
        "args": args,
        "cwd": cwd,
        "shell": False,
        "start_new_session": True,
        "text": text,
    }
    if capture_output and stdout_file is None:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    elif stdout_file is not None:
        popen_kwargs["stdout"] = stdout_file
        if stderr_file is not None:
            popen_kwargs["stderr"] = stderr_file
        else:
            popen_kwargs["stderr"] = subprocess.PIPE

    if input is not None:
        popen_kwargs["stdin"] = subprocess.PIPE

    proc = subprocess.Popen(**popen_kwargs)
    timed_out = False
    killed = False
    interrupted = False
    hung = False
    started = time.monotonic()
    stdout = ""
    stderr = ""
    force_kill = threading.Event()

    def _watchdog() -> None:
        if timeout_seconds is None:
            return
        time.sleep(timeout_seconds + kill_grace_seconds + 1.0)
        if proc.poll() is None:
            force_kill.set()
            sigkill_required = _terminate_process_group(proc.pid, grace_seconds=kill_grace_seconds)
            if sigkill_required:
                force_kill.set()

    watchdog_thread: threading.Thread | None = None
    if timeout_seconds is not None:
        watchdog_thread = threading.Thread(target=_watchdog, name="cc-loop-subprocess-watchdog", daemon=True)
        watchdog_thread.start()

    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout_seconds)
    except KeyboardInterrupt:
        interrupted = True
        killed = True
        hung = _terminate_process_group(proc.pid, grace_seconds=kill_grace_seconds)
        stdout, stderr = _drain_communicate(proc, timeout=COMMUNICATE_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = True
        hung = _terminate_process_group(proc.pid, grace_seconds=kill_grace_seconds)
        stdout, stderr = _drain_communicate(proc, timeout=COMMUNICATE_DRAIN_SECONDS)
        if _process_group_alive(proc.pid):
            hung = True
            _terminate_process_group(proc.pid, grace_seconds=0)
    finally:
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=0.2)
        if force_kill.is_set():
            hung = True
            killed = True
            if not timed_out and not interrupted:
                timed_out = True

    duration_seconds = time.monotonic() - started
    returncode = proc.returncode
    if interrupted or timed_out:
        returncode = -1
    if proc.poll() is None:
        hung = True
        killed = True
        _terminate_process_group(proc.pid, grace_seconds=0)
        returncode = -1

    return RunResult(
        args=args,
        returncode=returncode if returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        killed=killed,
        interrupted=interrupted,
        hung=hung,
        duration_seconds=duration_seconds,
    )
