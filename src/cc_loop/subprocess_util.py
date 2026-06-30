"""Timeout-safe subprocess execution with dedicated process groups."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass
class RunResult:
    """Result of a timeout-aware subprocess invocation."""

    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _terminate_process_group(pid: int, *, grace_seconds: float = 2.0) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


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
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

    return RunResult(
        args=args,
        returncode=-1 if timed_out else proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
    )
