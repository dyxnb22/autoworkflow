//! Parallel node scheduling helpers + merge queue.

use crate::graph::{NodeStatus, TaskGraph};
use crate::state::TaskState;

/// Select ready nodes respecting max_parallel_nodes / allow_parallel_execution.
pub fn schedule_ready_nodes(state: &TaskState) -> Vec<(String, String)> {
    let Some(graph) = state.task_graph.as_ref() else {
        return vec![("n1".into(), state.goal.clone())];
    };
    let max = if state.config.allow_parallel_execution {
        state.config.max_parallel_nodes.max(1)
    } else {
        1
    };
    // Subtract already-running nodes
    let running = state.running_attempts.len() as u32;
    let slots = max.saturating_sub(running).max(if running == 0 { 1 } else { 0 });
    if slots == 0 {
        return Vec::new();
    }
    graph
        .next_ready_nodes(slots)
        .into_iter()
        .map(|n| {
            (
                n.id.clone(),
                if n.goal.is_empty() {
                    state.goal.clone()
                } else {
                    n.goal.clone()
                },
            )
        })
        .collect()
}

pub fn enqueue_merge(state: &mut TaskState, node_id: &str) {
    if !state.merge_queue.contains(&node_id.to_string()) {
        state.merge_queue.push(node_id.to_string());
    }
}

pub fn dequeue_merge(state: &mut TaskState) -> Option<String> {
    if state.merge_queue.is_empty() {
        None
    } else {
        Some(state.merge_queue.remove(0))
    }
}

pub fn mark_running(state: &mut TaskState, node_id: &str, iteration: u32) {
    state.running_attempts.insert(node_id.to_string(), iteration);
    if let Some(g) = state.task_graph.as_mut() {
        g.mark_node(node_id, NodeStatus::Running);
    }
}

pub fn clear_running(state: &mut TaskState, node_id: &str) {
    state.running_attempts.remove(node_id);
}

pub fn apply_graph_patch(graph: &mut TaskGraph, patch: &serde_json::Value) -> bool {
    // Minimal replan patch: replace nodes array and optional summary
    let mut changed = false;
    if let Some(summary) = patch.get("summary").and_then(|v| v.as_str()) {
        graph.summary = summary.to_string();
        changed = true;
    }
    if let Some(nodes) = patch.get("nodes") {
        if let Ok(parsed) = serde_json::from_value::<Vec<crate::graph::GraphNode>>(nodes.clone()) {
            if !parsed.is_empty() {
                graph.history.push(serde_json::json!({
                    "op": "replan",
                    "previous_nodes": graph.nodes.len(),
                }));
                graph.nodes = parsed;
                graph.version += 1;
                changed = true;
            }
        }
    }
    changed
}
