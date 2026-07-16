//! Repair prompt templates for recoverable failures.

pub fn test_failure_repair_prompt(goal: &str, test_output: &str, reject_or_fail: &str) -> String {
    format!(
        "Repair the implementation so tests pass.\nGoal: {goal}\nContext: {reject_or_fail}\n\n\
         Recent test output (truncated):\n{}\n\n\
         Make the minimal fix. Do not push or merge.",
        test_output.chars().take(4000).collect::<String>()
    )
}

pub fn reviewer_reject_repair_prompt(goal: &str, reason: &str, retry_prompt: &str) -> String {
    let extra = if retry_prompt.is_empty() {
        String::new()
    } else {
        format!("\nReviewer retry_prompt:\n{retry_prompt}\n")
    };
    format!(
        "Address the reviewer rejection and re-implement.\nGoal: {goal}\n\
         Reject reason: {reason}{extra}\nMake the minimal correct change."
    )
}

pub fn merge_conflict_repair_prompt(goal: &str, merge_output: &str) -> String {
    format!(
        "Resolve merge conflicts for goal: {goal}\nMerge output:\n{}\n\
         Prefer keeping the attempt-branch intent. Do not push.",
        merge_output.chars().take(4000).collect::<String>()
    )
}

pub fn provider_error_repair_prompt(goal: &str, failure_type: &str) -> String {
    format!(
        "Retry implementation after provider failure ({failure_type}).\nGoal: {goal}\n\
         Continue from the last known good state. Minimal changes only."
    )
}
