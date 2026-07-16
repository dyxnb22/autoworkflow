//! Reviewer prompt context mode (hybrid / artifact_refs / inline).

use crate::config::LoopConfig;
use crate::prompt_cache::DYNAMIC_REVIEW_MARKER;
use crate::quality::{resolve_review_facets, DEFAULT_FACETS};

#[derive(Debug, Clone)]
pub struct ReviewPayload {
    pub prompt: String,
    pub context_mode: String,
    pub inline_patch: bool,
    pub omitted_patch_chars: usize,
}

fn facet_checklist(facets: &[String]) -> String {
    let lines: Vec<String> = facets
        .iter()
        .map(|f| {
            let hint = match f.as_str() {
                "correctness" => "meets goal/plan; logic correct",
                "tests" => "meaningful coverage; no false green",
                "security" => "secrets, auth, injection, unsafe defaults",
                "reliability" => "error handling, data loss, timeouts",
                "maintainability" => "clarity, coupling, API boundaries",
                "ux_cli" => "user-visible / CLI contract breakage",
                _ => "review this facet carefully",
            };
            format!("- `{f}`: {hint}")
        })
        .collect();
    lines.join("\n")
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
    build_reviewer_prompt_for_facet(
        goal,
        plan_text,
        diff_stat,
        patch,
        config,
        diff_stat_path,
        diff_files_path,
        None,
    )
}

#[allow(clippy::too_many_arguments)]
pub fn build_reviewer_prompt_for_facet(
    goal: &str,
    plan_text: &str,
    diff_stat: &str,
    patch: &str,
    config: &LoopConfig,
    diff_stat_path: &str,
    diff_files_path: &str,
    facet_focus: Option<&str>,
) -> ReviewPayload {
    let max = config.max_review_patch_bytes;
    let threshold = config.review_inline_patch_threshold;
    let mode = config.review_context_mode.as_str();
    let facets = resolve_review_facets(config);
    let checklist = facet_checklist(&facets);

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

    let facet_extra = if let Some(f) = facet_focus {
        format!(
            "\n## Facet focus (this pass only)\n\
             Evaluate ONLY facet `{f}`.\n\
             Tag every issue with \"facet\":\"{f}\".\n\
             Still return the full JSON schema.\n"
        )
    } else {
        String::new()
    };

    let facets_json = serde_json::to_string(&facets).unwrap_or_else(|_| {
        format!("{:?}", DEFAULT_FACETS)
    });

    let prompt = format!(
        r#"## Stable Review Contract
You are the reviewer. You did NOT write this code. Gate on tests already ran.
You MUST return ONLY JSON (no markdown fence) with this schema:
{{
  "decision":"approve"|"reject"|"stop"|"replan",
  "reason":"one-line summary",
  "retry_prompt":"concrete fix guidance for implementer",
  "issues":[
    {{"id":"issue-1","facet":"correctness","severity":"P0"|"P1"|"P2"|"P3","title":"...","detail":"...","blocking":true}}
  ],
  "facets_covered":{facets_json},
  "blocking_counts":{{"P0":0,"P1":0,"P2":0,"P3":0}}
}}

Severity rules (engine-enforced):
- P0/P1 = blocking delivery → you MUST decision=reject when any P0/P1 exists
- P2/P3 = non-blocking; may still approve if no P0/P1
- Prefer explicit "blocking": true for P0/P1

## Facet checklist (cover each)
{checklist}
{facet_extra}
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
