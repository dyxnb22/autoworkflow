//! Quality loop: severity gates, stop policy, review facets.

use serde_json::{json, Map, Value};

use crate::config::LoopConfig;
use crate::state::AttemptRecord;

pub const DEFAULT_FACETS: &[&str] = &[
    "correctness",
    "tests",
    "security",
    "reliability",
    "maintainability",
    "ux_cli",
];

pub const DEFAULT_BLOCKING: &[&str] = &["P0", "P1"];

/// Normalize severity labels to P0–P3.
pub fn normalize_severity(raw: &str) -> String {
    let s = raw.trim().to_uppercase();
    match s.as_str() {
        "P0" | "BLOCKER" | "CRITICAL" | "0" => "P0".into(),
        "P1" | "HIGH" | "1" => "P1".into(),
        "P2" | "MEDIUM" | "2" => "P2".into(),
        "P3" | "LOW" | "NIT" | "3" => "P3".into(),
        _ if s.starts_with('P') && s.len() == 2 => s,
        _ => String::new(),
    }
}

pub fn default_review_facets() -> Vec<String> {
    DEFAULT_FACETS.iter().map(|s| (*s).to_string()).collect()
}

pub fn default_blocking_severities() -> Vec<String> {
    DEFAULT_BLOCKING.iter().map(|s| (*s).to_string()).collect()
}

pub fn resolve_review_facets(config: &LoopConfig) -> Vec<String> {
    if config.review_facets.is_empty() {
        default_review_facets()
    } else {
        config.review_facets.clone()
    }
}

pub fn resolve_blocking_severities(config: &LoopConfig) -> Vec<String> {
    if config.blocking_severities.is_empty() {
        default_blocking_severities()
    } else {
        config
            .blocking_severities
            .iter()
            .map(|s| normalize_severity(s))
            .filter(|s| !s.is_empty())
            .collect()
    }
}

fn issue_is_blocking(issue: &Value, blocking: &[String]) -> bool {
    if issue.get("blocking").and_then(|v| v.as_bool()) == Some(true) {
        return true;
    }
    let sev = issue
        .get("severity")
        .and_then(|v| v.as_str())
        .map(normalize_severity)
        .unwrap_or_default();
    !sev.is_empty() && blocking.iter().any(|b| b == &sev)
}

pub fn count_severities(issues: &[Value]) -> Map<String, Value> {
    let mut counts = Map::new();
    for key in ["P0", "P1", "P2", "P3"] {
        counts.insert(key.into(), json!(0u64));
    }
    for issue in issues {
        let sev = issue
            .get("severity")
            .and_then(|v| v.as_str())
            .map(normalize_severity)
            .unwrap_or_default();
        if sev.is_empty() {
            continue;
        }
        if let Some(Value::Number(n)) = counts.get(&sev) {
            let v = n.as_u64().unwrap_or(0) + 1;
            counts.insert(sev, json!(v));
        }
    }
    counts
}

pub fn open_blocking_issues(issues: &[Value], blocking: &[String]) -> Vec<Value> {
    issues
        .iter()
        .filter(|i| issue_is_blocking(i, blocking))
        .cloned()
        .collect()
}

fn issues_from_review(review: &Value) -> Vec<Value> {
    review
        .get("issues")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default()
}

/// Normalize issues (severity/facet/blocking) and enforce stop policy on decision.
pub fn apply_quality_gate(review: &Value, config: &LoopConfig) -> Value {
    let mut out = review.clone();
    let blocking = resolve_blocking_severities(config);
    let facets = resolve_review_facets(config);
    let mut issues = issues_from_review(&out);
    for issue in &mut issues {
        if let Some(obj) = issue.as_object_mut() {
            if let Some(sev) = obj.get("severity").and_then(|v| v.as_str()) {
                let n = normalize_severity(sev);
                if !n.is_empty() {
                    obj.insert("severity".into(), json!(n));
                }
            }
            if obj.get("facet").and_then(|v| v.as_str()).unwrap_or("").is_empty() {
                obj.insert("facet".into(), json!("correctness"));
            }
            let sev = obj
                .get("severity")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let is_block = blocking.iter().any(|b| b == &sev)
                || obj.get("blocking").and_then(|v| v.as_bool()) == Some(true);
            obj.insert("blocking".into(), json!(is_block));
        }
    }
    let counts = count_severities(&issues);
    let open = open_blocking_issues(&issues, &blocking);
    let covered = out
        .get("facets_covered")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(|s| s.to_string()))
                .collect::<Vec<_>>()
        })
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| facets.clone());

    if let Some(obj) = out.as_object_mut() {
        obj.insert("issues".into(), json!(issues));
        obj.insert("blocking_counts".into(), Value::Object(counts.clone()));
        obj.insert("facets_covered".into(), json!(covered));
    }

    let policy = config.stop_policy.trim().to_lowercase();
    let decision = out
        .get("decision")
        .and_then(|v| v.as_str())
        .unwrap_or("stop")
        .to_lowercase();

    let must_block = match policy.as_str() {
        "approve_only" => false,
        "no_p0_only" => open.iter().any(|i| {
            normalize_severity(i.get("severity").and_then(|v| v.as_str()).unwrap_or("")) == "P0"
        }),
        // default: no_p0_p1
        _ => !open.is_empty(),
    };

    if must_block && decision == "approve" {
        if let Some(obj) = out.as_object_mut() {
            obj.insert("decision".into(), json!("reject"));
            obj.insert(
                "quality_override".into(),
                json!({
                    "from": "approve",
                    "to": "reject",
                    "reason": format!(
                        "stop_policy={} blocked by {} open blocking issue(s)",
                        config.stop_policy,
                        open.len()
                    ),
                }),
            );
            if obj
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .is_empty()
            {
                obj.insert(
                    "reason".into(),
                    json!(format!(
                        "quality gate: {} blocking issue(s) under {}",
                        open.len(),
                        config.stop_policy
                    )),
                );
            }
            if obj
                .get("retry_prompt")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .is_empty()
            {
                let titles: Vec<String> = open
                    .iter()
                    .filter_map(|i| {
                        i.get("title")
                            .or_else(|| i.get("detail"))
                            .and_then(|v| v.as_str())
                            .map(|s| s.chars().take(120).collect::<String>())
                    })
                    .collect();
                obj.insert(
                    "retry_prompt".into(),
                    json!(format!(
                        "Fix blocking review findings ({:?}): {}",
                        blocking,
                        titles.join("; ")
                    )),
                );
            }
        }
    }

    // Recompute after possible mutation
    let issues = issues_from_review(&out);
    let counts = count_severities(&issues);
    if let Some(obj) = out.as_object_mut() {
        obj.insert("blocking_counts".into(), Value::Object(counts));
    }
    out
}

pub fn stop_conditions_met(review: &Value, config: &LoopConfig, test_status: &str) -> bool {
    if test_status != "passed" && !(config.allow_merge_without_tests && test_status == "skipped") {
        return false;
    }
    let decision = review
        .get("decision")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_lowercase();
    if decision != "approve" {
        return false;
    }
    let blocking = resolve_blocking_severities(config);
    let issues = issues_from_review(review);
    let open = open_blocking_issues(&issues, &blocking);
    match config.stop_policy.trim().to_lowercase().as_str() {
        "approve_only" => true,
        "no_p0_only" => !open.iter().any(|i| {
            normalize_severity(i.get("severity").and_then(|v| v.as_str()).unwrap_or("")) == "P0"
        }),
        _ => open.is_empty(),
    }
}

pub fn build_quality_snapshot(config: &LoopConfig, attempt: Option<&AttemptRecord>) -> Value {
    let review = attempt
        .and_then(|a| a.review_json.clone())
        .unwrap_or(json!({}));
    let blocking = resolve_blocking_severities(config);
    let issues = issues_from_review(&review);
    let counts = review
        .get("blocking_counts")
        .cloned()
        .unwrap_or_else(|| Value::Object(count_severities(&issues)));
    let open = open_blocking_issues(&issues, &blocking);
    let facets = review
        .get("facets_covered")
        .cloned()
        .unwrap_or_else(|| json!(resolve_review_facets(config)));
    json!({
        "stop_policy": config.stop_policy,
        "blocking_severities": blocking,
        "review_mode": config.review_mode,
        "review_facets": resolve_review_facets(config),
        "blocking_counts": counts,
        "open_blocking_issues": open,
        "facets_covered": facets,
        "quality_override": review.get("quality_override").cloned().unwrap_or(Value::Null),
    })
}

/// Merge multiple facet review JSON objects into one.
pub fn merge_facet_reviews(parts: &[Value], facets: &[String]) -> Value {
    let mut issues = Vec::new();
    let mut reasons = Vec::new();
    let mut retry_bits = Vec::new();
    let mut any_reject = false;
    let mut any_stop = false;
    for (idx, part) in parts.iter().enumerate() {
        let facet = facets.get(idx).cloned().unwrap_or_else(|| "correctness".into());
        if let Some(arr) = part.get("issues").and_then(|v| v.as_array()) {
            for issue in arr {
                let mut issue = issue.clone();
                if let Some(obj) = issue.as_object_mut() {
                    if obj.get("facet").and_then(|v| v.as_str()).unwrap_or("").is_empty() {
                        obj.insert("facet".into(), json!(facet));
                    }
                }
                issues.push(issue);
            }
        }
        if let Some(r) = part.get("reason").and_then(|v| v.as_str()) {
            if !r.is_empty() {
                reasons.push(format!("{facet}: {r}"));
            }
        }
        if let Some(r) = part.get("retry_prompt").and_then(|v| v.as_str()) {
            if !r.is_empty() {
                retry_bits.push(r.to_string());
            }
        }
        match part.get("decision").and_then(|v| v.as_str()).unwrap_or("") {
            "reject" => any_reject = true,
            "stop" => any_stop = true,
            _ => {}
        }
    }
    let decision = if any_stop {
        "stop"
    } else if any_reject {
        "reject"
    } else {
        "approve"
    };
    json!({
        "decision": decision,
        "reason": reasons.join(" | "),
        "retry_prompt": retry_bits.join("\n"),
        "issues": issues,
        "facets_covered": facets,
    })
}

pub fn facet_review_prompt_suffix(facet: &str) -> String {
    format!(
        "\n## Facet focus (this pass only)\n\
         Evaluate ONLY facet `{facet}`.\n\
         Still return full JSON with issues tagged facet=\"{facet}\".\n"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::default_config;

    #[test]
    fn force_reject_on_p0_even_if_approve() {
        let cfg = default_config();
        let raw = json!({
            "decision": "approve",
            "reason": "looks fine",
            "issues": [{"severity": "P0", "title": "broken", "facet": "correctness"}]
        });
        let gated = apply_quality_gate(&raw, &cfg);
        assert_eq!(gated["decision"], "reject");
        assert!(gated.get("quality_override").is_some());
        assert_eq!(gated["blocking_counts"]["P0"], 1);
    }

    #[test]
    fn p2_does_not_block_under_default_policy() {
        let cfg = default_config();
        let raw = json!({
            "decision": "approve",
            "reason": "nits only",
            "issues": [{"severity": "P2", "title": "style", "facet": "maintainability"}]
        });
        let gated = apply_quality_gate(&raw, &cfg);
        assert_eq!(gated["decision"], "approve");
        assert!(stop_conditions_met(&gated, &cfg, "passed"));
    }

    #[test]
    fn approve_only_ignores_severity() {
        let mut cfg = default_config();
        cfg.stop_policy = "approve_only".into();
        let raw = json!({
            "decision": "approve",
            "issues": [{"severity": "P0", "title": "x"}]
        });
        let gated = apply_quality_gate(&raw, &cfg);
        assert_eq!(gated["decision"], "approve");
    }
}
