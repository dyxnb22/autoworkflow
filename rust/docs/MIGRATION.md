# Rust rewrite migration (v0.12)

## Status

| Phase | Scope | Status |
|-------|-------|--------|
| A | Workspace, CLI surface, contract fixtures | Done |
| B | Config / state / git / locks | Done |
| C | Providers (codex, cursor, claude-code, fake) | Done |
| D | Single-loop orchestrator + handoff defaults | Done |
| E | `status` / `summary` Luma JSON | Done |
| F | Contract tests + dual-run docs | Done |
| G | Graph sequential, recovery steps, eval/export, detach | Done |

## Dual-run

- **Rust binary:** `rust/target/release/cc-loop` (package version `0.12.0`)
- **Python package:** `src/cc_loop/` remains the reference implementation (`0.11.0`) during dual-run
- **Integration schema:** still `1` — Luma should keep using `docs/INTEGRATION.md`

## Build / test

```bash
cd rust
cargo build --release
cargo test --workspace
```

## Fake providers (offline loop)

```bash
export CC_LOOP_FAKE_PROVIDERS=1
cc-loop --state-root /tmp/s init ... --planner fake --implementer fake --reviewer fake --allow-same-reviewer --test-command true
cc-loop --state-root /tmp/s auto --task-id ...
```

When `CC_LOOP_FAKE_PROVIDERS=1`, all provider names resolve to the offline `fake` adapter.

## Invariants preserved

- `shell=false` (argv only)
- process-group kill on timeout (`killpg`)
- no merge on failed/skipped tests unless `allow_merge_without_tests`
- default `auto_merge=false` → `success=ready_for_handoff`
- `require_distinct_reviewer=true` by default
- dirty repo blocks run
- legacy `state.json` without `task_graph` / missing config keys still loads
