# cc-loop v1 Technical Design

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

Initial built-in provider support for v1:

- `codex`: planner and reviewer
- `cursor`: implementer

The default v1 mapping is:

```json
{
  "planner_provider": "codex",
  "reviewer_provider": "codex",
  "implementer_provider": "cursor"
}
```

Future built-in adapters such as `claude-code` are in scope only if they can implement the same role contracts and safety rules. v1 should not support arbitrary shell-defined providers.

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
      cursor.prompt.txt
      cursor.raw.json
      cursor.provider.txt
      test.output.txt
      diff.stat.txt
      diff.files.txt
      patches/
      review.prompt.txt
      review.raw.jsonl
      review.last-message.txt
      review.parsed.json
      review.provider.txt
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
5. Verify `codex exec --help` succeeds.
6. Verify `cursor agent --help` succeeds.
7. Verify configured `test_command`, if present, is an argv list or a trusted command string parsed without shell.

Provider-specific preflight checks should run only for configured providers. For example, `codex exec --help` is required only when `codex` is configured for at least one role.

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

After Cursor edits:

```bash
git -C worktree_path status --porcelain
git -C worktree_path add -A
git -C worktree_path commit -m "cc-loop: iter 001"
```

After approval and passing tests:

```bash
git -C target_repo merge --no-ff cc-loop/<task-id>/iter-001
```

The main checkout remains on its original branch throughout implementation.

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
  "max_iterations": 10,
  "max_retries_per_step": 2,
  "codex_timeout_seconds": 300,
  "cursor_timeout_seconds": 900,
  "test_timeout_seconds": 600,
  "planner_provider": "codex",
  "reviewer_provider": "codex",
  "implementer_provider": "cursor",
  "codex_model": "",
  "cursor_model": "",
  "base_branch": "main",
  "auto_merge": true,
  "allow_merge_without_tests": false,
  "max_review_patch_bytes": 60000,
  "cursor_force": false,
  "cursor_sandbox": ""
}
```

## v1 implementation milestones

1. State file and artifact writer.
2. Preflight checks.
3. Provider adapter interface and built-in provider registry.
4. Worktree creation and cleanup commands.
5. Default Codex planner call with JSON parsing.
6. Default Cursor implementation call with timeout-safe process handling.
7. Test runner.
8. Bounded diff collector.
9. Default Codex reviewer call.
10. Merge/retry/stop state transitions.
11. `status` and `resume` commands.

## Open decisions

- Whether a future version should support repo-local state/worktree overrides in addition to the v1 central `~/.cc-loop` default.
- Whether `cc-loop init` should create a git repo for this workflow project itself.
- Whether review should use `codex exec` with a custom JSON schema file once the schema stabilizes.
- Whether retry attempts should branch from the failed attempt or restart from the original base commit. v1 should restart from the base commit for cleaner review.
- Which additional built-in providers beyond `codex` and `cursor` are stable enough to support in the first public iteration.
