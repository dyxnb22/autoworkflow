# Quality Loop Workflow

产品设计文档（目标态）。实现以本文为准逐步落地；**当前已上线能力**见文末 [与 0.12 的关系](#与-012-的关系)。

相关：[`README.md`](../README.md) · [`INTEGRATION.md`](INTEGRATION.md) · [`OPERATIONS.md`](OPERATIONS.md)

---

## 一句话

cc-loop 把 **vibe coding** 收成现代软件工程里的一条可重复交付环：

> 一人写，另一人（侧）规划并审查；自动化测试把门；按严重级别返工；**没有 P0/P1 才允许交付**。

不是「多挂几个 agent」，而是 **分工 + 门禁 + 可恢复闭环 + 可配置停止条件**。

---

## 对照现代研发流程

| 现代工程环节 | cc-loop 阶段 | 谁做 | 产物 |
|--------------|--------------|------|------|
| 需求 / Goal | `goal` | 人（或上游） | 任务目标 |
| 方案 / 设计 | `plan` | planner（非实现方） | plan JSON / 摘要 |
| 实现 | `implement` | implementer（另一 CLI/角色） | worktree 提交 + diff |
| 自动化验证 | `test` | `test_command`（CI 等价物） | pass / fail |
| Code review | `review`（多维） | reviewer（≠ implementer） | decision + severity issues |
| 返工 | reject / repair | implementer | 下一轮 attempt |
| 验收 / 交接 | `ready_for_handoff` | 状态机 | 分支可开 PR（默不合 main） |

刻意对齐的工程原则：

1. **Separation of duties** — 写的人不能审自己的活  
2. **Fail closed on quality** — 无测试、红测、有 P0/P1 → 不能交付  
3. ** Tight feedback loop** — 拒绝/失败带着原因回到实现，状态机可见  
4. **Definition of Done 可配置** — 停止条件写进配置与 JSON，不靠聊天默契  
5. **Handoff ≠ merge** — 交付成功默认停在 attempt 分支  

---

## 目标态主环（产品级）

```text
                 ┌──────────────────────────────────────────────┐
                 │                                              │
goal → plan → implement → test ──► review facets（多维审查）      │
                 ▲                    │                         │
                 │         ┌──────────┼──────────┐              │
                 │         ▼          ▼          ▼              │
                 │     P0/P1 存在   仅 P2+     无阻断项            │
                 │         │          │          │              │
                 │      reject     可选放行    approve           │
                 │      + repair   / 记债      + stop_check     │
                 │         │                      │              │
                 └─────────┘                      ▼              │
                                    ready_for_handoff            │
                                    （默不合 main）               │
```

每一轮 attempt 至少经过：**实现 → 测试门 → 审查门 → 停止条件判定**。  
审查拒绝或测试失败 → **同一条状态机**回到实现（不是另开聊天）。

---

## 角色模型（不变的硬差异）

| 角色 | 职责 | 默认 provider 倾向 |
|------|------|-------------------|
| **planner** | 把 goal 收成可执行计划 / 验收要点 | `codex` / `claude-code` |
| **implementer** | 在 worktree 改代码、提交 | `cursor` |
| **reviewer** | 多维审查 + 分级 issues + 决定 approve/reject | 与 implementer **不同** 的 provider/model |

约束：

- `require_distinct_reviewer=true`（默认）  
- planner 与 reviewer **可以**同侧（一人规划并审核）；implementer 必须另一侧  
- 合 main 仅 `--auto-merge` opt-in，**不是** Definition of Done  

---

## 多维审查（Review facets）

产品默认审查维度（可配置开关，默认全开「基础集」）：

| Facet | 问什么 | 典型 P0/P1 例子 |
|-------|--------|-----------------|
| `correctness` | 是否满足 goal / plan；逻辑对不对 | 行为错误、漏需求 |
| `tests` | 测试是否覆盖关键路径；假绿？ | 无断言、测错东西 |
| `security` | 注入、密钥、权限、不安全默认 | 明文密钥、鉴权绕过 |
| `reliability` | 错误处理、超时、数据丢失风险 | 静默吞错、破坏性默认 |
| `maintainability` | 可读性、耦合、API 边界 | 无法维护的捷径（升为 P1 当阻碍交付时） |
| `ux_cli` | 用户可见行为 / CLI 契约是否破坏 | 破坏性 flag 变更未说明 |

**实现要点（目标态）：**

- 一次 attempt 可跑 **一个或多个 facet**（串行；默认同一 reviewer provider，不同 system 提示）  
- 也可配置「合成一次 review」：单次调用但强制按 facet 填 issues  
- 每个 issue **必须**带 `severity`（见下）与 `facet`  
- 汇总后才做 approve/reject，**不是**聊完一句「看起来行」  

第一版落地建议（最小产品级）：

1. **Structured single review**（强制 JSON issues + severity）— 必做  
2. **Facet checklist 嵌入同一 reviewer prompt** — 必做  
3. **可选二次 pass**（如 `security` 单独再跑）— 配置项，默认关  

---

## 严重级别（Severity）

| Level | 含义 | 对交付的默认影响 |
|-------|------|------------------|
| **P0** | 阻断：错误、安全、数据丢失、目标未达成 | **必须 reject**；不得 handoff |
| **P1** | 高优先：明显缺陷、契约破坏、关键路径缺测 | **必须 reject**；不得 handoff |
| **P2** | 应改：可维护性、小改进、非阻断风险 | 默认 **不阻断**；记入 issues，可 handoff |
| **P3** | 建议 / nit | 不阻断 |

引擎必须 **解析并执行** severity，而不是只把文字塞给下一轮模型碰运气。

### Reviewer 输出契约（目标 JSON）

```json
{
  "decision": "approve" | "reject" | "stop",
  "reason": "one-line summary",
  "retry_prompt": "concrete fix guidance for implementer",
  "issues": [
    {
      "id": "issue-1",
      "facet": "correctness",
      "severity": "P0",
      "title": "short title",
      "detail": "what/where/why",
      "blocking": true
    }
  ],
  "facets_covered": ["correctness", "tests", "security", "reliability", "maintainability"],
  "blocking_counts": { "P0": 0, "P1": 0, "P2": 1 }
}
```

规则：

- 存在任一 `severity` ∈ {P0, P1} **或** `blocking: true` → 引擎将 decision **强制为 reject**（即使模型误写 approve）  
- `decision=approve` 仅当：测试已绿 **且** 停止条件满足  
- `retry_prompt` / issues 进入下一轮 implementer 上下文  

---

## 停止条件（Definition of Done）

可配置；**产品默认**如下：

```text
ready_for_handoff 当且仅当：
  1. test_status == passed
  2. review.decision == approve（或经引擎校正后仍为 approve）
  3. blocking_counts.P0 == 0 且 blocking_counts.P1 == 0
  4. 未触发 max_retries / budget 耗尽
  5. auto_merge == false → 成功词仍为 ready_for_handoff（不合 main）
```

### 配置草案（目标态）

| Key | Default | 含义 |
|-----|---------|------|
| `stop_policy` | `no_p0_p1` | 停止策略名 |
| `blocking_severities` | `["P0","P1"]` | 哪些级别阻断交付 |
| `review_facets` | 基础集（见上） | 要覆盖的审查维 |
| `review_mode` | `structured_single` | `structured_single` \| `per_facet` |
| `max_retries_per_step` | `2` | 单节点返工上限（已有） |
| `require_distinct_reviewer` | `true` | 已有 |
| `test_command` | **必填** | 已有 |
| `allow_merge_without_tests` | `false` | 已有逃生口 |

可选策略（非默认）：

| `stop_policy` | 行为 |
|---------------|------|
| `no_p0_p1` | 默认：无 P0/P1 + 测试绿 + approve |
| `no_p0_only` | 仅 P0 阻断（更松，不推荐默认） |
| `approve_only` | 兼容旧行为：只看 decision + tests（迁移用） |

---

## 状态机（可见闭环）

```text
Initialized
    → Running(plan → implement → test → review)
         ├─ test failed/timed_out     → Stopped(recoverable) → repair/resume → Running
         ├─ review reject (P0/P1)     → Stopped(recoverable) → resume → Running
         ├─ approve ∧ stop_policy ok  → Done / ready_for_handoff
         ├─ reviewer stop / budget    → Stopped|Failed (inspect)
         └─ max retries exceeded      → Failed
```

Luma / `status --json` 必须能回答：

- 卡在哪一阶段  
- 最新阻断 issues（P0/P1 列表）  
- 第几次返工  
- `next_action`：`resume` / `repair` / `done` / `inspect`  

---

## Luma 交付卡（目标字段增量）

在现有 `delivery` 卡之上增加（加法，schema minor）：

| 卡面 | 字段 |
|------|------|
| 质量门 | `quality.stop_policy` · `quality.blocking_severities` |
| 本轮阻断 | `quality.blocking_counts` · `quality.open_blocking_issues[]` |
| 审查维 | `quality.facets_covered` |
| 能否交付 | 仍用 `success` / `next_action`；仅当无 P0/P1 且测试绿才为 `ready_for_handoff` |

第一屏仍是一张卡：**谁写谁审 · plan · diff · tests · review+级别 · retry · 能否交付**。  
不要先甩 facet 配置面板或任务图。

---

## 推荐日常用法（目标 UX）

```bash
cc-loop init \
  --goal "Add --json to status without breaking human output" \
  --repo "$REPO" \
  --task-id status-json \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q \
  --stop-policy no_p0_p1

cc-loop auto --detach --task-id status-json

cc-loop summary --task-id status-json --json
# delivery.success == ready_for_handoff
# quality.blocking_counts.P0/P1 == 0
```

人要介入时：看 `open_blocking_issues` 与 `latest_reject_reason`，`resume` 继续环，而不是新开无状态的对话。

---

## 非目标（保持窄）

- 不是通用多 agent 调度器 / 动态招人框架  
- 不是默认合 main、不是替团队做完整 CI 矩阵产品  
- 第一版不做并行多 reviewer 竞赛、不做无限重试  
- 大任务图 / 多 node 并行仍为 advanced，**不**替代本质量环  

---

## 落地顺序（实现指引）

| 里程碑 | 内容 | 验收 |
|--------|------|------|
| **M1** | Reviewer 强制 structured issues + severity；引擎按 P0/P1 校正 decision | 有 P0 时无法 handoff；契约测试锁定 |
| **M2** | `stop_policy` / `blocking_severities` 配置 + status/summary `quality` 块 | Luma 能展示阻断计数与 issues |
| **M3** | Facet checklist 写入 reviewer 稳定前缀；`facets_covered` 落盘 | 摘要可见多维覆盖 |
| **M4** | 可选 `review_mode=per_facet`（串行二次审查） | 默认仍 off；文档标 advanced |

文档先行 → M1 起改代码；破坏 JSON 则升 `schema_version` 或按 INTEGRATION 做 minor 加法。

---

## 与 0.12 的关系

**已具备（可立即使用）：**

- 分角色 plan / implement / test / review 闭环  
- 写≠审、测试门、reject→实现  
- `ready_for_handoff`、worktree、resume/stop、delivery JSON 卡  

**本文补齐的产品级缺口（待实现）：**

- Severity 作为 **硬门禁**（不只靠 prompt）  
- 可配置停止条件（默认无 P0/P1）  
- 多维审查 checklist / 可选分 facet 再审  
- Luma `quality.*` 字段  

在引擎落地前：可用 reviewer prompt **约定** P0/P1 语义做软约束，但 **不以**「模型自觉」当作产品完成定义。
