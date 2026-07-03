"""Tests for v0.10 observability, eval, and export capabilities."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.config import merge_config
from cc_loop.evals import evaluate_assertion, run_eval_suite
from cc_loop.export import build_export_rows, write_jsonl_export
from cc_loop.prompt_metadata import build_prompt_metadata, resolve_implementer_layout
from cc_loop.report import build_report
from cc_loop.run import (
    ImplementingError,
    build_reviewer_prompt,
    build_reviewer_prompt_metrics,
    execute_run,
    run_implementer_phase,
)
from cc_loop.state import (
    AttemptPhase,
    AttemptRecord,
    TaskState,
    TaskStatus,
    artifacts_dir,
    load_state,
    plan_artifact_paths,
    save_state,
)
from cc_loop.task_graph import graph_from_planner_json
from cc_loop.trace import build_trace_snapshot, update_trace_phase
from tests.helpers import TempEnv, make_task


def _cli(*args: str, state_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cc_loop.cli", "--state-root", str(state_root), *args],
        capture_output=True,
        text=True,
    )


def _attempt_record(**overrides) -> AttemptRecord:
    base = {
        "iteration": 1,
        "retry": 0,
        "created_at": "2026-07-02T00:00:00+00:00",
        "base_commit": "abc123",
        "worktree_path": "/tmp/worktree",
        "branch": "cc-loop/test/iter-001",
        "phase": AttemptPhase.REVIEWING,
        "implementer_exit_code": 0,
        "test_status": "passed",
        "decision": "approve",
    }
    base.update(overrides)
    return AttemptRecord(**base)


def _state_with_attempt(attempt: AttemptRecord, task_id: str = "obs-task") -> TaskState:
    return TaskState(
        task_id=task_id,
        goal="Test observability",
        target_repo="/repo",
        base_branch="main",
        base_commit="abc123",
        status=TaskStatus.RUNNING,
        iteration=1,
        config=merge_config({}),
        history=[attempt],
        providers={"planner": "fake-planner", "reviewer": "fake-reviewer", "implementer": "fake-implementer"},
    )


class PromptMetadataTests(unittest.TestCase):
    def test_plan_artifact_paths_include_metadata_and_trace(self) -> None:
        paths = plan_artifact_paths(Path("/tmp/artifacts"))
        self.assertEqual(paths["plan_prompt_meta"], Path("/tmp/artifacts/plan.prompt.meta.json"))
        self.assertEqual(
            paths["implementer_prompt_meta"],
            Path("/tmp/artifacts/implementer.prompt.meta.json"),
        )
        self.assertEqual(
            paths["implementer_prompt_metrics"],
            Path("/tmp/artifacts/implementer.prompt.metrics.json"),
        )
        self.assertEqual(paths["review_prompt_meta"], Path("/tmp/artifacts/review.prompt.meta.json"))
        self.assertEqual(paths["attempt_trace"], Path("/tmp/artifacts/attempt.trace.json"))
        self.assertEqual(paths["task_context"], Path("/tmp/artifacts/task.context.json"))

    def test_build_prompt_metadata_defaults(self) -> None:
        attempt = _attempt_record(graph_node_id="T1")
        state = _state_with_attempt(attempt)
        metadata = build_prompt_metadata(
            role="reviewer",
            provider="codex",
            config=state.config,
            state=state,
            attempt=attempt,
            prompt_path=Path("/artifacts/review.prompt.txt"),
        )
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["role"], "reviewer")
        self.assertEqual(metadata["prompt_name"], "cc-loop-reviewer")
        self.assertEqual(metadata["prompt_version"], "0.10.0")
        self.assertEqual(metadata["label"], "production")
        self.assertEqual(metadata["layout"], "stable-prefix-v1")
        self.assertEqual(metadata["task_id"], "obs-task")
        self.assertEqual(metadata["graph_node_id"], "T1")

    def test_implementer_layout_legacy_vs_graph(self) -> None:
        attempt = _attempt_record(graph_node_id="")
        state = _state_with_attempt(attempt)
        self.assertEqual(resolve_implementer_layout(state, attempt), "legacy-single-step-v1")

        attempt.graph_node_id = "T2"
        state.task_graph = graph_from_planner_json(
            {
                "mode": "task_graph",
                "summary": "test graph",
                "nodes": [
                    {
                        "id": "T2",
                        "title": "Implement",
                        "description": "do work",
                        "kind": "implementation",
                        "owner": "implementer",
                        "dependencies": [],
                    }
                ],
            }
        )
        self.assertEqual(resolve_implementer_layout(state, attempt), "node-scoped-v1")


class TraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        self.worktree_root = self.env.worktree_root()

    def tearDown(self) -> None:
        self.env.close()

    def test_trace_updates_include_phase_fields(self) -> None:
        make_task(repo=self.repo, state_root=self.state_root, task_id="trace-task")
        state = load_state("trace-task", self.state_root)
        with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.worktree_root):
            state, attempt, artifact_paths = execute_run(state, self.state_root)

        trace_path = artifact_paths["attempt_trace"]
        self.assertTrue(trace_path.is_file())
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(trace["schema_version"], 1)
        self.assertEqual(trace["task_id"], "trace-task")
        self.assertIn("planning", trace["phases"])
        self.assertIn("prompt_path", trace["phases"]["planning"])
        self.assertIn("estimated_prompt_tokens", trace["phases"]["planning"])

    def test_build_trace_snapshot_from_artifacts(self) -> None:
        attempt = _attempt_record()
        state = _state_with_attempt(attempt, task_id="snap-task")
        artifact_root = self.env.root / "artifacts"
        artifact_root.mkdir()
        paths = plan_artifact_paths(artifact_root)
        paths["review_prompt"].write_text("review prompt\n", encoding="utf-8")
        metrics = {
            "schema_version": 1,
            "layout": "stable-prefix-v1",
            "stable_prefix_ratio": 0.8,
            "estimated_prompt_tokens": 42,
        }
        paths["review_prompt_metrics"].write_text(json.dumps(metrics), encoding="utf-8")

        trace = build_trace_snapshot(state, attempt, artifact_root, state.config)
        self.assertIn("review", trace["phases"])
        self.assertEqual(trace["phases"]["review"]["stable_prefix_ratio"], 0.8)
        self.assertEqual(trace["phases"]["review"]["estimated_prompt_tokens"], 4)


class ReviewerMetricsArtifactTests(unittest.TestCase):
    def test_reviewer_metrics_still_written(self) -> None:
        diff_stat = " README.md | 1 +"
        patch_body = "diff --git a/README.md b/README.md\n"
        prompt = build_reviewer_prompt(
            state=_state_with_attempt(_attempt_record()),
            attempt=_attempt_record(),
            diff_stat=diff_stat,
            patch_body=patch_body,
            test_status="passed",
        )
        metrics = build_reviewer_prompt_metrics(
            prompt=prompt,
            diff_stat=diff_stat,
            patch_body=patch_body,
        )
        self.assertEqual(metrics["layout"], "stable-prefix-v1")
        self.assertGreater(metrics["stable_prefix_ratio"], 0.6)


class EvalSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        make_task(repo=self.repo, state_root=self.state_root, task_id="eval-task")
        self.state = load_state("eval-task", self.state_root)
        attempt = AttemptRecord(
            iteration=1,
            retry=0,
            created_at="2026-07-02T00:00:00+00:00",
            base_commit="abc",
        )
        self.state.history = [attempt]
        save_state(self.state, self.state_root)
        self.artifact_root = artifacts_dir("eval-task", 1, 0, self.state_root)
        self.artifact_root.mkdir(parents=True)
        paths = plan_artifact_paths(self.artifact_root)
        paths["review_prompt_metrics"].write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "layout": "stable-prefix-v1",
                    "stable_prefix_ratio": 0.75,
                    "estimated_prompt_tokens": 100,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.env.close()

    def test_eval_suite_pass_and_fail_exit_codes(self) -> None:
        passing_suite = self.env.root / "pass.json"
        passing_suite.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "reviewer-cache-smoke",
                    "cases": [
                        {
                            "id": "reviewer-stable-prefix",
                            "description": "Reviewer prompt stable prefix should be high",
                            "artifact": "review.prompt.metrics.json",
                            "assertions": [
                                {"path": "stable_prefix_ratio", "op": ">=", "value": 0.6},
                                {"path": "layout", "op": "==", "value": "stable-prefix-v1"},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = run_eval_suite(self.state, self.state_root, passing_suite)
        self.assertTrue(result["passed"])
        cli = _cli("eval", "--task-id", "eval-task", "--suite", str(passing_suite), state_root=self.state_root)
        self.assertEqual(cli.returncode, 0)

        failing_suite = self.env.root / "fail.json"
        failing_suite.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "reviewer-cache-smoke",
                    "cases": [
                        {
                            "id": "too-high",
                            "description": "should fail",
                            "artifact": "review.prompt.metrics.json",
                            "assertions": [{"path": "stable_prefix_ratio", "op": ">=", "value": 0.99}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = run_eval_suite(self.state, self.state_root, failing_suite)
        self.assertFalse(result["passed"])
        cli = _cli(
            "eval",
            "--task-id",
            "eval-task",
            "--suite",
            str(failing_suite),
            state_root=self.state_root,
        )
        self.assertEqual(cli.returncode, 1)

    def test_eval_invalid_suite_exit_code(self) -> None:
        bad_suite = self.env.root / "bad.json"
        bad_suite.write_text('{"schema_version": 2}', encoding="utf-8")
        cli = _cli("eval", "--task-id", "eval-task", "--suite", str(bad_suite), state_root=self.state_root)
        self.assertEqual(cli.returncode, 2)

        malformed_case_suite = self.env.root / "malformed-case.json"
        malformed_case_suite.write_text(
            json.dumps({"schema_version": 1, "cases": ["not an object"]}),
            encoding="utf-8",
        )
        cli = _cli(
            "eval",
            "--task-id",
            "eval-task",
            "--suite",
            str(malformed_case_suite),
            state_root=self.state_root,
        )
        self.assertEqual(cli.returncode, 2)

    def test_assertion_ops(self) -> None:
        passed, _ = evaluate_assertion(0.8, ">=", 0.6)
        self.assertTrue(passed)
        passed, _ = evaluate_assertion("stable-prefix-v1", "==", "stable-prefix-v1")
        self.assertTrue(passed)
        passed, _ = evaluate_assertion("hello world", "contains", "world")
        self.assertTrue(passed)
        passed, _ = evaluate_assertion({"a": 1}, "exists", True)
        self.assertTrue(passed)


class ExportReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.state_root = self.env.state_root()
        self.repo = self.env.repo()
        make_task(repo=self.repo, state_root=self.state_root, task_id="export-task")
        self.state = load_state("export-task", self.state_root)
        attempt = _attempt_record(
            implementer_provider="fake-implementer",
            review_provider="fake-reviewer",
            review_json={"decision": "approve", "reason": "ok"},
        )
        self.state.history = [attempt]
        save_state(self.state, self.state_root)
        self.artifact_root = artifacts_dir("export-task", 1, 0, self.state_root)
        self.artifact_root.mkdir(parents=True)
        paths = plan_artifact_paths(self.artifact_root)
        paths["plan_prompt"].write_text("plan\n", encoding="utf-8")
        paths["implementer_prompt"].write_text("implementer\n", encoding="utf-8")
        paths["review_prompt"].write_text("review\n", encoding="utf-8")
        paths["review_prompt_metrics"].write_text(
            json.dumps({"schema_version": 1, "stable_prefix_ratio": 0.7, "estimated_prompt_tokens": 10}),
            encoding="utf-8",
        )
        update_trace_phase(
            state=self.state,
            attempt=attempt,
            artifact_paths=paths,
            config=self.state.config,
            phase="planning",
            status="completed",
            prompt_path=str(paths["plan_prompt"]),
            prompt_meta_path=str(paths["plan_prompt_meta"]),
            raw_path=str(paths["plan_raw"]),
            estimated_prompt_tokens=1,
        )
        update_trace_phase(
            state=self.state,
            attempt=attempt,
            artifact_paths=paths,
            config=self.state.config,
            phase="review",
            status="completed",
            prompt_path=str(paths["review_prompt"]),
            prompt_meta_path=str(paths["review_prompt_meta"]),
            metrics_path=str(paths["review_prompt_metrics"]),
            decision="approve",
            estimated_prompt_tokens=3,
            stable_prefix_ratio=0.7,
        )

    def tearDown(self) -> None:
        self.env.close()

    def test_export_jsonl_contains_expected_rows(self) -> None:
        rows = build_export_rows(self.state, self.state_root)
        roles = {row["role"] for row in rows}
        self.assertIn("planner", roles)
        self.assertIn("implementer", roles)
        self.assertIn("reviewer", roles)
        reviewer = next(row for row in rows if row["role"] == "reviewer")
        self.assertEqual(reviewer["decision"], "approve")
        self.assertEqual(reviewer["stable_prefix_ratio"], 0.7)

        output_path = self.env.root / "export.jsonl"
        count = write_jsonl_export(self.state, self.state_root, output_path)
        self.assertGreaterEqual(count, 3)
        lines = output_path.read_text(encoding="utf-8").strip().splitlines()
        payload = json.loads(lines[-1])
        self.assertEqual(payload["schema_version"], 1)

        cli = _cli(
            "export",
            "--task-id",
            "export-task",
            "--format",
            "jsonl",
            "--output",
            str(output_path),
            state_root=self.state_root,
        )
        self.assertEqual(cli.returncode, 0)

    def test_export_sums_multi_reviewer_last_messages(self) -> None:
        paths = plan_artifact_paths(self.artifact_root)
        first_last_message = paths["review_last_message"]
        second_last_message = paths["review_last_message"].parent / "review.last-message.1.txt"
        first_raw = paths["review_raw"]
        second_raw = paths["review_raw"].parent / "review.raw.1.jsonl"
        first_last_message.write_text("12345678", encoding="utf-8")
        second_last_message.write_text("1234", encoding="utf-8")
        first_raw.write_text("{}\n", encoding="utf-8")
        second_raw.write_text("{}\n", encoding="utf-8")
        update_trace_phase(
            state=self.state,
            attempt=self.state.history[-1],
            artifact_paths=paths,
            config=self.state.config,
            phase="review",
            status="completed",
            prompt_path=str(paths["review_prompt"]),
            prompt_meta_path=str(paths["review_prompt_meta"]),
            raw_path=str(first_raw),
            raw_paths=[str(first_raw), str(second_raw)],
            last_message_paths=[str(first_last_message), str(second_last_message)],
            metrics_path=str(paths["review_prompt_metrics"]),
            decision="approve",
            estimated_prompt_tokens=3,
            stable_prefix_ratio=0.7,
        )

        rows = build_export_rows(self.state, self.state_root)
        reviewer = next(row for row in rows if row["role"] == "reviewer")
        self.assertEqual(reviewer["estimated_output_tokens"], 3)
        self.assertEqual(reviewer["last_message_paths"], [str(first_last_message), str(second_last_message)])

    def test_failed_implementer_attempt_still_updates_trace_and_export(self) -> None:
        attempt = _attempt_record(
            plan_json={"prompt": "do work", "expected_changes": "", "acceptance_criteria": ""},
            implementer_exit_code=None,
            implementer_provider="missing-provider",
            worktree_path=str(self.repo),
        )
        state = _state_with_attempt(attempt, task_id="failed-export-task")
        state.config["implementer_provider"] = "missing-provider"
        state.providers["implementer"] = "missing-provider"
        save_state(state, self.state_root)
        artifact_root = artifacts_dir("failed-export-task", 1, 0, self.state_root)
        artifact_root.mkdir(parents=True)
        paths = plan_artifact_paths(artifact_root)

        with self.assertRaises(ImplementingError):
            run_implementer_phase(state, self.state_root, paths, attempt=attempt)

        trace = json.loads(paths["attempt_trace"].read_text(encoding="utf-8"))
        self.assertEqual(trace["phases"]["implementation"]["status"], "failed")
        self.assertIn("unknown provider", trace["phases"]["implementation"]["error"])

        rows = build_export_rows(state, self.state_root)
        implementer = next(row for row in rows if row["role"] == "implementer")
        self.assertEqual(implementer["provider"], "missing-provider")
        self.assertGreater(implementer["estimated_prompt_tokens"], 0)

    def test_report_json_includes_observability_fields(self) -> None:
        report = build_report(self.state, self.state_root)
        observability = report["observability"]
        self.assertIn("trace_path", observability)
        self.assertIn("reviewer_prompt_metrics", observability)
        self.assertIn("prompt_metadata_paths", observability)
        self.assertIn("planner", observability["prompt_metadata_paths"])

        cli = _cli("report", "--task-id", "export-task", "--json", state_root=self.state_root)
        payload = json.loads(cli.stdout)
        self.assertIn("observability", payload)
