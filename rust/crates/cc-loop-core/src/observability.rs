//! Attempt trace + execution timeline + prompt cache snapshots.

use std::fs;
use std::path::Path;

use serde_json::{json, Value};

use crate::error::Result;
use crate::paths::{task_dir, ArtifactPaths};
use crate::prompt_cache::{
    build_implementer_phase_cache, build_planner_phase_cache, build_reviewer_phase_cache,
    update_prompt_cache_artifact, write_review_prompt_metrics_from_phase,
};
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

/// Persist phase metrics into prompt.cache.json + review.prompt.metrics.json.
pub fn record_planner_cache(paths: &ArtifactPaths, prompt: &str, skipped: bool, reason: &str) -> Result<()> {
    let phase = build_planner_phase_cache(
        prompt,
        skipped,
        if skipped { "direct" } else { "provider" },
        reason,
    );
    update_prompt_cache_artifact(&paths.prompt_cache, "planner", phase)
}

pub fn record_implementer_cache(paths: &ArtifactPaths, prompt: &str) -> Result<()> {
    let phase = build_implementer_phase_cache(prompt);
    update_prompt_cache_artifact(&paths.prompt_cache, "implementer", phase)
}

pub fn record_reviewer_cache(
    paths: &ArtifactPaths,
    prompt: &str,
    context_mode: &str,
    inline_patch: bool,
    omitted_patch_chars: usize,
) -> Result<()> {
    let phase = build_reviewer_phase_cache(prompt, context_mode, inline_patch, omitted_patch_chars);
    write_review_prompt_metrics_from_phase(&paths.root, &phase)?;
    update_prompt_cache_artifact(&paths.prompt_cache, "reviewer", phase)
}

// Back-compat shims used by older call sites.
pub fn write_prompt_cache(
    paths: &ArtifactPaths,
    review_context_mode: &str,
    omitted_patch_chars: usize,
    _estimated_tokens: usize,
) -> Result<()> {
    record_reviewer_cache(paths, "", review_context_mode, false, omitted_patch_chars)
}

pub fn write_review_prompt_metrics(
    artifact_root: &Path,
    context_mode: &str,
    inline_patch: bool,
    omitted_patch_chars: usize,
    _estimated_tokens: usize,
) -> Result<()> {
    let phase = build_reviewer_phase_cache("", context_mode, inline_patch, omitted_patch_chars);
    write_review_prompt_metrics_from_phase(artifact_root, &phase)
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
