//! Provider adapters (codex / cursor / claude-code / fake).

mod fake;

pub use fake::{fake_providers_enabled, FakeProvider};

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::Value;

use crate::config::{provider_timeout_seconds, LoopConfig};
use crate::error::{CcError, Result};
use crate::process::{run_with_timeout, RunResult};

fn command_exists(name: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| {
            std::env::split_paths(&paths).any(|dir| {
                let p = dir.join(name);
                p.is_file()
            })
        })
        .unwrap_or(false)
}

#[derive(Debug, Clone)]
pub struct ProviderRunResult {
    pub provider: String,
    pub exit_code: i32,
    pub raw_artifact_path: PathBuf,
    pub timed_out: bool,
    pub interrupted: bool,
    pub killed: bool,
    pub hung: bool,
    pub duration_seconds: f64,
    pub summary: String,
}

pub trait ProviderAdapter: Send + Sync {
    fn name(&self) -> &'static str;

    fn build_args(
        &self,
        worktree_path: &Path,
        prompt: &str,
        output_path: &Path,
        config: &LoopConfig,
        print_only: bool,
    ) -> Result<Vec<String>>;

    fn parse_planner_output(&self, last_message: &str) -> Result<Value>;
    fn parse_reviewer_output(&self, last_message: &str) -> Result<Value>;

    fn preflight_check_argv(&self) -> Vec<String>;

    fn preflight_check(&self) -> Result<()> {
        let argv = self.preflight_check_argv();
        let result = run_with_timeout(&argv, None, Duration::from_secs(30), &[])?;
        if result.returncode != 0 {
            let detail = result.stderr.trim();
            return Err(CcError::provider(format!(
                "provider preflight check failed for {} ({}){}",
                self.name(),
                argv.join(" "),
                if detail.is_empty() {
                    String::new()
                } else {
                    format!(": {detail}")
                }
            )));
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn run(
        &self,
        worktree_path: &Path,
        prompt: &str,
        output_path: &Path,
        config: &LoopConfig,
        timeout_seconds: u64,
        raw_output_path: Option<&Path>,
        print_only: bool,
    ) -> Result<ProviderRunResult> {
        let args = self.build_args(worktree_path, prompt, output_path, config, print_only)?;
        // Persist argv for observability
        if let Some(parent) = output_path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let result = run_with_timeout(
            &args,
            if print_only { None } else { Some(worktree_path) },
            Duration::from_secs(timeout_seconds.max(1)),
            &[],
        )?;
        let raw_path = raw_output_path.unwrap_or(output_path);
        persist_raw(&result, raw_path, prompt)?;
        // Also write last-message style output
        let combined = if !result.stdout.trim().is_empty() {
            result.stdout.clone()
        } else {
            result.stderr.clone()
        };
        fs::write(output_path, &combined)?;

        Ok(ProviderRunResult {
            provider: self.name().to_string(),
            exit_code: result.returncode,
            raw_artifact_path: raw_path.to_path_buf(),
            timed_out: result.timed_out,
            interrupted: result.interrupted,
            killed: result.killed,
            hung: result.hung,
            duration_seconds: result.duration_seconds,
            summary: combined.chars().take(200).collect(),
        })
    }
}

fn persist_raw(result: &RunResult, path: &Path, prompt: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let payload = serde_json::json!({
        "args": result.args,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "killed": result.killed,
        "hung": result.hung,
        "duration_seconds": result.duration_seconds,
        "prompt_chars": prompt.len(),
    });
    fs::write(path, serde_json::to_string_pretty(&payload)?)?;
    Ok(())
}

/// Extract JSON object from provider text (fenced or raw).
pub fn extract_json_object(text: &str) -> Result<Value> {
    let trimmed = text.trim();
    if let Ok(v) = serde_json::from_str::<Value>(trimmed) {
        if v.is_object() {
            return Ok(v);
        }
    }
    // Fenced ```json ... ```
    if let Some(start) = trimmed.find("```") {
        let after = &trimmed[start + 3..];
        let after = after
            .strip_prefix("json")
            .or_else(|| after.strip_prefix("JSON"))
            .unwrap_or(after)
            .trim_start_matches('\n');
        if let Some(end) = after.find("```") {
            let block = after[..end].trim();
            if let Ok(v) = serde_json::from_str::<Value>(block) {
                if v.is_object() {
                    return Ok(v);
                }
            }
        }
    }
    // First { ... } scan
    if let Some(start) = trimmed.find('{') {
        let mut depth = 0i32;
        for (i, ch) in trimmed[start..].char_indices() {
            match ch {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        let slice = &trimmed[start..start + i + 1];
                        if let Ok(v) = serde_json::from_str::<Value>(slice) {
                            if v.is_object() {
                                return Ok(v);
                            }
                        }
                        break;
                    }
                }
                _ => {}
            }
        }
    }
    Err(CcError::provider(
        "failed to parse provider JSON object from output",
    ))
}

pub struct CodexProvider;
pub struct CursorProvider;
pub struct ClaudeCodeProvider;

impl ProviderAdapter for CodexProvider {
    fn name(&self) -> &'static str {
        "codex"
    }

    fn build_args(
        &self,
        _worktree_path: &Path,
        prompt: &str,
        output_path: &Path,
        config: &LoopConfig,
        _print_only: bool,
    ) -> Result<Vec<String>> {
        let mut args = vec![
            "codex".into(),
            "exec".into(),
            "--full-auto".into(),
            "-o".into(),
            output_path.display().to_string(),
        ];
        if !config.codex_model.is_empty() {
            args.push("-m".into());
            args.push(config.codex_model.clone());
        }
        args.push(prompt.to_string());
        Ok(args)
    }

    fn parse_planner_output(&self, last_message: &str) -> Result<Value> {
        extract_json_object(last_message)
    }

    fn parse_reviewer_output(&self, last_message: &str) -> Result<Value> {
        extract_json_object(last_message)
    }

    fn preflight_check_argv(&self) -> Vec<String> {
        vec!["codex".into(), "--version".into()]
    }
}

impl ProviderAdapter for CursorProvider {
    fn name(&self) -> &'static str {
        "cursor"
    }

    fn build_args(
        &self,
        worktree_path: &Path,
        prompt: &str,
        _output_path: &Path,
        config: &LoopConfig,
        _print_only: bool,
    ) -> Result<Vec<String>> {
        // Prefer `agent` CLI when available.
        let bin = if command_exists("agent") {
            "agent"
        } else {
            "cursor"
        };
        let mut args = vec![
            bin.into(),
            "-p".into(),
            prompt.to_string(),
            "--workspace".into(),
            worktree_path.display().to_string(),
        ];
        if config.cursor_force {
            args.push("--force".into());
        }
        if !config.cursor_sandbox.is_empty() {
            args.push("--sandbox".into());
            args.push(config.cursor_sandbox.clone());
        }
        if !config.cursor_model.is_empty() {
            args.push("--model".into());
            args.push(config.cursor_model.clone());
        }
        Ok(args)
    }

    fn parse_planner_output(&self, last_message: &str) -> Result<Value> {
        extract_json_object(last_message)
    }

    fn parse_reviewer_output(&self, last_message: &str) -> Result<Value> {
        extract_json_object(last_message)
    }

    fn preflight_check_argv(&self) -> Vec<String> {
        if command_exists("agent") {
            vec!["agent".into(), "--version".into()]
        } else {
            vec!["cursor".into(), "--version".into()]
        }
    }
}

impl ProviderAdapter for ClaudeCodeProvider {
    fn name(&self) -> &'static str {
        "claude-code"
    }

    fn build_args(
        &self,
        _worktree_path: &Path,
        prompt: &str,
        _output_path: &Path,
        config: &LoopConfig,
        print_only: bool,
    ) -> Result<Vec<String>> {
        // planner/reviewer: print_only=true; implementer: print_only=false
        let mut args = vec![
            "claude".into(),
            "--dangerously-skip-permissions".into(),
        ];
        if print_only {
            args.push("--print".into());
        }
        if !config.claude_code_model.is_empty() {
            args.push("-m".into());
            args.push(config.claude_code_model.clone());
        }
        args.push("-p".into());
        args.push(prompt.to_string());
        Ok(args)
    }

    fn parse_planner_output(&self, last_message: &str) -> Result<Value> {
        extract_json_object(last_message)
    }

    fn parse_reviewer_output(&self, last_message: &str) -> Result<Value> {
        extract_json_object(last_message)
    }

    fn preflight_check_argv(&self) -> Vec<String> {
        vec!["claude".into(), "--version".into()]
    }
}

pub fn get_provider(name: &str) -> Result<Box<dyn ProviderAdapter>> {
    if fake_providers_enabled() || name.trim() == "fake" {
        return Ok(Box::new(FakeProvider));
    }
    match name.trim() {
        "codex" => Ok(Box::new(CodexProvider)),
        "cursor" => Ok(Box::new(CursorProvider)),
        "claude-code" => Ok(Box::new(ClaudeCodeProvider)),
        other => Err(CcError::config(format!("unknown provider: {other}"))),
    }
}

#[allow(clippy::too_many_arguments)]
pub fn run_role(
    role: &str,
    provider_name: &str,
    worktree_path: &Path,
    prompt: &str,
    output_path: &Path,
    raw_path: &Path,
    config: &LoopConfig,
    print_only: bool,
) -> Result<ProviderRunResult> {
    let provider = get_provider(provider_name)?;
    let timeout = provider_timeout_seconds(provider_name, config);
    let _ = role;
    provider.run(
        worktree_path,
        prompt,
        output_path,
        config,
        timeout,
        Some(raw_path),
        print_only,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_fenced_json() {
        let text = "here\n```json\n{\"decision\":\"approve\"}\n```\n";
        let v = extract_json_object(text).unwrap();
        assert_eq!(v["decision"], "approve");
    }

    #[test]
    fn claude_print_only_flag() {
        let p = ClaudeCodeProvider;
        let cfg = crate::config::default_config();
        let args = p
            .build_args(Path::new("/tmp"), "hi", Path::new("/tmp/o"), &cfg, true)
            .unwrap();
        assert!(args.contains(&"--print".into()));
        let args2 = p
            .build_args(Path::new("/tmp"), "hi", Path::new("/tmp/o"), &cfg, false)
            .unwrap();
        assert!(!args2.contains(&"--print".into()));
    }
}
