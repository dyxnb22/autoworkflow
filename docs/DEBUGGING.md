# Debugging cc-loop

## Quick orientation

When something goes wrong, start with:

```bash
cc-loop status [--task-id ID]
# or machine-readable:
cc-loop status --task-id ID --json
```

For detached runs (`auto --detach`), also check:

```text
~/.cc-loop/tasks/<task-id>/runner.pid   # background child PID (removed when child exits)
~/.cc-loop/tasks/<task-id>/runner.log   # child stdout/stderr
```

Poll `running` / `runner_pid` in `status --json`. Integration contract: [INTEGRATION.md](INTEGRATION.md).

The human `status` output `next:` line tells you what cc-loop thinks should happen next. The `phase:` and `decision:` lines tell you where the attempt stopped.

State file: `~/.cc-loop/tasks/<task-id>/state.json`
Artifacts:  `~/.cc-loop/tasks/<task-id>/artifacts/iter-NNN[-retry-NN]/`
Worktree:   `~/.cc-loop/worktrees/<repo-name>/<task-id>/iter-NNN[-retry-NN]/`

## Reading the artifacts

Every phase writes deterministic files. Read them in order to trace what happened.

### Planning phase

| File | What it contains |
|---|---|
| `plan.prompt.txt` | The exact prompt sent to the planner |
| `plan.provider.txt` | Which provider was invoked |
| `plan.raw.jsonl` | Raw provider output (JSONL or plain text) |
| `plan.last-message.txt` | The final assistant message |
| `plan.parsed.json` | Normalized planner JSON after parsing |

If `plan.parsed.json` is missing or empty, planning failed before or during JSON parse. Check `plan.raw.jsonl` for the raw output and `plan.last-message.txt` for what the model actually returned.

### Implementer phase

| File | What it contains |
|---|---|
| `implementer.prompt.txt` | The exact prompt sent to the implementer |
| `implementer.provider.txt` | Which provider was invoked |
| `implementer.raw.json` | Raw provider output |

The implementer edits files in the worktree directly — its output artifact is a log, not the changes themselves. To inspect changes:

```bash
git -C ~/.cc-loop/worktrees/<repo>/<task-id>/iter-NNN diff HEAD
git -C ~/.cc-loop/worktrees/<repo>/<task-id>/iter-NNN status
```

### Test phase

| File | What it contains |
|---|---|
| `test.output.txt` | Command, cwd, exit_code, timed_out, stdout, stderr |

`test_status` in `state.json` is one of: `passed`, `failed`, `skipped`, `timed_out`.

### Diff phase

| File | What it contains |
|---|---|
| `diff.stat.txt` | `git diff --stat` + porcelain status across working tree, staged, and base..HEAD |
| `diff.files.txt` | Changed file names only |
| `patches/` | Per-file `.patch` files collected for the reviewer |

### Review phase

| File | What it contains |
|---|---|
| `review.prompt.txt` | The exact prompt sent to the reviewer (includes diff stat and selected patches) |
| `review.provider.txt` | Which provider was invoked |
| `review.raw.jsonl` | Raw provider output |
| `review.last-message.txt` | The final assistant message |
| `review.parsed.json` | Normalized reviewer JSON: decision, reason, issues, retry_prompt |

### Merge phase

| File | What it contains |
|---|---|
| `merge.output.txt` | merge_target, source_branch, target_head, result — or git error text |

## Common failure modes

### Planner fails to parse JSON

Symptom: `phase: failed`, `plan.parsed.json` is missing or empty.

Look at `plan.last-message.txt`. The model likely responded in prose instead of JSON, or wrapped the JSON in extra text. The `claude-code` adapter strips fenced code blocks; `codex` expects raw JSON.

Fix: the planner prompt is in `plan.prompt.txt`. You can re-run manually to test:

```bash
codex exec --cd <worktree> --json -o /tmp/test-plan.txt - < plan.prompt.txt
```

### Implementer times out

Symptom: `implementer_exit_code` is non-zero, `test.output.txt` says `timed_out: True`.

The implementer ran longer than `cursor_timeout_seconds` (or `claude_code_timeout_seconds`). Increase the timeout in `state.json`:

```json
"cursor_timeout_seconds": 1800
```

Then run `cc-loop resume`.

### Tests fail

Symptom: `test_status: failed`, `status: stopped`.

Check `test.output.txt` for the test output. The worktree is still intact at `worktree_path`. You can run the tests manually:

```bash
cd <worktree_path>
pytest tests/ -v
```

After manual fixes (if any), stage and commit in the worktree, then `cc-loop resume`. The reviewer will still run and can produce a `retry_prompt` for the next attempt.

### Reviewer rejects

Symptom: `decision: reject`, `status: stopped`.

Check `review.parsed.json` for `retry_prompt` and `issues`. Run `cc-loop resume` to start a retry from the base commit with the reviewer's feedback fed back to the planner.

Retries remaining = `max_retries_per_step - attempt.retry`. When exhausted, `cc-loop auto` stops; `cc-loop resume` raises an error.

### Merge fails

Symptom: `merge_error` is set in `state.json`, `merge.output.txt` contains git output.

The worktree branch was approved and tests passed, but the merge into `base_branch` failed (typically a conflict).

Options:

1. Manually resolve conflicts in the worktree, commit, then `cc-loop resume`.
2. Manually merge the branch from the worktree into the base branch, then mark the task done by editing `state.json`:
   ```json
   "status": "done"
   ```

### Worktree already exists

Symptom: error `worktree path already exists`.

A previous attempt left a worktree behind. Either:

- Remove it: `git -C <target_repo> worktree remove --force <worktree_path>`
- Or prune stale entries: `git -C <target_repo> worktree prune`

Then `cc-loop resume`.

### Provider not found / preflight fails

Symptom: error `unknown provider: <name>` or `provider preflight check failed`.

Verify the binary is on `PATH`:

```bash
codex exec --help
cursor agent --help
claude --version
```

If using `claude-code`, ensure `claude` CLI is installed and authenticated.

## Manually advancing stuck state

If cc-loop is stuck in a phase that cannot continue automatically, you can edit `state.json` directly.

**Skip a failed attempt and start fresh:**

```json
{
  "status": "stopped",
  "history": [
    { ..., "phase": "failed" }
  ]
}
```

Then `cc-loop run` starts iteration 2.

**Mark a task done after manual merge:**

```json
{
  "status": "done",
  "history": [
    { ..., "phase": "merged" }
  ]
}
```

**Reset to initialized to restart from scratch:**

```json
{
  "status": "initialized",
  "iteration": 0,
  "history": []
}
```

Then `cc-loop run`.

Always back up `state.json` before editing manually.

## Inspecting the worktree

The worktree is a normal git checkout. You can cd into it, run the test suite, make manual edits, and commit. cc-loop will pick up committed changes when you `cc-loop resume`.

```bash
cd ~/.cc-loop/worktrees/<repo-name>/<task-id>/iter-NNN
git log --oneline
git diff HEAD~1
```

## Checking what cc-loop auto will do next

```bash
cc-loop status
```

The `next:` line shows the next intended action. The state machine is:

- `initialized` → `cc-loop run` starts iter 1
- `stopped` + last phase `rejected` + retries remain → `cc-loop resume` starts a retry
- `stopped` + last phase `rejected` + retries exhausted → manual intervention or re-init
- `stopped` + last phase `approved` + merge_error → `cc-loop resume` retries merge
- `stopped` + decision `stop` → reviewer explicitly halted; inspect and decide
- `done` + `is_final_step: false` + iterations remain → `cc-loop auto` starts next iteration
- `done` + `is_final_step: true` → task complete
- `failed` → inspect artifacts; re-init or manually fix state
