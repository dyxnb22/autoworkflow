"""Tests for bounded diff collection."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from cc_loop.diff import collect_bounded_review_patches
from tests.helpers import init_git_repo


class DiffCollectorTests(unittest.TestCase):
    def test_collects_source_patch_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            (repo / "feature.py").write_text("print('x')\n", encoding="utf-8")
            subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add feature"],
                cwd=repo,
                check=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD~1"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            patches_dir = Path(tmp) / "patches"
            paths, body, used = collect_bounded_review_patches(
                repo,
                base,
                patches_dir=patches_dir,
                max_bytes=10_000,
            )
            self.assertEqual(len(paths), 1)
            self.assertIn("feature.py", body)
            self.assertGreater(used, 0)

    def test_respects_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_git_repo(repo)
            (repo / "big.py").write_text("x = " + "1\n" * 5000, encoding="utf-8")
            subprocess.run(["git", "add", "big.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "big"],
                cwd=repo,
                check=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD~1"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            patches_dir = Path(tmp) / "patches"
            paths, body, used = collect_bounded_review_patches(
                repo,
                base,
                patches_dir=patches_dir,
                max_bytes=200,
            )
            self.assertEqual(paths, [])
            self.assertIn("exceeds max_review_patch_bytes", body)


if __name__ == "__main__":
    unittest.main()
