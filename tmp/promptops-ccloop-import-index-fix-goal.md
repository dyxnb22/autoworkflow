Improve PromptOps Workbench ccloop-import metadata/index behavior.

Target repository:
- /Users/diaoyuxuan/PromptOpsWorkbench

Current baseline:
- The previous cc-loop task `promptops-ccloop-report` successfully merged:
  - T1 shared artifact model in `src/promptops/ccloop_report.py`
  - T2 `promptops ccloop-report ARTIFACT_DIR --format markdown|json`
- Current tests pass with `python -m pytest tests/ -q`.
- The previous T3 failed reviewer after retries. Use the reviewer feedback below as the precise contract to satisfy.

Goal:
Enhance `promptops ccloop-import` with index JSON, JSON CLI output, task prefixing, and per-prompt cache metadata while preserving backward compatibility.

Required CLI/API behavior:

1. Extend `promptops ccloop-import ARTIFACT_DIR --output OUTPUT_DIR` with:
   - `--index-json PATH`
   - `--format text|json`
   - `--task-prefix`

2. Preserve default behavior:
   - Existing command without new flags must keep current text output and deterministic filenames.
   - Plain imports must not parse optional review/failure artifacts in a way that can fail the import.
   - If optional review/failure artifacts are malformed, a plain import without `--index-json` must still succeed.
   - If `--index-json` needs optional metadata and hits malformed JSON, raise/catch a helpful CLI error using existing Click error style.

3. Index JSON:
   - Write machine-readable deterministic index JSON when `--index-json PATH` is provided.
   - The index should map each imported prompt source to its imported prompt file.
   - Every index record must always include the full schema, using null for unavailable scalar fields and [] for recommendation lists:
     - task_id
     - iteration
     - retry
     - graph_node_id
     - role
     - source_path
     - imported_path
     - prompt_id
     - review_decision
     - test_status
     - cache_health
     - stable_prefix_ratio
     - estimated_cache_miss_tokens
     - recommendations

4. Per-prompt cache metadata:
   - When importing a task artifact root with multiple attempts, compute cache analysis per role using existing `cache_analysis` behavior.
   - Add front matter fields per imported prompt, not a single aggregate value per role:
     - cache_health
     - stable_prefix_ratio
     - estimated_cache_miss_tokens
     - cache_recommendations
   - Baseline prompts must use existing baseline semantics from cache_analysis.
   - Do not add misleading aggregate values to every prompt.

5. Task prefix:
   - With `--task-prefix`, prefix output filenames and prompt ids with a deterministic safe slug derived from task_id.
   - Do not use raw task_id in filenames or prompt ids.
   - Sanitize path separators, `..`, spaces, and unsafe punctuation.
   - Preserve the original task_id in front matter and index metadata.
   - Without `--task-prefix`, filenames and ids must remain backward compatible.

6. Body preservation:
   - Preserve prompt body content exactly after front matter, including CRLF newline style.
   - Add a regression test for CRLF body preservation.

7. Tests:
   - Add/update focused tests in `tests/test_ccloop_import.py` and CLI tests as needed:
     - import with `--index-json`
     - import with `--format json`
     - import with `--task-prefix`
     - unsafe task_id slugging
     - index records include all required keys even without review/test/cache data
     - malformed `review.parsed.json` does not break default import without `--index-json`
     - malformed optional metadata produces helpful error with `--index-json`
     - per-attempt baseline/cache front matter values
     - CRLF body preservation

Implementation guidance:
- Reuse `src/promptops/ccloop_report.py` where safe.
- Keep changes scoped to import/index/cache metadata and CLI wiring.
- Avoid new dependencies.
- Follow existing Click style and dataclass style.
- Do not modify the web UI or README in this focused task.

Important:
- Do not weaken tests.
- Do not change existing default import output text.
- Run `python -m pytest tests/ -q`.

Verification:
- `python -m pytest tests/ -q` must pass.
