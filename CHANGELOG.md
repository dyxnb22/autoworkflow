# Changelog

## v0.12.0 — 分角色交付引擎（Rust）

定位：**不是**通用 agent 调度器；输入 goal，输出「另一人审过、测试过」的 attempt 分支（默认 `ready_for_handoff`，不合 main）。

硬差异：

- 跨 provider 角色锁定（`require_distinct_reviewer=true`）
- 测试门：`auto` 必填 `test_command`；默认不允许跳过测试当成功
- 审核拒绝 → 状态机重回实现

实现：`rust/` 二进制 `cc-loop`。默认单环；任务图 / 并行 / 合 main 为 advanced 或 opt-in。

文档：[README.md](README.md) · [docs/INTEGRATION.md](docs/INTEGRATION.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md)

安装：`make install` 或 `./scripts/cc-loop`
