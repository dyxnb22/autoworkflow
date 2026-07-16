//! Offline fake provider for deterministic loop tests.
//!
//! Enabled when `CC_LOOP_FAKE_PROVIDERS=1` (or provider name `fake`).

use std::cell::Cell;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::config::LoopConfig;
use crate::error::Result;
use crate::provider::{ProviderAdapter, ProviderRunResult};

thread_local! {
    /// How many reviewer calls should return `reject` before approving (per test thread).
    static REJECTS_REMAINING: Cell<u32> = const { Cell::new(0) };
    /// How many reviewer calls should return approve+P0 (engine must force reject).
    static P0_APPROVE_REMAINING: Cell<u32> = const { Cell::new(0) };
}

/// Script the next N fake reviewer decisions as reject (then approve).
pub fn set_fake_reviewer_rejects(n: u32) {
    REJECTS_REMAINING.with(|c| c.set(n));
}

pub fn fake_reviewer_rejects_remaining() -> u32 {
    REJECTS_REMAINING.with(|c| c.get())
}

/// Script N fake reviews that claim approve but include a P0 issue.
pub fn set_fake_reviewer_p0_approves(n: u32) {
    P0_APPROVE_REMAINING.with(|c| c.set(n));
}

fn next_reviewer_decision() -> Value {
    // P0-with-approve takes priority for quality-gate tests.
    let p0_left = P0_APPROVE_REMAINING.with(|c| c.get());
    if p0_left > 0 {
        P0_APPROVE_REMAINING.with(|c| c.set(p0_left - 1));
        return json!({
            "decision": "approve",
            "reason": "fake wrongly approved despite P0",
            "retry_prompt": "Fix the P0 finding.",
            "issues": [{
                "id": "fake-p0",
                "facet": "correctness",
                "severity": "P0",
                "title": "fake P0 defect",
                "detail": "injected for quality gate test",
                "blocking": true
            }],
            "facets_covered": ["correctness","tests","security","reliability","maintainability","ux_cli"],
            "blocking_counts": {"P0": 1, "P1": 0, "P2": 0, "P3": 0}
        });
    }

    REJECTS_REMAINING.with(|c| {
        let left = c.get();
        if left > 0 {
            c.set(left - 1);
            json!({
                "decision": "reject",
                "reason": "fake reject: needs another pass",
                "retry_prompt": "Address the fake reviewer rejection.",
                "issues": [{
                    "id": "fake-p1",
                    "facet": "correctness",
                    "severity": "P1",
                    "title": "fake issue",
                    "detail": "needs fix",
                    "blocking": true
                }],
                "facets_covered": ["correctness","tests","security","reliability","maintainability","ux_cli"],
                "blocking_counts": {"P0": 0, "P1": 1, "P2": 0, "P3": 0}
            })
        } else {
            json!({
                "decision": "approve",
                "reason": "fake ok",
                "retry_prompt": "",
                "issues": [],
                "facets_covered": ["correctness","tests","security","reliability","maintainability","ux_cli"],
                "blocking_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
            })
        }
    })
}

pub struct FakeProvider;

impl ProviderAdapter for FakeProvider {
    fn name(&self) -> &'static str {
        "fake"
    }

    fn build_args(
        &self,
        _worktree_path: &Path,
        _prompt: &str,
        _output_path: &Path,
        _config: &LoopConfig,
        _print_only: bool,
    ) -> Result<Vec<String>> {
        Ok(vec!["true".into()])
    }

    fn parse_planner_output(&self, _last_message: &str) -> Result<Value> {
        Ok(json!({
            "mode": "task_graph",
            "summary": "fake plan",
            "nodes": [{"id": "n1", "title": "deliver", "goal": "fake", "depends_on": []}]
        }))
    }

    fn parse_reviewer_output(&self, _last_message: &str) -> Result<Value> {
        Ok(next_reviewer_decision())
    }

    fn preflight_check_argv(&self) -> Vec<String> {
        vec!["true".into()]
    }

    #[allow(clippy::too_many_arguments)]
    fn run(
        &self,
        worktree_path: &Path,
        prompt: &str,
        output_path: &Path,
        _config: &LoopConfig,
        _timeout_seconds: u64,
        raw_output_path: Option<&Path>,
        print_only: bool,
    ) -> Result<ProviderRunResult> {
        if let Some(parent) = output_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let payload = if prompt.contains("planner") || prompt.contains("Return ONLY a JSON object") {
            self.parse_planner_output("")?
        } else if prompt.contains("reviewer")
            || prompt.contains("Stable Review Contract")
            || prompt.contains("\"decision\"")
        {
            self.parse_reviewer_output("")?
        } else {
            // implementer: touch a file in worktree
            if !print_only {
                fs::create_dir_all(worktree_path)?;
                let marker = if prompt.contains("reviewer rejection")
                    || prompt.contains("Previous review reject")
                    || prompt.contains("fake reject")
                    || prompt.contains("blocking")
                    || prompt.contains("P0")
                {
                    "FAKE_REPAIR.md"
                } else {
                    "FAKE_CHANGE.md"
                };
                fs::write(worktree_path.join(marker), "fake implementer\n")?;
                let _ = std::process::Command::new("git")
                    .args(["-C", &worktree_path.display().to_string(), "add", marker])
                    .status();
                let _ = std::process::Command::new("git")
                    .args([
                        "-C",
                        &worktree_path.display().to_string(),
                        "commit",
                        "-m",
                        "fake",
                    ])
                    .status();
            }
            json!({"ok": true})
        };
        let text = serde_json::to_string_pretty(&payload)?;
        fs::write(output_path, &text)?;
        let raw = raw_output_path.unwrap_or(output_path);
        fs::write(
            raw,
            serde_json::to_string_pretty(&json!({
                "provider": "fake",
                "stdout": text,
                "returncode": 0,
            }))?,
        )?;
        Ok(ProviderRunResult {
            provider: "fake".into(),
            exit_code: 0,
            raw_artifact_path: PathBuf::from(raw),
            timed_out: false,
            interrupted: false,
            killed: false,
            hung: false,
            duration_seconds: 0.01,
            summary: "fake".into(),
        })
    }
}

pub fn fake_providers_enabled() -> bool {
    matches!(
        std::env::var("CC_LOOP_FAKE_PROVIDERS").as_deref(),
        Ok("1") | Ok("true") | Ok("yes")
    )
}
