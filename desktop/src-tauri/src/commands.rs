//! What the page can ask the shell for, through `window.PlexoraDesktop`.
//!
//! Every native feature is an app command here -- dialogs, notifications,
//! the clipboard -- rather than a plugin's JavaScript API, so the page needs
//! no plugin permissions at all and the whole surface is this one file.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::Serialize;
use tauri::ipc::{InvokeBody, Request, Response};
use tauri::{AppHandle, Manager, Runtime, Webview, WebviewWindow};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

use crate::{opens, windows, ServerState};

/// Largest file `read_file` hands to the page. Pixels are read by the server
/// from disk; this is for the odd dropped PNG, not a whole slide.
const READ_LIMIT: u64 = 512 * 1024 * 1024;

// -- who may ask ---------------------------------------------------------------

/// Whether a page at `page` is the app's own server, at `server_origin`.
pub fn is_server_page(page: &url::Url, server_origin: &str) -> bool {
    url::Url::parse(server_origin).map(|server| page.origin() == server.origin()).unwrap_or(false)
}

/// Every command here is for the page the shell's own server serves, and for
/// nothing else on loopback. The capability (capabilities/main.json) can only
/// say "any port on 127.0.0.1", because the server picks its port at launch;
/// this says "the port this launch's server is on". Run before every command
/// by the handler in lib.rs, so a command cannot forget it.
///
/// The webview's own address is what Tauri's ACL itself resolves against, and
/// sub-frames never reach a command at all: Tauri's per-launch invoke key is
/// injected into the top frame only.
pub fn check_caller<R: Runtime>(webview: &Webview<R>) -> Result<(), String> {
    let page = webview.url().map_err(|e| e.to_string())?;
    let server = webview.state::<ServerState>().0.ready().map(|ready| ready.origin);
    match server {
        Some(origin) if is_server_page(&page, &origin) => Ok(()),
        _ => Err(format!("{} is not this app's page", page.origin().ascii_serialization())),
    }
}

// -- paths the shell handed the page ------------------------------------------

/// How many handed paths are remembered; the oldest go first.
const HANDED_LIMIT: usize = 4096;

/// Paths the shell itself gave the page this run: picked in its file dialogs,
/// dropped on a window, opened from Explorer or Finder (or a link the user
/// confirmed), or saved through its Save dialogs. `read_file` reads only
/// these, and `reveal_path` reaches a network location only through them.
#[derive(Default)]
pub struct HandedPaths(Mutex<VecDeque<PathBuf>>);

impl HandedPaths {
    pub fn add<P: AsRef<Path>>(&self, paths: impl IntoIterator<Item = P>) {
        // Resolved outside the lock: a slow disk must not hold up a command.
        let mut forms = Vec::new();
        for path in paths {
            let path = path.as_ref().to_path_buf();
            if let Ok(real) = std::fs::canonicalize(&path) {
                if real != path {
                    forms.push(real);
                }
            }
            forms.push(path);
        }
        let mut held = self.0.lock().unwrap();
        for form in forms {
            if !held.contains(&form) {
                held.push_back(form);
            }
        }
        while held.len() > HANDED_LIMIT {
            held.pop_front();
        }
    }

    /// Exactly this path text was handed over.
    pub fn holds(&self, path: &Path) -> bool {
        self.0.lock().unwrap().iter().any(|held| held == path)
    }

    /// This path, or the file it resolves to, was handed over. A network
    /// path must match as given: resolving it would connect to that
    /// computer, which is what a page naming one may be after.
    pub fn allows(&self, path: &Path) -> bool {
        if self.holds(path) {
            return true;
        }
        if crate::opens::is_network_path(&path.to_string_lossy()) {
            return false;
        }
        match std::fs::canonicalize(path) {
            Ok(real) => self.holds(&real),
            Err(_) => false,
        }
    }
}

fn hand<R: Runtime>(app: &AppHandle<R>, paths: &[String]) {
    app.state::<HandedPaths>().add(paths.iter().map(PathBuf::from));
}

/// What `reveal_path` will show: something that exists, by an absolute path,
/// and a network location only when the shell handed it over itself.
fn revealable(handed: &HandedPaths, path: &Path) -> Result<(), String> {
    if !path.is_absolute() {
        return Err(format!("{} is not an absolute path", path.display()));
    }
    if crate::opens::is_network_path(&path.to_string_lossy()) && !handed.holds(path) {
        return Err(format!("{} is on the network and was not opened here", path.display()));
    }
    if !path.exists() {
        return Err(format!("{} does not exist", path.display()));
    }
    Ok(())
}

/// The same filters as the server's own dialogs (native_dialog.py), so a
/// Browse button offers the same file types in a browser tab and in the app.
fn filter_for(name: &str) -> Option<(&'static str, &'static [&'static str])> {
    match name {
        "image" => Some(("Image files", &["tif", "tiff", "svs", "ndpi", "scn", "bif", "qptiff",
                                           "dcm", "png", "jpg", "jpeg", "mrxs"])),
        "csv" => Some(("CSV files", &["csv"])),
        "h5ad" => Some(("AnnData files", &["h5ad"])),
        "data" => Some(("Single-cell data", &["csv", "tsv", "txt", "parquet", "h5ad"])),
        "channels" => Some(("Channel names", &["csv", "tsv", "txt", "xlsx", "xlsm"])),
        _ => None,
    }
}

#[tauri::command]
pub async fn pick_paths(
    app: AppHandle,
    window: WebviewWindow,
    mode: Option<String>,
    multiple: Option<bool>,
    filter: Option<String>,
    title: Option<String>,
    default_path: Option<String>,
) -> Result<Vec<String>, String> {
    let mode = mode.unwrap_or_else(|| "file".into());
    let multiple = multiple.unwrap_or(false);
    let mut dialog = window.dialog().file().set_parent(&window);
    if let Some(title) = title {
        dialog = dialog.set_title(title);
    }
    if let Some(start) = default_path.filter(|p| !p.is_empty()) {
        let start = PathBuf::from(start);
        let dir = if start.is_dir() { Some(start) } else { start.parent().map(|p| p.to_path_buf()) };
        if let Some(dir) = dir.filter(|d| d.is_dir()) {
            dialog = dialog.set_directory(dir);
        }
    }
    if mode == "file" {
        if let Some((name, extensions)) = filter.as_deref().and_then(filter_for) {
            dialog = dialog.add_filter(name, extensions).add_filter("All files", &["*"]);
        }
    }
    let picked = tauri::async_runtime::spawn_blocking(move || {
        let paths = match (mode.as_str(), multiple) {
            ("directory", false) => dialog.blocking_pick_folder().map(|p| vec![p]),
            ("directory", true) => dialog.blocking_pick_folders(),
            (_, false) => dialog.blocking_pick_file().map(|p| vec![p]),
            (_, true) => dialog.blocking_pick_files(),
        };
        paths
            .unwrap_or_default()
            .into_iter()
            .filter_map(|p| p.into_path().ok())
            .map(|p| p.to_string_lossy().to_string())
            .collect::<Vec<_>>()
    })
    .await
    .map_err(|e| e.to_string())?;
    hand(&app, &picked);
    Ok(picked)
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
            if let Ok(byte) = u8::from_str_radix(hex, 16) {
                out.push(byte);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

/// A file name a save dialog can safely propose.
fn safe_name(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|c| if matches!(c, '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|') || c.is_control() { '_' } else { c })
        .collect();
    let cleaned = cleaned.trim().trim_matches('.').to_string();
    if cleaned.is_empty() { "download".into() } else { cleaned }
}

/// Save bytes the page made (a PNG of the view, a CSV, a PDF) through a
/// native Save dialog. The body is the raw bytes; the name travels in the
/// `x-plexora-name` header, percent-encoded. Returns the path, or None if the
/// user cancelled.
#[tauri::command]
pub async fn save_bytes(app: AppHandle, window: WebviewWindow, request: Request<'_>) -> Result<Option<String>, String> {
    let InvokeBody::Raw(bytes) = request.body() else {
        return Err("save_bytes expects raw bytes".into());
    };
    let bytes = bytes.clone();
    let name = request
        .headers()
        .get("x-plexora-name")
        .and_then(|value| value.to_str().ok())
        .map(percent_decode)
        .map(|name| safe_name(&name))
        .unwrap_or_else(|| "download".into());
    let mut dialog = window.dialog().file().set_parent(&window).set_file_name(&name);
    if let Some(ext) = std::path::Path::new(&name).extension().and_then(|e| e.to_str()) {
        let upper = ext.to_uppercase();
        dialog = dialog.add_filter(format!("{upper} file"), &[ext]);
    }
    let saved = tauri::async_runtime::spawn_blocking(move || -> Result<Option<String>, String> {
        let Some(target) = dialog.blocking_save_file() else { return Ok(None) };
        let target = target.into_path().map_err(|e| e.to_string())?;
        std::fs::write(&target, &bytes).map_err(|e| format!("Could not save {}: {e}", target.display()))?;
        Ok(Some(target.to_string_lossy().to_string()))
    })
    .await
    .map_err(|e| e.to_string())??;
    if let Some(path) = &saved {
        // So "Show in Finder" on the toast can reveal it wherever it went.
        hand(&app, std::slice::from_ref(path));
    }
    Ok(saved)
}

/// The bytes of a file the user dropped on the window (the figure canvas
/// takes dropped images this way). Only a regular file under the size limit,
/// and only one the shell handed the page (`HandedPaths`): the page cannot
/// name any other file on this computer and get its bytes.
#[tauri::command]
pub async fn read_file(app: AppHandle, path: String) -> Result<Response, String> {
    let path = PathBuf::from(path);
    if !app.state::<HandedPaths>().allows(&path) {
        return Err(format!(
            "{} was not dropped, picked or opened in Plexora, so the app will not read it",
            path.display()
        ));
    }
    let meta = std::fs::metadata(&path).map_err(|e| e.to_string())?;
    if !meta.is_file() {
        return Err(format!("{} is not a file", path.display()));
    }
    if meta.len() > READ_LIMIT {
        return Err(format!("{} is larger than {} MB", path.display(), READ_LIMIT / 1024 / 1024));
    }
    let bytes = tauri::async_runtime::spawn_blocking(move || std::fs::read(path))
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;
    Ok(Response::new(bytes))
}

/// "Show in Finder" / "Show in folder" for a file the app saved.
#[tauri::command]
pub fn reveal_path(app: AppHandle, path: String) -> Result<(), String> {
    let path = PathBuf::from(path);
    revealable(&app.state::<HandedPaths>(), &path)?;
    app.opener().reveal_item_in_dir(path).map_err(|e| e.to_string())
}

/// The same session in the user's browser: the tokened URL, which trades
/// itself for a cookie on the browser's first request.
#[tauri::command]
pub fn open_in_browser(app: AppHandle, path: Option<String>) -> Result<(), String> {
    let ready = app.state::<ServerState>().0.ready().ok_or("the server is not running")?;
    let mut url: url::Url = ready.url.parse().map_err(|_| "bad server URL")?;
    if let Some(path) = path.filter(|p| p.starts_with('/')) {
        let (only_path, query) = path.split_once('?').unwrap_or((&path, ""));
        url.set_path(only_path);
        let mut pairs: Vec<(String, String)> = url::form_urlencoded::parse(query.as_bytes())
            .into_owned()
            .filter(|(k, _)| k != "token")
            .collect();
        pairs.push(("token".into(), ready.token.clone()));
        url.query_pairs_mut().clear().extend_pairs(pairs);
    }
    app.opener().open_url(url.as_str(), None::<&str>).map_err(|e| e.to_string())
}

/// Links out of the app (documentation, a dataset's home page).
#[tauri::command]
pub fn open_url(app: AppHandle, url: String) -> Result<(), String> {
    let parsed: url::Url = url.parse().map_err(|_| "not a URL")?;
    if !matches!(parsed.scheme(), "http" | "https" | "mailto") {
        return Err(format!("{} links are not opened", parsed.scheme()));
    }
    app.opener().open_url(parsed.as_str(), None::<&str>).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn quit_app(app: AppHandle) {
    app.exit(0);
}

#[tauri::command]
pub fn close_window(window: WebviewWindow) -> Result<(), String> {
    window.close().map_err(|e| e.to_string())
}

/// Async, not sync: a sync command runs on the main thread, and building a
/// WebView there deadlocks on Windows -- the window appears and never loads.
#[tauri::command]
pub async fn new_window(app: AppHandle) -> Result<(), String> {
    windows::new_window(&app).map_err(|e| e.to_string())
}

#[derive(Serialize)]
pub struct ServerInfo {
    origin: String,
    version: String,
    data_root: String,
    settings_path: String,
    pid: u32,
    log_dir: Option<String>,
}

/// About the server, without its token.
#[tauri::command]
pub fn server_info(app: AppHandle) -> Result<ServerInfo, String> {
    let ready = app.state::<ServerState>().0.ready().ok_or("the server is not running")?;
    Ok(ServerInfo {
        origin: ready.origin,
        version: ready.version,
        data_root: ready.data_root,
        settings_path: ready.settings_path,
        pid: ready.pid,
        log_dir: app.path().app_log_dir().ok().map(|p| p.to_string_lossy().to_string()),
    })
}

#[tauri::command]
pub fn take_pending_opens(app: AppHandle) -> Vec<opens::Open> {
    opens::take(&app)
}

#[tauri::command]
pub fn notify(app: AppHandle, title: String, body: Option<String>) -> Result<(), String> {
    let mut builder = app.notification().builder().title(title);
    if let Some(body) = body {
        builder = builder.body(body);
    }
    builder.show().map_err(|e| e.to_string())
}

/// A PNG the page rendered, onto the system clipboard as an image.
#[tauri::command]
pub fn copy_image(app: AppHandle, request: Request<'_>) -> Result<(), String> {
    let InvokeBody::Raw(bytes) = request.body() else {
        return Err("copy_image expects raw PNG bytes".into());
    };
    let image = tauri::image::Image::from_bytes(bytes).map_err(|e| e.to_string())?;
    app.clipboard().write_image(&image).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn toggle_fullscreen(window: WebviewWindow) -> Result<bool, String> {
    let next = !window.is_fullscreen().map_err(|e| e.to_string())?;
    window.set_fullscreen(next).map_err(|e| e.to_string())?;
    Ok(next)
}

#[tauri::command]
pub fn is_fullscreen(window: WebviewWindow) -> bool {
    window.is_fullscreen().unwrap_or(false)
}

/// The navbar rows only the shell can carry out, by the id the native menu
/// gives the same action (menu.rs). On Windows and Linux there is no native
/// menu bar, so these rows in the page's own navbar are the way to them.
pub const SHELL_ACTIONS: &[&str] = &["show_data", "show_logs", "zoom_in", "zoom_out", "zoom_reset"];

#[tauri::command]
pub fn shell_action(app: AppHandle, window: WebviewWindow, id: String) -> Result<(), String> {
    if !SHELL_ACTIONS.contains(&id.as_str()) {
        return Err(format!("not a shell action: {id}"));
    }
    crate::menu::run(&app, &id, Some(window));
    Ok(())
}

/// What the WebView turned out to support. The viewer needs WebGL2; a
/// WebKitGTK without GPU WebGL cannot draw, and the browser can.
#[tauri::command]
pub fn report_capabilities(app: AppHandle, webgl2: bool, offscreen_canvas: bool) {
    log::info!("webview capabilities: webgl2={webgl2} offscreen_canvas={offscreen_canvas}");
    if webgl2 {
        return;
    }
    let mut text = String::from(
        "This window's web engine cannot draw with WebGL 2, which the Plexora viewer needs.\n\n\
         File > Open in Browser opens the same session in your web browser, where it works.",
    );
    if cfg!(target_os = "linux") {
        text.push_str(
            "\n\nOn some Linux graphics drivers, starting Plexora with \
             WEBKIT_DISABLE_DMABUF_RENDERER=1 set fixes the window itself.",
        );
    }
    app.dialog()
        .message(text)
        .title("Plexora")
        .kind(tauri_plugin_dialog::MessageDialogKind::Warning)
        .show(|_| {});
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_decoded_and_made_safe() {
        assert_eq!(percent_decode("caf%C3%A9%20map.png"), "café map.png");
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(safe_name("../etc/passwd"), "_etc_passwd");
        assert_eq!(safe_name("a:b*c?.csv"), "a_b_c_.csv");
        assert_eq!(safe_name("..."), "download");
    }

    #[test]
    fn filters_match_the_server_side_names() {
        for name in ["image", "csv", "h5ad", "data", "channels"] {
            assert!(filter_for(name).is_some(), "{name}");
        }
        assert!(filter_for("any").is_none());
    }

    #[test]
    fn the_menu_bar_is_native_only_where_it_sits_outside_the_window() {
        // Windows and Linux draw a native menu inside the window, above the
        // page's own navbar; that is the duplicate this keeps from returning.
        assert_eq!(crate::menu::NATIVE_MENU_BAR, cfg!(target_os = "macos"));
    }

    #[test]
    fn only_the_servers_own_origin_is_its_page() {
        let server = "http://127.0.0.1:8420";
        let page = |text: &str| text.parse::<url::Url>().unwrap();
        assert!(is_server_page(&page("http://127.0.0.1:8420/tonsil?x=1"), server));
        assert!(is_server_page(&page("http://127.0.0.1:8420"), server));
        // Another server on loopback matches the capability's pattern, not this.
        assert!(!is_server_page(&page("http://127.0.0.1:8421/"), server));
        assert!(!is_server_page(&page("http://localhost:8420/"), server));
        assert!(!is_server_page(&page("https://127.0.0.1:8420/"), server));
        assert!(!is_server_page(&page("data:text/html,hi"), server));
        assert!(!is_server_page(&page("http://127.0.0.1:8420/"), "not a url"));
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("plexora-shell-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn only_handed_paths_are_readable() {
        let dir = scratch("handed");
        let dropped = dir.join("dropped.png");
        let other = dir.join("secret.txt");
        std::fs::write(&dropped, b"png").unwrap();
        std::fs::write(&other, b"key").unwrap();
        let handed = HandedPaths::default();
        assert!(!handed.allows(&dropped));
        handed.add([&dropped]);
        assert!(handed.allows(&dropped));
        assert!(!handed.allows(&other));
        // The same file by another spelling resolves to what was handed...
        assert!(handed.allows(&dir.join(".").join("dropped.png")));
        // ...but a way out of it does not.
        assert!(!handed.allows(&dir.join("dropped.png").join("..").join("secret.txt")));
        // A network path is never resolved to be checked.
        assert!(!handed.allows(Path::new(r"\\attacker\share\x.png")));
        assert!(!handed.allows(Path::new("//attacker/share/x.png")));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn handed_paths_forget_the_oldest_past_the_limit() {
        let handed = HandedPaths::default();
        handed.add((0..HANDED_LIMIT + 10).map(|i| PathBuf::from(format!("/nowhere/{i}"))));
        assert!(!handed.holds(Path::new("/nowhere/0")));
        assert!(handed.holds(&PathBuf::from(format!("/nowhere/{}", HANDED_LIMIT + 9))));
    }

    #[test]
    fn reveal_takes_existing_absolute_paths_and_only_handed_network_ones() {
        let dir = scratch("reveal");
        let saved = dir.join("export.png");
        std::fs::write(&saved, b"png").unwrap();
        let handed = HandedPaths::default();
        assert!(revealable(&handed, &saved).is_ok());
        assert!(revealable(&handed, &dir).is_ok());
        assert!(revealable(&handed, &dir.join("missing.png")).is_err());
        assert!(revealable(&handed, Path::new("relative/export.png")).is_err());
        let share = Path::new("//fileserver/lab/export.png");
        assert!(revealable(&handed, share).unwrap_err().contains("network"));
        handed.add([share]);
        // Handed, so it is let through to the existence check.
        if let Err(error) = revealable(&handed, share) {
            assert!(!error.contains("network"), "{error}");
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn shell_actions_are_only_the_ones_the_navbar_cannot_do_itself() {
        // Anything else the page reaches has its own command; this one must
        // not grow into a way to run arbitrary menu ids.
        assert_eq!(SHELL_ACTIONS, &["show_data", "show_logs", "zoom_in", "zoom_out", "zoom_reset"]);
    }
}
