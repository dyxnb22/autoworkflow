//! Repair prompt templates for recoverable failures.

use crate::prompt_cache::DYNAMIC_IMPLEMENTER_MARKER;

fn repair_scaffold(goal: &str, body: &str) -> String {
    format!(
        "## Stable Implementer Contract\n\
         You are the cc-loop implementer running a repair pass.\n\
         Make the minimal correct change. Do not push. Do not merge.\n\n\
         ## Task Implementer Context\n\
         Goal: {goal}\n\n\
         {DYNAMIC_IMPLEMENTER_MARKER}\n\
         {body}\n"
    )
}

pub fn test_failure_repair_prompt(goal: &str, test_output: &str, reject_or_fail: &str) -> String {
    repair_scaffold(
        goal,
        &format!(
            "Context: {reject_or_fail}\n\n\
             Recent test output (truncated):\n{}\n\n\
             Fix the implementation so the configured tests pass.",
            test_output.chars().take(4000).collect::<String>()
        ),
    )
}

pub fn reviewer_reject_repair_prompt(goal: &str, reason: &str, retry_prompt: &str) -> String {
    let extra = if retry_prompt.is_empty() {
        String::new()
    } else {
        format!("\nReviewer retry_prompt:\n{retry_prompt}\n")
    };
    repair_scaffold(
        goal,
        &format!(
            "Address the reviewer rejection and re-implement.\n\
             Reject reason: {reason}{extra}\n\
             Make the minimal correct change."
        ),
    )
}

pub fn merge_conflict_repair_prompt(goal: &str, merge_output: &str) -> String {
    repair_scaffold(
        goal,
        &format!(
            "Resolve merge conflicts.\nMerge output:\n{}\n\
             Prefer keeping the attempt-branch intent.",
            merge_output.chars().take(4000).collect::<String>()
        ),
    )
}

pub fn provider_error_repair_prompt(goal: &str, failure_type: &str) -> String {
    repair_scaffold(
        goal,
        &format!(
            "Retry implementation after provider failure ({failure_type}).\n\
             Continue from the last known good state. Minimal changes only."
        ),
    )
}
