"""Tests for commit staging diagnostics and artifact warnings."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cc_loop.git import commit_worktree_changes
from tests.helpers import TempEnv


class CommitStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = TempEnv()

    def tearDown(self) -> None:
        self.env.close()

    def test_commit_staging_report_records_files_and_warns_on_artifacts(self) -> None:
        repo = self.env.repo()
        (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        cache_dir = repo / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "tracked.cpython-311.pyc").write_text("bytecode\n", encoding="utf-8")
        (repo / ".DS_Store").write_text("ds\n", encoding="utf-8")

        report_path = self.env.root / "commit.staging.json"
        head = commit_worktree_changes(
            repo,
            "stage generated artifacts",
            staging_report_path=report_path,
        )
        self.assertIsNotNone(head)

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertIn("tracked.py", payload["staged_files"])
        self.assertTrue(payload["artifact_warnings"])
        warning_text = "\n".join(payload["artifact_warnings"])
        self.assertIn("__pycache__", warning_text)
        self.assertIn(".DS_Store", warning_text)
        self.assertIn(".pyc", warning_text)

    def test_clean_worktree_skips_staging_report(self) -> None:
        repo = self.env.repo()
        report_path = Path(self.env.root / "clean.staging.json")
        head = commit_worktree_changes(repo, "noop", staging_report_path=report_path)
        self.assertTrue(head)
        self.assertFalse(report_path.is_file())


if __name__ == "__main__":
    unittest.main()
