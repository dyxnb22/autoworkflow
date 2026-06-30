# CLAUDE.md — cc-loop

**Package:** 0.3.0 · Read [AGENTS.md](AGENTS.md) · Recovery: [docs/RECOVERY.md](docs/RECOVERY.md)

## What you are working on

Local CLI orchestrator: planner → worktree → implementer → tests → reviewer → merge/retry/stop. You are editing **cc-loop itself**, not running as its `claude-code` provider unless explicitly testing providers.

## Quick start

```bash
pip install -e .
python -m pytest tests/ -q
```

Use `tests/helpers.TempEnv` and `tests/fake_providers` for integration tests. Real git repos in temp dirs — do not mock git.

## Commands (v0.2.0)

Global flags **before** subcommand: `cc-loop --state-root PATH <cmd> ...`

`init` · `doctor` · `list` · `run` · `resume` · `auto` · `status`

Operational commands accept `--task-id`. `status` / `list` / `doctor` support `--json`. `auto --detach` writes `runner.pid` + `runner.log`.

`CC_LOOP_STATE_ROOT` mirrors `--state-root` when the flag is omitted.

External integration contract: [docs/INTEGRATION.md](docs/INTEGRATION.md)

## Key modules (touch these when changing behavior)

| Module | Role |
|--------|------|
| `cli.py` | argparse, `resolve_task_id`, command handlers |
| `run.py` | phase orchestration; claude-code uses `print_only=True` for planner/reviewer |
| `state.py` | `TaskState`, `schema_version`, persistence |
| `inspect.py` | `status --json`, `next_action`, runner liveness |
| `list_tasks.py` / `detach.py` | `list`, `auto --detach` |
| `preflight.py` | `run_preflight`, `run_doctor_preflight` |
| `providers/*.py` | codex, cursor, claude_code adapters |

## claude-code provider (when cc-loop calls Claude)

```bash
# planner / reviewer (no worktree edits):
claude --dangerously-skip-permissions --print [-m MODEL] -p "<prompt>"

# implementer (cwd = worktree):
claude --dangerously-skip-permissions [-m MODEL] -p "<prompt>"
```

Orchestrator must pass `print_only=True` for planner/reviewer in `run.py`.

## Invariants

- `shell=False` always · no `pkill -f` · bounded review patches · dirty repo blocks run
- No merge on failed/skipped tests unless `allow_merge_without_tests`
- Never switch user's main branch checkout
- Breaking integration surface → update `docs/INTEGRATION.md` and bump `schema_version` if needed

## Tests to run

```bash
python -m pytest tests/ -q
```

Contract coverage: `tests/test_cli_contract.py`. Full loop: `tests/test_run_flow.py`.
