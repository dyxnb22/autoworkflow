# Project Plan

## Product intent

`cc-loop` is a personal command-line workflow tool for coordinating configurable local coding agents on local repositories.

The product bet is simple: multiple agents are useful when their responsibilities are separated. A planner should plan, a reviewer should review, and an implementer should implement. `cc-loop` should keep the loop deterministic enough that the user can inspect, resume, and recover it.

## Core roles

- Planner: architecture, task slicing, prompt generation.
- Reviewer: code review, approval or rejection, retry direction.
- Implementer: concrete code edits in a checked-out worktree.
- `cc-loop`: state machine, CLI invocation, worktree lifecycle, test gates, logs, artifacts, merge policy.

`cc-loop` should not become a coding agent itself. It should not invent implementation details beyond prompt construction, process control, and safety gates.

## Provider model

The workflow roles are fixed, but the provider behind each role is configurable.

- `planner_provider`
- `reviewer_provider`
- `implementer_provider`

Built-in providers:

- `codex`: planner and reviewer roles
- `cursor`: implementer role
- `claude-code`: planner, reviewer, and implementer roles

The default v1 mapping:

- `planner_provider = codex`
- `reviewer_provider = codex`
- `implementer_provider = cursor`

`cc-loop` should own the role contract and normalized outputs. Providers should be implemented as adapters, not as arbitrary user-defined shell snippets in v1.

## v1 scope

The first useful version should be a conservative local loop:

- initialize a task state file from a goal and target repo (`cc-loop init`)
- accept provider, test command, and merge policy overrides at init time via CLI flags
- verify the target repo is a git repo and clean before starting
- create one isolated git worktree per iteration or retry
- call the configured planner provider in non-interactive mode for planning
- call the configured implementer provider in non-interactive mode for implementation
- run a configured test command
- call the configured reviewer provider in non-interactive mode for review
- automatically merge only when review approves and tests pass
- persist enough state to resume or debug a failed run
- leave rejected or failed worktrees inspectable unless explicitly cleaned
- `cc-loop resume` to continue after interruption without corrupting history
- `cc-loop auto` to run unattended until the task is done, with macOS notifications

## Required state

Each run should persist machine-readable state under `~/.cc-loop/tasks/<task-id>/state.json` or a user-selected state path. The default should be outside the target repository so artifact writes do not make the target repo dirty.

Minimum top-level fields:

- `task_id`
- `goal`
- `target_repo`
- `base_branch`
- `base_commit`
- `status`
- `iteration`
- `config`
- `history`
- `providers`

Minimum per-attempt fields:

- `iteration`
- `retry`
- `created_at`
- `base_commit`
- `head_commit`
- `branch`
- `worktree_path`
- `plan_raw_path`
- `plan_json`
- `plan_provider`
- `implementer_prompt_path`
- `implementer_raw_path`
- `implementer_exit_code`
- `implementer_provider`
- `test_command`
- `test_exit_code`
- `test_status`
- `test_raw_path`
- `diff_stat_path`
- `diff_patch_paths`
- `review_raw_path`
- `review_json`
- `review_provider`
- `decision`
- `merge_error`
- `merge_output_path`

Raw model output should be stored as artifacts, even when JSON parsing fails.

## Built-in provider contracts

The v1 design is based on the CLI shape observed on this machine on 2026-06-30.

Default Codex adapter:

```bash
codex exec --cd /path/to/worktree --json -o /path/to/last-message.txt -
```

Use stdin for the prompt. `-p` must not be used for print/headless mode because it is `--profile` in the current Codex CLI.

Default Cursor adapter:

```bash
cursor agent -p --output-format json --trust --workspace /path/to/worktree "prompt text"
```

`--output-format json` is valid only with `-p/--print`. `--no-interactive` is not part of the current Cursor CLI.

Default claude-code adapter:

```bash
# planner / reviewer (print-only):
claude --dangerously-skip-permissions --print [-m model] -p "prompt text"

# implementer (makes direct edits in worktree):
claude --dangerously-skip-permissions [-m model] -p "prompt text"
```

Planner/reviewer: JSON is extracted from stdout (fenced code block or bare object). Preflight: `claude --version`.

All built-in providers must normalize their prompts, outputs, timeout handling, and result parsing behind the same role contracts.

## Safety rules

- Use `subprocess.run([...], shell=False)` or `subprocess.Popen([...], shell=False)`.
- Never build command strings that embed model prompts.
- Do not use `cursor agent "$(cat file)"`.
- Do not use `pkill -f` for timeout cleanup.
- Track the child process object and terminate only that process group on timeout.
- Refuse to start when the target repo has uncommitted changes, unless an explicit future override is added.
- Use git worktrees by default. Do not switch branches in the user's main checkout.
- Do not auto-merge when tests fail.
- Do not feed unbounded full diffs to a reviewer model.
- Do not accept arbitrary user-supplied shell templates as providers in v1.

## Diff policy

Review context should be bounded:

1. Always include `git diff --stat`.
2. Include patches for changed files up to a configured byte limit.
3. Prefer source files and tests over generated or lock files.
4. Store full diff artifacts locally for user inspection.
5. If the patch exceeds the model limit, split by file or ask the configured reviewer to review a summarized attempt.

## Merge policy

Default auto-merge requirements:

- The implementer command exits successfully.
- Test command exits `0`, unless tests are explicitly disabled.
- The reviewer returns `approve`.
- The worktree branch can be merged into the resolved `base_branch` without switching the user's main checkout to some other branch.

If any requirement fails, `cc-loop` should stop, retry, or leave the branch for manual inspection according to config.
Merge failures should persist concrete diagnostics in deterministic artifacts so `status` and `resume` have something actionable to point at.

## Out of scope for v1

- remote workers
- hosted dashboard
- scheduling/nightly automation
- multi-agent chat between providers
- automatic conflict resolution
- automatic destructive cleanup
- arbitrary command execution exposed to the user

## Success criteria

v1 is good enough when it can complete a small real repository task with:

- no edits to the main checkout during implementation
- clear artifacts for each model call
- deterministic test gating
- resumable state after interruption
- no global process cleanup side effects
- a readable final summary that says what merged, what failed, and where artifacts live
