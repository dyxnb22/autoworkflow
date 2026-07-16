# Task graphs (advanced, v0.4–v0.9)

> **Product note (v0.11):** The default delivery path is a **single closed loop**
> (`planner_granularity=single`). Multi-node task graphs and parallel execution are
> advanced / explicit. See [README.md](../README.md) and [INTEGRATION.md](INTEGRATION.md).

cc-loop can layer a **task graph** on top of `plan → implement → test → review`.
v0.7 adds dynamic replanning; v0.8 adds per-node provider/policy routing; v0.9 adds
optional parallel execution for independent nodes.

## Modes

### Single-loop / legacy single-step (default path)

With `planner_granularity=single` (default), the planner is steered toward one
implementation step. Legacy single-step JSON still works and is wrapped into a
one-node graph automatically:

```json
{
  "prompt": "...",
  "expected_changes": "...",
  "acceptance_criteria": "...",
  "is_final_step": true
}
```

### Task graph mode (advanced)

Enable with `--planner-granularity graph` (or `auto` when the goal warrants
decomposition). The planner may return:

```json
{
  "mode": "task_graph",
  "summary": "Short implementation strategy",
  "nodes": [
    {
      "id": "T1",
      "title": "Set up project structure",
      "description": "Create package skeleton and baseline docs.",
      "kind": "implementation",
      "owner": "implementer",
      "dependencies": [],
      "acceptance_criteria": ["pyproject.toml exists"],
      "files_scope": ["pyproject.toml", "src/"]
    }
  ],
  "is_final_step": false
}
```

cc-loop stores the graph in `state.json` under `task_graph` and runs nodes in
dependency order.

## Graph node statuses

| Status | Meaning |
|--------|---------|
| `pending` | Not started; may run when dependencies pass |
| `running` | Current attempt targets this node |
| `passed` | Node approved + tests green; merged **or** marked handoff-ready when `auto_merge=false` |
| `failed` | Terminal failure on this node |
| `rejected` | Reviewer rejected; may retry within budget |
| `blocked` | Dependency failed or invalid dependency id |
| `skipped` | Explicitly skipped (allowed for completion) |

## Execution flow

1. **Init** — create a task with a goal (and usually `--test-command`).
2. **Plan** — first iteration runs the planner (or direct single-node synthesis).
3. **Dispatch** — select the first runnable node.
4. **Implement** — implementer gets a node-scoped prompt.
5. **Test** — configured `test_command` runs in the worktree.
6. **Review** — reviewer judges the **current node**.
7. **Finalize** — on approve + green tests:
   - default (`auto_merge=false`): node → `passed`, work stays on the attempt branch (`ready_for_handoff`)
   - with `--auto-merge`: merge into the base branch, then node → `passed`
8. **Continue** — if more nodes remain, `auto` starts the next runnable node.
9. **Done** — when required nodes are `passed`, task status becomes `done`.

Nodes run **sequentially** by default (`max_parallel_nodes: 1`). Parallel
execution requires both `allow_parallel_execution=true` and `max_parallel_nodes > 1`.

## Roles (not a general multi-agent framework)

Graph-aware runs still use the same fixed roles:

- **Planner / reviewer** — planning and review brain (must differ from implementer when `require_distinct_reviewer=true`).
- **Implementer** — writes code in an isolated worktree per node.

cc-loop does not spawn a distributed worker pool or compete with IDE subagent
task-splitting. Role separation + test gate remain the product.

## Inspecting progress

```bash
cc-loop status --task-id TASK_ID
cc-loop status --task-id TASK_ID --json   # additive task_graph block when present
cc-loop graph --task-id TASK_ID           # advanced human node list
cc-loop graph --task-id TASK_ID --json
cc-loop graph --task-id TASK_ID --history
cc-loop summary --task-id TASK_ID --json  # Luma single-file recap
```

## State and attempts

- `TaskState.task_graph` — persisted graph (optional; absent in pre-v0.4 state files).
- `AttemptRecord.graph_node_id` — links each attempt to a graph node.

## Recovery

Test failures, merge conflicts, and provider errors apply to the **current graph
node**. Reviewer reject retries the implementer for that node. See
[RECOVERY.md](RECOVERY.md).

When a dependency node fails terminally, downstream nodes are marked `blocked`.

## Per-node routing (v0.8, advanced)

Graph nodes may optionally specify:

- `planner_provider`, `implementer_provider`, `reviewer_provider`, `reviewer_providers`
- `test_policy`, `merge_policy`, `max_changed_files`, `max_review_patch_bytes`, `requires_manual_review`, `allow_merge_without_tests`

Task-level config is the fallback. Node policy cannot weaken task-level safety
unless `allow_node_policy_weakening` is explicitly enabled.

## Dynamic replanning (v0.7, advanced)

Reviewer `decision: replan` triggers a planner graph patch (`mode: graph_patch`).
Valid patches apply to the persisted graph; invalid patches fail with a structured
failure report. Mutations are recorded in `graph_events.jsonl`.

## Runnable node rules

A node is runnable when:

- Status is `pending`, or `rejected` with retries remaining.
- All dependencies have status `passed`.

A node is `blocked` when a dependency is terminally failed or references an unknown node id.

## Limitations

- Parallel execution is opt-in and advanced.
- Graph replanning requires reviewer `replan` and valid planner patch JSON.
- Default product path does not require multi-node graphs.
