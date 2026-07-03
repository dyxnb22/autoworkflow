"""Parse and normalize ``--test-command`` argv from the CLI."""

from __future__ import annotations

import json
import shlex

TEST_COMMAND_HINT = (
    "Use --test-command -- pytest tests -q "
    "(place `--` before the command so flags like -q are not parsed as cc-loop options)"
)


def normalize_test_command(argv: list[str] | None) -> list[str] | None:
    """Normalize CLI ``--test-command`` tokens into a subprocess argv list."""
    if argv is None:
        return None
    if not argv:
        raise ValueError("test_command must be a non-empty argv list")

    parts = list(argv)
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        raise ValueError("test_command is empty after `--` separator")

    if len(parts) == 1:
        token = parts[0].strip()
        if " " in token:
            parts = shlex.split(token)

    if not parts or not all(isinstance(part, str) and part for part in parts):
        raise ValueError("test_command must contain only non-empty strings")
    return parts


def format_test_command_argv(argv: list[str] | None) -> str:
    if not argv:
        return "(not configured)"
    return " ".join(argv)


def format_test_command_display(argv: list[str] | None) -> str:
    if not argv:
        return "(not configured)"
    return json.dumps(argv)
