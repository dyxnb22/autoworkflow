# Task graph orchestration (v0.4–v0.9)

cc-loop v0.4 adds a **task graph** layer on top of the linear planner → implementer → test → reviewer pipeline. v0.7 adds dynamic replanning; v0.8 adds per-node provider/policy routing; v0.9 adds parallel execution for independent nodes.

## Modes

### Legacy single-step mode

If the planner returns the original JSON shape:

```json
{
  "prompt": "...",
  "expected_changes": "...",
  "acceptance_criteria": "...",
  "is_final_step": true
}
```

cc-loop wraps it into a single-node task graph (`T1`) automatically. Behavior matches v0.3 for most workflows.

### Task graph mode (preferred)

The planner returns:

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

cc-loop stores the graph in `state.json` under `task_graph` and runs nodes in dependency order.

## Graph node statuses

| Status | Meaning |
|--------|---------|
| `pending` | Not started; may run when dependencies pass |
| `running` | Current attempt targets this node |
| `passed` | Node approved, tests passed, changes merged |
| `failed` | Terminal failure on this node |
| `rejected` | Reviewer rejected; may retry within budget |
| `blocked` | Dependency failed or invalid dependency id |
| `skipped` | Explicitly skipped (allowed for completion) |

## Execution flow

1. **Init** — user creates a task with a goal.
2. **Plan** — first iteration runs the planner; output becomes a `TaskGraph`.
3. **Dispatch** — cc-loop selects the first runnable node (dependencies satisfied).
4. **Implement** — implementer receives a **node-scoped** prompt (goal, graph summary, acceptance criteria, files scope).
5. **Test** — configured `test_command` runs in the worktree.
6. **Review** — reviewer judges whether the **current node** is complete.
7. **Merge** — on approve + passing tests, changes merge into the base branch; node → `passed`.
8. **Continue** — if more nodes remain, `auto` starts the next iteration for the next runnable node.
9. **Done** — when all required nodes are `passed`, task status becomes `done`.

Nodes run **sequentially** by default (`max_parallel_nodes: 1`). Set `max_parallel_nodes` > 1 in task config for parallel execution of independent nodes (v0.9). Approved parallel nodes merge through a serial merge queue.

## Multi-agent orchestration

v0.4 “multi-agent” means graph-aware orchestration with configurable provider roles:

- **Planner / reviewer** — e.g. `claude-code` as the planning and review brain.
- **Implementer** — e.g. `cursor` as the implementation worker per node.
- **Node metadata** — `owner` and `kind` are tracked for future routing.

cc-loop does not spawn a distributed worker pool or cloud runtime in v0.4.

## Inspecting progress

```bash
cc-loop status --task-id TASK_ID          # human summary includes graph progress
cc-loop status --task-id TASK_ID --json   # additive task_graph block
cc-loop graph --task-id TASK_ID           # human node list
cc-loop graph --task-id TASK_ID --json    # graph JSON snapshot
cc-loop graph --task-id TASK_ID --history # graph mutation events (v0.7)
cc-loop report --task-id TASK_ID --json   # full task report (v0.6)
```

Example human `graph` output:

```
Task graph: spec-planner
Progress: 2/5 passed

T1 passed   Set up project structure
T2 passed   Implement parser
T3 pending  Implement renderers
T4 pending  Implement CLI
T5 pending  Add integration tests
```

## State and attempts

- `TaskState.task_graph` — persisted graph (optional; absent in pre-v0.4 state files).
- `AttemptRecord.graph_node_id` — links each attempt to a graph node (empty for legacy tasks).

## Recovery

Test failures, merge conflicts, and provider errors apply to the **current graph node**. Repair prompts and retry budgets are unchanged from v0.3; see [RECOVERY.md](RECOVERY.md).

When a dependency node fails terminally, downstream nodes are marked `blocked`.

## Per-node routing (v0.8)

Graph nodes may optionally specify:

- `planner_provider`, `implementer_provider`, `reviewer_provider`, `reviewer_providers`
- `test_policy`, `merge_policy`, `max_changed_files`, `max_review_patch_bytes`, `requires_manual_review`, `allow_merge_without_tests`

Task-level config is the fallback. Node policy cannot weaken task-level safety unless `allow_node_policy_weakening` is explicitly enabled.

## Dynamic replanning (v0.7)

Reviewer `decision: replan` triggers a planner graph patch (`mode: graph_patch`). Valid patches apply to the persisted graph; invalid patches fail with a structured failure report. Mutations are recorded in `graph_events.jsonl`.

## Runnable node rules

A node is runnable when:

- Status is `pending`, or `rejected` with retries remaining.
- All dependencies have status `passed`.

A node is `blocked` when a dependency is terminally failed or references an unknown node id.

## Limitations

- Parallel execution requires `max_parallel_nodes` > 1 and independent runnable nodes.
- Graph replanning requires reviewer `replan` decision and valid planner patch JSON.
- Planner must produce valid graph JSON, graph patch JSON, or legacy JSON.
