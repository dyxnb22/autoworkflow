"""Provider invocation helpers: heartbeat refresh and subprocess diagnostics."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from cc_loop.providers.base import ProviderAdapter, ProviderRunResult
from cc_loop.runner_heartbeat import refresh_heartbeat
from cc_loop.state import AttemptRecord, TaskState
from cc_loop.subprocess_util import RunResult, kill_active_subprocess_group

PROVIDER_WORKER_JOIN_SECONDS = 5.0


def _heartbeat_interval_seconds(stale_seconds: int) -> float:
    if stale_seconds <= 0:
        return 30.0
    return max(5.0, min(30.0, stale_seconds / 3))


def _provider_watchdog_grace_seconds(state: TaskState) -> int:
    return max(1, int(state.config.get("provider_watchdog_grace_seconds", 5) or 5))


def run_provider_with_heartbeat(
    provider: ProviderAdapter,
    *,
    state_root: Path,
    state: TaskState,
    attempt: AttemptRecord,
    heartbeat_pid: int | None = None,
    **provider_kwargs: Any,
) -> ProviderRunResult:
    """Run a provider while refreshing runner heartbeat during long calls."""
    pid = heartbeat_pid if heartbeat_pid is not None else os.getpid()
    stale_seconds = int(state.config.get("stale_heartbeat_seconds", 120) or 120)
    stop_event = threading.Event()
    interval = _heartbeat_interval_seconds(stale_seconds)
    timeout_seconds = int(provider_kwargs.get("timeout_seconds") or 0)
    watchdog_grace = _provider_watchdog_grace_seconds(state)
    outer_limit: float | None = None
    if timeout_seconds > 0:
        outer_limit = float(timeout_seconds + watchdog_grace)

    def _refresh_loop() -> None:
        while not stop_event.wait(timeout=interval):
            refresh_heartbeat(
                state_root,
                task_id=state.task_id,
                pid=pid,
                status=state.status.value,
                phase=attempt.phase.value,
                iteration=state.iteration,
                graph_node_id=attempt.graph_node_id,
                running_provider=attempt.running_provider,
            )

    thread = threading.Thread(target=_refresh_loop, name="cc-loop-heartbeat", daemon=True)
    thread.start()

    holder: dict[str, ProviderRunResult] = {}
    errors: list[BaseException] = []

    def _run_provider() -> None:
        try:
            holder["result"] = provider.run(**provider_kwargs)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=_run_provider, name="cc-loop-provider", daemon=True)
    worker.start()
    timed_out_externally = False
    if outer_limit is not None:
        worker.join(timeout=outer_limit)
        if worker.is_alive():
            timed_out_externally = True
            kill_active_subprocess_group(grace_seconds=0)
            worker.join(timeout=PROVIDER_WORKER_JOIN_SECONDS)
    else:
        worker.join()

    stop_event.set()
    thread.join(timeout=1.0)
    refresh_heartbeat(
        state_root,
        task_id=state.task_id,
        pid=pid,
        status=state.status.value,
        phase=attempt.phase.value,
        iteration=state.iteration,
        graph_node_id=attempt.graph_node_id,
        running_provider=attempt.running_provider,
    )

    if errors:
        raise errors[0]

    if "result" in holder:
        result = holder["result"]
        if timed_out_externally and (result.timed_out or result.hung or result.killed):
            return result
        if not timed_out_externally:
            return result

    if timed_out_externally or worker.is_alive():
        output_path = Path(provider_kwargs.get("output_path", "."))
        return ProviderRunResult(
            provider=provider.name,
            exit_code=-1,
            raw_artifact_path=output_path,
            timed_out=True,
            hung=True,
            killed=True,
            summary=(
                "provider adapter did not return before watchdog deadline; "
                "active subprocess group was force-killed"
            ),
        )

    raise RuntimeError("provider worker finished without result or error")


def write_command_argv_artifact(
    artifact_root: Path,
    *,
    phase: str,
    argv: list[str],
    existing: dict[str, list[str]] | None = None,
) -> Path:
    path = artifact_root / "command.argv.json"
    payload = dict(existing or {})
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = {str(k): list(v) for k, v in loaded.items() if isinstance(v, list)}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    payload[phase] = list(argv)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _result_entry(
    result: RunResult | ProviderRunResult,
    *,
    stdout_path: str = "",
    stderr_path: str = "",
) -> dict[str, Any]:
    if isinstance(result, RunResult):
        entry = {
            "exit_code": result.returncode,
            "timed_out": result.timed_out,
            "duration_seconds": round(result.duration_seconds, 3),
            "killed": result.killed,
            "interrupted": result.interrupted,
            "hung": result.hung,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        }
        if result.pid is not None:
            entry["pid"] = result.pid
        return entry
    return {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": round(result.duration_seconds, 3),
        "killed": result.killed,
        "interrupted": result.interrupted,
        "hung": result.hung,
        "stdout_path": stdout_path or str(result.raw_artifact_path),
        "stderr_path": stderr_path,
    }


def write_subprocess_result_artifact(
    artifact_root: Path,
    *,
    phase: str,
    result: RunResult | ProviderRunResult,
    stdout_path: str = "",
    stderr_path: str = "",
    existing: dict[str, Any] | None = None,
) -> Path:
    path = artifact_root / "subprocess.result.json"
    payload: dict[str, Any] = dict(existing or {})
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = dict(loaded)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    payload[phase] = _result_entry(result, stdout_path=stdout_path, stderr_path=stderr_path)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def provider_argv_from_result(provider: ProviderAdapter, **build_kwargs: Any) -> list[str]:
    return list(
        provider.build_args(
            worktree_path=build_kwargs["worktree_path"],
            prompt=build_kwargs.get("prompt", ""),
            output_path=build_kwargs["output_path"],
            config=build_kwargs["config"],
        )
    )
