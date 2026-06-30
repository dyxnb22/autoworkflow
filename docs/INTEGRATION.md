# cc-loop integration contract (v1)

This document defines the **stable external interface** for invoking cc-loop as a black-box subprocess. Consumers such as macOS apps must depend only on the CLI subset and JSON schemas here—not on internal Python modules, artifact layouts, or orchestration logic.

**Package version:** 0.2.0  
**Integration schema version:** 1

## Purpose

cc-loop is a local CLI orchestrator. External apps should:

1. Spawn documented commands with `subprocess`
2. Parse `status --json` (and optionally `list --json`, `doctor --json`)
3. Never embed cc-loop Python code or duplicate its state machine

## Stable CLI subset

These commands and flags are the integration contract. Other commands exist for interactive use but are not required for thin integrations.

| Command | Purpose |
|---------|---------|
| `cc-loop init ...` | Create a task |
| `cc-loop doctor --repo PATH` | Preflight without creating a task |
| `cc-loop list [--repo PATH] [--json]` | Enumerate tasks |
| `cc-loop status [--task-id ID] [--json]` | Poll task state |
| `cc-loop auto --detach [--task-id ID]` | Start unattended loop in background |
| `cc-loop resume [--task-id ID]` | Continue after stop/interrupt (optional; polling may be enough) |

Global flags:

- `--state-root PATH` — state directory (default `~/.cc-loop`); must appear **before** the subcommand (e.g. `cc-loop --state-root PATH status --json`)
- `--version` — print version and exit

Environment:

- `CC_LOOP_STATE_ROOT` — when set and `--state-root` is not passed on the command line, defaults `--state-root` to this path.

## Recommended integration flow

```bash
cc-loop doctor --repo "$PROJECT_PATH" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command python -m pytest tests/ -q

cc-loop init --goal "..." --repo "$PROJECT_PATH" --task-id "$TASK_ID" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command python -m pytest tests/ -q

cc-loop auto --detach --task-id "$TASK_ID"

# Poll until done:
cc-loop status --task-id "$TASK_ID" --json
```

## `status --json` schema (schema_version 1)

Stdout is a single JSON object. No extra prose.

```json
{
  "schema_version": 1,
  "cc_loop_version": "0.2.0",
  "task_id": "abc123",
  "goal": "...",
  "target_repo": "/absolute/path",
  "base_branch": "main",
  "base_commit": "sha",
  "status": "stopped",
  "iteration": 1,
  "attempt": {
    "iteration": 1,
    "retry": 0,
    "phase": "rejected",
    "decision": "reject",
    "test_status": "passed",
    "implementer_exit_code": 0,
    "worktree_path": "/path or empty string",
    "merge_error": "",
    "artifact_dir": "/absolute/path/to/artifacts/iter-001",
    "created_at": "ISO8601 or empty"
  },
  "next_action": "resume",
  "running": false,
  "runner_pid": null
}
```

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | int | Integration JSON schema version (currently `1`) |
| `cc_loop_version` | string | cc-loop package version |
| `task_id` | string | Task identifier |
| `goal` | string | Task goal from init |
| `target_repo` | string | Absolute path to target git repo |
| `base_branch` | string | Base branch name |
| `base_commit` | string | Current base commit SHA |
| `status` | string | Task status: `initialized`, `running`, `stopped`, `done`, `failed`, etc. |
| `iteration` | int | Current iteration counter |
| `attempt` | object | Latest attempt snapshot (empty strings when no attempt yet) |
| `next_action` | string | Stable enum (see below) |
| `running` | bool | `true` when `runner.pid` exists and process is alive |
| `runner_pid` | int \| null | PID from detached `auto`, or null |

### `next_action` values

| Value | Meaning |
|-------|---------|
| `none` | Detached runner is active; wait and poll |
| `run` | Task initialized, no attempts yet |
| `resume` | Continue or retry the current attempt |
| `inspect` | Reviewer requested stop; human inspection recommended |
| `done` | Task completed successfully |
| `failed` | Task or attempt failed |
| `repair` | Auto loop will run implementer repair on a recoverable failure |
| `terminal` | Unrecoverable stop; inspect `failure` block in JSON |

Mapping follows the auto recovery dispatcher in `recovery.decide_auto_step`.

Optional `failure` object (additive, schema v1):

```json
"failure": {
  "failure_type": "merge_conflict",
  "disposition": "recoverable",
  "stop_reason": "",
  "recovery_retry_count": 1,
  "merge_retry_count": 0,
  "attempted_repairs": ["implementer_repair:merge_conflict"],
  "suggested_actions": ["..."],
  "details": {}
}
```

See [RECOVERY.md](RECOVERY.md) for failure types and budgets.

## `list --json` item schema

Stdout is a JSON array of objects:

```json
{
  "task_id": "...",
  "status": "initialized",
  "target_repo": "/abs/path",
  "phase": "planning",
  "updated_at": "2026-06-30T12:00:00+00:00",
  "goal": "...",
  "iteration": 0
}
```

`phase` is `-` when no attempts exist. `updated_at` is the `state.json` modification time (UTC ISO8601).

Human output (default): tab-separated `task_id`, `status`, `target_repo`, `phase`, `updated_at`.

## Exit codes

See [EXIT_CODES.md](EXIT_CODES.md).

## Detached `auto`

`cc-loop auto --detach --task-id ID`:

1. Spawns a background child running `auto` without `--detach`
2. Writes child PID to `<state-root>/tasks/<id>/runner.pid`
3. Appends child stdout/stderr to `<state-root>/tasks/<id>/runner.log`
4. Parent prints one line: `detached pid=<pid> task_id=<id> log=<path>` and exits 0

Poll `status --json` fields `running` and `runner_pid` to track the background runner.

## State file `schema_version`

New and saved `state.json` files include top-level `"schema_version": 1`. Older files without this field load with default `1`.

## Semantic versioning policy

- **Patch** (0.2.x): bug fixes, no contract change
- **Minor** (0.x.0): backward-compatible additions (new optional JSON fields)
- **Major** (x.0.0): breaking CLI or JSON changes — bump integration `schema_version`

## What integrators should NOT do

- Parse artifact directories or internal prompt files
- Block on foreground `auto` for long-running tasks (use `--detach`)
- Import `cc_loop` Python modules from another application
- Depend on undocumented CLI flags or exit-code nuances without reading `status --json`

## `init` flags (integration-relevant)

In addition to goal/repo/providers/test-command:

- `--task-id ID` — explicit task id (recommended for integrations)
- `--codex-model`, `--cursor-model`, `--claude-code-model`
- `--cursor-force`, `--cursor-sandbox`
- `--goal-file PATH` — mutually exclusive with `--goal`

## `doctor` flags

```
cc-loop doctor --repo PATH [--base-branch main]
  [--planner NAME] [--reviewer NAME] [--implementer NAME]
  [--test-command ARG ...] [--json]
```

Success: exit 0, prints `ok` or `{"ok": true}`. Failure: exit 1, message on stderr.
