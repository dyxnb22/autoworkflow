"""Fake provider adapters for deterministic tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider


@register_provider
class FakeGraphPlanner(ProviderAdapter):
    """Planner that returns a two-node task graph."""

    name = "fake-graph-planner"

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
        payload = {
            "mode": "task_graph",
            "summary": "Add hello files in two steps",
            "nodes": [
                {
                    "id": "T1",
                    "title": "Create hello.txt",
                    "description": "Create hello.txt with contents hello",
                    "kind": "implementation",
                    "owner": "implementer",
                    "dependencies": [],
                    "acceptance_criteria": ["hello.txt exists"],
                    "files_scope": ["hello.txt"],
                },
                {
                    "id": "T2",
                    "title": "Create world.txt",
                    "description": "Create world.txt with contents world",
                    "kind": "implementation",
                    "owner": "implementer",
                    "dependencies": ["T1"],
                    "acceptance_criteria": ["world.txt exists"],
                    "files_scope": ["world.txt"],
                },
            ],
            "is_final_step": False,
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        if raw_output_path is not None:
            raw_output_path.write_text("{}\n", encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        return json.loads(last_message_path.read_text(encoding="utf-8"))

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class FakeGraphImplementer(ProviderAdapter):
    """Implementer that creates files based on graph node id in the prompt."""

    name = "fake-graph-implementer"

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
        if "T2" in prompt and "world.txt" in prompt:
            (worktree_path / "world.txt").write_text("world\n", encoding="utf-8")
        else:
            (worktree_path / "hello.txt").write_text("hello\n", encoding="utf-8")
        output_path.write_text('{"result":"ok"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class FakePlanner(ProviderAdapter):
    name = "fake-planner"

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
        payload = {
            "prompt": "Create hello.txt with contents hello",
            "expected_changes": "hello.txt",
            "acceptance_criteria": "hello.txt exists",
            "is_final_step": True,
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        if raw_output_path is not None:
            raw_output_path.write_text("{}\n", encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        return json.loads(last_message_path.read_text(encoding="utf-8"))

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class FakeImplementer(ProviderAdapter):
    name = "fake-implementer"

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
        (worktree_path / "hello.txt").write_text("hello\n", encoding="utf-8")
        output_path.write_text('{"result":"ok"}\n', encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def preflight_check_argv(self) -> list[str]:
        return ["true"]


@register_provider
class FakeReviewer(ProviderAdapter):
    name = "fake-reviewer"

    decision = "approve"

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
        payload = {
            "decision": self.decision,
            "reason": f"fake reviewer says {self.decision}",
            "issues": [],
            "retry_prompt": "try again" if self.decision == "reject" else "",
            "stop_reason": "user stop" if self.decision == "stop" else "",
        }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        if raw_output_path is not None:
            raw_output_path.write_text("{}\n", encoding="utf-8")
        return ProviderRunResult(provider=self.name, exit_code=0, raw_artifact_path=output_path)

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        return json.loads(last_message_path.read_text(encoding="utf-8"))

    def preflight_check_argv(self) -> list[str]:
        return ["true"]
