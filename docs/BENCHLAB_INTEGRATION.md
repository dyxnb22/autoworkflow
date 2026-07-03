# cc-loop-benchlab integration

cc-loop-benchlab lives in a separate repository. This document describes how benchlab
consumes cc-loop artifacts and how to run A/B scenarios that compare prompt-cache
configurations introduced in v0.10+.

## Artifact inputs for benchmarks

Benchlab should read these per-attempt files from cc-loop state:

| Artifact | Purpose |
|----------|---------|
| `prompt.cache.json` | Per-phase cache budget totals |
| `implementer.prompt.metrics.json` | Implementer three-section layout metrics |
| `review.prompt.metrics.json` | Reviewer layout + context mode metrics |
| `review.fast.prompt.metrics.json` | Fast review stage metrics when `review_depth=auto` |
| `run.summary.json` | Terminal run recap (task dir) |
| `execution.timeline.json` | Phase timing timeline (task dir) |

Key metric fields shared across prompt metrics artifacts:

- `stable_prefix_ratio` — contract + task context before dynamic marker
- `contract_prefix_ratio` — stable contract only
- `task_context_ratio` — task/node context section
- `estimated_avoidable_miss_tokens` — reviewer only (omitted patch/diff stat)
- `cache_health` / `total_prompt_cache_health`

## Scenario templates

Example multi-variant scenarios live under `bench/scenarios/` in this repo. Copy them
into cc-loop-benchlab's `scenarios/` directory or reference them from a benchlab config path.

Each variant runs the same fixture goal with different cc-loop CLI flags. Compare
`prompt.cache.json` totals and per-phase `stable_prefix_ratio` across variants.

### Variant dimensions

| Dimension | Flag / config | A/B use |
|-----------|---------------|---------|
| Review context | `--review-context-mode inline \| artifact_refs \| hybrid` | Patch inlining vs artifact refs |
| Review depth | `--review-depth standard \| fast \| deep \| auto` | Two-stage fast/deep path |
| Planner mode | `--planner-mode direct \| single \| auto` | Skip planner provider vs full plan |

## Running from benchlab (external)

```bash
# In cc-loop-benchlab checkout:
cc-loop-benchlab run scenarios/reviewer_context_ab.yaml
cc-loop-benchlab report --run-id <id> --compare-variants
```

Benchlab should parse `implementer.prompt.metrics.json` and `review.prompt.metrics.json`
using the same field names as PromptOpsWorkbench `ccloop_report.py` (see P0-9 in
`docs/REVIEW_2026-07-03.md`).

## In-repo validation

`tests/test_benchlab_scenarios.py` validates scenario YAML structure and asserts that
config variants produce measurably different prompt metrics without requiring the
full benchlab runner.

## Recommended A/B scenarios

1. **reviewer_context_ab** — `inline` vs `artifact_refs` on a large-patch fixture
2. **review_depth_ab** — `fast` vs `deep` on the same passing small change
3. **planner_mode_ab** — `direct` vs `single` on a short goal

Expected outcomes after v0.10 prompt-cache work:

- `artifact_refs` reviewer variant: higher `contract_prefix_ratio`, lower `estimated_provider_prompt_tokens`
- `direct` planner variant: planner phase skipped in `prompt.cache.json` (`provider_skipped: true`)
- Implementer three-section layout: `task_context_ratio` > 0 with iteration/retry only in dynamic section
