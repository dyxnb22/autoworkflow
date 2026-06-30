"""Detached background runner for cc-loop auto."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cc_loop.inspect import runner_log_path, runner_pid_path
from cc_loop.state import task_dir


def spawn_detached_auto(
    *,
    state_root: Path,
    task_id: str,
    max_iterations: int | None = None,
) -> int:
    task_path = task_dir(task_id, state_root)
    task_path.mkdir(parents=True, exist_ok=True)

    log_path = runner_log_path(state_root, task_id)
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115

    argv = [
        sys.executable,
        "-m",
        "cc_loop.cli",
        "auto",
        "--task-id",
        task_id,
        "--state-root",
        str(state_root),
    ]
    if max_iterations is not None:
        argv.extend(["--max-iterations", str(max_iterations)])

    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        shell=False,
    )
    log_file.close()

    pid_path = runner_pid_path(state_root, task_id)
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    return process.pid
