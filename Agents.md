# AGENTS.md

Canonical agent reference for cc-loop. **Package 0.12.0 (Rust).**

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
- [rust/docs/MIGRATION.md](rust/docs/MIGRATION.md) — Rust rewrite status
- [CLAUDE.md](CLAUDE.md) — Claude Code entry
- [.cursor/rules/cc-loop.mdc](.cursor/rules/cc-loop.mdc) — Cursor rules
- [CHANGELOG.md](CHANGELOG.md)

## Product gates

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

## Key modules (`rust/crates/cc-loop-core`)

| Module | Role |
|--------|------|
| `orchestrator` | phase loop; handoff finalize; parallel dispatch |
| `config` | defaults and distinct-reviewer helpers |
| `preflight` | dirty-repo / provider / distinct-reviewer gates |
| `recovery` | `decide_auto_step` (reject→resume, repair, handoff done) |
| `inspect` | `status --json` including roles/success |
| `summary` | Luma `summary --json` / `run.summary.json` |
| `state` | persistence; old state without `task_graph` still loads |
| `graph` | advanced multi-node graphs |
| `parallel` | concurrent ready-node execution + merge queue |
| `failure` / `repair` | classification and repair prompts |
| `prompt_cache` | stable/dynamic prefix health scoring |
| `provider/*` | codex, cursor, claude_code, fake |

CLI binary: `rust/crates/cc-loop-cli`.

## Invariants

- `shell=false`; never `pkill -f`; process-group timeout cleanup
- Dirty repo blocks run; never switch user’s main checkout
- No success on failed/skipped tests unless `allow_merge_without_tests`
- Default success is handoff; merge is opt-in
- `require_distinct_reviewer=true` by default
- Breaking integration surface → update `docs/INTEGRATION.md`
- Legacy state without `task_graph` must still load

## Tests

```bash
make test
# or: cd rust && cargo test --workspace
```

Contract: `rust/crates/cc-loop-contract-tests`. Clippy: `make clippy`.
