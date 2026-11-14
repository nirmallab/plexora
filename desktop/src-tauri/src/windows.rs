//! The splash and the main windows.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

use crate::{downloads, localfiles, ServerState};

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
        // Where the page reads a granted file (localfiles.rs): the
        // `plexora-file` scheme as this platform's WebView serves it.
        "localFiles": localfiles::page_base(),
    });
    format!(
        "Object.defineProperty(window, '__PLEXORA_DESKTOP__', {{ value: Object.freeze({info}), writable: false }});"
    )
}

/// What the navigation guard does with a window's move to `target`.
#[derive(Debug, PartialEq, Eq)]
enum Navigation {
    Allow,
    /// The user's browser opens it instead; the window stays.
    External,
    Refuse,
}

/// The navigation guard. The app's own pages are those of the server running
/// now (`server`), parsed by the rule `commands::is_server_page` uses: after a
/// restart the server is on a new port, and the origin a window was built with
/// is a port that may now be anyone's. `built` stands in only while no server
/// is ready -- before the first one, or between one and its successor.
fn navigation_for(target: &url::Url, built: &url::Origin, server: Option<&str>) -> Navigation {
    let ours = match server {
        Some(origin) => crate::commands::is_server_page(target, origin),
        None => target.origin() == *built,
    };
    if ours {
        return Navigation::Allow;
    }
    match target.scheme() {
        // Documentation, a paper, a bucket's web page: the user's browser,
        // not a Plexora window that cannot go back.
        "http" | "https" | "mailto" => Navigation::External,
        "about" | "blob" | "data" => Navigation::Allow,
        _ => Navigation::Refuse,
    }
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
            let server = for_links.state::<ServerState>().0.ready().map(|ready| ready.origin);
            match navigation_for(target, &origin, server.as_deref()) {
                Navigation::Allow => true,
                Navigation::External => {
                    let _ = for_links.opener().open_url(target.as_str(), None::<&str>);
                    false
                }
                Navigation::Refuse => false,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn page(text: &str) -> url::Url {
        text.parse().unwrap()
    }

    #[test]
    fn a_window_follows_its_server_to_a_new_port() {
        // Built on 8420; the server restarted on 51234 (8420 was taken).
        let built = page("http://127.0.0.1:8420/?token=a").origin();
        let now = Some("http://127.0.0.1:51234");
        assert_eq!(navigation_for(&page("http://127.0.0.1:51234/?token=b"), &built, now), Navigation::Allow);
        // The old port is somebody else's now: not a page of this app.
        assert_eq!(navigation_for(&page("http://127.0.0.1:8420/"), &built, now), Navigation::External);
    }

    #[test]
    fn the_built_origin_stands_in_only_while_no_server_is_ready() {
        let built = page("http://127.0.0.1:8420/").origin();
        assert_eq!(navigation_for(&page("http://127.0.0.1:8420/tonsil"), &built, None), Navigation::Allow);
        assert_eq!(navigation_for(&page("http://127.0.0.1:8421/"), &built, None), Navigation::External);
    }

    #[test]
    fn other_pages_go_to_the_browser_or_nowhere() {
        let built = page("http://127.0.0.1:8420/").origin();
        let now = Some("http://127.0.0.1:8420");
        assert_eq!(navigation_for(&page("https://plexora.app/docs"), &built, now), Navigation::External);
        assert_eq!(navigation_for(&page("mailto:a@b.c"), &built, now), Navigation::External);
        assert_eq!(navigation_for(&page("about:blank"), &built, now), Navigation::Allow);
        assert_eq!(navigation_for(&page("data:text/html,hi"), &built, now), Navigation::Allow);
        assert_eq!(navigation_for(&page("file:///etc/passwd"), &built, now), Navigation::Refuse);
    }
}
