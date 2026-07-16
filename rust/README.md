# cc-loop (Rust workspace)

Package **0.12.0** · Integration schema **1** · [docs/INTEGRATION.md](../docs/INTEGRATION.md)

```bash
cd rust && cargo test --workspace && cargo build --release
# from repo root:
make test && make install
```

| Crate | Role |
|-------|------|
| `cc-loop-cli` | `cc-loop` binary |
| `cc-loop-core` | Engine (state, providers, orchestrator, inspect/summary) |
| `cc-loop-contract-tests` | Black-box CLI contracts |

Defaults: `auto_merge=false`, `require_distinct_reviewer=true`, `planner_granularity=single`.
