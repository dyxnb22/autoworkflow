# CLAUDE.md — cc-loop

**0.12.0 · 分角色交付引擎（Rust）。** 详规：[AGENTS.md](AGENTS.md) · [docs/INTEGRATION.md](docs/INTEGRATION.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md)

你在改 **cc-loop 本身**（`rust/`），除非明确在测 provider。

核心不变：goal → plan/review 一侧角色，implement 另一侧 → **测试把门** → 默认停在分支。

三硬差异：角色锁定 · 测试门 · reject→实现闭环。不要把产品做成更强的 agent 调度器。

```bash
make test && make clippy
make release && make install
```

`--state-root` 在子命令前。claude-code：plan/review 用 `--print`；implement 在 worktree 改代码。
