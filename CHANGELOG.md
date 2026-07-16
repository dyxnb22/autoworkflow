# Changelog

## Unreleased

### Docs — quality loop（产品设计先行）

- 新增 [`docs/WORKFLOW.md`](docs/WORKFLOW.md)：现代工程对照、多维审查、P0/P1 停止条件、状态机、Luma `quality.*`、落地里程碑 M1–M4。
- README / INTEGRATION / OPERATIONS / Agents 对齐「无 P0/P1 才交付」目标态；标明已上线 vs 待实现。

### Engine（已合入 harden 分支能力）

- **Test gate:** `run` / `resume` / `auto` 均要求 `test_command`；`skipped` 不得进入 review / `ready_for_handoff`。
- **Preflight:** 执行路径 `require_test_command=true`；`init`/`doctor` warning-only。
- **Luma card:** `plan_summary` · `tests` · `review` · `delivery`。
- **Contracts:** `cc-loop-cli/tests`；reject 闭环 / 红测 / skipped。

## v0.12.0 — 分角色交付引擎（Rust）

定位：**不是**通用 agent 调度器；输入 goal，输出「另一人审过、测试过」的 attempt 分支（默认 `ready_for_handoff`，不合 main）。

硬差异：

- 跨 provider 角色锁定（`require_distinct_reviewer=true`）
- 测试门：`auto`/`run`/`resume` 必填 `test_command`；默认不允许跳过测试当成功
- 审核拒绝 → 状态机重回实现

实现：`rust/` 二进制 `cc-loop`。默认单环；任务图 / 并行 / 合 main 为 advanced 或 opt-in。

文档：[README.md](README.md) · [docs/WORKFLOW.md](docs/WORKFLOW.md) · [docs/INTEGRATION.md](docs/INTEGRATION.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md)

安装：`make install` 或 `./scripts/cc-loop`
