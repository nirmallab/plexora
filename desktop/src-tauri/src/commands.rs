//! What the page can ask the shell for, through `window.PlexoraDesktop`.
//!
//! Every native feature is an app command here -- dialogs, notifications,
//! the clipboard -- rather than a plugin's JavaScript API, so the page needs
//! no plugin permissions at all and the whole surface is this one file.

use std::path::PathBuf;

use serde::Serialize;
use tauri::ipc::{InvokeBody, Request, Response};
use tauri::{AppHandle, Manager, WebviewWindow};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

use crate::{opens, windows, ServerState};

/// Largest file `read_file` hands to the page. Pixels are read by the server
/// from disk; this is for the odd dropped PNG, not a whole slide.
const READ_LIMIT: u64 = 512 * 1024 * 1024;

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
pub async fn save_bytes(window: WebviewWindow, request: Request<'_>) -> Result<Option<String>, String> {
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
    tauri::async_runtime::spawn_blocking(move || {
        let Some(target) = dialog.blocking_save_file() else { return Ok(None) };
        let target = target.into_path().map_err(|e| e.to_string())?;
        std::fs::write(&target, &bytes).map_err(|e| format!("Could not save {}: {e}", target.display()))?;
        Ok(Some(target.to_string_lossy().to_string()))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// The bytes of a file the user dropped on the window (the figure canvas
/// takes dropped images this way). Only a regular file under the size limit.
#[tauri::command]
pub async fn read_file(path: String) -> Result<Response, String> {
    let path = PathBuf::from(path);
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

#[tauri::command]
pub fn reveal_path(app: AppHandle, path: String) -> Result<(), String> {
    app.opener().reveal_item_in_dir(PathBuf::from(path)).map_err(|e| e.to_string())
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
}
