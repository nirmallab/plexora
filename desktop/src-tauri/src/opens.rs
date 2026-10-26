//! Files and links handed to the app from outside it.
//!
//! Four ways in, one queue: arguments at launch (a file association, "Open
//! with"), a second launch forwarding its arguments, a `plexora://` link, and
//! macOS's Opened event. Everything is queued and the page is told only that
//! something is waiting (`plexora://opens-pending`); it collects the queue
//! with `take_pending_opens`, on that ping and on every page load -- so an
//! open that arrives while a page is reloading is never lost.
//!
//! What the user did themselves -- double-clicked a file, chose Open With --
//! is queued at once. A link is not something the user did: any web page,
//! e-mail or document can carry one. So paths that arrive in a link wait for
//! the user to say yes in a native dialog before they reach the page (which
//! imports them through `/desktop/open`), and so does any path on another
//! computer (`\\server\share`), however it arrived, because merely looking at
//! one connects to that computer. Opening a registered project by name only
//! navigates, and is not asked about.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use crate::commands::HandedPaths;
use crate::windows;

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Open {
    /// Files or folders to open, or import and open.
    Paths { paths: Vec<String> },
    /// A registered project, by name.
    Project { name: String },
}

/// Where an open came from.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Source {
    /// The user's own gesture in the system: a double-click on an associated
    /// file, Open With, a path on the command line.
    User,
    /// A `plexora://` link, which anything that can show a link can carry.
    Link,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Incoming {
    pub open: Open,
    pub source: Source,
}

/// How long the same link counts as the same arrival. One click can arrive
/// twice -- through the deep-link plugin and macOS's Opened event, or the
/// plugin and a second launch's arguments -- and must ask only once.
const SAME_LINK: Duration = Duration::from_secs(10);

/// Paths listed in the dialog before "and N more".
const LISTED: usize = 5;

#[derive(Default)]
pub struct PendingOpens {
    items: Mutex<Vec<Open>>,
    window_ready: AtomicBool,
    recent_links: Mutex<Vec<(Open, Instant)>>,
}

/// A path on another computer: `\\server\share\...` (and every other form
/// that starts with two separators, `\\?\UNC\...` included). Touching one --
/// even to see whether it exists -- connects to that computer, and on
/// Windows offers it the user's sign-in.
pub fn is_network_path(path: &str) -> bool {
    let mut chars = path.chars();
    matches!((chars.next(), chars.next()), (Some('\\' | '/'), Some('\\' | '/')))
}

/// Whether a path handed over from outside is kept: a network path is kept
/// unexamined (it is asked about first), anything else must exist.
fn keep(path: &Path) -> bool {
    is_network_path(&path.to_string_lossy()) || path.exists()
}

/// Paths and links in a command line. Flags are skipped; relative paths are
/// made absolute against the directory the launch came from.
pub fn from_args(args: &[String], cwd: Option<&Path>) -> Vec<Incoming> {
    let mut paths = Vec::new();
    let mut incoming = Vec::new();
    for arg in args {
        if arg.starts_with('-') || arg.is_empty() {
            continue;
        }
        if arg.starts_with("plexora:") {
            if let Some(item) = arg.parse::<url::Url>().ok().as_ref().and_then(from_url) {
                incoming.push(item);
            }
            continue;
        }
        let path = Path::new(arg);
        let absolute = if path.is_absolute() || is_network_path(arg) {
            path.to_path_buf()
        } else {
            match cwd {
                Some(cwd) => cwd.join(path),
                None => path.to_path_buf(),
            }
        };
        if keep(&absolute) {
            paths.push(absolute.to_string_lossy().to_string());
        }
    }
    if !paths.is_empty() {
        incoming.insert(0, Incoming { open: Open::Paths { paths }, source: Source::User });
    }
    incoming
}

/// `plexora://open?project=<name>` or `plexora://open?path=<path>`, and the
/// `file://` URLs macOS hands over for a document opened in Finder. A link's
/// paths must be absolute and exist (or be network paths, asked about first).
pub fn from_url(url: &url::Url) -> Option<Incoming> {
    match url.scheme() {
        "file" => url
            .to_file_path()
            .ok()
            .filter(|p| keep(p))
            .map(|p| Incoming {
                open: Open::Paths { paths: vec![p.to_string_lossy().to_string()] },
                source: Source::User,
            }),
        "plexora" => {
            let pairs: Vec<(String, String)> = url.query_pairs().into_owned().collect();
            if let Some((_, name)) = pairs.iter().find(|(k, _)| k == "project") {
                return Some(Incoming { open: Open::Project { name: name.clone() }, source: Source::Link });
            }
            let paths: Vec<String> = pairs
                .into_iter()
                .filter(|(k, _)| k == "path")
                .map(|(_, v)| v)
                .filter(|v| {
                    let path = Path::new(v);
                    is_network_path(v) || (path.is_absolute() && path.exists())
                })
                .collect();
            (!paths.is_empty()).then_some(Incoming { open: Open::Paths { paths }, source: Source::Link })
        }
        _ => None,
    }
}

pub fn from_urls(urls: &[url::Url]) -> Vec<Incoming> {
    urls.iter().filter_map(from_url).collect()
}

/// Whether the user has to agree before this reaches the page.
pub fn needs_confirmation(item: &Incoming) -> bool {
    match &item.open {
        Open::Paths { paths } => item.source == Source::Link || paths.iter().any(|p| is_network_path(p)),
        Open::Project { .. } => false,
    }
}

/// What the confirmation dialog says.
pub fn confirmation_text(paths: &[String], source: Source) -> String {
    let mut listed: Vec<String> = paths.iter().take(LISTED).cloned().collect();
    if paths.len() > LISTED {
        listed.push(format!("and {} more", paths.len() - LISTED));
    }
    let mut text = match source {
        Source::Link => format!(
            "A link asks Plexora to open:\n\n{}\n\nPlexora will import it as a sample. Open it only if \
             you expected this link.",
            listed.join("\n")
        ),
        Source::User => format!("Plexora was asked to open:\n\n{}", listed.join("\n")),
    };
    if paths.iter().any(|p| is_network_path(p)) {
        text.push_str(
            "\n\nThis is on another computer on the network. Opening it connects to that computer.",
        );
    }
    text
}

/// Take what arrived: queue what the user did, ask about the rest.
pub fn deliver(app: &AppHandle, incoming: Vec<Incoming>) {
    let mut now = Vec::new();
    for item in incoming {
        if item.source == Source::Link && !first_sighting(app, &item.open) {
            continue;
        }
        if needs_confirmation(&item) {
            confirm(app, item);
        } else {
            now.push(item.open);
        }
    }
    accept(app, now);
}

fn first_sighting(app: &AppHandle, open: &Open) -> bool {
    let state = app.state::<PendingOpens>();
    let mut recent = state.recent_links.lock().unwrap();
    let now = Instant::now();
    recent.retain(|(_, at)| now.duration_since(*at) < SAME_LINK);
    if recent.iter().any(|(seen, _)| seen == open) {
        return false;
    }
    recent.push((open.clone(), now));
    true
}

/// Into the queue; paths become ones the page may read (`read_file`).
fn accept(app: &AppHandle, opens: Vec<Open>) {
    for open in &opens {
        if let Open::Paths { paths } = open {
            app.state::<HandedPaths>().add(paths.iter().map(Path::new));
        }
    }
    queue(app, opens);
}

fn confirm(app: &AppHandle, item: Incoming) {
    let Open::Paths { paths } = &item.open else {
        return accept(app, vec![item.open]);
    };
    let text = confirmation_text(paths, item.source);
    let for_answer = app.clone();
    let open = item.open.clone();
    let mut dialog = app
        .dialog()
        .message(text)
        .title("Open in Plexora?")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom("Open".into(), "Cancel".into()));
    if let Some(window) = windows::main_window(app) {
        windows::bring_to_front(&window);
        dialog = dialog.parent(&window);
    }
    dialog.show(move |yes| {
        if yes {
            accept(&for_answer, vec![open]);
        } else {
            log::info!("an open from outside was declined");
        }
    });
}

fn queue(app: &AppHandle, opens: Vec<Open>) {
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
    deliver(app, from_args(&args, Some(Path::new(&cwd))));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn here(name: &str) -> String {
        Path::new(env!("CARGO_MANIFEST_DIR")).join(name).to_string_lossy().to_string()
    }

    fn link(query: &str) -> url::Url {
        format!("plexora://open?{query}").parse().unwrap()
    }

    fn encoded(path: &str) -> String {
        url::form_urlencoded::byte_serialize(path.as_bytes()).collect()
    }

    #[test]
    fn links_name_a_project_or_paths() {
        let found = from_url(&link("project=tonsil%201")).unwrap();
        assert_eq!(found, Incoming { open: Open::Project { name: "tonsil 1".into() }, source: Source::Link });
        let (a, b) = (here("Cargo.toml"), here("build.rs"));
        let found = from_url(&link(&format!("path={}&path={}", encoded(&a), encoded(&b)))).unwrap();
        assert_eq!(found, Incoming { open: Open::Paths { paths: vec![a, b] }, source: Source::Link });
        let url: url::Url = "https://example.org".parse().unwrap();
        assert_eq!(from_url(&url), None);
    }

    #[test]
    fn a_link_keeps_only_absolute_paths_that_exist_and_network_ones() {
        let real = here("Cargo.toml");
        let missing = here("nope.tif");
        let query = format!(
            "path={}&path={}&path=Cargo.toml&path={}",
            encoded(&real),
            encoded(&missing),
            encoded(r"\\fileserver\lab\slide.svs")
        );
        let found = from_url(&link(&query)).unwrap();
        assert_eq!(found.open, Open::Paths { paths: vec![real, r"\\fileserver\lab\slide.svs".into()] });
        assert_eq!(from_url(&link(&format!("path={}", encoded(&missing)))), None);
        assert_eq!(from_url(&link("path=")), None);
    }

    #[test]
    fn links_with_paths_and_anything_on_the_network_are_asked_about() {
        let paths = |list: &[&str]| Open::Paths { paths: list.iter().map(|p| p.to_string()).collect() };
        let item = |open, source| Incoming { open, source };
        assert!(needs_confirmation(&item(paths(&["/data/a.tif"]), Source::Link)));
        assert!(!needs_confirmation(&item(paths(&["/data/a.tif"]), Source::User)));
        assert!(needs_confirmation(&item(paths(&["/data/a.tif", r"\\nas\x\b.svs"]), Source::User)));
        assert!(!needs_confirmation(&item(Open::Project { name: "tonsil".into() }, Source::Link)));
    }

    #[test]
    fn network_paths_are_every_double_separator_form() {
        for path in [r"\\server\share\a.svs", "//server/share/a.svs", r"\\?\UNC\server\share", r"\/server"] {
            assert!(is_network_path(path), "{path}");
        }
        for path in ["/data/a.svs", r"C:\data\a.svs", "a.svs", "", "/"] {
            assert!(!is_network_path(path), "{path}");
        }
    }

    #[test]
    fn the_dialog_names_the_paths_and_says_what_a_network_one_does() {
        let many: Vec<String> = (0..8).map(|i| format!("/data/{i}.tif")).collect();
        let text = confirmation_text(&many, Source::Link);
        assert!(text.contains("A link asks Plexora to open"));
        assert!(text.contains("/data/4.tif") && !text.contains("/data/5.tif"));
        assert!(text.contains("and 3 more"));
        assert!(!text.contains("network"));
        let text = confirmation_text(&[r"\\nas\lab\a.svs".to_string()], Source::User);
        assert!(text.contains(r"\\nas\lab\a.svs") && text.contains("connects to that computer"));
    }

    #[test]
    fn arguments_skip_flags_and_missing_paths() {
        let here = std::env::current_dir().unwrap();
        let args = vec!["--smoke-test".to_string(), "Cargo.toml".to_string(), "nope.tif".to_string()];
        let opens = from_args(&args, Some(&here));
        assert_eq!(opens.len(), 1);
        assert_eq!(opens[0].source, Source::User);
        match &opens[0].open {
            Open::Paths { paths } => {
                assert_eq!(paths.len(), 1);
                assert!(paths[0].ends_with("Cargo.toml"));
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_network_argument_is_kept_unexamined_and_a_link_argument_is_a_link() {
        let args = vec![r"\\nas\lab\a.svs".to_string(), "plexora://open?project=tonsil".to_string()];
        let opens = from_args(&args, Some(Path::new("/tmp")));
        assert_eq!(opens.len(), 2);
        assert_eq!(opens[0].open, Open::Paths { paths: vec![r"\\nas\lab\a.svs".into()] });
        assert!(needs_confirmation(&opens[0]));
        assert_eq!(opens[1].source, Source::Link);
    }
}
