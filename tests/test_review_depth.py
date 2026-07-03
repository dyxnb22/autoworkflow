"""Tests for two-stage reviewer depth (fast/deep/auto)."""

from __future__ import annotations

import unittest
from pathlib import Path

from cc_loop.config import merge_config
from cc_loop.review_depth import resolve_review_depth_mode, should_pre_escalate_to_deep
from cc_loop.run import (
    DYNAMIC_REVIEW_MARKER,
    build_fast_reviewer_prompt,
    build_reviewer_prompt,
    _aggregate_reviewer_decisions,
)
from cc_loop.state import AttemptRecord, TaskState, TaskStatus, plan_artifact_paths


def _state(*, config: dict | None = None, goal: str = "Fix docs typo") -> TaskState:
    return TaskState(
        task_id="depth-test",
        goal=goal,
        target_repo="/repo",
        base_branch="main",
        base_commit="abc",
        status=TaskStatus.RUNNING,
        iteration=1,
        config=merge_config(config or {}),
        providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
    )


def _attempt(*, test_status: str = "passed") -> AttemptRecord:
    return AttemptRecord(
        iteration=1,
        retry=0,
        created_at="2026-07-03T00:00:00+00:00",
        base_commit="abc",
        head_commit="def",
        implementer_exit_code=0,
        test_status=test_status,
    )


class ReviewDepthHeuristicTests(unittest.TestCase):
    def test_default_depth_is_standard(self) -> None:
        self.assertEqual(resolve_review_depth_mode(merge_config({})), "standard")

    def test_pre_escalate_when_tests_failed(self) -> None:
        escalate, reasons = should_pre_escalate_to_deep(
            test_status="failed",
            changed_file_count=1,
            diff_stat_chars=100,
            patch_paths=[],
            config=merge_config({"review_depth": "auto"}),
        )
        self.assertTrue(escalate)
        self.assertIn("test_not_passed", reasons)

    def test_pre_escalate_when_many_files(self) -> None:
        escalate, reasons = should_pre_escalate_to_deep(
            test_status="passed",
            changed_file_count=15,
            diff_stat_chars=100,
            patch_paths=[],
            config=merge_config({"review_fast_max_changed_files": 10}),
        )
        self.assertTrue(escalate)
        self.assertTrue(any("changed_files" in reason for reason in reasons))

    def test_no_pre_escalate_for_small_passing_change(self) -> None:
        escalate, reasons = should_pre_escalate_to_deep(
            test_status="passed",
            changed_file_count=2,
            diff_stat_chars=200,
            patch_paths=[Path("/tmp/patches/000-readme.patch")],
            config=merge_config({}),
        )
        self.assertFalse(escalate)
        self.assertEqual(reasons, [])


class FastReviewerPromptTests(unittest.TestCase):
    def test_fast_prompt_uses_artifact_refs_only(self) -> None:
        diff_lines = [f" file-{i}.py | {i} +" for i in range(25)]
        diff_stat = "\n".join(diff_lines)
        paths = plan_artifact_paths(Path("/tmp/review-depth-fast"))
        prompt = build_fast_reviewer_prompt(
            state=_state(),
            attempt=_attempt(),
            diff_stat=diff_stat,
            test_status="passed",
            artifact_paths=paths,
            patch_paths=[paths["patches_dir"] / "000-a.patch"],
            patch_body_chars=9000,
        )
        self.assertIn("Review stage: fast", prompt)
        self.assertIn("### Diff stat summary", prompt)
        self.assertNotIn("file-24.py", prompt)
        self.assertIn(str(paths["diff_stat"]), prompt)
        self.assertNotIn("diff --git", prompt)
        dynamic = prompt[prompt.index(DYNAMIC_REVIEW_MARKER) :]
        self.assertNotIn("### Selected patches", dynamic)

    def test_fast_prompt_has_higher_stable_prefix_than_inline_deep(self) -> None:
        diff_stat = " README.md | 1 +"
        patch_body = "x" * 12000
        paths = plan_artifact_paths(Path("/tmp/review-depth-compare"))
        fast = build_fast_reviewer_prompt(
            state=_state(),
            attempt=_attempt(),
            diff_stat=diff_stat,
            test_status="passed",
            artifact_paths=paths,
            patch_paths=[],
            patch_body_chars=len(patch_body),
        )
        deep = build_reviewer_prompt(
            state=_state(),
            attempt=_attempt(),
            diff_stat=diff_stat,
            patch_body=patch_body,
            test_status="passed",
            config=merge_config({"review_context_mode": "inline"}),
            artifact_paths=paths,
            patch_paths=[],
            context_mode="inline",
            inline_patch=True,
        )
        fast_prefix = len(fast[: fast.index(DYNAMIC_REVIEW_MARKER)])
        deep_prefix = len(deep[: deep.index(DYNAMIC_REVIEW_MARKER)])
        self.assertGreater(fast_prefix, 0)
        self.assertLess(len(fast), len(deep))


class ReviewDecisionAggregationTests(unittest.TestCase):
    def test_aggregate_prefers_escalate_over_reject(self) -> None:
        aggregated = _aggregate_reviewer_decisions(
            [
                {"decision": "reject", "reason": "no"},
                {"decision": "escalate", "reason": "needs deep review"},
            ]
        )
        self.assertEqual(aggregated["decision"], "escalate")


if __name__ == "__main__":
    unittest.main()
