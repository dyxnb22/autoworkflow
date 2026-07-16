//! Failure report helpers.

use std::path::Path;

use serde_json::{json, Value};

use crate::error::Result;
use crate::paths::failure_report_path;
use crate::state::{atomic_write_text, AttemptRecord};

pub fn write_failure_report(
    state_root: &Path,
    task_id: &str,
    attempt: &AttemptRecord,
    failure_type: &str,
    disposition: &str,
    message: &str,
    suggested_actions: &[&str],
) -> Result<Value> {
    let report = json!({
        "failure_type": failure_type,
        "disposition": disposition,
        "stop_reason": message,
        "message": message,
        "recovery_retry_count": attempt.recovery_retry_count,
        "merge_retry_count": attempt.merge_retry_count,
        "attempted_repairs": attempt.attempted_repairs,
        "suggested_actions": suggested_actions,
        "details": attempt.failure_details,
    });
    let path = failure_report_path(state_root, task_id);
    atomic_write_text(&path, &(serde_json::to_string_pretty(&report)? + "\n"))?;
    // Also write per-attempt copy
    if !attempt.worktree_path.is_empty() {
        // artifact dir inferred from diff paths if present
    }
    Ok(report)
}

pub fn write_attempt_failure_report(artifact_dir: &Path, report: &Value) -> Result<()> {
    std::fs::create_dir_all(artifact_dir)?;
    atomic_write_text(
        &artifact_dir.join("failure.report.json"),
        &(serde_json::to_string_pretty(report)? + "\n"),
    )?;
    Ok(())
}
