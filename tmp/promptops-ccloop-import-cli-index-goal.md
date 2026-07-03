# PromptOpsWorkbench cc-loop import CLI index wiring

Repository: /Users/diaoyuxuan/PromptOpsWorkbench

Implement a focused fix for `promptops ccloop-import` so cc-loop can call the importer index feature from the CLI.

Scope:

- Add `--index-json PATH` to the `ccloop-import` Click command.
- Pass the option through to `import_ccloop_artifacts(index_json=...)`.
- Preserve the existing default text output and existing `Error: ...` handling for `CcLoopImportError` / `OSError`.
- Add a focused CLI test that invokes:
  `promptops ccloop-import ARTIFACT_DIR --output OUTPUT_DIR --index-json INDEX_PATH`
  and asserts:
  - exit code is 0
  - imported prompt exists
  - index file exists
  - JSON contains the expected record fields/paths
- Add or adjust a CLI test proving malformed optional metadata with `--index-json` exits 1 with the existing helpful `Error: Invalid JSON artifact...` style.

Non-goals:

- Do not redesign the import metadata model.
- Do not change default import behavior when `--index-json` is omitted.
- Do not add web UI work in this task.

Acceptance:

- `python -m pytest tests/ -q` passes.
- Reviewer can call `promptops ccloop-import ARTIFACT_DIR --output OUTPUT_DIR --index-json INDEX_PATH` successfully.
