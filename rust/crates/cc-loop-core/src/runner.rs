//! Detached runner control (pid / log / heartbeat / stop / cancel).

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::{CcError, Result};
use crate::paths::{heartbeat_path, runner_log_path, runner_pid_path, task_dir};
use crate::state::{atomic_write_text, load_state, save_state, utc_now_iso, TaskStatus};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunnerHeartbeat {
    pub status: String,
    pub updated_at: String,
    #[serde(default)]
    pub started_at: String,
    #[serde(default)]
    pub phase: String,
    #[serde(default)]
    pub running_provider: String,
    #[serde(default)]
    pub iteration: u32,
    #[serde(default)]
    pub pid: u32,
    #[serde(default)]
    pub task_id: String,
    #[serde(default)]
    pub provider_progress: Value,
}

pub fn write_heartbeat(state_root: &Path, task_id: &str, hb: &RunnerHeartbeat) -> Result<()> {
    let path = heartbeat_path(state_root, task_id);
    atomic_write_text(&path, &(serde_json::to_string_pretty(hb)? + "\n"))?;
    Ok(())
}

pub fn read_heartbeat(state_root: &Path, task_id: &str) -> Option<RunnerHeartbeat> {
    let path = heartbeat_path(state_root, task_id);
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

pub fn is_heartbeat_stale(hb: &RunnerHeartbeat, stale_seconds: u64) -> bool {
    let Ok(ts) = chrono::DateTime::parse_from_rfc3339(&hb.updated_at) else {
        return true;
    };
    let age = chrono::Utc::now().signed_duration_since(ts.with_timezone(&chrono::Utc));
    age.num_seconds() > stale_seconds as i64
}

pub fn write_pid(state_root: &Path, task_id: &str, pid: u32) -> Result<()> {
    atomic_write_text(&runner_pid_path(state_root, task_id), &format!("{pid}\n"))?;
    Ok(())
}

pub fn read_pid(state_root: &Path, task_id: &str) -> Option<u32> {
    let text = fs::read_to_string(runner_pid_path(state_root, task_id)).ok()?;
    text.trim().parse().ok()
}

pub fn clear_runner_files(state_root: &Path, task_id: &str) {
    let _ = fs::remove_file(runner_pid_path(state_root, task_id));
    let _ = fs::remove_file(heartbeat_path(state_root, task_id));
}

pub fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    let rc = unsafe { libc::kill(pid as i32, 0) };
    rc == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

pub fn is_runner_alive(state_root: &Path, task_id: &str) -> (bool, Option<u32>) {
    let pid = read_pid(state_root, task_id);
    if let Some(p) = pid {
        if pid_alive(p) {
            return (true, Some(p));
        }
    }
    if let Some(hb) = read_heartbeat(state_root, task_id) {
        if matches!(hb.status.as_str(), "running" | "replanning") && hb.pid != 0 && pid_alive(hb.pid)
        {
            return (true, Some(hb.pid));
        }
        if pid.is_some() {
            return (false, pid);
        }
        if hb.pid != 0 {
            return (false, Some(hb.pid));
        }
    }
    (false, pid)
}

pub fn stop_runner(state_root: &Path, task_id: &str) -> Result<Value> {
    let (alive, pid) = is_runner_alive(state_root, task_id);
    if let Some(p) = pid {
        if alive {
            unsafe {
                libc::kill(p as i32, libc::SIGTERM);
            }
            std::thread::sleep(std::time::Duration::from_millis(200));
            if pid_alive(p) {
                unsafe {
                    libc::kill(p as i32, libc::SIGKILL);
                }
            }
        }
    }
    clear_runner_files(state_root, task_id);
    Ok(serde_json::json!({
        "ok": true,
        "action": "stop",
        "task_id": task_id,
        "stopped": alive,
        "pid": pid,
        "message": if alive { "runner stopped" } else { "no live runner" },
    }))
}

pub fn cancel_task(state_root: &Path, task_id: &str) -> Result<Value> {
    let stop = stop_runner(state_root, task_id)?;
    let mut state = load_state(task_id, state_root)?;
    state.status = TaskStatus::Cancelled;
    save_state(&state, state_root)?;
    crate::events::append_event(
        state_root,
        task_id,
        "task.cancelled",
        state.iteration,
        0,
        "",
        "cancelled",
        "task cancelled",
        serde_json::json!({}),
    )?;
    Ok(serde_json::json!({
        "ok": true,
        "action": "cancel",
        "task_id": task_id,
        "status": "cancelled",
        "stop": stop,
        "message": "task cancelled",
    }))
}

pub fn cleanup_task(state_root: &Path, task_id: &str) -> Result<Value> {
    let _ = stop_runner(state_root, task_id)?;
    let dir = task_dir(state_root, task_id);
    let mut removed = Vec::new();
    for name in ["runner.pid", "runner.log", "runner.heartbeat.json"] {
        let p = dir.join(name);
        if p.exists() {
            let _ = fs::remove_file(&p);
            removed.push(name);
        }
    }
    Ok(serde_json::json!({
        "ok": true,
        "action": "cleanup",
        "task_id": task_id,
        "removed": removed,
        "message": "runtime artifacts cleaned",
    }))
}

pub fn spawn_detached_auto(
    state_root: &Path,
    task_id: &str,
    exe: &Path,
) -> Result<Value> {
    let (alive, _) = is_runner_alive(state_root, task_id);
    if alive {
        return Err(CcError::user(format!(
            "runner already alive for task {task_id}"
        )));
    }
    fs::create_dir_all(task_dir(state_root, task_id))?;
    let log_path = runner_log_path(state_root, task_id);
    let log = fs::File::create(&log_path)?;
    let log_err = log.try_clone()?;
    let child = Command::new(exe)
        .args([
            "--state-root",
            &state_root.display().to_string(),
            "auto",
            "--task-id",
            task_id,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err))
        .spawn()
        .map_err(|e| CcError::execution(format!("failed to detach auto: {e}")))?;
    let pid = child.id();
    std::mem::forget(child);
    write_pid(state_root, task_id, pid)?;
    let now = utc_now_iso();
    write_heartbeat(
        state_root,
        task_id,
        &RunnerHeartbeat {
            status: "running".into(),
            updated_at: now.clone(),
            started_at: now,
            phase: "preflight".into(),
            running_provider: String::new(),
            iteration: 0,
            pid,
            task_id: task_id.to_string(),
            provider_progress: Value::Null,
        },
    )?;
    Ok(serde_json::json!({
        "ok": true,
        "task_id": task_id,
        "pid": pid,
        "log": log_path.display().to_string(),
    }))
}
