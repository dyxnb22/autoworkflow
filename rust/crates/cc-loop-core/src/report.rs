//! Task report builder.

use std::path::Path;

use serde_json::{json, Value};

use crate::inspect::{
    attempt_artifact_paths, build_failure_snapshot, derive_next_action, derive_success_outcome,
    runner_state_label,
};
use crate::paths::{events_path, runner_log_path};
use crate::runner::is_runner_alive;
use crate::state::TaskState;

pub fn build_report(state: &TaskState, state_root: &Path) -> Value {
    let attempt = state.latest_attempt();
    let (running, _) = is_runner_alive(state_root, &state.task_id);
    let runner_state = runner_state_label(
        state_root,
        &state.task_id,
        state.config.stale_heartbeat_seconds,
    );
    let next_action = derive_next_action(state, attempt, running, &runner_state);
    let mut artifact_paths = json!({});
    if let Some(a) = attempt {
        let paths = attempt_artifact_paths(state, a, state_root);
        artifact_paths = json!(paths.as_string_map());
    }
    let review_decision = attempt
        .and_then(|a| a.review_json.clone())
        .map(|rj| {
            json!({
                "decision": rj.get("decision").cloned().unwrap_or(json!("")),
                "reason": rj.get("reason").cloned().unwrap_or(json!("")),
            })
        })
        .unwrap_or(json!({"decision":"", "reason":""}));

    let graph_progress = state.task_graph.as_ref().map(|g| g.status_summary());

    json!({
        "task_id": state.task_id,
        "status": state.status.as_str(),
        "success": derive_success_outcome(state, attempt),
        "iteration": state.iteration,
        "goal": state.goal,
        "suggested_next_action": next_action,
        "review_decision": review_decision,
        "artifact_paths": artifact_paths,
        "failure_summary": build_failure_snapshot(attempt, state_root, &state.task_id, Some(state)),
        "graph_progress": graph_progress,
        "events_path": events_path(state_root, &state.task_id).display().to_string(),
        "log_path": runner_log_path(state_root, &state.task_id).display().to_string(),
        "observability": {},
    })
}

pub fn format_report_human(report: &Value) -> String {
    format!(
        "Report: {}\nStatus: {}\nSuccess: {}\nNext: {}\n",
        report.get("task_id").and_then(|v| v.as_str()).unwrap_or(""),
        report.get("status").and_then(|v| v.as_str()).unwrap_or(""),
        report.get("success").and_then(|v| v.as_str()).unwrap_or(""),
        report
            .get("suggested_next_action")
            .and_then(|v| v.as_str())
            .unwrap_or("")
    )
}
