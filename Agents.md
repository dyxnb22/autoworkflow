# AGENTS.md

Canonical agent reference for cc-loop. **Package 0.11.0.**

## Product positioning

cc-loop is a **role-separated delivery engine**: planner/reviewer vs implementer, hard test gate, reject→retry implement. Default success is handoff-ready on a branch (`auto_merge=false`). Not a general multi-agent framework.

## Docs

- [docs/INTEGRATION.md](docs/INTEGRATION.md) — external CLI/JSON contract
- [docs/EVOLUTION.md](docs/EVOLUTION.md) — v0.4-v0.10 roadmap and implementation guidance
- [docs/TASK_GRAPH.md](docs/TASK_GRAPH.md) — task graph orchestration (advanced)
- [docs/RECOVERY.md](docs/RECOVERY.md) — failure classification, repair budgets, auto dispatch
- [docs/EXIT_CODES.md](docs/EXIT_CODES.md)
- [CLAUDE.md](CLAUDE.md) — Claude Code entry
- [.cursor/rules/cc-loop.mdc](.cursor/rules/cc-loop.mdc) — Cursor rules

## v0.11 product gates

| Config | Default | Role |
|--------|---------|------|
| `require_distinct_reviewer` | `false` | When true, implementer/reviewer provider+model must differ |
| `auto_merge` | `false` | Opt-in merge; default success = `ready_for_handoff` |
| `planner_granularity` | `single` | Default single closed loop |
| `allow_merge_without_tests` | `false` | Explicit only |
| `test_command` | unset | Required for `cc-loop auto` |

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
| `summary.py` | Luma `summary --json` / `run.summary.json` |

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
| `parallel_scheduler.py` | Concurrent runnable node dispatch (advanced) |

## v0.4 task graph modules

| Module | Role |
|--------|------|
| `task_graph.py` | `TaskGraph`, `GraphNode`, dispatcher, planner JSON parsing |
| `run.py` | node-scoped prompts; sequential graph execution in `auto` |
| `inspect.py` | `task_graph` block in `status --json`; `format_task_graph_human` |

Planner prefers `mode: task_graph` JSON; legacy single-step JSON auto-wraps to one-node graph. Default planner granularity is `single`.

## v0.3 recovery modules

| Module | Role |
|--------|------|
| `failure.py` | `FailureType`, classifiers, `failure.report.json` |
| `recovery.py` | `decide_auto_step`, retry budgets |
| `repair_prompts.py` | implementer repair prompts |

`auto` uses `decide_auto_step` — not ad-hoc `needs_resume` / merge_error exits. Reviewer reject → `RESUME` implementer retry.

## Tests

```bash
python -m pytest tests/ -q
```

Graph tests: `test_task_graph.py`. Recovery tests: `test_failure_classification.py`, `test_recovery_dispatch.py`, `test_auto_recovery.py`. Provider/subprocess: `test_subprocess_util.py`, `test_provider_failure_paths.py`. Product gates: `test_product_sharpening.py`, summary contract in `test_summary.py` / `test_cli_contract.py`.
