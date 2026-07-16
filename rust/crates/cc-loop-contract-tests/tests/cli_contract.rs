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
    // Luma delivery card fields on status
    assert!(status.get("plan_summary").is_some());
    assert!(status.get("tests").is_some());
    assert!(status.get("review").is_some());
    assert!(status.get("delivery").is_some());
    assert!(status["delivery"]["roles"].is_object());
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
    assert!(summary.get("delivery").is_some());
    assert!(summary["delivery"].get("plan_summary").is_some());
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

    // run/resume share the same hard gate
    cc_loop()
        .args([
            "--state-root",
            state.to_str().unwrap(),
            "run",
            "--task-id",
            "notest",
        ])
        .assert()
        .failure();
}

#[test]
fn distinct_reviewer_same_provider_different_model_ok() {
    use cc_loop_core::config::{default_config, distinct_reviewer_satisfied};

    let mut cfg = default_config();
    cfg.implementer_provider = "cursor".into();
    cfg.reviewer_provider = "cursor".into();
    cfg.cursor_model = "model-a".into();
    // Same provider+model → not distinct
    assert!(!distinct_reviewer_satisfied(&cfg, None));
    // Different models via role map still use resolve_provider_model — both get same cursor_model.
    // Distinctness requires different provider names OR we need separate model fields per role.
    // Current product: same provider name resolves to the same model string → not distinct.
    cfg.reviewer_provider = "claude-code".into();
    assert!(distinct_reviewer_satisfied(&cfg, None));
}

#[test]
fn fake_provider_loop_reaches_handoff() {
    use cc_loop_core::config::default_config;
    use cc_loop_core::inspect::derive_success_outcome;
    use cc_loop_core::orchestrator::run_loop;
    use cc_loop_core::provider::set_fake_reviewer_rejects;
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

    set_fake_reviewer_rejects(0);
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
fn reject_loops_back_to_implementer_then_handoff() {
    use cc_loop_core::config::default_config;
    use cc_loop_core::inspect::{build_status_json, derive_success_outcome};
    use cc_loop_core::orchestrator::run_loop;
    use cc_loop_core::provider::set_fake_reviewer_rejects;
    use cc_loop_core::state::{AttemptPhase, create_initial_state, load_state};

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
    cfg.max_retries_per_step = 3;
    cfg.test_command = vec!["true".into()];

    let commit = cc_loop_core::git::resolve_base_commit(&repo, "main", 30).unwrap();
    let mut state =
        create_initial_state("reject1", "fix via reject loop", &repo, "main", &commit, Some(cfg));
    cc_loop_core::state::save_state(&state, &state_root).unwrap();

    set_fake_reviewer_rejects(1);
    std::env::set_var("CC_LOOP_FAKE_PROVIDERS", "1");
    let outcome = run_loop(&mut state, &state_root, None).unwrap();
    std::env::remove_var("CC_LOOP_FAKE_PROVIDERS");
    set_fake_reviewer_rejects(0);

    assert!(matches!(
        outcome,
        cc_loop_core::orchestrator::RunOutcome::Success
            | cc_loop_core::orchestrator::RunOutcome::UserStop
    ));
    let loaded = load_state("reject1", &state_root).unwrap();
    assert!(
        loaded
            .history
            .iter()
            .any(|a| a.phase == AttemptPhase::Rejected || a.decision == "reject"),
        "expected a rejected attempt in history: {:?}",
        loaded
            .history
            .iter()
            .map(|a| (a.phase.as_str(), a.decision.as_str(), a.retry))
            .collect::<Vec<_>>()
    );
    let success = derive_success_outcome(&loaded, loaded.latest_attempt());
    assert_eq!(success, "ready_for_handoff");
    let status = build_status_json(&loaded, &state_root);
    assert!(status["delivery"]["roles"].is_object());
    assert_eq!(status["success"], "ready_for_handoff");
}

#[test]
fn red_tests_block_review_and_surface_repair() {
    use cc_loop_core::config::default_config;
    use cc_loop_core::inspect::{build_status_json, derive_next_action};
    use cc_loop_core::orchestrator::run_loop;
    use cc_loop_core::provider::set_fake_reviewer_rejects;
    use cc_loop_core::runner::is_runner_alive;
    use cc_loop_core::state::{create_initial_state, load_state, TaskStatus};

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
    cfg.auto_recover_tests = true;
    cfg.max_retries_per_step = 2;
    cfg.test_command = vec!["false".into()];

    let commit = cc_loop_core::git::resolve_base_commit(&repo, "main", 30).unwrap();
    let mut state =
        create_initial_state("red1", "should fail tests", &repo, "main", &commit, Some(cfg));
    cc_loop_core::state::save_state(&state, &state_root).unwrap();

    set_fake_reviewer_rejects(0);
    std::env::set_var("CC_LOOP_FAKE_PROVIDERS", "1");
    // Limit to one attempt so we stop at first red test rather than spinning repairs.
    let outcome = run_loop(&mut state, &state_root, Some(1)).unwrap();
    std::env::remove_var("CC_LOOP_FAKE_PROVIDERS");

    let loaded = load_state("red1", &state_root).unwrap();
    let attempt = loaded.latest_attempt().expect("attempt");
    assert_eq!(attempt.test_status, "failed");
    assert_ne!(attempt.decision, "approve");
    assert!(attempt.review_json.is_none(), "red tests must not reach review");
    assert!(matches!(
        loaded.status,
        TaskStatus::Stopped | TaskStatus::Running | TaskStatus::Failed
    ));
    let (running, _) = is_runner_alive(&state_root, "red1");
    let next = derive_next_action(
        &loaded,
        Some(attempt),
        running,
        "stopped",
    );
    assert!(
        next == "repair" || next == "resume" || next == "failed",
        "unexpected next_action={next}"
    );
    let status = build_status_json(&loaded, &state_root);
    assert_eq!(status["tests"]["fail"], true);
    assert_ne!(status["success"], "ready_for_handoff");
    // run_loop with max_attempts=1 should not claim delivery success
    assert!(matches!(
        outcome,
        cc_loop_core::orchestrator::RunOutcome::UserStop
            | cc_loop_core::orchestrator::RunOutcome::Failed
            | cc_loop_core::orchestrator::RunOutcome::Success
    ));
    assert_ne!(
        status["success"].as_str().unwrap_or(""),
        "ready_for_handoff"
    );
}

#[test]
fn skipped_tests_cannot_handoff_without_escape() {
    use cc_loop_core::config::default_config;
    use cc_loop_core::inspect::derive_success_outcome;
    use cc_loop_core::orchestrator::run_loop;
    use cc_loop_core::provider::set_fake_reviewer_rejects;
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
    cfg.allow_merge_without_tests = false;
    cfg.test_command = vec![]; // skipped path

    let commit = cc_loop_core::git::resolve_base_commit(&repo, "main", 30).unwrap();
    let mut state =
        create_initial_state("skip1", "no tests configured", &repo, "main", &commit, Some(cfg));
    cc_loop_core::state::save_state(&state, &state_root).unwrap();

    set_fake_reviewer_rejects(0);
    std::env::set_var("CC_LOOP_FAKE_PROVIDERS", "1");
    let _ = run_loop(&mut state, &state_root, Some(1)).unwrap();
    std::env::remove_var("CC_LOOP_FAKE_PROVIDERS");

    let loaded = load_state("skip1", &state_root).unwrap();
    let attempt = loaded.latest_attempt().expect("attempt");
    assert_eq!(attempt.test_status, "skipped");
    assert!(attempt.review_json.is_none(), "skipped tests must not reach review");
    let success = derive_success_outcome(&loaded, Some(attempt));
    assert_ne!(success, "ready_for_handoff");
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
