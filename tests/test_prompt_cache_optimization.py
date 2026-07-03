"""Tests for prompt cache optimization: review context, direct planner, argv sanitization."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import tests.fake_providers  # noqa: F401
from cc_loop.config import merge_config
from cc_loop.provider_runtime import provider_argv_from_result, sanitize_argv_for_display
from cc_loop.providers.claude_code import ClaudeCodeAdapter
from cc_loop.providers.cursor import CursorAdapter
from cc_loop.providers.base import register_provider
from cc_loop.run import (
    DYNAMIC_REVIEW_MARKER,
    build_direct_plan_json,
    build_planner_prompt,
    build_reviewer_prompt,
    build_reviewer_prompt_metrics,
    prepare_run,
    run_planning_phase,
    should_use_direct_planner,
)
from cc_loop.state import artifacts_dir, load_state, plan_artifact_paths
from tests.helpers import TempEnv, make_task


class ReviewContextModeTests(unittest.TestCase):
    def test_hybrid_inlines_small_patch(self) -> None:
        patch_body = "diff --git a/small.txt b/small.txt\n+line\n"
        prompt = build_reviewer_prompt(
            state=_state(config={"review_context_mode": "hybrid", "review_inline_patch_threshold": 8000}),
            attempt=_attempt(),
            diff_stat=" small.txt | 1 +",
            patch_body=patch_body,
            test_status="passed",
            config=merge_config({"review_context_mode": "hybrid", "review_inline_patch_threshold": 8000}),
            artifact_paths=_artifact_paths(),
            patch_paths=[Path("/tmp/patches/000-small.txt.patch")],
        )
        self.assertIn("diff --git", prompt)
        self.assertIn("### Selected patches", prompt)
        self.assertNotIn("### Patch artifact references", prompt)

    def test_hybrid_uses_artifact_refs_for_large_patch(self) -> None:
        patch_body = "x" * 9000
        config = merge_config({"review_context_mode": "hybrid", "review_inline_patch_threshold": 8000})
        paths = _artifact_paths()
        prompt = build_reviewer_prompt(
            state=_state(config=dict(config)),
            attempt=_attempt(),
            diff_stat=" big.txt | 999 +",
            patch_body=patch_body,
            test_status="passed",
            config=config,
            artifact_paths=paths,
            patch_paths=[paths["patches_dir"] / "000-big.txt.patch"],
        )
        self.assertNotIn("diff --git", prompt)
        self.assertIn("### Patch artifact references", prompt)
        self.assertIn(str(paths["test_output"]), prompt)
        self.assertIn(str(paths["patches_dir"]), prompt)
        self.assertIn("Omitted patch chars: 9000", prompt)

    def test_artifact_refs_mode_never_inlines_patch(self) -> None:
        patch_body = "diff --git a/t.txt b/t.txt\n"
        config = merge_config({"review_context_mode": "artifact_refs"})
        prompt = build_reviewer_prompt(
            state=_state(config=dict(config)),
            attempt=_attempt(),
            diff_stat=" t.txt | 1 +",
            patch_body=patch_body,
            test_status="passed",
            config=config,
            artifact_paths=_artifact_paths(),
            patch_paths=[],
        )
        self.assertNotIn("diff --git", prompt)
        self.assertIn("Inline patch: False", prompt)
        self.assertIn("### Diff stat summary", prompt)

    def test_artifact_refs_mode_omits_large_inline_diff_stat(self) -> None:
        diff_stat = "\n".join(f" file-{i}.py | {i} +" for i in range(25))
        config = merge_config({"review_context_mode": "artifact_refs"})
        paths = _artifact_paths()
        prompt = build_reviewer_prompt(
            state=_state(config=dict(config)),
            attempt=_attempt(),
            diff_stat=diff_stat,
            patch_body="z" * 9000,
            test_status="passed",
            config=config,
            artifact_paths=paths,
            patch_paths=[],
            inline_patch=False,
            context_mode="artifact_refs",
        )
        self.assertNotIn("file-24.py", prompt)
        self.assertIn(str(paths["diff_stat"]), prompt)
        self.assertIn(str(paths["diff_files"]), prompt)
        self.assertIn("### Diff stat summary", prompt)

    def test_inline_mode_keeps_full_diff_stat(self) -> None:
        diff_stat = " README.md | 1 +\n 1 file changed, 1 insertion(+)"
        prompt = build_reviewer_prompt(
            state=_state(),
            attempt=_attempt(),
            diff_stat=diff_stat,
            patch_body="diff --git a/README.md b/README.md\n",
            test_status="passed",
            context_mode="inline",
            inline_patch=True,
        )
        self.assertIn("1 file changed", prompt)
        self.assertIn("### Diff stat\n", prompt)

    def test_reviewer_metrics_track_omitted_tokens(self) -> None:
        patch_body = "y" * 10000
        config = merge_config({"review_context_mode": "artifact_refs"})
        prompt = build_reviewer_prompt(
            state=_state(config=dict(config)),
            attempt=_attempt(),
            diff_stat=" f.txt | 1 +",
            patch_body=patch_body,
            test_status="passed",
            config=config,
            artifact_paths=_artifact_paths(),
            patch_paths=[],
            inline_patch=False,
            context_mode="artifact_refs",
        )
        metrics = build_reviewer_prompt_metrics(
            prompt=prompt,
            diff_stat=" f.txt | 1 +",
            patch_body=patch_body,
            inline_patch=False,
            context_mode="artifact_refs",
        )
        self.assertEqual(metrics["omitted_patch_chars"], 10000)
        self.assertLess(metrics["evidence_payload_chars"], 10000)
        self.assertGreater(metrics["estimated_avoidable_miss_tokens"], 0)
        self.assertFalse(metrics["inline_patch"])


class PromptCacheArtifactTests(unittest.TestCase):
    def test_prompt_cache_written_after_review_metrics(self) -> None:
        patch_body = "z" * 12000
        config = merge_config({"review_context_mode": "artifact_refs"})
        prompt = build_reviewer_prompt(
            state=_state(config=dict(config)),
            attempt=_attempt(),
            diff_stat=" big.txt | 1 +",
            patch_body=patch_body,
            test_status="passed",
            config=config,
            artifact_paths=_artifact_paths(),
            patch_paths=[],
            inline_patch=False,
            context_mode="artifact_refs",
        )
        metrics = build_reviewer_prompt_metrics(
            prompt=prompt,
            diff_stat=" big.txt | 1 +",
            patch_body=patch_body,
            inline_patch=False,
            context_mode="artifact_refs",
        )
        from cc_loop.prompt_cache import build_reviewer_phase_cache, update_prompt_cache_artifact

        cache_path = _artifact_paths()["prompt_cache"]
        update_prompt_cache_artifact(
            cache_path,
            phase="reviewer",
            phase_data=build_reviewer_phase_cache(metrics=metrics),
        )
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        reviewer = payload["phases"]["reviewer"]
        self.assertGreater(reviewer["omitted_patch_chars"], 0)
        self.assertGreater(payload["totals"]["estimated_avoidable_miss_tokens"], 0)


class DirectPlannerModeTests(unittest.TestCase):
    def test_direct_planner_skips_provider_and_produces_plan_artifacts(self) -> None:
        env = TempEnv()
        planner_calls: list[str] = []

        @register_provider
        class CountingPlanner(tests.fake_providers.FakePlanner):
            name = "counting-planner-direct"

            def run(self, **kwargs):
                planner_calls.append("run")
                return super().run(**kwargs)

        try:
            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="direct-plan",
                config={
                    "planner_provider": "counting-planner-direct",
                    "planner_mode": "direct",
                },
            )
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("direct-plan", env.state_root())
                state, _attempt, artifact_paths = prepare_run(state, env.state_root())
                state = run_planning_phase(state, env.state_root(), artifact_paths)

            self.assertEqual(planner_calls, [])
            self.assertTrue(artifact_paths["plan_prompt"].is_file())
            self.assertTrue(artifact_paths["plan_prompt_meta"].is_file())
            self.assertTrue(artifact_paths["plan_raw"].is_file())
            self.assertTrue(artifact_paths["plan_parsed"].is_file())
            from cc_loop.trace import trace_file_path

            self.assertTrue(trace_file_path(artifact_paths["plan_prompt"].parent).is_file())
            plan = json.loads(artifact_paths["plan_parsed"].read_text(encoding="utf-8"))
            self.assertEqual(plan["nodes"][0]["id"], "T1")
            self.assertEqual(state.history[-1].plan_json["mode"], "task_graph")

            argv_payload = json.loads(
                (artifact_paths["plan_prompt"].parent / "command.argv.json").read_text(encoding="utf-8")
            )
            self.assertEqual(argv_payload["planner"], ["(planner-skipped-direct)"])
            self.assertEqual(
                artifact_paths["plan_provider"].read_text(encoding="utf-8").strip(),
                "(direct)",
            )
            meta = json.loads(artifact_paths["plan_prompt_meta"].read_text(encoding="utf-8"))
            self.assertEqual(meta["provider"], "(direct)")
            prompt_cache = json.loads(artifact_paths["prompt_cache"].read_text(encoding="utf-8"))
            self.assertEqual(
                prompt_cache["phases"]["planner"]["estimated_provider_prompt_tokens"],
                0,
            )
            self.assertEqual(
                prompt_cache["totals"]["estimated_provider_prompt_tokens"],
                0,
            )
        finally:
            env.close()

    def test_build_direct_plan_json_shape(self) -> None:
        plan = build_direct_plan_json("Implement feature X with tests")
        self.assertEqual(plan["nodes"][0]["id"], "T1")
        self.assertEqual(plan["nodes"][0]["description"], "Implement feature X with tests")
        self.assertEqual(len(plan["nodes"][0]["acceptance_criteria"]), 2)

    def test_planner_mode_single_controls_planner_prompt_granularity(self) -> None:
        prompt = build_planner_prompt(
            _state(config={"planner_mode": "single", "planner_granularity": "graph"})
        )
        self.assertIn("Planner granularity: SINGLE NODE.", prompt)
        self.assertNotIn("Planner granularity: MULTI-NODE GRAPH.", prompt)

    def test_auto_direct_planner_simple_goal(self) -> None:
        state = _state(config={"planner_mode": "auto"})
        state.goal = "Fix CLI bug in argparse handling"
        use_direct, reason = should_use_direct_planner(state)
        self.assertTrue(use_direct)
        self.assertIn("simple keyword", reason)

    def test_auto_direct_planner_complex_goal(self) -> None:
        state = _state(config={"planner_mode": "auto"})
        state.goal = "Redesign architecture for multi-service migration"
        use_direct, reason = should_use_direct_planner(state)
        self.assertFalse(use_direct)
        self.assertIn("complex keyword", reason)

    def test_auto_direct_planner_respects_graph_mode(self) -> None:
        state = _state(config={"planner_mode": "graph"})
        state.goal = "Fix typo in docs"
        use_direct, reason = should_use_direct_planner(state)
        self.assertFalse(use_direct)
        self.assertEqual(reason, "planner_mode=graph")

    def test_auto_direct_planner_skips_provider_and_records_cache(self) -> None:
        env = TempEnv()
        planner_calls: list[str] = []

        @register_provider
        class CountingPlannerAuto(tests.fake_providers.FakePlanner):
            name = "counting-planner-auto"

            def run(self, **kwargs):
                planner_calls.append("run")
                return super().run(**kwargs)

        try:
            make_task(
                repo=env.repo(),
                state_root=env.state_root(),
                task_id="auto-direct-plan",
                config={
                    "planner_provider": "counting-planner-auto",
                    "planner_mode": "auto",
                },
            )
            with mock.patch("cc_loop.run.DEFAULT_WORKTREE_ROOT", env.worktree_root()):
                state = load_state("auto-direct-plan", env.state_root())
                state.goal = "Fix failing test in cli module"
                from cc_loop.state import save_state

                save_state(state, env.state_root())
                state, _attempt, artifact_paths = prepare_run(state, env.state_root())
                state = run_planning_phase(state, env.state_root(), artifact_paths)

            self.assertEqual(planner_calls, [])
            prompt_cache = json.loads(artifact_paths["prompt_cache"].read_text(encoding="utf-8"))
            planner_phase = prompt_cache["phases"]["planner"]
            self.assertTrue(planner_phase["provider_skipped"])
            self.assertEqual(planner_phase["planner_mode_resolved"], "direct")
            self.assertIn("simple keyword", planner_phase["planner_direct_reason"])
        finally:
            env.close()

    def test_reviewer_task_context_before_dynamic_payload(self) -> None:
        prompt = build_reviewer_prompt(
            state=_state(goal="Scoped goal"),
            attempt=_attempt(),
            diff_stat=" a.py | 1 +",
            patch_body="diff --git a/a.py b/a.py\n",
            test_status="passed",
        )
        task_idx = prompt.index("## Task Review Context")
        dynamic_idx = prompt.index(DYNAMIC_REVIEW_MARKER)
        self.assertLess(task_idx, dynamic_idx)
        self.assertIn("Task goal: Scoped goal", prompt[task_idx:dynamic_idx])
        self.assertIn("- Iteration:", prompt[dynamic_idx:])
        self.assertNotIn("Task goal:", prompt[dynamic_idx:])


class ProviderArgvSanitizationTests(unittest.TestCase):
    def test_sanitize_argv_replaces_large_prompt(self) -> None:
        prompt = "A" * 500
        argv = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        sanitized = sanitize_argv_for_display(argv)
        self.assertEqual(len(sanitized), 4)
        self.assertTrue(sanitized[-1].startswith("<prompt:500 chars sha256="))
        self.assertNotIn("A" * 500, sanitized)

    def test_claude_provider_argv_artifact_excludes_full_prompt(self) -> None:
        adapter = ClaudeCodeAdapter()
        prompt = "review " * 300
        argv = provider_argv_from_result(
            adapter,
            worktree_path=Path("/tmp/wt"),
            prompt=prompt,
            output_path=Path("/tmp/out"),
            config=merge_config({}),
        )
        joined = " ".join(argv)
        self.assertNotIn(prompt, joined)
        self.assertIn("<prompt:", joined)

    def test_claude_provider_argv_display_preserves_print_only_flag(self) -> None:
        adapter = ClaudeCodeAdapter()
        argv = provider_argv_from_result(
            adapter,
            worktree_path=Path("/tmp/wt"),
            prompt="review " * 300,
            output_path=Path("/tmp/out"),
            config=merge_config({}),
            print_only=True,
        )
        self.assertIn("--print", argv)

    def test_cursor_provider_argv_artifact_excludes_full_prompt(self) -> None:
        adapter = CursorAdapter()
        prompt = "implement " * 400
        argv = provider_argv_from_result(
            adapter,
            worktree_path=Path("/tmp/wt"),
            prompt=prompt,
            output_path=Path("/tmp/out"),
            config=merge_config({}),
        )
        joined = " ".join(argv)
        self.assertNotIn(prompt, joined)
        self.assertIn("<prompt:", joined)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)


def _state(*, config: dict | None = None, goal: str = "Optimize cache behavior"):
    from cc_loop.state import TaskState, TaskStatus

    return TaskState(
        task_id="cache-opt",
        goal=goal,
        target_repo="/repo",
        base_branch="main",
        base_commit="abc",
        status=TaskStatus.RUNNING,
        iteration=1,
        config=merge_config(config or {}),
        providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
    )


def _attempt():
    from cc_loop.state import AttemptRecord

    return AttemptRecord(
        iteration=1,
        retry=0,
        created_at="2026-07-03T00:00:00+00:00",
        base_commit="abc",
        head_commit="def",
        implementer_exit_code=0,
        test_status="passed",
    )


def _artifact_paths(tmp: Path | None = None) -> dict[str, Path]:
    root = tmp or Path("/tmp/cc-loop-artifacts-test")
    root.mkdir(parents=True, exist_ok=True)
    paths = plan_artifact_paths(root)
    paths["patches_dir"].mkdir(parents=True, exist_ok=True)
    return paths
