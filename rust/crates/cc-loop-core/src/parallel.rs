//! Parallel node scheduling + concurrent execution + merge queue.

use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::thread;

use crate::error::{CcError, Result};
use crate::graph::{NodeStatus, TaskGraph};
use crate::state::{load_state, save_state, with_state_mut, AttemptPhase, TaskState};

pub fn parallel_execution_enabled(state: &TaskState) -> bool {
    state.config.allow_parallel_execution && state.config.max_parallel_nodes > 1
}

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

pub fn pending_merge_count(state: &TaskState) -> usize {
    state.merge_queue.len()
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

pub struct ParallelJob {
    pub node_id: String,
    pub node_goal: String,
    pub iteration: u32,
    pub retry: u32,
}

/// Reserve iterations / running slots under a single lock, then run workers concurrently.
pub fn prepare_parallel_jobs(
    state: &mut TaskState,
    state_root: &Path,
    ready: &[(String, String)],
    max_retries: u32,
    retry_for: impl Fn(&TaskState, &str) -> u32,
) -> Result<Vec<ParallelJob>> {
    let mut jobs = Vec::new();
    for (node_id, node_goal) in ready {
        let retry = retry_for(state, node_id);
        if retry > max_retries {
            return Err(CcError::execution(format!(
                "max retries exceeded for node {node_id}"
            )));
        }
        state.iteration += 1;
        let iteration = state.iteration;
        mark_running(state, node_id, iteration);
        jobs.push(ParallelJob {
            node_id: node_id.clone(),
            node_goal: node_goal.clone(),
            iteration,
            retry,
        });
    }
    save_state(state, state_root)?;
    Ok(jobs)
}

/// Run `worker` concurrently for each job. Worker receives (state_root, task_id, job).
/// After all join, reload state into `state`.
pub fn run_jobs_concurrently<F>(
    state: &mut TaskState,
    state_root: &Path,
    jobs: Vec<ParallelJob>,
    worker: F,
) -> Result<()>
where
    F: Fn(PathBuf, String, ParallelJob) -> Result<()> + Send + Sync + 'static,
{
    let task_id = state.task_id.clone();
    let state_root_buf = state_root.to_path_buf();
    let worker = std::sync::Arc::new(worker);
    let (tx, rx) = mpsc::channel();
    let mut handles = Vec::new();

    for job in jobs {
        let tx = tx.clone();
        let sr = state_root_buf.clone();
        let tid = task_id.clone();
        let w = worker.clone();
        handles.push(thread::spawn(move || {
            let result = w(sr, tid, job);
            let _ = tx.send(result);
        }));
    }
    drop(tx);

    let mut first_err: Option<CcError> = None;
    for _ in 0..handles.len() {
        match rx.recv() {
            Ok(Ok(())) => {}
            Ok(Err(e)) => {
                if first_err.is_none() {
                    first_err = Some(e);
                }
            }
            Err(_) => {
                if first_err.is_none() {
                    first_err = Some(CcError::execution("parallel worker channel closed"));
                }
            }
        }
    }
    for h in handles {
        let _ = h.join();
    }

    *state = load_state(&task_id, state_root)?;
    if let Some(e) = first_err {
        return Err(e);
    }
    Ok(())
}

/// After parallel approve, enqueue merges atomically.
pub fn finalize_parallel_approvals(state_root: &Path, task_id: &str) -> Result<()> {
    with_state_mut(task_id, state_root, |state| {
        let approved: Vec<String> = state
            .history
            .iter()
            .rev()
            .filter(|a| {
                a.phase == AttemptPhase::Approved
                    && a.decision == "approve"
                    && !a.graph_node_id.is_empty()
            })
            .map(|a| a.graph_node_id.clone())
            .collect();
        for node_id in approved {
            if state.config.auto_merge {
                enqueue_merge(state, &node_id);
            }
            clear_running(state, &node_id);
        }
        Ok(())
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::default_config;
    use crate::graph::TaskGraph;
    use crate::state::create_initial_state;
    use tempfile::tempdir;

    #[test]
    fn parallel_flag_requires_explicit_config() {
        let dir = tempdir().unwrap();
        let mut cfg = default_config();
        assert!(!parallel_execution_enabled(&create_initial_state(
            "t", "g", dir.path(), "main", "abc", Some(cfg.clone())
        )));
        cfg.allow_parallel_execution = true;
        cfg.max_parallel_nodes = 2;
        assert!(parallel_execution_enabled(&create_initial_state(
            "t", "g", dir.path(), "main", "abc", Some(cfg)
        )));
    }

    #[test]
    fn schedule_respects_max_parallel() {
        let dir = tempdir().unwrap();
        let mut cfg = default_config();
        cfg.allow_parallel_execution = true;
        cfg.max_parallel_nodes = 2;
        let mut state = create_initial_state("t", "g", dir.path(), "main", "abc", Some(cfg));
        state.task_graph = Some(TaskGraph {
            summary: "x".into(),
            nodes: vec![
                crate::graph::GraphNode {
                    id: "a".into(),
                    title: "a".into(),
                    goal: "a".into(),
                    depends_on: vec![],
                    status: NodeStatus::Ready,
                    attempt_iteration: None,
                    attempt_retry: None,
                    summary: String::new(),
                },
                crate::graph::GraphNode {
                    id: "b".into(),
                    title: "b".into(),
                    goal: "b".into(),
                    depends_on: vec![],
                    status: NodeStatus::Ready,
                    attempt_iteration: None,
                    attempt_retry: None,
                    summary: String::new(),
                },
                crate::graph::GraphNode {
                    id: "c".into(),
                    title: "c".into(),
                    goal: "c".into(),
                    depends_on: vec![],
                    status: NodeStatus::Ready,
                    attempt_iteration: None,
                    attempt_retry: None,
                    summary: String::new(),
                },
            ],
            version: 1,
            history: vec![],
        });
        assert_eq!(schedule_ready_nodes(&state).len(), 2);
    }

    #[test]
    fn concurrent_workers_preserve_all_history() {
        use crate::state::{create_initial_state, save_state};
        use std::sync::{Arc, Mutex};
        use std::time::{Duration, Instant};

        let dir = tempdir().unwrap();
        let mut cfg = default_config();
        cfg.allow_parallel_execution = true;
        cfg.max_parallel_nodes = 2;
        let mut state = create_initial_state("par", "g", dir.path(), "main", "abc", Some(cfg));
        state.task_graph = Some(TaskGraph {
            summary: "x".into(),
            nodes: vec![
                crate::graph::GraphNode {
                    id: "a".into(),
                    title: "a".into(),
                    goal: "a".into(),
                    depends_on: vec![],
                    status: NodeStatus::Ready,
                    attempt_iteration: None,
                    attempt_retry: None,
                    summary: String::new(),
                },
                crate::graph::GraphNode {
                    id: "b".into(),
                    title: "b".into(),
                    goal: "b".into(),
                    depends_on: vec![],
                    status: NodeStatus::Ready,
                    attempt_iteration: None,
                    attempt_retry: None,
                    summary: String::new(),
                },
            ],
            version: 1,
            history: vec![],
        });
        save_state(&state, dir.path()).unwrap();

        let ready = schedule_ready_nodes(&state);
        let jobs = prepare_parallel_jobs(&mut state, dir.path(), &ready, 2, |_, _| 0).unwrap();
        assert_eq!(jobs.len(), 2);

        let started = Arc::new(Mutex::new(Vec::new()));
        let started_c = started.clone();
        let t0 = Instant::now();
        run_jobs_concurrently(&mut state, dir.path(), jobs, move |_sr, tid, job| {
            started_c.lock().unwrap().push(job.node_id.clone());
            // Overlap window: both workers should be inside this sleep together.
            thread::sleep(Duration::from_millis(80));
            with_state_mut(&tid, &_sr, |st| {
                st.history.push(crate::state::AttemptRecord::new(
                    job.iteration,
                    job.retry,
                    "abc",
                ));
                if let Some(last) = st.history.last_mut() {
                    last.graph_node_id = job.node_id.clone();
                    last.phase = AttemptPhase::Approved;
                    last.decision = "approve".into();
                }
                clear_running(st, &job.node_id);
                if let Some(g) = st.task_graph.as_mut() {
                    g.mark_node(&job.node_id, NodeStatus::Done);
                }
                Ok(())
            })?;
            Ok(())
        })
        .unwrap();
        let elapsed = t0.elapsed();
        assert!(
            elapsed < Duration::from_millis(200),
            "expected overlap, elapsed={elapsed:?}"
        );
        assert_eq!(started.lock().unwrap().len(), 2);
        assert_eq!(state.history.len(), 2);
    }
}
