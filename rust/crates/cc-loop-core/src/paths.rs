//! Filesystem path helpers for state and artifacts.

use std::path::{Path, PathBuf};

pub fn default_state_root() -> PathBuf {
    dirs_home()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".cc-loop")
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

pub fn default_worktree_root(state_root: &Path) -> PathBuf {
    state_root.join("worktrees")
}

pub fn task_dir(state_root: &Path, task_id: &str) -> PathBuf {
    state_root.join("tasks").join(task_id)
}

pub fn state_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("state.json")
}

pub fn iteration_suffix(iteration: u32, retry: u32) -> String {
    if retry == 0 {
        format!("iter-{iteration:03}")
    } else {
        format!("iter-{iteration:03}-retry-{retry:02}")
    }
}

pub fn artifacts_dir(state_root: &Path, task_id: &str, iteration: u32, retry: u32) -> PathBuf {
    task_dir(state_root, task_id)
        .join("artifacts")
        .join(iteration_suffix(iteration, retry))
}

pub fn branch_name(task_id: &str, iteration: u32, retry: u32) -> String {
    format!("cc-loop/{task_id}/{}", iteration_suffix(iteration, retry))
}

pub fn worktree_path(
    worktree_root: &Path,
    repo_label: &str,
    task_id: &str,
    iteration: u32,
    retry: u32,
) -> PathBuf {
    worktree_root
        .join(repo_label)
        .join(task_id)
        .join(iteration_suffix(iteration, retry))
}

pub fn runner_pid_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("runner.pid")
}

pub fn runner_log_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("runner.log")
}

pub fn heartbeat_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("runner.heartbeat.json")
}

pub fn failure_report_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("failure.report.json")
}

pub fn run_summary_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("run.summary.json")
}

pub fn events_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("events.jsonl")
}

/// Deterministic artifact path map for one attempt.
#[derive(Debug, Clone)]
pub struct ArtifactPaths {
    pub root: PathBuf,
    pub plan_prompt: PathBuf,
    pub plan_raw: PathBuf,
    pub plan_last_message: PathBuf,
    pub plan_parsed: PathBuf,
    pub plan_provider: PathBuf,
    pub implementer_prompt: PathBuf,
    pub implementer_raw: PathBuf,
    pub implementer_provider: PathBuf,
    pub test_output: PathBuf,
    pub diff_stat: PathBuf,
    pub diff_files: PathBuf,
    pub patches_dir: PathBuf,
    pub review_prompt: PathBuf,
    pub review_raw: PathBuf,
    pub review_last_message: PathBuf,
    pub review_parsed: PathBuf,
    pub review_provider: PathBuf,
    pub merge_output: PathBuf,
    pub attempt_trace: PathBuf,
    pub prompt_cache: PathBuf,
    pub subprocess_result: PathBuf,
    pub command_argv: PathBuf,
}

impl ArtifactPaths {
    pub fn new(root: PathBuf) -> Self {
        Self {
            plan_prompt: root.join("plan.prompt.txt"),
            plan_raw: root.join("plan.raw.jsonl"),
            plan_last_message: root.join("plan.last-message.txt"),
            plan_parsed: root.join("plan.parsed.json"),
            plan_provider: root.join("plan.provider.txt"),
            implementer_prompt: root.join("implementer.prompt.txt"),
            implementer_raw: root.join("implementer.raw.json"),
            implementer_provider: root.join("implementer.provider.txt"),
            test_output: root.join("test.output.txt"),
            diff_stat: root.join("diff.stat.txt"),
            diff_files: root.join("diff.files.txt"),
            patches_dir: root.join("patches"),
            review_prompt: root.join("review.prompt.txt"),
            review_raw: root.join("review.raw.jsonl"),
            review_last_message: root.join("review.last-message.txt"),
            review_parsed: root.join("review.parsed.json"),
            review_provider: root.join("review.provider.txt"),
            merge_output: root.join("merge.output.txt"),
            attempt_trace: root.join("attempt.trace.json"),
            prompt_cache: root.join("prompt.cache.json"),
            subprocess_result: root.join("subprocess.result.json"),
            command_argv: root.join("command.argv.json"),
            root,
        }
    }

    pub fn as_string_map(&self) -> std::collections::BTreeMap<String, String> {
        let pairs = [
            ("plan_prompt", &self.plan_prompt),
            ("plan_raw", &self.plan_raw),
            ("plan_last_message", &self.plan_last_message),
            ("plan_parsed", &self.plan_parsed),
            ("plan_provider", &self.plan_provider),
            ("implementer_prompt", &self.implementer_prompt),
            ("implementer_raw", &self.implementer_raw),
            ("implementer_provider", &self.implementer_provider),
            ("test_output", &self.test_output),
            ("diff_stat", &self.diff_stat),
            ("diff_files", &self.diff_files),
            ("patches_dir", &self.patches_dir),
            ("review_prompt", &self.review_prompt),
            ("review_raw", &self.review_raw),
            ("review_last_message", &self.review_last_message),
            ("review_parsed", &self.review_parsed),
            ("review_provider", &self.review_provider),
            ("merge_output", &self.merge_output),
            ("attempt_trace", &self.attempt_trace),
            ("prompt_cache", &self.prompt_cache),
        ];
        pairs
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.display().to_string()))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iteration_suffix_formats() {
        assert_eq!(iteration_suffix(1, 0), "iter-001");
        assert_eq!(iteration_suffix(2, 3), "iter-002-retry-03");
    }
}
