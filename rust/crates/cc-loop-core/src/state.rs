//! Persistent task state and attempt records.

use std::collections::HashMap;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use chrono::{SecondsFormat, Utc};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::{default_config, merge_config, LoopConfig};
use crate::error::{CcError, Result};
use crate::graph::TaskGraph;
use crate::paths::{state_path, task_dir};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Initialized,
    Running,
    WaitingManualReview,
    Done,
    Failed,
    Stopped,
    Interrupted,
    Cancelled,
    Replanning,
}

impl TaskStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Initialized => "initialized",
            Self::Running => "running",
            Self::WaitingManualReview => "waiting_manual_review",
            Self::Done => "done",
            Self::Failed => "failed",
            Self::Stopped => "stopped",
            Self::Interrupted => "interrupted",
            Self::Cancelled => "cancelled",
            Self::Replanning => "replanning",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttemptPhase {
    Preflight,
    Planning,
    WorktreeCreated,
    Executing,
    Testing,
    Reviewing,
    Approved,
    Rejected,
    Merged,
    Failed,
    Replanning,
}

impl AttemptPhase {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Preflight => "preflight",
            Self::Planning => "planning",
            Self::WorktreeCreated => "worktree_created",
            Self::Executing => "executing",
            Self::Testing => "testing",
            Self::Reviewing => "reviewing",
            Self::Approved => "approved",
            Self::Rejected => "rejected",
            Self::Merged => "merged",
            Self::Failed => "failed",
            Self::Replanning => "replanning",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AttemptRecord {
    pub iteration: u32,
    pub retry: u32,
    pub created_at: String,
    pub base_commit: String,
    #[serde(default)]
    pub graph_node_id: String,
    #[serde(default)]
    pub head_commit: String,
    #[serde(default)]
    pub branch: String,
    #[serde(default)]
    pub worktree_path: String,
    pub phase: AttemptPhase,
    #[serde(default)]
    pub plan_raw_path: String,
    #[serde(default)]
    pub plan_json: Option<Value>,
    #[serde(default)]
    pub plan_provider: String,
    #[serde(default)]
    pub implementer_prompt_path: String,
    #[serde(default)]
    pub implementer_raw_path: String,
    #[serde(default)]
    pub implementer_exit_code: Option<i32>,
    #[serde(default)]
    pub implementer_provider: String,
    #[serde(default)]
    pub running_provider: String,
    #[serde(default)]
    pub test_command: Vec<String>,
    #[serde(default)]
    pub test_exit_code: Option<i32>,
    #[serde(default)]
    pub test_status: String,
    #[serde(default)]
    pub test_raw_path: String,
    #[serde(default)]
    pub diff_stat_path: String,
    #[serde(default)]
    pub diff_patch_paths: Vec<String>,
    #[serde(default)]
    pub review_raw_path: String,
    #[serde(default)]
    pub review_json: Option<Value>,
    #[serde(default)]
    pub review_provider: String,
    #[serde(default)]
    pub decision: String,
    #[serde(default)]
    pub merge_error: String,
    #[serde(default)]
    pub merge_output_path: String,
    #[serde(default)]
    pub failure_type: String,
    #[serde(default)]
    pub recovery_disposition: String,
    #[serde(default)]
    pub stop_reason: String,
    #[serde(default)]
    pub attempted_repairs: Vec<String>,
    #[serde(default)]
    pub recovery_retry_count: u32,
    #[serde(default)]
    pub failure_details: Value,
    #[serde(default)]
    pub merge_retry_count: u32,
}

impl AttemptRecord {
    pub fn new(iteration: u32, retry: u32, base_commit: impl Into<String>) -> Self {
        Self {
            iteration,
            retry,
            created_at: utc_now_iso(),
            base_commit: base_commit.into(),
            graph_node_id: String::new(),
            head_commit: String::new(),
            branch: String::new(),
            worktree_path: String::new(),
            phase: AttemptPhase::Preflight,
            plan_raw_path: String::new(),
            plan_json: None,
            plan_provider: String::new(),
            implementer_prompt_path: String::new(),
            implementer_raw_path: String::new(),
            implementer_exit_code: None,
            implementer_provider: String::new(),
            running_provider: String::new(),
            test_command: Vec::new(),
            test_exit_code: None,
            test_status: String::new(),
            test_raw_path: String::new(),
            diff_stat_path: String::new(),
            diff_patch_paths: Vec::new(),
            review_raw_path: String::new(),
            review_json: None,
            review_provider: String::new(),
            decision: String::new(),
            merge_error: String::new(),
            merge_output_path: String::new(),
            failure_type: String::new(),
            recovery_disposition: String::new(),
            stop_reason: String::new(),
            attempted_repairs: Vec::new(),
            recovery_retry_count: 0,
            failure_details: Value::Object(Default::default()),
            merge_retry_count: 0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskState {
    pub task_id: String,
    pub goal: String,
    pub target_repo: String,
    pub base_branch: String,
    pub base_commit: String,
    pub status: TaskStatus,
    pub iteration: u32,
    pub config: LoopConfig,
    #[serde(default)]
    pub history: Vec<AttemptRecord>,
    #[serde(default)]
    pub providers: HashMap<String, String>,
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    #[serde(default)]
    pub task_graph: Option<TaskGraph>,
    #[serde(default)]
    pub merge_queue: Vec<String>,
    #[serde(default)]
    pub running_attempts: HashMap<String, u32>,
}

fn default_schema_version() -> u32 {
    1
}

impl TaskState {
    pub fn latest_attempt(&self) -> Option<&AttemptRecord> {
        self.history.last()
    }

    pub fn latest_attempt_mut(&mut self) -> Option<&mut AttemptRecord> {
        self.history.last_mut()
    }
}

pub fn utc_now_iso() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

pub fn create_initial_state(
    task_id: &str,
    goal: &str,
    target_repo: &Path,
    base_branch: &str,
    base_commit: &str,
    config: Option<LoopConfig>,
) -> TaskState {
    let merged = config.unwrap_or_else(default_config);
    let mut providers = HashMap::new();
    providers.insert("planner".into(), merged.planner_provider.clone());
    providers.insert("reviewer".into(), merged.reviewer_provider.clone());
    providers.insert("implementer".into(), merged.implementer_provider.clone());
    TaskState {
        task_id: task_id.to_string(),
        goal: goal.to_string(),
        target_repo: target_repo
            .canonicalize()
            .unwrap_or_else(|_| target_repo.to_path_buf())
            .display()
            .to_string(),
        base_branch: base_branch.to_string(),
        base_commit: base_commit.to_string(),
        status: TaskStatus::Initialized,
        iteration: 0,
        config: merged,
        history: Vec::new(),
        providers,
        schema_version: 1,
        task_graph: None,
        merge_queue: Vec::new(),
        running_attempts: HashMap::new(),
    }
}

fn lock_path(state_root: &Path, task_id: &str) -> PathBuf {
    task_dir(state_root, task_id).join("state.lock")
}

fn with_task_lock<T>(state_root: &Path, task_id: &str, f: impl FnOnce() -> Result<T>) -> Result<T> {
    let dir = task_dir(state_root, task_id);
    fs::create_dir_all(&dir)?;
    let path = lock_path(state_root, task_id);
    let file = fs::OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(&path)?;
    file.lock_exclusive().map_err(|e| CcError::user(format!("state lock failed: {e}")))?;
    let result = f();
    let _ = file.unlock();
    result
}

pub fn atomic_write_text(path: &Path, content: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("tmp");
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(content.as_bytes())?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    Ok(())
}

pub fn save_state(state: &TaskState, state_root: &Path) -> Result<PathBuf> {
    with_task_lock(state_root, &state.task_id, || {
        let path = state_path(state_root, &state.task_id);
        let payload = serde_json::to_string_pretty(state)? + "\n";
        atomic_write_text(&path, &payload)?;
        Ok(path)
    })
}

fn normalize_legacy_attempt(mut item: Value) -> Value {
    if let Some(obj) = item.as_object_mut() {
        if !obj.contains_key("implementer_prompt_path") {
            if let Some(v) = obj.remove("cursor_prompt_path") {
                obj.insert("implementer_prompt_path".into(), v);
            }
        }
        if !obj.contains_key("implementer_raw_path") {
            if let Some(v) = obj.remove("cursor_raw_path") {
                obj.insert("implementer_raw_path".into(), v);
            }
        }
        if !obj.contains_key("implementer_exit_code") {
            if let Some(v) = obj.remove("cursor_exit_code") {
                obj.insert("implementer_exit_code".into(), v);
            }
        }
        obj.remove("cursor_prompt_path");
        obj.remove("cursor_raw_path");
        obj.remove("cursor_exit_code");
    }
    item
}

pub fn task_state_from_value(mut data: Value) -> Result<TaskState> {
    if let Some(obj) = data.as_object_mut() {
        // Merge config onto defaults for legacy files.
        let overrides = obj.get("config").cloned();
        let merged = merge_config(overrides.as_ref());
        obj.insert("config".into(), serde_json::to_value(merged)?);

        if let Some(Value::Array(history)) = obj.get_mut("history") {
            for item in history.iter_mut() {
                *item = normalize_legacy_attempt(item.clone());
            }
        }
    }
    Ok(serde_json::from_value(data)?)
}

pub fn load_state(task_id: &str, state_root: &Path) -> Result<TaskState> {
    with_task_lock(state_root, task_id, || {
        let path = state_path(state_root, task_id);
        if !path.is_file() {
            return Err(CcError::user(format!("task not found: {task_id}")));
        }
        let mut f = fs::File::open(&path)?;
        let mut buf = String::new();
        f.read_to_string(&mut buf)?;
        let data: Value = serde_json::from_str(&buf)?;
        task_state_from_value(data)
    })
}

pub fn list_task_ids(state_root: &Path) -> Result<Vec<String>> {
    let root = state_root.join("tasks");
    if !root.is_dir() {
        return Ok(Vec::new());
    }
    let mut ids = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            let name = entry.file_name().to_string_lossy().to_string();
            if state_path(state_root, &name).is_file() {
                ids.push(name);
            }
        }
    }
    ids.sort();
    Ok(ids)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn roundtrip_state() {
        let dir = tempdir().unwrap();
        let state = create_initial_state(
            "t1",
            "goal",
            dir.path(),
            "main",
            "abc",
            None,
        );
        save_state(&state, dir.path()).unwrap();
        let loaded = load_state("t1", dir.path()).unwrap();
        assert_eq!(loaded.task_id, "t1");
        assert_eq!(loaded.schema_version, 1);
        assert!(!loaded.config.auto_merge);
    }

    #[test]
    fn legacy_missing_config_keys_get_defaults() {
        let raw = serde_json::json!({
            "task_id": "legacy",
            "goal": "g",
            "target_repo": "/tmp",
            "base_branch": "main",
            "base_commit": "abc",
            "status": "initialized",
            "iteration": 0,
            "config": {"max_iterations": 5},
            "history": [],
            "providers": {}
        });
        let state = task_state_from_value(raw).unwrap();
        assert_eq!(state.config.max_iterations, 5);
        assert!(!state.config.auto_merge);
        assert!(state.config.require_distinct_reviewer);
    }
}
