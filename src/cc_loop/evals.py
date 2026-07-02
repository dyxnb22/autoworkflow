"""Local eval suite runner for CI-style artifact assertions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cc_loop.state import TaskState, artifacts_dir, plan_artifact_paths

SUPPORTED_OPS = {"==", "!=", ">=", ">", "<=", "<", "contains", "exists"}
EVAL_SUITE_SCHEMA_VERSION = 1
_MISSING = object()


@dataclass
class AssertionResult:
    case_id: str
    assertion_index: int
    path: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    message: str


@dataclass
class CaseResult:
    case_id: str
    description: str
    artifact: str
    passed: bool
    assertions: list[AssertionResult]
    error: str = ""


def load_eval_suite(path: Path) -> dict[str, Any]:
    """Load and validate an eval suite JSON file."""
    if not path.is_file():
        raise ValueError(f"eval suite not found: {path}")
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid eval suite JSON: {exc}") from exc
    if not isinstance(suite, dict):
        raise ValueError("eval suite must be a JSON object")
    if suite.get("schema_version") != EVAL_SUITE_SCHEMA_VERSION:
        raise ValueError(f"unsupported eval suite schema_version: {suite.get('schema_version')}")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("eval suite must include a non-empty cases array")
    return suite


def resolve_value(data: Any, path: str) -> Any:
    """Resolve a dotted path against nested dict data."""
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def evaluate_assertion(actual: Any, op: str, expected: Any) -> tuple[bool, str]:
    if op not in SUPPORTED_OPS:
        return False, f"unsupported op: {op}"

    if op == "exists":
        exists = actual is not _MISSING
        passed = exists if expected else not exists
        return passed, f"exists={exists}"

    if actual is _MISSING:
        return False, "path not found"

    if op == "==":
        passed = actual == expected
    elif op == "!=":
        passed = actual != expected
    elif op == ">=":
        passed = actual >= expected
    elif op == ">":
        passed = actual > expected
    elif op == "<=":
        passed = actual <= expected
    elif op == "<":
        passed = actual < expected
    elif op == "contains":
        if isinstance(actual, str):
            passed = str(expected) in actual
        elif isinstance(actual, (list, tuple, dict)):
            passed = expected in actual
        else:
            passed = False
    else:
        passed = False

    return passed, f"actual={actual!r}"


def _load_artifact_json(artifact_path: Path) -> dict[str, Any]:
    if not artifact_path.is_file():
        raise ValueError(f"artifact not found: {artifact_path}")
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"artifact is not valid JSON: {artifact_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"artifact must be a JSON object: {artifact_path}")
    return data


def evaluate_case(case: dict[str, Any], artifact_dir: Path) -> CaseResult:
    case_id = str(case.get("id", ""))
    description = str(case.get("description", ""))
    artifact_name = str(case.get("artifact", ""))
    assertions = case.get("assertions", [])

    if not case_id:
        return CaseResult(
            case_id="(missing-id)",
            description=description,
            artifact=artifact_name,
            passed=False,
            assertions=[],
            error="case id is required",
        )
    if not artifact_name:
        return CaseResult(
            case_id=case_id,
            description=description,
            artifact=artifact_name,
            passed=False,
            assertions=[],
            error="case artifact is required",
        )
    if not isinstance(assertions, list) or not assertions:
        return CaseResult(
            case_id=case_id,
            description=description,
            artifact=artifact_name,
            passed=False,
            assertions=[],
            error="case assertions must be a non-empty array",
        )

    artifact_path = artifact_dir / artifact_name
    try:
        artifact_data = _load_artifact_json(artifact_path)
    except ValueError as exc:
        return CaseResult(
            case_id=case_id,
            description=description,
            artifact=artifact_name,
            passed=False,
            assertions=[],
            error=str(exc),
        )

    assertion_results: list[AssertionResult] = []
    all_passed = True
    for index, assertion in enumerate(assertions):
        if not isinstance(assertion, dict):
            all_passed = False
            assertion_results.append(
                AssertionResult(
                    case_id=case_id,
                    assertion_index=index,
                    path="",
                    op="",
                    expected=None,
                    actual=None,
                    passed=False,
                    message="assertion must be an object",
                )
            )
            continue

        path = str(assertion.get("path", ""))
        op = str(assertion.get("op", ""))
        expected = assertion.get("value", None)
        if op == "exists" and "value" not in assertion:
            expected = True

        actual = resolve_value(artifact_data, path) if path else _MISSING
        passed, message = evaluate_assertion(actual, op, expected)
        if not passed:
            all_passed = False
        assertion_results.append(
            AssertionResult(
                case_id=case_id,
                assertion_index=index,
                path=path,
                op=op,
                expected=expected,
                actual=None if actual is _MISSING else actual,
                passed=passed,
                message=message,
            )
        )

    return CaseResult(
        case_id=case_id,
        description=description,
        artifact=artifact_name,
        passed=all_passed,
        assertions=assertion_results,
    )


def latest_attempt_artifact_dir(state: TaskState, state_root: Path) -> Path:
    if not state.history:
        raise ValueError("task has no attempts")
    attempt = state.history[-1]
    return artifacts_dir(state.task_id, attempt.iteration, attempt.retry, state_root)


def run_eval_suite(state: TaskState, state_root: Path, suite_path: Path) -> dict[str, Any]:
    """Evaluate all cases in a suite against the latest attempt artifacts."""
    suite = load_eval_suite(suite_path)
    artifact_dir = latest_attempt_artifact_dir(state, state_root)
    case_results = [evaluate_case(case, artifact_dir) for case in suite["cases"]]
    passed_count = sum(1 for result in case_results if result.passed)
    total_count = len(case_results)
    return {
        "schema_version": EVAL_SUITE_SCHEMA_VERSION,
        "suite_name": suite.get("name", ""),
        "suite_path": str(suite_path),
        "task_id": state.task_id,
        "artifact_dir": str(artifact_dir),
        "passed": passed_count == total_count and total_count > 0,
        "summary": {
            "total": total_count,
            "passed": passed_count,
            "failed": total_count - passed_count,
        },
        "cases": [
            {
                "id": result.case_id,
                "description": result.description,
                "artifact": result.artifact,
                "passed": result.passed,
                "error": result.error,
                "assertions": [
                    {
                        "index": assertion.assertion_index,
                        "path": assertion.path,
                        "op": assertion.op,
                        "expected": assertion.expected,
                        "actual": assertion.actual,
                        "passed": assertion.passed,
                        "message": assertion.message,
                    }
                    for assertion in result.assertions
                ],
            }
            for result in case_results
        ],
    }


def format_eval_human(result: dict[str, Any]) -> str:
    lines = [
        f"Eval suite: {result.get('suite_name', '')}",
        f"Task: {result.get('task_id', '')}",
        f"Artifacts: {result.get('artifact_dir', '')}",
        f"Result: {'PASS' if result.get('passed') else 'FAIL'} "
        f"({result.get('summary', {}).get('passed', 0)}/"
        f"{result.get('summary', {}).get('total', 0)} cases)",
        "",
    ]
    for case in result.get("cases", []):
        status = "PASS" if case.get("passed") else "FAIL"
        lines.append(f"[{status}] {case.get('id', '')}: {case.get('description', '')}")
        if case.get("error"):
            lines.append(f"  error: {case['error']}")
        for assertion in case.get("assertions", []):
            mark = "ok" if assertion.get("passed") else "fail"
            lines.append(
                f"  - {mark} {assertion.get('path', '')} {assertion.get('op', '')} "
                f"{assertion.get('expected', '')!r} ({assertion.get('message', '')})"
            )
    return "\n".join(lines).rstrip() + "\n"
