# cc-loop integration contract (v1)

This document defines the **stable external interface** for invoking cc-loop as a black-box subprocess. Consumers such as macOS apps / Luma must depend only on the CLI subset and JSON schemas here—not on internal modules, artifact layouts, or private delivery-loop internals.

**Package version:** 0.12.0 (Rust)  
**Integration schema version:** 1

> Spawn the `cc-loop` binary (`make install`, `./scripts/cc-loop`, or `rust/target/release/cc-loop`). See [rust/docs/MIGRATION.md](../rust/docs/MIGRATION.md).

## Purpose

cc-loop is a **role-separated delivery engine** (not a general multi-agent framework). External apps should:

1. Spawn documented commands with `subprocess`
2. Parse `status --json` and `summary --json` (and optionally `list --json`, `doctor --json`)
3. Never embed cc-loop internals or duplicate its state machine

Default success = tests green + review approve + changes ready for handoff on an attempt branch. Merging into the user’s base branch is opt-in (`auto_merge`).

## Stable CLI subset

These commands and flags are the integration contract. Other commands exist for interactive use but are not required for thin integrations.

| Command | Purpose |
|---------|---------|
| `cc-loop init ...` | Create a task |
| `cc-loop doctor --repo PATH` | Preflight without creating a task |
| `cc-loop list [--repo PATH] [--json]` | Enumerate tasks |
| `cc-loop status [--task-id ID] [--json]` | Poll task state |
| `cc-loop graph [--task-id ID] [--json] [--history]` | Inspect task graph progress (advanced / v0.4+); `--history` shows graph mutations (v0.7) |
| `cc-loop report [--task-id ID] [--json] [--format json\|human]` | Task report with graph progress, failures, artifacts (v0.6) |
| `cc-loop summary [--task-id ID] [--json]` | Luma-oriented single JSON summary (v0.10+; sharpened in v0.11) |
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
  --test-command -- cargo test -q

cc-loop init --goal "..." --repo "$PROJECT_PATH" --task-id "$TASK_ID" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q

cc-loop auto --detach --task-id "$TASK_ID"

# Poll until done:
cc-loop status --task-id "$TASK_ID" --json

# Single-file recap for Luma (after terminal state or anytime):
cc-loop summary --task-id "$TASK_ID" --json
```

### v0.11 default behavior (compatibility notes)

| Setting | Default | Notes |
|---------|---------|-------|
| `auto_merge` | `false` | Success leaves work on the attempt branch (`success=ready_for_handoff`). Pass `--auto-merge` / set `auto_merge=true` to merge into the base branch. |
| `planner_granularity` | `single` | Default path is a single closed loop. Use `auto`/`graph` for advanced multi-node plans. |
| `require_distinct_reviewer` | `true` | Default on. init/doctor/run/auto fail if implementer and reviewer share provider+model. Escape hatch: `--allow-same-reviewer`. |
| `allow_merge_without_tests` | `false` | Must be explicit. |
| `test_command` for `auto` | required | `cc-loop auto` exits `1` when `test_command` is empty unless `allow_merge_without_tests=true`. |

Old state files without `task_graph` still load. Loading old `state.json` merges missing config keys onto current defaults, so:
- tasks without `auto_merge` become handoff-default (`false`)
- tasks without `require_distinct_reviewer` become enforced (`true`); use `--allow-same-reviewer` on a new init or set the config key false if a legacy same-role setup must continue

## `status --json` schema (schema_version 1)

Stdout is a single JSON object. No extra prose.

```json
{
  "schema_version": 1,
  "cc_loop_version": "0.12.0",
  "task_id": "abc123",
  "goal": "...",
  "target_repo": "/absolute/path",
  "base_branch": "main",
  "base_commit": "sha",
  "status": "stopped",
  "iteration": 1,
  "roles": {
    "planner": {"provider": "claude-code", "model": "sonnet"},
    "implementer": {"provider": "cursor", "model": ""},
    "reviewer": {"provider": "claude-code", "model": "sonnet"}
  },
  "distinct_reviewer": true,
  "require_distinct_reviewer": true,
  "auto_merge": false,
  "success": "stopped",
  "latest_reject_reason": null,
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
    "graph_node_id": "T1 or empty for legacy tasks",
    "running_provider": "cursor or empty when no provider subprocess is active"
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

> Note: `task_graph` is omitted for the default single-loop path and for legacy
> tasks without a graph. When present (advanced multi-node runs), it remains an
> additive block as documented below.

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
| `roles` | object | `{planner,implementer,reviewer}` with `provider`/`model` (v0.11) |
| `distinct_reviewer` | bool | Whether writer/reviewer identities differ (v0.11) |
| `require_distinct_reviewer` | bool | Config flag (v0.11; default true) |
| `auto_merge` | bool | Whether approved work merges into base (v0.11; default false) |
| `success` | string | `ready_for_handoff` / `merged` / `stopped` / `failed` / … (v0.11) |
| `latest_reject_reason` | string \| null | Most recent reviewer reject reason (v0.11) |
| `running_node_ids` | array | Parallel running node ids when applicable (v0.9) |
| `reviewer_prompt_metrics` | object \| omitted | Latest attempt reviewer cache metrics when `review.prompt.metrics.json` exists (v0.10 additive) |
| `prompt_cache` | object \| omitted | Summary from `prompt.cache.json` when present: path, token totals, reviewer context mode, omitted patch chars (additive) |
| `heartbeat` | object \| omitted | Fresh `runner.heartbeat.json` phase/provider snapshot during active runs (v0.10 additive) |

`attempt.running_provider` (v0.10) is non-empty while a provider subprocess is active.

The `task_graph` block is omitted for legacy tasks without a graph. See [TASK_GRAPH.md](TASK_GRAPH.md).

### `next_action` values

| Value | Meaning |
|-------|---------|
| `none` | Detached runner is active; wait and poll |
| `run` | Task initialized, no attempts yet |
| `resume` | Continue or retry the current attempt |
| `inspect` | Reviewer requested stop; human inspection recommended |
| `done` | Task completed successfully (`success` is typically `ready_for_handoff` or `merged`) |
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
- Import or link against cc-loop internals from another application (use the CLI only)
- Depend on undocumented CLI flags or exit-code nuances without reading `status --json`

## `init` flags (integration-relevant)

In addition to goal/repo/providers/test-command:

- `--test-command -- ARG ...` — **required for `auto`** unless `allow_merge_without_tests` is set. Place `--` before the command so pytest/cargo flags are not parsed as cc-loop options.
- `--require-distinct-reviewer` — enforce distinct identities (default already on).
- `--allow-same-reviewer` — escape hatch to disable distinct-reviewer enforcement (not recommended).
- `--auto-merge` — opt in to merge approved work into the base branch (default off; success is handoff-ready on the attempt branch).
- `--allow-merge-without-tests` — explicit escape hatch only; never the default.
- `--planner-granularity single|auto|graph` — default `single` (simple closed loop). `graph` is advanced.
- `--planner-mode auto|graph|single|direct` — planner execution mode (default `auto`). `direct` skips the planner provider and synthesizes a single-node task graph from the goal.
- `--review-context-mode hybrid|inline|artifact_refs` — reviewer prompt context (default `hybrid`).
- `--review-inline-patch-threshold N` — hybrid reviewer inline patch character limit (default `8000`)
- `--provider-watchdog-grace-seconds N` — extra seconds after provider timeout before force-kill (default `5`)
- `--task-id ID` — explicit task id (recommended for integrations)
- `--codex-model`, `--cursor-model`, `--claude-code-model`
- `--cursor-force`, `--cursor-sandbox`
- `--goal-file PATH` — mutually exclusive with `--goal`

## `doctor` flags

```
cc-loop doctor --repo PATH [--base-branch main]
  [--planner NAME] [--reviewer NAME] [--implementer NAME]
  [--require-distinct-reviewer | --allow-same-reviewer]
  [--test-command ARG ...] [--json]
```

Success: exit 0, prints `ok` plus roles / distinct_reviewer / require_distinct_reviewer (human), or
`{"ok": true, "warnings": [...], "distinct_reviewer": bool, "require_distinct_reviewer": bool, "auto_merge_default": bool}`.
Failure: exit 1, message on stderr.

Even when checks pass, doctor prints strong recommendations when `require_distinct_reviewer` is off or `test_command` is missing.

## v0.10 artifacts and observability (additive)

Each attempt artifact directory may include:

| Artifact | Description |
|----------|-------------|
| `plan.prompt.meta.json` | Planner prompt version/label/deployment metadata |
| `implementer.prompt.meta.json` | Implementer prompt metadata |
| `review.prompt.meta.json` | Reviewer prompt metadata |
| `review.prompt.metrics.json` | Reviewer cache-layout metrics (`stable_prefix_ratio`, `contract_prefix_ratio`, `cache_health`, `context_mode`, `omitted_patch_chars`, `estimated_avoidable_miss_tokens`, token estimates) |
| `prompt.cache.json` | Per-attempt prompt cache budget across planner/implementer/reviewer phases with totals |
| `attempt.trace.json` | Normalized per-attempt trace with phase status and artifact paths |
| `command.argv.json` | Executed argv per phase (`planner`, `implementer`, `reviewer`, `test`); prompt text is redacted as `<prompt:N chars sha256=...>` placeholders |
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
    "contract_prefix_ratio": 0.92,
    "cache_health": "good",
    "total_prompt_cache_health": "warning",
    "estimated_prompt_tokens": 456,
    "context_mode": "hybrid",
    "inline_patch": false,
    "omitted_patch_chars": 12000,
    "estimated_avoidable_miss_tokens": 3000
  },
  "prompt_cache": {
    "path": "/absolute/path/prompt.cache.json",
    "estimated_prompt_tokens": 12000,
    "estimated_provider_prompt_tokens": 9000,
    "estimated_avoidable_miss_tokens": 3000,
    "reviewer_context_mode": "hybrid",
    "reviewer_inline_patch": false,
    "reviewer_omitted_patch_chars": 12000
  },
  "prompt_metadata_paths": {
    "planner": "/absolute/path/plan.prompt.meta.json",
    "implementer": "/absolute/path/implementer.prompt.meta.json",
    "reviewer": "/absolute/path/review.prompt.meta.json"
  }
}
```

### `summary --json` schema (v0.10+, sharpened in v0.11)

Luma-oriented single JSON object. Does not replace `report --json`. One payload should tell the delivery story.

```bash
cc-loop summary --task-id ID --json
```

Example (shape; fields may be null/empty before the first attempt):

```json
{
  "schema_version": 1,
  "task_id": "abc123",
  "goal": "...",
  "status": "done",
  "phase": "approved",
  "next_action": "done",
  "roles": {
    "planner": {"provider": "codex", "model": ""},
    "implementer": {"provider": "cursor", "model": ""},
    "reviewer": {"provider": "codex", "model": ""}
  },
  "distinct_reviewer": true,
  "require_distinct_reviewer": true,
  "auto_merge": false,
  "plan_summary": "…",
  "latest_attempt": {"iteration": 1, "retry": 0, "phase": "approved", "test_status": "passed"},
  "latest_reject_reason": null,
  "tests": {"status": "passed", "pass": true, "fail": false, "skipped": false, "reason": "test_command passed"},
  "review": {"decision": "approve", "reason": "…", "issues": []},
  "diff_stat": {"path": "…/diff.stat.txt", "preview": "…", "branch": "cc-loop/…"},
  "success": "ready_for_handoff",
  "artifacts": {"diff_stat": "…", "test_output": "…", "review_parsed": "…"}
}
```

Key fields:

| Field | Description |
|-------|-------------|
| `schema_version` | Summary schema version (currently `1`) |
| `task_id`, `goal`, `status`, `phase`, `next_action` | Task identity and dispatcher hint |
| `roles` | `{planner,implementer,reviewer}` each with `provider` and `model` |
| `providers` | Legacy flat planner/implementer/reviewer provider map |
| `distinct_reviewer` | `true` when implementer and reviewer identities differ |
| `require_distinct_reviewer` | Config flag |
| `auto_merge` | Whether approved work auto-merges into the base branch |
| `plan_summary` | Short plan / graph summary |
| `latest_attempt` | Iteration, retry, phase, decision, test status, branch, worktree, artifact dir |
| `latest_reject_reason` | Most recent reviewer reject reason (or null) |
| `tests` | `{status, pass, fail, skipped, reason, exit_code}` |
| `review` | Decision, reason, issues |
| `diff_stat` | `{path, preview, branch, worktree_path, head_commit}` |
| `success` | Stable enum: `ready_for_handoff`, `merged`, `stopped`, `failed`, `cancelled`, `running`, `initialized` |
| `artifacts` | Small key-path map (plan/diff/test/review/merge) |
| `artifact_paths` | Full latest-attempt artifact path map |
| `prompt_cache` / `reviewer_prompt_metrics` / `subprocess_result` | Observability snapshots |
| `failure` | Failure summary compatible with `status --json` |
| `suggested_next_action` | Recovery hint |

Human output (`cc-loop summary --task-id ID`) shows who writes, who reviews, tests, review decision, and whether the work is handoff-ready.

### Reviewer reject → implement retry

On reviewer `reject`, `auto` / `resume` return to the implementer (`AutoStep.RESUME` / `_begin_retry_attempt`) with rejection feedback in the implementer prompt, until `max_retries_per_step` is exhausted. `summary --json` exposes `latest_attempt.retry` and `latest_reject_reason`.

### Task-level `run.summary.json` (v0.10+, additive)

Written to `~/.cc-loop/tasks/<task-id>/run.summary.json` when a task reaches a terminal state (`done`, `failed`, `cancelled`, or non-recoverable `stopped`). Content matches `cc-loop summary --json` for that task.

### Reviewer prompt layout (v0.10+)

Reviewer prompts use three sections before per-attempt evidence:

1. `## Stable Review Contract` / Rubric / JSON output contract
2. `## Task Review Context` — goal, node criteria, files scope (stable across retries of the same node)
3. `## Dynamic Review Payload` — iteration, commits, test output, diff/patch evidence

When `review_context_mode` is `artifact_refs`, or `hybrid` with a large patch, the reviewer prompt omits the full `git diff --stat` body and instead includes a short summary plus paths to `diff.stat.txt` and `diff.files.txt`.

### Auto direct planner (`auto_direct_planner`)

Config keys (defaults):

- `auto_direct_planner`: `true`
- `auto_direct_max_goal_chars`: `500`

When `planner_mode` is `auto` (default), cc-loop may skip the planner provider for short, simple goals (keywords like `fix`, `bug`, `cli`, `docs`, `typo`, `test`, or phrases like `minimal change`). Complex goals containing keywords like `architecture`, `migration`, or `redesign` always use the planner provider.

`planner_mode: direct` still forces direct mode. `planner_mode: single|graph` is never overridden.

`prompt.cache.json` planner phase records `planner_mode_resolved`, `planner_direct_reason`, and `provider_skipped`.

### Task-level terminal artifacts (v0.10+)

On terminal disposition (`done`, `failed`, `cancelled`, or non-recoverable `stopped`), cc-loop writes:

| Artifact | Description |
|----------|-------------|
| `run.summary.json` | Same payload as `cc-loop summary --json` |
| `execution.timeline.json` | Time-ordered phase events from `events.jsonl` with subprocess durations |

Terminal events in `events.jsonl`: `task.completed`, `task.failed`, `task.cancelled` (deduplicated).

### Cursor implementer progress (v0.10+)

When the Cursor implementer produces 0-byte raw output for 30+ seconds, `runner.heartbeat.json` and `status --json` heartbeat include `provider_progress` such as `cursor implementer: waiting for output (45s elapsed)`.
