"""Cursor CLI adapter for implementer role."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider


@register_provider
class CursorAdapter(ProviderAdapter):
    name = "cursor"

    def build_args(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
    ) -> list[str]:
        args = [
            "cursor",
            "agent",
            "-p",
            "--output-format",
            "json",
            "--trust",
            "--workspace",
            str(worktree_path),
            prompt,
        ]
        model = config.get("cursor_model", "")
        if model:
            args.extend(["--model", model])
        sandbox = config.get("cursor_sandbox", "")
        if sandbox:
            args.extend(["--sandbox", sandbox])
        if config.get("cursor_force", False):
            args.append("--force")
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
            with output_path.open("w", encoding="utf-8") as raw_out:
                completed = subprocess.run(
                    args,
                    stdout=raw_out,
                    stderr=subprocess.PIPE,
                    text=True,
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

    def preflight_check_argv(self) -> list[str]:
        return ["cursor", "agent", "--help"]

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError("cursor adapter does not support planner role")

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError("cursor adapter does not support reviewer role")
