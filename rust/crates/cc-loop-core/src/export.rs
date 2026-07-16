//! Analytics-compatible JSONL export.

use std::fs;
use std::io::Write;
use std::path::Path;

use serde_json::json;

use crate::error::{CcError, Result};
use crate::inspect::{derive_success_outcome, latest_reject_reason};
use crate::state::load_state;
use crate::version::CC_LOOP_VERSION;

pub fn export_jsonl(state_root: &Path, task_id: &str, output: &Path) -> Result<u32> {
    let state = load_state(task_id, state_root)?;
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut f = fs::File::create(output)
        .map_err(|e| CcError::user(format!("cannot write export: {e}")))?;
    let mut rows = 0u32;

    let header = json!({
        "type": "task",
        "cc_loop_version": CC_LOOP_VERSION,
        "task_id": state.task_id,
        "goal": state.goal,
        "status": state.status.as_str(),
        "success": derive_success_outcome(&state, state.latest_attempt()),
        "auto_merge": state.config.auto_merge,
        "require_distinct_reviewer": state.config.require_distinct_reviewer,
        "latest_reject_reason": latest_reject_reason(&state),
    });
    writeln!(f, "{}", header)?;
    rows += 1;

    for attempt in &state.history {
        let row = json!({
            "type": "attempt",
            "task_id": state.task_id,
            "iteration": attempt.iteration,
            "retry": attempt.retry,
            "phase": attempt.phase.as_str(),
            "decision": attempt.decision,
            "test_status": attempt.test_status,
            "graph_node_id": attempt.graph_node_id,
            "branch": attempt.branch,
        });
        writeln!(f, "{}", row)?;
        rows += 1;
    }
    Ok(rows)
}
