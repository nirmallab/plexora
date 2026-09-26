//! The splash and the main windows.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

use crate::{downloads, ServerState};

/// Per-window zoom, since a WebView cannot be asked for its own.
#[derive(Default)]
pub struct WindowState {
    pub zoom: Mutex<HashMap<String, f64>>,
    counter: AtomicU32,
}

pub fn create_splash(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    WebviewWindowBuilder::new(app, "splash", WebviewUrl::App("splash.html".into()))
        .title("Plexora")
        .inner_size(420.0, 280.0)
        .resizable(false)
        .decorations(false)
        .center()
        .focused(true)
        .build()
}

pub fn close_splash(app: &AppHandle) {
    if let Some(splash) = app.get_webview_window("splash") {
        let _ = splash.close();
    }
}

fn platform_word() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(windows) {
        "windows"
    } else {
        "linux"
    }
}

/// What the page learns about the shell before any of its scripts run. No
/// token: the page authenticates with the cookie the first request set.
fn init_script(app: &AppHandle, origin: &str) -> String {
    let webview = if cfg!(target_os = "macos") {
        "wkwebview"
    } else if cfg!(windows) {
        "webview2"
    } else {
        "webkitgtk"
    };
    let info = serde_json::json!({
        "version": app.package_info().version.to_string(),
        "platform": platform_word(),
        "arch": std::env::consts::ARCH,
        "webview": webview,
        "serverOrigin": origin,
    });
    format!(
        "Object.defineProperty(window, '__PLEXORA_DESKTOP__', {{ value: Object.freeze({info}), writable: false }});"
    )
}

/// A window on the app's server. Every one is built here so each gets the
/// init script, the download handler and the navigation guard.
pub fn create_main(app: &AppHandle, label: &str, url: &str) -> tauri::Result<WebviewWindow> {
    let parsed: url::Url = url.parse().map_err(|_| tauri::Error::InvalidWebviewUrl("bad server URL"))?;
    let origin = parsed.origin();
    let origin_text = origin.ascii_serialization();
    let for_links = app.clone();
    WebviewWindowBuilder::new(app, label, WebviewUrl::External(parsed))
        .title("Plexora")
        .inner_size(1440.0, 900.0)
        .min_inner_size(900.0, 600.0)
        .center()
        .initialization_script(&init_script(app, &origin_text))
        .on_navigation(move |target| {
            if target.origin() == origin {
                return true;
            }
            match target.scheme() {
                // Documentation, a paper, a bucket's web page: the user's
                // browser, not a Plexora window that cannot go back.
                "http" | "https" | "mailto" => {
                    let _ = for_links.opener().open_url(target.as_str(), None::<&str>);
                    false
                }
                "about" | "blob" | "data" => true,
                _ => false,
            }
        })
        .on_download(downloads::handler(app.clone()))
        .build()
}

pub fn new_window(app: &AppHandle) -> tauri::Result<()> {
    let Some(ready) = app.state::<ServerState>().0.ready() else { return Ok(()) };
    let state = app.state::<WindowState>();
    let label = format!("main-{}", state.counter.fetch_add(1, Ordering::SeqCst) + 1);
    // The cookie the first window's tokened URL set is shared by every
    // window, so later ones need no token in their address.
    create_main(app, &label, &format!("{}/", ready.origin))?;
    Ok(())
}

/// After a restart: the server has a new port and a new token.
pub fn renavigate_all(app: &AppHandle, url: &str) {
    let Ok(parsed) = url.parse::<url::Url>() else { return };
    for (label, window) in app.webview_windows() {
        if label.starts_with("main") {
            let _ = window.navigate(parsed.clone());
        }
    }
}

pub fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window("main")
        .or_else(|| app.webview_windows().into_iter().find(|(l, _)| l.starts_with("main")).map(|(_, w)| w))
}

/// The window a menu command is about: the focused one, else the first.
pub fn focused_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.webview_windows()
        .into_values()
        .find(|window| window.label().starts_with("main") && window.is_focused().unwrap_or(false))
        .or_else(|| main_window(app))
}

pub fn bring_to_front(window: &WebviewWindow) {
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
}
