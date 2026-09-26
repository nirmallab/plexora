//! Files and links handed to the app from outside it.
//!
//! Four ways in, one queue: arguments at launch (a file association, "Open
//! with"), a second launch forwarding its arguments, a `plexora://` link, and
//! macOS's Opened event. Everything is queued and the page is told only that
//! something is waiting (`plexora://opens-pending`); it collects the queue
//! with `take_pending_opens`, on that ping and on every page load -- so an
//! open that arrives while a page is reloading is never lost.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use crate::windows;

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Open {
    /// Files or folders to open, or import and open.
    Paths { paths: Vec<String> },
    /// A registered project, by name.
    Project { name: String },
}

#[derive(Default)]
pub struct PendingOpens {
    items: Mutex<Vec<Open>>,
    window_ready: AtomicBool,
}

/// Paths and links in a command line. Flags are skipped; relative paths are
/// made absolute against the directory the launch came from.
pub fn from_args(args: &[String], cwd: Option<&Path>) -> Vec<Open> {
    let mut paths = Vec::new();
    let mut opens = Vec::new();
    for arg in args {
        if arg.starts_with('-') || arg.is_empty() {
            continue;
        }
        if arg.starts_with("plexora:") {
            if let Some(open) = arg.parse::<url::Url>().ok().as_ref().and_then(from_url) {
                opens.push(open);
            }
            continue;
        }
        let path = Path::new(arg);
        let absolute = if path.is_absolute() {
            path.to_path_buf()
        } else {
            match cwd {
                Some(cwd) => cwd.join(path),
                None => path.to_path_buf(),
            }
        };
        if absolute.exists() {
            paths.push(absolute.to_string_lossy().to_string());
        }
    }
    if !paths.is_empty() {
        opens.insert(0, Open::Paths { paths });
    }
    opens
}

/// `plexora://open?project=<name>` or `plexora://open?path=<path>`, and the
/// `file://` URLs macOS hands over for a document opened in Finder.
pub fn from_url(url: &url::Url) -> Option<Open> {
    match url.scheme() {
        "file" => url
            .to_file_path()
            .ok()
            .map(|p| Open::Paths { paths: vec![p.to_string_lossy().to_string()] }),
        "plexora" => {
            let pairs: Vec<(String, String)> = url.query_pairs().into_owned().collect();
            if let Some((_, name)) = pairs.iter().find(|(k, _)| k == "project") {
                return Some(Open::Project { name: name.clone() });
            }
            let paths: Vec<String> =
                pairs.into_iter().filter(|(k, _)| k == "path").map(|(_, v)| v).collect();
            (!paths.is_empty()).then_some(Open::Paths { paths })
        }
        _ => None,
    }
}

pub fn queue(app: &AppHandle, opens: Vec<Open>) {
    if opens.is_empty() {
        return;
    }
    let state = app.state::<PendingOpens>();
    state.items.lock().unwrap().extend(opens);
    if state.window_ready.load(Ordering::SeqCst) {
        ping(app);
    }
}

pub fn window_ready(app: &AppHandle) {
    app.state::<PendingOpens>().window_ready.store(true, Ordering::SeqCst);
    ping(app);
}

fn ping(app: &AppHandle) {
    if let Some(window) = windows::main_window(app) {
        let _ = app.emit_to(window.label(), "plexora://opens-pending", ());
    }
}

pub fn take(app: &AppHandle) -> Vec<Open> {
    std::mem::take(&mut *app.state::<PendingOpens>().items.lock().unwrap())
}

/// A second launch: bring the running app forward and give it the files.
pub fn from_second_instance(app: &AppHandle, argv: Vec<String>, cwd: String) {
    if let Some(window) = windows::main_window(app) {
        windows::bring_to_front(&window);
    }
    let args: Vec<String> = argv.into_iter().skip(1).collect();
    queue(app, from_args(&args, Some(Path::new(&cwd))));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn links_name_a_project_or_paths() {
        let url: url::Url = "plexora://open?project=tonsil%201".parse().unwrap();
        assert_eq!(from_url(&url), Some(Open::Project { name: "tonsil 1".into() }));
        let url: url::Url = "plexora://open?path=%2Fdata%2Fa.tif&path=%2Fdata%2Fb.csv".parse().unwrap();
        assert_eq!(from_url(&url), Some(Open::Paths { paths: vec!["/data/a.tif".into(), "/data/b.csv".into()] }));
        let url: url::Url = "https://example.org".parse().unwrap();
        assert_eq!(from_url(&url), None);
    }

    #[test]
    fn arguments_skip_flags_and_missing_paths() {
        let here = std::env::current_dir().unwrap();
        let args = vec!["--smoke-test".to_string(), "Cargo.toml".to_string(), "nope.tif".to_string()];
        let opens = from_args(&args, Some(&here));
        assert_eq!(opens.len(), 1);
        match &opens[0] {
            Open::Paths { paths } => {
                assert_eq!(paths.len(), 1);
                assert!(paths[0].ends_with("Cargo.toml"));
            }
            other => panic!("{other:?}"),
        }
    }
}
