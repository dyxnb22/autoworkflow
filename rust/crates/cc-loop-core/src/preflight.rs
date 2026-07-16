//! Preflight / doctor checks.

use std::path::Path;

use serde_json::{json, Value};

use crate::config::{
    distinct_reviewer_satisfied, format_distinct_reviewer_error, LoopConfig,
};
use crate::error::{CcError, Result};
use crate::git::{ensure_git_repo, is_dirty, resolve_base_commit, resolve_repo_path};
use crate::provider::get_provider;

#[derive(Debug, Clone)]
pub struct PreflightResult {
    pub ok: bool,
    pub repo: String,
    pub base_branch: String,
    pub base_commit: String,
    pub checks: Vec<Value>,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}

/// Options for preflight / doctor.
#[derive(Debug, Clone, Copy)]
pub struct PreflightOptions {
    pub check_providers: bool,
    pub allow_dirty: bool,
    /// When true, empty `test_command` (without escape hatch) is an error.
    /// Use for run/auto/resume. Init/doctor may keep this false and only warn.
    pub require_test_command: bool,
}

impl Default for PreflightOptions {
    fn default() -> Self {
        Self {
            check_providers: true,
            allow_dirty: false,
            require_test_command: false,
        }
    }
}

impl PreflightResult {
    pub fn to_json(&self, config: &LoopConfig) -> Value {
        json!({
            "ok": self.ok,
            "repo": self.repo,
            "base_branch": self.base_branch,
            "base_commit": self.base_commit,
            "checks": self.checks,
            "errors": self.errors,
            "warnings": self.warnings,
            "roles": {
                "planner": {"provider": config.planner_provider, "model": crate::config::resolve_provider_model(&config.planner_provider, config)},
                "implementer": {"provider": config.implementer_provider, "model": crate::config::resolve_provider_model(&config.implementer_provider, config)},
                "reviewer": {"provider": config.reviewer_provider, "model": crate::config::resolve_provider_model(&config.reviewer_provider, config)},
            },
            "distinct_reviewer": distinct_reviewer_satisfied(config, None),
            "require_distinct_reviewer": config.require_distinct_reviewer,
            "auto_merge": config.auto_merge,
            "planner_granularity": config.planner_granularity,
            "test_command": config.test_command,
        })
    }
}

pub fn run_preflight(
    repo: &Path,
    config: &LoopConfig,
    check_providers: bool,
    allow_dirty: bool,
) -> Result<PreflightResult> {
    run_preflight_with(
        repo,
        config,
        PreflightOptions {
            check_providers,
            allow_dirty,
            require_test_command: false,
        },
    )
}

pub fn run_preflight_with(
    repo: &Path,
    config: &LoopConfig,
    opts: PreflightOptions,
) -> Result<PreflightResult> {
    let repo = resolve_repo_path(repo);
    let timeout = config.git_timeout_seconds.max(1);
    let mut errors = Vec::new();
    let mut warnings = Vec::new();
    let mut checks = Vec::new();

    match ensure_git_repo(&repo, timeout) {
        Ok(()) => checks.push(json!({"name": "git_repo", "ok": true})),
        Err(e) => {
            errors.push(e.to_string());
            checks.push(json!({"name": "git_repo", "ok": false, "error": e.to_string()}));
        }
    }

    let base_commit = match resolve_base_commit(&repo, &config.base_branch, timeout) {
        Ok(c) => {
            checks.push(json!({"name": "base_branch", "ok": true, "commit": c}));
            c
        }
        Err(e) => {
            errors.push(e.to_string());
            checks.push(json!({"name": "base_branch", "ok": false, "error": e.to_string()}));
            String::new()
        }
    };

    match is_dirty(&repo, timeout) {
        Ok(true) if !opts.allow_dirty => {
            let msg = "dirty working tree blocks run; commit or stash changes first".to_string();
            errors.push(msg.clone());
            checks.push(json!({"name": "clean_worktree", "ok": false, "error": msg}));
        }
        Ok(dirty) => {
            checks.push(json!({"name": "clean_worktree", "ok": !dirty || opts.allow_dirty, "dirty": dirty}));
            if dirty {
                warnings.push("working tree is dirty".into());
            }
        }
        Err(e) => {
            errors.push(e.to_string());
            checks.push(json!({"name": "clean_worktree", "ok": false, "error": e.to_string()}));
        }
    }

    if config.require_distinct_reviewer && !distinct_reviewer_satisfied(config, None) {
        let msg = format_distinct_reviewer_error(config, None);
        errors.push(msg.clone());
        checks.push(json!({"name": "distinct_reviewer", "ok": false, "error": msg}));
    } else {
        checks.push(json!({
            "name": "distinct_reviewer",
            "ok": true,
            "satisfied": distinct_reviewer_satisfied(config, None),
            "required": config.require_distinct_reviewer,
        }));
    }

    let test_configured = !config.test_command.is_empty();
    let test_escape = config.allow_merge_without_tests;
    if !test_configured && !test_escape {
        let msg = "test_command is empty; set --test-command (or allow_merge_without_tests as explicit escape)".to_string();
        if opts.require_test_command {
            errors.push(msg.clone());
            checks.push(json!({
                "name": "test_command",
                "ok": false,
                "configured": false,
                "required": true,
                "error": msg,
            }));
        } else {
            warnings.push(format!(
                "{msg}; run/auto/resume will refuse until configured"
            ));
            checks.push(json!({
                "name": "test_command",
                "ok": true,
                "configured": false,
                "required": false,
            }));
        }
    } else {
        checks.push(json!({
            "name": "test_command",
            "ok": true,
            "configured": test_configured,
            "command": config.test_command,
            "allow_merge_without_tests": test_escape,
        }));
    }

    if opts.check_providers {
        for (role, name) in [
            ("planner", config.planner_provider.as_str()),
            ("implementer", config.implementer_provider.as_str()),
            ("reviewer", config.reviewer_provider.as_str()),
        ] {
            match get_provider(name) {
                Ok(p) => match p.preflight_check() {
                    Ok(()) => checks.push(json!({"name": format!("provider_{role}"), "ok": true, "provider": name})),
                    Err(e) => {
                        errors.push(format!("{role}: {e}"));
                        checks.push(json!({"name": format!("provider_{role}"), "ok": false, "provider": name, "error": e.to_string()}));
                    }
                },
                Err(e) => {
                    errors.push(format!("{role}: {e}"));
                    checks.push(json!({"name": format!("provider_{role}"), "ok": false, "error": e.to_string()}));
                }
            }
        }
    }

    Ok(PreflightResult {
        ok: errors.is_empty(),
        repo: repo.display().to_string(),
        base_branch: config.base_branch.clone(),
        base_commit,
        checks,
        errors,
        warnings,
    })
}

pub fn require_preflight_ok(result: &PreflightResult) -> Result<()> {
    if result.ok {
        Ok(())
    } else {
        Err(CcError::config(result.errors.join("; ")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::default_config;
    use std::process::Command;
    use tempfile::tempdir;

    fn init_repo() -> (tempfile::TempDir, std::path::PathBuf) {
        let root = tempdir().unwrap();
        let repo = root.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        Command::new("git")
            .args(["init", "-b", "main"])
            .current_dir(&repo)
            .status()
            .unwrap();
        Command::new("git")
            .args(["config", "user.email", "t@example.com"])
            .current_dir(&repo)
            .status()
            .unwrap();
        Command::new("git")
            .args(["config", "user.name", "tester"])
            .current_dir(&repo)
            .status()
            .unwrap();
        std::fs::write(repo.join("README"), "x\n").unwrap();
        Command::new("git")
            .args(["add", "."])
            .current_dir(&repo)
            .status()
            .unwrap();
        Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&repo)
            .status()
            .unwrap();
        (root, repo)
    }

    #[test]
    fn require_test_command_errors_when_empty() {
        let (_root, repo) = init_repo();
        let mut cfg = default_config();
        cfg.require_distinct_reviewer = false;
        cfg.implementer_provider = "fake".into();
        cfg.reviewer_provider = "fake".into();
        cfg.planner_provider = "fake".into();
        cfg.test_command.clear();

        let soft = run_preflight_with(
            &repo,
            &cfg,
            PreflightOptions {
                check_providers: false,
                allow_dirty: false,
                require_test_command: false,
            },
        )
        .unwrap();
        assert!(soft.ok);
        assert!(soft.warnings.iter().any(|w| w.contains("test_command")));

        let hard = run_preflight_with(
            &repo,
            &cfg,
            PreflightOptions {
                check_providers: false,
                allow_dirty: false,
                require_test_command: true,
            },
        )
        .unwrap();
        assert!(!hard.ok);
        assert!(hard.errors.iter().any(|e| e.contains("test_command")));
    }
}
