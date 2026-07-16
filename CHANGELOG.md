# Changelog

## Unreleased

- **Test gate:** `run` / `resume` / `auto` 均要求 `test_command`（或显式 `allow_merge_without_tests`）；`skipped` 不得进入 review / `ready_for_handoff`。
- **Preflight:** `run`/`resume`/`auto` 使用 `require_test_command=true`；空测试命令升为 error。`init`/`doctor` 仍可 warning-only。
- **Luma card:** `status`/`summary` 增加 `plan_summary` · `tests` · `review` · 聚合对象 `delivery`。
- **Contracts:** 迁入 `cc-loop-cli/tests`（`CARGO_BIN_EXE_cc-loop` 可靠）；覆盖 reject 闭环、红测阻断、skipped 不可 handoff。

## v0.12.0 — 分角色交付引擎（Rust）

定位：**不是**通用 agent 调度器；输入 goal，输出「另一人审过、测试过」的 attempt 分支（默认 `ready_for_handoff`，不合 main）。

硬差异：

- 跨 provider 角色锁定（`require_distinct_reviewer=true`）
- 测试门：`auto`/`run`/`resume` 必填 `test_command`；默认不允许跳过测试当成功
- 审核拒绝 → 状态机重回实现

实现：`rust/` 二进制 `cc-loop`。默认单环；任务图 / 并行 / 合 main 为 advanced 或 opt-in。

文档：[README.md](README.md) · [docs/INTEGRATION.md](docs/INTEGRATION.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md)

安装：`make install` 或 `./scripts/cc-loop`
