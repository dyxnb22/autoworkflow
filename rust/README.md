# cc-loop (Rust)

Role-separated delivery engine — Rust rewrite of the Python `cc-loop` package.

**Version:** 0.12.0  
**Integration schema:** 1 (compatible with [docs/INTEGRATION.md](../docs/INTEGRATION.md))

## Build

```bash
cd rust
cargo build --release
./target/release/cc-loop --version
```

## Product defaults (unchanged from Python 0.11)

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
cargo test
```

Python package under `src/cc_loop/` remains available as a fallback (`CC_LOOP_FORCE_PYTHON=1`). The default entry prefers the Rust binary (`scripts/cc-loop`, `make install-rust-bin`, or the Python console script when a Rust build is present).
