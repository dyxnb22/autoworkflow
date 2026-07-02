# cc-loop Evolution Roadmap

This document is the planning and implementation guide for evolving cc-loop
from the current v0.4 task-graph orchestrator into a reliable local autonomous
development loop.

The product boundary is intentionally narrow:

- cc-loop is the local execution loop.
- External apps consume cc-loop through CLI and JSON contracts.
- Enterprise governance, RBAC, approvals, and permission-aware RAG belong in a
  higher platform layer, not inside cc-loop core.

## Version Themes

| Version | Theme | Goal |
|---|---|---|
| v0.4.x | Contract stabilization | Keep task graph behavior stable and integration-safe. |
| v0.5 | Unattended reliability | Make overnight runs controllable and recoverable. |
| v0.6 | Reports and event stream | Make completed or failed runs easy to understand. |
| v0.7 | Dynamic replanning | Let planner revise the graph from test/review feedback. |
| v0.8 | Multi-role routing | Route different node kinds to different providers/reviewers. |
| v0.9 | Parallel execution | Run independent graph nodes concurrently behind a merge queue. |

## Design Principles

1. **Stable outside, iterative inside.** External consumers should depend only
   on documented CLI commands and JSON schemas in `docs/INTEGRATION.md`.
2. **Additive JSON changes by default.** Add optional fields for minor versions;
   bump integration schema only for breaking changes.
3. **State is the source of truth.** Runtime behavior must be derivable from
   `state.json`, artifacts, and documented runner files.
4. **No direct main-worktree edits.** Implementers always work in isolated git
   worktrees.
5. **Tests gate merge.** Do not merge failed or skipped tests unless the
   existing explicit policy allows it.
6. **Recovery is centralized.** `auto` must use `recovery.decide_auto_step`;
   avoid ad-hoc phase checks in CLI loops.
7. **Provider output is untrusted.** Parse, validate, bound, and persist model
   outputs before using them.
8. **No global process killing.** Process control must use task-owned runner
   metadata and PID validation.

## v0.4.x: Contract Stabilization

### Objective

Harden the current v0.4 task graph system without changing its core behavior.
This lane should be safe for existing integrations.

### Scope

- Keep the existing planner -> graph -> node execution path stable.
- Preserve legacy single-step planner JSON wrapping.
- Improve provider JSON parsing only when tests capture the behavior.
- Fix stale failure reports, status inconsistencies, and integration edge cases.
- Add contract tests around `status --json`, `graph --json`, `list --json`, and
  detached `auto` output.

### Out of Scope

- New graph mutation features.
- New runner control commands except small compatibility fixes.
- Parallel execution.

### Acceptance Criteria

- `python -m pytest tests/ -q` passes.
- Existing v0.3 state files still load.
- Existing v0.4 task graph states still load.
- `status --json` and `graph --json` remain backward-compatible.
- `docs/INTEGRATION.md` stays accurate.

## v0.5: Unattended Reliability

### Objective

Make cc-loop safe and understandable enough to run unattended for many hours.

### Required Capabilities

1. **Runner control commands**
   - `cc-loop stop --task-id ID [--json]`
   - `cc-loop cancel --task-id ID [--json]`
   - `cc-loop cleanup --task-id ID [--json]`

2. **Runner liveness**
   - Write `<task-dir>/runner.heartbeat.json` from detached `auto`.
   - Include task id, pid, started timestamp, updated timestamp, current phase,
     current graph node id, and loop iteration.
   - Detect stale PID files and stale heartbeats.

3. **Status contract additions**
   - Add optional fields to `status --json`:
     - `runner_state`
     - `last_heartbeat_at`
     - `runner_started_at`
     - `elapsed_seconds`
     - `can_stop`
     - `can_resume`
     - `can_cleanup`
     - `log_path`
   - Existing fields must keep their meaning.

4. **Budgets**
   - Maximum wall-clock duration.
   - Maximum consecutive failures.
   - Maximum artifact/log byte size.
   - Maximum changed file count per attempt.
   - Clear terminal failure reports when budgets are exhausted.

5. **Cleanup**
   - Remove stale runner pid files.
   - Remove task-owned worktrees when cleanup is requested.
   - Never delete unrelated user worktrees or branches.

### Suggested Modules

- `runner_control.py` for stop/cancel/cleanup helpers.
- `runner_heartbeat.py` for heartbeat read/write/staleness logic.
- `inspect.py` for additive status fields.
- `cli.py` for new commands only; keep business logic out of CLI parsing.

### Acceptance Criteria

- Detached runner writes and refreshes heartbeat.
- `status --json` reports stopped/stale runners accurately.
- `stop --json` terminates only the task-owned runner.
- `cancel --json` marks the task terminal without corrupting artifacts.
- `cleanup --json` removes task-owned temporary runtime artifacts.
- Targeted tests cover stale PID, stale heartbeat, stop, cancel, cleanup, and
  additive JSON fields.
- Full test suite passes.

## v0.6: Reports and Event Stream

### Objective

Make every run auditable and easy to inspect after completion or failure.

### Required Capabilities

1. **Event stream**
   - Write `<task-dir>/events.jsonl`.
   - Include stable event types:
     - `task.initialized`
     - `runner.started`
     - `runner.stopped`
     - `planner.started`
     - `planner.completed`
     - `graph.node_started`
     - `implementer.started`
     - `implementer.completed`
     - `tests.started`
     - `tests.completed`
     - `reviewer.started`
     - `reviewer.completed`
     - `repair.started`
     - `repair.completed`
     - `merge.started`
     - `merge.completed`
     - `failure.recorded`
     - `task.completed`

2. **Report command**
   - `cc-loop report --task-id ID`
   - `cc-loop report --task-id ID --json`
   - Include graph progress, completed nodes, failed nodes, tests, review
     decisions, merge results, failure summary, and artifact paths.

3. **Current message**
   - Add optional `current_message` to `status --json`.
   - Keep the message short and UI-friendly.

### Suggested Modules

- `events.py` for append-only JSONL event writes and reads.
- `report.py` for report model and renderers.
- `inspect.py` for `current_message`.

### Acceptance Criteria

- Every major phase emits events once and in order.
- Report JSON is deterministic and covered by contract tests.
- Human report is readable in a terminal.
- Full test suite passes.

## v0.7: Dynamic Replanning

### Objective

Let the planner revise the task graph when implementation, tests, or review show
that the original plan is wrong or incomplete.

### Required Capabilities

1. **Reviewer decision**
   - Extend reviewer output with `decision: "replan"`.
   - Require a `replan_reason` and suggested graph change summary.

2. **Graph patch model**
   - Support planner output for graph patches:
     - add node
     - update node title/description/acceptance/files scope
     - add dependency
     - remove dependency from non-passed nodes
     - skip node with reason
   - Do not replace the entire graph by default.

3. **Graph patch validation**
   - Forbid deleting passed nodes.
   - Forbid changing passed node acceptance criteria.
   - Forbid dependency cycles.
   - Forbid marking a node passed without a successful attempt.
   - Forbid silently unblocking nodes whose dependency failed terminally.

4. **Graph history**
   - Write `graph_events.jsonl`.
   - Add `cc-loop graph --history [--json]`.

### Suggested Modules

- `graph_patch.py` for patch models, validation, and application.
- `task_graph.py` for only graph primitives that are not patch-specific.
- `run.py` for replanning phase orchestration.

### Acceptance Criteria

- Reviewer `replan` leads to planner graph patch request.
- Valid graph patches apply and persist.
- Invalid graph patches fail terminally with actionable failure reports.
- `graph --history --json` exposes graph mutation events.
- Legacy approve/reject/stop behavior remains unchanged.
- Full test suite passes.

## v0.8: Multi-Role Routing

### Objective

Use graph metadata to route different work to different providers and review
strategies.

### Required Capabilities

1. **Per-node routing**
   - Optional node-level provider overrides:
     - `planner_provider`
     - `implementer_provider`
     - `reviewer_provider`
   - Fallback to task-level config.

2. **Node policy**
   - Optional node-level controls:
     - `test_policy`
     - `merge_policy`
     - `max_changed_files`
     - `requires_manual_review`
   - Policies must not weaken task-level safety defaults unless explicitly
     allowed by config.

3. **Multi-reviewer support**
   - Allow review chains such as code review then test review.
   - Aggregate reviewer decisions conservatively: any terminal stop stops; any
     reject rejects; all required reviewers must approve.

4. **Prompt templates**
   - Move role prompts toward template functions or files with test coverage.
   - Keep defaults embedded and deterministic.
   - Project-level overrides should be explicit and bounded.

### Acceptance Criteria

- A single graph can route docs, tests, implementation, and review nodes to
  different providers.
- Status and graph JSON expose effective provider names additively.
- Multi-reviewer aggregation is tested.
- Existing single-reviewer tasks behave unchanged.
- Full test suite passes.

## v0.9: Parallel Execution

### Objective

Run independent graph nodes concurrently while preserving deterministic state and
safe merges.

### Required Capabilities

1. **Concurrent scheduling**
   - Discover all runnable nodes.
   - Respect `max_parallel_nodes`.
   - Create one worktree/attempt per running node.

2. **State locking**
   - Use a file lock around state reads/writes.
   - Use atomic writes.
   - Detect stale locks without corrupting state.

3. **Merge queue**
   - Approved nodes enter a merge queue.
   - Merge queue runs serially.
   - Merge conflict repair remains node-scoped.

4. **Failure isolation**
   - Failed nodes block only downstream dependents.
   - Independent nodes may continue.
   - Terminal task status requires the graph to be complete or unrecoverably
     blocked.

### Acceptance Criteria

- Multiple independent nodes can run simultaneously.
- Merge queue preserves base branch integrity.
- State remains valid under concurrent runners.
- Failed nodes block dependents but not unrelated nodes.
- Full test suite includes concurrency tests and passes reliably.

## Coding Guidance for Implementers

Use this section when asking an implementation agent to change cc-loop.

### Before Editing

- Read `AGENTS.md`, `README.md`, `docs/INTEGRATION.md`,
  `docs/TASK_GRAPH.md`, `docs/RECOVERY.md`, and this document.
- Inspect `git status --short --branch`.
- Identify the target version lane and keep changes scoped to that lane.
- Preserve unrelated user changes.

### Implementation Style

- Prefer small modules with plain dataclasses/enums over large framework
  dependencies.
- Keep CLI parsing thin; put orchestration logic in testable modules.
- Preserve backward compatibility for old state files.
- Additive JSON fields must be optional for consumers.
- Do not parse artifact files from external integrations as a primary contract.
- Use structured JSON models for state, reports, graph patches, events, and
  failure reports.
- Keep subprocess handling timeout-safe.
- Validate provider outputs before they affect state or git.

### Testing Expectations

- Add unit tests for new pure logic.
- Add CLI contract tests for new commands and JSON fields.
- Add integration-style tests with fake providers for run-loop behavior.
- Run:

```bash
python -m pytest tests/ -q
```

For narrow changes, run the targeted tests first, then the full suite before
completion.

### Documentation Expectations

- Update `docs/INTEGRATION.md` for any external CLI/JSON contract change.
- Update `docs/TASK_GRAPH.md` for graph behavior changes.
- Update `docs/RECOVERY.md` for recovery or failure classification changes.
- Update this roadmap when scope or version order changes.

### Safety Checks

- No automatic merge when tests fail unless existing explicit config allows it.
- No global process killing.
- No destructive git commands.
- No direct edits in the user's main worktree.
- No hidden network or cloud coordinator.
- No unbounded diffs or logs in model prompts.
