"""Planner granularity heuristics for narrow vs multi-node planning."""

from __future__ import annotations

from cc_loop.config import LoopConfig

_FOCUS_KEYWORDS = (
    "focused fix",
    "small cli",
    "cli wiring",
    "non-goals",
    " only ",
    "only fix",
    "narrow task",
    "narrow fix",
    "minimal change",
    "single file",
    "wiring fix",
    "do not refactor",
)


def resolve_planner_granularity(goal: str, config: LoopConfig) -> str:
    setting = str(config.get("planner_granularity", "auto") or "auto").lower()
    if setting in {"single", "graph"}:
        return setting
    lower = goal.lower()
    if any(keyword in lower for keyword in _FOCUS_KEYWORDS):
        return "single"
    return "auto"


def planner_granularity_prompt_section(granularity: str) -> str:
    if granularity == "single":
        return (
            "\nPlanner granularity: SINGLE NODE.\n"
            "Prefer legacy single-step JSON or a task_graph with exactly one implementation node.\n"
            "Do not split narrow CLI wiring or focused fixes into multiple nodes.\n"
            "Do not plan work that duplicates completed steps or existing repository functionality.\n"
        )
    if granularity == "graph":
        return (
            "\nPlanner granularity: MULTI-NODE GRAPH.\n"
            "Decompose the goal into dependent nodes when that reduces risk or clarifies ownership.\n"
        )
    return (
        "\nPlanner granularity: AUTO.\n"
        "Use a single node for focused fixes, CLI wiring, or explicitly scoped goals.\n"
        "Use multiple nodes only when the goal clearly spans independent workstreams.\n"
        "Avoid duplicating work already completed in prior nodes or existing repo features.\n"
    )
