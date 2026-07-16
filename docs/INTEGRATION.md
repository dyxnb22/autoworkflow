# Integration contract (schema 1)

Stable CLI/JSON for integrators（尤其是 Luma / TUI）。只依赖本文，不要解析内部模块或把 artifact 目录当控制面。

**Package:** 0.12.0 · **Binary:** `make install` / `./scripts/cc-loop` / `rust/target/release/cc-loop`

## 产品一句话

cc-loop 是 **分角色交付引擎**：输入 goal，输出「另一人审过、测试过」的 attempt 分支提交。

- 写的人不能审自己的活（`require_distinct_reviewer`）
- 不过测试不能过关（`test_command` + 双绿）
- 默认成功 = `ready_for_handoff`（可交接 / 可开 PR），**不合 main**

不是通用多 agent 调度器。任务图 / 并行 / 合 main 均为 advanced 或 opt-in。

## Integrator rules

1. 用 subprocess 调文档内命令。
2. **第一屏只读** `status --json` + `summary --json`（见下方 [Luma delivery card](#luma-delivery-card)）。
3. 长跑用 `auto --detach`，轮询 JSON；不要阻塞前台 `auto` 当 UI。
4. 不要 scrape artifact 目录做主控制流（排障除外）。

## Day-to-day CLI（先卖这些）

| Command | Purpose |
|---------|---------|
| `init` | 建任务（带 test_command + 分角色 provider） |
| `doctor --repo PATH` | 预检 |
| `auto --detach [--task-id ID]` | 挂机跑默认闭环 |
| `status [--task-id ID] [--json]` | 轮询：谁写/谁审/测没过/能否交付 |
| `summary [--task-id ID] [--json]` | **一张交付卡**（Luma 主数据源） |
| `resume` / `stop` | 继续 / 停 runner |
| `list [--json]` | 列任务 |

Global：`--state-root PATH` **在子命令前**；`--version`。Env：`CC_LOOP_STATE_ROOT`。

少用 / advanced：`graph` · `report` · `cancel` · `cleanup` · `eval` · `export` · `--auto-merge` · 并行相关 flag。

## Recommended flow

```bash
cc-loop doctor --repo "$PROJECT_PATH" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q

cc-loop init --goal "..." --repo "$PROJECT_PATH" --task-id "$TASK_ID" \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q

cc-loop auto --detach --task-id "$TASK_ID"
cc-loop status --task-id "$TASK_ID" --json
cc-loop summary --task-id "$TASK_ID" --json
```

### Hard defaults（三硬差异）

| Setting | Default | Integrator 含义 |
|---------|---------|-----------------|
| `require_distinct_reviewer` | `true` | implementer ≠ reviewer（provider+model）；UI 应写死展示 `roles` + `distinct_reviewer` |
| `test_command` for `auto`/`run`/`resume` | **required** | 缺省 → exit `1`；不要提供「无测试默认成功」的 UX |
| `allow_merge_without_tests` | `false` | 仅显式逃生；UI 不应当默认开关推销 |
| `auto_merge` | `false` | `success=ready_for_handoff`；合 main 仅 `--auto-merge` |
| `planner_granularity` | `single` | 默认单环；`graph` 为 advanced |

Reject → 状态机回到实现：`attempt.decision=reject` 且 retries 未尽时，`next_action` 偏向 `resume` / repair；`latest_reject_reason` 供下一轮 implementer。

空 `test_command` 若仍进入 orchestrator，会记 `test_status=skipped` 并**阻断 review/handoff**（除非显式 `allow_merge_without_tests`）。

## Luma delivery card

TUI **第一屏只渲染这件事**（不要先画任务图 / 并行 / 命令清单）：

| 卡面 | `summary --json`（优先） | `status --json` 备份 |
|------|--------------------------|----------------------|
| 谁在写 | `roles.implementer` / `delivery.roles` | 同 |
| 谁在审 | `roles.reviewer` · `distinct_reviewer` | 同 |
| Plan 摘要 | `plan_summary` | `plan_summary` |
| 改了什么 | `diff_stat` | attempt artifact / `delivery`（summary 更全） |
| Tests | `tests` / `latest_attempt.test_status` | `tests` / `attempt.test_status` |
| Review | `review.decision` + `review.reason` | `review` · `latest_reject_reason` |
| 第几次重试 | `latest_attempt.retry` / `delivery.retry` | `attempt.retry` / `delivery.retry` |
| 能否交付 | `success` · `next_action` | 同 |

Luma 也可只读嵌套对象 `delivery`（status/summary 均有）：卡面字段集中在一处，排障字段留在顶层。

成功目标值：`success == "ready_for_handoff"`（默认）。`merged` 仅在 opt-in `auto_merge` 后出现。

终端态也会落盘 `~/.cc-loop/tasks/<id>/run.summary.json`（与 `summary --json` 同形）。

## `status --json`（schema_version 1）

单对象 stdout。第一屏相关字段：

| Field | Type | Description |
|-------|------|-------------|
| `roles` | object | `{planner,implementer,reviewer}` × `{provider,model}` |
| `distinct_reviewer` | bool | 写审是否分离（实测） |
| `require_distinct_reviewer` | bool | 配置是否强制 |
| `plan_summary` | string | plan / goal 一句话 |
| `tests` | object | `{status,pass,fail,skipped,reason,exit_code}` |
| `review` | object | `{decision,reason,issues}` |
| `delivery` | object | 上述卡面字段的聚合（含 `retry` · `success` · `next_action`） |
| `attempt.test_status` | string | `passed` / `failed` / `skipped` / `timed_out` / … |
| `attempt.decision` | string | `approve` / `reject` / … |
| `attempt.retry` | int | 当前节点重试次数 |
| `latest_reject_reason` | string \| null | 最近一次拒绝原因 |
| `success` | string | `ready_for_handoff` / `merged` / `stopped` / `failed` / … |
| `next_action` | string | 见下表 |
| `auto_merge` | bool | 是否合 main |
| `running` / `runner_pid` / `can_stop` / `can_resume` | … | 挂机控制 |

身份与仓库：`task_id` · `goal` · `target_repo` · `base_branch` · `base_commit` · `status` · `iteration` · `cc_loop_version` · `schema_version`。

`task_graph`：**可省略**；仅 advanced 多节点时出现——TUI 默认不要展示。

### `next_action`

| Value | Meaning |
|-------|---------|
| `none` | runner 活跃，继续轮询 |
| `run` | 已 init，尚无 attempt |
| `resume` | 继续 / reject 后重回实现 |
| `repair` | 可恢复失败 → 实现侧修复 |
| `inspect` | 需人看 |
| `done` | 交付成功 |
| `failed` / `terminal` | 失败 / 不可恢复 |

可选 `failure` 对象：排障用，不是第一屏主角。

## `list --json`

JSON **数组**：`{task_id, status, target_repo, phase, updated_at, goal, iteration}`。

## `summary --json`

Luma 主合约。除 delivery card 字段外可含排障附加（`artifact_paths`、`execution_timeline`、`prompt_cache` 等）——**UI 可折叠，勿抢第一屏**。

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | 成功，或非执行错误的可恢复停顿 |
| `1` | 用户/配置错误（含 `auto`/`run`/`resume` 无 `test_command`、preflight、缺任务） |
| `2` | 执行失败（provider / task `failed`） |

挂机以 JSON 为准，不要只靠 exit code。

## Detached `auto`

1. 后台跑无 `--detach` 的 `auto`
2. 写 `runner.pid` / `runner.log` / `runner.heartbeat.json`
3. 父进程打印 `detached …` 后 exit `0`

## Init flags（集成相关）

- `--test-command -- ARG ...` — **`auto` 必填**（`--` 后跟真实命令）
- `--planner` / `--implementer` / `--reviewer` — 角色锁定靠不同 provider（或同 provider 不同 model）
- `--allow-same-reviewer` — 关掉角色锁定（不推荐；UI 应警告）
- `--auto-merge` / `--allow-merge-without-tests` — 显式逃生，勿当默认
- `--task-id` · `--goal-file` · 各 provider model flag

Advanced（不必进第一屏）：`--planner-granularity graph` · 并行相关 · review context 调优。

## Doctor

```bash
cc-loop doctor --repo PATH [--json] [--test-command -- …]
```

Exit `0` / `1`。缺 `test_command` 时给出 **warning**（`init`/`doctor` 仍可通过）；`run`/`resume`/`auto` 的 preflight 则把空 `test_command` 升为 **error**（`require_test_command=true`）。未分角色时同样硬失败（除非 `--allow-same-reviewer`）。

## Artifacts（排障，非第一屏）

`artifacts/iter-NNN[-retry-NN]/`：`plan.*` · `implementer.*` · `test.output.txt` · `diff.*` · `review.*` · 可选 `prompt.cache.json`（优先服务 **reviewer / 多轮 retry** 的省 token，不是并行挂机）。

## Versioning

- **Patch:** 修 bug，不改合约  
- **Minor:** 可选 JSON 字段加法  
- **Major / schema bump:** 破坏 CLI 或 JSON → 升 `schema_version`
