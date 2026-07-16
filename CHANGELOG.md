# Changelog

## v0.12.0 — Rust sole implementation

Role-separated delivery engine rewritten in Rust under `rust/` (binary `cc-loop` 0.12.0).

- Full INTEGRATION schema **1** CLI/JSON surface
- Product defaults from v0.11: handoff (`auto_merge=false`), distinct reviewer, single-loop planner, `auto` requires `test_command`
- Concurrent parallel nodes (opt-in) + prompt-cache health scoring
- Offline `fake` provider (`CC_LOOP_FAKE_PROVIDERS=1`)
- Python package removed

Install: `make install` or `./scripts/cc-loop`. Docs: [README.md](README.md), [docs/INTEGRATION.md](docs/INTEGRATION.md), [docs/OPERATIONS.md](docs/OPERATIONS.md).
