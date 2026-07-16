# Rust rewrite migration (v0.12) — completion checklist

## Status: feature-complete for replacement dual-run

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
| Events / failure.report / trace / timeline / prompt_cache | Done |
| Sequential graph + merge_queue helpers + replan step | Done |
| Parallel ready-set scheduling (sequential execute) | Done |
| Detach / stop / cancel / cleanup | Done |
| Eval / export | Done |
| Contract tests | Done |
| Install path | `make install-rust-bin` or `scripts/cc-loop` |

## Build / test / install

```bash
make rust-test
make rust-clippy
make rust-release
make install-rust-bin   # ~/.local/bin/cc-loop
# or:
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
./scripts/cc-loop --state-root /tmp/s summary --task-id t1 --json
```

## Remaining intentional differences vs Python

- True concurrent parallel node execution is scheduled but still run sequentially in-process (same outcome for default `max_parallel_nodes=1`).
- Prompt-cache *health scoring* is stubbed (metrics files written; ratios are placeholders).
- Python package remains installable for reference; prefer Rust binary for new integrations.

## Invariants

- `shell=false`, process-group kill, no merge on failed tests, handoff default, distinct reviewer default, dirty repo blocks run.
