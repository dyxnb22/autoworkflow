"""Claude Code CLI adapter for planner, reviewer, and implementer roles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cc_loop.config import LoopConfig
from cc_loop.subprocess_util import run_with_timeout
from cc_loop.providers.base import ProviderAdapter, ProviderRunResult, register_provider

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.MULTILINE)


def _extract_json(text: str) -> dict[str, Any]:
    """Extract and parse the first JSON object from a text response."""
    for match in _JSON_FENCE_RE.finditer(text):
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    return json.loads(text.strip())


def _extract_lenient_string_field(text: str, key: str, following_keys: tuple[str, ...]) -> str:
    start_marker = f'"{key}"'
    start = text.find(start_marker)
    if start == -1:
        raise KeyError(key)
    colon = text.find(":", start + len(start_marker))
    if colon == -1:
        raise KeyError(key)
    value_start = text.find('"', colon)
    if value_start == -1:
        raise KeyError(key)
    value_start += 1

    candidates = []
    for next_key in following_keys:
        marker = f'"{next_key}"'
        marker_index = text.find(marker, value_start)
        if marker_index != -1:
            comma = text.rfind(",", value_start, marker_index)
            if comma != -1:
                candidates.append(comma)
    if not candidates:
        end_quote = text.rfind('"')
        if end_quote <= value_start:
            raise KeyError(key)
        end = end_quote
    else:
        end = min(candidates)

    value = text[value_start:end].strip()
    if value.endswith('"'):
        value = value[:-1]
    return value.strip()


def _extract_lenient_planner_json(text: str) -> dict[str, Any]:
    """Parse Claude's JSON-like planner output when multiline strings are not escaped."""
    prompt = _extract_lenient_string_field(
        text,
        "prompt",
        ("expected_changes", "acceptance_criteria", "is_final_step"),
    )
    expected_changes = _extract_lenient_string_field(
        text,
        "expected_changes",
        ("acceptance_criteria", "is_final_step"),
    )
    acceptance_criteria = _extract_lenient_string_field(
        text,
        "acceptance_criteria",
        ("is_final_step",),
    )
    final_match = re.search(r'"is_final_step"\s*:\s*(true|false)', text, re.IGNORECASE)
    return {
        "prompt": prompt,
        "expected_changes": expected_changes,
        "acceptance_criteria": acceptance_criteria,
        "is_final_step": bool(final_match and final_match.group(1).lower() == "true"),
    }


@register_provider
class ClaudeCodeAdapter(ProviderAdapter):
    name = "claude-code"

    def build_args(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        print_only: bool = False,
    ) -> list[str]:
        args = ["claude", "--dangerously-skip-permissions"]
        if print_only:
            args.append("--print")
        model = config.get("claude_code_model", "")
        if model:
            args.extend(["--model", model])
        args.extend(["-p", prompt])
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
            print_only=print_only,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        result = run_with_timeout(
            args,
            cwd=str(worktree_path),
            timeout_seconds=timeout_seconds,
            capture_output=True,
        )
        combined = result.stdout
        if raw_output_path is not None:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            raw_output_path.write_text(combined, encoding="utf-8")
        output_path.write_text(combined, encoding="utf-8")

        return ProviderRunResult(
            provider=self.name,
            exit_code=result.returncode,
            raw_artifact_path=raw_output_path or output_path,
            timed_out=result.timed_out,
            interrupted=result.interrupted,
            killed=result.killed,
            hung=result.hung,
            duration_seconds=result.duration_seconds,
        )

    def run_planner(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
    ) -> ProviderRunResult:
        return self.run(
            worktree_path=worktree_path,
            prompt=prompt,
            output_path=output_path,
            config=config,
            timeout_seconds=timeout_seconds,
            raw_output_path=raw_output_path,
            print_only=True,
        )

    def run_reviewer(
        self,
        *,
        worktree_path: Path,
        prompt: str,
        output_path: Path,
        config: LoopConfig,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
    ) -> ProviderRunResult:
        return self.run(
            worktree_path=worktree_path,
            prompt=prompt,
            output_path=output_path,
            config=config,
            timeout_seconds=timeout_seconds,
            raw_output_path=raw_output_path,
            print_only=True,
        )

    def preflight_check_argv(self) -> list[str]:
        return ["claude", "--version"]

    def parse_planner_output(self, last_message_path: Path) -> dict[str, Any]:
        text = last_message_path.read_text(encoding="utf-8")
        try:
            data = _extract_json(text)
        except json.JSONDecodeError:
            data = _extract_lenient_planner_json(text)
        if data.get("mode") == "task_graph" or isinstance(data.get("nodes"), list):
            return {
                "mode": "task_graph",
                "summary": data.get("summary", ""),
                "nodes": data.get("nodes", []),
                "is_final_step": bool(data.get("is_final_step", False)),
            }
        return {
            "prompt": data["prompt"],
            "expected_changes": data.get("expected_changes", ""),
            "acceptance_criteria": data.get("acceptance_criteria", ""),
            "is_final_step": bool(data.get("is_final_step", False)),
        }

    def parse_reviewer_output(self, last_message_path: Path) -> dict[str, Any]:
        text = last_message_path.read_text(encoding="utf-8")
        data = _extract_json(text)
        decision = data.get("decision", "reject")
        if decision not in {"approve", "reject", "stop", "replan"}:
            decision = "reject"
        return {
            "decision": decision,
            "reason": data.get("reason", ""),
            "issues": data.get("issues", []),
            "retry_prompt": data.get("retry_prompt", ""),
            "stop_reason": data.get("stop_reason", ""),
        }
