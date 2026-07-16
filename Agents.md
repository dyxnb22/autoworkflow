# AGENTS.md

Canonical agent reference for cc-loop. **Package 0.11.0.**

## Product positioning

cc-loop is a **role-separated delivery engine**: planner/reviewer vs implementer, hard test gate, reject→retry implement. Default success is handoff-ready on a branch (`auto_merge=false`). Not a general multi-agent framework.

Default loop:

```text
goal → plan → implement → test → review → (approve: handoff | reject/fail: retry implement)
```

## Docs

- [docs/INTEGRATION.md](docs/INTEGRATION.md) — external CLI/JSON contract (source of truth for integrators)
- [docs/RECOVERY.md](docs/RECOVERY.md) — failure classification, repair budgets, reject→retry
- [docs/EXIT_CODES.md](docs/EXIT_CODES.md)
- [docs/TASK_GRAPH.md](docs/TASK_GRAPH.md) — task graphs (**advanced**)
- [docs/EVOLUTION.md](docs/EVOLUTION.md) — historical v0.4–v0.10 roadmap (superseded for product positioning by README / this file)
- [CLAUDE.md](CLAUDE.md) — Claude Code entry
- [.cursor/rules/cc-loop.mdc](.cursor/rules/cc-loop.mdc) — Cursor rules
- [CHANGELOG.md](CHANGELOG.md)

## v0.11 product gates

| Config | Default | Role |
|--------|---------|------|
| `require_distinct_reviewer` | `true` | Writer/reviewer provider+model must differ; escape `--allow-same-reviewer` |
| `auto_merge` | `false` | Opt-in merge; default success = `ready_for_handoff` |
| `planner_granularity` | `single` | Default single closed loop |
| `allow_merge_without_tests` | `false` | Explicit only |
| `test_command` | unset | Required for `cc-loop auto` |
| `allow_parallel_execution` | `false` | Advanced |

## Commands (day-to-day)

`init` · `doctor` · `list` · `run` · `resume` · `auto` · `status` · `summary`

Advanced / operational: `graph` · `report` · `stop` · `cancel` · `cleanup` · `eval` · `export`

## Key modules

| Module | Role |
|--------|------|
| `cli.py` | argparse, command handlers |
| `config.py` | defaults and distinct-reviewer helpers |
| `preflight.py` | dirty-repo / provider / distinct-reviewer gates |
| `run.py` | phase loop; handoff finalize; planner/reviewer `print_only` for claude-code |
| `recovery.py` | `decide_auto_step` (reject→resume, repair, handoff done) |
| `inspect.py` | `status --json` including roles/success |
| `summary.py` | Luma `summary --json` / `run.summary.json` |
| `state.py` | persistence; old state without `task_graph` still loads |
| `task_graph.py` | advanced multi-node graphs |
| `failure.py` / `repair_prompts.py` | classification and repair prompts |
| `providers/*.py` | codex, cursor, claude_code |

## Invariants

- `shell=False`; never `pkill -f`; process-group timeout cleanup
- Dirty repo blocks run; never switch user’s main checkout
- No success on failed/skipped tests unless `allow_merge_without_tests`
- Default success is handoff; merge is opt-in
- `require_distinct_reviewer=true` by default
- Breaking integration surface → update `docs/INTEGRATION.md`
- Legacy state without `task_graph` must still load

## Tests

```bash
python -m pytest tests/ -q
```

Product gates: `tests/test_product_sharpening.py`. Contract: `tests/test_cli_contract.py`. Loop: `tests/test_run_flow.py`. Recovery: `tests/test_recovery_dispatch.py`, `tests/test_auto_recovery.py`.
