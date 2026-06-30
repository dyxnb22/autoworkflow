# Exit codes

cc-loop uses a small set of stable exit codes across commands.

| Code | Meaning |
|------|---------|
| 0 | Success, or an operational stop that is not an execution error |
| 1 | User or configuration error |
| 2 | Execution failure during planning, implementation, review, or terminal task failure |

## Exit code 0

Typical cases:

- `init`, `list`, `status`, `doctor` succeeded
- `run` completed with reviewer `reject` and retries remaining
- `run` completed with reviewer `stop` (state persisted for inspection)
- `run` stopped with merge blocked but state saved (`STOPPED` with `merge_error`)
- `auto --detach` parent exited after spawning the background runner
- `auto` child completed the task successfully

## Exit code 1

Typical cases:

- No task found under `--state-root`
- Explicit `--task-id` does not exist
- Preflight failed (dirty repo, missing provider, invalid test command, bad base branch)
- `resume` not allowed for the current state
- `doctor` checks failed
- `auto` hit max iterations, reviewer stop, merge failure notification path, or retry exhaustion

Note: `run` returns 0 on reviewer `stop`, while `auto` classifies stop via heuristics — fixable stops enter repair; terminal stops exit 1 with structured `failure.report.json`. Prefer `status --json` (`next_action`, `failure`) when polling detached runs.

## Exit code 2

Typical cases:

- Planner, implementer, or reviewer subprocess failed or timed out
- Task `status` is `failed` after `run` or `resume`
- `auto` child ended with task `failed`

## Commands and exit codes

| Command | 0 | 1 | 2 |
|---------|---|---|---|
| `init` | created | bad args / missing repo | — |
| `doctor` | ok | preflight failed | — |
| `list` | listed | — | — |
| `status` | shown | no / bad task | — |
| `run` | success / resumable stop | no task / preflight / run error | phase failure / task failed |
| `resume` | continued | no task / preflight / resume error | phase failure / task failed |
| `auto` | done | config stop / max iter / merge notify | task failed / phase failure |
| `auto --detach` | spawned | no / bad task | — |
