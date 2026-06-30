# AGENTS.md

Canonical agent reference for cc-loop. **Package 0.3.0.**

## Docs

- [docs/INTEGRATION.md](docs/INTEGRATION.md) — external CLI/JSON contract
- [docs/RECOVERY.md](docs/RECOVERY.md) — failure classification, repair budgets, auto dispatch
- [docs/EXIT_CODES.md](docs/EXIT_CODES.md)
- [CLAUDE.md](CLAUDE.md) — Claude Code entry
- [.cursor/rules/cc-loop.mdc](.cursor/rules/cc-loop.mdc) — Cursor rules

## v0.3 recovery modules

| Module | Role |
|--------|------|
| `failure.py` | `FailureType`, classifiers, `failure.report.json` |
| `recovery.py` | `decide_auto_step`, retry budgets |
| `repair_prompts.py` | implementer repair prompts |

`auto` uses `decide_auto_step` — not ad-hoc `needs_resume` / merge_error exits.

## Tests

```bash
python -m pytest tests/ -q
```

Recovery tests: `test_failure_classification.py`, `test_recovery_dispatch.py`, `test_auto_recovery.py`
