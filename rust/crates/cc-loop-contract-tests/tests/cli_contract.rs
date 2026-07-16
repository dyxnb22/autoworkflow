//! Black-box CLI contract tests (INTEGRATION.md schema v1).

use std::fs;

use assert_cmd::Command;
use serde_json::Value;
use tempfile::tempdir;

fn init_git_repo(dir: &std::path::Path) {
    std::process::Command::new("git")
        .args(["init", "-b", "main"])
        .current_dir(dir)
        .status()
        .unwrap();
    std::process::Command::new("git")
        .args(["config", "user.email", "t@example.com"])
        .current_dir(dir)
        .status()
        .unwrap();
    std::process::Command::new("git")
        .args(["config", "user.name", "tester"])
        .current_dir(dir)
        .status()
        .unwrap();
    fs::write(dir.join("README.md"), "hello\n").unwrap();
    std::process::Command::new("git")
        .args(["add", "."])
        .current_dir(dir)
        .status()
        .unwrap();
    std::process::Command::new("git")
        .args(["commit", "-m", "init"])
        .current_dir(dir)
        .status()
        .unwrap();
}

fn cc_loop() -> Command {
    Command::cargo_bin("cc-loop").unwrap()
}

#[test]
fn version_prints() {
    cc_loop()
        .arg("--version")
        .assert()
        .success()
        .stdout(predicates::str::contains("0.12.0"));
}

#[test]
fn state_root_must_precede_or_be_global() {
    // clap global flag works after binary name before subcommand
    let dir = tempdir().unwrap();
    cc_loop()
        .args(["--state-root", dir.path().to_str().unwrap(), "list", "--json"])
        .assert()
        .success();
}

#[test]
fn init_doctor_status_summary_contract_fields() {
    let root = tempdir().unwrap();
    let repo = root.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    init_git_repo(&repo);
    let state = root.path().join("state");
    fs::create_dir_all(&state).unwrap();

    // Distinct reviewer: implementer=cursor reviewer=codex
    cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "doctor",
            "--repo",
            repo.to_str().unwrap(),
            "--planner",
            "codex",
            "--implementer",
            "cursor",
            "--reviewer",
            "codex",
            "--skip-provider-check",
            "--json",
        ])
        .assert()
        .success();

    cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "init",
            "--goal",
            "demo goal",
            "--repo",
            repo.to_str().unwrap(),
            "--task-id",
            "demo",
            "--planner",
            "codex",
            "--implementer",
            "cursor",
            "--reviewer",
            "codex",
            "--test-command",
            "true",
            "--json",
        ])
        .assert()
        .success();

    let status = cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "status",
            "--task-id",
            "demo",
            "--json",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let status: Value = serde_json::from_slice(&status).unwrap();
    assert_eq!(status["schema_version"], 1);
    assert_eq!(status["task_id"], "demo");
    assert_eq!(status["status"], "initialized");
    assert_eq!(status["success"], "initialized");
    assert_eq!(status["auto_merge"], false);
    assert_eq!(status["require_distinct_reviewer"], true);
    assert!(status["roles"].is_object());
    assert!(status["distinct_reviewer"].as_bool().unwrap());
    // INTEGRATION.md flat runner fields
    assert!(status.get("attempt").is_some());
    assert!(status.get("running").is_some());
    assert!(status.get("runner_state").is_some());
    assert!(status.get("can_stop").is_some());
    assert!(status.get("can_resume").is_some());
    assert!(status.get("can_cleanup").is_some());
    assert!(status.get("log_path").is_some());
    assert!(status.get("current_message").is_some());
    assert!(status.get("next_action").is_some());
    assert_eq!(status["next_action"], "run");
    assert!(status.get("failure").is_some());

    let list = cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "list",
            "--json",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let list: Value = serde_json::from_slice(&list).unwrap();
    assert!(list.is_array());

    let summary = cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "summary",
            "--task-id",
            "demo",
            "--json",
        ])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let summary: Value = serde_json::from_slice(&summary).unwrap();
    assert_eq!(summary["schema_version"], 1);
    assert_eq!(summary["integration_schema_version"], 1);
    assert_eq!(summary["task_id"], "demo");
    assert!(summary["roles"].is_object());
    assert_eq!(summary["auto_merge"], false);
    assert!(summary.get("tests").is_some());
    assert!(summary.get("review").is_some());
    assert!(summary.get("execution_timeline").is_some());
}

#[test]
fn distinct_reviewer_blocks_init() {
    let root = tempdir().unwrap();
    let repo = root.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    init_git_repo(&repo);
    let state = root.path().join("state");

    cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "init",
            "--goal",
            "x",
            "--repo",
            repo.to_str().unwrap(),
            "--task-id",
            "same",
            "--planner",
            "cursor",
            "--implementer",
            "cursor",
            "--reviewer",
            "cursor",
        ])
        .assert()
        .failure();
}

#[test]
fn auto_refuses_without_test_command() {
    let root = tempdir().unwrap();
    let repo = root.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    init_git_repo(&repo);
    let state = root.path().join("state");

    cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "init",
            "--goal",
            "x",
            "--repo",
            repo.to_str().unwrap(),
            "--task-id",
            "notest",
            "--planner",
            "codex",
            "--implementer",
            "cursor",
            "--reviewer",
            "codex",
        ])
        .assert()
        .success();

    cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "auto",
            "--task-id",
            "notest",
        ])
        .assert()
        .failure();
}

#[test]
fn fake_provider_loop_reaches_handoff() {
    use cc_loop_core::config::default_config;
    use cc_loop_core::inspect::derive_success_outcome;
    use cc_loop_core::orchestrator::run_loop;
    use cc_loop_core::state::{create_initial_state, load_state};

    let root = tempdir().unwrap();
    let repo = root.path().join("repo");
    fs::create_dir_all(&repo).unwrap();
    init_git_repo(&repo);
    let state_root = root.path().join("state");
    fs::create_dir_all(&state_root).unwrap();

    let mut cfg = default_config();
    cfg.planner_provider = "fake".into();
    cfg.implementer_provider = "fake".into();
    cfg.reviewer_provider = "fake".into();
    cfg.require_distinct_reviewer = false;
    cfg.auto_merge = false;
    cfg.test_command = vec!["true".into()];

    let commit = cc_loop_core::git::resolve_base_commit(&repo, "main", 30).unwrap();
    let mut state = create_initial_state("fake1", "add note", &repo, "main", &commit, Some(cfg));
    cc_loop_core::state::save_state(&state, &state_root).unwrap();

    std::env::set_var("CC_LOOP_FAKE_PROVIDERS", "1");
    let outcome = run_loop(&mut state, &state_root, None).unwrap();
    std::env::remove_var("CC_LOOP_FAKE_PROVIDERS");

    assert!(matches!(
        outcome,
        cc_loop_core::orchestrator::RunOutcome::Success
            | cc_loop_core::orchestrator::RunOutcome::UserStop
    ));
    let loaded = load_state("fake1", &state_root).unwrap();
    let success = derive_success_outcome(&loaded, loaded.latest_attempt());
    assert!(
        success == "ready_for_handoff",
        "unexpected success={success} status={:?}",
        loaded.status
    );
}

#[test]
fn handoff_success_vocabulary() {
    use cc_loop_core::config::default_config;
    use cc_loop_core::inspect::derive_success_outcome;
    use cc_loop_core::orchestrator::inject_fake_success_handoff;
    use cc_loop_core::state::create_initial_state;

    let root = tempdir().unwrap();
    let mut cfg = default_config();
    cfg.auto_merge = false;
    cfg.require_distinct_reviewer = false;
    let mut state = create_initial_state("h1", "g", root.path(), "main", "abc", Some(cfg));
    inject_fake_success_handoff(&mut state, root.path()).unwrap();
    assert_eq!(
        derive_success_outcome(&state, state.latest_attempt()),
        "ready_for_handoff"
    );
}
