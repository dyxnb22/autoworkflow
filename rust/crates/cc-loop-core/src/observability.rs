//! Attempt trace + execution timeline + prompt cache snapshots.

use std::fs;
use std::path::Path;

use serde_json::{json, Value};

use crate::error::Result;
use crate::paths::{task_dir, ArtifactPaths};
use crate::state::{atomic_write_text, TaskState, utc_now_iso};

pub fn write_attempt_trace(paths: &ArtifactPaths, events: &[Value]) -> Result<()> {
    let payload = json!({
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "events": events,
    });
    atomic_write_text(
        &paths.attempt_trace,
        &(serde_json::to_string_pretty(&payload)? + "\n"),
    )?;
    Ok(())
}

pub fn write_prompt_cache(
    paths: &ArtifactPaths,
    review_context_mode: &str,
    omitted_patch_chars: usize,
    estimated_tokens: usize,
) -> Result<()> {
    let payload = json!({
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "review_context_mode": review_context_mode,
        "omitted_patch_chars": omitted_patch_chars,
        "total_prompt_tokens": estimated_tokens,
        "estimated_prompt_tokens": estimated_tokens,
    });
    atomic_write_text(
        &paths.prompt_cache,
        &(serde_json::to_string_pretty(&payload)? + "\n"),
    )?;
    Ok(())
}

pub fn write_review_prompt_metrics(
    artifact_root: &Path,
    context_mode: &str,
    inline_patch: bool,
    omitted_patch_chars: usize,
    estimated_tokens: usize,
) -> Result<()> {
    let payload = json!({
        "layout": "stable_contract_task_dynamic",
        "context_mode": context_mode,
        "inline_patch": inline_patch,
        "omitted_patch_chars": omitted_patch_chars,
        "estimated_prompt_tokens": estimated_tokens,
        "stable_prefix_ratio": 0.4,
        "contract_prefix_ratio": 0.2,
        "cache_health": "unknown",
        "total_prompt_cache_health": "unknown",
    });
    atomic_write_text(
        &artifact_root.join("review.prompt.metrics.json"),
        &(serde_json::to_string_pretty(&payload)? + "\n"),
    )?;
    Ok(())
}

pub fn execution_timeline_path(state_root: &Path, task_id: &str) -> std::path::PathBuf {
    task_dir(state_root, task_id).join("execution.timeline.json")
}

pub fn build_execution_timeline(state: &TaskState) -> Value {
    let mut steps = Vec::new();
    for a in &state.history {
        steps.push(json!({
            "iteration": a.iteration,
            "retry": a.retry,
            "phase": a.phase.as_str(),
            "decision": a.decision,
            "test_status": a.test_status,
            "graph_node_id": a.graph_node_id,
            "created_at": a.created_at,
            "branch": a.branch,
        }));
    }
    json!({
        "schema_version": 1,
        "task_id": state.task_id,
        "status": state.status.as_str(),
        "steps": steps,
        "updated_at": utc_now_iso(),
    })
}

pub fn write_execution_timeline(state: &TaskState, state_root: &Path) -> Result<()> {
    let path = execution_timeline_path(state_root, &state.task_id);
    let payload = build_execution_timeline(state);
    atomic_write_text(&path, &(serde_json::to_string_pretty(&payload)? + "\n"))?;
    Ok(())
}

pub fn append_command_argv(paths: &ArtifactPaths, role: &str, args: &[String]) -> Result<()> {
    let mut existing = if paths.command_argv.is_file() {
        fs::read_to_string(&paths.command_argv)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .unwrap_or_else(|| json!({}))
    } else {
        json!({})
    };
    if let Some(obj) = existing.as_object_mut() {
        obj.insert(role.into(), json!(args));
    }
    atomic_write_text(
        &paths.command_argv,
        &(serde_json::to_string_pretty(&existing)? + "\n"),
    )?;
    Ok(())
}

pub fn write_subprocess_result(
    paths: &ArtifactPaths,
    role: &str,
    exit_code: i32,
    timed_out: bool,
    duration_seconds: f64,
    killed: bool,
    hung: bool,
) -> Result<()> {
    let mut existing = if paths.subprocess_result.is_file() {
        fs::read_to_string(&paths.subprocess_result)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .unwrap_or_else(|| json!({}))
    } else {
        json!({})
    };
    if let Some(obj) = existing.as_object_mut() {
        obj.insert(
            role.into(),
            json!({
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_seconds": duration_seconds,
                "killed": killed,
                "hung": hung,
            }),
        );
    }
    atomic_write_text(
        &paths.subprocess_result,
        &(serde_json::to_string_pretty(&existing)? + "\n"),
    )?;
    Ok(())
}
