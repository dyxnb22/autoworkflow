"""Auto-loop recovery dispatch and retry budgets."""

from __future__ import annotations

import time
from enum import StrEnum

from cc_loop.config import LoopConfig
from cc_loop.failure import (
    FailureReport,
    FailureType,
    RecoveryDisposition,
    apply_report_to_attempt,
    budget_exhausted_report,
    classify_attempt_outcome,
    classify_merge_error_message,
    classify_reviewer_outcome,
    write_failure_report,
)
from cc_loop.state import AttemptPhase, AttemptRecord, TaskState, TaskStatus


class AutoStep(StrEnum):
    RUN = "run"
    RESUME = "resume"
    REPAIR = "repair"
    MERGE_RETRY = "merge_retry"
    TERMINAL = "terminal"
    DONE = "done"
    WAIT = "wait"


def _config_bool(config: LoopConfig, key: str, default: bool = True) -> bool:
    value = config.get(key, default)
    return bool(value)


def recovery_budget_remaining(attempt: AttemptRecord, config: LoopConfig, report: FailureReport) -> bool:
    if report.failure_type == FailureType.MERGE_WORKTREE_BUSY:
        return attempt.merge_retry_count < int(config.get("max_merge_retries", 2))

    if report.failure_type == FailureType.MERGE_CONFLICT:
        if not _config_bool(config, "auto_recover_merge", True):
            return False
        return attempt.recovery_retry_count < int(config.get("max_merge_recovery_attempts", 2))

    if report.failure_type in {
        FailureType.TEST_IMPLEMENTATION,
        FailureType.TEST_GATE_BLOCKED,
        FailureType.PROVIDER_EXIT_ERROR,
        FailureType.PROVIDER_TIMEOUT,
        FailureType.REVIEWER_STOP_FIXABLE,
    }:
        if report.failure_type in {FailureType.TEST_IMPLEMENTATION, FailureType.TEST_GATE_BLOCKED}:
            if not _config_bool(config, "auto_recover_tests", True):
                return False
        if report.failure_type in {FailureType.PROVIDER_EXIT_ERROR, FailureType.PROVIDER_TIMEOUT}:
            if not _config_bool(config, "auto_recover_provider_errors", True):
                return False
        return attempt.recovery_retry_count < int(config.get("max_recovery_attempts_per_iteration", 3))

    if report.failure_type == FailureType.REVIEWER_REJECT:
        return attempt.retry < int(config.get("max_retries_per_step", 2))

    return False


def increment_recovery_counter(attempt: AttemptRecord, report: FailureReport) -> None:
    if report.failure_type == FailureType.MERGE_WORKTREE_BUSY:
        attempt.merge_retry_count += 1
    elif report.disposition == RecoveryDisposition.RECOVERABLE:
        attempt.recovery_retry_count += 1


def persist_failure_state(
    state: TaskState,
    attempt: AttemptRecord,
    report: FailureReport,
    artifact_paths: dict,
) -> FailureReport:
    apply_report_to_attempt(attempt, report)
    artifact_root = artifact_paths["plan_prompt"].parent
    write_failure_report(artifact_root, report)
    return report


def decide_auto_step(
    state: TaskState,
    attempt: AttemptRecord | None,
    config: LoopConfig,
    *,
    artifact_paths: dict | None = None,
    running: bool = False,
) -> tuple[AutoStep, FailureReport | None]:
    if running:
        return AutoStep.WAIT, None

    if state.status == TaskStatus.DONE:
        if attempt is not None and attempt.plan_json and not attempt.plan_json.get("is_final_step", True):
            if state.iteration >= int(config.get("max_iterations", 10)):
                return AutoStep.TERMINAL, FailureReport(
                    failure_type=FailureType.NONE,
                    disposition=RecoveryDisposition.TERMINAL,
                    message="reached max_iterations",
                    stop_reason="max_iterations",
                )
            return AutoStep.RUN, None
        return AutoStep.DONE, None

    if state.status == TaskStatus.FAILED:
        return AutoStep.TERMINAL, FailureReport(
            failure_type=FailureType.PROVIDER_EXIT_ERROR,
            disposition=RecoveryDisposition.TERMINAL,
            message="task failed",
            stop_reason="task_failed",
        )

    if attempt is None:
        return AutoStep.RUN, None

    report: FailureReport | None = None
    if attempt is not None:
        report = classify_reviewer_outcome(attempt)
        if report is None and artifact_paths is not None:
            report = classify_attempt_outcome(state, attempt, artifact_paths)
        elif report is None and attempt.merge_error:
            report = classify_merge_error_message(attempt.merge_error)

    if (
        state.status == TaskStatus.STOPPED
        and attempt.phase == AttemptPhase.REJECTED
        and attempt.retry >= int(config.get("max_retries_per_step", 2))
    ):
        return AutoStep.TERMINAL, FailureReport(
            failure_type=FailureType.REVIEWER_REJECT,
            disposition=RecoveryDisposition.TERMINAL,
            message="reviewer rejected all attempts",
            stop_reason="retry_exhausted",
        )

    if report is not None:
        if report.disposition == RecoveryDisposition.TERMINAL:
            return AutoStep.TERMINAL, report
        if not recovery_budget_remaining(attempt, config, report):
            return AutoStep.TERMINAL, budget_exhausted_report(report.failure_type)

        if report.failure_type == FailureType.MERGE_WORKTREE_BUSY:
            return AutoStep.MERGE_RETRY, report
        if report.failure_type in {
            FailureType.MERGE_CONFLICT,
            FailureType.TEST_IMPLEMENTATION,
            FailureType.TEST_GATE_BLOCKED,
            FailureType.PROVIDER_EXIT_ERROR,
            FailureType.PROVIDER_TIMEOUT,
            FailureType.REVIEWER_STOP_FIXABLE,
        }:
            return AutoStep.REPAIR, report

    if state.status in {TaskStatus.RUNNING, TaskStatus.INTERRUPTED}:
        return AutoStep.RESUME, report

    if (
        state.status == TaskStatus.STOPPED
        and attempt.phase == AttemptPhase.REJECTED
        and attempt.retry < int(config.get("max_retries_per_step", 2))
    ):
        return AutoStep.RESUME, report

    if state.status == TaskStatus.STOPPED and attempt.phase == AttemptPhase.APPROVED:
        if attempt.merge_error:
            if report is not None and report.disposition == RecoveryDisposition.RECOVERABLE:
                if report.failure_type == FailureType.MERGE_WORKTREE_BUSY:
                    return AutoStep.MERGE_RETRY, report
                return AutoStep.REPAIR, report
            return AutoStep.MERGE_RETRY, report
        return AutoStep.MERGE_RETRY, report

    if state.status == TaskStatus.INITIALIZED:
        return AutoStep.RUN, report

    if state.status == TaskStatus.STOPPED and attempt.phase == AttemptPhase.MERGED:
        return AutoStep.RUN, report

    if state.status == TaskStatus.STOPPED:
        return AutoStep.RESUME, report

    return AutoStep.RUN, report


def derive_next_action_from_step(step: AutoStep, report: FailureReport | None = None) -> str:
    mapping = {
        AutoStep.WAIT: "none",
        AutoStep.DONE: "done",
        AutoStep.RUN: "run",
        AutoStep.RESUME: "resume",
        AutoStep.REPAIR: "repair",
        AutoStep.MERGE_RETRY: "resume",
        AutoStep.TERMINAL: "terminal",
    }
    if step == AutoStep.TERMINAL and report is not None:
        if report.failure_type in {
            FailureType.REVIEWER_STOP_TERMINAL,
        }:
            return "inspect"
        if report.failure_type == FailureType.RECOVERY_BUDGET_EXHAUSTED:
            return "inspect"
    return mapping.get(step, "resume")


def maybe_backoff(config: LoopConfig) -> None:
    seconds = int(config.get("recovery_retry_backoff_seconds", 0) or 0)
    if seconds > 0:
        time.sleep(seconds)
