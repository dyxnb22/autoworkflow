//! Reviewer prompt context mode (hybrid / artifact_refs / inline).

use crate::config::LoopConfig;
use crate::prompt_cache::DYNAMIC_REVIEW_MARKER;

#[derive(Debug, Clone)]
pub struct ReviewPayload {
    pub prompt: String,
    pub context_mode: String,
    pub inline_patch: bool,
    pub omitted_patch_chars: usize,
}

pub fn build_reviewer_prompt(
    goal: &str,
    plan_text: &str,
    diff_stat: &str,
    patch: &str,
    config: &LoopConfig,
    diff_stat_path: &str,
    diff_files_path: &str,
) -> ReviewPayload {
    let max = config.max_review_patch_bytes;
    let threshold = config.review_inline_patch_threshold;
    let mode = config.review_context_mode.as_str();

    let use_refs = mode == "artifact_refs" || (mode == "hybrid" && patch.len() > threshold);

    let (patch_section, inline, omitted) = if use_refs {
        let omitted = patch.len();
        (
            format!(
                "### Diff stat summary\n{}\n\n\
                 ### Patch artifact references\n\
                 Patch omitted (artifact refs mode).\n\
                 diff_stat_path: {diff_stat_path}\n\
                 diff_files_path: {diff_files_path}\n",
                diff_stat.lines().take(20).collect::<Vec<_>>().join("\n")
            ),
            false,
            omitted,
        )
    } else if patch.len() > max {
        let omitted = patch.len() - max;
        (
            format!(
                "### Diff stat\n{diff_stat}\n\n### Selected patches\n{}\n\n...[truncated {omitted} bytes]...",
                &patch[..max]
            ),
            true,
            omitted,
        )
    } else {
        (
            format!("### Diff stat\n{diff_stat}\n\n### Selected patches\n{patch}\n"),
            true,
            0,
        )
    };

    let prompt = format!(
        r#"## Stable Review Contract
You are the reviewer. You did NOT write this code. Gate on tests already ran.
Return ONLY JSON:
{{"decision":"approve"|"reject"|"stop"|"replan","reason":"...","retry_prompt":"...","issues":[]}}

## Task Review Context
Goal: {goal}
Plan:
{plan_text}

{DYNAMIC_REVIEW_MARKER}
{patch_section}
"#
    );

    ReviewPayload {
        prompt,
        context_mode: if use_refs {
            "artifact_refs".into()
        } else {
            mode.to_string()
        },
        inline_patch: inline,
        omitted_patch_chars: omitted,
    }
}
