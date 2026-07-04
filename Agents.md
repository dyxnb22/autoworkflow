# AGENTS.md

Canonical agent reference for cc-loop. **Package 0.10.0.**

## Docs

- [docs/INTEGRATION.md](docs/INTEGRATION.md) — external CLI/JSON contract
- [docs/EVOLUTION.md](docs/EVOLUTION.md) — v0.4-v0.10 roadmap and implementation guidance
- [docs/TASK_GRAPH.md](docs/TASK_GRAPH.md) — task graph orchestration (v0.4)
- [docs/RECOVERY.md](docs/RECOVERY.md) — failure classification, repair budgets, auto dispatch
- [docs/EXIT_CODES.md](docs/EXIT_CODES.md)
- [CLAUDE.md](CLAUDE.md) — Claude Code entry
- [.cursor/rules/cc-loop.mdc](.cursor/rules/cc-loop.mdc) — Cursor rules

## v0.10 modules

| Module | Role |
|--------|------|
| `prompt_cache.py` | Stable-prefix prompt caching for planner/reviewer/implementer |
| `prompt_metadata.py` | Prompt layout metadata and cache-health artifacts |
| `trace.py` | Provider phase trace records for status/report |
| `execution_timeline.py` | Attempt timeline from artifacts and subprocess results |
| `export.py` | Task export for offline inspection |
| `evals.py` | Eval case runner and result aggregation |
| `provider_runtime.py` | Provider subprocess wrapper with heartbeat + watchdog |
| `subprocess_util.py` | Timeout-safe subprocess execution and process-group cleanup |

## v0.5–v0.9 modules

| Module | Role |
|--------|------|
| `runner_heartbeat.py` | Detached runner heartbeat read/write/staleness |
| `runner_control.py` | `stop`, `cancel`, `cleanup`; cross-platform PID ownership |
| `events.py` | Append-only `events.jsonl` / `graph_events.jsonl` |
| `report.py` | `cc-loop report` human + JSON |
| `graph_patch.py` | Dynamic replanning patch model + validation |
| `budgets.py` | Wall-clock and failure budgets |
| `state_lock.py` | File lock + atomic state writes |
| `merge_queue.py` | Serial merge queue for parallel nodes |
| `parallel_scheduler.py` | Concurrent runnable node dispatch |

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

Graph tests: `test_task_graph.py`. Recovery tests: `test_failure_classification.py`, `test_recovery_dispatch.py`, `test_auto_recovery.py`. Provider/subprocess: `test_subprocess_util.py`, `test_provider_failure_paths.py`.
