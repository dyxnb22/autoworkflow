//! status --json aligned to docs/INTEGRATION.md schema v1.

use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::config::{distinct_reviewer_satisfied, resolve_provider_model};
use crate::graph::build_graph_snapshot;
use crate::paths::{
    artifacts_dir, events_path, failure_report_path, runner_log_path, state_path, task_dir,
    ArtifactPaths,
};
use crate::recovery::decide_auto_step;
use crate::recovery::AutoStep;
use crate::runner::{
    is_heartbeat_stale, is_runner_alive, read_heartbeat, read_pid, RunnerHeartbeat,
};
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
        roles.insert(role.into(), json!({"provider": provider, "model": model}));
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

pub fn format_test_command_display(cmd: &[String]) -> String {
    if cmd.is_empty() {
        return String::new();
    }
    cmd.join(" ")
}

pub fn wall_clock_elapsed_seconds(state: &TaskState) -> i64 {
    let Some(first) = state.history.first() else {
        return 0;
    };
    let Ok(ts) = chrono::DateTime::parse_from_rfc3339(&first.created_at) else {
        return 0;
    };
    let age = chrono::Utc::now().signed_duration_since(ts.with_timezone(&chrono::Utc));
    age.num_seconds().max(0)
}

pub fn runner_state_label(
    state_root: &Path,
    task_id: &str,
    stale_seconds: u64,
) -> String {
    let (alive, pid) = is_runner_alive(state_root, task_id);
    let hb = read_heartbeat(state_root, task_id);
    if alive {
        if let Some(ref h) = hb {
            if is_heartbeat_stale(h, stale_seconds) {
                return "stale_heartbeat".into();
            }
        }
        return "running".into();
    }
    if pid.is_some() && !alive {
        return "stale_pid".into();
    }
    if read_pid(state_root, task_id).is_none() && hb.is_none() {
        return "idle".into();
    }
    "stopped".into()
}

fn capability_flags(state: &TaskState, running: bool) -> (bool, bool, bool) {
    let can_stop = running;
    let can_resume = matches!(
        state.status,
        TaskStatus::Stopped
            | TaskStatus::Interrupted
            | TaskStatus::Running
            | TaskStatus::Replanning
    );
    let can_cleanup = matches!(
        state.status,
        TaskStatus::Stopped
            | TaskStatus::Done
            | TaskStatus::Failed
            | TaskStatus::Cancelled
            | TaskStatus::Initialized
    ) && !running;
    (can_stop, can_resume, can_cleanup)
}

pub fn derive_next_action(
    state: &TaskState,
    attempt: Option<&AttemptRecord>,
    running: bool,
    runner_state: &str,
) -> String {
    if running {
        return "none".into();
    }
    if runner_state == "stale_heartbeat" {
        return "resume".into();
    }
    if state.status == TaskStatus::Initialized && state.history.is_empty() {
        return "run".into();
    }
    let step = decide_auto_step(state);
    match step {
        AutoStep::Done => "done".into(),
        AutoStep::Fail => "failed".into(),
        AutoStep::Resume => "resume".into(),
        AutoStep::Stop => {
            if attempt.map(|a| a.decision == "stop").unwrap_or(false) {
                "inspect".into()
            } else if !attempt
                .map(|a| a.failure_type.is_empty())
                .unwrap_or(true)
            {
                "terminal".into()
            } else {
                "inspect".into()
            }
        }
        AutoStep::Replan => "resume".into(),
        AutoStep::Repair => "repair".into(),
    }
}

pub fn derive_current_message(
    state: &TaskState,
    attempt: Option<&AttemptRecord>,
    running: bool,
    runner_state: &str,
    live_phase: &str,
    live_provider: &str,
) -> String {
    if runner_state == "stale_heartbeat" {
        return if running {
            "Runner heartbeat is stale but process is still alive — cancel or wait".into()
        } else {
            "Runner heartbeat is stale with no live runner — safe to resume or cleanup".into()
        };
    }
    let phase = if live_phase.is_empty() {
        attempt.map(|a| a.phase.as_str()).unwrap_or("")
    } else {
        live_phase
    };
    if running {
        return match phase {
            "reviewing" => {
                if live_provider.is_empty() {
                    "Review in progress".into()
                } else {
                    format!("Reviewer running ({live_provider})")
                }
            }
            "testing" => "Running tests".into(),
            "executing" => {
                if live_provider.is_empty() {
                    "Implementer running".into()
                } else {
                    format!("Implementer running ({live_provider})")
                }
            }
            "planning" => {
                if live_provider.is_empty() {
                    "Planning".into()
                } else {
                    format!("Planner running ({live_provider})")
                }
            }
            _ => "Auto runner active".into(),
        };
    }
    match state.status {
        TaskStatus::Done => return "Task completed".into(),
        TaskStatus::Cancelled => return "Task cancelled".into(),
        TaskStatus::Replanning => return "Replanning task graph".into(),
        _ => {}
    }
    let Some(a) = attempt else {
        return "Ready to run".into();
    };
    if !a.merge_error.is_empty() {
        return "Merge failed — recovery available".into();
    }
    if a.phase == AttemptPhase::Merged {
        return "Merged successfully".into();
    }
    if a.decision == "stop" {
        return "Reviewer requested stop".into();
    }
    if a.phase == AttemptPhase::Rejected {
        return "Reviewer rejected — retry available".into();
    }
    if a.phase == AttemptPhase::Approved {
        if state.config.auto_merge {
            return "Approved — pending merge".into();
        }
        return "Approved — ready for handoff".into();
    }
    format!("Phase: {}", a.phase.as_str())
}

pub fn build_attempt_snapshot(
    state: &TaskState,
    attempt: Option<&AttemptRecord>,
    state_root: &Path,
) -> Value {
    let Some(attempt) = attempt else {
        return json!({
            "iteration": 0,
            "retry": 0,
            "phase": "",
            "decision": "",
            "test_status": "",
            "implementer_exit_code": 0,
            "worktree_path": "",
            "merge_error": "",
            "artifact_dir": "",
            "created_at": "",
            "graph_node_id": "",
            "running_provider": "",
        });
    };
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
        "implementer_exit_code": attempt.implementer_exit_code.unwrap_or(0),
        "worktree_path": attempt.worktree_path,
        "merge_error": attempt.merge_error,
        "artifact_dir": art.display().to_string(),
        "created_at": attempt.created_at,
        "graph_node_id": attempt.graph_node_id,
        "running_provider": attempt.running_provider,
        "branch": attempt.branch,
        "head_commit": attempt.head_commit,
    })
}

pub fn empty_failure_snapshot(attempt: Option<&AttemptRecord>) -> Value {
    json!({
        "failure_type": "",
        "disposition": "",
        "stop_reason": "",
        "recovery_retry_count": attempt.map(|a| a.recovery_retry_count).unwrap_or(0),
        "merge_retry_count": attempt.map(|a| a.merge_retry_count).unwrap_or(0),
        "attempted_repairs": attempt.map(|a| a.attempted_repairs.clone()).unwrap_or_default(),
        "suggested_actions": [],
        "details": {},
    })
}

pub fn build_failure_snapshot(
    attempt: Option<&AttemptRecord>,
    state_root: &Path,
    task_id: &str,
    state: Option<&TaskState>,
) -> Value {
    let path = failure_report_path(state_root, task_id);
    if path.is_file() {
        if let Ok(text) = fs::read_to_string(&path) {
            if let Ok(v) = serde_json::from_str::<Value>(&text) {
                if v.get("failure_type")
                    .and_then(|x| x.as_str())
                    .map(|s| !s.is_empty())
                    .unwrap_or(false)
                {
                    return v;
                }
            }
        }
    }
    // Also check attempt-level artifact failure report
    if let Some(a) = attempt {
        let art = artifacts_dir(state_root, task_id, a.iteration, a.retry);
        let local = art.join("failure.report.json");
        if local.is_file() {
            if let Ok(text) = fs::read_to_string(&local) {
                if let Ok(v) = serde_json::from_str::<Value>(&text) {
                    return v;
                }
            }
        }
        if let Some(st) = state {
            if st.status == TaskStatus::Done
                && a.phase == AttemptPhase::Merged
                && a.merge_error.is_empty()
            {
                return empty_failure_snapshot(Some(a));
            }
            if a.phase == AttemptPhase::Approved
                && a.decision == "approve"
                && a.merge_error.is_empty()
                && a.failure_type.is_empty()
            {
                return empty_failure_snapshot(Some(a));
            }
        }
        if !a.failure_type.is_empty() {
            return json!({
                "failure_type": a.failure_type,
                "disposition": if a.recovery_disposition.is_empty() { "terminal" } else { &a.recovery_disposition },
                "stop_reason": a.stop_reason,
                "recovery_retry_count": a.recovery_retry_count,
                "merge_retry_count": a.merge_retry_count,
                "attempted_repairs": a.attempted_repairs,
                "suggested_actions": [],
                "details": a.failure_details,
            });
        }
        if a.phase == AttemptPhase::Rejected || a.decision == "reject" || a.decision == "stop" {
            return json!({
                "failure_type": if a.decision == "stop" { "reviewer_stop" } else { "reviewer_reject" },
                "disposition": if a.decision == "reject" { "recoverable" } else { "terminal" },
                "stop_reason": a.stop_reason,
                "recovery_retry_count": a.recovery_retry_count,
                "merge_retry_count": a.merge_retry_count,
                "attempted_repairs": a.attempted_repairs,
                "suggested_actions": [],
                "details": a.failure_details,
            });
        }
        return empty_failure_snapshot(Some(a));
    }
    empty_failure_snapshot(None)
}

fn resolve_live_fields(
    attempt: Option<&AttemptRecord>,
    running: bool,
    hb: Option<&RunnerHeartbeat>,
    stale_seconds: u64,
) -> (String, String) {
    let mut phase = attempt.map(|a| a.phase.as_str().to_string()).unwrap_or_default();
    let mut provider = attempt
        .map(|a| a.running_provider.clone())
        .unwrap_or_default();
    if !running {
        return (phase, provider);
    }
    let Some(hb) = hb else {
        return (phase, provider);
    };
    if is_heartbeat_stale(hb, stale_seconds) {
        return (phase, provider);
    }
    if !hb.running_provider.is_empty() {
        provider = hb.running_provider.clone();
    }
    if matches!(
        hb.phase.as_str(),
        "planning" | "executing" | "reviewing" | "testing"
    ) {
        phase = hb.phase.clone();
    }
    (phase, provider)
}

fn read_prompt_cache_snapshot(state: &TaskState, attempt: Option<&AttemptRecord>, state_root: &Path) -> Option<Value> {
    let a = attempt?;
    let paths = ArtifactPaths::new(artifacts_dir(
        state_root,
        &state.task_id,
        a.iteration,
        a.retry,
    ));
    if !paths.prompt_cache.is_file() {
        return None;
    }
    let text = fs::read_to_string(&paths.prompt_cache).ok()?;
    let data: Value = serde_json::from_str(&text).ok()?;
    Some(json!({
        "path": paths.prompt_cache.display().to_string(),
        "total_prompt_tokens": data.get("total_prompt_tokens").cloned().unwrap_or(Value::Null),
        "review_context_mode": data.get("review_context_mode").cloned().unwrap_or(Value::Null),
        "omitted_patch_chars": data.get("omitted_patch_chars").cloned().unwrap_or(Value::Null),
    }))
}

fn read_reviewer_metrics(state: &TaskState, attempt: Option<&AttemptRecord>, state_root: &Path) -> Option<Value> {
    let a = attempt?;
    let path = artifacts_dir(state_root, &state.task_id, a.iteration, a.retry)
        .join("review.prompt.metrics.json");
    if !path.is_file() {
        return None;
    }
    let text = fs::read_to_string(&path).ok()?;
    let data: Value = serde_json::from_str(&text).ok()?;
    Some(json!({
        "layout": data.get("layout"),
        "stable_prefix_ratio": data.get("stable_prefix_ratio"),
        "contract_prefix_ratio": data.get("contract_prefix_ratio"),
        "cache_health": data.get("cache_health"),
        "total_prompt_cache_health": data.get("total_prompt_cache_health"),
        "estimated_prompt_tokens": data.get("estimated_prompt_tokens"),
        "omitted_patch_chars": data.get("omitted_patch_chars"),
        "context_mode": data.get("context_mode"),
        "inline_patch": data.get("inline_patch"),
    }))
}

/// Full status snapshot matching INTEGRATION.md.
pub fn build_status_json(state: &TaskState, state_root: &Path) -> Value {
    let attempt = state.latest_attempt();
    let mut running_pair = is_runner_alive(state_root, &state.task_id);
    let hb = read_heartbeat(state_root, &state.task_id);
    let stale_seconds = state.config.stale_heartbeat_seconds.max(1);
    let mut runner_state = runner_state_label(state_root, &state.task_id, stale_seconds);

    if !matches!(state.status, TaskStatus::Running | TaskStatus::Replanning)
        && read_pid(state_root, &state.task_id).is_none()
        && hb.is_some()
    {
        running_pair.0 = false;
        if let Some(ref h) = hb {
            if h.pid != 0 {
                running_pair.1 = Some(h.pid);
            }
        }
        runner_state = "stopped".into();
    }

    let (running, runner_pid) = running_pair;
    let (live_phase, live_provider) =
        resolve_live_fields(attempt, running, hb.as_ref(), stale_seconds);
    let next_action = derive_next_action(state, attempt, running, &runner_state);
    let (can_stop, can_resume, can_cleanup) = capability_flags(state, running);
    let reject = latest_reject_reason(state);
    let mut attempt_snap = build_attempt_snapshot(state, attempt, state_root);
    if let Some(obj) = attempt_snap.as_object_mut() {
        if !live_phase.is_empty() {
            obj.insert("phase".into(), json!(live_phase));
        }
        obj.insert("running_provider".into(), json!(live_provider));
    }

    let mut snapshot = json!({
        "schema_version": INTEGRATION_SCHEMA_VERSION,
        "cc_loop_version": CC_LOOP_VERSION,
        "task_id": state.task_id,
        "goal": state.goal,
        "target_repo": state.target_repo,
        "base_branch": state.base_branch,
        "base_commit": state.base_commit,
        "status": state.status.as_str(),
        "iteration": state.iteration,
        "test_command_argv": state.config.test_command,
        "test_command_display": format_test_command_display(&state.config.test_command),
        "providers": state.providers,
        "roles": build_roles_snapshot(state),
        "distinct_reviewer": distinct_reviewer_satisfied(&state.config, Some(&state.providers)),
        "require_distinct_reviewer": state.config.require_distinct_reviewer,
        "auto_merge": state.config.auto_merge,
        "planner_granularity": state.config.planner_granularity,
        "success": derive_success_outcome(state, attempt),
        "latest_reject_reason": if reject.is_empty() { Value::Null } else { json!(reject) },
        "attempt": attempt_snap,
        "failure": build_failure_snapshot(attempt, state_root, &state.task_id, Some(state)),
        "next_action": next_action,
        "running": running,
        "runner_pid": runner_pid,
        "runner_state": runner_state,
        "last_heartbeat_at": hb.as_ref().map(|h| h.updated_at.clone()).unwrap_or_default(),
        "runner_started_at": hb.as_ref().map(|h| h.started_at.clone()).unwrap_or_default(),
        "elapsed_seconds": wall_clock_elapsed_seconds(state),
        "log_path": runner_log_path(state_root, &state.task_id).display().to_string(),
        "current_message": derive_current_message(
            state, attempt, running, &runner_state, &live_phase, &live_provider
        ),
        "can_stop": can_stop,
        "can_resume": can_resume,
        "can_cleanup": can_cleanup,
        "task_dir": task_dir(state_root, &state.task_id).display().to_string(),
        "events_path": events_path(state_root, &state.task_id).display().to_string(),
    });

    if let Some(ref h) = hb {
        if !is_heartbeat_stale(h, stale_seconds) {
            let mut hb_obj = json!({
                "phase": h.phase,
                "running_provider": h.running_provider,
                "updated_at": h.updated_at,
            });
            if !h.provider_progress.is_null() {
                hb_obj
                    .as_object_mut()
                    .unwrap()
                    .insert("provider_progress".into(), h.provider_progress.clone());
            }
            snapshot
                .as_object_mut()
                .unwrap()
                .insert("heartbeat".into(), hb_obj);
        }
    }

    if runner_state == "stale_heartbeat" {
        snapshot.as_object_mut().unwrap().insert(
            "stale_heartbeat_guidance".into(),
            json!({
                "resume": if running {
                    "risk: may start a second provider while the existing runner is still alive"
                } else {
                    "recommended: no live runner detected; resume from saved phase"
                },
                "cancel": if running {
                    "recommended: stop the hung runner and mark the task cancelled"
                } else {
                    "mark task cancelled without starting new work"
                },
                "cleanup": "remove runner pid/heartbeat/worktrees after confirming no live process",
            }),
        );
    }

    if let Some(ref g) = state.task_graph {
        snapshot
            .as_object_mut()
            .unwrap()
            .insert("task_graph".into(), build_graph_snapshot(g));
        if state.running_attempts.len() > 1 {
            snapshot.as_object_mut().unwrap().insert(
                "running_node_ids".into(),
                json!(state.running_attempts.keys().collect::<Vec<_>>()),
            );
        }
    }

    if let Some(metrics) = read_reviewer_metrics(state, attempt, state_root) {
        snapshot
            .as_object_mut()
            .unwrap()
            .insert("reviewer_prompt_metrics".into(), metrics);
    }
    if let Some(pc) = read_prompt_cache_snapshot(state, attempt, state_root) {
        snapshot
            .as_object_mut()
            .unwrap()
            .insert("prompt_cache".into(), pc);
    }

    snapshot
}

pub fn state_mtime_iso(state_root: &Path, task_id: &str) -> String {
    let path = state_path(state_root, task_id);
    let meta = match fs::metadata(&path) {
        Ok(m) => m,
        Err(_) => return String::new(),
    };
    let modified = match meta.modified() {
        Ok(t) => t,
        Err(_) => return String::new(),
    };
    let dur = modified
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    chrono::DateTime::from_timestamp(dur, 0)
        .map(|dt| dt.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
        .unwrap_or_default()
}

/// list --json is a JSON array per INTEGRATION.md.
pub fn list_tasks_json(state_root: &Path, repo_filter: Option<&Path>) -> crate::error::Result<Value> {
    let ids = crate::state::list_task_ids(state_root)?;
    let mut tasks = Vec::new();
    for id in ids {
        let state = match crate::state::load_state(&id, state_root) {
            Ok(s) => s,
            Err(_) => continue,
        };
        if let Some(repo) = repo_filter {
            let target = Path::new(&state.target_repo);
            let ok = target
                .canonicalize()
                .ok()
                .and_then(|t| repo.canonicalize().ok().map(|r| t == r))
                .unwrap_or(false);
            if !ok {
                continue;
            }
        }
        let phase = state
            .latest_attempt()
            .map(|a| a.phase.as_str().to_string())
            .unwrap_or_else(|| "-".into());
        tasks.push(json!({
            "task_id": state.task_id,
            "status": state.status.as_str(),
            "target_repo": state.target_repo,
            "phase": phase,
            "updated_at": state_mtime_iso(state_root, &id),
            "goal": state.goal,
            "iteration": state.iteration,
            "success": derive_success_outcome(&state, state.latest_attempt()),
        }));
    }
    tasks.sort_by(|a, b| {
        let au = a.get("updated_at").and_then(|v| v.as_str()).unwrap_or("");
        let bu = b.get("updated_at").and_then(|v| v.as_str()).unwrap_or("");
        bu.cmp(au)
    });
    Ok(Value::Array(tasks))
}

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

// Silence unused import if SystemTime only used via meta
#[allow(dead_code)]
fn _now() -> SystemTime {
    SystemTime::now()
}
