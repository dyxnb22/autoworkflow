# Changelog

## v0.11.0 — product sharpening

Positions cc-loop as a **role-separated delivery engine** and narrows the default path.

### Defaults

- `auto_merge=false` — success is `ready_for_handoff` on the attempt branch/worktree; merge into base is opt-in (`--auto-merge`)
- `planner_granularity=single` — default single closed loop; multi-node graphs are advanced
- `require_distinct_reviewer=true` — implementer and reviewer provider+model must differ; escape hatch `--allow-same-reviewer`
- `allow_parallel_execution=false` / `max_parallel_nodes=1` unchanged (parallel remains advanced)

### Gates

- `cc-loop auto` exits `1` when `test_command` is unset unless `allow_merge_without_tests=true`
- Reviewer `reject` → `auto`/`resume` returns to implementer with rejection feedback
- Failed/skipped tests never count as success; merge-without-tests stays explicit only

### Luma / integration surface

- `summary --json` tells the delivery story: roles, distinct_reviewer, tests, review, diff_stat, `success`, artifacts
- `status --json` additive: `roles`, `distinct_reviewer`, `require_distinct_reviewer`, `auto_merge`, `success`
- Human `status` / `summary` show who writes, who reviews, tests, and handoff outcome

### Docs

- README / INTEGRATION / Agents / CLAUDE / EXIT_CODES aligned to the delivery-engine positioning
- Task graphs and parallel marked advanced; not the default narrative

### Compatibility

Loading older `state.json` merges missing config keys onto current defaults (`auto_merge=false`, `require_distinct_reviewer=true`). See [INTEGRATION.md](docs/INTEGRATION.md).

## v0.9.0 — 2026-07-02

Full evolution from v0.4 task graph through v0.9 parallel execution (historical).

### v0.4.x — Contract stabilization

- Stale `failure.report.json` no longer pollutes successful `status --json` snapshots
- Graph node status synced with attempt phase in status output
- Contract tests for legacy state load, planner JSON wrapping, recovery-derived `next_action`

### v0.5 — Unattended reliability

- `cc-loop stop --task-id ID [--json]`
- `cc-loop cancel --task-id ID [--json]`
- `cc-loop cleanup --task-id ID [--json]`
- `runner.heartbeat.json` from detached `auto` with staleness detection
- `status --json` additive: `runner_state`, `last_heartbeat_at`, `runner_started_at`, `elapsed_seconds`, `can_stop`, `can_resume`, `can_cleanup`, `log_path`
- Budgets: `max_wall_clock_seconds`, `max_consecutive_failures`, `max_artifact_log_bytes`, `max_changed_files_per_attempt`

### v0.6 — Reports and event stream

- `events.jsonl` append-only audit stream
- `cc-loop report --task-id ID [--json]`
- `status --json` additive: `current_message`

### v0.7 — Dynamic replanning

- Reviewer `decision: replan` with `replan_reason` / `replan_prompt`
- `graph_patch.py` — add/update/skip nodes, dependency edits, validation
- `graph_events.jsonl` and `cc-loop graph --history [--json]`
- Replanning phase via `execute_replan`

### v0.8 — Multi-role routing

- Per-node `planner_provider`, `implementer_provider`, `reviewer_provider`, `reviewer_providers`
- Per-node policy fields with safety guard (`allow_node_policy_weakening`)
- Multi-reviewer chain with conservative aggregation
- `status`/`graph` JSON expose `effective_providers` and `policy` per node

### v0.9 — Parallel execution

- `max_parallel_nodes` concurrent scheduling for independent graph nodes
- `state_lock.py` — file lock + atomic writes (reentrant per thread)
- Merge queue serializes approved parallel nodes
- Failed nodes block dependents only; unrelated nodes continue

### New modules

- `runner_heartbeat.py`, `runner_control.py`, `events.py`, `report.py`, `graph_patch.py`, `budgets.py`, `state_lock.py`, `merge_queue.py`, `parallel_scheduler.py`

### Preserved

- Integration schema version 1 (additive JSON only)
- Legacy state files without `task_graph` still load
- `auto` uses `recovery.decide_auto_step`
- No `pkill`, no main-worktree edits

## v0.4.0 — 2026-07-02

Task graph orchestration for multi-node workflows.

### Added

- [`src/cc_loop/task_graph.py`](src/cc_loop/task_graph.py) — `TaskGraph`, `GraphNode`, dispatcher (`next_runnable_node`, status transitions, completion)
- Planner task graph JSON contract (`mode: task_graph`) with automatic legacy single-step wrapping
- Node-scoped implementer and reviewer prompts
- `TaskState.task_graph` persistence (backward compatible: absent in old state files)
- `AttemptRecord.graph_node_id` linking attempts to graph nodes
- `cc-loop graph [--task-id ID] [--json]` — inspect graph progress
- `status --json` additive `task_graph` block
- [`docs/TASK_GRAPH.md`](docs/TASK_GRAPH.md)
- Tests: `test_task_graph.py`; graph integration tests in `test_run_flow.py`, `test_cli_contract.py`, `test_auto_recovery.py`
- Fake providers: `fake-graph-planner`, `fake-graph-implementer`

### Changed

- `auto` progresses through graph nodes sequentially (one node per iteration)
- Planner prompt prefers task graph output; legacy JSON still supported
- Package version 0.3.0 → 0.4.0

### Preserved

- Legacy single-step planner output and v0.3 recovery behavior
- Existing CLI commands and integration schema version 1

## v0.3.0 — 2026-06-30

Recoverable failure handling for `auto` loop.

### Added

- [`src/cc_loop/failure.py`](src/cc_loop/failure.py) — failure classification and `failure.report.json`
- [`src/cc_loop/recovery.py`](src/cc_loop/recovery.py) — unified `decide_auto_step` dispatch for `auto`
- [`src/cc_loop/repair_prompts.py`](src/cc_loop/repair_prompts.py) — implementer repair prompts
- Recovery config: `max_merge_retries`, `max_merge_recovery_attempts`, `max_recovery_attempts_per_iteration`, `auto_recover_*`
- `AttemptRecord` fields: `failure_type`, `recovery_disposition`, `stop_reason`, `attempted_repairs`, `recovery_retry_count`, `failure_details`
- `status --json` additive `failure` block; `next_action` values `repair`, `terminal`
- [`docs/RECOVERY.md`](docs/RECOVERY.md)
- Tests: `test_failure_classification.py`, `test_recovery_dispatch.py`, `test_auto_recovery.py`

### Changed

- `auto` no longer exits immediately on merge failure or fixable reviewer stop; routes through repair/retry budgets
- Merge failures classified (conflict, worktree busy, permission, etc.)
- Package version 0.2.0 → 0.3.0

### Fixed

- `auto --detach` child subprocess now passes `--state-root` before the `auto` subcommand so global flag ordering matches the CLI contract (`detach.spawn_detached_auto`; `test_detached_child_puts_state_root_before_subcommand`)

## v0.2.0 — 2026-06-30

Integration contract release (v1.1).

### Added

- `--task-id` on `run`, `resume`, `status`, `auto` (explicit task selection; mtime fallback preserved)
- `cc-loop list [--repo PATH] [--json]` — enumerate tasks under `--state-root`
- `cc-loop status --json` — machine-readable status snapshot (integration schema v1)
- `cc-loop doctor --repo PATH` — preflight without creating a task (`--json` supported)
- `cc-loop auto --detach` — background runner with `runner.pid` and `runner.log`
- `schema_version` field in `state.json` (default 1 for legacy files)
- `CC_LOOP_STATE_ROOT` environment variable mirrors `--state-root` when flag omitted
- Init flags: `--codex-model`, `--cursor-model`, `--claude-code-model`, `--cursor-force`, `--cursor-sandbox`, `--goal-file`
- [docs/INTEGRATION.md](docs/INTEGRATION.md) and [docs/EXIT_CODES.md](docs/EXIT_CODES.md)

### Fixed

- `claude-code` planner and reviewer now invoke `--print` mode (was missing when called via generic `provider.run()`)

### Changed

- Package version 0.1.0 → 0.2.0
- `init` requires exactly one of `--goal` or `--goal-file`

## v0.1.0 — 2026-06-30

Initial v1 release.

### Added

- `cc-loop init` — initialize a task state file from a goal and target repo
- `cc-loop run` — execute one planning → implementation → test → review → merge iteration
- `cc-loop resume` — continue an interrupted or stopped attempt
- `cc-loop auto` — run unattended until done
- `cc-loop status` — show current phase, decision, artifact paths, and next-action hint
- Provider adapters: `codex`, `cursor`, `claude-code`
- Isolated git worktree per attempt; bounded diff collection; timeout-safe subprocess handling
