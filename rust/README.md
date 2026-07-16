# cc-loop (Rust)

Role-separated delivery engine — package **0.12.0**.

**Integration schema:** 1 (see [docs/INTEGRATION.md](../../docs/INTEGRATION.md))

## Build

```bash
cd rust
cargo build --release
./target/release/cc-loop --version
```

From repo root: `make test` · `make release` · `make install`

## Product defaults

- `auto_merge=false` → success = `ready_for_handoff`
- `require_distinct_reviewer=true`
- `planner_granularity=single`
- Tests gate progress; writer cannot review their own work

## Layout

| Crate | Role |
|-------|------|
| `cc-loop-cli` | `cc-loop` binary |
| `cc-loop-core` | config, state, git, providers, orchestrator, inspect/summary |
| `cc-loop-contract-tests` | black-box CLI contract tests |

## Tests

```bash
cd rust
cargo test --workspace
```
