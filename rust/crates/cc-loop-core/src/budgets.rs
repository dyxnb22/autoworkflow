//! Execution budget checks.

use std::path::Path;

use crate::inspect::wall_clock_elapsed_seconds;
use crate::state::{AttemptPhase, TaskState};

pub fn count_consecutive_failures(state: &TaskState) -> u32 {
    let mut count = 0u32;
    for attempt in state.history.iter().rev() {
        if matches!(attempt.phase, AttemptPhase::Failed | AttemptPhase::Rejected)
            && (attempt.decision == "reject" || attempt.phase == AttemptPhase::Failed)
        {
            count += 1;
            continue;
        }
        if attempt.phase == AttemptPhase::Merged || attempt.decision == "approve" {
            break;
        }
    }
    count
}

pub fn budget_exhausted_message(state: &TaskState) -> Option<String> {
    let cfg = &state.config;
    if cfg.max_wall_clock_seconds > 0 {
        let elapsed = wall_clock_elapsed_seconds(state) as u64;
        if elapsed >= cfg.max_wall_clock_seconds {
            return Some(format!(
                "max_wall_clock_seconds exhausted ({elapsed} >= {})",
                cfg.max_wall_clock_seconds
            ));
        }
    }
    if cfg.max_consecutive_failures > 0 {
        let n = count_consecutive_failures(state);
        if n >= cfg.max_consecutive_failures {
            return Some(format!(
                "max_consecutive_failures exhausted ({n} >= {})",
                cfg.max_consecutive_failures
            ));
        }
    }
    if state.iteration >= cfg.max_iterations {
        return Some(format!(
            "max_iterations exhausted ({} >= {})",
            state.iteration, cfg.max_iterations
        ));
    }
    None
}

pub fn artifact_log_bytes(dir: &Path) -> u64 {
    if !dir.is_dir() {
        return 0;
    }
    let mut total = 0u64;
    if let Ok(walk) = std::fs::read_dir(dir) {
        for entry in walk.flatten() {
            let path = entry.path();
            if path.is_file() {
                if let Ok(meta) = path.metadata() {
                    total += meta.len();
                }
            } else if path.is_dir() {
                total += artifact_log_bytes(&path);
            }
        }
    }
    total
}
