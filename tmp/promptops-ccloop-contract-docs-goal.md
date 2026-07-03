# PromptOpsWorkbench cc-loop contract docs and smoke

Repository: /Users/diaoyuxuan/PromptOpsWorkbench

Implement a focused documentation and contract-smoke improvement for the cc-loop integration surface.

Context:

- `promptops ccloop-report ARTIFACT_DIR --format json|markdown` exists.
- `promptops ccloop-import ARTIFACT_DIR --output OUTPUT_DIR` supports:
  - `--index-json PATH`
  - `--format text|json`
  - `--task-prefix`
  - `--overwrite`
- Import index records include task/attempt identity, source/imported paths, review/test status, and cache recommendation fields.

Scope:

1. Update README ccloop sections so the documented CLI matches the current implementation:
   - `ccloop-import` usage includes `--index-json`, `--format text|json`, and `--task-prefix`.
   - Include one end-to-end example that runs `ccloop-report` then `ccloop-import` on a cc-loop task artifact root.
   - Include a compact JSON index schema example or field list.
   - Explain when to use `--task-prefix` to avoid collisions when importing multiple tasks into one prompt library.

2. Add a lightweight integration/smoke test or documented example test that verifies the documented command shape:
   - Build a small temporary cc-loop artifact root with `iter-001/attempt.trace.json`, `plan.prompt.txt`, and `review.parsed.json`.
   - Invoke the Click CLI for `ccloop-import` with `--index-json`, `--format json`, and `--task-prefix`.
   - Assert the JSON output and index file contain the expected imported path and task id.
   - Keep this in the existing test style; do not add new dependencies.

Non-goals:

- Do not add web UI artifact browsing in this task.
- Do not change existing import/report behavior except where a test reveals a real bug in the documented command contract.
- Do not redesign the index schema.

Acceptance:

- `python -m pytest tests -q` passes.
- README examples match the actual CLI options.
- Reviewer can see a clear cc-loop-to-PromptOps command contract for future cc-loop integration.
