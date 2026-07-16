//! cc-loop CLI — stable integration surface for Luma.

use std::path::{Path, PathBuf};
use std::process::ExitCode as StdExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand};
use serde_json::{json, Map, Value};

use cc_loop_core::config::{distinct_reviewer_satisfied, merge_config, LoopConfig};
use cc_loop_core::error::{CcError, ExitCode};
use cc_loop_core::eval::run_eval_suite;
use cc_loop_core::export::export_jsonl;
use cc_loop_core::git::resolve_repo_path;
use cc_loop_core::graph::build_graph_cli_payload;
use cc_loop_core::inspect::{build_status_json, list_tasks_json};
use cc_loop_core::orchestrator::{
    require_test_command_for_execution, run_loop, RunOutcome,
};
use cc_loop_core::paths::{default_state_root, state_path};
use cc_loop_core::preflight::{
    require_preflight_ok, run_preflight, run_preflight_with, PreflightOptions,
};
use cc_loop_core::report::{build_report, format_report_human};
use cc_loop_core::runner::{cancel_task, cleanup_task, spawn_detached_auto, stop_runner};
use cc_loop_core::state::{
    create_initial_state, list_task_ids, load_state, save_state, TaskStatus,
};
use cc_loop_core::summary::{build_task_summary, format_task_summary_human};
use cc_loop_core::version::CC_LOOP_VERSION;

#[derive(Parser, Debug)]
#[command(name = "cc-loop", version = CC_LOOP_VERSION, about = "Role-separated delivery engine")]
struct Cli {
    /// State directory (default ~/.cc-loop or CC_LOOP_STATE_ROOT)
    #[arg(long, global = true, env = "CC_LOOP_STATE_ROOT")]
    state_root: Option<PathBuf>,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand, Debug)]
#[allow(clippy::large_enum_variant)]
enum Commands {
    Init {
        #[arg(long)]
        goal: String,
        #[arg(long)]
        repo: PathBuf,
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long, default_value = "codex")]
        planner: String,
        #[arg(long, default_value = "cursor")]
        implementer: String,
        #[arg(long, default_value = "codex")]
        reviewer: String,
        #[arg(long)]
        base_branch: Option<String>,
        #[arg(long)]
        auto_merge: bool,
        #[arg(long)]
        allow_merge_without_tests: bool,
        #[arg(long)]
        allow_same_reviewer: bool,
        #[arg(long)]
        planner_granularity: Option<String>,
        /// Stop policy: no_p0_p1 (default) | no_p0_only | approve_only
        #[arg(long)]
        stop_policy: Option<String>,
        /// Comma-separated severities that block handoff (default P0,P1)
        #[arg(long)]
        blocking_severities: Option<String>,
        /// Review mode: structured_single (default) | per_facet
        #[arg(long)]
        review_mode: Option<String>,
        /// Comma-separated review facets (empty = engine defaults)
        #[arg(long)]
        review_facets: Option<String>,
        #[arg(long, num_args = 1.., allow_hyphen_values = true)]
        test_command: Vec<String>,
        #[arg(long)]
        codex_model: Option<String>,
        #[arg(long)]
        cursor_model: Option<String>,
        #[arg(long)]
        claude_code_model: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Doctor {
        #[arg(long)]
        repo: PathBuf,
        #[arg(long, default_value = "codex")]
        planner: String,
        #[arg(long, default_value = "cursor")]
        implementer: String,
        #[arg(long, default_value = "codex")]
        reviewer: String,
        #[arg(long)]
        base_branch: Option<String>,
        #[arg(long)]
        auto_merge: bool,
        #[arg(long)]
        allow_merge_without_tests: bool,
        #[arg(long)]
        allow_same_reviewer: bool,
        #[arg(long, num_args = 0.., allow_hyphen_values = true)]
        test_command: Vec<String>,
        #[arg(long)]
        skip_provider_check: bool,
        #[arg(long)]
        json: bool,
    },
    List {
        #[arg(long)]
        repo: Option<PathBuf>,
        #[arg(long)]
        json: bool,
    },
    Status {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Summary {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Graph {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
        #[arg(long)]
        history: bool,
    },
    Report {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
        #[arg(long, default_value = "human")]
        format: String,
    },
    Stop {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Cancel {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Cleanup {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Eval {
        #[arg(long)]
        task_id: String,
        #[arg(long)]
        suite: PathBuf,
        #[arg(long)]
        json: bool,
    },
    Export {
        #[arg(long)]
        task_id: String,
        #[arg(long, default_value = "jsonl")]
        format: String,
        #[arg(long)]
        output: PathBuf,
    },
    Run {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Resume {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        json: bool,
    },
    Auto {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long)]
        detach: bool,
        #[arg(long)]
        json: bool,
    },
}

fn resolve_state_root(cli: &Cli) -> PathBuf {
    cli.state_root.clone().unwrap_or_else(default_state_root)
}

fn resolve_task_id(state_root: &Path, explicit: Option<&str>) -> Result<String, CcError> {
    if let Some(id) = explicit {
        if !state_path(state_root, id).is_file() {
            return Err(CcError::user(format!("task not found: {id}")));
        }
        return Ok(id.to_string());
    }
    let ids = list_task_ids(state_root)?;
    match ids.as_slice() {
        [only] => Ok(only.clone()),
        [] => Err(CcError::user("no tasks found under state-root")),
        _ => Err(CcError::user("multiple tasks found; pass --task-id")),
    }
}

fn new_task_id() -> String {
    let n = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("t{n:x}")
}

#[allow(clippy::too_many_arguments)]
fn build_config_from_flags(
    planner: &str,
    implementer: &str,
    reviewer: &str,
    base_branch: Option<&str>,
    auto_merge: bool,
    allow_merge_without_tests: bool,
    allow_same_reviewer: bool,
    planner_granularity: Option<&str>,
    test_command: &[String],
    codex_model: Option<&str>,
    cursor_model: Option<&str>,
    claude_code_model: Option<&str>,
    stop_policy: Option<&str>,
    blocking_severities: Option<&str>,
    review_mode: Option<&str>,
    review_facets: Option<&str>,
) -> LoopConfig {
    let mut map = Map::new();
    map.insert("planner_provider".into(), json!(planner));
    map.insert("implementer_provider".into(), json!(implementer));
    map.insert("reviewer_provider".into(), json!(reviewer));
    if let Some(b) = base_branch {
        map.insert("base_branch".into(), json!(b));
    }
    map.insert("auto_merge".into(), json!(auto_merge));
    map.insert(
        "allow_merge_without_tests".into(),
        json!(allow_merge_without_tests),
    );
    map.insert(
        "require_distinct_reviewer".into(),
        json!(!allow_same_reviewer),
    );
    if let Some(g) = planner_granularity {
        map.insert("planner_granularity".into(), json!(g));
    }
    if !test_command.is_empty() {
        map.insert("test_command".into(), json!(test_command));
    }
    if let Some(m) = codex_model {
        map.insert("codex_model".into(), json!(m));
    }
    if let Some(m) = cursor_model {
        map.insert("cursor_model".into(), json!(m));
    }
    if let Some(m) = claude_code_model {
        map.insert("claude_code_model".into(), json!(m));
    }
    if let Some(p) = stop_policy {
        map.insert("stop_policy".into(), json!(p));
    }
    if let Some(s) = blocking_severities {
        let list: Vec<String> = s
            .split(',')
            .map(|x| x.trim().to_string())
            .filter(|x| !x.is_empty())
            .collect();
        if !list.is_empty() {
            map.insert("blocking_severities".into(), json!(list));
        }
    }
    if let Some(m) = review_mode {
        map.insert("review_mode".into(), json!(m));
    }
    if let Some(f) = review_facets {
        let list: Vec<String> = f
            .split(',')
            .map(|x| x.trim().to_string())
            .filter(|x| !x.is_empty())
            .collect();
        if !list.is_empty() {
            map.insert("review_facets".into(), json!(list));
        }
    }
    merge_config(Some(&Value::Object(map)))
}

fn print_json(v: &Value) {
    println!(
        "{}",
        serde_json::to_string_pretty(v).unwrap_or_else(|_| "{}".into())
    );
}

fn map_err(err: CcError) -> StdExitCode {
    eprintln!("error: {err}");
    StdExitCode::from(i32::from(err.exit_code()) as u8)
}

fn main() -> StdExitCode {
    let cli = Cli::parse();
    let state_root = resolve_state_root(&cli);
    match dispatch(cli, &state_root) {
        Ok(code) => StdExitCode::from(i32::from(code) as u8),
        Err(e) => map_err(e),
    }
}

fn dispatch(cli: Cli, state_root: &Path) -> Result<ExitCode, CcError> {
    match cli.command {
        Commands::Init {
            goal,
            repo,
            task_id,
            planner,
            implementer,
            reviewer,
            base_branch,
            auto_merge,
            allow_merge_without_tests,
            allow_same_reviewer,
            planner_granularity,
            stop_policy,
            blocking_severities,
            review_mode,
            review_facets,
            test_command,
            codex_model,
            cursor_model,
            claude_code_model,
            json,
        } => cmd_init(
            state_root,
            goal,
            repo,
            task_id,
            planner,
            implementer,
            reviewer,
            base_branch,
            auto_merge,
            allow_merge_without_tests,
            allow_same_reviewer,
            planner_granularity,
            stop_policy,
            blocking_severities,
            review_mode,
            review_facets,
            test_command,
            codex_model,
            cursor_model,
            claude_code_model,
            json,
        ),
        Commands::Doctor {
            repo,
            planner,
            implementer,
            reviewer,
            base_branch,
            auto_merge,
            allow_merge_without_tests,
            allow_same_reviewer,
            test_command,
            skip_provider_check,
            json,
        } => {
            let config = build_config_from_flags(
                &planner,
                &implementer,
                &reviewer,
                base_branch.as_deref(),
                auto_merge,
                allow_merge_without_tests,
                allow_same_reviewer,
                None,
                &test_command,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            );
            let pre = run_preflight(&repo, &config, !skip_provider_check, true)?;
            if json {
                print_json(&pre.to_json(&config));
            } else {
                println!("doctor: {}", if pre.ok { "ok" } else { "failed" });
                for e in &pre.errors {
                    println!("  error: {e}");
                }
                for w in &pre.warnings {
                    println!("  warning: {w}");
                }
                println!(
                    "roles: planner={} implementer={} reviewer={}",
                    config.planner_provider, config.implementer_provider, config.reviewer_provider
                );
                println!(
                    "distinct_reviewer={} require_distinct_reviewer={} auto_merge={}",
                    distinct_reviewer_satisfied(&config, None),
                    config.require_distinct_reviewer,
                    config.auto_merge
                );
            }
            Ok(if pre.ok {
                ExitCode::Success
            } else {
                ExitCode::UserOrConfig
            })
        }
        Commands::List { json, repo } => {
            if json {
                print_json(&list_tasks_json(state_root, repo.as_deref())?);
            } else {
                let items = list_tasks_json(state_root, repo.as_deref())?;
                if let Some(arr) = items.as_array() {
                    for item in arr {
                        println!(
                            "{}\t{}\t{}\t{}\t{}",
                            item.get("task_id").and_then(|v| v.as_str()).unwrap_or(""),
                            item.get("status").and_then(|v| v.as_str()).unwrap_or(""),
                            item.get("target_repo").and_then(|v| v.as_str()).unwrap_or(""),
                            item.get("phase").and_then(|v| v.as_str()).unwrap_or("-"),
                            item.get("updated_at").and_then(|v| v.as_str()).unwrap_or(""),
                        );
                    }
                }
            }
            Ok(ExitCode::Success)
        }
        Commands::Status { task_id, json } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let state = load_state(&id, state_root)?;
            let payload = build_status_json(&state, state_root);
            if json {
                print_json(&payload);
            } else {
                println!(
                    "task {} status={} success={} next={}",
                    id,
                    state.status.as_str(),
                    payload.get("success").and_then(|v| v.as_str()).unwrap_or(""),
                    payload
                        .get("next_action")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                );
            }
            Ok(ExitCode::Success)
        }
        Commands::Summary { task_id, json } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let state = load_state(&id, state_root)?;
            let summary = build_task_summary(&state, state_root);
            if json {
                print_json(&summary);
            } else {
                println!("{}", format_task_summary_human(&summary));
            }
            Ok(ExitCode::Success)
        }
        Commands::Graph {
            task_id,
            json,
            history,
        } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let state = load_state(&id, state_root)?;
            let mut payload = build_graph_cli_payload(state.task_graph.as_ref());
            if history {
                if let Some(g) = state.task_graph.as_ref() {
                    if let Some(tg) = payload.get_mut("task_graph").and_then(|v| v.as_object_mut()) {
                        tg.insert("history".into(), json!(g.history));
                    }
                }
            }
            if json {
                print_json(&payload);
            } else if let Some(g) = state.task_graph.as_ref() {
                let status = g.status_summary();
                println!(
                    "Task graph: {}  complete={}",
                    id,
                    status.get("complete").and_then(|v| v.as_bool()).unwrap_or(false)
                );
                for n in &g.nodes {
                    println!("{}  {:<8}  {}", n.id, n.status.as_str(), n.title);
                }
            } else {
                println!("No task graph for this task.");
            }
            Ok(ExitCode::Success)
        }
        Commands::Report {
            task_id,
            json,
            format,
        } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let state = load_state(&id, state_root)?;
            let report = build_report(&state, state_root);
            if json || format == "json" {
                print_json(&report);
            } else {
                print!("{}", format_report_human(&report));
            }
            Ok(ExitCode::Success)
        }
        Commands::Stop { task_id, json } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let out = stop_runner(state_root, &id)?;
            if json {
                print_json(&out);
            } else {
                println!("stopped {id}");
            }
            Ok(ExitCode::Success)
        }
        Commands::Cancel { task_id, json } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let out = cancel_task(state_root, &id)?;
            if json {
                print_json(&out);
            } else {
                println!("cancelled {id}");
            }
            Ok(ExitCode::Success)
        }
        Commands::Cleanup { task_id, json } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let out = cleanup_task(state_root, &id)?;
            if json {
                print_json(&out);
            } else {
                println!("cleaned {id}");
            }
            Ok(ExitCode::Success)
        }
        Commands::Eval {
            task_id,
            suite,
            json,
        } => {
            let out = run_eval_suite(state_root, &task_id, &suite)?;
            if json {
                print_json(&out);
            } else {
                println!(
                    "eval ok={} passed={} failed={}",
                    out.get("ok").and_then(|v| v.as_bool()).unwrap_or(false),
                    out.get("passed").and_then(|v| v.as_u64()).unwrap_or(0),
                    out.get("failed").and_then(|v| v.as_u64()).unwrap_or(0)
                );
            }
            Ok(
                if out.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
                    ExitCode::Success
                } else {
                    ExitCode::UserOrConfig
                },
            )
        }
        Commands::Export {
            task_id,
            format,
            output,
        } => {
            if format != "jsonl" {
                return Err(CcError::user("only --format jsonl is supported"));
            }
            let rows = export_jsonl(state_root, &task_id, &output)?;
            println!("exported {rows} rows to {}", output.display());
            Ok(ExitCode::Success)
        }
        Commands::Run { task_id, .. } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let mut state = load_state(&id, state_root)?;
            require_test_command_for_execution(&state.config)?;
            let pre = run_preflight_with(
                Path::new(&state.target_repo),
                &state.config,
                PreflightOptions {
                    check_providers: true,
                    allow_dirty: false,
                    require_test_command: true,
                },
            )?;
            require_preflight_ok(&pre)?;
            Ok(match run_loop(&mut state, state_root, Some(1))? {
                RunOutcome::Success | RunOutcome::UserStop => ExitCode::Success,
                RunOutcome::Failed => ExitCode::ExecutionFailure,
            })
        }
        Commands::Resume { task_id, .. } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let mut state = load_state(&id, state_root)?;
            require_test_command_for_execution(&state.config)?;
            if matches!(
                state.status,
                TaskStatus::Done | TaskStatus::Failed | TaskStatus::Cancelled
            ) {
                return Err(CcError::user(format!(
                    "resume not allowed for status={}",
                    state.status.as_str()
                )));
            }
            let pre = run_preflight_with(
                Path::new(&state.target_repo),
                &state.config,
                PreflightOptions {
                    check_providers: true,
                    allow_dirty: false,
                    require_test_command: true,
                },
            )?;
            require_preflight_ok(&pre)?;
            Ok(match run_loop(&mut state, state_root, None)? {
                RunOutcome::Success | RunOutcome::UserStop => ExitCode::Success,
                RunOutcome::Failed => ExitCode::ExecutionFailure,
            })
        }
        Commands::Auto {
            task_id,
            detach,
            json,
        } => {
            let id = resolve_task_id(state_root, task_id.as_deref())?;
            let mut state = load_state(&id, state_root)?;
            require_test_command_for_execution(&state.config)?;
            if detach {
                let exe = std::env::current_exe()
                    .map_err(|e| CcError::execution(format!("current_exe: {e}")))?;
                let out = spawn_detached_auto(state_root, &id, &exe)?;
                if json {
                    print_json(&out);
                } else {
                    println!(
                        "detached auto pid={}",
                        out.get("pid").and_then(|v| v.as_u64()).unwrap_or(0)
                    );
                }
                return Ok(ExitCode::Success);
            }
            let pre = run_preflight_with(
                Path::new(&state.target_repo),
                &state.config,
                PreflightOptions {
                    check_providers: true,
                    allow_dirty: false,
                    require_test_command: true,
                },
            )?;
            require_preflight_ok(&pre)?;
            Ok(match run_loop(&mut state, state_root, None)? {
                RunOutcome::Success => ExitCode::Success,
                RunOutcome::UserStop => ExitCode::UserOrConfig,
                RunOutcome::Failed => ExitCode::ExecutionFailure,
            })
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn cmd_init(
    state_root: &Path,
    goal: String,
    repo: PathBuf,
    task_id: Option<String>,
    planner: String,
    implementer: String,
    reviewer: String,
    base_branch: Option<String>,
    auto_merge: bool,
    allow_merge_without_tests: bool,
    allow_same_reviewer: bool,
    planner_granularity: Option<String>,
    stop_policy: Option<String>,
    blocking_severities: Option<String>,
    review_mode: Option<String>,
    review_facets: Option<String>,
    test_command: Vec<String>,
    codex_model: Option<String>,
    cursor_model: Option<String>,
    claude_code_model: Option<String>,
    json: bool,
) -> Result<ExitCode, CcError> {
    let config = build_config_from_flags(
        &planner,
        &implementer,
        &reviewer,
        base_branch.as_deref(),
        auto_merge,
        allow_merge_without_tests,
        allow_same_reviewer,
        planner_granularity.as_deref(),
        &test_command,
        codex_model.as_deref(),
        cursor_model.as_deref(),
        claude_code_model.as_deref(),
        stop_policy.as_deref(),
        blocking_severities.as_deref(),
        review_mode.as_deref(),
        review_facets.as_deref(),
    );
    let repo = resolve_repo_path(&repo);
    let pre = run_preflight(&repo, &config, false, false)?;
    require_preflight_ok(&pre)?;
    let id = task_id.unwrap_or_else(new_task_id);
    let base_branch = config.base_branch.clone();
    let state = create_initial_state(
        &id,
        &goal,
        &repo,
        &base_branch,
        &pre.base_commit,
        Some(config),
    );
    save_state(&state, state_root)?;
    if json {
        print_json(&json!({
            "ok": true,
            "task_id": id,
            "state_path": state_path(state_root, &id).display().to_string(),
            "base_commit": pre.base_commit,
        }));
    } else {
        println!("initialized task {id}");
    }
    Ok(ExitCode::Success)
}
