# Operations

产品叙事与默认路径见 [README.md](../README.md)。集成 / Luma 合约见 [INTEGRATION.md](INTEGRATION.md)。  
质量环设计（多维审查、P0/P1 停止条件）：[WORKFLOW.md](WORKFLOW.md)。

本文只谈：**默认闭环怎么排障**，以及 advanced 能力如何标注——不把编排当主叙事。

## 默认闭环（你真正在运维的东西）

```text
plan → implement → test → review →（approve ∧ 无阻断 → handoff | reject/fail → resume）
```

四态 + 控制：`resume` · `stop`。  
成功 = 测试绿 + 非实现方 approve +（目标态：无 P0/P1）+ 停在分支。

### 第一眼该看

```bash
cc-loop status --task-id ID --json
cc-loop summary --task-id ID --json
```

优先：`roles` · `distinct_reviewer` · `attempt.test_status` · `review` / `latest_reject_reason` · `attempt.retry` · `success` · `next_action`。  
目标态再加：`quality.blocking_counts` · `quality.open_blocking_issues`。

### 落盘位置

```text
~/.cc-loop/tasks/<id>/state.json
~/.cc-loop/tasks/<id>/run.summary.json          # 终端态交付卡
~/.cc-loop/tasks/<id>/runner.{pid,log,heartbeat.json}
~/.cc-loop/tasks/<id>/artifacts/iter-NNN[-retry-NN]/
~/.cc-loop/worktrees/<repo>/<id>/iter-NNN[-retry-NN]/
```

## 三硬差异（运维含义）

| 差异 | 行为 |
|------|------|
| 角色锁定 | init/doctor/run/auto：implementer 与 reviewer 身份相同则失败（除非 `--allow-same-reviewer`） |
| 测试门 | `auto`/`run`/`resume` 无 `test_command` → 拒绝启动（CLI + preflight error）；测试红/`skipped` → 不得 review 过关；红测走 repair/`resume` |
| reject→实现 | `decision=reject` → `Stopped` + `next_action≈resume`；下一轮 implementer 带上拒绝原因 |

**质量门：** review issues 含 P0/P1 → 强制 reject 并返工；仅当 `blocking_counts.P0/P1 == 0` 且测试绿才 `ready_for_handoff`。

脏主仓默认阻断开跑（preflight）。不要在用户正在编辑的 main worktree 里直接改文件。

## 排障速查

| 阶段 | 关键文件 |
|------|----------|
| Plan | `plan.parsed.json` · `plan.last-message.txt` |
| Implement | worktree `git diff` / `git status` · `implementer.raw*` |
| Test | `test.output.txt` |
| Diff | `diff.stat.txt` · `patches/` |
| Review | `review.parsed.json`（`decision` · `reason` · `retry_prompt` · `issues[].severity`） |

常见处理：

- 测挂了 → 看 `test.output.txt`，`cc-loop resume` 走修复/重试  
- 审拒了 / 有 P0·P1 → `latest_reject_reason` + issues，`resume`（闭环，不是另开聊天）  
- provider 挂了 → 查 raw / timeout 配置，再 resume  
- 要停挂机 → `cc-loop stop --task-id ID`

### 控制

```bash
cc-loop stop --task-id ID
cc-loop resume --task-id ID
cc-loop cancel --task-id ID    # 停并标取消（少用）
cc-loop cleanup --task-id ID   # 清 runner 运行时文件（少用）
```

## 效率（服务原目标，别本末倒置）

Prompt cache / 稳定前缀优先服务 **reviewer 与多轮 reject→retry**（「一审一写」+ 分级门）。  
先让单环稳、质量门硬，再谈并行挂机。

## Advanced（后做或不做）

默认产品 **不依赖** 这些。文档保留入口，避免误当成卖点：

| 能力 | 说明 |
|------|------|
| 多节点 task graph | `--planner-granularity graph`；`cc-loop graph` |
| 并行 node | `allow_parallel_execution` + `max_parallel_nodes>1` |
| 动态 replan | reviewer `replan` / graph patch（实验） |
| 合进 main | `--auto-merge`（会动用户仓库 checkout，非默认成功定义） |
| 无测试逃生 | `--allow-merge-without-tests`（显式，勿当默认） |
| `review_mode=per_facet` | 串行多维再审（WORKFLOW M4；默认 off） |

TUI / 对外文案：**不要**把上述能力放在第一屏。

## 非目标（运维）

不自动 `git reset --hard` / `clean -fd` · 不默默 stash 用户脏仓 · 不为 flaky test 无限重试 · 不把「多挂几个 agent」当健康指标 · 不以「模型口头说没问题」代替 P0/P1 门禁。
