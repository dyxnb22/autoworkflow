Improve PromptOps Workbench for future integration with cc-loop by adding a local bridge that imports cc-loop attempt prompt artifacts into a normal PromptOps prompt library.

Context:
- The project is /Users/diaoyuxuan/PromptOpsWorkbench.
- Existing functionality includes prompt parsing/validation/rendering/diff/report, a web UI, and `promptops cache` for cc-loop iter-* artifact directories.
- cc-loop v0.10 writes attempt artifacts such as:
  - iter-001/plan.prompt.txt
  - iter-001/plan.prompt.meta.json
  - iter-001/implementer.prompt.txt
  - iter-001/implementer.prompt.meta.json
  - iter-001/review.prompt.txt
  - iter-001/review.prompt.meta.json
  - iter-001/attempt.trace.json

Goal:
Add a deterministic offline command that converts cc-loop prompt artifacts into Markdown prompt files with YAML front matter so they can be managed by PromptOps Workbench using validate/diff/report/render/cache.

Required behavior:
1. Add a cohesive module, e.g. `src/promptops/ccloop_import.py`.
2. Add CLI command:
   `promptops ccloop-import ARTIFACT_DIR --output OUTPUT_DIR [--overwrite]`
3. ARTIFACT_DIR may be either:
   - a cc-loop task artifact root containing `iter-*` directories, or
   - a single attempt directory containing the prompt files directly.
4. For each found prompt file (`plan.prompt.txt`, `implementer.prompt.txt`, `review.prompt.txt`), write one Markdown file under OUTPUT_DIR.
5. Output filenames must be deterministic and avoid collisions, e.g. `iter-001-plan.md`, `iter-001-implementer.md`, `iter-001-review.md` for task roots, and `plan.md` etc. for single attempt directories.
6. Markdown front matter must include PromptOps required fields:
   - `id` stable and unique, such as `cc-loop-iter-001-plan`
   - `title`
   - `role` (`planner`, `implementer`, `reviewer`)
   - `version`, preferably from prompt metadata `prompt_version`, fallback `0.0.0`
   - `description`
   - `tags`, including `cc-loop` and the role
7. Preserve useful cc-loop metadata in extra front matter fields when available, e.g. `source_path`, `task_id`, `iteration`, `retry`, `graph_node_id`, `provider`, `model`, `prompt_name`, `prompt_label`, `prompt_layout`.
8. The Markdown body must exactly contain the original prompt text after front matter.
9. By default, refuse to overwrite existing files and exit with a helpful error. With `--overwrite`, replace generated files.
10. Print deterministic human output summarizing imported file count and output directory.
11. Add JSON-serializable return models or simple dataclasses as fits existing style.
12. Update README command documentation and project layout/module list.
13. Add focused tests for:
   - importing a task artifact root with two iterations and metadata files
   - importing a single attempt directory without metadata fallbacks
   - overwrite refusal and --overwrite behavior
   - CLI command success and error handling
   - generated files parse with existing PromptOps parser and pass lint where reasonable
14. Keep dependencies unchanged unless absolutely necessary.
15. `python -m pytest tests/ -q` must pass.

Implementation guidance:
- Follow existing style: small modules, dataclasses where helpful, Click CLI patterns, deterministic output.
- Use PyYAML already present in dependencies for front matter emission; avoid ad-hoc escaping where practical.
- Be careful not to disturb existing cache-analysis behavior.

Important cc-loop planner constraint:
- Every task graph node `kind` must be one of: `implementation`, `test`, `docs`, `refactor`, `review`, `integration`.
- Use `implementation` for coding and tests, `docs` for README, and `test` only if the node's owner can implement test files.
- Do not use unsupported kinds such as `testing`, `documentation`, or `verification`.
