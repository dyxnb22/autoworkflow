# Operations guide

Recovery, debugging, and advanced task graphs. Product overview: [README.md](../README.md). Integrator contract: [INTEGRATION.md](INTEGRATION.md).

## First checks

```bash
cc-loop status --task-id ID --json
cc-loop summary --task-id ID --json
```

Inspect `success`, `roles`, `attempt`, `next_action`, and optional `failure`.

Detached runs:

```text
~/.cc-loop/tasks/<id>/runner.pid
~/.cc-loop/tasks/<id>/runner.log
~/.cc-loop/tasks/<id>/runner.heartbeat.json
~/.cc-loop/tasks/<id>/state.json
~/.cc-loop/tasks/<id>/artifacts/iter-NNN[-retry-NN]/
~/.cc-loop/worktrees/<repo>/<id>/iter-NNN[-retry-NN]/
```

## Recovery principles

- **Recoverable** → retry / implementer repair within budgets
- **Terminal** → stop with `failure.report.json` and `status --json.failure`
- Every failure gets a `failure_type` and `disposition`
- Reviewer `reject` → implementer resume while retries remain
- Failed/skipped tests cannot become success unless `allow_merge_without_tests`

### Failure types (summary)

| Type | Default disposition |
|------|---------------------|
| `merge_conflict` / `merge_worktree_busy` | recoverable (only if `auto_merge`) |
| `merge_branch_missing` / `merge_permission` | terminal |
| `test_implementation` | recoverable |
| `test_gate_blocked` / `test_environment` | terminal / inspect |
| `provider_timeout` / `provider_exit_error` | recoverable |
| `reviewer_reject` / `reviewer_stop_fixable` | recoverable |
| `reviewer_stop_terminal` / `recovery_budget_exhausted` | terminal |

### Budgets (config)

`max_merge_retries`, `max_merge_recovery_attempts`, `max_recovery_attempts_per_iteration` (and optional wall-clock / consecutive-failure caps). `0` = unlimited where applicable.

### Approve gate

On `decision=approve`:

- Persist approve immediately; clear stale recoverable failures that would re-trigger repair
- Default (`auto_merge=false`): handoff-ready if tests green
- Opt-in (`auto_merge=true`): merge into base after the same test gate
- If tests failed/skipped without escape hatch → `test_gate_blocked`, `next_action=inspect`

### Non-goals

No `git reset --hard` / `clean -fd`, no auto-stash of the user’s dirty main tree, no infinite flaky-test loops.

## Debugging

| Phase | Key artifacts |
|-------|----------------|
| Plan | `plan.prompt.txt`, `plan.last-message.txt`, `plan.parsed.json` |
| Implement | `implementer.prompt.txt`, `implementer.raw*`; inspect worktree with `git status` / `git diff` |
| Test | `test.output.txt` (`passed` / `failed` / `skipped` / `timed_out`) |
| Diff | `diff.stat.txt`, `diff.files.txt`, `patches/` |
| Review | `review.parsed.json` (`decision`, `reason`, `retry_prompt`) |
| Merge | `merge.output.txt` (only when `auto_merge`) |

Common fixes:

- Planner JSON missing → read `plan.last-message.txt`; re-run provider with `plan.prompt.txt`
- Implementer timeout → raise role timeout in config / `state.json`
- Tests fail → fix in worktree or `resume` for repair
- Reviewer reject → `resume` feeds reject reason into implementer
- Merge conflict → only with `--auto-merge`; repair or resolve manually then resume

Control:

```bash
cc-loop stop --task-id ID
cc-loop cancel --task-id ID
cc-loop cleanup --task-id ID
cc-loop resume --task-id ID
```

## Task graphs (advanced)

Default path is a **single closed loop** (`planner_granularity=single`). Multi-node graphs and parallel execution are opt-in.

Enable decomposition with `--planner-granularity graph` (or `auto`). Planner may return:

```json
{
  "mode": "task_graph",
  "summary": "…",
  "nodes": [
    {"id": "n1", "title": "…", "goal": "…", "depends_on": []}
  ]
}
```

Legacy single-step JSON is wrapped into a one-node graph.

### Node statuses

`pending` · `ready` · `running` · `blocked` · `done` · `failed` · `skipped` · `cancelled`

A node runs when dependencies are `done` and status is runnable. Terminal dependency failure marks dependents `blocked`.

### Parallel

Requires `allow_parallel_execution=true` **and** `max_parallel_nodes > 1`. Independent ready nodes run concurrently; merges (if enabled) serialize via the merge queue.

### Inspect

```bash
cc-loop graph --task-id ID
cc-loop graph --task-id ID --json
cc-loop graph --task-id ID --history
```

### Replan / routing

Reviewer `decision: replan` may apply a planner `graph_patch`. Nodes may override providers/policies when configured; task-level safety gates still apply unless explicitly weakened.
