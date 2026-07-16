//! status --json inspection for Luma.

use std::path::Path;

use serde_json::{json, Value};

use crate::config::{distinct_reviewer_satisfied, resolve_provider_model};
use crate::graph::build_graph_snapshot;
use crate::paths::{artifacts_dir, failure_report_path, task_dir, ArtifactPaths};
use crate::recovery::{decide_auto_step, derive_next_action_from_step};
use crate::runner::{is_heartbeat_stale, is_runner_alive, read_heartbeat};
use crate::state::{AttemptPhase, AttemptRecord, TaskState, TaskStatus};
use crate::version::{CC_LOOP_VERSION, INTEGRATION_SCHEMA_VERSION};

pub const SUCCESS_READY_FOR_HANDOFF: &str = "ready_for_handoff";
pub const SUCCESS_MERGED: &str = "merged";
pub const SUCCESS_STOPPED: &str = "stopped";
pub const SUCCESS_FAILED: &str = "failed";
pub const SUCCESS_CANCELLED: &str = "cancelled";
pub const SUCCESS_RUNNING: &str = "running";
pub const SUCCESS_INITIALIZED: &str = "initialized";

pub fn build_roles_snapshot(state: &TaskState) -> Value {
    let mut roles = serde_json::Map::new();
    for role in ["planner", "implementer", "reviewer"] {
        let provider = state
            .providers
            .get(role)
            .cloned()
            .unwrap_or_else(|| match role {
                "planner" => state.config.planner_provider.clone(),
                "implementer" => state.config.implementer_provider.clone(),
                "reviewer" => state.config.reviewer_provider.clone(),
                _ => String::new(),
            });
        let model = resolve_provider_model(&provider, &state.config);
        roles.insert(
            role.into(),
            json!({"provider": provider, "model": model}),
        );
    }
    Value::Object(roles)
}

pub fn derive_success_outcome(state: &TaskState, attempt: Option<&AttemptRecord>) -> String {
    match state.status {
        TaskStatus::Cancelled => return SUCCESS_CANCELLED.into(),
        TaskStatus::Failed => return SUCCESS_FAILED.into(),
        TaskStatus::Initialized => return SUCCESS_INITIALIZED.into(),
        TaskStatus::Running | TaskStatus::Interrupted | TaskStatus::Replanning => {
            return SUCCESS_RUNNING.into();
        }
        _ => {}
    }
    if let Some(a) = attempt {
        if a.phase == AttemptPhase::Merged {
            return SUCCESS_MERGED.into();
        }
    }
    if state.status == TaskStatus::Done {
        if let Some(a) = attempt {
            if a.phase == AttemptPhase::Approved && !state.config.auto_merge {
                return SUCCESS_READY_FOR_HANDOFF.into();
            }
            if a.phase == AttemptPhase::Merged {
                return SUCCESS_MERGED.into();
            }
        }
        if !state.config.auto_merge {
            return SUCCESS_READY_FOR_HANDOFF.into();
        }
        return SUCCESS_MERGED.into();
    }
    if state.status == TaskStatus::Stopped {
        if let Some(a) = attempt {
            if a.phase == AttemptPhase::Approved
                && a.decision == "approve"
                && !state.config.auto_merge
            {
                let complete = state
                    .task_graph
                    .as_ref()
                    .map(|g| g.is_complete())
                    .unwrap_or(true);
                if complete {
                    return SUCCESS_READY_FOR_HANDOFF.into();
                }
            }
        }
    }
    SUCCESS_STOPPED.into()
}

pub fn latest_reject_reason(state: &TaskState) -> String {
    for prev in state.history.iter().rev() {
        if prev.phase == AttemptPhase::Rejected {
            if let Some(ref rj) = prev.review_json {
                if let Some(r) = rj.get("reason").and_then(|v| v.as_str()) {
                    let t = r.trim();
                    if !t.is_empty() {
                        return t.chars().take(400).collect();
                    }
                }
                if let Some(r) = rj.get("retry_prompt").and_then(|v| v.as_str()) {
                    let t = r.trim();
                    if !t.is_empty() {
                        return t.chars().take(400).collect();
                    }
                }
            }
        }
    }
    String::new()
}

pub fn derive_next_action(
    state: &TaskState,
    attempt: Option<&AttemptRecord>,
    running: bool,
) -> String {
    let _ = attempt;
    let step = decide_auto_step(state);
    derive_next_action_from_step(step, running)
}

pub fn build_attempt_snapshot(
    state: &TaskState,
    attempt: &AttemptRecord,
    state_root: &Path,
) -> Value {
    let art = artifacts_dir(
        state_root,
        &state.task_id,
        attempt.iteration,
        attempt.retry,
    );
    json!({
        "iteration": attempt.iteration,
        "retry": attempt.retry,
        "phase": attempt.phase.as_str(),
        "decision": attempt.decision,
        "test_status": attempt.test_status,
        "implementer_exit_code": attempt.implementer_exit_code,
        "graph_node_id": attempt.graph_node_id,
        "artifact_dir": art.display().to_string(),
        "branch": attempt.branch,
        "worktree_path": attempt.worktree_path,
        "running_provider": attempt.running_provider,
        "head_commit": attempt.head_commit,
    })
}

pub fn build_failure_snapshot(
    attempt: Option<&AttemptRecord>,
    state_root: &Path,
    task_id: &str,
) -> Value {
    let path = failure_report_path(state_root, task_id);
    if path.is_file() {
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(v) = serde_json::from_str::<Value>(&text) {
                return v;
            }
        }
    }
    if let Some(a) = attempt {
        if !a.failure_type.is_empty() {
            return json!({
                "failure_type": a.failure_type,
                "message": a.stop_reason,
                "recovery_disposition": a.recovery_disposition,
            });
        }
    }
    Value::Null
}

pub fn build_status_json(state: &TaskState, state_root: &Path) -> Value {
    let attempt = state.latest_attempt();
    let (running, pid) = is_runner_alive(state_root, &state.task_id);
    let hb = read_heartbeat(state_root, &state.task_id);
    let stale = hb
        .as_ref()
        .map(|h| is_heartbeat_stale(h, state.config.stale_heartbeat_seconds))
        .unwrap_or(true);
    let roles = build_roles_snapshot(state);
    let success = derive_success_outcome(state, attempt);
    let reject = latest_reject_reason(state);
    let next_action = derive_next_action(state, attempt, running);

    let mut body = json!({
        "schema_version": INTEGRATION_SCHEMA_VERSION,
        "cc_loop_version": CC_LOOP_VERSION,
        "task_id": state.task_id,
        "goal": state.goal,
        "status": state.status.as_str(),
        "iteration": state.iteration,
        "base_branch": state.base_branch,
        "base_commit": state.base_commit,
        "target_repo": state.target_repo,
        "providers": state.providers,
        "roles": roles,
        "distinct_reviewer": distinct_reviewer_satisfied(&state.config, Some(&state.providers)),
        "require_distinct_reviewer": state.config.require_distinct_reviewer,
        "auto_merge": state.config.auto_merge,
        "planner_granularity": state.config.planner_granularity,
        "success": success,
        "latest_reject_reason": if reject.is_empty() { Value::Null } else { json!(reject) },
        "next_action": next_action,
        "runner": {
            "alive": running,
            "pid": pid,
            "heartbeat_stale": stale,
        },
        "task_dir": task_dir(state_root, &state.task_id).display().to_string(),
    });

    if let Some(a) = attempt {
        body.as_object_mut().unwrap().insert(
            "latest_attempt".into(),
            build_attempt_snapshot(state, a, state_root),
        );
        body.as_object_mut().unwrap().insert(
            "phase".into(),
            json!(a.phase.as_str()),
        );
    } else {
        body.as_object_mut()
            .unwrap()
            .insert("phase".into(), json!(""));
    }

    if let Some(ref g) = state.task_graph {
        body.as_object_mut()
            .unwrap()
            .insert("task_graph".into(), build_graph_snapshot(g));
    }

    let failure = build_failure_snapshot(attempt, state_root, &state.task_id);
    if !failure.is_null() {
        body.as_object_mut()
            .unwrap()
            .insert("failure".into(), failure);
    }

    body
}

pub fn list_tasks_json(state_root: &Path) -> crate::error::Result<Value> {
    let ids = crate::state::list_task_ids(state_root)?;
    let mut tasks = Vec::new();
    for id in ids {
        match crate::state::load_state(&id, state_root) {
            Ok(state) => {
                tasks.push(json!({
                    "task_id": state.task_id,
                    "status": state.status.as_str(),
                    "goal": state.goal,
                    "iteration": state.iteration,
                    "success": derive_success_outcome(&state, state.latest_attempt()),
                    "target_repo": state.target_repo,
                }));
            }
            Err(_) => continue,
        }
    }
    Ok(json!({"tasks": tasks}))
}

/// Artifact path helper used by summary/report.
pub fn attempt_artifact_paths(
    state: &TaskState,
    attempt: &AttemptRecord,
    state_root: &Path,
) -> ArtifactPaths {
    ArtifactPaths::new(artifacts_dir(
        state_root,
        &state.task_id,
        attempt.iteration,
        attempt.retry,
    ))
}
