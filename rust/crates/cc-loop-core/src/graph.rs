//! Task graph models (single-node default; multi-node advanced).

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NodeStatus {
    Pending,
    Ready,
    Running,
    Blocked,
    Done,
    Failed,
    Skipped,
    Cancelled,
}

impl NodeStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Ready => "ready",
            Self::Running => "running",
            Self::Blocked => "blocked",
            Self::Done => "done",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
            Self::Cancelled => "cancelled",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphNode {
    pub id: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub goal: String,
    #[serde(default)]
    pub depends_on: Vec<String>,
    #[serde(default = "default_pending")]
    pub status: NodeStatus,
    #[serde(default)]
    pub attempt_iteration: Option<u32>,
    #[serde(default)]
    pub attempt_retry: Option<u32>,
    #[serde(default)]
    pub summary: String,
}

fn default_pending() -> NodeStatus {
    NodeStatus::Pending
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskGraph {
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub nodes: Vec<GraphNode>,
    #[serde(default)]
    pub version: u32,
    #[serde(default)]
    pub history: Vec<Value>,
}

impl TaskGraph {
    pub fn single_node(goal: &str) -> Self {
        Self {
            summary: goal.chars().take(240).collect(),
            nodes: vec![GraphNode {
                id: "n1".into(),
                title: "deliver".into(),
                goal: goal.to_string(),
                depends_on: Vec::new(),
                status: NodeStatus::Ready,
                attempt_iteration: None,
                attempt_retry: None,
                summary: String::new(),
            }],
            version: 1,
            history: Vec::new(),
        }
    }

    pub fn is_complete(&self) -> bool {
        !self.nodes.is_empty()
            && self.nodes.iter().all(|n| {
                matches!(
                    n.status,
                    NodeStatus::Done | NodeStatus::Skipped | NodeStatus::Cancelled
                )
            })
    }

    pub fn next_ready_nodes(&self, max_parallel: u32) -> Vec<&GraphNode> {
        let done: std::collections::HashSet<&str> = self
            .nodes
            .iter()
            .filter(|n| matches!(n.status, NodeStatus::Done | NodeStatus::Skipped))
            .map(|n| n.id.as_str())
            .collect();
        let mut ready: Vec<&GraphNode> = self
            .nodes
            .iter()
            .filter(|n| {
                matches!(n.status, NodeStatus::Pending | NodeStatus::Ready)
                    && n.depends_on.iter().all(|d| done.contains(d.as_str()))
            })
            .collect();
        ready.sort_by(|a, b| a.id.cmp(&b.id));
        let limit = max_parallel.max(1) as usize;
        ready.into_iter().take(limit).collect()
    }

    pub fn mark_node(&mut self, id: &str, status: NodeStatus) {
        if let Some(n) = self.nodes.iter_mut().find(|n| n.id == id) {
            n.status = status;
        }
    }

    pub fn status_summary(&self) -> serde_json::Value {
        let mut counts = serde_json::Map::new();
        for n in &self.nodes {
            let key = n.status.as_str().to_string();
            let entry = counts.entry(key).or_insert(serde_json::json!(0));
            if let Some(v) = entry.as_u64() {
                *entry = serde_json::json!(v + 1);
            }
        }
        serde_json::json!({
            "total": self.nodes.len(),
            "complete": self.is_complete(),
            "counts": counts,
        })
    }
}

/// Parse planner JSON into a task graph. Legacy single-step wraps to one node.
pub fn parse_planner_graph(plan: &Value, goal: &str) -> TaskGraph {
    if let Some(mode) = plan.get("mode").and_then(|v| v.as_str()) {
        if mode == "task_graph" {
            if let Ok(graph) = serde_json::from_value::<TaskGraph>(plan.clone()) {
                if !graph.nodes.is_empty() {
                    return graph;
                }
            }
            if let Some(nodes) = plan.get("nodes").cloned() {
                if let Ok(parsed) = serde_json::from_value::<Vec<GraphNode>>(nodes) {
                    if !parsed.is_empty() {
                        return TaskGraph {
                            summary: plan
                                .get("summary")
                                .and_then(|v| v.as_str())
                                .unwrap_or(goal)
                                .to_string(),
                            nodes: parsed,
                            version: 1,
                            history: Vec::new(),
                        };
                    }
                }
            }
        }
    }
    // Legacy / single
    let summary = plan
        .get("summary")
        .or_else(|| plan.get("title"))
        .or_else(|| plan.get("expected_changes"))
        .and_then(|v| v.as_str())
        .unwrap_or(goal);
    let mut graph = TaskGraph::single_node(goal);
    graph.summary = summary.chars().take(240).collect();
    if let Some(n) = graph.nodes.first_mut() {
        n.goal = plan
            .get("implementer_prompt")
            .or_else(|| plan.get("goal"))
            .and_then(|v| v.as_str())
            .unwrap_or(goal)
            .to_string();
    }
    graph
}

pub fn ensure_task_graph(state: &mut crate::state::TaskState) -> &TaskGraph {
    if state.task_graph.is_none() {
        state.task_graph = Some(TaskGraph::single_node(&state.goal));
    }
    state.task_graph.as_ref().expect("just set")
}

pub fn build_graph_snapshot(graph: &TaskGraph) -> serde_json::Value {
    let status = graph.status_summary();
    let current = graph
        .next_ready_nodes(1)
        .first()
        .map(|n| n.id.clone())
        .or_else(|| graph.nodes.last().map(|n| n.id.clone()))
        .unwrap_or_default();
    serde_json::json!({
        "schema_version": 1,
        "summary": graph.summary,
        "version": graph.version,
        "current_node_id": current,
        "status": status,
        "nodes": graph.nodes,
    })
}

/// Wrapper used by `graph --json` CLI.
pub fn build_graph_cli_payload(graph: Option<&TaskGraph>) -> serde_json::Value {
    match graph {
        Some(g) => serde_json::json!({"task_graph": build_graph_snapshot(g)}),
        None => serde_json::json!({"task_graph": null}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn single_node_ready() {
        let g = TaskGraph::single_node("do it");
        assert_eq!(g.next_ready_nodes(1).len(), 1);
        assert!(!g.is_complete());
    }

    #[test]
    fn dependency_order() {
        let mut g = TaskGraph {
            summary: "x".into(),
            nodes: vec![
                GraphNode {
                    id: "a".into(),
                    title: "a".into(),
                    goal: "a".into(),
                    depends_on: vec![],
                    status: NodeStatus::Done,
                    attempt_iteration: None,
                    attempt_retry: None,
                    summary: String::new(),
                },
                GraphNode {
                    id: "b".into(),
                    title: "b".into(),
                    goal: "b".into(),
                    depends_on: vec!["a".into()],
                    status: NodeStatus::Pending,
                    attempt_iteration: None,
                    attempt_retry: None,
                    summary: String::new(),
                },
            ],
            version: 1,
            history: vec![],
        };
        assert_eq!(g.next_ready_nodes(2)[0].id, "b");
        g.mark_node("b", NodeStatus::Done);
        assert!(g.is_complete());
    }
}
