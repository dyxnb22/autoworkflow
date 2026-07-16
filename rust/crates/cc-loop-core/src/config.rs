//! Task configuration defaults and role-identity helpers.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Default loop configuration (v0.11 / Rust 0.12 product defaults).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LoopConfig {
    pub max_iterations: u32,
    pub max_retries_per_step: u32,
    pub codex_timeout_seconds: u64,
    pub cursor_timeout_seconds: u64,
    pub claude_code_timeout_seconds: u64,
    pub test_timeout_seconds: u64,
    pub planner_provider: String,
    pub reviewer_provider: String,
    pub implementer_provider: String,
    #[serde(default)]
    pub codex_model: String,
    #[serde(default)]
    pub cursor_model: String,
    #[serde(default)]
    pub claude_code_model: String,
    pub base_branch: String,
    /// Default success = handoff; merging into base is opt-in.
    pub auto_merge: bool,
    pub allow_merge_without_tests: bool,
    /// Writer must not review their own work.
    pub require_distinct_reviewer: bool,
    pub max_review_patch_bytes: usize,
    #[serde(default)]
    pub cursor_force: bool,
    #[serde(default)]
    pub cursor_sandbox: String,
    #[serde(default)]
    pub test_command: Vec<String>,
    pub max_merge_retries: u32,
    pub max_merge_recovery_attempts: u32,
    pub max_recovery_attempts_per_iteration: u32,
    pub auto_recover_merge: bool,
    pub auto_recover_tests: bool,
    pub auto_recover_provider_errors: bool,
    pub recovery_retry_backoff_seconds: u64,
    pub max_wall_clock_seconds: u64,
    pub max_consecutive_failures: u32,
    pub max_artifact_log_bytes: usize,
    pub max_changed_files_per_attempt: u32,
    pub stale_heartbeat_seconds: u64,
    pub max_parallel_nodes: u32,
    pub allow_parallel_execution: bool,
    pub allow_node_policy_weakening: bool,
    /// Default path is a single closed loop.
    pub planner_granularity: String,
    pub planner_mode: String,
    pub auto_direct_planner: bool,
    pub auto_direct_max_goal_chars: usize,
    pub review_context_mode: String,
    pub review_inline_patch_threshold: usize,
    pub provider_watchdog_grace_seconds: u64,
    pub git_timeout_seconds: u64,
    /// Stop policy: `no_p0_p1` (default) | `no_p0_only` | `approve_only`.
    #[serde(default = "default_stop_policy")]
    pub stop_policy: String,
    /// Severities that block handoff (default P0,P1).
    #[serde(default = "default_blocking_severities_cfg")]
    pub blocking_severities: Vec<String>,
    /// Review facets to cover (empty → engine default set).
    #[serde(default)]
    pub review_facets: Vec<String>,
    /// `structured_single` (default) | `per_facet`.
    #[serde(default = "default_review_mode")]
    pub review_mode: String,
}

fn default_stop_policy() -> String {
    "no_p0_p1".into()
}
fn default_blocking_severities_cfg() -> Vec<String> {
    vec!["P0".into(), "P1".into()]
}
fn default_review_mode() -> String {
    "structured_single".into()
}

pub fn default_config() -> LoopConfig {
    LoopConfig {
        max_iterations: 10,
        max_retries_per_step: 2,
        codex_timeout_seconds: 300,
        cursor_timeout_seconds: 900,
        claude_code_timeout_seconds: 600,
        test_timeout_seconds: 600,
        planner_provider: "codex".into(),
        reviewer_provider: "codex".into(),
        implementer_provider: "cursor".into(),
        codex_model: String::new(),
        cursor_model: String::new(),
        claude_code_model: String::new(),
        base_branch: "main".into(),
        auto_merge: false,
        allow_merge_without_tests: false,
        require_distinct_reviewer: true,
        max_review_patch_bytes: 60_000,
        cursor_force: false,
        cursor_sandbox: String::new(),
        test_command: Vec::new(),
        max_merge_retries: 2,
        max_merge_recovery_attempts: 2,
        max_recovery_attempts_per_iteration: 3,
        auto_recover_merge: true,
        auto_recover_tests: true,
        auto_recover_provider_errors: true,
        recovery_retry_backoff_seconds: 0,
        max_wall_clock_seconds: 0,
        max_consecutive_failures: 0,
        max_artifact_log_bytes: 0,
        max_changed_files_per_attempt: 0,
        stale_heartbeat_seconds: 120,
        max_parallel_nodes: 1,
        allow_parallel_execution: false,
        allow_node_policy_weakening: false,
        planner_granularity: "single".into(),
        planner_mode: "auto".into(),
        auto_direct_planner: true,
        auto_direct_max_goal_chars: 500,
        review_context_mode: "hybrid".into(),
        review_inline_patch_threshold: 8000,
        provider_watchdog_grace_seconds: 5,
        git_timeout_seconds: 60,
        stop_policy: "no_p0_p1".into(),
        blocking_severities: vec!["P0".into(), "P1".into()],
        review_facets: Vec::new(),
        review_mode: "structured_single".into(),
    }
}

/// Merge optional JSON overrides onto defaults (legacy state compatibility).
pub fn merge_config(overrides: Option<&Value>) -> LoopConfig {
    let mut base = serde_json::to_value(default_config()).expect("default config serializes");
    if let Some(Value::Object(map)) = overrides {
        if let Value::Object(ref mut base_map) = base {
            for (k, v) in map {
                base_map.insert(k.clone(), v.clone());
            }
        }
    }
    serde_json::from_value(base).unwrap_or_else(|_| default_config())
}

/// Merge a typed partial overlay (CLI flags) onto defaults.
pub fn merge_config_partial(partial: &Map<String, Value>) -> LoopConfig {
    merge_config(Some(&Value::Object(partial.clone())))
}

pub fn resolve_provider_model(provider: &str, config: &LoopConfig) -> String {
    match provider.trim() {
        "codex" => config.codex_model.clone(),
        "cursor" => config.cursor_model.clone(),
        "claude-code" => config.claude_code_model.clone(),
        _ => String::new(),
    }
}

pub fn role_provider_identity(provider: &str, config: &LoopConfig) -> (String, String) {
    let name = provider.trim().to_string();
    let model = if name.is_empty() {
        String::new()
    } else {
        resolve_provider_model(&name, config)
    };
    (name, model)
}

pub fn distinct_reviewer_satisfied(
    config: &LoopConfig,
    providers: Option<&std::collections::HashMap<String, String>>,
) -> bool {
    let implementer = providers
        .and_then(|p| p.get("implementer"))
        .cloned()
        .unwrap_or_else(|| config.implementer_provider.clone());
    let reviewer = providers
        .and_then(|p| p.get("reviewer"))
        .cloned()
        .unwrap_or_else(|| config.reviewer_provider.clone());
    let impl_id = role_provider_identity(&implementer, config);
    let rev_id = role_provider_identity(&reviewer, config);
    if impl_id.0.is_empty() || rev_id.0.is_empty() {
        return false;
    }
    impl_id != rev_id
}

pub fn format_distinct_reviewer_error(
    config: &LoopConfig,
    providers: Option<&std::collections::HashMap<String, String>>,
) -> String {
    let implementer = providers
        .and_then(|p| p.get("implementer"))
        .cloned()
        .unwrap_or_else(|| config.implementer_provider.clone());
    let reviewer = providers
        .and_then(|p| p.get("reviewer"))
        .cloned()
        .unwrap_or_else(|| config.reviewer_provider.clone());
    let (impl_name, impl_model) = role_provider_identity(&implementer, config);
    let (rev_name, rev_model) = role_provider_identity(&reviewer, config);
    let impl_label = if impl_model.is_empty() {
        impl_name
    } else {
        format!("{impl_name} model={impl_model}")
    };
    let rev_label = if rev_model.is_empty() {
        rev_name
    } else {
        format!("{rev_name} model={rev_model}")
    };
    format!(
        "require_distinct_reviewer=true but implementer and reviewer are the same \
         ({impl_label} vs {rev_label}); configure a different reviewer provider (and/or model) \
         so the writer cannot review their own work"
    )
}

pub fn provider_timeout_seconds(provider: &str, config: &LoopConfig) -> u64 {
    match provider.trim() {
        "codex" => config.codex_timeout_seconds,
        "cursor" => config.cursor_timeout_seconds,
        "claude-code" => config.claude_code_timeout_seconds,
        _ => 600,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_product_contract() {
        let c = default_config();
        assert!(!c.auto_merge);
        assert!(c.require_distinct_reviewer);
        assert_eq!(c.planner_granularity, "single");
        assert!(!c.allow_merge_without_tests);
        assert_eq!(c.stop_policy, "no_p0_p1");
        assert_eq!(c.blocking_severities, vec!["P0".to_string(), "P1".to_string()]);
        assert_eq!(c.review_mode, "structured_single");
    }

    #[test]
    fn merge_fills_missing_keys() {
        let overrides = serde_json::json!({"max_iterations": 3});
        let c = merge_config(Some(&overrides));
        assert_eq!(c.max_iterations, 3);
        assert!(!c.auto_merge);
        assert!(c.require_distinct_reviewer);
    }

    #[test]
    fn distinct_reviewer_detects_same_identity() {
        let mut c = default_config();
        c.implementer_provider = "cursor".into();
        c.reviewer_provider = "cursor".into();
        assert!(!distinct_reviewer_satisfied(&c, None));
        c.reviewer_provider = "claude-code".into();
        assert!(distinct_reviewer_satisfied(&c, None));
    }
}
