# CLAUDE.md — cc-loop

**Package:** 0.11.0 · Read [AGENTS.md](AGENTS.md) · Contract: [docs/INTEGRATION.md](docs/INTEGRATION.md) · Recovery: [docs/RECOVERY.md](docs/RECOVERY.md)

## What you are working on

Role-separated delivery engine: planner/reviewer vs implementer → tests → review → handoff (merge is opt-in). Default path is a single closed loop; task graphs / parallel are advanced. You are editing **cc-loop itself**, not running as its `claude-code` provider unless explicitly testing providers.

## Quick start

```bash
pip install -e .
python -m pytest tests/ -q
```

Use `tests/helpers.TempEnv` and `tests/fake_providers` for integration tests. Real git repos in temp dirs — do not mock git.

## Commands

Global flags **before** subcommand: `cc-loop --state-root PATH <cmd> ...`

Day-to-day: `init` · `doctor` · `list` · `run` · `resume` · `auto` · `status` · `summary`

Advanced / ops: `graph` · `report` · `stop` · `cancel` · `cleanup`

Operational commands accept `--task-id`. `status` / `list` / `doctor` / `summary` / `graph` support `--json`. `auto --detach` writes `runner.pid` + `runner.log`.

`CC_LOOP_STATE_ROOT` mirrors `--state-root` when the flag is omitted.

## Key modules

| Module | Role |
|--------|------|
| `cli.py` | argparse, `resolve_task_id`, command handlers |
| `config.py` | defaults (`auto_merge=false`, `require_distinct_reviewer=true`, `planner_granularity=single`) |
| `preflight.py` | dirty-repo, providers, distinct-reviewer |
| `run.py` | phase loop; handoff finalize; claude-code planner/reviewer use `print_only=True` |
| `recovery.py` | `decide_auto_step` |
| `inspect.py` / `summary.py` | `status --json` / `summary --json` for Luma |
| `state.py` | persistence; legacy state without `task_graph` still loads |
| `task_graph.py` | advanced multi-node graphs |
| `providers/*.py` | codex, cursor, claude_code adapters |

## claude-code provider (when cc-loop calls Claude)

```bash
# planner / reviewer (no worktree edits):
claude --dangerously-skip-permissions --print [-m MODEL] -p "<prompt>"

# implementer (cwd = worktree):
claude --dangerously-skip-permissions [-m MODEL] -p "<prompt>"
```

cc-loop must pass `print_only=True` for planner/reviewer in `run.py`.

Planner defaults to a **single closed loop** (`planner_granularity=single`). Multi-node `mode: task_graph` is advanced; legacy single-step JSON still works.

## Invariants

- `shell=False` always · no `pkill -f` · bounded review patches · dirty repo blocks run
- No success on failed/skipped tests unless `allow_merge_without_tests`
- Default success is handoff (`auto_merge=false`); merge is opt-in
- `require_distinct_reviewer=true` by default (escape: `--allow-same-reviewer`)
- `auto` requires `test_command` unless `allow_merge_without_tests`
- Never switch user's main branch checkout
- Breaking integration surface → update `docs/INTEGRATION.md`
- Old state files without `task_graph` must still load

## Tests to run

```bash
python -m pytest tests/ -q
```

Product gates: `tests/test_product_sharpening.py`. Contract: `tests/test_cli_contract.py`. Full loop: `tests/test_run_flow.py`.
