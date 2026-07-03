Improve PromptOps Workbench for deeper future integration with cc-loop.

Target repository:
- /Users/diaoyuxuan/PromptOpsWorkbench

Current baseline:
- PromptOps already supports prompt parsing/validation/rendering/diff/report.
- It has `promptops cache` for cc-loop iter-* artifact directories.
- It has `promptops ccloop-import ARTIFACT_DIR --output OUTPUT_DIR [--overwrite]`, implemented in `src/promptops/ccloop_import.py`.
- Tests currently pass with `python -m pytest tests/ -q`.

Goal:
Add a run-level cc-loop artifact reporting and indexing layer, and enhance existing cc-loop import metadata so PromptOps can inspect a whole cc-loop run, not only individual prompt files.

Required behavior:

1. Add `promptops ccloop-report ARTIFACT_DIR --format markdown|json`
- Implement a cohesive module such as `src/promptops/ccloop_report.py`.
- Accept a cc-loop artifact root containing attempt directories such as `iter-001`, `iter-001-retry-01`, etc.
- Also support a single attempt directory where reasonable.
- For every attempt, summarize:
  - task_id
  - iteration
  - retry
  - graph_node_id
  - phase status from `attempt.trace.json` when present
  - prompt files found by role: planner/implementer/reviewer
  - test status and exit code from trace and/or `test.output.txt`
  - reviewer decision and reason from `review.parsed.json`
  - failure type / stop reason from `failure.report.json` when present
  - cache health metrics for each prompt role across attempts
- JSON output must be deterministic and machine-readable.
- Markdown output must be concise and useful in a terminal.

2. Add artifact index generation during import
- Extend `promptops ccloop-import` with:
  - `--index-json PATH` to write an index JSON file mapping source artifacts to imported prompt files.
  - `--format text|json` where text preserves current human output, and json prints `ImportResult.to_dict()`.
  - `--task-prefix` boolean option to prefix output filenames and prompt ids with task id when available, avoiding collisions across multiple task imports.
- The index JSON should include, per imported prompt:
  - task_id
  - iteration
  - retry
  - graph_node_id
  - role
  - source artifact path
  - imported prompt path
  - review decision if available for the attempt
  - test status if available for the attempt
  - cache_health
  - stable_prefix_ratio
  - estimated_cache_miss_tokens
  - recommendations
- Keep backward compatibility: existing import behavior without new flags should remain unchanged.

3. Add cache metadata to imported prompt front matter
- When importing a task artifact root with multiple attempts, compute cache analysis per role, consistent with existing `promptops cache`.
- Add front matter fields where available:
  - cache_health
  - stable_prefix_ratio
  - estimated_cache_miss_tokens
  - cache_recommendations
- For baseline prompts, use existing baseline semantics from cache_analysis where appropriate.
- Preserve exact prompt body after front matter.

4. Strengthen artifact directory parsing
- Support attempt directories named both:
  - `iter-001`
  - `iter-001-retry-01`
- Parse iteration/retry explicitly from directory names.
- Fall back to `attempt.trace.json` values when available.
- Ensure deterministic ordering: iteration ascending, retry ascending, then role order.

5. Lightweight Web UI integration
- Extend the existing web UI minimally so it can browse a cc-loop artifact root.
- Prefer simple routes/views over large frontend rewrites.
- A user should be able to open a cc-loop artifact root and see attempts grouped by iteration/retry with prompt roles, test result, reviewer decision, and cache health.
- Keep the UI local/offline and covered by focused tests if the current test style supports it.

6. Documentation
- Update README command docs for:
  - `promptops ccloop-report`
  - new `ccloop-import` flags
  - index JSON purpose
  - cache metadata in imported prompt front matter
  - light web UI artifact browsing if implemented
- Update project layout/module list.

Implementation guidance:
- Follow existing project style: small modules, dataclasses where helpful, Click CLI style, deterministic output.
- Reuse existing parser, cache_analysis, ccloop_import helpers where practical.
- Avoid new third-party dependencies.
- Do not break existing CLI output or tests.
- Keep the web UI changes modest and maintainable.

Important cc-loop planner constraint:
- Every task graph node `kind` must be one of: `implementation`, `test`, `docs`, `refactor`, `review`, `integration`.
- The current cc-loop can normalize some aliases, but still prefer valid canonical kinds.

Tests to add/update:
- `ccloop-report` JSON and Markdown for a multi-attempt artifact root.
- Attempt directory parsing for `iter-001-retry-01`.
- Import with cache front matter metadata.
- Import with `--index-json`.
- Import with `--format json`.
- Import with `--task-prefix`.
- Web UI route tests for cc-loop artifact browsing if implemented.
- Existing tests must continue passing.

Verification:
- Run `python -m pytest tests/ -q`.
