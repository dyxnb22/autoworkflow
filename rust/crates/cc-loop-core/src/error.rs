//! Stable exit codes and typed errors.

use thiserror::Error;

/// Stable CLI exit codes (see docs/INTEGRATION.md).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(i32)]
pub enum ExitCode {
    Success = 0,
    UserOrConfig = 1,
    ExecutionFailure = 2,
}

impl From<ExitCode> for i32 {
    fn from(value: ExitCode) -> Self {
        value as i32
    }
}

#[derive(Debug, Error)]
pub enum CcError {
    #[error("{0}")]
    User(String),

    #[error("{0}")]
    Config(String),

    #[error("{0}")]
    Execution(String),

    #[error("git: {0}")]
    Git(String),

    #[error("provider: {0}")]
    Provider(String),

    #[error("io: {0}")]
    Io(#[from] std::io::Error),

    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
}

impl CcError {
    pub fn exit_code(&self) -> ExitCode {
        match self {
            Self::User(_) | Self::Config(_) => ExitCode::UserOrConfig,
            Self::Execution(_) | Self::Git(_) | Self::Provider(_) => ExitCode::ExecutionFailure,
            Self::Io(_) | Self::Json(_) => ExitCode::UserOrConfig,
        }
    }

    pub fn user(msg: impl Into<String>) -> Self {
        Self::User(msg.into())
    }

    pub fn config(msg: impl Into<String>) -> Self {
        Self::Config(msg.into())
    }

    pub fn execution(msg: impl Into<String>) -> Self {
        Self::Execution(msg.into())
    }

    pub fn git(msg: impl Into<String>) -> Self {
        Self::Git(msg.into())
    }

    pub fn provider(msg: impl Into<String>) -> Self {
        Self::Provider(msg.into())
    }
}

pub type Result<T> = std::result::Result<T, CcError>;
