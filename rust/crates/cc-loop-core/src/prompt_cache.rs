//! Prompt cache budget artifact (parity with Python prompt_cache.py).

use std::fs;
use std::path::Path;

use serde_json::{json, Map, Value};

use crate::error::Result;
use crate::state::{atomic_write_text, utc_now_iso};

pub const PROMPT_CACHE_SCHEMA_VERSION: u32 = 1;
pub const DYNAMIC_PLANNER_MARKER: &str = "## Dynamic Planner Payload";
pub const DYNAMIC_IMPLEMENTER_MARKER: &str = "## Dynamic Implementer Payload";
pub const DYNAMIC_REVIEW_MARKER: &str = "## Dynamic Review Payload";

pub fn estimated_tokens_from_chars(char_count: usize) -> usize {
    if char_count == 0 {
        0
    } else {
        (char_count + 3) / 4
    }
}

fn classify_cache_health(prefix_ratio: f64) -> &'static str {
    if prefix_ratio >= 0.60 {
        "good"
    } else if prefix_ratio >= 0.40 {
        "warning"
    } else {
        "poor"
    }
}

fn split_prompt_at_marker(prompt: &str, marker: &str) -> (usize, usize) {
    match prompt.find(marker) {
        Some(idx) => (idx, prompt.len() - idx),
        None => (prompt.len(), 0),
    }
}

fn estimate_evidence_chars(prompt: &str, marker: &str) -> usize {
    let Some(start) = prompt.find(marker) else {
        return 0;
    };
    let dynamic = &prompt[start..];
    let mut evidence = 0usize;
    for section in [
        "### Diff stat",
        "### Diff stat summary",
        "### Selected patches",
        "### Patch artifact references",
        "Diff stat:",
        "Patch omitted",
    ] {
        if let Some(sec_start) = dynamic.find(section) {
            let after = &dynamic[sec_start..];
            let next = after[section.len()..]
                .find("\n### ")
                .map(|i| section.len() + i)
                .unwrap_or(after.len());
            evidence += next;
        }
    }
    evidence
}

pub fn phase_layout_metrics(prompt: &str, marker: &str, layout: &str) -> Value {
    let (stable_prefix_chars, dynamic_payload_chars) = split_prompt_at_marker(prompt, marker);
    let prompt_chars = prompt.len();
    let stable_prefix_ratio = if prompt_chars == 0 {
        0.0
    } else {
        (stable_prefix_chars as f64) / (prompt_chars as f64)
    };
    let evidence = estimate_evidence_chars(prompt, marker);
    let contract_denominator =
        stable_prefix_chars + dynamic_payload_chars.saturating_sub(evidence);
    let contract_prefix_ratio = if contract_denominator == 0 {
        0.0
    } else {
        (stable_prefix_chars as f64) / (contract_denominator as f64)
    };
    json!({
        "layout": layout,
        "prompt_chars": prompt_chars,
        "stable_prefix_chars": stable_prefix_chars,
        "dynamic_payload_chars": dynamic_payload_chars,
        "stable_prefix_ratio": (stable_prefix_ratio * 1_000_000.0).round() / 1_000_000.0,
        "contract_prefix_ratio": (contract_prefix_ratio * 1_000_000.0).round() / 1_000_000.0,
        "cache_health": classify_cache_health(contract_prefix_ratio),
        "total_prompt_cache_health": classify_cache_health(stable_prefix_ratio),
        "estimated_prompt_tokens": estimated_tokens_from_chars(prompt_chars),
        "estimated_provider_prompt_tokens": estimated_tokens_from_chars(prompt_chars),
        "estimated_stable_prefix_tokens": estimated_tokens_from_chars(stable_prefix_chars),
        "estimated_dynamic_payload_tokens": estimated_tokens_from_chars(dynamic_payload_chars),
        "estimated_provider_dynamic_payload_tokens": estimated_tokens_from_chars(dynamic_payload_chars),
    })
}

pub fn build_planner_phase_cache(
    prompt: &str,
    skipped: bool,
    planner_mode_resolved: &str,
    planner_direct_reason: &str,
) -> Value {
    let mut metrics = phase_layout_metrics(prompt, DYNAMIC_PLANNER_MARKER, "stable-prefix-v1");
    if let Some(obj) = metrics.as_object_mut() {
        obj.insert("skipped".into(), json!(skipped));
        obj.insert("planner_mode_resolved".into(), json!(planner_mode_resolved));
        obj.insert("planner_direct_reason".into(), json!(planner_direct_reason));
        obj.insert("provider_skipped".into(), json!(skipped));
        let mut recommendations = Vec::new();
        if skipped {
            obj.insert("estimated_provider_prompt_tokens".into(), json!(0));
            obj.insert("estimated_provider_dynamic_payload_tokens".into(), json!(0));
            let note = if planner_direct_reason.is_empty() {
                "planner_mode=direct"
            } else {
                planner_direct_reason
            };
            recommendations.push(format!("Planner provider skipped ({note})."));
        } else if obj.get("total_prompt_cache_health").and_then(|v| v.as_str()) != Some("good")
        {
            recommendations.push(
                "Keep planner stable contract before Dynamic Planner Payload marker.".into(),
            );
        }
        obj.insert("recommendations".into(), json!(recommendations));
    }
    metrics
}

pub fn build_implementer_phase_cache(prompt: &str) -> Value {
    let mut metrics = phase_layout_metrics(prompt, DYNAMIC_IMPLEMENTER_MARKER, "stable-prefix-v1");
    if let Some(obj) = metrics.as_object_mut() {
        let mut recommendations: Vec<String> = Vec::new();
        if obj.get("total_prompt_cache_health").and_then(|v| v.as_str()) != Some("good") {
            recommendations.push(
                "Keep implementer stable contract before Dynamic Implementer Payload marker."
                    .into(),
            );
        }
        obj.insert("recommendations".into(), json!(recommendations));
    }
    metrics
}

pub fn build_reviewer_phase_cache(
    prompt: &str,
    context_mode: &str,
    inline_patch: bool,
    omitted_patch_chars: usize,
) -> Value {
    let mut metrics = phase_layout_metrics(prompt, DYNAMIC_REVIEW_MARKER, "stable_contract_task_dynamic");
    if let Some(obj) = metrics.as_object_mut() {
        obj.insert("context_mode".into(), json!(context_mode));
        obj.insert("inline_patch".into(), json!(inline_patch));
        obj.insert("omitted_patch_chars".into(), json!(omitted_patch_chars));
        let avoidable = if !inline_patch {
            estimated_tokens_from_chars(omitted_patch_chars)
        } else {
            0
        };
        obj.insert("estimated_avoidable_miss_tokens".into(), json!(avoidable));
        let mut recommendations: Vec<String> = Vec::new();
        if matches!(context_mode, "artifact_refs" | "hybrid") && !inline_patch {
            recommendations.push(
                "Patch content omitted from reviewer prompt; inspect artifact paths or worktree git diff.".into(),
            );
        }
        if avoidable > 0 {
            recommendations.push(
                "Large patch omitted from inline prompt; cache miss tokens reduced via artifact refs.".into(),
            );
        }
        if obj.get("cache_health").and_then(|v| v.as_str()) != Some("good") {
            recommendations
                .push("Increase stable reviewer prefix ratio before dynamic payload.".into());
        }
        obj.insert("recommendations".into(), json!(recommendations));
    }
    metrics
}

fn compute_totals(phases: &Map<String, Value>) -> Value {
    let mut estimated_prompt_tokens = 0i64;
    let mut estimated_provider_prompt_tokens = 0i64;
    let mut estimated_dynamic_payload_tokens = 0i64;
    let mut estimated_provider_dynamic_payload_tokens = 0i64;
    let mut estimated_avoidable_miss_tokens = 0i64;
    for phase_data in phases.values() {
        if let Some(obj) = phase_data.as_object() {
            estimated_prompt_tokens += obj
                .get("estimated_prompt_tokens")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            estimated_provider_prompt_tokens += obj
                .get("estimated_provider_prompt_tokens")
                .or_else(|| obj.get("estimated_prompt_tokens"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            estimated_dynamic_payload_tokens += obj
                .get("estimated_dynamic_payload_tokens")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            estimated_provider_dynamic_payload_tokens += obj
                .get("estimated_provider_dynamic_payload_tokens")
                .or_else(|| obj.get("estimated_dynamic_payload_tokens"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            estimated_avoidable_miss_tokens += obj
                .get("estimated_avoidable_miss_tokens")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
        }
    }
    json!({
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "estimated_provider_prompt_tokens": estimated_provider_prompt_tokens,
        "estimated_dynamic_payload_tokens": estimated_dynamic_payload_tokens,
        "estimated_provider_dynamic_payload_tokens": estimated_provider_dynamic_payload_tokens,
        "estimated_avoidable_miss_tokens": estimated_avoidable_miss_tokens,
    })
}

pub fn load_prompt_cache(path: &Path) -> Value {
    if !path.is_file() {
        return json!({
            "schema_version": PROMPT_CACHE_SCHEMA_VERSION,
            "phases": {},
            "totals": {},
        });
    }
    fs::read_to_string(path)
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_else(|| {
            json!({
                "schema_version": PROMPT_CACHE_SCHEMA_VERSION,
                "phases": {},
                "totals": {},
            })
        })
}

pub fn update_prompt_cache_artifact(cache_path: &Path, phase: &str, phase_data: Value) -> Result<()> {
    let mut payload = load_prompt_cache(cache_path);
    let phases = payload
        .as_object_mut()
        .map(|o| o.entry("phases".to_string()).or_insert_with(|| json!({})))
        .and_then(|v| v.as_object_mut());
    if let Some(phases) = phases {
        phases.insert(phase.to_string(), phase_data);
        let totals = compute_totals(phases);
        if let Some(root) = payload.as_object_mut() {
            root.insert("schema_version".into(), json!(PROMPT_CACHE_SCHEMA_VERSION));
            root.insert("updated_at".into(), json!(utc_now_iso()));
            root.insert("totals".into(), totals);
        }
    }
    atomic_write_text(
        cache_path,
        &(serde_json::to_string_pretty(&payload)? + "\n"),
    )?;
    Ok(())
}

pub fn prompt_cache_snapshot(cache_path: &Path) -> Option<Value> {
    let payload = load_prompt_cache(cache_path);
    let totals = payload.get("totals").cloned().unwrap_or(json!({}));
    let phases = payload.get("phases").cloned().unwrap_or(json!({}));
    if totals.as_object().map(|o| o.is_empty()).unwrap_or(true)
        && phases.as_object().map(|o| o.is_empty()).unwrap_or(true)
    {
        return None;
    }
    let reviewer = phases.get("reviewer").cloned().unwrap_or(json!({}));
    Some(json!({
        "path": cache_path.display().to_string(),
        "estimated_prompt_tokens": totals.get("estimated_prompt_tokens").cloned().unwrap_or(json!(0)),
        "estimated_provider_prompt_tokens": totals.get("estimated_provider_prompt_tokens").cloned().unwrap_or(json!(0)),
        "estimated_dynamic_payload_tokens": totals.get("estimated_dynamic_payload_tokens").cloned().unwrap_or(json!(0)),
        "estimated_provider_dynamic_payload_tokens": totals.get("estimated_provider_dynamic_payload_tokens").cloned().unwrap_or(json!(0)),
        "estimated_avoidable_miss_tokens": totals.get("estimated_avoidable_miss_tokens").cloned().unwrap_or(json!(0)),
        "reviewer_context_mode": reviewer.get("context_mode"),
        "reviewer_inline_patch": reviewer.get("inline_patch"),
        "reviewer_omitted_patch_chars": reviewer.get("omitted_patch_chars").cloned().unwrap_or(json!(0)),
    }))
}

pub fn write_review_prompt_metrics_from_phase(artifact_root: &Path, phase: &Value) -> Result<()> {
    atomic_write_text(
        &artifact_root.join("review.prompt.metrics.json"),
        &(serde_json::to_string_pretty(phase)? + "\n"),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_health_from_prefix() {
        let prompt = format!(
            "{}{}\n{}",
            "S".repeat(700),
            DYNAMIC_REVIEW_MARKER,
            "D".repeat(300)
        );
        let m = phase_layout_metrics(&prompt, DYNAMIC_REVIEW_MARKER, "x");
        assert!(m["stable_prefix_ratio"].as_f64().unwrap() >= 0.6);
        assert_eq!(m["total_prompt_cache_health"], "good");
    }

    #[test]
    fn update_accumulates_totals() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("prompt.cache.json");
        update_prompt_cache_artifact(
            &path,
            "planner",
            build_planner_phase_cache("hello", true, "direct", "short"),
        )
        .unwrap();
        update_prompt_cache_artifact(
            &path,
            "reviewer",
            build_reviewer_phase_cache(
                &format!("stable\n{DYNAMIC_REVIEW_MARKER}\npatch"),
                "hybrid",
                false,
                100,
            ),
        )
        .unwrap();
        let snap = prompt_cache_snapshot(&path).unwrap();
        assert!(snap["estimated_prompt_tokens"].as_i64().unwrap() > 0);
        assert_eq!(snap["reviewer_context_mode"], "hybrid");
    }
}
