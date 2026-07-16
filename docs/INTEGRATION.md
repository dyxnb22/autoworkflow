# Integration contract (schema 1)

Stable CLI/JSON surface for integrators (e.g. Luma). Depend on this document only — not on internal modules or artifact layouts.

**Package:** 0.12.0 · **Binary:** `make install` / `./scripts/cc-loop` / `rust/target/release/cc-loop`

## Rules for integrators

1. Spawn documented commands via subprocess.
2. Parse `status --json` and `summary --json` (optionally `list` / `doctor` / `graph`).
3. Do not embed cc-loop internals or scrape artifact directories for control flow.
4. Prefer `auto --detach` + polling over blocking foreground `auto`.

Default success = green tests + review approve + handoff-ready branch. Merge is opt-in (`auto_merge`).

## Stable CLI

| Command | Purpose |
|---------|---------|
| `init` | Create a task |
| `doctor --repo PATH` | Preflight |
| `list [--json]` | Enumerate tasks |
| `status [--task-id ID] [--json]` | Poll state |
| `summary [--task-id ID] [--json]` | Single-file delivery recap |
| `graph [--task-id ID] [--json] [--history]` | Task graph (advanced) |
| `report [--task-id ID] [--json]` | Report with failures/artifacts |
| `auto --detach [--task-id ID]` | Background loop |
| `resume` / `stop` / `cancel` / `cleanup` | Control |
| `eval` / `export` | Local eval / JSONL export |

Global: `--state-root PATH` **before** the subcommand; `--version`. Env: `CC_LOOP_STATE_ROOT`.

## Recommended flow

```bash
cc-loop doctor --repo "$PROJECT_PATH" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q

cc-loop init --goal "..." --repo "$PROJECT_PATH" --task-id "$TASK_ID" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q

cc-loop auto --detach --task-id "$TASK_ID"
cc-loop status --task-id "$TASK_ID" --json
cc-loop summary --task-id "$TASK_ID" --json
```

### Product defaults

| Setting | Default | Notes |
|---------|---------|-------|
| `auto_merge` | `false` | `success=ready_for_handoff`; use `--auto-merge` to merge |
| `planner_granularity` | `single` | `graph` / `auto` for multi-node |
| `require_distinct_reviewer` | `true` | Escape: `--allow-same-reviewer` |
| `allow_merge_without_tests` | `false` | Explicit only |
| `test_command` for `auto` | required | Else exit `1` |

Legacy `state.json` without `task_graph` still loads; missing config keys merge onto current defaults.

## `status --json` (schema_version 1)

Single JSON object on stdout. Core fields:

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | int | Always `1` |
| `cc_loop_version` | string | Package version |
| `task_id` / `goal` / `target_repo` / `base_branch` / `base_commit` | string | Task identity |
| `status` | string | `initialized`, `running`, `stopped`, `done`, `failed`, … |
| `iteration` | int | Iteration counter |
| `roles` | object | `{planner,implementer,reviewer}` × `{provider,model}` |
| `distinct_reviewer` / `require_distinct_reviewer` / `auto_merge` | bool | Gates |
| `success` | string | `ready_for_handoff` / `merged` / `stopped` / `failed` / … |
| `latest_reject_reason` | string \| null | Last reject reason |
| `attempt` | object | Latest attempt (`phase`, `decision`, `test_status`, paths, …) |
| `next_action` | string | See below |
| `running` / `runner_pid` / `runner_state` | bool / int? / string | Detached runner |
| `can_stop` / `can_resume` / `can_cleanup` | bool | Capabilities |
| `log_path` / `current_message` | string | UI helpers |
| `failure` | object \| omitted | Structured failure when present |
| `task_graph` | object \| omitted | Advanced multi-node only |

### `next_action`

| Value | Meaning |
|-------|---------|
| `none` | Runner active — poll |
| `run` | Initialized, no attempts |
| `resume` | Continue / retry |
| `inspect` | Human inspection |
| `done` | Success |
| `failed` | Failed |
| `repair` | Recoverable → implementer repair |
| `terminal` | Unrecoverable; inspect `failure` |

### Optional `failure`

```json
{
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

See [OPERATIONS.md](OPERATIONS.md) for failure types and budgets.

## `list --json`

JSON **array** of `{task_id, status, target_repo, phase, updated_at, goal, iteration}`.

## `graph --json`

`{"task_graph": {…} | null}` — see [OPERATIONS.md](OPERATIONS.md#task-graphs-advanced).

## `summary --json`

Luma-oriented single payload (also written to `run.summary.json` on terminal states). Prefer over parsing many artifact files.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success, or operational stop that is not an execution error |
| `1` | User / config error (missing task, preflight, `auto` without `test_command`, …) |
| `2` | Execution failure (provider timeout/exit, task `failed`) |

Prefer `status --json` / `summary --json` when polling detached runs — do not infer solely from exit codes.

## Detached `auto`

1. Spawns background `auto` without `--detach`
2. Writes `runner.pid`, appends `runner.log`, refreshes `runner.heartbeat.json`
3. Parent prints `detached pid=… task_id=… log=…` and exits `0`

## Init flags (integration-relevant)

- `--test-command -- ARG ...` — required for `auto` (place `--` before the command)
- `--allow-same-reviewer` / `--auto-merge` / `--allow-merge-without-tests`
- `--planner-granularity single|auto|graph` · `--planner-mode auto|graph|single|direct`
- `--review-context-mode hybrid|inline|artifact_refs`
- `--task-id` · `--goal-file` · provider model flags

## Doctor

```bash
cc-loop doctor --repo PATH [--json] [--test-command -- …] \
  [--require-distinct-reviewer | --allow-same-reviewer]
```

Exit `0` → ok; `1` → preflight failed. Warns when distinct-reviewer is off or `test_command` is missing.

## Observability artifacts (additive)

Per-attempt under `artifacts/iter-NNN[-retry-NN]/`:

| Artifact | Role |
|----------|------|
| `*.prompt.txt` / `*.raw*` / `*.parsed.json` | Phase I/O |
| `prompt.cache.json` / `review.prompt.metrics.json` | Cache health |
| `attempt.trace.json` / `command.argv.json` / `subprocess.result.json` | Trace |
| `test.output.txt` / `diff.*` / `patches/` / `merge.output.txt` | Evidence |
| `failure.report.json` | Structured failure |

Prompt argv in `command.argv.json` may be redacted as `<prompt:N chars sha256=…>`; execution still uses the full prompt.

## Versioning

- **Patch:** bug fixes, no contract change
- **Minor:** additive optional JSON fields
- **Major / schema bump:** breaking CLI or JSON — increment `schema_version`
