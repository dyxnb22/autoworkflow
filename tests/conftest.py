"""Pytest defaults for the Python package test suite."""

from __future__ import annotations

import os

# Keep Python CLI tests on the Python implementation. Rust entry delegation is
# covered by cargo contract tests and scripts/cc-loop.
os.environ.setdefault("CC_LOOP_FORCE_PYTHON", "1")
