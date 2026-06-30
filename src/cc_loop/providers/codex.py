"""Codex CLI adapter for planner and reviewer roles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
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
    ) -> ProviderRunResult:
        args = self.build_args(
            worktree_path=worktree_path,
            prompt=prompt,
            output_path=output_path,
            config=config,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        timed_out = False
        try:
            completed = subprocess.run(
                args,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1

        return ProviderRunResult(
            provider=self.name,
            exit_code=exit_code,
            raw_artifact_path=output_path,
            timed_out=timed_out,
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
