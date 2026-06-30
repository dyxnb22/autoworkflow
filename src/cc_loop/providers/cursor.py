"""Cursor CLI adapter for implementer role."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.subprocess_util import run_with_timeout
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
        raw_output_path: Path | None = None,
        print_only: bool = False,
    ) -> ProviderRunResult:
        args = self.build_args(
            worktree_path=worktree_path,
            prompt=prompt,
            output_path=output_path,
            config=config,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as raw_out:
            result = run_with_timeout(
                args,
                timeout_seconds=timeout_seconds,
                capture_output=True,
                stdout_file=raw_out,
                stderr_file=subprocess.PIPE,
            )

        return ProviderRunResult(
            provider=self.name,
            exit_code=result.returncode,
            raw_artifact_path=output_path,
            timed_out=result.timed_out,
        )

    def preflight_check_argv(self) -> list[str]:
        return ["cursor", "agent", "--help"]

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError("cursor adapter does not support planner role")

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        raise NotImplementedError("cursor adapter does not support reviewer role")
