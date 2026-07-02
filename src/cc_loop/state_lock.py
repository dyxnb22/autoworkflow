"""File locking and atomic state persistence for concurrent runners."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_STALE_LOCK_SECONDS = 300

_lock_local = threading.local()


def lock_path(state_root: Path, task_id: str) -> Path:
    return state_root / "tasks" / task_id / "state.lock"


def _lock_depth() -> int:
    return int(getattr(_lock_local, "depth", 0) or 0)


def _set_lock_depth(value: int) -> None:
    _lock_local.depth = value


@contextmanager
def task_state_lock(
    state_root: Path,
    task_id: str,
    *,
    stale_seconds: int = DEFAULT_STALE_LOCK_SECONDS,
) -> Iterator[None]:
    """Acquire an exclusive file lock for task state read/write (reentrant per thread)."""
    if _lock_depth() > 0:
        yield
        return

    path = lock_path(state_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _acquire_with_stale_detection(fd, path, stale_seconds=stale_seconds)
        _set_lock_depth(_lock_depth() + 1)
        try:
            yield
        finally:
            _set_lock_depth(max(0, _lock_depth() - 1))
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _acquire_with_stale_detection(fd: int, path: Path, *, stale_seconds: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        if stale_seconds > 0 and path.is_file():
            age = time.time() - path.stat().st_mtime
            if age > stale_seconds:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    pass
        fcntl.flock(fd, fcntl.LOCK_EX)


def atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
