# autoworkflow

Personal automation workflows for local AI coding agents.

The primary tool is `cc-loop`: a command-line orchestrator that assigns planning, review, and implementation roles to configurable local agents while the local script manages state, git isolation, tests, retries, and recovery.

This repository is for the workflow tooling itself. It is not part of DeckBridge and should not document DeckBridge features as implemented here.

## cc-loop positioning

`cc-loop` is a small local coordinator, not a third coding agent.

- `planner`, `reviewer`, and `implementer` are fixed workflow roles.
- Each role is backed by a configurable provider: `codex`, `cursor`, or `claude-code`.
- `cc-loop` owns orchestration, state, prompts, subprocess calls, worktrees, test gates, and audit artifacts.

The initial default setup is:

- `planner = codex`
- `reviewer = codex`
- `implementer = cursor`

The design goal is a reliable personal loop:

1. The configured planner analyzes the target repo and produces a bounded implementation prompt.
2. `cc-loop` creates an isolated git worktree for the attempt.
3. The configured implementer runs headlessly in that worktree and makes code changes.
4. `cc-loop` runs configured tests and gathers bounded diff context.
5. The configured reviewer reviews the result.
6. `cc-loop` either merges, retries, stops, or leaves the worktree for manual inspection.

## Current status

Status: **v0.3 recoverable failure handling** (package 0.3.0).

v1 core loop + v0.2.0 integration contract + v0.3 auto recovery (see [RECOVERY.md](docs/RECOVERY.md)).

- task initialization, state persistence, and artifact layout under `~/.cc-loop`
- preflight checks including dirty-repo blocking
- configurable provider adapters (`codex`, `cursor`, `claude-code`) for planner, implementer, and reviewer roles
- isolated git worktree per iteration/retry
- planner and implementer execution with timeout-safe process groups
- configured `test_command` execution with pass/fail/skipped gating
- bounded diff collection for reviewer context
- reviewer phase with normalized `approve` / `reject` / `stop` decisions
- auto-merge when tests pass (or are explicitly allowed to be skipped), review approves, and git merge succeeds
- retry from base commit after reviewer reject
- `cc-loop resume` for stopped, interrupted, or in-progress attempts
- `cc-loop auto` for fully unattended execution with macOS notifications
- `cc-loop status` with phase, decision, artifacts, and next-action hints
- **v0.2.0:** `--task-id` on operational commands, `list`, `status --json`, `doctor`, `auto --detach`, `CC_LOOP_STATE_ROOT`, init model/cursor flags

References:

- [Integration contract](docs/INTEGRATION.md)
- [Exit codes](docs/EXIT_CODES.md)
- [Project plan](docs/PROJECT_PLAN.md)
- [v1 technical design](docs/V1_TECHNICAL_DESIGN.md)
- [Debugging guide](docs/DEBUGGING.md)
- [Changelog](CHANGELOG.md)

## Command shape

```bash
# Initialize — config flags are optional
cc-loop init \
  --goal "Implement the requested workflow" \
  --repo /path/to/repo \
  --task-id my-task \
  --test-command pytest tests/ \
  --planner claude-code \
  --reviewer claude-code \
  --implementer claude-code \
  --claude-code-model sonnet

cc-loop doctor --repo /path/to/repo
cc-loop list --json
cc-loop run --task-id my-task
cc-loop resume --task-id my-task
cc-loop auto --detach --task-id my-task
cc-loop status --task-id my-task --json
```

Set `CC_LOOP_STATE_ROOT` to override the default `~/.cc-loop` state directory without passing `--state-root` on every command.

Global flags such as `--state-root` must appear **before** the subcommand, e.g. `cc-loop --state-root PATH list --json`.

### Provider reference

| Provider | Roles | Notes |
|---|---|---|
| `codex` | planner, reviewer | `codex exec` CLI; JSON output |
| `cursor` | implementer | `cursor agent` CLI; edits in worktree |
| `claude-code` | planner, reviewer, implementer | `claude` CLI; `--print` for planning/review, direct edits for implementation |

## v1 non-goals

- No cloud coordinator.
- No multi-user service.
- No user-defined arbitrary shell provider runner in v1.
- No direct edits in the user's main working tree.
- No automatic merge when tests fail.
- No global process killing such as `pkill -f`.
- No attempt to make planner/reviewer and implementer providers talk to each other directly.
