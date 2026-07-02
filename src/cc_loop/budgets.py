"""Execution budget checks for unattended runs."""

from __future__ import annotations

from pathlib import Path

from cc_loop.config import LoopConfig
from cc_loop.failure import FailureReport, FailureType, RecoveryDisposition
from cc_loop.state import AttemptPhase, AttemptRecord, TaskState, TaskStatus, utc_now_iso


class BudgetExhausted(Exception):
    """Raised when a configured budget is exhausted."""

    def __init__(self, report: FailureReport) -> None:
        super().__init__(report.message)
        self.report = report


def _parse_iso(ts: str) -> float | None:
    if not ts:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def wall_clock_elapsed_seconds(state: TaskState) -> float:
    """Seconds since the first attempt was created."""
    if not state.history:
        return 0.0
    first_ts = _parse_iso(state.history[0].created_at)
    if first_ts is None:
        return 0.0
    import time

    return max(0.0, time.time() - first_ts)


def count_consecutive_failures(state: TaskState) -> int:
    count = 0
    for attempt in reversed(state.history):
        if attempt.phase in {AttemptPhase.FAILED, AttemptPhase.REJECTED}:
            if attempt.decision == "reject" or attempt.phase == AttemptPhase.FAILED:
                count += 1
                continue
        if attempt.phase == AttemptPhase.MERGED:
            break
        if attempt.decision == "approve":
            break
    return count


def artifact_log_bytes(artifact_dir: Path) -> int:
    if not artifact_dir.is_dir():
        return 0
    total = 0
    for path in artifact_dir.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def count_changed_files(diff_files_path: Path) -> int:
    if not diff_files_path.is_file():
        return 0
    lines = [
        line.strip()
        for line in diff_files_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return len(lines)


def check_budgets(
    state: TaskState,
    attempt: AttemptRecord | None,
    config: LoopConfig,
    *,
    artifact_dir: Path | None = None,
) -> FailureReport | None:
    """Return a terminal failure report if any budget is exhausted."""
    max_wall = int(config.get("max_wall_clock_seconds", 0) or 0)
    if max_wall > 0:
        elapsed = wall_clock_elapsed_seconds(state)
        if elapsed > max_wall:
            return FailureReport(
                failure_type=FailureType.RECOVERY_BUDGET_EXHAUSTED,
                disposition=RecoveryDisposition.TERMINAL,
                message="wall clock budget exhausted",
                stop_reason="max_wall_clock_seconds",
                details={"elapsed_seconds": int(elapsed), "limit": max_wall},
                suggested_actions=["Increase max_wall_clock_seconds or restart task"],
            )

    max_consecutive = int(config.get("max_consecutive_failures", 0) or 0)
    if max_consecutive > 0:
        consecutive = count_consecutive_failures(state)
        if consecutive >= max_consecutive:
            return FailureReport(
                failure_type=FailureType.RECOVERY_BUDGET_EXHAUSTED,
                disposition=RecoveryDisposition.TERMINAL,
                message="consecutive failure budget exhausted",
                stop_reason="max_consecutive_failures",
                details={"consecutive_failures": consecutive, "limit": max_consecutive},
                suggested_actions=["Fix underlying failures before resuming"],
            )

    if artifact_dir is not None:
        max_bytes = int(config.get("max_artifact_log_bytes", 0) or 0)
        if max_bytes > 0:
            size = artifact_log_bytes(artifact_dir)
            if size > max_bytes:
                return FailureReport(
                    failure_type=FailureType.RECOVERY_BUDGET_EXHAUSTED,
                    disposition=RecoveryDisposition.TERMINAL,
                    message="artifact log size budget exhausted",
                    stop_reason="max_artifact_log_bytes",
                    details={"bytes": size, "limit": max_bytes},
                    suggested_actions=["Reduce artifact verbosity or increase budget"],
                )

    if attempt is not None and attempt.diff_stat_path:
        max_files = int(config.get("max_changed_files_per_attempt", 0) or 0)
        if max_files > 0:
            changed = count_changed_files(Path(attempt.diff_stat_path).parent / "diff.files.txt")
            if changed > max_files:
                return FailureReport(
                    failure_type=FailureType.RECOVERY_BUDGET_EXHAUSTED,
                    disposition=RecoveryDisposition.TERMINAL,
                    message="changed files budget exhausted",
                    stop_reason="max_changed_files_per_attempt",
                    details={"changed_files": changed, "limit": max_files},
                    suggested_actions=["Scope changes to fewer files per attempt"],
                )

    return None
