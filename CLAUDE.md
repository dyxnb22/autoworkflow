# CLAUDE.md — cc-loop

**0.12.0 · 分角色交付引擎（Rust）。**  
详规：[AGENTS.md](AGENTS.md) · [docs/WORKFLOW.md](docs/WORKFLOW.md) · [docs/INTEGRATION.md](docs/INTEGRATION.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md)

你在改 **cc-loop 本身**（`rust/`），除非明确在测 provider。

核心：goal → plan/review 一侧，implement 另一侧 → **测试门** →（目标态：**P0/P1 门**）→ 停在分支 handoff。

硬差异：角色锁定 · 测试门 · reject→实现闭环。质量环设计与里程碑见 WORKFLOW——落地跟 M1→M4，不要先做编排增强。

```bash
make test && make clippy
make release && make install
```

`--state-root` 在子命令前。claude-code：plan/review 用 `--print`；implement 在 worktree 改代码。
