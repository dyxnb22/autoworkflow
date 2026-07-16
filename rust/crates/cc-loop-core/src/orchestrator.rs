//! Single-loop / graph orchestrator (sequential or concurrent parallel nodes).

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};

use crate::config::LoopConfig;
use crate::error::{CcError, ExitCode, Result};
use crate::git::{
    create_worktree, diff_name_only, diff_patch, diff_stat, head_commit, merge_branch,
    repo_label, resolve_repo_path, warn_artifact_paths,
};
use crate::graph::{ensure_task_graph, parse_planner_graph, NodeStatus, TaskGraph};
use crate::inspect::latest_reject_reason;
use crate::paths::{
    artifacts_dir, branch_name, default_worktree_root, worktree_path, ArtifactPaths,
};
use crate::process::run_with_timeout;
use crate::provider::{extract_json_object, run_role};
use crate::budgets::budget_exhausted_message;
use crate::events::append_event;
use crate::failure::{write_attempt_failure_report, write_failure_report};
use crate::observability::{
    append_command_argv, record_implementer_cache, record_planner_cache, record_reviewer_cache,
    write_attempt_trace, write_execution_timeline, write_subprocess_result,
};
use crate::parallel::{
    clear_running, enqueue_merge, mark_running, parallel_execution_enabled, prepare_parallel_jobs,
    run_jobs_concurrently, schedule_ready_nodes, ParallelJob,
};
use crate::planner_direct::{direct_plan_json, should_skip_planner};
use crate::recovery::{decide_auto_step, AutoStep};
use crate::repair::{
    provider_error_repair_prompt, reviewer_reject_repair_prompt, test_failure_repair_prompt,
};
use crate::review_context::build_reviewer_prompt;
use crate::state::{
    load_state, save_state, with_state_mut, AttemptPhase, AttemptRecord, TaskState, TaskStatus,
};
use crate::summary::write_run_summary_if_terminal;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunOutcome {
    Success,
    UserStop,
    Failed,
}

impl RunOutcome {
    pub fn exit_code(self) -> ExitCode {
        match self {
            Self::Success | Self::UserStop => ExitCode::Success,
            Self::Failed => ExitCode::ExecutionFailure,
        }
    }
}

fn planner_prompt(goal: &str, granularity: &str) -> String {
    format!(
        r#"## Stable Planner Contract
You are the planner for cc-loop, a role-separated delivery engine.
Return ONLY a JSON object.

## Task Planner Context
Goal: {goal}
Planner granularity: {granularity}

## Dynamic Planner Payload
Prefer single closed-loop for default:
{{
  "mode": "task_graph",
  "summary": "short plan summary",
  "nodes": [{{"id": "n1", "title": "deliver", "goal": "...", "depends_on": []}}]
}}

Or legacy single-step:
{{"summary":"...","implementer_prompt":"...","expected_changes":"..."}}
"#
    )
}

fn implementer_prompt(goal: &str, plan: &Value, reject_reason: &str) -> String {
    let plan_text = serde_json::to_string_pretty(plan).unwrap_or_default();
    let retry = if reject_reason.is_empty() {
        String::new()
    } else {
        format!("\nPrevious review reject reason (fix this):\n{reject_reason}\n")
    };
    format!(
        "## Stable Implementer Contract\n\
         Implement the goal in this worktree. Do not push. Do not merge.\n\
         Make the minimal correct change.\n\n\
         ## Task Implementer Context\n\
         Goal: {goal}\n{retry}\n\
         ## Dynamic Implementer Payload\n\
         Plan JSON:\n{plan_text}\n"
    )
}

fn resolve_implementer_prompt(
    goal: &str,
    plan: &Value,
    reject_reason: &str,
    test_status: &str,
    test_output: &str,
    failure_type: &str,
) -> String {
    if !reject_reason.is_empty() {
        return reviewer_reject_repair_prompt(goal, reject_reason, "");
    }
    if test_status == "failed" || test_status == "timed_out" {
        return test_failure_repair_prompt(goal, test_output, test_status);
    }
    if !failure_type.is_empty() {
        return provider_error_repair_prompt(goal, failure_type);
    }
    implementer_prompt(goal, plan, "")
}

fn run_tests(
    worktree: &Path,
    test_command: &[String],
    timeout_secs: u64,
    output_path: &Path,
) -> Result<(i32, String)> {
    if test_command.is_empty() {
        fs::write(output_path, "test_command not configured; skipped\n")?;
        return Ok((0, "skipped".into()));
    }
    let result = run_with_timeout(
        test_command,
        Some(worktree),
        Duration::from_secs(timeout_secs.max(1)),
        &[],
    )?;
    let body = format!(
        "exit={}\ntimed_out={}\n\nSTDOUT:\n{}\n\nSTDERR:\n{}\n",
        result.returncode, result.timed_out, result.stdout, result.stderr
    );
    fs::write(output_path, &body)?;
    if result.timed_out {
        return Ok((-1, "timed_out".into()));
    }
    if result.returncode == 0 {
        Ok((0, "passed".into()))
    } else {
        Ok((result.returncode, "failed".into()))
    }
}

fn finalize_handoff(state: &mut TaskState, attempt: &mut AttemptRecord) {
    attempt.phase = AttemptPhase::Approved;
    attempt.decision = "approve".into();
    let complete = state
        .task_graph
        .as_ref()
        .map(|g| g.is_complete())
        .unwrap_or(true);
    state.status = if complete {
        TaskStatus::Done
    } else {
        TaskStatus::Running
    };
}

fn finalize_merged(state: &mut TaskState, attempt: &mut AttemptRecord) {
    attempt.phase = AttemptPhase::Merged;
    attempt.decision = "approve".into();
    let complete = state
        .task_graph
        .as_ref()
        .map(|g| g.is_complete())
        .unwrap_or(true);
    state.status = if complete {
        TaskStatus::Done
    } else {
        TaskStatus::Running
    };
}

/// Merge one attempt into shared state without clobbering sibling parallel updates.
fn persist_attempt_and_reload(
    state: &mut TaskState,
    state_root: &Path,
    attempt: AttemptRecord,
) -> Result<()> {
    let task_id = state.task_id.clone();
    let node_id = attempt.graph_node_id.clone();
    let node_status = state
        .task_graph
        .as_ref()
        .and_then(|g| g.nodes.iter().find(|n| n.id == node_id).map(|n| n.status))
        .unwrap_or(NodeStatus::Failed);
    let desired_status = state.status;
    let should_enqueue = state.merge_queue.iter().any(|id| id == &node_id)
        || (attempt.decision == "approve"
            && matches!(
                attempt.phase,
                AttemptPhase::Approved | AttemptPhase::Merged
            )
            && state.config.auto_merge);

    with_state_mut(&task_id, state_root, |st| {
        if let Some(idx) = st.history.iter().position(|a| {
            a.iteration == attempt.iteration
                && a.retry == attempt.retry
                && a.graph_node_id == attempt.graph_node_id
        }) {
            st.history[idx] = attempt.clone();
        } else {
            st.history.push(attempt.clone());
        }
        if let Some(g) = st.task_graph.as_mut() {
            g.mark_node(&node_id, node_status);
        }
        clear_running(st, &node_id);
        if should_enqueue {
            enqueue_merge(st, &node_id);
        }
        match desired_status {
            TaskStatus::Failed | TaskStatus::Cancelled => {
                st.status = desired_status;
            }
            TaskStatus::Done => {
                if st
                    .task_graph
                    .as_ref()
                    .map(|g| g.is_complete())
                    .unwrap_or(true)
                {
                    st.status = TaskStatus::Done;
                } else {
                    st.status = TaskStatus::Running;
                }
            }
            TaskStatus::Stopped | TaskStatus::Interrupted | TaskStatus::Replanning => {
                // Don't downgrade a sibling's terminal Done/Failed.
                if !matches!(st.status, TaskStatus::Done | TaskStatus::Failed | TaskStatus::Cancelled)
                {
                    st.status = desired_status;
                }
            }
            TaskStatus::Running | TaskStatus::Initialized | TaskStatus::WaitingManualReview => {
                if !matches!(
                    st.status,
                    TaskStatus::Done | TaskStatus::Failed | TaskStatus::Cancelled
                ) {
                    st.status = TaskStatus::Running;
                }
            }
        }
        Ok(())
    })?;
    *state = load_state(&task_id, state_root)?;
    write_execution_timeline(state, state_root)?;
    write_run_summary_if_terminal(state, state_root)?;
    Ok(())
}

/// Run one closed-loop attempt for a graph node (or single default node).
fn run_one_attempt(
    state: &mut TaskState,
    state_root: &Path,
    node_id: &str,
    node_goal: &str,
    iteration: u32,
    retry: u32,
    reject_reason: &str,
) -> Result<()> {
    let repo = resolve_repo_path(Path::new(&state.target_repo));
    let timeout = state.config.git_timeout_seconds.max(1);
    let worktree_root = default_worktree_root(state_root);
    let label = repo_label(&repo);
    let wt = worktree_path(&worktree_root, &label, &state.task_id, iteration, retry);
    let branch = branch_name(&state.task_id, iteration, retry);
    let art_root = artifacts_dir(state_root, &state.task_id, iteration, retry);
    fs::create_dir_all(&art_root)?;
    let paths = ArtifactPaths::new(art_root);

    let mut attempt = AttemptRecord::new(iteration, retry, &state.base_commit);
    attempt.graph_node_id = node_id.to_string();
    attempt.branch = branch.clone();
    attempt.worktree_path = wt.display().to_string();
    attempt.phase = AttemptPhase::Planning;
    mark_running(state, node_id, iteration);
    let _ = append_event(
        state_root,
        &state.task_id,
        "planner.started",
        iteration,
        retry,
        node_id,
        "planning",
        "planner started",
        json!({}),
    );

    // Plan (auto-direct may skip provider)
    let plan_json = if should_skip_planner(node_goal, &state.config) {
        let plan = direct_plan_json(node_goal);
        let skip_prompt = planner_prompt(node_goal, &state.config.planner_granularity);
        fs::write(&paths.plan_prompt, &skip_prompt)?;
        let _ = record_planner_cache(&paths, &skip_prompt, true, "auto_direct");
        fs::write(
            &paths.plan_last_message,
            serde_json::to_string_pretty(&plan)?,
        )?;
        fs::write(&paths.plan_provider, "auto_direct")?;
        attempt.plan_provider = "auto_direct".into();
        plan
    } else {
        let plan_prompt = planner_prompt(node_goal, &state.config.planner_granularity);
        fs::write(&paths.plan_prompt, &plan_prompt)?;
        let _ = record_planner_cache(&paths, &plan_prompt, false, "");
        let planner = state
            .providers
            .get("planner")
            .cloned()
            .unwrap_or_else(|| state.config.planner_provider.clone());
        attempt.running_provider = planner.clone();
        let plan_result = run_role(
            "planner",
            &planner,
            &wt,
            &plan_prompt,
            &paths.plan_last_message,
            &paths.plan_raw,
            &state.config,
            true,
        );
        fs::create_dir_all(state_root)?;
        match plan_result {
            Ok(r) => {
                let _ = write_subprocess_result(
                    &paths,
                    "planner",
                    r.exit_code,
                    r.timed_out,
                    r.duration_seconds,
                    r.killed,
                    r.hung,
                );
                fs::write(&paths.plan_provider, &r.provider)?;
                attempt.plan_provider = r.provider.clone();
                attempt.plan_raw_path = r.raw_artifact_path.display().to_string();
                if r.timed_out || r.exit_code != 0 {
                    attempt.phase = AttemptPhase::Failed;
                    attempt.failure_type = "planner_failed".into();
                    state.status = TaskStatus::Failed;
                    clear_running(state, node_id);
                    let _ = write_failure_report(
                        state_root,
                        &state.task_id,
                        &attempt,
                        "planner_failed",
                        "terminal",
                        "planner failed or timed out",
                        &["inspect artifacts", "resume after fixing provider"],
                    );
                    persist_attempt_and_reload(state, state_root, attempt)?;
                    return Err(CcError::execution("planner failed or timed out"));
                }
                let text = fs::read_to_string(&paths.plan_last_message).unwrap_or_default();
                extract_json_object(&text).unwrap_or_else(|_| {
                    json!({
                        "summary": node_goal,
                        "implementer_prompt": node_goal,
                        "parse_error": true,
                    })
                })
            }
            Err(e) => {
                attempt.phase = AttemptPhase::Failed;
                attempt.failure_type = "planner_failed".into();
                state.status = TaskStatus::Failed;
                clear_running(state, node_id);
                persist_attempt_and_reload(state, state_root, attempt)?;
                return Err(CcError::execution(e.to_string()));
            }
        }
    };
    fs::write(
        &paths.plan_parsed,
        serde_json::to_string_pretty(&plan_json)?,
    )?;
    attempt.plan_json = Some(plan_json.clone());
    let _ = append_event(
        state_root,
        &state.task_id,
        "planner.completed",
        iteration,
        retry,
        node_id,
        "planning",
        "planner completed",
        json!({}),
    );

    // Update / create graph from plan when first planning
    if state.task_graph.is_none() || state.config.planner_granularity == "single" {
        state.task_graph = Some(parse_planner_graph(&plan_json, node_goal));
    }
    ensure_task_graph(state);
    if let Some(g) = state.task_graph.as_mut() {
        g.mark_node(node_id, NodeStatus::Running);
    }

    // Worktree
    attempt.phase = AttemptPhase::WorktreeCreated;
    create_worktree(&repo, &wt, &branch, &state.base_commit, timeout)?;

    // Implement
    attempt.phase = AttemptPhase::Executing;
    let implementer = state
        .providers
        .get("implementer")
        .cloned()
        .unwrap_or_else(|| state.config.implementer_provider.clone());
    attempt.running_provider = implementer.clone();
    attempt.implementer_provider = implementer.clone();
    let prior_test = state
        .latest_attempt()
        .map(|a| a.test_status.clone())
        .unwrap_or_default();
    let prior_fail = state
        .latest_attempt()
        .map(|a| a.failure_type.clone())
        .unwrap_or_default();
    let prior_test_out = state
        .latest_attempt()
        .map(|a| fs::read_to_string(&a.test_raw_path).unwrap_or_default())
        .unwrap_or_default();
    let impl_prompt = resolve_implementer_prompt(
        node_goal,
        &plan_json,
        reject_reason,
        &prior_test,
        &prior_test_out,
        &prior_fail,
    );
    fs::write(&paths.implementer_prompt, &impl_prompt)?;
    let _ = record_implementer_cache(&paths, &impl_prompt);
    attempt.implementer_prompt_path = paths.implementer_prompt.display().to_string();
    let _ = append_event(
        state_root,
        &state.task_id,
        "implementer.started",
        iteration,
        retry,
        node_id,
        "executing",
        "implementer started",
        json!({}),
    );
    let impl_result = run_role(
        "implementer",
        &implementer,
        &wt,
        &impl_prompt,
        &paths.implementer_raw.with_extension("out.txt"),
        &paths.implementer_raw,
        &state.config,
        false,
    )?;
    let _ = write_subprocess_result(
        &paths,
        "implementer",
        impl_result.exit_code,
        impl_result.timed_out,
        impl_result.duration_seconds,
        impl_result.killed,
        impl_result.hung,
    );
    fs::write(&paths.implementer_provider, &impl_result.provider)?;
    attempt.implementer_exit_code = Some(impl_result.exit_code);
    attempt.implementer_raw_path = impl_result.raw_artifact_path.display().to_string();
    if impl_result.timed_out || impl_result.exit_code != 0 {
        attempt.phase = AttemptPhase::Failed;
        attempt.failure_type = "implementer_failed".into();
        state.status = TaskStatus::Failed;
        if let Some(g) = state.task_graph.as_mut() {
            g.mark_node(node_id, NodeStatus::Failed);
        }
        clear_running(state, node_id);
        let _ = write_failure_report(
            state_root,
            &state.task_id,
            &attempt,
            "implementer_failed",
            "recoverable",
            "implementer failed or timed out",
            &["resume", "inspect implementer.raw"],
        );
        persist_attempt_and_reload(state, state_root, attempt)?;
        return Err(CcError::execution("implementer failed or timed out"));
    }

    attempt.head_commit = head_commit(&wt, timeout).unwrap_or_default();
    let stat = diff_stat(&wt, &state.base_commit, timeout).unwrap_or_default();
    fs::write(&paths.diff_stat, &stat)?;
    attempt.diff_stat_path = paths.diff_stat.display().to_string();
    let files = diff_name_only(&wt, &state.base_commit, timeout).unwrap_or_default();
    fs::write(&paths.diff_files, files.join("\n"))?;
    for w in warn_artifact_paths(&files) {
        let _ = w; // surfaced via events later
    }
    let patch = diff_patch(&wt, &state.base_commit, timeout).unwrap_or_default();
    fs::create_dir_all(&paths.patches_dir)?;
    let patch_path = paths.patches_dir.join("full.patch");
    fs::write(&patch_path, &patch)?;
    attempt.diff_patch_paths = vec![patch_path.display().to_string()];

    // Tests
    attempt.phase = AttemptPhase::Testing;
    attempt.running_provider = String::new();
    attempt.test_command = state.config.test_command.clone();
    let (test_code, test_status) = run_tests(
        &wt,
        &state.config.test_command,
        state.config.test_timeout_seconds,
        &paths.test_output,
    )?;
    attempt.test_exit_code = Some(test_code);
    attempt.test_status = test_status.clone();
    attempt.test_raw_path = paths.test_output.display().to_string();

    if (test_status == "failed" || test_status == "timed_out")
        && !state.config.allow_merge_without_tests
    {
        attempt.phase = AttemptPhase::Failed;
        attempt.failure_type = format!("test_{test_status}");
        attempt.decision = "reject".into();
        attempt.recovery_disposition = "recoverable".into();
        state.status = TaskStatus::Stopped;
        clear_running(state, node_id);
        let report = write_failure_report(
            state_root,
            &state.task_id,
            &attempt,
            &attempt.failure_type,
            "recoverable",
            &format!("tests {test_status}"),
            &["resume for repair", "inspect test.output.txt"],
        )?;
        let _ = write_attempt_failure_report(&paths.root, &report);
        persist_attempt_and_reload(state, state_root, attempt)?;
        return Ok(());
    }

    // Review
    attempt.phase = AttemptPhase::Reviewing;
    let reviewer = state
        .providers
        .get("reviewer")
        .cloned()
        .unwrap_or_else(|| state.config.reviewer_provider.clone());
    attempt.running_provider = reviewer.clone();
    attempt.review_provider = reviewer.clone();
    let plan_text = serde_json::to_string_pretty(&plan_json).unwrap_or_default();
    let review_payload = build_reviewer_prompt(
        node_goal,
        &plan_text,
        &stat,
        &patch,
        &state.config,
        &paths.diff_stat.display().to_string(),
        &paths.diff_files.display().to_string(),
    );
    fs::write(&paths.review_prompt, &review_payload.prompt)?;
    let _ = record_reviewer_cache(
        &paths,
        &review_payload.prompt,
        &review_payload.context_mode,
        review_payload.inline_patch,
        review_payload.omitted_patch_chars,
    );
    let _ = append_event(
        state_root,
        &state.task_id,
        "reviewer.started",
        iteration,
        retry,
        node_id,
        "reviewing",
        "reviewer started",
        json!({}),
    );
    let rev_result = run_role(
        "reviewer",
        &reviewer,
        &wt,
        &review_payload.prompt,
        &paths.review_last_message,
        &paths.review_raw,
        &state.config,
        true,
    )?;
    let _ = write_subprocess_result(
        &paths,
        "reviewer",
        rev_result.exit_code,
        rev_result.timed_out,
        rev_result.duration_seconds,
        rev_result.killed,
        rev_result.hung,
    );
    fs::write(&paths.review_provider, &rev_result.provider)?;
    attempt.review_raw_path = rev_result.raw_artifact_path.display().to_string();
    if rev_result.timed_out || rev_result.exit_code != 0 {
        attempt.phase = AttemptPhase::Failed;
        attempt.failure_type = "reviewer_failed".into();
        state.status = TaskStatus::Failed;
        clear_running(state, node_id);
        persist_attempt_and_reload(state, state_root, attempt)?;
        return Err(CcError::execution("reviewer failed or timed out"));
    }
    let review_text = fs::read_to_string(&paths.review_last_message).unwrap_or_default();
    let review_json = extract_json_object(&review_text).unwrap_or_else(|_| {
        json!({
            "decision": "stop",
            "reason": "failed to parse reviewer output",
            "parse_error": true,
        })
    });
    fs::write(
        &paths.review_parsed,
        serde_json::to_string_pretty(&review_json)?,
    )?;
    attempt.review_json = Some(review_json.clone());
    let decision = review_json
        .get("decision")
        .and_then(|v| v.as_str())
        .unwrap_or("stop")
        .to_lowercase();
    attempt.decision = decision.clone();
    attempt.running_provider.clear();

    match decision.as_str() {
        "approve" => {
            if state.config.auto_merge {
                let merge_out = merge_into_base(&repo, &state.config.base_branch, &branch, timeout)?;
                fs::write(&paths.merge_output, &merge_out)?;
                attempt.merge_output_path = paths.merge_output.display().to_string();
                if merge_out.contains("MERGE_FAILED") {
                    attempt.merge_error = merge_out.clone();
                    attempt.phase = AttemptPhase::Approved;
                    attempt.recovery_disposition = "recoverable".into();
                    state.status = TaskStatus::Stopped;
                    enqueue_merge(state, node_id);
                } else {
                    if let Some(g) = state.task_graph.as_mut() {
                        g.mark_node(node_id, NodeStatus::Done);
                    }
                    finalize_merged(state, &mut attempt);
                }
            } else {
                if let Some(g) = state.task_graph.as_mut() {
                    g.mark_node(node_id, NodeStatus::Done);
                }
                finalize_handoff(state, &mut attempt);
            }
        }
        "reject" => {
            attempt.phase = AttemptPhase::Rejected;
            attempt.recovery_disposition = "recoverable".into();
            state.status = TaskStatus::Stopped;
            if let Some(g) = state.task_graph.as_mut() {
                g.mark_node(node_id, NodeStatus::Pending);
            }
        }
        "replan" => {
            attempt.phase = AttemptPhase::Replanning;
            state.status = TaskStatus::Replanning;
        }
        _ => {
            attempt.phase = AttemptPhase::Failed;
            attempt.stop_reason = review_json
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("reviewer stop")
                .to_string();
            state.status = TaskStatus::Stopped;
        }
    }

    clear_running(state, node_id);
    let _ = write_attempt_trace(
        &paths,
        &[json!({
            "phase": attempt.phase.as_str(),
            "decision": attempt.decision,
            "test_status": attempt.test_status,
        })],
    );
    let _ = append_command_argv(&paths, "reviewer", &[]);
    persist_attempt_and_reload(state, state_root, attempt)?;
    Ok(())
}

fn merge_into_base(repo: &Path, base_branch: &str, branch: &str, timeout: u64) -> Result<String> {
    // WARNING: This checks out the user's repo onto base_branch briefly.
    // Only called when auto_merge=true (opt-in).
    use crate::git::{checkout_branch, current_branch};
    let prev = current_branch(repo, timeout).unwrap_or_else(|_| base_branch.to_string());
    checkout_branch(repo, base_branch, timeout)?;
    let result = merge_branch(repo, branch, timeout)?;
    let out = if result.returncode != 0 {
        format!(
            "MERGE_FAILED\nstdout:\n{}\nstderr:\n{}\n",
            result.stdout, result.stderr
        )
    } else {
        format!("MERGE_OK\n{}\n{}", result.stdout, result.stderr)
    };
    // Restore previous branch when possible
    let _ = checkout_branch(repo, &prev, timeout);
    Ok(out)
}

/// Execute the delivery loop until handoff/merge/stop/fail.
pub fn run_loop(state: &mut TaskState, state_root: &Path, max_steps: Option<u32>) -> Result<RunOutcome> {
    state.status = TaskStatus::Running;
    save_state(state, state_root)?;

    let max_iter = state.config.max_iterations;
    let max_retries = state.config.max_retries_per_step;
    let _ = max_iter; // checked via budget_exhausted_message
    let steps_cap = max_steps.unwrap_or(u32::MAX);
    let mut steps = 0u32;

    while steps < steps_cap {
        steps += 1;
        if let Some(msg) = budget_exhausted_message(state) {
            state.status = TaskStatus::Failed;
            if let Some(a) = state.latest_attempt_mut() {
                a.failure_type = "budget_exhausted".into();
                a.stop_reason = msg.clone();
            }
            save_state(state, state_root)?;
            let _ = append_event(
                state_root,
                &state.task_id,
                "task.failed",
                state.iteration,
                0,
                "",
                "failed",
                &msg,
                json!({}),
            );
            return Ok(RunOutcome::Failed);
        }

        // Ensure graph
        {
            let goal = state.goal.clone();
            if state.task_graph.is_none() {
                state.task_graph = Some(TaskGraph::single_node(&goal));
            }
        }

        let ready_ids = {
            let g = state.task_graph.as_ref().unwrap();
            if g.is_complete() && matches!(state.status, TaskStatus::Done) {
                write_run_summary_if_terminal(state, state_root)?;
                return Ok(RunOutcome::Success);
            }
            schedule_ready_nodes(state)
        };

        if ready_ids.is_empty() {
            if state
                .task_graph
                .as_ref()
                .map(|g| g.is_complete())
                .unwrap_or(false)
            {
                if state.status != TaskStatus::Done {
                    state.status = TaskStatus::Done;
                    save_state(state, state_root)?;
                }
                write_run_summary_if_terminal(state, state_root)?;
                return Ok(RunOutcome::Success);
            }
            state.status = TaskStatus::Stopped;
            save_state(state, state_root)?;
            return Ok(RunOutcome::UserStop);
        }

        // Concurrent when explicitly enabled and multiple ready nodes; else sequential.
        if parallel_execution_enabled(state) && ready_ids.len() > 1 {
            let reject_reason = latest_reject_reason(state);
            let jobs = prepare_parallel_jobs(
                state,
                state_root,
                &ready_ids,
                max_retries,
                current_retry_for_node,
            )?;
            run_jobs_concurrently(state, state_root, jobs, {
                let reject_reason = reject_reason.clone();
                move |sr, tid, job| run_one_attempt_job(sr, tid, job, reject_reason.clone())
            })?;
            match decide_auto_step(state) {
                AutoStep::Done => {
                    write_run_summary_if_terminal(state, state_root)?;
                    return Ok(RunOutcome::Success);
                }
                AutoStep::Fail => {
                    write_run_summary_if_terminal(state, state_root)?;
                    return Ok(RunOutcome::Failed);
                }
                AutoStep::Resume | AutoStep::Repair => continue,
                AutoStep::Stop => {
                    write_run_summary_if_terminal(state, state_root)?;
                    return Ok(RunOutcome::UserStop);
                }
                AutoStep::Replan => {
                    state.status = TaskStatus::Replanning;
                    let goal = state.goal.clone();
                    state.task_graph = Some(TaskGraph::single_node(&goal));
                    state.status = TaskStatus::Running;
                    save_state(state, state_root)?;
                }
            }
            continue;
        }

        for (node_id, node_goal) in ready_ids {
            let reject_reason = latest_reject_reason(state);
            let retry = current_retry_for_node(state, &node_id);
            if retry > max_retries {
                state.status = TaskStatus::Failed;
                save_state(state, state_root)?;
                return Ok(RunOutcome::Failed);
            }
            state.iteration += 1;
            let iteration = state.iteration;
            run_one_attempt(
                state,
                state_root,
                &node_id,
                &node_goal,
                iteration,
                retry,
                &reject_reason,
            )?;

            match decide_auto_step(state) {
                AutoStep::Done => {
                    write_run_summary_if_terminal(state, state_root)?;
                    return Ok(RunOutcome::Success);
                }
                AutoStep::Fail => {
                    write_run_summary_if_terminal(state, state_root)?;
                    return Ok(RunOutcome::Failed);
                }
                AutoStep::Resume | AutoStep::Repair => continue,
                AutoStep::Stop => {
                    write_run_summary_if_terminal(state, state_root)?;
                    return Ok(RunOutcome::UserStop);
                }
                AutoStep::Replan => {
                    state.status = TaskStatus::Replanning;
                    let goal = state.goal.clone();
                    state.task_graph = Some(TaskGraph::single_node(&goal));
                    state.status = TaskStatus::Running;
                    save_state(state, state_root)?;
                }
            }
        }
    }
    Ok(RunOutcome::UserStop)
}

fn run_one_attempt_job(
    state_root: PathBuf,
    task_id: String,
    job: ParallelJob,
    reject_reason: String,
) -> Result<()> {
    let mut state = load_state(&task_id, &state_root)?;
    run_one_attempt(
        &mut state,
        &state_root,
        &job.node_id,
        &job.node_goal,
        job.iteration,
        job.retry,
        &reject_reason,
    )
}

fn current_retry_for_node(state: &TaskState, node_id: &str) -> u32 {
    state
        .history
        .iter()
        .rev()
        .find(|a| a.graph_node_id == node_id || (node_id == "n1" && a.graph_node_id.is_empty()))
        .map(|a| {
            if a.phase == AttemptPhase::Rejected {
                a.retry + 1
            } else {
                a.retry
            }
        })
        .unwrap_or(0)
}

pub fn require_test_command_for_auto(config: &LoopConfig) -> Result<()> {
    if config.test_command.is_empty() && !config.allow_merge_without_tests {
        return Err(CcError::config(
            "auto refused: test_command is not configured (set test_command or allow_merge_without_tests)",
        ));
    }
    Ok(())
}

pub fn warn_if_no_test_command(config: &LoopConfig) {
    if config.test_command.is_empty() && !config.allow_merge_without_tests {
        eprintln!(
            "warning: test_command is empty; delivery gate is weaker without tests"
        );
    }
}

/// Inject a successful handoff attempt (tests / dry fixtures).
pub fn inject_fake_success_handoff(state: &mut TaskState, state_root: &Path) -> Result<()> {
    state.iteration = 1;
    let mut attempt = AttemptRecord::new(1, 0, &state.base_commit);
    attempt.phase = AttemptPhase::Approved;
    attempt.decision = "approve".into();
    attempt.test_status = "passed".into();
    attempt.test_exit_code = Some(0);
    attempt.review_json = Some(json!({"decision":"approve","reason":"ok"}));
    attempt.branch = branch_name(&state.task_id, 1, 0);
    state.history.push(attempt);
    state.status = TaskStatus::Done;
    state.task_graph = Some(TaskGraph::single_node(&state.goal));
    if let Some(g) = state.task_graph.as_mut() {
        g.mark_node("n1", NodeStatus::Done);
    }
    save_state(state, state_root)?;
    write_run_summary_if_terminal(state, state_root)?;
    Ok(())
}
