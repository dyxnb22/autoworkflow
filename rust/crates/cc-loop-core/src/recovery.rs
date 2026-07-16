//! Auto-step recovery decisions.

use crate::state::{AttemptPhase, TaskState, TaskStatus};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutoStep {
    Done,
    Resume,
    Stop,
    Fail,
    Replan,
}

pub fn decide_auto_step(state: &TaskState) -> AutoStep {
    match state.status {
        TaskStatus::Done => AutoStep::Done,
        TaskStatus::Failed | TaskStatus::Cancelled => AutoStep::Fail,
        TaskStatus::Replanning => AutoStep::Replan,
        TaskStatus::Stopped | TaskStatus::Interrupted => {
            if let Some(attempt) = state.latest_attempt() {
                if attempt.phase == AttemptPhase::Rejected {
                    return AutoStep::Resume;
                }
                if attempt.phase == AttemptPhase::Approved && !state.config.auto_merge {
                    // Handoff ready — treat as done if graph complete
                    if state
                        .task_graph
                        .as_ref()
                        .map(|g| g.is_complete())
                        .unwrap_or(true)
                    {
                        return AutoStep::Done;
                    }
                }
                if attempt.test_status == "failed" || attempt.test_status == "timed_out" {
                    if state.config.auto_recover_tests
                        && attempt.retry < state.config.max_retries_per_step
                    {
                        return AutoStep::Resume;
                    }
                    return AutoStep::Fail;
                }
            }
            AutoStep::Stop
        }
        TaskStatus::Running | TaskStatus::Initialized | TaskStatus::WaitingManualReview => {
            AutoStep::Resume
        }
    }
}

pub fn derive_next_action_from_step(step: AutoStep, running: bool) -> String {
    if running {
        return "wait".into();
    }
    match step {
        AutoStep::Done => "none".into(),
        AutoStep::Resume => "resume".into(),
        AutoStep::Stop => "inspect".into(),
        AutoStep::Fail => "inspect".into(),
        AutoStep::Replan => "replan".into(),
    }
}
