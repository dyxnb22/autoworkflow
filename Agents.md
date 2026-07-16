# AGENTS.md

cc-loop **0.12.0** (Rust) — role-separated delivery engine.

```text
goal → plan → implement → test → review → (approve: handoff | reject/fail: retry)
```

## Docs

| Doc | Role |
|-----|------|
| [README.md](README.md) | Product + install |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | External CLI/JSON contract |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Recovery, debugging, task graphs |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |
| [rust/README.md](rust/README.md) | Crate layout |

## Gates

| Config | Default |
|--------|---------|
| `require_distinct_reviewer` | `true` |
| `auto_merge` | `false` |
| `planner_granularity` | `single` |
| `test_command` (for `auto`) | required |
| `allow_parallel_execution` | `false` |

## Code map (`rust/crates/cc-loop-core`)

`orchestrator` · `state` · `graph` · `parallel` · `inspect` · `summary` · `recovery` / `repair` · `prompt_cache` · `provider/*`

Binary: `cc-loop-cli`.

## Invariants

`shell=false` · no `pkill -f` · process-group kill · dirty repo blocks run · never switch user main checkout · no success on failed tests unless escaped · handoff default · distinct reviewer default · legacy state without `task_graph` still loads · breaking CLI/JSON → update INTEGRATION.md

## Tests

```bash
make test && make clippy
```
