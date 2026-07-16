//! Git helpers — always shell=false; never switches user's main checkout for work.

use std::path::{Path, PathBuf};
use std::time::Duration;

use regex::Regex;

use crate::error::{CcError, Result};
use crate::process::run_with_timeout;

const DEFAULT_GIT_TIMEOUT_SECS: u64 = 60;

#[derive(Debug, Clone)]
pub struct GitCommandResult {
    pub returncode: i32,
    pub stdout: String,
    pub stderr: String,
    pub args: Vec<String>,
}

pub fn resolve_repo_path(path: &Path) -> PathBuf {
    let expanded = if path.starts_with("~") {
        if let Some(home) = std::env::var_os("HOME") {
            PathBuf::from(home).join(path.strip_prefix("~").unwrap_or(path))
        } else {
            path.to_path_buf()
        }
    } else {
        path.to_path_buf()
    };
    expanded.canonicalize().unwrap_or(expanded)
}

pub fn repo_label(repo: &Path) -> String {
    repo.file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "repo".into())
}

pub fn run_git(
    repo: &Path,
    args: &[&str],
    check: bool,
    timeout_secs: u64,
) -> Result<GitCommandResult> {
    let mut argv = vec!["git".into(), "-C".into(), repo.display().to_string()];
    argv.extend(args.iter().map(|s| (*s).to_string()));
    let result = run_with_timeout(&argv, None, Duration::from_secs(timeout_secs.max(1)), &[])?;
    let out = GitCommandResult {
        returncode: result.returncode,
        stdout: result.stdout,
        stderr: result.stderr,
        args: args.iter().map(|s| (*s).to_string()).collect(),
    };
    if check && out.returncode != 0 {
        let detail = out.stderr.trim();
        let detail = if detail.is_empty() {
            out.stdout.trim()
        } else {
            detail
        };
        return Err(CcError::git(if detail.is_empty() {
            format!("git {} failed", args.join(" "))
        } else {
            detail.to_string()
        }));
    }
    Ok(out)
}

pub fn ensure_git_repo(repo: &Path, timeout_secs: u64) -> Result<()> {
    let result = run_git(repo, &["rev-parse", "--is-inside-work-tree"], false, timeout_secs)?;
    if result.returncode != 0 || result.stdout.trim() != "true" {
        return Err(CcError::config(format!(
            "not a git repository: {}",
            repo.display()
        )));
    }
    Ok(())
}

pub fn resolve_base_commit(repo: &Path, base_branch: &str, timeout_secs: u64) -> Result<String> {
    ensure_git_repo(repo, timeout_secs)?;
    let candidates = [
        format!("refs/heads/{base_branch}"),
        format!("refs/remotes/origin/{base_branch}"),
        base_branch.to_string(),
    ];
    for cand in candidates {
        let r = run_git(repo, &["rev-parse", "--verify", &cand], false, timeout_secs)?;
        if r.returncode == 0 {
            return Ok(r.stdout.trim().to_string());
        }
    }
    Err(CcError::config(format!(
        "base branch not found: {base_branch}"
    )))
}

pub fn is_dirty(repo: &Path, timeout_secs: u64) -> Result<bool> {
    let r = run_git(repo, &["status", "--porcelain"], true, timeout_secs)?;
    Ok(!r.stdout.trim().is_empty())
}

pub fn current_branch(repo: &Path, timeout_secs: u64) -> Result<String> {
    let r = run_git(repo, &["rev-parse", "--abbrev-ref", "HEAD"], true, timeout_secs)?;
    Ok(r.stdout.trim().to_string())
}

pub fn head_commit(repo: &Path, timeout_secs: u64) -> Result<String> {
    let r = run_git(repo, &["rev-parse", "HEAD"], true, timeout_secs)?;
    Ok(r.stdout.trim().to_string())
}

pub fn create_worktree(
    repo: &Path,
    worktree: &Path,
    branch: &str,
    base_commit: &str,
    timeout_secs: u64,
) -> Result<()> {
    if let Some(parent) = worktree.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // Create branch from base, then add worktree.
    let _ = run_git(
        repo,
        &["branch", branch, base_commit],
        false,
        timeout_secs,
    )?;
    run_git(
        repo,
        &[
            "worktree",
            "add",
            worktree.to_str().ok_or_else(|| CcError::git("invalid worktree path"))?,
            branch,
        ],
        true,
        timeout_secs,
    )?;
    Ok(())
}

pub fn remove_worktree(repo: &Path, worktree: &Path, timeout_secs: u64) -> Result<()> {
    let _ = run_git(
        repo,
        &[
            "worktree",
            "remove",
            "--force",
            worktree.to_str().unwrap_or(""),
        ],
        false,
        timeout_secs,
    )?;
    Ok(())
}

pub fn diff_stat(repo: &Path, base_commit: &str, timeout_secs: u64) -> Result<String> {
    let r = run_git(
        repo,
        &["diff", "--stat", base_commit, "HEAD"],
        true,
        timeout_secs,
    )?;
    Ok(r.stdout)
}

pub fn diff_name_only(repo: &Path, base_commit: &str, timeout_secs: u64) -> Result<Vec<String>> {
    let r = run_git(
        repo,
        &["diff", "--name-only", base_commit, "HEAD"],
        true,
        timeout_secs,
    )?;
    Ok(r.stdout
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect())
}

pub fn diff_patch(repo: &Path, base_commit: &str, timeout_secs: u64) -> Result<String> {
    let r = run_git(repo, &["diff", base_commit, "HEAD"], true, timeout_secs)?;
    Ok(r.stdout)
}

pub fn merge_branch(
    repo: &Path,
    branch: &str,
    timeout_secs: u64,
) -> Result<GitCommandResult> {
    // Merge into current checkout — caller must ensure we're on base and user opted in.
    run_git(
        repo,
        &["merge", "--no-ff", "-m", &format!("cc-loop: merge {branch}"), branch],
        false,
        timeout_secs,
    )
}

pub fn checkout_branch(repo: &Path, branch: &str, timeout_secs: u64) -> Result<()> {
    run_git(repo, &["checkout", branch], true, timeout_secs)?;
    Ok(())
}

pub fn warn_artifact_paths(paths: &[String]) -> Vec<String> {
    let patterns: Vec<(Regex, &str)> = [
        (r"(^|/)__pycache__(/|$)", "__pycache__"),
        (r"\.pyc$", "*.pyc"),
        (r"(^|/)\.DS_Store$", ".DS_Store"),
        (r"\.egg-info(/|$)", "*.egg-info"),
        (r"(^|/)\.pytest_cache(/|$)", ".pytest_cache"),
    ]
    .into_iter()
    .filter_map(|(p, label)| Regex::new(p).ok().map(|r| (r, label)))
    .collect();

    let mut warnings = Vec::new();
    for path in paths {
        for (re, label) in &patterns {
            if re.is_match(path) {
                warnings.push(format!("{path} looks like build artifact ({label})"));
            }
        }
    }
    warnings
}

pub fn default_git_timeout() -> u64 {
    DEFAULT_GIT_TIMEOUT_SECS
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    use tempfile::tempdir;

    fn init_repo() -> (tempfile::TempDir, PathBuf) {
        let dir = tempdir().unwrap();
        let repo = dir.path().to_path_buf();
        Command::new("git").args(["init", "-b", "main"]).current_dir(&repo).status().unwrap();
        Command::new("git")
            .args(["config", "user.email", "t@example.com"])
            .current_dir(&repo)
            .status()
            .unwrap();
        Command::new("git")
            .args(["config", "user.name", "t"])
            .current_dir(&repo)
            .status()
            .unwrap();
        std::fs::write(repo.join("README"), "hi").unwrap();
        Command::new("git").args(["add", "."]).current_dir(&repo).status().unwrap();
        Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(&repo)
            .status()
            .unwrap();
        (dir, repo)
    }

    #[test]
    fn resolve_base_and_dirty() {
        let (_dir, repo) = init_repo();
        let commit = resolve_base_commit(&repo, "main", 30).unwrap();
        assert!(!commit.is_empty());
        assert!(!is_dirty(&repo, 30).unwrap());
        std::fs::write(repo.join("x"), "y").unwrap();
        assert!(is_dirty(&repo, 30).unwrap());
    }
}
