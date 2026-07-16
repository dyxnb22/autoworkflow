# AGENTS.md

cc-loop **0.12.0** — **分角色交付引擎**（Rust），不是通用 agent 调度器。

> 写的人不能审自己的活；不过测试不能过关。  
> 目标态：没有 P0/P1，才允许交付。

```text
goal → plan → implement（另一角色）→ test → review（多维 / 分级）
         ↑______ reject / P0·P1 / 红测 重试 ______|
                   双绿且无阻断 → ready_for_handoff（不合 main）
```

## 价值（三硬差异 + 质量门）

1. **跨 provider 角色锁定** — `require_distinct_reviewer=true`
2. **测试门默认不可关** — `auto`/`run`/`resume` 必填 `test_command`；红测/`skipped` 不能当成功
3. **reject → 重回实现** — 状态机闭环，不是聊完就散
4. **P0/P1 阻断交付** — 默认 `stop_policy=no_p0_p1`；见 [docs/WORKFLOW.md](docs/WORKFLOW.md)

## Docs

| Doc | Role |
|-----|------|
| [README.md](README.md) | 产品叙事 + 默认路径 |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | **质量环设计**（现代工程对照、多维审查、停止条件、里程碑） |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | Luma 卡 + CLI/JSON 合约 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 排障；advanced 降权 |
| [CHANGELOG.md](CHANGELOG.md) | 版本 |
| [rust/README.md](rust/README.md) | Crate 布局 |

## 先做厚 / 后做或不做

**厚：** 单切片 · worktree · 四态 · 测试门 · severity 门 · resume/stop · `status`/`summary --json`  
**薄或不做：** 大图 · 并行 · 动态 replan · 默认合 main · 「万能编排」叙事

## 改代码时

- 默认路径与硬差异 / 质量门优先于新编排能力
- 破坏 JSON/CLI → 更新 INTEGRATION.md；质量环行为 → 同步 WORKFLOW.md
- Luma 第一屏：`roles` · `distinct_reviewer` · tests · review(+severity) · retry · `success` ·（目标）`quality.*`
- Prompt cache 优先服务 reviewer / 多轮 retry
- 落地顺序跟 WORKFLOW 里程碑 M1→M4，不要先做并行

## Tests

```bash
make test && make clippy
```

契约：`rust/crates/cc-loop-cli/tests/cli_contract.rs`。Invariants：`shell=false` · 无 `pkill -f` · 脏仓阻断 · 不默认切换用户主 checkout 作为成功路径 · 成功=handoff。
