# cc-loop

**Role-separated delivery engine** (Rust 0.12): the writer cannot review their own work, and nothing advances past a failing or missing test gate.

```text
goal → plan → implement → test → review
         ↑__________ reject / fail retry _________|
                   approve → ready for handoff
```

Merge into the base branch is **opt-in** (`--auto-merge`). Default success is green tests + review approve on an attempt branch (`ready_for_handoff`).

## Install

```bash
make test && make release
make install                 # ~/.local/bin/cc-loop
./scripts/cc-loop --version
```

Implementation: [`rust/`](rust/). External contract: [`docs/INTEGRATION.md`](docs/INTEGRATION.md). Ops guide: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Defaults

| Setting | Default |
|---------|---------|
| `auto_merge` | `false` (handoff) |
| `require_distinct_reviewer` | `true` |
| `planner_granularity` | `single` |
| `test_command` for `auto` | required |
| Providers | planner/reviewer `codex`, implementer `cursor` |

## Quick start

```bash
cc-loop init \
  --goal "Fix the failing CLI flag" \
  --repo /path/to/repo \
  --task-id my-task \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q

cc-loop auto --detach --task-id my-task
cc-loop status --task-id my-task --json
cc-loop summary --task-id my-task --json
```

Global flags (`--state-root`) must precede the subcommand. `CC_LOOP_STATE_ROOT` sets the default state root.

Offline fake loop: `CC_LOOP_FAKE_PROVIDERS=1` with providers `fake`.

## Commands

Day-to-day: `init` · `doctor` · `list` · `run` · `resume` · `auto` · `status` · `summary`

Ops / advanced: `graph` · `report` · `stop` · `cancel` · `cleanup` · `eval` · `export`

## Providers

| Provider | Roles | Notes |
|----------|-------|-------|
| `codex` | planner, reviewer | `codex exec` |
| `cursor` | implementer | `cursor agent` in worktree |
| `claude-code` | all | `--print` for plan/review |
| `fake` | all | offline contract testing |

## Non-goals

No cloud coordinator, no general multi-agent framework, no default merge into the user’s main checkout, no edits in a dirty main worktree, no `pkill -f`.
