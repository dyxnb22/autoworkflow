//! Single-loop / sequential graph orchestrator.

use std::fs;
use std::path::Path;
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
use crate::recovery::{decide_auto_step, AutoStep};
use crate::state::{
    save_state, AttemptPhase, AttemptRecord, TaskState, TaskStatus,
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
        r#"You are the planner for cc-loop, a role-separated delivery engine.
Goal: {goal}
Planner granularity: {granularity}

Return ONLY a JSON object. Prefer single closed-loop for default:
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
        "Implement the following goal in this worktree.\nGoal: {goal}\n{retry}\nPlan JSON:\n{plan_text}\n\
         Make the minimal correct change. Do not push. Do not merge."
    )
}

fn reviewer_prompt(
    goal: &str,
    plan: &Value,
    diff_stat_text: &str,
    patch: &str,
    max_patch_bytes: usize,
) -> String {
    let plan_text = serde_json::to_string_pretty(plan).unwrap_or_default();
    let patch_trimmed = if patch.len() > max_patch_bytes {
        format!(
            "{}\n\n...[truncated {} bytes]...",
            &patch[..max_patch_bytes],
            patch.len() - max_patch_bytes
        )
    } else {
        patch.to_string()
    };
    format!(
        r#"You are the reviewer. You did NOT write this code. Gate on tests already ran.
Goal: {goal}
Plan:
{plan_text}

Diff stat:
{diff_stat_text}

Patch:
{patch_trimmed}

Return ONLY JSON:
{{"decision":"approve"|"reject"|"stop","reason":"...","retry_prompt":"...","issues":[]}}
"#
    )
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
    state.status = TaskStatus::Done;
}

fn finalize_merged(state: &mut TaskState, attempt: &mut AttemptRecord) {
    attempt.phase = AttemptPhase::Merged;
    attempt.decision = "approve".into();
    state.status = TaskStatus::Done;
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
    attempt.running_provider = state
        .providers
        .get("planner")
        .cloned()
        .unwrap_or_else(|| state.config.planner_provider.clone());

    // Plan
    let plan_prompt = planner_prompt(node_goal, &state.config.planner_granularity);
    fs::write(&paths.plan_prompt, &plan_prompt)?;
    let planner = state
        .providers
        .get("planner")
        .cloned()
        .unwrap_or_else(|| state.config.planner_provider.clone());
    let plan_result = run_role(
        "planner",
        &planner,
        &wt,
        &plan_prompt,
        &paths.plan_last_message,
        &paths.plan_raw,
        &state.config,
        true, // print_only
    );
    // Worktree may not exist yet for planner print_only — create empty cwd parent
    fs::create_dir_all(state_root)?;

    let plan_json = match &plan_result {
        Ok(r) => {
            fs::write(&paths.plan_provider, &r.provider)?;
            attempt.plan_provider = r.provider.clone();
            attempt.plan_raw_path = r.raw_artifact_path.display().to_string();
            if r.timed_out || r.exit_code != 0 {
                attempt.phase = AttemptPhase::Failed;
                attempt.failure_type = "planner_failed".into();
                state.status = TaskStatus::Failed;
                state.history.push(attempt);
                save_state(state, state_root)?;
                return Err(CcError::execution("planner failed or timed out"));
            }
            let text = fs::read_to_string(&paths.plan_last_message).unwrap_or_default();
            match extract_json_object(&text) {
                Ok(v) => v,
                Err(_) => {
                    // Persist raw even on parse failure; synthesize single-node plan.
                    let fallback = json!({
                        "summary": node_goal,
                        "implementer_prompt": node_goal,
                        "parse_error": true,
                    });
                    fallback
                }
            }
        }
        Err(e) => {
            attempt.phase = AttemptPhase::Failed;
            attempt.failure_type = "planner_failed".into();
            state.status = TaskStatus::Failed;
            state.history.push(attempt);
            save_state(state, state_root)?;
            return Err(CcError::execution(e.to_string()));
        }
    };
    fs::write(
        &paths.plan_parsed,
        serde_json::to_string_pretty(&plan_json)?,
    )?;
    attempt.plan_json = Some(plan_json.clone());

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
    let impl_prompt = implementer_prompt(node_goal, &plan_json, reject_reason);
    fs::write(&paths.implementer_prompt, &impl_prompt)?;
    attempt.implementer_prompt_path = paths.implementer_prompt.display().to_string();
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
        state.history.push(attempt);
        save_state(state, state_root)?;
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
        // Treat as retryable via recovery when auto_recover_tests
        state.status = TaskStatus::Stopped;
        state.history.push(attempt);
        save_state(state, state_root)?;
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
    let rev_prompt = reviewer_prompt(
        node_goal,
        &plan_json,
        &stat,
        &patch,
        state.config.max_review_patch_bytes,
    );
    fs::write(&paths.review_prompt, &rev_prompt)?;
    let rev_result = run_role(
        "reviewer",
        &reviewer,
        &wt,
        &rev_prompt,
        &paths.review_last_message,
        &paths.review_raw,
        &state.config,
        true,
    )?;
    fs::write(&paths.review_provider, &rev_result.provider)?;
    attempt.review_raw_path = rev_result.raw_artifact_path.display().to_string();
    if rev_result.timed_out || rev_result.exit_code != 0 {
        attempt.phase = AttemptPhase::Failed;
        attempt.failure_type = "reviewer_failed".into();
        state.status = TaskStatus::Failed;
        state.history.push(attempt);
        save_state(state, state_root)?;
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
                // Opt-in merge into base — checkout base in main repo carefully.
                let merge_out = merge_into_base(&repo, &state.config.base_branch, &branch, timeout)?;
                fs::write(&paths.merge_output, &merge_out)?;
                attempt.merge_output_path = paths.merge_output.display().to_string();
                if merge_out.contains("MERGE_FAILED") {
                    attempt.merge_error = merge_out.clone();
                    attempt.phase = AttemptPhase::Approved;
                    state.status = TaskStatus::Stopped;
                } else {
                    finalize_merged(state, &mut attempt);
                    if let Some(g) = state.task_graph.as_mut() {
                        g.mark_node(node_id, NodeStatus::Done);
                    }
                }
            } else {
                finalize_handoff(state, &mut attempt);
                if let Some(g) = state.task_graph.as_mut() {
                    g.mark_node(node_id, NodeStatus::Done);
                }
            }
        }
        "reject" => {
            attempt.phase = AttemptPhase::Rejected;
            state.status = TaskStatus::Stopped;
            if let Some(g) = state.task_graph.as_mut() {
                g.mark_node(node_id, NodeStatus::Pending);
            }
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

    state.history.push(attempt);
    save_state(state, state_root)?;
    write_run_summary_if_terminal(state, state_root)?;
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
    let steps_cap = max_steps.unwrap_or(u32::MAX);
    let mut steps = 0u32;

    while steps < steps_cap {
        steps += 1;
        if state.iteration >= max_iter {
            state.status = TaskStatus::Failed;
            save_state(state, state_root)?;
            return Ok(RunOutcome::Failed);
        }

        // Ensure graph
        {
            let goal = state.goal.clone();
            if state.task_graph.is_none() {
                state.task_graph = Some(TaskGraph::single_node(&goal));
            }
        }
        let parallel = if state.config.allow_parallel_execution {
            state.config.max_parallel_nodes.max(1)
        } else {
            1
        };

        let ready_ids: Vec<(String, String)> = {
            let g = state.task_graph.as_ref().unwrap();
            if g.is_complete() && matches!(state.status, TaskStatus::Done) {
                write_run_summary_if_terminal(state, state_root)?;
                return Ok(RunOutcome::Success);
            }
            g.next_ready_nodes(parallel)
                .into_iter()
                .map(|n| (n.id.clone(), if n.goal.is_empty() { state.goal.clone() } else { n.goal.clone() }))
                .collect()
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

        // Sequential in v0.12 (parallel scheduler can expand ready set later)
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
                AutoStep::Resume => continue,
                AutoStep::Stop => {
                    write_run_summary_if_terminal(state, state_root)?;
                    return Ok(RunOutcome::UserStop);
                }
                AutoStep::Replan => {
                    state.status = TaskStatus::Replanning;
                    // Reset graph to single node for replan
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
