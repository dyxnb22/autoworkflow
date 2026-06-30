"""Failure classification and structured failure reports."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from cc_loop.git import GitCommandError, GitError
from cc_loop.state import AttemptRecord, AttemptPhase, TaskStatus, TaskState


class RecoveryDisposition(StrEnum):
    RECOVERABLE = "recoverable"
    TERMINAL = "terminal"


class FailureType(StrEnum):
    MERGE_CONFLICT = "merge_conflict"
    MERGE_WORKTREE_BUSY = "merge_worktree_busy"
    MERGE_BRANCH_MISSING = "merge_branch_missing"
    MERGE_PERMISSION = "merge_permission"
    MERGE_UNKNOWN = "merge_unknown"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_EXIT_ERROR = "provider_exit_error"
    PROVIDER_PARSE_ERROR = "provider_parse_error"
    TEST_IMPLEMENTATION = "test_implementation"
    TEST_ENVIRONMENT = "test_environment"
    TEST_FLAKY_SUSPECTED = "test_flaky_suspected"
    REVIEWER_STOP_FIXABLE = "reviewer_stop_fixable"
    REVIEWER_STOP_TERMINAL = "reviewer_stop_terminal"
    REVIEWER_REJECT = "reviewer_reject"
    TEST_GATE_BLOCKED = "test_gate_blocked"
    PREFLIGHT_DIRTY_REPO = "preflight_dirty_repo"
    RECOVERY_BUDGET_EXHAUSTED = "recovery_budget_exhausted"
    NONE = "none"


_TERMINAL_STOP_KEYWORDS = (
    "requirement",
    "unclear",
    "permission",
    "access",
    "external",
    "dependency",
    "blocked",
    "cannot proceed",
)

_CONFLICT_FILE_RE = re.compile(r"CONFLICT.*?:\s*(?:Merge conflict in\s+)?(.+)$", re.MULTILINE)
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


@dataclass
class FailureReport:
    failure_type: FailureType
    disposition: RecoveryDisposition
    message: str
    stop_reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    suggested_actions: list[str] = field(default_factory=list)
    attempted_repairs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["failure_type"] = self.failure_type.value
        data["disposition"] = self.disposition.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailureReport:
        return cls(
            failure_type=FailureType(data.get("failure_type", FailureType.NONE.value)),
            disposition=RecoveryDisposition(data.get("disposition", RecoveryDisposition.TERMINAL.value)),
            message=data.get("message", ""),
            stop_reason=data.get("stop_reason", ""),
            details=dict(data.get("details") or {}),
            suggested_actions=list(data.get("suggested_actions") or []),
            attempted_repairs=list(data.get("attempted_repairs") or []),
        )


def failure_report_path(artifact_root: Path) -> Path:
    return artifact_root / "failure.report.json"


def write_failure_report(artifact_root: Path, report: FailureReport) -> Path:
    path = failure_report_path(artifact_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def read_failure_report(artifact_root: Path) -> FailureReport | None:
    path = failure_report_path(artifact_root)
    if not path.is_file():
        return None
    return FailureReport.from_dict(json.loads(path.read_text(encoding="utf-8")))


def apply_report_to_attempt(attempt: AttemptRecord, report: FailureReport) -> None:
    attempt.failure_type = report.failure_type.value
    attempt.recovery_disposition = report.disposition.value
    attempt.stop_reason = report.stop_reason or report.message
    attempt.failure_details = dict(report.details)
    attempt.attempted_repairs = list(report.attempted_repairs)


def extract_conflict_files(text: str) -> list[str]:
    files: list[str] = []
    for match in _CONFLICT_FILE_RE.finditer(text):
        candidate = match.group(1).strip()
        if candidate and candidate not in files:
            files.append(candidate)
    return files


def classify_merge_failure(exc: BaseException) -> FailureReport:
    text = str(exc)
    stderr = ""
    if isinstance(exc, GitCommandError):
        stderr = exc.result.stderr
        text = f"{text}\n{exc.result.stdout}\n{stderr}"

    lower = text.lower()
    conflict_files = extract_conflict_files(text)

    if "conflict" in lower or conflict_files:
        return FailureReport(
            failure_type=FailureType.MERGE_CONFLICT,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="merge failed due to conflicts",
            details={"conflict_files": conflict_files, "stderr_tail": stderr[-2000:]},
            suggested_actions=[
                "Review conflict files in merge.output.txt",
                "Allow cc-loop to run an implementer repair in the iteration worktree",
            ],
        )

    if "already checked out" in lower:
        return FailureReport(
            failure_type=FailureType.MERGE_WORKTREE_BUSY,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="base branch is checked out in another worktree",
            details={"stderr_tail": stderr[-2000:]},
            suggested_actions=[
                "Free the base branch worktree, then retry merge via cc-loop resume/auto",
            ],
        )

    if "not found" in lower or "unknown revision" in lower:
        return FailureReport(
            failure_type=FailureType.MERGE_BRANCH_MISSING,
            disposition=RecoveryDisposition.TERMINAL,
            message="merge failed because a branch or revision was not found",
            details={"stderr_tail": stderr[-2000:]},
            suggested_actions=["Verify branch exists and base_commit is current"],
        )

    if "permission" in lower or "denied" in lower:
        return FailureReport(
            failure_type=FailureType.MERGE_PERMISSION,
            disposition=RecoveryDisposition.TERMINAL,
            message="merge failed due to permission or access error",
            details={"stderr_tail": stderr[-2000:]},
            suggested_actions=["Check filesystem permissions and git credentials"],
        )

    return FailureReport(
        failure_type=FailureType.MERGE_UNKNOWN,
        disposition=RecoveryDisposition.TERMINAL,
        message=str(exc),
        details={"stderr_tail": stderr[-2000:]},
        suggested_actions=["Inspect merge.output.txt and repository state manually"],
    )


def classify_merge_error_message(message: str) -> FailureReport:
    if isinstance(message, str) and message:
        try:
            return classify_merge_failure(GitError(message))
        except Exception:
            pass
    return FailureReport(
        failure_type=FailureType.MERGE_UNKNOWN,
        disposition=RecoveryDisposition.TERMINAL,
        message=message or "unknown merge error",
        suggested_actions=["Inspect merge.output.txt"],
    )


def classify_provider_failure(
    *,
    phase: str,
    provider: str,
    exit_code: int | None = None,
    timed_out: bool = False,
    parse_error: str = "",
) -> FailureReport:
    if parse_error:
        return FailureReport(
            failure_type=FailureType.PROVIDER_PARSE_ERROR,
            disposition=RecoveryDisposition.TERMINAL,
            message=parse_error,
            details={"phase": phase, "provider": provider},
            suggested_actions=["Inspect provider raw artifacts and fix prompt/output contract"],
        )
    if timed_out:
        return FailureReport(
            failure_type=FailureType.PROVIDER_TIMEOUT,
            disposition=RecoveryDisposition.RECOVERABLE,
            message=f"{provider} timed out during {phase}",
            details={"phase": phase, "provider": provider, "exit_code": exit_code},
            suggested_actions=["Retry the same phase or run implementer repair"],
        )
    return FailureReport(
        failure_type=FailureType.PROVIDER_EXIT_ERROR,
        disposition=RecoveryDisposition.RECOVERABLE,
        message=f"{provider} exited with code {exit_code} during {phase}",
        details={"phase": phase, "provider": provider, "exit_code": exit_code},
        suggested_actions=["Run implementer repair with captured stderr context"],
    )


def parse_failed_tests(test_output: str) -> list[str]:
    return _PYTEST_FAILED_RE.findall(test_output)


def classify_test_failure(test_output: str, test_status: str) -> FailureReport:
    if test_status == "timed_out":
        return FailureReport(
            failure_type=FailureType.TEST_FLAKY_SUSPECTED,
            disposition=RecoveryDisposition.TERMINAL,
            message="tests timed out",
            details={"test_status": test_status},
            suggested_actions=["Increase test_timeout_seconds or optimize tests"],
        )

    failed_tests = parse_failed_tests(test_output)
    lower = test_output.lower()
    if "modulenotfounderror" in lower or "importerror" in lower or "command not found" in lower:
        return FailureReport(
            failure_type=FailureType.TEST_ENVIRONMENT,
            disposition=RecoveryDisposition.TERMINAL,
            message="tests failed due to environment or dependency issue",
            details={"failed_tests": failed_tests, "stderr_tail": test_output[-2000:]},
            suggested_actions=[
                "Install missing dependencies in the worktree environment",
                "Fix test_command configuration",
            ],
        )

    if "flaky" in lower or "intermittent" in lower:
        return FailureReport(
            failure_type=FailureType.TEST_FLAKY_SUSPECTED,
            disposition=RecoveryDisposition.TERMINAL,
            message="tests may be flaky",
            details={"failed_tests": failed_tests},
            suggested_actions=["Re-run tests manually to confirm stability"],
        )

    return FailureReport(
        failure_type=FailureType.TEST_IMPLEMENTATION,
        disposition=RecoveryDisposition.RECOVERABLE,
        message="tests failed; likely implementation issue",
        details={"failed_tests": failed_tests, "stderr_tail": test_output[-2000:]},
        suggested_actions=[
            "Fix implementation to satisfy failing tests",
            "Do not delete or weaken tests unless they are objectively incorrect",
        ],
    )


def _issues_text(review_json: dict[str, Any]) -> str:
    issues = review_json.get("issues") or []
    if isinstance(issues, list):
        return "\n".join(str(item) for item in issues)
    return str(issues)


def classify_reviewer_outcome(attempt: AttemptRecord) -> FailureReport | None:
    decision = attempt.decision
    review_json = attempt.review_json or {}

    if decision == "reject":
        return FailureReport(
            failure_type=FailureType.REVIEWER_REJECT,
            disposition=RecoveryDisposition.RECOVERABLE,
            message=review_json.get("reason", "reviewer rejected"),
            stop_reason="",
            details={"retry_prompt": review_json.get("retry_prompt", "")},
            suggested_actions=["Retry from base commit if retries remain"],
        )

    if decision != "stop":
        return None

    stop_reason = str(review_json.get("stop_reason", "")).strip()
    retry_prompt = str(review_json.get("retry_prompt", "")).strip()
    issues = _issues_text(review_json)
    combined = f"{stop_reason}\n{issues}\n{retry_prompt}".lower()

    if any(keyword in combined for keyword in _TERMINAL_STOP_KEYWORDS):
        return FailureReport(
            failure_type=FailureType.REVIEWER_STOP_TERMINAL,
            disposition=RecoveryDisposition.TERMINAL,
            message="reviewer requested stop",
            stop_reason=stop_reason or review_json.get("reason", ""),
            details={"issues": review_json.get("issues", []), "retry_prompt": retry_prompt},
            suggested_actions=[
                "Clarify requirements or resolve external blockers",
                "Inspect review.parsed.json before restarting",
            ],
        )

    if retry_prompt or any(token in combined for token in (".py", ".ts", "test", "implement", "fix")):
        return FailureReport(
            failure_type=FailureType.REVIEWER_STOP_FIXABLE,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="reviewer stop appears fixable",
            stop_reason=stop_reason or review_json.get("reason", ""),
            details={"issues": review_json.get("issues", []), "retry_prompt": retry_prompt},
            suggested_actions=["Run implementer repair using reviewer retry_prompt"],
        )

    return FailureReport(
        failure_type=FailureType.REVIEWER_STOP_TERMINAL,
        disposition=RecoveryDisposition.TERMINAL,
        message="reviewer requested stop",
        stop_reason=stop_reason or review_json.get("reason", ""),
        details={"issues": review_json.get("issues", [])},
        suggested_actions=["Inspect artifacts and clarify goal before continuing"],
    )


def classify_attempt_outcome(state: TaskState, attempt: AttemptRecord, artifact_paths: dict[str, Path]) -> FailureReport | None:
    if attempt.merge_error:
        return classify_merge_error_message(attempt.merge_error)

    reviewer_report = classify_reviewer_outcome(attempt)
    if reviewer_report is not None:
        return reviewer_report

    if attempt.test_status == "failed" and attempt.decision == "approve":
        test_output = ""
        test_path = artifact_paths.get("test_output")
        if test_path is not None and test_path.is_file():
            test_output = test_path.read_text(encoding="utf-8")
        return classify_test_failure(test_output, attempt.test_status)

    if attempt.test_status in {"failed", "timed_out"} and not attempt.decision:
        test_output = ""
        test_path = artifact_paths.get("test_output")
        if test_path is not None and test_path.is_file():
            test_output = test_path.read_text(encoding="utf-8")
        return classify_test_failure(test_output, attempt.test_status)

    if (
        state.status == TaskStatus.STOPPED
        and attempt.phase == AttemptPhase.APPROVED
        and attempt.test_status == "failed"
    ):
        return FailureReport(
            failure_type=FailureType.TEST_GATE_BLOCKED,
            disposition=RecoveryDisposition.RECOVERABLE,
            message="merge blocked because tests failed",
            details={"test_status": attempt.test_status},
            suggested_actions=["Fix implementation and re-run tests"],
        )

    if state.status == TaskStatus.FAILED or attempt.phase == AttemptPhase.FAILED:
        return FailureReport(
            failure_type=FailureType.PROVIDER_EXIT_ERROR,
            disposition=RecoveryDisposition.TERMINAL,
            message="attempt failed",
            details={"phase": attempt.phase.value},
            suggested_actions=["Inspect artifacts for the failed phase"],
        )

    return None


def budget_exhausted_report(failure_type: FailureType) -> FailureReport:
    return FailureReport(
        failure_type=FailureType.RECOVERY_BUDGET_EXHAUSTED,
        disposition=RecoveryDisposition.TERMINAL,
        message="recovery retry budget exhausted",
        stop_reason="recovery_budget_exhausted",
        details={"original_failure_type": failure_type.value},
        suggested_actions=[
            "Inspect failure.report.json and artifacts",
            "Fix underlying issue manually, then cc-loop resume",
        ],
    )
