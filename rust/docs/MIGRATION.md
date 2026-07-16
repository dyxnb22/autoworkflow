# Rust rewrite migration (v0.12) — complete

## Status: Rust is the sole implementation

The Python package (`src/cc_loop/`, `pyproject.toml`, pytest suite) has been removed. All runtime behavior lives under `rust/`.

| Area | Status |
|------|--------|
| CLI surface (all INTEGRATION commands) | Done |
| `status --json` flat fields | Done |
| `list --json` as array | Done |
| `graph --json` `{task_graph:…}` wrapper | Done |
| `summary --json` + observability paths | Done |
| Config/state/git/locks/legacy load | Done |
| Providers (codex, cursor, claude, fake) | Done |
| Single-loop + reject/repair + handoff | Done |
| Prompt-cache health scoring | Done |
| Concurrent parallel nodes (opt-in) | Done |
| Detach / stop / cancel / cleanup | Done |
| Eval / export | Done |
| Contract tests | `cargo test` in `cc-loop-contract-tests` |

## Build / test / install

```bash
make test
make clippy
make release
make install   # ~/.local/bin/cc-loop
./scripts/cc-loop --version
```

## Fake offline loop

```bash
export CC_LOOP_FAKE_PROVIDERS=1
./scripts/cc-loop --state-root /tmp/s init --goal "fix typo" --repo "$REPO" \
  --planner fake --implementer fake --reviewer fake --allow-same-reviewer \
  --test-command true --task-id t1
./scripts/cc-loop --state-root /tmp/s auto --task-id t1
./scripts/cc-loop --state-root /tmp/s status --task-id t1 --json
```

## Invariants

- `shell=false`, process-group kill, no merge on failed tests, handoff default, distinct reviewer default, dirty repo blocks run.
- Parallel workers commit via locked merge updates (no last-writer-wins on `history`).
