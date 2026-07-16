# Recovery and failure handling (v0.3+)

cc-loop classifies failures, applies retry budgets, and routes recoverable issues
through implementer repair prompts. It does not perform destructive git operations
or auto-resolve merge conflicts by picking sides.

> **Product note (v0.11):** Default success is handoff-ready work after green
> tests + review approve (`auto_merge=false`). Merge failures only apply when
> `--auto-merge` is enabled. `auto` refuses to start without `test_command`
> unless `allow_merge_without_tests` is explicit.

## Task graph context (advanced / v0.4+)

When a task uses a multi-node task graph, recovery applies to the **current graph
node** (`AttemptRecord.graph_node_id`). Downstream nodes are marked `blocked` when
a dependency fails terminally. See [TASK_GRAPH.md](TASK_GRAPH.md).

## Principles

- **Recoverable:** cc-loop retries or invokes the implementer with a structured repair prompt.
- **Terminal:** loop stops with structured `failure.report.json` and `status --json.failure`.
- **No blind continuation:** every failure gets a `failure_type` and `disposition`.
- **Budgets:** `max_merge_retries`, `max_merge_recovery_attempts`, `max_recovery_attempts_per_iteration`.
- **Reject → implement:** reviewer `reject` returns to the implementer while retries remain.
- **Tests gate delivery:** failed/skipped tests cannot become success; merge-without-tests is explicit only.

## Failure types

| Type | Typical cause | Default disposition |
|------|---------------|---------------------|
| `merge_conflict` | Git merge conflict (only when auto-merge is on) | recoverable → implementer repair |
| `merge_worktree_busy` | Base branch checked out elsewhere | recoverable → merge retry |
| `merge_branch_missing` | Missing branch/revision | terminal |
| `merge_permission` | Permission denied | terminal |
| `test_implementation` | Assertion/test logic failure | recoverable → implementer repair |
| `test_gate_blocked` | Reviewer approved but tests block finalize/merge | terminal → inspect / manual decision |
| `test_environment` | Import/module missing | terminal |
| `provider_timeout` | Agent timed out | recoverable |
| `provider_exit_error` | Agent non-zero exit | recoverable |
| `reviewer_reject` | Reviewer rejected; retries may remain | recoverable → implement resume |
| `reviewer_stop_fixable` | Stop with fixable retry_prompt | recoverable |
| `reviewer_stop_terminal` | Requirement/permission/external | terminal |
| `recovery_budget_exhausted` | Retry limits hit | terminal |

## Config keys

```json
{
  "max_merge_retries": 2,
  "max_merge_recovery_attempts": 2,
  "max_recovery_attempts_per_iteration": 3,
  "auto_recover_merge": true,
  "auto_recover_tests": true,
  "auto_recover_provider_errors": true,
  "recovery_retry_backoff_seconds": 0,
  "max_wall_clock_seconds": 0,
  "max_consecutive_failures": 0,
  "max_artifact_log_bytes": 0,
  "max_changed_files_per_attempt": 0,
  "auto_merge": false,
  "allow_merge_without_tests": false,
  "require_distinct_reviewer": true
}
```

`0` means unlimited for budget fields.

## Artifacts

Each attempt may write:

- `failure.report.json` — full structured report
- Existing phase artifacts (`test.output.txt`, `merge.output.txt`, etc.)

## `status --json` / `summary --json`

Additive recovery-facing fields:

- `failure` object with `failure_type`, `disposition`, `stop_reason`, `attempted_repairs`, `suggested_actions`
- `next_action` may be `repair` or `terminal` in addition to existing values
- `success` enum includes `ready_for_handoff`, `merged`, `stopped`, `failed`, …
- `summary --json` also exposes `latest_reject_reason` and `latest_attempt.retry`

## Non-goals

- `git reset --hard`, `checkout -- .`, `clean -fd`
- Auto stash of user changes in target repo
- Flaky test infinite retry
- Parsing every test framework (pytest text output first)

## Reviewer `stop` policy

`decision=stop` is **not** silently ignored. Heuristics classify:

- **Terminal:** stop_reason mentions requirement, permission, external dependency, etc.
- **Fixable:** non-empty `retry_prompt` or code/test-oriented issues → implementer repair

When ambiguous, default is **terminal** (conservative).

## Reviewer `reject` → implement retry

On `decision=reject` with retries remaining:

- Attempt phase becomes `rejected`
- `auto` / `resume` select `AutoStep.RESUME` and start a new implementer attempt from the base commit
- Rejection `reason` / `retry_prompt` are fed into the implementer prompt
- Exhausted retries become terminal (`stop_reason=retry_exhausted`)

## Reviewer approve gate (v0.9+)

When the reviewer returns `decision=approve`, cc-loop treats that as the quality gate for the current node/iteration:

- `attempt.decision`, `attempt.review_json`, and `attempt.phase=approved` are persisted immediately.
- Prior recoverable failures (for example `patch_not_captured`) are cleared so they cannot re-trigger auto repair.
- Auto recovery will **not** re-enter test/review for an approved attempt.

### Finalize after approve

- **Default (`auto_merge=false`):** tests must be green (unless `allow_merge_without_tests`); then the attempt is marked handoff-ready (`success=ready_for_handoff`) without merging into the user’s base branch.
- **Opt-in (`auto_merge=true`):** same test gate, then merge into the base branch (`success=merged` when complete).

### Tests failed but reviewer approved

If tests failed (or were skipped without `allow_merge_without_tests`) but the reviewer approved, finalize/merge is blocked and the task stops with `failure_type=test_gate_blocked` and `next_action=inspect`.

This is intentional. The loop does **not** spin on test/review; choose one of:

- Inspect artifacts (`test.output.txt`, `review.parsed.json`) and decide whether the failed/skipped tests should block delivery.
- Fix tests through a repair path before continuing.
- Set `allow_merge_without_tests=true` only when you explicitly accept skipped tests.

Stale `failure.report.json` files from earlier recovery attempts are ignored in `status --json` / `report` once the attempt is approved, except when merge failed or the test gate is actively blocking.
