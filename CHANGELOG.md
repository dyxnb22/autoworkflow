# Changelog

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
  - `--test-command`, `--planner`, `--reviewer`, `--implementer`, `--allow-merge-without-tests`, `--max-iterations`, `--max-retries`, `--base-branch`, `--task-id` flags
- `cc-loop run` — execute one planning → implementation → test → review → merge iteration
- `cc-loop resume` — continue an interrupted or stopped attempt without corrupting history
- `cc-loop auto` — run unattended until done, with retry-exhaustion detection and macOS notifications
  - `--max-iterations` override flag
- `cc-loop status` — show current phase, decision, artifact paths, and next-action hint
- `--state-root` global flag to override `~/.cc-loop`
- Provider adapters: `codex` (planner, reviewer), `cursor` (implementer), `claude-code` (planner, reviewer, implementer)
- State machine with full phase tracking: `preflight` → `planning` → `worktree_created` → `executing` → `testing` → `reviewing` → `approved`/`rejected`/`merged`/`failed`
- Isolated git worktree per attempt; merge via ephemeral worktree to avoid switching user's checkout
- Bounded diff collection for reviewer context (`max_review_patch_bytes`)
- Timeout-safe subprocess handling with process-group kill (no `pkill -f`)
- Legacy state migration for renamed fields (`cursor_*` → `implementer_*`)
- `cc-loop resume` from `approved` phase retries merge after merge failure
- Retry from base commit after reviewer `reject`; reviewer `stop` leaves worktree inspectable

### Config defaults

```json
{
  "planner_provider": "codex",
  "reviewer_provider": "codex",
  "implementer_provider": "cursor",
  "codex_timeout_seconds": 300,
  "cursor_timeout_seconds": 900,
  "claude_code_timeout_seconds": 600,
  "test_timeout_seconds": 600,
  "max_iterations": 10,
  "max_retries_per_step": 2,
  "auto_merge": true,
  "allow_merge_without_tests": false,
  "max_review_patch_bytes": 60000
}
```

### Known limitations (v0.2.0)

- macOS notifications only (uses `osascript`); no equivalent on Linux/Windows
- No automatic worktree cleanup; failed/rejected worktrees are left for manual inspection
- `--state-root` is a global flag and must precede the subcommand (`cc-loop --state-root PATH list`, not `cc-loop list --state-root PATH`)

### Known limitations (v0.1.0, historical)
- `run`, `resume`, `status`, `auto` auto-detect the most recently modified task; there is no `--task-id` selector on these commands
- Model names (`codex_model`, `cursor_model`, `claude_code_model`) and `cursor_force`/`cursor_sandbox` must be set by editing `state.json` directly after `init`
- macOS notifications only (uses `osascript`); no equivalent on Linux/Windows
- No automatic worktree cleanup; failed/rejected worktrees are left for manual inspection
- Single active task at a time per `--state-root`
