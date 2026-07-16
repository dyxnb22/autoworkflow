# cc-loop

**分角色交付引擎**（Rust 0.12）——不是通用 agent 调度器。

输入一个 goal，输出「**另一人审过、测试过**」的一串提交（停在 attempt 分支，默认可交接 / 可开 PR）。

> **写的人不能审自己的活；不过测试不能过关。**

```text
goal → plan → implement（另一角色/CLI）→ test → review（非实现方）
         ↑______________ reject / fail 重试 _______________|
                   双绿 → 停在分支（ready_for_handoff）
```

合进 main **不是**默认成功（`--auto-merge` 才是 opt-in）。

## 三个硬差异（相对「聊天里开个 subagent」）

| 能力 | 默认行为 |
|------|----------|
| **跨 provider 角色锁定** | `require_distinct_reviewer=true`：implementer 与 reviewer 的 provider+model 必须不同 |
| **测试门不可关** | `auto` 没有 `test_command` 直接拒绝；红测只能 repair/retry，不能把「跳过测试」当默认成功 |
| **审核拒绝 → 重回实现** | `reject` 进入可恢复状态；`auto`/`resume` 带着原因再跑 implementer——状态机在转，不是聊完就散 |

## Install

```bash
make test && make release
make install                 # ~/.local/bin/cc-loop
./scripts/cc-loop --version
```

代码：[`rust/`](rust/)。Luma/集成契约：[`docs/INTEGRATION.md`](docs/INTEGRATION.md)。运维：[`docs/OPERATIONS.md`](docs/OPERATIONS.md)。

## 默认只卖这一条路径

| 设置 | 默认 | 含义 |
|------|------|------|
| `planner_granularity` | `single` | 单切片闭环 |
| `require_distinct_reviewer` | `true` | 写≠审 |
| `auto_merge` | `false` | 成功 = 可交接，不合 main |
| `test_command`（`auto`） | **必填** | 没测试命令不挂机 |
| `allow_merge_without_tests` | `false` | 逃生口，绝非默认 |
| Providers | planner/reviewer `codex`，implementer `cursor` | 开箱即跨 CLI |

**先做厚：** worktree 隔离 · plan/implement/test/review · resume/stop · `status`/`summary --json`  
**后做或不做：** 大任务图、多 node 并行、动态 replan、通用多 agent 框架、默认合 main

## Quick start

```bash
cc-loop init \
  --goal "Fix the failing CLI flag" \
  --repo /path/to/repo \
  --task-id my-task \
  --planner claude-code --reviewer claude-code --implementer cursor \
  --test-command -- cargo test -q

cc-loop auto --detach --task-id my-task

# Luma / TUI 第一屏：谁在写、谁在审、测试过了没、能否交付
cc-loop status --task-id my-task --json
cc-loop summary --task-id my-task --json
```

`--state-root` 必须写在子命令前。`CC_LOOP_STATE_ROOT` 可设默认状态根。

## Luma 第一屏（一张卡）

`summary --json`（及 `status --json` 的对应字段）优先讲故事，不要先甩任务图/并行/子命令清单：

| 卡面 | 字段线索 |
|------|----------|
| 谁在写 / 谁在审 | `roles` · `distinct_reviewer` |
| Plan 摘要 | `plan` / plan summary |
| Implementer 改了什么 | diff stat（artifact / summary 内） |
| Tests | `attempt.test_status` / `tests` |
| Review | `decision` + 一句 `reason`；reject 时 `latest_reject_reason` |
| 第几次重试 | `attempt.retry` |
| 现在能否交付 | `success`（目标：`ready_for_handoff`）· `next_action` |

详规见 [INTEGRATION.md](docs/INTEGRATION.md#luma-delivery-card)。

## 日常命令

`init` · `doctor` · `list` · `run` · `resume` · `auto` · `status` · `summary` · `stop`

高级 / 少用：`graph` · `report` · `cancel` · `cleanup` · `eval` · `export`（以及并行、合 main 相关开关）

## Providers

| Provider | 典型角色 |
|----------|----------|
| `cursor` | implementer（worktree 里改代码） |
| `codex` / `claude-code` | planner / reviewer（`--print` 规划与审核） |
| `fake` | 离线契约冒烟（`CC_LOOP_FAKE_PROVIDERS=1`） |

## 非目标

不是更强的 subagent 调度器 · 不是默认合 main · 不在脏主仓上改文件 · 不用 `pkill -f` · 价值不来自「挂了多少 agent」。
