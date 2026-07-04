"""End-to-end provider failure path tests."""

from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.cli import _run_auto_loop
from cc_loop.failure import FailureType, failure_report_path
from cc_loop.inspect import build_status_snapshot
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider
from cc_loop.run import (
    ImplementingError,
    PlanningError,
    ReviewError,
    prepare_run,
    run_implementer_phase,
    run_planning_phase,
    run_review_phase,
)
from cc_loop.config import LoopConfig
from cc_loop.state import TaskStatus, artifacts_dir, load_state
from tests.helpers import TempEnv, make_task


@register_provider
class TimedOutPlanner(ProviderAdapter):
    name = "timed-out-planner"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        output_path.write_text("", encoding="utf-8")
        return ProviderRunResult(
            provider=self.name,
            exit_code=-1,
            raw_artifact_path=output_path,
            timed_out=True,
        )

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class HungImplementer(ProviderAdapter):
    name = "hung-implementer"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        output_path.write_text('{"result":"hung"}\n', encoding="utf-8")
        return ProviderRunResult(
            provider=self.name,
            exit_code=-9,
            raw_artifact_path=output_path,
            killed=True,
            hung=True,
        )

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class ExitCodeImplementer(ProviderAdapter):
    name = "exit-code-implementer"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        output_path.write_text('{"result":"failed"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=1, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class MalformedPlanner(ProviderAdapter):
    name = "malformed-planner"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        output_path.write_text("not-json", encoding="utf-8")
        if raw_output_path is not None:
            raw_output_path.write_text("not-json\n", encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path):
        import json

        return json.loads(last_message_path.read_text(encoding="utf-8"))

    def parse_reviewer_output(self, last_message_path: Path):
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class MalformedReviewer(ProviderAdapter):
    name = "malformed-reviewer"

    def build_args(self, *, worktree_path: Path, prompt: str, output_path: Path, config: LoopConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        output_path.write_text("{bad json", encoding="utf-8")
        if raw_output_path is not None:
            raw_output_path.write_text("{bad json\n", encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path):
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path):
        import json

        return json.loads(last_message_path.read_text(encoding="utf-8"))

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


class ProviderFailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()
        self.repo = self.env.repo()
        self.state_root = self.env.state_root()
        self.worktree_root = self.env.worktree_root()

    def tearDown(self) -> None:
        self.env.close()

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(state_root=self.state_root, max_iterations=None)

    def _patch_worktree_root(self):
        return mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", self.worktree_root)

    def test_implementer_exit_code_writes_failure_and_subprocess_artifacts(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="impl-exit",
            config={
                "implementer_provider": "exit-code-implementer",
                "auto_recover_provider_errors": False,
            },
        )
        with self._patch_worktree_root():
            state = load_state("impl-exit", self.state_root)
            state, _attempt, artifact_paths = prepare_run(state, self.state_root)
            state = run_planning_phase(state, self.state_root, artifact_paths)
            with self.assertRaises(ImplementingError):
                run_implementer_phase(state, self.state_root, artifact_paths)

        artifact_root = artifact_paths["plan_prompt"].parent
        self.assertTrue((artifact_root / "subprocess.result.json").is_file())
        self.assertTrue(failure_report_path(artifact_root).is_file())
        failure = json.loads(failure_report_path(artifact_root).read_text(encoding="utf-8"))
        self.assertEqual(failure["failure_type"], FailureType.PROVIDER_EXIT_ERROR.value)
        self.assertEqual(failure["disposition"], "recoverable")

    def test_planner_malformed_json_writes_parse_failure_artifact(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="plan-parse",
            config={"planner_provider": "malformed-planner"},
        )
        with self._patch_worktree_root():
            state = load_state("plan-parse", self.state_root)
            state, _attempt, artifact_paths = prepare_run(state, self.state_root)
            with self.assertRaises(PlanningError):
                run_planning_phase(state, self.state_root, artifact_paths)

        artifact_root = artifact_paths["plan_prompt"].parent
        self.assertTrue(failure_report_path(artifact_root).is_file())
        failure = json.loads(failure_report_path(artifact_root).read_text(encoding="utf-8"))
        self.assertEqual(failure["failure_type"], FailureType.PROVIDER_PARSE_ERROR.value)

    def test_reviewer_malformed_json_writes_parse_failure_artifact(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="review-parse",
            config={"reviewer_provider": "malformed-reviewer"},
        )
        with self._patch_worktree_root():
            state = load_state("review-parse", self.state_root)
            state, _attempt, artifact_paths = prepare_run(state, self.state_root)
            state = run_planning_phase(state, self.state_root, artifact_paths)
            state = run_implementer_phase(state, self.state_root, artifact_paths)
            with self.assertRaises(ReviewError):
                run_review_phase(state, self.state_root, artifact_paths)

        artifact_root = artifact_paths["plan_prompt"].parent
        self.assertTrue(failure_report_path(artifact_root).is_file())
        failure = json.loads(failure_report_path(artifact_root).read_text(encoding="utf-8"))
        self.assertEqual(failure["failure_type"], FailureType.PROVIDER_PARSE_ERROR.value)

    def test_provider_timeout_surfaces_in_status_and_report(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="provider-timeout",
            config={
                "planner_provider": "timed-out-planner",
                "auto_recover_provider_errors": False,
            },
        )

        with self._patch_worktree_root():
            code = _run_auto_loop(self._args(), "provider-timeout")

        self.assertNotEqual(code, 0)
        state = load_state("provider-timeout", self.state_root)
        attempt = state.history[-1]
        artifact_root = artifacts_dir("provider-timeout", attempt.iteration, attempt.retry, self.state_root)
        self.assertTrue((artifact_root / "subprocess.result.json").is_file())
        failure = json.loads((artifact_root / "failure.report.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["failure_type"], FailureType.PROVIDER_TIMEOUT.value)
        snapshot = build_status_snapshot(state, self.state_root)
        self.assertEqual(snapshot["failure"]["failure_type"], FailureType.PROVIDER_TIMEOUT.value)

    def test_provider_hung_surfaces_in_status_and_report(self) -> None:
        make_task(
            repo=self.repo,
            state_root=self.state_root,
            task_id="provider-hung",
            config={
                "implementer_provider": "hung-implementer",
                "auto_recover_provider_errors": False,
            },
        )

        with self._patch_worktree_root():
            state = load_state("provider-hung", self.state_root)
            state, _attempt, artifact_paths = prepare_run(state, self.state_root)
            state = run_planning_phase(state, self.state_root, artifact_paths)
            with self.assertRaises(ImplementingError):
                run_implementer_phase(state, self.state_root, artifact_paths)

        artifact_root = artifact_paths["plan_prompt"].parent
        self.assertTrue((artifact_root / "subprocess.result.json").is_file())
        failure = json.loads(failure_report_path(artifact_root).read_text(encoding="utf-8"))
        self.assertEqual(failure["failure_type"], FailureType.PROVIDER_HUNG.value)
        state = load_state("provider-hung", self.state_root)
        snapshot = build_status_snapshot(state, self.state_root)
        self.assertEqual(snapshot["failure"]["failure_type"], FailureType.PROVIDER_HUNG.value)


if __name__ == "__main__":
    unittest.main()
