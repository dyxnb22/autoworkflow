# cc-loop v1 Technical Design

> **v0.2.0 note:** External integration (Luma / subprocess consumers) is documented separately in [INTEGRATION.md](INTEGRATION.md). This file describes the v1 internal design; it remains accurate for orchestration semantics.

## Summary

`cc-loop` runs a bounded local automation loop:

```text
state -> planner provider -> git worktree -> implementer provider -> tests -> reviewer provider -> merge/retry/stop -> state
```

The tool is intentionally boring: it should be easier to debug than a free-form agent conversation.

## Role and provider model

The workflow has three fixed roles:

- `planner`
- `reviewer`
- `implementer`

Each role is backed by a configured provider adapter.

Built-in providers:

- `codex`: planner and reviewer
- `cursor`: implementer
- `claude-code`: planner, reviewer, and implementer

The default mapping is:

```json
{
  "planner_provider": "codex",
  "reviewer_provider": "codex",
  "implementer_provider": "cursor"
}
```

All providers must implement the same role contracts and safety rules. Arbitrary shell-defined providers are not supported.

## State machine

Top-level statuses:

- `initialized`
- `running`
- `waiting_manual_review`
- `done`
- `failed`
- `stopped`
- `interrupted`

Attempt phases:

- `preflight`
- `planning`
- `worktree_created`
- `executing`
- `testing`
- `reviewing`
- `approved`
- `rejected`
- `merged`
- `failed`

## Filesystem layout

Default state directory:

```text
~/.cc-loop/tasks/<task-id>/
  state.json
  artifacts/
    iter-001/
      plan.prompt.txt
      plan.raw.jsonl
      plan.last-message.txt
      plan.parsed.json
      plan.provider.txt
      implementer.prompt.txt
      implementer.raw.json
      implementer.provider.txt
      test.output.txt
      diff.stat.txt
      diff.files.txt
      patches/
      review.prompt.txt
      review.raw.jsonl
      review.last-message.txt
      review.parsed.json
      review.provider.txt
      merge.output.txt
```

Default worktree directory:

```text
~/.cc-loop/worktrees/<repo-name>/<task-id>/iter-001
~/.cc-loop/worktrees/<repo-name>/<task-id>/iter-001-retry-01
```

The implementation may later support a repo-local state directory, but v1 should keep default artifacts outside the target repo so preflight cleanliness remains meaningful.

## Preflight checks

Before the first iteration:

1. Verify `target_repo` exists.
2. Verify `target_repo/.git` or `git -C target_repo rev-parse --show-toplevel` succeeds.
3. Verify `git status --porcelain` is empty.
4. Resolve `base_branch` and `base_commit`.
5. Run `preflight_check()` for each configured provider:
   - `codex`: `codex exec --help`
   - `cursor`: `cursor agent --help`
   - `claude-code`: `claude --version`
6. Verify configured `test_command`, if present, is a non-empty argv list with only non-empty strings.

Provider preflight checks run only for providers assigned to at least one role.

If the repo is dirty, stop with a clear message. Do not stash automatically in v1.

## Provider adapter contract

Each built-in provider adapter should implement:

- argument construction
- prompt delivery
- raw output capture
- timeout handling
- parsing into a normalized role result

`cc-loop` should depend on normalized role outputs, not on provider-specific output layouts.

Planner normalized output:

```json
{
  "prompt": "Detailed implementation prompt for the implementer provider",
  "expected_changes": "Expected files or areas",
  "acceptance_criteria": "How this step will be judged",
  "is_final_step": false
}
```

Reviewer normalized output:

```json
{
  "decision": "approve",
  "reason": "Why this attempt is acceptable or not",
  "issues": [],
  "retry_prompt": "",
  "stop_reason": ""
}
```

Implementer normalized result metadata should include at minimum:

- provider name
- exit code
- raw artifact path
- timeout or interrupted status
- optional summary text

## Default Codex planner call

Use stdin for prompts and file output for the final response:

```python
args = [
    "codex",
    "exec",
    "--cd", worktree_path,
    "--json",
    "-o", plan_last_message_path,
    "-",
]
```

Optional model:

```python
args.extend(["--model", config["codex_model"]])
```

Prompt expectations:

```json
{
  "prompt": "Detailed implementation prompt for the implementer provider",
  "expected_changes": "Expected files or areas",
  "acceptance_criteria": "How this step will be judged",
  "is_final_step": false
}
```

Parsing rule:

- Prefer parsing `plan.last-message.txt`.
- Store raw JSONL separately.
- If JSON parsing fails, mark the attempt failed and keep artifacts.

## Default Cursor implementation call

Use Cursor print mode:

```python
args = [
    "cursor",
    "agent",
    "-p",
    "--output-format", "json",
    "--trust",
    "--workspace", worktree_path,
    prompt_text,
]
```

Optional permissions should be conservative:

- Start without `--force`.
- Add `--sandbox disabled` only when the target environment requires it and the user explicitly enables that config.
- Add `--force` only as a separate opt-in.

Do not use `--no-interactive`; it is not a current Cursor Agent option.

## Default claude-code call

For planner and reviewer roles (print-only, no file edits):

```python
args = [
    "claude",
    "--dangerously-skip-permissions",
    "--print",
    "-p", prompt_text,
]
# run with cwd=worktree_path; capture stdout as last-message artifact
```

For implementer role (makes direct edits in the worktree):

```python
args = [
    "claude",
    "--dangerously-skip-permissions",
    "-p", prompt_text,
]
# run with cwd=worktree_path
```

Optional model:

```python
args.extend(["--model", config["claude_code_model"]])
```

Parsing rule for planner/reviewer: extract the first JSON object from stdout, stripping fenced code blocks if present.

Preflight check: `claude --version`.

Other providers should follow their own adapters, but they must still produce bounded artifacts and normalized attempt metadata.

## Process timeout handling

Use `subprocess.Popen` with a new process group:

```python
proc = subprocess.Popen(args, start_new_session=True, ...)
```

On timeout:

1. Send `SIGTERM` to that process group.
2. Wait a short grace period.
3. Send `SIGKILL` to that process group only if needed.
4. Record timeout in the attempt artifact.

Never use global cleanup such as `pkill -f 'cursor agent'`.

## Git workflow

Default flow:

```bash
git -C target_repo worktree add -b cc-loop/<task-id>/iter-001 <worktree_path> <base_commit>
```

After implementer edits:

```bash
git -C worktree_path status --porcelain
git -C worktree_path add -A
git -C worktree_path commit -m "cc-loop: iter 001"
```

After approval and passing tests:

```bash
git -C <base-branch-checkout> merge --no-ff cc-loop/<task-id>/iter-001
```

The merge target is the resolved `base_branch`. If the user's main checkout is already on that branch, merge there. Otherwise, create an ephemeral worktree for the base branch and merge there so the main checkout stays on its original branch.
If the merge fails, persist diagnostics in `merge.output.txt` and leave the attempt resumable.

## Test gate

Default behavior:

- If `test_command` is configured and exits non-zero, auto-merge is forbidden.
- The configured reviewer may still review the failed attempt to produce a retry prompt.
- A review `approve` cannot override failed tests.

If no `test_command` is configured:

- Mark tests as `skipped`.
- Allow auto-merge only if `allow_merge_without_tests` is true.
- Default `allow_merge_without_tests` should be false for code repositories.

## Review context

Generate:

```bash
git diff --stat base_commit...HEAD
git diff --name-only base_commit...HEAD
git diff base_commit...HEAD -- <selected-file>
```

Selection rules:

- Include changed source and test files first.
- Exclude generated files by default.
- Include lockfiles only as name/stat unless within size budget.
- Enforce `max_review_patch_bytes`.

Reviewer normalized output:

```json
{
  "decision": "approve",
  "reason": "Why this attempt is acceptable or not",
  "issues": [],
  "retry_prompt": "",
  "stop_reason": ""
}
```

Allowed decisions:

- `approve`
- `reject`
- `stop`

Any unknown decision is treated as `reject`.

## Initial config

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
  "max_review_patch_bytes": 60000,
  "codex_model": "",
  "cursor_model": "",
  "claude_code_model": "",
  "base_branch": "main",
  "cursor_force": false,
  "cursor_sandbox": ""
}
```

## v1 implementation status — complete

1. ✅ State file and artifact writer.
2. ✅ Preflight checks.
3. ✅ Provider adapter interface and built-in provider registry.
4. ✅ Worktree creation and cleanup commands.
5. ✅ Default Codex planner call with JSON parsing.
6. ✅ Default Cursor implementer call with timeout-safe process handling.
7. ✅ Test runner.
8. ✅ Bounded diff collector.
9. ✅ Default Codex reviewer call.
10. ✅ Merge/retry/stop state transitions.
11. ✅ `status` and `resume` commands.
12. ✅ `auto` command with macOS notifications and retry-exhaustion detection.
13. ✅ `claude-code` adapter for planner, reviewer, and implementer roles.
14. ✅ `cc-loop init` config flags (test-command, provider selection, merge policy, iteration limits).

## v1.1 integration contract (v0.2.0)

See [INTEGRATION.md](INTEGRATION.md) for the stable CLI subset (`doctor`, `list`, `status --json`, `auto --detach`, `--task-id`, `CC_LOOP_STATE_ROOT`) and JSON schemas.
