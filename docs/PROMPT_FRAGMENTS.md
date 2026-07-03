# Prompt fragments (P1-2)

Stable prompt text lives in `src/cc_loop/prompts/` as plain `.txt` files. `run.py` loads and composes them at runtime.

## Layout

```
src/cc_loop/prompts/
  planner/     stable_prefix, schemas, graph_rules, dynamic_intro
  implementer/ stable_contract, task_context intros, node_instructions
  reviewer/    stable_contract, rubric, JSON contracts, fast review fragments
```

## Override

- Global CLI: `cc-loop --prompts-dir /path/to/custom/prompts init ...`
- Persisted config: `prompts_dir` in `state.json` (set at init)

Override files use the same relative paths as the package (e.g. `reviewer/stable_contract.txt`).

## Loader

`cc_loop.prompts.load_prompt_fragment(relative_path, config=state.config, **format_vars)`

Fragments are cached in memory per `(prompts_dir, relative_path)`.
