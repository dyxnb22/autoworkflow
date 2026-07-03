"""Parse and normalize ``--test-command`` argv from the CLI."""

from __future__ import annotations

import json
import re
import shlex
import sys

TEST_COMMAND_HINT = (
    "Use --test-command -- pytest tests -q "
    "(place `--` before the command so flags like -q are not parsed as cc-loop options; "
    "put cc-loop flags before --test-command)"
)

SHELL_OPERATOR_RE = re.compile(r"(&&|\|\||[|;&<>])")

INIT_SUBCOMMAND_FLAGS = {
    "--goal",
    "--goal-file",
    "--repo",
    "--task-id",
    "--base-branch",
    "--test-command",
    "--planner-granularity",
    "--planner",
    "--reviewer",
    "--implementer",
    "--allow-merge-without-tests",
    "--max-iterations",
    "--max-retries",
    "--codex-model",
    "--cursor-model",
    "--claude-code-model",
    "--cursor-force",
    "--cursor-sandbox",
    "--max-parallel-nodes",
    "--allow-parallel-execution",
    "--max-wall-clock-seconds",
    "--max-changed-files-per-attempt",
    "--max-consecutive-failures",
    "--max-artifact-log-bytes",
    "--allow-node-policy-weakening",
}

DOCTOR_SUBCOMMAND_FLAGS = {
    "--repo",
    "--base-branch",
    "--planner",
    "--reviewer",
    "--implementer",
    "--test-command",
    "--json",
}

GLOBAL_FLAGS = {"--state-root", "--version"}

SUBCOMMANDS_WITH_TEST_COMMAND: set[str] = {"init", "doctor"}
_EXTRA_TEST_COMMAND_FLAGS: dict[str, set[str]] = {}


def register_test_command_subcommand(name: str, flags: set[str]) -> None:
    """Register another CLI subcommand that accepts ``--test-command``."""
    SUBCOMMANDS_WITH_TEST_COMMAND.add(name)
    _EXTRA_TEST_COMMAND_FLAGS[name] = set(flags)


def _find_subcommand(argv: list[str]) -> str | None:
    for token in argv:
        if token in SUBCOMMANDS_WITH_TEST_COMMAND:
            return token
    return None


def _is_cc_loop_flag(token: str, subcommand: str | None) -> bool:
    if token in GLOBAL_FLAGS:
        return True
    if subcommand == "init" and token in INIT_SUBCOMMAND_FLAGS:
        return True
    if subcommand == "doctor" and token in DOCTOR_SUBCOMMAND_FLAGS:
        return True
    if subcommand is not None and token in _EXTRA_TEST_COMMAND_FLAGS.get(subcommand, set()):
        return True
    return False


def _reject_shell_operators(text: str) -> None:
    if SHELL_OPERATOR_RE.search(text):
        raise ValueError(
            "test_command contains shell operators; pass argv tokens after "
            f"`--test-command --` instead of a shell pipeline. {TEST_COMMAND_HINT}"
        )


def _warn_misplaced_cc_loop_flags(tokens: list[str], subcommand: str) -> None:
    for token in tokens:
        if _is_cc_loop_flag(token, subcommand):
            print(
                f"warning: {token} appears after --test-command -- and will be passed to the "
                "test command; put cc-loop flags before --test-command",
                file=sys.stderr,
            )


def expand_test_command_in_argv(argv: list[str]) -> list[str]:
    """Normalize ``--test-command`` sections before argparse parses subcommands.

    With an explicit ``--test-command --`` separator, every remaining token belongs to
    the test command. Put cc-loop flags before ``--test-command`` in that form.
    Legacy unseparated argv keeps the previous behavior and stops at known cc-loop
    flags.
    """
    subcommand = _find_subcommand(argv)
    if subcommand is None:
        return list(argv)

    result: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token != "--test-command":
            result.append(token)
            i += 1
            continue

        result.append(token)
        i += 1
        if i >= len(argv):
            break

        parts: list[str] = []
        if argv[i] == "--":
            i += 1
            tail = argv[i:]
            _warn_misplaced_cc_loop_flags(tail, subcommand)
            parts.extend(tail)
            i = len(argv)
        while i < len(argv) and not _is_cc_loop_flag(argv[i], subcommand):
            parts.append(argv[i])
            i += 1
        if parts:
            result.append(" ".join(parts))
    return result


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
            _reject_shell_operators(token)
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
