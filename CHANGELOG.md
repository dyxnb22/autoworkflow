# Changelog

## Unreleased

### Quality loop engine（M1–M4）

- Severity 硬门禁：`apply_quality_gate`；P0/P1（可配）强制 reject，即使模型误写 approve。
- 配置：`stop_policy` · `blocking_severities` · `review_facets` · `review_mode`（`structured_single` 默认 / `per_facet`）。
- Reviewer 稳定前缀含 facet checklist + structured issues JSON 契约。
- `status`/`summary`/`delivery` 暴露 `quality.*`。
- CLI：`--stop-policy` · `--blocking-severities` · `--review-mode` · `--review-facets`。
- 契约：P0 误 approve → 引擎 reject → 再实现 → handoff。

### Docs — quality loop

- [`docs/WORKFLOW.md`](docs/WORKFLOW.md) 标 M1–M4 已落地；INTEGRATION quality 字段改为已上线。

### Engine（harden）

- Test gate / preflight / delivery card / contract-tests 迁入 CLI（既有）。

## v0.12.0 — 分角色交付引擎（Rust）

定位：**不是**通用 agent 调度器；输入 goal，输出「另一人审过、测试过」的 attempt 分支（默认 `ready_for_handoff`，不合 main）。

硬差异：

- 跨 provider 角色锁定（`require_distinct_reviewer=true`）
- 测试门：`auto`/`run`/`resume` 必填 `test_command`；默认不允许跳过测试当成功
- 审核拒绝 → 状态机重回实现

实现：`rust/` 二进制 `cc-loop`。默认单环；任务图 / 并行 / 合 main 为 advanced 或 opt-in。

文档：[README.md](README.md) · [docs/WORKFLOW.md](docs/WORKFLOW.md) · [docs/INTEGRATION.md](docs/INTEGRATION.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md)

安装：`make install` 或 `./scripts/cc-loop`
