"""Tests for prompt fragment loading and overrides."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cc_loop.config import merge_config
from cc_loop.prompts import PACKAGE_PROMPTS_DIR, clear_prompt_fragment_cache, load_prompt_fragment, resolve_prompts_dir
from cc_loop.run import build_planner_prompt
from cc_loop.state import TaskState, TaskStatus


class PromptFragmentTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_prompt_fragment_cache()

    def test_default_fragments_load_from_package(self) -> None:
        text = load_prompt_fragment("implementer/stable_contract.txt")
        self.assertIn("## Implementer Contract", text)
        self.assertEqual(resolve_prompts_dir(merge_config({})), PACKAGE_PROMPTS_DIR)

    def test_prompts_dir_override_replaces_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override_dir = Path(tmp)
            fragment_path = override_dir / "implementer" / "stable_contract.txt"
            fragment_path.parent.mkdir(parents=True)
            fragment_path.write_text("CUSTOM IMPLEMENTER CONTRACT\n", encoding="utf-8")
            config = merge_config({"prompts_dir": str(override_dir)})
            clear_prompt_fragment_cache()
            text = load_prompt_fragment("implementer/stable_contract.txt", config=config)
            self.assertEqual(text, "CUSTOM IMPLEMENTER CONTRACT\n")

    def test_planner_prompt_uses_fragments(self) -> None:
        state = TaskState(
            task_id="frag-planner",
            goal="Unique planner fragment goal",
            target_repo="/repo",
            base_branch="main",
            base_commit="abc",
            status=TaskStatus.RUNNING,
            iteration=1,
            config=merge_config({}),
            providers={"planner": "codex", "reviewer": "codex", "implementer": "cursor"},
        )
        prompt = build_planner_prompt(state)
        self.assertIn("You are the cc-loop planner.", prompt)
        self.assertIn("## Preferred Task Graph Shape", prompt)
        self.assertIn("Unique planner fragment goal", prompt)
