//! Timeout-safe subprocess execution with process groups (shell=false).

use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use nix::sys::signal::{killpg, Signal};
use nix::unistd::{setsid, Pid};

use crate::error::{CcError, Result};

const DEFAULT_KILL_GRACE_MS: u64 = 500;

static ACTIVE_PGID: OnceLock<Mutex<Option<i32>>> = OnceLock::new();

fn active_pgid() -> &'static Mutex<Option<i32>> {
    ACTIVE_PGID.get_or_init(|| Mutex::new(None))
}

pub fn get_active_subprocess_pgid() -> Option<i32> {
    *active_pgid().lock().unwrap()
}

fn set_active_pgid(pid: Option<i32>) {
    *active_pgid().lock().unwrap() = pid;
}

#[derive(Debug, Clone)]
pub struct RunResult {
    pub args: Vec<String>,
    pub returncode: i32,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
    pub killed: bool,
    pub interrupted: bool,
    pub hung: bool,
    pub duration_seconds: f64,
    pub pid: Option<u32>,
}

fn pgid_alive(pgid: i32) -> bool {
    let rc = unsafe { libc::kill(-pgid, 0) };
    if rc == 0 {
        return true;
    }
    let err = std::io::Error::last_os_error().raw_os_error();
    matches!(err, Some(libc::EPERM))
}

fn terminate_process_group(pgid: i32, grace: Duration) -> bool {
    let _ = killpg(Pid::from_raw(pgid), Signal::SIGTERM);
    if grace.is_zero() {
        let _ = killpg(Pid::from_raw(pgid), Signal::SIGKILL);
        return true;
    }
    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        if !pgid_alive(pgid) {
            return false;
        }
        thread::sleep(Duration::from_millis(50));
    }
    let _ = killpg(Pid::from_raw(pgid), Signal::SIGKILL);
    true
}

pub fn kill_active_subprocess_group(grace: Duration) -> bool {
    let Some(pgid) = get_active_subprocess_pgid() else {
        return false;
    };
    terminate_process_group(pgid, grace)
}

/// Run argv with timeout. Never uses a shell.
pub fn run_with_timeout(
    args: &[String],
    cwd: Option<&std::path::Path>,
    timeout: Duration,
    env: &[(&str, &str)],
) -> Result<RunResult> {
    run_with_timeout_stdin(args, cwd, timeout, env, None)
}

pub fn run_with_timeout_stdin(
    args: &[String],
    cwd: Option<&std::path::Path>,
    timeout: Duration,
    env: &[(&str, &str)],
    stdin_data: Option<&str>,
) -> Result<RunResult> {
    if args.is_empty() {
        return Err(CcError::config("empty argv"));
    }
    let started = Instant::now();
    let mut cmd = Command::new(&args[0]);
    cmd.args(&args[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if stdin_data.is_some() {
        cmd.stdin(Stdio::piped());
    } else {
        cmd.stdin(Stdio::null());
    }
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    for (k, v) in env {
        cmd.env(k, v);
    }

    unsafe {
        cmd.pre_exec(|| match setsid() {
            Ok(_) => Ok(()),
            Err(e) => Err(std::io::Error::from_raw_os_error(e as i32)),
        });
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| CcError::execution(format!("failed to spawn {}: {e}", args[0])))?;
    let pid = child.id();
    set_active_pgid(Some(pid as i32));

    if let Some(data) = stdin_data {
        if let Some(mut stdin) = child.stdin.take() {
            let _ = stdin.write_all(data.as_bytes());
        }
    }

    let mut stdout_pipe = child.stdout.take();
    let mut stderr_pipe = child.stderr.take();
    let stdout_handle = thread::spawn(move || {
        let mut buf = String::new();
        if let Some(ref mut r) = stdout_pipe {
            let _ = r.read_to_string(&mut buf);
        }
        buf
    });
    let stderr_handle = thread::spawn(move || {
        let mut buf = String::new();
        if let Some(ref mut r) = stderr_pipe {
            let _ = r.read_to_string(&mut buf);
        }
        buf
    });

    let mut timed_out = false;
    let mut killed = false;
    let mut hung = false;
    let returncode;

    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                returncode = status.code().unwrap_or(-1);
                break;
            }
            Ok(None) => {
                if started.elapsed() >= timeout {
                    timed_out = true;
                    killed = terminate_process_group(
                        pid as i32,
                        Duration::from_millis(DEFAULT_KILL_GRACE_MS),
                    );
                    returncode = match child.wait() {
                        Ok(status) => status.code().unwrap_or(-1),
                        Err(_) => -1,
                    };
                    if pgid_alive(pid as i32) {
                        hung = true;
                    }
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                set_active_pgid(None);
                return Err(CcError::execution(format!("wait failed: {e}")));
            }
        }
    }

    let stdout = stdout_handle.join().unwrap_or_default();
    let stderr = stderr_handle.join().unwrap_or_default();
    set_active_pgid(None);

    Ok(RunResult {
        args: args.to_vec(),
        returncode: if timed_out { -1 } else { returncode },
        stdout,
        stderr,
        timed_out,
        killed,
        interrupted: false,
        hung,
        duration_seconds: started.elapsed().as_secs_f64(),
        pid: Some(pid),
    })
}
