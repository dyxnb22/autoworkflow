//! cc-loop core library — role-separated delivery engine.

pub mod budgets;
pub mod config;
pub mod error;
pub mod eval;
pub mod events;
pub mod export;
pub mod failure;
pub mod git;
pub mod graph;
pub mod inspect;
pub mod observability;
pub mod orchestrator;
pub mod parallel;
pub mod paths;
pub mod planner_direct;
pub mod preflight;
pub mod process;
pub mod provider;
pub mod recovery;
pub mod repair;
pub mod report;
pub mod review_context;
pub mod runner;
pub mod state;
pub mod summary;
pub mod version;

pub use config::{
    default_config, distinct_reviewer_satisfied, format_distinct_reviewer_error, merge_config,
    role_provider_identity, LoopConfig,
};
pub use error::{CcError, ExitCode};
pub use state::{
    create_initial_state, load_state, save_state, AttemptPhase, AttemptRecord, TaskState,
    TaskStatus,
};
pub use version::{CC_LOOP_VERSION, INTEGRATION_SCHEMA_VERSION, SUMMARY_SCHEMA_VERSION};
