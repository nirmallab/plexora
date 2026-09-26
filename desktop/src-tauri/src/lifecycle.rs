//! The app's life around the server's: quit stops it, and a server that
//! stops on its own gets a dialog rather than a window that quietly breaks.

use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

#[cfg(target_os = "macos")]
use crate::opens;
use crate::{setup, windows, ServerState};

pub fn on_run_event(app: &AppHandle, event: RunEvent) {
    match event {
        RunEvent::Exit => {
            let clean = app.state::<ServerState>().0.shutdown();
            log::info!("server stopped ({})", if clean { "cleanly" } else { "forced" });
        }
        #[cfg(target_os = "macos")]
        RunEvent::Opened { urls } => {
            let opened: Vec<_> = urls.iter().filter_map(opens::from_url).collect();
            opens::queue(app, opened);
        }
        _ => {}
    }
}

fn show_log(app: &AppHandle) {
    if let Some(path) = setup::log_path(app) {
        if path.exists() {
            let _ = app.opener().reveal_item_in_dir(path);
            return;
        }
    }
    if let Ok(dir) = app.path().app_log_dir() {
        let _ = app.opener().open_path(dir.to_string_lossy(), None::<&str>);
    }
}

fn with_tail(message: &str, tail: &str) -> String {
    if tail.trim().is_empty() {
        message.to_string()
    } else {
        let lines: Vec<&str> = tail.lines().collect();
        let start = lines.len().saturating_sub(12);
        format!("{message}\n\nThe last lines it wrote:\n{}", lines[start..].join("\n"))
    }
}

/// Called from the server thread (dialogs may block there, not on the UI
/// thread).
pub fn start_failed(app: &AppHandle, message: &str, tail: &str) {
    log::error!("start failed: {message}\n{tail}");
    windows::close_splash(app);
    let show = app
        .dialog()
        .message(with_tail(message, tail))
        .title("Plexora could not start")
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::OkCancelCustom("Show Log".into(), "Quit".into()))
        .blocking_show();
    if show {
        show_log(app);
    }
    app.exit(1);
}

pub fn server_died(app: &AppHandle, code: Option<i32>, tail: &str) {
    let code = code.map(|c| format!(" (exit code {c})")).unwrap_or_default();
    log::error!("server stopped unexpectedly{code}\n{tail}");
    let restart = app
        .dialog()
        .message(with_tail(
            &format!("Plexora's server stopped unexpectedly{code}. Anything unsaved in the window may be lost."),
            tail,
        ))
        .title("Plexora stopped")
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::OkCancelCustom("Restart".into(), "Quit".into()))
        .blocking_show();
    if restart {
        setup::start_server(app.clone(), false);
    } else {
        app.exit(1);
    }
}
