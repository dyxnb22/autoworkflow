//! Luma-oriented task summary.

use std::fs;
use std::path::Path;

use serde_json::{json, Value};

use crate::config::distinct_reviewer_satisfied;
use crate::error::Result;
use crate::graph::ensure_task_graph;
use crate::inspect::{
    build_attempt_snapshot, build_failure_snapshot, build_roles_snapshot, derive_next_action,
    derive_success_outcome, latest_reject_reason,
};
use crate::paths::{run_summary_path, task_dir};
use crate::report::build_report;
use crate::runner::is_runner_alive;
use crate::state::{atomic_write_text, AttemptRecord, TaskState, TaskStatus};
use crate::version::{CC_LOOP_VERSION, INTEGRATION_SCHEMA_VERSION, SUMMARY_SCHEMA_VERSION};

pub fn build_task_summary(state: &TaskState, state_root: &Path) -> Value {
    // ensure_task_graph needs &mut — clone graph view without mutating when possible
    let mut state_clone = state.clone();
    let graph_summary = {
        let g = ensure_task_graph(&mut state_clone);
        g.summary.clone()
    };
    let report = build_report(state, state_root);
    let attempt = state.latest_attempt();
    let artifact_paths = report
        .get("artifact_paths")
        .cloned()
        .unwrap_or(Value::Null);
    let roles = build_roles_snapshot(state);
    let (running, _) = is_runner_alive(state_root, &state.task_id);
    let next_action = derive_next_action(state, attempt, running);
    let phase = attempt.map(|a| a.phase.as_str()).unwrap_or("");
    let success = derive_success_outcome(state, attempt);
    let reject = latest_reject_reason(state);

    let latest_attempt = attempt.map(|a| {
        let snap = build_attempt_snapshot(state, a, state_root);
        json!({
            "iteration": snap.get("iteration").cloned().unwrap_or(json!(0)),
            "retry": snap.get("retry").cloned().unwrap_or(json!(0)),
            "phase": snap.get("phase").cloned().unwrap_or(json!("")),
            "decision": snap.get("decision").cloned().unwrap_or(json!("")),
            "test_status": snap.get("test_status").cloned().unwrap_or(json!("")),
            "implementer_exit_code": snap.get("implementer_exit_code").cloned().unwrap_or(Value::Null),
            "graph_node_id": snap.get("graph_node_id").cloned().unwrap_or(json!("")),
            "artifact_dir": snap.get("artifact_dir").cloned().unwrap_or(json!("")),
            "branch": a.branch,
            "worktree_path": a.worktree_path,
        })
    });

    let plan_summary = plan_summary_text(state, attempt, &graph_summary);
    let tests = tests_summary(attempt);
    let review_json = attempt.and_then(|a| a.review_json.clone()).unwrap_or(json!({}));
    let review_decision = report
        .get("review_decision")
        .cloned()
        .unwrap_or(json!({}));

    let mut artifacts = serde_json::Map::new();
    if let Some(obj) = artifact_paths.as_object() {
        for key in [
            "plan_parsed",
            "diff_stat",
            "test_output",
            "review_parsed",
            "merge_output",
            "implementer_prompt",
            "review_prompt",
        ] {
            if let Some(v) = obj.get(key) {
                artifacts.insert(key.into(), v.clone());
            }
        }
    }

    json!({
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "cc_loop_version": CC_LOOP_VERSION,
        "integration_schema_version": INTEGRATION_SCHEMA_VERSION,
        "task_id": state.task_id,
        "goal": state.goal,
        "status": state.status.as_str(),
        "phase": phase,
        "next_action": next_action,
        "base_branch": state.base_branch,
        "target_repo": state.target_repo,
        "providers": state.providers,
        "roles": roles,
        "distinct_reviewer": distinct_reviewer_satisfied(&state.config, Some(&state.providers)),
        "require_distinct_reviewer": state.config.require_distinct_reviewer,
        "auto_merge": state.config.auto_merge,
        "plan_summary": plan_summary,
        "latest_attempt": latest_attempt,
        "latest_reject_reason": if reject.is_empty() { Value::Null } else { json!(reject) },
        "tests": tests,
        "prompt_cache": Value::Null,
        "reviewer_prompt_metrics": Value::Null,
        "subprocess_result": Value::Null,
        "review": {
            "decision": review_decision.get("decision").cloned().unwrap_or(json!("")),
            "reason": review_decision.get("reason").cloned().unwrap_or(json!("")),
            "issues": review_json.get("issues").cloned().unwrap_or(json!([])),
        },
        "diff_stat": diff_stat_summary(attempt),
        "success": success,
        "failure": report.get("failure_summary").cloned().unwrap_or_else(|| {
            build_failure_snapshot(attempt, state_root, &state.task_id)
        }),
        "artifact_paths": artifact_paths,
        "artifacts": artifacts,
        "suggested_next_action": report.get("suggested_next_action").cloned().unwrap_or(json!(next_action)),
        "task_dir": task_dir(state_root, &state.task_id).display().to_string(),
        "events_path": report.get("events_path").cloned(),
        "log_path": report.get("log_path").cloned(),
    })
}

fn plan_summary_text(state: &TaskState, attempt: Option<&AttemptRecord>, graph_summary: &str) -> String {
    if !graph_summary.trim().is_empty() {
        return graph_summary.chars().take(240).collect();
    }
    if let Some(a) = attempt {
        if let Some(ref plan) = a.plan_json {
            for key in ["summary", "expected_changes", "title"] {
                if let Some(v) = plan.get(key).and_then(|x| x.as_str()) {
                    let t = v.trim();
                    if !t.is_empty() {
                        return t.chars().take(240).collect();
                    }
                }
            }
        }
    }
    state.goal.chars().take(240).collect()
}

fn tests_summary(attempt: Option<&AttemptRecord>) -> Value {
    let status = attempt.map(|a| a.test_status.as_str()).unwrap_or("");
    let reason = match status {
        "skipped" => "test_command not configured",
        "failed" => "test_command exited non-zero",
        "timed_out" => "test_command timed out",
        "passed" => "test_command passed",
        _ => "",
    };
    json!({
        "status": status,
        "pass": status == "passed",
        "fail": status == "failed" || status == "timed_out",
        "skipped": status == "skipped",
        "reason": reason,
        "exit_code": attempt.and_then(|a| a.test_exit_code),
    })
}

fn diff_stat_summary(attempt: Option<&AttemptRecord>) -> Value {
    let Some(a) = attempt else {
        return Value::Null;
    };
    if a.diff_stat_path.is_empty() && a.branch.is_empty() {
        return Value::Null;
    }
    let text = fs::read_to_string(&a.diff_stat_path).unwrap_or_default();
    let preview: String = text.lines().take(12).collect::<Vec<_>>().join("\n");
    json!({
        "path": if a.diff_stat_path.is_empty() { Value::Null } else { json!(a.diff_stat_path) },
        "preview": preview.chars().take(800).collect::<String>(),
        "branch": a.branch,
        "worktree_path": a.worktree_path,
        "head_commit": a.head_commit,
    })
}

pub fn format_task_summary_human(summary: &Value) -> String {
    let roles = summary.get("roles").cloned().unwrap_or(json!({}));
    let tests = summary.get("tests").cloned().unwrap_or(json!({}));
    let review = summary.get("review").cloned().unwrap_or(json!({}));
    let latest = summary.get("latest_attempt").cloned().unwrap_or(Value::Null);
    let diff = summary.get("diff_stat").cloned().unwrap_or(Value::Null);

    fn role_line(label: &str, role: &Value) -> String {
        let provider = role.get("provider").and_then(|v| v.as_str()).unwrap_or("-");
        let model = role.get("model").and_then(|v| v.as_str()).unwrap_or("");
        if model.is_empty() {
            format!("{label}: {provider}")
        } else {
            format!("{label}: {provider} ({model})")
        }
    }

    let mut lines = vec![
        format!("Task summary: {}", summary.get("task_id").and_then(|v| v.as_str()).unwrap_or("")),
        format!(
            "Status: {}  success={}",
            summary.get("status").and_then(|v| v.as_str()).unwrap_or(""),
            summary.get("success").and_then(|v| v.as_str()).unwrap_or("")
        ),
        format!("Goal: {}", summary.get("goal").and_then(|v| v.as_str()).unwrap_or("")),
        String::new(),
        "Roles".into(),
        format!("  {}", role_line("planner", &roles["planner"])),
        format!("  {}", role_line("implementer (writes)", &roles["implementer"])),
        format!("  {}", role_line("reviewer (reviews)", &roles["reviewer"])),
        format!(
            "  distinct_reviewer: {}",
            summary
                .get("distinct_reviewer")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        ),
    ];
    if let Some(ps) = summary.get("plan_summary").and_then(|v| v.as_str()) {
        if !ps.is_empty() {
            lines.push(format!("Plan: {ps}"));
        }
    }
    if latest.is_object() {
        lines.push(String::new());
        lines.push(format!(
            "Latest attempt: iter-{:03} retry-{:02}",
            latest.get("iteration").and_then(|v| v.as_u64()).unwrap_or(0),
            latest.get("retry").and_then(|v| v.as_u64()).unwrap_or(0)
        ));
        lines.push(format!(
            "Phase: {}",
            latest.get("phase").and_then(|v| v.as_str()).unwrap_or("")
        ));
        let mut test_line = format!(
            "Tests: {}",
            tests
                .get("status")
                .or_else(|| latest.get("test_status"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
        );
        if let Some(r) = tests.get("reason").and_then(|v| v.as_str()) {
            if !r.is_empty() {
                test_line.push_str(&format!(" ({r})"));
            }
        }
        lines.push(test_line);
        if let Some(b) = latest.get("branch").and_then(|v| v.as_str()) {
            if !b.is_empty() {
                lines.push(format!("Branch: {b}"));
            }
        }
    }
    lines.push(String::new());
    lines.push(format!(
        "Review: {}",
        review.get("decision").and_then(|v| v.as_str()).unwrap_or("")
    ));
    if let Some(r) = review.get("reason").and_then(|v| v.as_str()) {
        if !r.is_empty() {
            lines.push(format!("  reason: {r}"));
        }
    }
    if let Some(rr) = summary.get("latest_reject_reason").and_then(|v| v.as_str()) {
        lines.push(format!("Latest reject: {rr}"));
    }
    if let Some(preview) = diff.get("preview").and_then(|v| v.as_str()) {
        if !preview.is_empty() {
            lines.push(String::new());
            lines.push("Diff stat:".into());
            lines.push(preview.to_string());
        }
    }
    lines.push(String::new());
    lines.push(format!(
        "Next: {}",
        summary
            .get("suggested_next_action")
            .or_else(|| summary.get("next_action"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
    ));
    lines.join("\n")
}

pub fn should_write_run_summary(state: &TaskState) -> bool {
    matches!(
        state.status,
        TaskStatus::Done | TaskStatus::Failed | TaskStatus::Cancelled | TaskStatus::Stopped
    )
}

pub fn write_run_summary_if_terminal(state: &TaskState, state_root: &Path) -> Result<()> {
    if !should_write_run_summary(state) {
        return Ok(());
    }
    let summary = build_task_summary(state, state_root);
    let path = run_summary_path(state_root, &state.task_id);
    atomic_write_text(&path, &(serde_json::to_string_pretty(&summary)? + "\n"))?;
    Ok(())
}
