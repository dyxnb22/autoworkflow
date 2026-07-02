# AGENTS.md

Canonical agent reference for cc-loop. **Package 0.4.0.**

## Docs

- [docs/INTEGRATION.md](docs/INTEGRATION.md) — external CLI/JSON contract
- [docs/EVOLUTION.md](docs/EVOLUTION.md) — v0.4-v0.9 roadmap and implementation guidance
- [docs/TASK_GRAPH.md](docs/TASK_GRAPH.md) — task graph orchestration (v0.4)
- [docs/RECOVERY.md](docs/RECOVERY.md) — failure classification, repair budgets, auto dispatch
- [docs/EXIT_CODES.md](docs/EXIT_CODES.md)
- [CLAUDE.md](CLAUDE.md) — Claude Code entry
- [.cursor/rules/cc-loop.mdc](.cursor/rules/cc-loop.mdc) — Cursor rules

## v0.4 task graph modules

| Module | Role |
|--------|------|
| `task_graph.py` | `TaskGraph`, `GraphNode`, dispatcher, planner JSON parsing |
| `run.py` | node-scoped prompts; sequential graph execution in `auto` |
| `inspect.py` | `task_graph` block in `status --json`; `format_task_graph_human` |

Planner prefers `mode: task_graph` JSON; legacy single-step JSON auto-wraps to one-node graph.

## v0.3 recovery modules

| Module | Role |
|--------|------|
| `failure.py` | `FailureType`, classifiers, `failure.report.json` |
| `recovery.py` | `decide_auto_step`, retry budgets |
| `repair_prompts.py` | implementer repair prompts |

`auto` uses `decide_auto_step` — not ad-hoc `needs_resume` / merge_error exits.

## Tests

```bash
python -m pytest tests/ -q
```

Graph tests: `test_task_graph.py`. Recovery tests: `test_failure_classification.py`, `test_recovery_dispatch.py`, `test_auto_recovery.py`
