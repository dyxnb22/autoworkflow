# cc-loop

**cc-loop is a role-separated delivery engine:** the person who writes the code cannot be the one who reviews it, and nothing advances past a failing (or missing) test gate.

You give a goal. One role plans and reviews; another role implements. The default loop is intentionally narrow:

```text
goal → plan → implement → test → review
         ↑________________reject/fail retry_______|
                   approve → ready for handoff (branch/worktree)
```

Merge into your base branch is **opt-in** (`--auto-merge`). Success means tests green + review approve + changes sitting on an attempt branch you can hand off.

This repo is the workflow tooling itself (Rust). It is not a general multi-agent framework and does not compete with Cursor/Claude built-in subagents on “who splits tasks better.”

## Positioning

- Fixed roles: `planner` / `reviewer` and `implementer` (writer ≠ reviewer).
- Providers are swappable (`codex`, `cursor`, `claude-code`), but the loop and gates belong to cc-loop.
- Recommended: distinct implementer/reviewer (`require_distinct_reviewer=true` by default; escape with `--allow-same-reviewer`).
- Required for `auto`: a real `--test-command`. Skipping tests is never the default path.

Default providers (still changeable):

- `planner = codex`
- `reviewer = codex`
- `implementer = cursor`

## Default path (simple closed loop)

1. Planner produces a **single-node** plan by default (`planner_granularity=single`).
2. Implementer edits in an isolated git worktree.
3. Configured tests run; fail → repair/retry, never “success.”
4. Reviewer approves or rejects; reject → resume implementer with the rejection reason.
5. On approve + green tests: **ready for handoff** on the attempt branch (`auto_merge=false` by default).

## Install / build (v0.12 Rust)

```bash
make test && make release
make install                 # ~/.local/bin/cc-loop
# or without install:
./scripts/cc-loop --version
```

Implementation lives under [`rust/`](rust/). Integration schema stays **1** ([INTEGRATION.md](docs/INTEGRATION.md)). See [rust/docs/MIGRATION.md](rust/docs/MIGRATION.md).

### Advanced (explicit)

Multi-node task graphs, parallel nodes (`allow_parallel_execution`), and auto-merge into the base branch remain available but are not the default story. See [TASK_GRAPH.md](docs/TASK_GRAPH.md).

## Current status

Status: **v0.12 Rust** (package 0.12.0) — role-separated delivery engine with v0.11 product gates.

v1 core loop + integration contract + recovery + task graphs + runner control + events/reports + replanning + multi-role routing + parallel (advanced) + observability + distinct-reviewer gate, hard auto test gate, handoff-default success, Luma summary contract.

References:

- [Integration contract](docs/INTEGRATION.md)
- [Task graphs](docs/TASK_GRAPH.md) (advanced)
- [Recovery](docs/RECOVERY.md)
- [Exit codes](docs/EXIT_CODES.md)
- [Changelog](CHANGELOG.md)

## Command shape

```bash
# Initialize — distinct writer/reviewer is default; require a test command for auto
cc-loop init \
  --goal "Fix the failing CLI flag" \
  --repo /path/to/repo \
  --task-id my-task \
  --planner claude-code \
  --reviewer claude-code \
  --implementer cursor \
  --test-command -- cargo test -q

cc-loop doctor --repo /path/to/repo \
  --test-command -- cargo test -q
cc-loop auto --detach --task-id my-task
cc-loop status --task-id my-task --json
cc-loop summary --task-id my-task --json   # Luma / TUI single-file recap
```

Default providers already separate writer (`cursor`) from reviewer (`codex`). Opt into merging only when you mean it:

```bash
cc-loop init ... --auto-merge --test-command -- cargo test -q
```

Set `CC_LOOP_STATE_ROOT` to override the default `~/.cc-loop` state directory without passing `--state-root` on every command.

Global flags such as `--state-root` must appear **before** the subcommand, e.g. `cc-loop --state-root PATH list --json`.

### Provider reference

| Provider | Roles | Notes |
|---|---|---|
| `codex` | planner, reviewer | `codex exec` CLI; JSON output |
| `cursor` | implementer | `cursor agent` CLI; edits in worktree |
| `claude-code` | planner, reviewer, implementer | `claude` CLI; `--print` for planning/review, direct edits for implementation |
| `fake` | all (offline) | `CC_LOOP_FAKE_PROVIDERS=1` for local contract loops |

## Non-goals

- No cloud coordinator / multi-user service.
- No general multi-agent framework or LangGraph-style graph runtime as the product.
- No automatic merge when tests fail; no default merge into the user’s main checkout.
- No edits in the user’s main working tree; dirty repos block runs.
- No `pkill -f`; subprocess cleanup uses process groups (`shell=false`).
