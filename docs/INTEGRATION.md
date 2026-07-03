# cc-loop integration contract (v1)

This document defines the **stable external interface** for invoking cc-loop as a black-box subprocess. Consumers such as macOS apps must depend only on the CLI subset and JSON schemas here—not on internal Python modules, artifact layouts, or orchestration logic.

**Package version:** 0.10.0  
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
| `cc-loop graph [--task-id ID] [--json] [--history]` | Inspect task graph progress (v0.4+); `--history` shows graph mutations (v0.7) |
| `cc-loop report [--task-id ID] [--json] [--format json\|human]` | Task report with graph progress, failures, artifacts (v0.6) |
| `cc-loop eval --task-id ID --suite PATH [--json]` | Run local eval suite against latest attempt artifacts (v0.10) |
| `cc-loop export --task-id ID --format jsonl --output PATH` | Export analytics-compatible JSONL rows (v0.10) |
| `cc-loop stop --task-id ID [--json]` | Stop detached runner (v0.5) |
| `cc-loop cancel --task-id ID [--json]` | Stop runner and mark task cancelled (v0.5) |
| `cc-loop cleanup --task-id ID [--json]` | Remove task-owned runtime artifacts (v0.5) |
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
  --test-command -- python -m pytest tests/ -q

cc-loop init --goal "..." --repo "$PROJECT_PATH" --task-id "$TASK_ID" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- python -m pytest tests/ -q

cc-loop auto --detach --task-id "$TASK_ID"

# Poll until done:
cc-loop status --task-id "$TASK_ID" --json
```

## `status --json` schema (schema_version 1)

Stdout is a single JSON object. No extra prose.

```json
{
  "schema_version": 1,
  "cc_loop_version": "0.10.0",
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
    "created_at": "ISO8601 or empty",
    "graph_node_id": "T1 or empty for legacy tasks"
  },
  "task_graph": {
    "schema_version": 1,
    "current_node_id": "T2",
    "summary": {
      "total": 5,
      "pending": 3,
      "running": 0,
      "passed": 2,
      "failed": 0,
      "rejected": 0,
      "blocked": 0,
      "skipped": 0
    },
    "nodes": [
      {
        "id": "T1",
        "title": "Set up project structure",
        "kind": "implementation",
        "owner": "implementer",
        "dependencies": [],
        "status": "passed",
        "retry_count": 0
      }
    ]
  },
  "next_action": "resume",
  "running": false,
  "runner_pid": null,
  "runner_state": "idle",
  "last_heartbeat_at": "",
  "runner_started_at": "",
  "elapsed_seconds": 0,
  "can_stop": false,
  "can_resume": true,
  "can_cleanup": true,
  "log_path": "/absolute/path/to/runner.log",
  "current_message": "Ready to run"
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
| `task_graph` | object \| omitted | Present when task has a graph (v0.4 additive) |
| `runner_state` | string | `idle`, `running`, `stopped`, `stale_pid`, `stale_heartbeat` (v0.5 additive) |
| `last_heartbeat_at` | string | ISO8601 from `runner.heartbeat.json` or empty (v0.5) |
| `runner_started_at` | string | ISO8601 runner start or empty (v0.5) |
| `elapsed_seconds` | int | Wall-clock seconds since first attempt (v0.5) |
| `can_stop` | bool | Whether `cc-loop stop` can terminate a runner (v0.5) |
| `can_resume` | bool | Whether resume/auto can continue (v0.5) |
| `can_cleanup` | bool | Whether cleanup is safe (v0.5) |
| `log_path` | string | Path to `runner.log` (v0.5) |
| `current_message` | string | Short UI-friendly status message (v0.6) |
| `running_node_ids` | array | Parallel running node ids when applicable (v0.9) |

The `task_graph` block is omitted for legacy tasks without a graph. See [TASK_GRAPH.md](TASK_GRAPH.md).

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

## `graph --json` schema (v0.4, additive)

Stdout is a JSON object:

```json
{
  "task_graph": {
    "schema_version": 1,
    "current_node_id": "T2",
    "summary": { "total": 2, "passed": 1, "pending": 1, "...": 0 },
    "nodes": [ { "id": "T1", "title": "...", "status": "passed", "...": "..." } ]
  }
}
```

When no graph exists: `{"task_graph": null}`.

Human `graph` output lists node id, status, and title with a progress line.

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
4. Child refreshes `<state-root>/tasks/<id>/runner.heartbeat.json` each loop iteration (v0.5)
5. Parent prints one line: `detached pid=<pid> task_id=<id> log=<path>` and exits 0

Poll `status --json` fields `running`, `runner_pid`, `runner_state`, and `last_heartbeat_at`.

## State file `schema_version`

New and saved `state.json` files include top-level `"schema_version": 1`. Older files without this field load with default `1`.

## Semantic versioning policy

- **Patch** (0.3.x): bug fixes, no contract change
- **Minor** (0.x.0): backward-compatible additions (new optional JSON fields)
- **Major** (x.0.0): breaking CLI or JSON changes — bump integration `schema_version`

## What integrators should NOT do

- Parse artifact directories or internal prompt files
- Block on foreground `auto` for long-running tasks (use `--detach`)
- Import `cc_loop` Python modules from another application
- Depend on undocumented CLI flags or exit-code nuances without reading `status --json`

## `init` flags (integration-relevant)

In addition to goal/repo/providers/test-command:

- `--test-command -- ARG ...` — recommended form; place `--` before the command so pytest/cargo flags are not parsed as cc-loop options. With this separator, every following token belongs to the test command; put cc-loop flags before `--test-command`. A single quoted string is also accepted and split with shell rules (shell pipelines are rejected).
- `--planner-granularity single|auto|graph` — control planner decomposition (default `auto`)
- `--provider-watchdog-grace-seconds N` — extra seconds after provider timeout before force-kill (config key `provider_watchdog_grace_seconds`, default `5`)
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

## v0.10 artifacts and observability (additive)

Each attempt artifact directory may include:

| Artifact | Description |
|----------|-------------|
| `plan.prompt.meta.json` | Planner prompt version/label/deployment metadata |
| `implementer.prompt.meta.json` | Implementer prompt metadata |
| `review.prompt.meta.json` | Reviewer prompt metadata |
| `review.prompt.metrics.json` | Reviewer cache-layout metrics (stable prefix ratio, token estimates) |
| `attempt.trace.json` | Normalized per-attempt trace with phase status and artifact paths |
| `command.argv.json` | Executed argv per phase (`planner`, `implementer`, `reviewer`, `test`) |
| `subprocess.result.json` | Subprocess exit metadata per phase (`exit_code`, `timed_out`, `hung`, `duration_seconds`, `killed`, stdout/stderr paths) |

### Prompt metadata contract (`schema_version` 1)

```json
{
  "schema_version": 1,
  "role": "reviewer",
  "prompt_name": "cc-loop-reviewer",
  "prompt_version": "0.10.0",
  "label": "production",
  "layout": "stable-prefix-v1",
  "provider": "codex",
  "model": "",
  "task_id": "abc123",
  "iteration": 1,
  "retry": 0,
  "graph_node_id": "T1",
  "created_at": "2026-07-02T12:00:00+00:00",
  "prompt_path": "/absolute/path/review.prompt.txt"
}
```

### Trace contract (`schema_version` 1)

`attempt.trace.json` summarizes providers, models, and per-phase status with
artifact paths and heuristic `estimated_prompt_tokens` values (`ceil(chars / 4)`).

### `eval` command

```
cc-loop eval --task-id ID --suite PATH [--json]
```

- Loads the latest attempt artifacts for the task.
- Evaluates JSON artifact assertions from a local suite file (`schema_version` 1).
- Exit `0` when all cases pass, `1` when any fail, `2` for invalid suite/input.
- Supported assertion ops: `==`, `!=`, `>=`, `>`, `<=`, `<`, `contains`, `exists`.

### `export` command

```
cc-loop export --task-id ID --format jsonl --output PATH
```

Writes one JSON object per line for planner, implementer, testing, reviewer, and
merge phases. Rows include `schema_version`, task/attempt identifiers, provider,
model, prompt paths, token estimates, reviewer `decision`, `stable_prefix_ratio`
when available, and `timestamp`.

### `report --json` observability fields (additive)

```json
"observability": {
  "trace_path": "/absolute/path/attempt.trace.json",
  "reviewer_prompt_metrics": {
    "layout": "stable-prefix-v1",
    "stable_prefix_ratio": 0.8,
    "estimated_prompt_tokens": 456
  },
  "prompt_metadata_paths": {
    "planner": "/absolute/path/plan.prompt.meta.json",
    "implementer": "/absolute/path/implementer.prompt.meta.json",
    "reviewer": "/absolute/path/review.prompt.meta.json"
  }
}
```
