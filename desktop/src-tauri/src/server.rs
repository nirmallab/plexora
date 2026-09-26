//! The Plexora server, run as a child process of the shell.
//!
//! The whole contract is on the child's three standard streams (see
//! `plexora/cli.py`, `_run_desktop`):
//!
//! * stdout: exactly one JSON line once the socket is bound ([`Ready`]);
//! * stderr: the server's log, copied to `server.log` and a ring buffer;
//! * stdin:  held open by us. Closing it is "please exit", and the OS closes
//!   it for us if the shell dies.
//!
//! Grandchildren (a data node, ssh, a tkinter dialog) are reaped with the
//! server: a Job Object with KILL_ON_JOB_CLOSE on Windows, the child's own
//! process group elsewhere, and PDEATHSIG on Linux.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// The ready-line protocol this shell speaks.
pub const PROTOCOL: u64 = 1;
/// How long a quiet server gets to print its ready line.
const READY_TIMEOUT: Duration = Duration::from_secs(90);
/// ... and a busy one, still writing to stderr (a first launch under an
/// antivirus scanner can take minutes to import the scientific stack).
const READY_TIMEOUT_BUSY: Duration = Duration::from_secs(240);
/// How long the server gets to exit after its stdin closes.
const STOP_GRACE: Duration = Duration::from_secs(5);
const RING_LINES: usize = 200;
const LOG_ROTATE_BYTES: u64 = 5 * 1024 * 1024;

/// Environment variables that would point the embedded interpreter at
/// somebody else's Python, or at libraries it did not ship with.
const SCRUBBED: &[&str] = &[
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONEXECUTABLE",
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PLEXORA_AUTH_TOKEN",
    "PLEXORA_BASE_URL", "PLEXORA_HOST", "PLEXORA_NOTEBOOK_MODE",
];

/// The one line `plexora --desktop` prints once it is listening.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Ready {
    pub event: String,
    pub protocol: u64,
    pub url: String,
    pub origin: String,
    pub host: String,
    pub port: u16,
    pub token: String,
    pub pid: u32,
    pub version: String,
    #[serde(default)]
    pub data_root: String,
    #[serde(default)]
    pub settings_path: String,
}

/// How to start the server.
#[derive(Clone, Debug)]
pub enum Launch {
    /// The runtime shipped inside the app: isolated mode, nothing inherited.
    Runtime(PathBuf),
    /// A developer's interpreter (`PLEXORA_DESKTOP_PYTHON`), for running the
    /// shell against a source checkout. Not isolated, so `-m plexora` can
    /// find the checkout from `cwd`.
    Dev { python: PathBuf, cwd: Option<PathBuf> },
}

impl Launch {
    pub fn python(&self) -> &Path {
        match self {
            Launch::Runtime(python) => python,
            Launch::Dev { python, .. } => python,
        }
    }
}

#[derive(Debug)]
pub enum StartError {
    NotFound(Vec<PathBuf>),
    Spawn(String),
    Timeout,
    Exited(Option<i32>),
    BadReadyLine(String),
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StartError::NotFound(looked) => write!(
                f,
                "The Python runtime is missing from this installation. Looked in:\n{}",
                looked.iter().map(|p| format!("  {}", p.display())).collect::<Vec<_>>().join("\n")
            ),
            StartError::Spawn(e) => write!(f, "Could not start the Plexora server: {e}"),
            StartError::Timeout => write!(f, "The Plexora server did not finish starting in time."),
            StartError::Exited(Some(code)) => {
                write!(f, "The Plexora server stopped while starting (exit code {code}).")
            }
            StartError::Exited(None) => write!(f, "The Plexora server stopped while starting."),
            StartError::BadReadyLine(line) => {
                write!(f, "The Plexora server answered in a way this app does not understand:\n{line}")
            }
        }
    }
}

// -- finding the runtime ---------------------------------------------------

/// Every place the runtime can be, most specific first. The resource
/// directory is right for an installed app; the rest cover a bare executable
/// in a build tree, an extracted .deb and an AppImage, where Tauri's own idea
/// of the resource directory is not where the bundler put the files.
pub fn runtime_dir_candidates(resource_dir: Option<PathBuf>) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(dir) = std::env::var("PLEXORA_DESKTOP_RUNTIME") {
        out.push(PathBuf::from(dir));
    }
    if let Some(resources) = resource_dir {
        out.push(resources.join("runtime"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            out.push(dir.join("runtime"));
            out.push(dir.join("../Resources/runtime"));
            out.push(dir.join("../lib/Plexora/runtime"));
        }
    }
    if let Ok(appdir) = std::env::var("APPDIR") {
        out.push(PathBuf::from(appdir).join("usr/lib/Plexora/runtime"));
    }
    if cfg!(target_os = "linux") {
        out.push(PathBuf::from("/usr/lib/Plexora/runtime"));
    }
    out
}

pub fn python_in(runtime: &Path) -> PathBuf {
    if cfg!(windows) {
        runtime.join("python.exe")
    } else {
        runtime.join("bin").join("python3")
    }
}

pub fn find_launch(resource_dir: Option<PathBuf>) -> Result<Launch, StartError> {
    if let Ok(python) = std::env::var("PLEXORA_DESKTOP_PYTHON") {
        let cwd = std::env::var("PLEXORA_DESKTOP_CWD").ok().map(PathBuf::from);
        return Ok(Launch::Dev { python: PathBuf::from(python), cwd });
    }
    let candidates = runtime_dir_candidates(resource_dir);
    candidates
        .iter()
        .map(|dir| python_in(dir))
        .find(|python| python.is_file())
        .map(Launch::Runtime)
        .ok_or(StartError::NotFound(candidates))
}

// -- the command -------------------------------------------------------------

pub fn build_command(launch: &Launch, log_path: Option<&Path>) -> Command {
    let mut cmd = Command::new(launch.python());
    let cwd = match launch {
        Launch::Runtime(_) => {
            // -I: no user site-packages, no cwd on sys.path, every PYTHON*
            // variable ignored. That is also why -u and -X utf8 are flags.
            cmd.arg("-I");
            None
        }
        Launch::Dev { cwd, .. } => cwd.clone(),
    };
    cmd.args(["-B", "-u", "-X", "utf8", "-m", "plexora", "--desktop"]);

    for name in SCRUBBED {
        cmd.env_remove(name);
    }
    for (name, _) in std::env::vars_os() {
        let name = name.to_string_lossy().to_string();
        if name.starts_with("PYTHON") || name.starts_with("DYLD_") {
            cmd.env_remove(&name);
        }
    }
    // An AppImage's AppRun exports these for WebKitGTK; leaked into Python
    // they load the bundle's libraries in place of the wheels' own.
    #[cfg(target_os = "linux")]
    {
        cmd.env_remove("LD_LIBRARY_PATH");
        cmd.env_remove("LD_PRELOAD");
    }
    cmd.env("PYTHONNOUSERSITE", "1");
    cmd.env("PLEXORA_DESKTOP", "1");
    if let Some(log) = log_path {
        cmd.env("PLEXORA_DESKTOP_LOG", log);
    }
    cmd.current_dir(cwd.unwrap_or_else(std::env::temp_dir));
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
        #[cfg(target_os = "linux")]
        unsafe {
            // Delivered when the THREAD that spawned the child exits, not the
            // process -- which is why the server is spawned from the thread
            // that then monitors it for its whole life (see `start`).
            cmd.pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                Ok(())
            });
        }
    }
    cmd
}

// -- Windows Job Object ----------------------------------------------------

#[cfg(windows)]
mod job {
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// Every process in it dies when the last handle closes -- which is
    /// when the shell exits, however it exits.
    pub struct Job(HANDLE);
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    impl Job {
        pub fn kill_on_close() -> Option<Job> {
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return None;
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const core::ffi::c_void,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    CloseHandle(handle);
                    return None;
                }
                Some(Job(handle))
            }
        }

        /// False inside a job that forbids nesting (some CI runners). Not
        /// fatal: the stdin tie still ends the server.
        pub fn assign(&self, child: &std::process::Child) -> bool {
            use std::os::windows::io::AsRawHandle;
            unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) != 0 }
        }

        pub fn terminate(&self) {
            unsafe {
                TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

// -- the running server ------------------------------------------------------

struct Running {
    child: Child,
    stdin: Option<ChildStdin>,
    #[cfg(windows)]
    job: Option<job::Job>,
}

/// Shared between the thread that owns the server and everything that asks
/// about it.
#[derive(Default)]
pub struct Server {
    running: Mutex<Option<Running>>,
    ready: Mutex<Option<Ready>>,
    expected_exit: AtomicBool,
    ring: Arc<Mutex<VecDeque<String>>>,
    last_stderr_ms: Arc<AtomicU64>,
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

fn open_log(path: Option<&Path>) -> Option<File> {
    let path = path?;
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if std::fs::metadata(path).map(|m| m.len() > LOG_ROTATE_BYTES).unwrap_or(false) {
        let _ = std::fs::rename(path, path.with_extension("log.1"));
    }
    OpenOptions::new().create(true).append(true).open(path).ok()
}

impl Server {
    pub fn ready(&self) -> Option<Ready> {
        self.ready.lock().unwrap().clone()
    }

    pub fn tail(&self, lines: usize) -> String {
        let ring = self.ring.lock().unwrap();
        let skip = ring.len().saturating_sub(lines);
        ring.iter().skip(skip).cloned().collect::<Vec<_>>().join("\n")
    }

    pub fn exit_expected(&self) -> bool {
        self.expected_exit.load(Ordering::SeqCst)
    }

    /// Spawn the server on THIS thread and wait for its ready line.
    ///
    /// The calling thread should go on to call [`Server::monitor`]: on Linux
    /// the child is tied to the lifetime of the thread that spawned it.
    pub fn start(
        &self,
        launch: &Launch,
        log_path: Option<&Path>,
        progress: &dyn Fn(&str),
    ) -> Result<Ready, StartError> {
        self.expected_exit.store(false, Ordering::SeqCst);
        *self.ready.lock().unwrap() = None;
        let mut child = build_command(launch, log_path)
            .spawn()
            .map_err(|e| StartError::Spawn(format!("{e} ({})", launch.python().display())))?;

        #[cfg(windows)]
        let job = job::Job::kill_on_close().filter(|job| job.assign(&child));

        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");

        let (tx, rx) = mpsc::channel::<String>();
        std::thread::Builder::new()
            .name("plexora-server-stdout".into())
            .spawn(move || {
                let mut reader = BufReader::new(stdout);
                let mut first = String::new();
                if reader.read_line(&mut first).unwrap_or(0) > 0 {
                    let _ = tx.send(first);
                }
                // The server promises nothing else ever reaches stdout; if
                // something does, it belongs in the log, not on the floor.
                let mut line = String::new();
                loop {
                    line.clear();
                    match reader.read_line(&mut line) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => log::warn!("server stdout: {}", line.trim_end()),
                    }
                }
            })
            .map_err(|e| StartError::Spawn(e.to_string()))?;

        let ring = self.ring.clone();
        let last = self.last_stderr_ms.clone();
        last.store(now_ms(), Ordering::SeqCst);
        let mut log_file = open_log(log_path);
        std::thread::Builder::new()
            .name("plexora-server-stderr".into())
            .spawn(move || {
                let mut reader = BufReader::new(stderr);
                let mut buffer = Vec::new();
                loop {
                    buffer.clear();
                    match reader.read_until(b'\n', &mut buffer) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            last.store(now_ms(), Ordering::SeqCst);
                            if let Some(file) = log_file.as_mut() {
                                let _ = file.write_all(&buffer);
                            }
                            let text = String::from_utf8_lossy(&buffer).trim_end().to_string();
                            let mut ring = ring.lock().unwrap();
                            if ring.len() >= RING_LINES {
                                ring.pop_front();
                            }
                            ring.push_back(text);
                        }
                    }
                }
            })
            .map_err(|e| StartError::Spawn(e.to_string()))?;

        let started = Instant::now();
        let mut announced = false;
        let result = loop {
            match rx.recv_timeout(Duration::from_millis(500)) {
                Ok(line) => break parse_ready(&line),
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if let Ok(Some(status)) = child.try_wait() {
                        break Err(StartError::Exited(status.code()));
                    }
                    let elapsed = started.elapsed();
                    let busy = now_ms().saturating_sub(self.last_stderr_ms.load(Ordering::SeqCst))
                        < 10_000;
                    if elapsed > Duration::from_secs(15) && !announced {
                        progress("Loading the analysis libraries…");
                        announced = true;
                    }
                    if elapsed > READY_TIMEOUT && !(busy && elapsed < READY_TIMEOUT_BUSY) {
                        break Err(StartError::Timeout);
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    let deadline = Instant::now() + Duration::from_secs(5);
                    let code = loop {
                        match child.try_wait() {
                            Ok(Some(status)) => break status.code(),
                            _ if Instant::now() > deadline => break None,
                            _ => std::thread::sleep(Duration::from_millis(50)),
                        }
                    };
                    break Err(StartError::Exited(code));
                }
            }
        };

        let running = Running {
            child,
            stdin,
            #[cfg(windows)]
            job,
        };
        match result {
            Ok(ready) => {
                *self.running.lock().unwrap() = Some(running);
                *self.ready.lock().unwrap() = Some(ready.clone());
                Ok(ready)
            }
            Err(error) => {
                self.expected_exit.store(true, Ordering::SeqCst);
                kill(running);
                Err(error)
            }
        }
    }

    /// Block until the server exits; returns its exit code.
    pub fn monitor(&self) -> Option<i32> {
        loop {
            {
                let mut guard = self.running.lock().unwrap();
                match guard.as_mut() {
                    None => return None,
                    Some(running) => {
                        if let Ok(Some(status)) = running.child.try_wait() {
                            guard.take();
                            return status.code();
                        }
                    }
                }
            }
            std::thread::sleep(Duration::from_millis(250));
        }
    }

    /// Ask the server to stop, then insist. True if it stopped on its own.
    pub fn shutdown(&self) -> bool {
        self.expected_exit.store(true, Ordering::SeqCst);
        let taken = self.running.lock().unwrap().take();
        let Some(mut running) = taken else { return true };
        drop(running.stdin.take());
        let deadline = Instant::now() + STOP_GRACE;
        loop {
            match running.child.try_wait() {
                Ok(Some(status)) => {
                    // Reap anything it left behind (a data node, ssh).
                    #[cfg(windows)]
                    if let Some(job) = running.job.as_ref() {
                        job.terminate();
                    }
                    #[cfg(unix)]
                    unsafe {
                        libc::killpg(running.child.id() as i32, libc::SIGTERM);
                    }
                    return status.success();
                }
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(100))
                }
                _ => {
                    log::warn!("the server did not exit within {STOP_GRACE:?}; stopping it");
                    kill(running);
                    return false;
                }
            }
        }
    }
}

fn kill(mut running: Running) {
    drop(running.stdin.take());
    #[cfg(unix)]
    unsafe {
        let pgid = running.child.id() as i32;
        libc::killpg(pgid, libc::SIGTERM);
        for _ in 0..20 {
            if matches!(running.child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        libc::killpg(pgid, libc::SIGKILL);
    }
    #[cfg(windows)]
    if let Some(job) = running.job.as_ref() {
        job.terminate();
    }
    let _ = running.child.kill();
    let _ = running.child.wait();
}

pub fn parse_ready(line: &str) -> Result<Ready, StartError> {
    let ready: Ready = serde_json::from_str(line.trim())
        .map_err(|_| StartError::BadReadyLine(line.trim().chars().take(300).collect()))?;
    if ready.event != "ready" || ready.protocol != PROTOCOL {
        return Err(StartError::BadReadyLine(line.trim().chars().take(300).collect()));
    }
    Ok(ready)
}

// -- a very small HTTP client ------------------------------------------------
//
// Two requests, both to our own loopback server: `/health` and
// `/desktop/info`. Not worth an HTTP stack.

pub fn http_get(origin: &str, path: &str, timeout: Duration) -> Result<(u16, String), String> {
    let url = url::Url::parse(origin).map_err(|e| e.to_string())?;
    let host = url.host_str().ok_or("no host")?.to_string();
    let port = url.port_or_known_default().ok_or("no port")?;
    let addr: SocketAddr = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or("unresolvable")?;
    let mut stream = TcpStream::connect_timeout(&addr, timeout).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(timeout)).ok();
    stream.set_write_timeout(Some(timeout)).ok();
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    )
    .map_err(|e| e.to_string())?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&raw);
    let (head, body) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or("no status line")?;
    let chunked = head.lines().any(|line| {
        let line = line.to_ascii_lowercase();
        line.starts_with("transfer-encoding:") && line.contains("chunked")
    });
    let body = if chunked { dechunk(body) } else { body.to_string() };
    Ok((status, body))
}

fn dechunk(body: &str) -> String {
    let mut out = String::new();
    let mut rest = body;
    while let Some((size, after)) = rest.split_once("\r\n") {
        let size = usize::from_str_radix(size.trim(), 16).unwrap_or(0);
        if size == 0 || after.len() < size {
            break;
        }
        out.push_str(&after[..size]);
        rest = after[size..].trim_start_matches("\r\n");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_ready_line_parses() {
        let line = r#"{"data_root":"C:\\d","event":"ready","host":"127.0.0.1","log":{"path":null,"stream":"stderr"},"origin":"http://127.0.0.1:8420","pid":7,"port":8420,"protocol":1,"settings_path":"","shutdown":["stdin"],"token":"t","url":"http://127.0.0.1:8420/?token=t","version":"0.0.23"}"#;
        let ready = parse_ready(line).unwrap();
        assert_eq!(ready.port, 8420);
        assert_eq!(ready.token, "t");
    }

    #[test]
    fn another_protocol_is_refused() {
        let line = r#"{"event":"ready","protocol":2,"url":"u","origin":"o","host":"h","port":1,"token":"t","pid":1,"version":"v"}"#;
        assert!(matches!(parse_ready(line), Err(StartError::BadReadyLine(_))));
        assert!(matches!(parse_ready("Serving on ..."), Err(StartError::BadReadyLine(_))));
    }

    #[test]
    fn chunked_bodies_are_reassembled() {
        assert_eq!(dechunk("4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"), "Wikipedia");
    }
}
