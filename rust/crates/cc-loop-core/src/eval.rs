//! Local eval suite runner against attempt artifacts.

use std::fs;
use std::path::Path;

use serde_json::{json, Value};

use crate::error::{CcError, Result};
use crate::inspect::attempt_artifact_paths;
use crate::state::{load_state, TaskState};

pub fn run_eval_suite(state_root: &Path, task_id: &str, suite_path: &Path) -> Result<Value> {
    let state = load_state(task_id, state_root)?;
    let suite_text = fs::read_to_string(suite_path)
        .map_err(|e| CcError::user(format!("cannot read suite: {e}")))?;
    let suite: Value = serde_json::from_str(&suite_text)?;
    let cases = suite
        .get("cases")
        .or_else(|| suite.get("tests"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let attempt = state.latest_attempt();
    let mut results = Vec::new();
    let mut passed = 0u32;
    let mut failed = 0u32;

    for case in cases {
        let name = case
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("unnamed")
            .to_string();
        let ok = eval_case(&state, state_root, attempt, &case);
        if ok {
            passed += 1;
        } else {
            failed += 1;
        }
        results.push(json!({"name": name, "ok": ok}));
    }

    Ok(json!({
        "task_id": task_id,
        "suite": suite_path.display().to_string(),
        "passed": passed,
        "failed": failed,
        "ok": failed == 0,
        "results": results,
    }))
}

fn eval_case(
    state: &TaskState,
    state_root: &Path,
    attempt: Option<&crate::state::AttemptRecord>,
    case: &Value,
) -> bool {
    let kind = case
        .get("type")
        .or_else(|| case.get("kind"))
        .and_then(|v| v.as_str())
        .unwrap_or("contains");

    match kind {
        "status_eq" => {
            let expected = case.get("status").and_then(|v| v.as_str()).unwrap_or("");
            state.status.as_str() == expected
        }
        "success_eq" => {
            let expected = case.get("success").and_then(|v| v.as_str()).unwrap_or("");
            crate::inspect::derive_success_outcome(state, attempt) == expected
        }
        "artifact_contains" => {
            let Some(a) = attempt else { return false };
            let key = case.get("artifact").and_then(|v| v.as_str()).unwrap_or("");
            let needle = case.get("contains").and_then(|v| v.as_str()).unwrap_or("");
            let paths = attempt_artifact_paths(state, a, state_root);
            let map = paths.as_string_map();
            let path = map.get(key).map(Path::new);
            let Some(path) = path else { return false };
            let text = fs::read_to_string(path).unwrap_or_default();
            text.contains(needle)
        }
        "review_decision" => {
            let expected = case.get("decision").and_then(|v| v.as_str()).unwrap_or("");
            attempt
                .and_then(|a| a.review_json.as_ref())
                .and_then(|r| r.get("decision"))
                .and_then(|v| v.as_str())
                .map(|d| d.eq_ignore_ascii_case(expected))
                .unwrap_or(false)
        }
        _ => {
            // Generic contains against goal
            let needle = case.get("contains").and_then(|v| v.as_str()).unwrap_or("");
            state.goal.contains(needle)
        }
    }
}
