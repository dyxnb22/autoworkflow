"""Validate benchlab A/B scenario templates and config-variant prompt metrics."""

from __future__ import annotations

import unittest
from pathlib import Path

from cc_loop.config import merge_config
from cc_loop.run import (
    build_implementer_prompt,
    build_implementer_prompt_metrics,
    build_reviewer_prompt,
    build_reviewer_prompt_metrics,
)
from cc_loop.state import AttemptRecord, TaskState, TaskStatus, utc_now_iso

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "bench" / "scenarios"


def _state(*, config: dict | None = None) -> TaskState:
    return TaskState(
        task_id="bench-ab",
        goal="Fix the CLI help text",
        target_repo="/repo",
        base_branch="main",
        base_commit="abc",
        status=TaskStatus.RUNNING,
        iteration=1,
        config=merge_config(config or {}),
        providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
    )


def _attempt() -> AttemptRecord:
    return AttemptRecord(
        iteration=1,
        retry=0,
        created_at=utc_now_iso(),
        base_commit="abc",
        implementer_exit_code=0,
    )


class BenchlabScenarioTemplateTests(unittest.TestCase):
    def test_scenario_yaml_files_are_well_formed(self) -> None:
        paths = sorted(SCENARIOS_DIR.glob("*.yaml"))
        self.assertGreaterEqual(len(paths), 3)
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("name:", text)
                self.assertIn("variants:", text)
                self.assertGreaterEqual(text.count("- name:"), 2)

    def test_reviewer_context_variants_change_avoidable_miss_tokens(self) -> None:
        diff_stat = " big.txt | 999 +"
        patch_body = "x" * 5000
        state = _state()
        attempt = _attempt()
        inline_prompt = build_reviewer_prompt(
            state=state,
            attempt=attempt,
            diff_stat=diff_stat,
            patch_body=patch_body,
            test_status="passed",
            config=merge_config({"review_context_mode": "inline"}),
            inline_patch=True,
            context_mode="inline",
        )
        refs_prompt = build_reviewer_prompt(
            state=state,
            attempt=attempt,
            diff_stat=diff_stat,
            patch_body=patch_body,
            test_status="passed",
            config=merge_config({"review_context_mode": "artifact_refs"}),
            inline_patch=False,
            context_mode="artifact_refs",
        )
        inline_metrics = build_reviewer_prompt_metrics(
            prompt=inline_prompt,
            diff_stat=diff_stat,
            patch_body=patch_body,
            inline_patch=True,
            context_mode="inline",
        )
        refs_metrics = build_reviewer_prompt_metrics(
            prompt=refs_prompt,
            diff_stat=diff_stat,
            patch_body=patch_body,
            inline_patch=False,
            context_mode="artifact_refs",
        )
        self.assertGreater(
            refs_metrics["estimated_avoidable_miss_tokens"],
            inline_metrics["estimated_avoidable_miss_tokens"],
        )
        self.assertLess(refs_prompt, inline_prompt)

    def test_implementer_metrics_expose_task_context_section(self) -> None:
        state = _state()
        plan_json = {
            "prompt": "Update help text in cli.py",
            "expected_changes": "src/cc_loop/cli.py",
            "acceptance_criteria": "Help mentions summary command",
        }
        prompt = build_implementer_prompt(state, plan_json)
        metrics = build_implementer_prompt_metrics(prompt=prompt)
        self.assertGreater(metrics["task_context_chars"], 0)
        self.assertGreater(metrics["stable_prefix_ratio"], metrics["contract_prefix_ratio"])
