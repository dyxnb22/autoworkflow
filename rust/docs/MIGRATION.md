# Rust rewrite migration (v0.12) — complete

## Status: Rust is the default implementation

| Area | Status |
|------|--------|
| CLI surface (all INTEGRATION commands) | Done |
| `status --json` flat fields (`attempt`, `running`, `runner_*`, `can_*`, …) | Done |
| `list --json` as array | Done |
| `graph --json` `{task_graph:…}` wrapper | Done |
| `summary --json` + observability paths | Done |
| Config/state/git/locks/legacy load | Done |
| Providers argv parity (codex stdin, cursor agent, claude --print/--model) | Done |
| Single-loop + reject/repair + handoff | Done |
| auto_direct planner, review_context, budgets | Done |
| Events / failure.report / trace / timeline / prompt_cache (real health scoring) | Done |
| Sequential graph + merge_queue + replan | Done |
| True concurrent parallel node execution (`allow_parallel_execution` + `max_parallel_nodes>1`) | Done |
| Detach / stop / cancel / cleanup | Done |
| Eval / export | Done |
| Contract tests | Done |
| Default entry | Rust binary (`scripts/cc-loop`, `make install-rust-bin`, or Python CLI delegates to Rust) |

## Build / test / install

```bash
make rust-test
make rust-clippy
make rust-release
make install-rust-bin   # ~/.local/bin/cc-loop
# or:
./scripts/cc-loop --version
```

Python `cc-loop` / `python -m cc_loop.cli` execs the Rust binary when found.
Force the Python implementation with `CC_LOOP_FORCE_PYTHON=1`.
Override binary path with `CC_LOOP_BIN=/path/to/cc-loop`.

## Fake offline loop

```bash
export CC_LOOP_FAKE_PROVIDERS=1
./scripts/cc-loop --state-root /tmp/s init --goal "fix typo" --repo "$REPO" \
  --planner fake --implementer fake --reviewer fake --allow-same-reviewer \
  --test-command true --task-id t1
./scripts/cc-loop --state-root /tmp/s auto --task-id t1
./scripts/cc-loop --state-root /tmp/s status --task-id t1 --json
./scripts/cc-loop --state-root /tmp/s summary --task-id t1 --json
```

## Invariants

- `shell=false`, process-group kill, no merge on failed tests, handoff default, distinct reviewer default, dirty repo blocks run.
- Parallel workers commit via locked merge updates (no last-writer-wins on `history`).
