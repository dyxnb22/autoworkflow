//! Auto-step recovery decisions.

use crate::state::{AttemptPhase, TaskState, TaskStatus};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutoStep {
    Done,
    Resume,
    Stop,
    Fail,
    Replan,
    Repair,
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
                if attempt.phase == AttemptPhase::Approved
                    && !state.config.auto_merge
                    && state
                        .task_graph
                        .as_ref()
                        .map(|g| g.is_complete())
                        .unwrap_or(true)
                {
                    return AutoStep::Done;
                }
                if attempt.test_status == "skipped" {
                    // Missing test_command is not an implementer repair — configure tests.
                    return AutoStep::Stop;
                }
                if attempt.test_status == "failed" || attempt.test_status == "timed_out" {
                    if state.config.auto_recover_tests
                        && attempt.retry < state.config.max_retries_per_step
                    {
                        return AutoStep::Repair;
                    }
                    return AutoStep::Fail;
                }
                if !attempt.failure_type.is_empty()
                    && state.config.auto_recover_provider_errors
                    && attempt.recovery_retry_count < state.config.max_recovery_attempts_per_iteration
                {
                    return AutoStep::Repair;
                }
                if !attempt.merge_error.is_empty() && state.config.auto_recover_merge {
                    return AutoStep::Repair;
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
        return "none".into();
    }
    match step {
        AutoStep::Done => "done".into(),
        AutoStep::Resume => "resume".into(),
        AutoStep::Stop => "inspect".into(),
        AutoStep::Fail => "failed".into(),
        AutoStep::Replan => "resume".into(),
        AutoStep::Repair => "repair".into(),
    }
}
