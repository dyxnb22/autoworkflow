# CLAUDE.md — cc-loop

**Package:** 0.12.0 (Rust) · Read [AGENTS.md](AGENTS.md) · Contract: [docs/INTEGRATION.md](docs/INTEGRATION.md) · Recovery: [docs/RECOVERY.md](docs/RECOVERY.md)

## What you are working on

Role-separated delivery engine: planner/reviewer vs implementer → tests → review → handoff (merge is opt-in). Default path is a single closed loop; task graphs / parallel are advanced. You are editing **cc-loop itself** (Rust under `rust/`), not running as its `claude-code` provider unless explicitly testing providers.

## Quick start

```bash
make test
make release
make install   # ~/.local/bin/cc-loop
./scripts/cc-loop --version
```

Contract tests live in `rust/crates/cc-loop-contract-tests`. Real git repos in temp dirs — do not mock git.

## Commands

Global flags **before** subcommand: `cc-loop --state-root PATH <cmd> ...`

Day-to-day: `init` · `doctor` · `list` · `run` · `resume` · `auto` · `status` · `summary`

Advanced / ops: `graph` · `report` · `stop` · `cancel` · `cleanup` · `eval` · `export`

Operational commands accept `--task-id`. `status` / `list` / `doctor` / `summary` / `graph` support `--json`. `auto --detach` writes `runner.pid` + `runner.log`.

`CC_LOOP_STATE_ROOT` mirrors `--state-root` when the flag is omitted.

## Key modules (`rust/crates/cc-loop-core/src`)

| Module | Role |
|--------|------|
| `orchestrator.rs` | phase orchestration; node prompts; parallel batch |
| `state.rs` | `TaskState`, `task_graph`, persistence, locks |
| `graph.rs` | graph models / ready-set |
| `parallel.rs` | concurrent jobs + merge queue |
| `inspect.rs` | `status --json`, runner liveness |
| `summary.rs` | Luma summary contract |
| `recovery.rs` / `repair.rs` | auto recovery and repair prompts |
| `prompt_cache.rs` | cache health scoring |
| `provider/` | codex, cursor, claude_code, fake |

## claude-code provider (when cc-loop calls Claude)

```bash
# planner / reviewer (no worktree edits):
claude --dangerously-skip-permissions --print [-m MODEL] -p "<prompt>"

# implementer (cwd = worktree):
claude --dangerously-skip-permissions [-m MODEL] -p "<prompt>"
```

Orchestrator must pass `print_only=true` for planner/reviewer.

Planner should prefer task graph JSON (`mode: task_graph`); legacy single-step JSON still works.

## Invariants

- `shell=false` always · no `pkill -f` · bounded review patches · dirty repo blocks run
- No merge on failed/skipped tests unless `allow_merge_without_tests`
- Never switch user's main branch checkout
- Breaking integration surface → update `docs/INTEGRATION.md`
- Old state files without `task_graph` must still load

## Tests to run

```bash
make test
make clippy
```
