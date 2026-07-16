# cc-loop（Rust workspace）

**分角色交付引擎** 0.12.0 · 集成 schema 1  
叙事：[README.md](../README.md) · 质量环：[docs/WORKFLOW.md](../docs/WORKFLOW.md) · Luma 合约：[docs/INTEGRATION.md](../docs/INTEGRATION.md)

```bash
make test && make install
```

| Crate | Role |
|-------|------|
| `cc-loop-cli` | 二进制 + CLI 契约测试（`tests/cli_contract.rs`） |
| `cc-loop-core` | 闭环引擎（角色锁定 · 测试门 · reject→实现） |

默认：`planner_granularity=single` · `require_distinct_reviewer=true` · `auto_merge=false`。  
大图 / 并行 / 合 main：advanced，不是产品主叙事。
