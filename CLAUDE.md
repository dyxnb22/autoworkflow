# CLAUDE.md — cc-loop

**0.12.0 (Rust).** Canonical agent notes: [AGENTS.md](AGENTS.md). Contract: [docs/INTEGRATION.md](docs/INTEGRATION.md). Ops: [docs/OPERATIONS.md](docs/OPERATIONS.md).

You edit **cc-loop itself** under `rust/`, not as a provider unless testing providers.

```bash
make test && make clippy
make release && make install
./scripts/cc-loop --version
```

Global flags before subcommand: `cc-loop --state-root PATH <cmd> …`

claude-code: planner/reviewer use `--print`; implementer edits in the worktree. Orchestrator must pass `print_only=true` for plan/review.

Invariants and module map: see [AGENTS.md](AGENTS.md).
