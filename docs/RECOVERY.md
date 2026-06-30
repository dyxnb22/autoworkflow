# Recovery and failure handling (v0.3)

cc-loop classifies failures, applies retry budgets, and routes recoverable issues through implementer repair prompts. It does not perform destructive git operations or auto-resolve merge conflicts by picking sides.

## Principles

- **Recoverable:** orchestrator retries or invokes implementer with a structured repair prompt.
- **Terminal:** loop stops with structured `failure.report.json` and `status --json.failure`.
- **No blind continuation:** every failure gets a `failure_type` and `disposition`.
- **Budgets:** `max_merge_retries`, `max_merge_recovery_attempts`, `max_recovery_attempts_per_iteration`.

## Failure types

| Type | Typical cause | Default disposition |
|------|---------------|---------------------|
| `merge_conflict` | Git merge conflict | recoverable → implementer repair |
| `merge_worktree_busy` | Base branch checked out elsewhere | recoverable → merge retry |
| `merge_branch_missing` | Missing branch/revision | terminal |
| `merge_permission` | Permission denied | terminal |
| `test_implementation` | Assertion/test logic failure | recoverable → implementer repair |
| `test_environment` | Import/module missing | terminal |
| `provider_timeout` | Agent timed out | recoverable |
| `provider_exit_error` | Agent non-zero exit | recoverable |
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
  "recovery_retry_backoff_seconds": 0
}
```

## Artifacts

Each attempt may write:

- `failure.report.json` — full structured report
- Existing phase artifacts (`test.output.txt`, `merge.output.txt`, etc.)

## `status --json`

Additive fields:

- `failure` object with `failure_type`, `disposition`, `stop_reason`, `attempted_repairs`, `suggested_actions`
- `next_action` may be `repair` or `terminal` in addition to existing values

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
