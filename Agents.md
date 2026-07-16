# AGENTS.md

cc-loop **0.12.0** — **分角色交付引擎**（Rust），不是通用 agent 调度器。

> 写的人不能审自己的活；不过测试不能过关。

```text
goal → plan → implement（另一角色）→ test → review（非实现方）
         ↑________ reject / fail 重试 _________|
                   双绿 → ready_for_handoff（不合 main）
```

## 价值（三硬差异）

1. **跨 provider 角色锁定** — `require_distinct_reviewer=true`
2. **测试门默认不可关** — `auto` 必填 `test_command`；红测不能当成功
3. **reject → 重回实现** — 状态机闭环，不是聊完就散

## Docs

| Doc | Role |
|-----|------|
| [README.md](README.md) | 产品叙事 + 默认路径 |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | Luma 卡 + CLI/JSON 合约 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 排障；advanced 降权 |
| [CHANGELOG.md](CHANGELOG.md) | 版本 |
| [rust/README.md](rust/README.md) | Crate 布局 |

## 先做厚 / 后做或不做

**厚：** 单切片 · worktree · 四态 · resume/stop · `status`/`summary --json`  
**薄或不做：** 大图 · 并行 · 动态 replan · 默认合 main · 「万能编排」叙事

## 改代码时

- 默认路径与三硬差异优先于新编排能力
- 破坏 JSON/CLI → 更新 INTEGRATION.md
- Luma 第一屏字段：`roles` · `distinct_reviewer` · tests · review reason · retry · `success`
- Prompt cache 优先服务 reviewer / 多轮 retry

## Tests

```bash
make test && make clippy
```

Invariants：`shell=false` · 无 `pkill -f` · 脏仓阻断 · 不默认切换用户主 checkout 作为成功路径 · 成功=handoff。
