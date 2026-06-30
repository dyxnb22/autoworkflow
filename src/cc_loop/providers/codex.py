"""Codex CLI adapter for planner and reviewer roles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.subprocess_util import run_with_timeout
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider


@register_provider
class CodexAdapter(ProviderAdapter):
    name = "codex"

    def build_args(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
    ) -> list[str]:
        args = [
            "codex",
            "exec",
            "--cd",
            str(worktree_path),
            "--json",
            "-o",
            str(output_path),
            "-",
        ]
        model = config.get("codex_model", "")
        if model:
            args.extend(["--model", model])
        return args

    def run(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
    ) -> ProviderRunResult:
        args = self.build_args(
            worktree_path=worktree_path,
            prompt=prompt,
            output_path=output_path,
            config=config,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if raw_output_path is not None:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)

        result = run_with_timeout(
            args,
            input=prompt,
            timeout_seconds=timeout_seconds,
            capture_output=True,
        )
        if raw_output_path is not None:
            raw_output_path.write_text(result.stdout, encoding="utf-8")

        return ProviderRunResult(
            provider=self.name,
            exit_code=result.returncode,
            raw_artifact_path=raw_output_path or output_path,
            timed_out=result.timed_out,
        )

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        text = last_message_path.read_text(encoding="utf-8")
        data = json.loads(text)
        return {
            "prompt": data["prompt"],
            "expected_changes": data.get("expected_changes", ""),
            "acceptance_criteria": data.get("acceptance_criteria", ""),
            "is_final_step": bool(data.get("is_final_step", False)),
        }

    def preflight_check_argv(self) -> list[str]:
        return ["codex", "exec", "--help"]

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        text = last_message_path.read_text(encoding="utf-8")
        data = json.loads(text)
        decision = data.get("decision", "reject")
        if decision not in {"approve", "reject", "stop"}:
            decision = "reject"
        return {
            "decision": decision,
            "reason": data.get("reason", ""),
            "issues": data.get("issues", []),
            "retry_prompt": data.get("retry_prompt", ""),
            "stop_reason": data.get("stop_reason", ""),
        }
