//! Downloads the server sends (a Figure Builder export, a gating CSV).
//!
//! A WebView has no download manager of its own worth the name, so each one
//! is staged in the cache directory first, and a native Save dialog asks
//! where it goes once it has fully arrived. The `Requested` callback runs on
//! the UI thread and must not block, which is why the dialog comes after.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;
use tauri::webview::DownloadEvent;
use tauri::{AppHandle, Emitter, Manager, Webview};
use tauri_plugin_dialog::DialogExt;

#[derive(Default)]
pub struct Downloads {
    staged: Mutex<HashMap<String, PathBuf>>,
}

#[derive(Clone, Serialize)]
struct Finished {
    name: String,
    path: Option<String>,
    success: bool,
    cancelled: bool,
}

fn name_for(url: &url::Url, suggested: &std::path::Path) -> String {
    suggested
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .filter(|n| !n.is_empty())
        .or_else(|| {
            url.path_segments()
                .and_then(|mut segments| segments.next_back().map(str::to_string))
                .filter(|n| !n.is_empty())
        })
        .unwrap_or_else(|| "download".into())
}

pub fn handler(app: AppHandle) -> impl Fn(Webview, DownloadEvent<'_>) -> bool + Send + Sync + 'static {
    move |webview, event| {
        match event {
            DownloadEvent::Requested { url, destination } => {
                let name = name_for(&url, destination);
                let dir = app
                    .path()
                    .app_cache_dir()
                    .unwrap_or_else(|_| std::env::temp_dir())
                    .join("downloads")
                    .join(uuid::Uuid::new_v4().to_string());
                if std::fs::create_dir_all(&dir).is_err() {
                    return true;
                }
                let staged = dir.join(&name);
                *destination = staged.clone();
                app.state::<Downloads>().staged.lock().unwrap().insert(url.to_string(), staged);
            }
            DownloadEvent::Finished { url, path, success } => {
                let staged = app.state::<Downloads>().staged.lock().unwrap().remove(url.as_str());
                let Some(staged) = path.or(staged) else { return true };
                let label = webview.label().to_string();
                if success {
                    offer_save(&app, label, staged);
                } else {
                    let name = staged.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
                    let _ = app.emit_to(&label, "plexora://download-finished",
                        Finished { name, path: None, success: false, cancelled: false });
                }
            }
            _ => {}
        }
        true
    }
}

fn offer_save(app: &AppHandle, label: String, staged: PathBuf) {
    let name = staged.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_else(|| "download".into());
    let mut dialog = app.dialog().file().set_file_name(&name);
    if let Some(window) = app.get_webview_window(&label) {
        dialog = dialog.set_parent(&window);
    }
    let app = app.clone();
    dialog.save_file(move |target| {
        let result = match target.and_then(|t| t.into_path().ok()) {
            None => Finished { name: name.clone(), path: None, success: false, cancelled: true },
            Some(target) => {
                let moved = std::fs::rename(&staged, &target)
                    .or_else(|_| std::fs::copy(&staged, &target).map(|_| ()));
                Finished {
                    name: name.clone(),
                    success: moved.is_ok(),
                    path: moved.is_ok().then(|| target.to_string_lossy().to_string()),
                    cancelled: false,
                }
            }
        };
        let _ = std::fs::remove_file(&staged);
        if let Some(dir) = staged.parent() {
            let _ = std::fs::remove_dir(dir);
        }
        let _ = app.emit_to(&label, "plexora://download-finished", result);
    });
}
