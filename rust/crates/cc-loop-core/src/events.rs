//! Append-only JSONL event stream.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use serde_json::{json, Value};
use uuid::Uuid;

use crate::error::Result;
use crate::paths::events_path;
use crate::state::utc_now_iso;

#[allow(clippy::too_many_arguments)]
pub fn append_event(
    state_root: &Path,
    task_id: &str,
    event_type: &str,
    iteration: u32,
    retry: u32,
    graph_node_id: &str,
    phase: &str,
    message: &str,
    details: Value,
) -> Result<()> {
    let path = events_path(state_root, task_id);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let row = json!({
        "event_id": Uuid::new_v4().to_string(),
        "task_id": task_id,
        "timestamp": utc_now_iso(),
        "type": event_type,
        "iteration": iteration,
        "retry": retry,
        "graph_node_id": graph_node_id,
        "phase": phase,
        "message": message,
        "details": details,
    });
    let mut f = OpenOptions::new().create(true).append(true).open(path)?;
    writeln!(f, "{}", serde_json::to_string(&row)?)?;
    Ok(())
}
