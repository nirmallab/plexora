//! The Plexora desktop shell.
//!
//! Deliberately thin: every window shows the page the app's own Python
//! server serves, and everything the app *does* is that server's business.
//! What lives here is what only a native process can do -- start and stop the
//! server, own the macOS menu bar, show native dialogs and notifications, accept
//! dropped and opened files -- plus the small bridge the page reaches it
//! through (`plexora/client/src/js/services/desktopBridge.js`).

mod commands;
mod downloads;
mod lifecycle;
mod menu;
mod opens;
pub mod server;
mod setup;
mod smoke;
mod updates;
mod windows;

use std::sync::Arc;

use tauri::Manager;

/// The server, shared by every command and thread that needs to reach it.
pub struct ServerState(pub Arc<server::Server>);

pub fn run() {
    if std::env::args().any(|arg| arg == "--smoke-test") {
        // Before Tauri initialises anything, so it runs on a machine with no
        // display (CI) and never shows a window.
        std::process::exit(smoke::run());
    }

    let app = tauri::Builder::default()
        // First, so a second launch hands over its arguments and exits before
        // any other plugin (or a second server) starts.
        .plugin(tauri_plugin_single_instance::init(|app, argv, cwd| {
            opens::from_second_instance(app, argv, cwd);
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_denylist(&["splash"])
                .build(),
        )
        .plugin(
            tauri_plugin_log::Builder::new()
                .level(log::LevelFilter::Info)
                .target(tauri_plugin_log::Target::new(
                    tauri_plugin_log::TargetKind::LogDir { file_name: Some("shell".into()) },
                ))
                .target(tauri_plugin_log::Target::new(tauri_plugin_log::TargetKind::Stderr))
                .build(),
        )
        .manage(ServerState(Arc::new(server::Server::default())))
        .manage(opens::PendingOpens::default())
        .manage(windows::WindowState::default())
        .manage(downloads::Downloads::default())
        .manage(updates::PendingUpdate::default())
        .manage(commands::HandedPaths::default())
        .on_menu_event(menu::on_event)
        // A file dropped on a window is one the page may read (`read_file`).
        // Recorded here, before the page can hear of the drop: the page's
        // event is delivered after this handler returns.
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::DragDrop(tauri::DragDropEvent::Drop { paths, .. }) = event {
                window.state::<commands::HandedPaths>().add(paths);
            }
        })
        .invoke_handler(|invoke| {
            // Only the page this launch's server serves may call the shell.
            if let Err(reason) = commands::check_caller(invoke.message.webview_ref()) {
                log::warn!("refused {}: {reason}", invoke.message.command());
                invoke.resolver.reject(reason);
                return true;
            }
            handler(invoke)
        })
        .setup(setup::setup)
        .build(tauri::generate_context!())
        .expect("error while building the Plexora desktop app");

    app.run(lifecycle::on_run_event);
}

/// Every command the page can call. `check_caller` runs before each one.
fn handler(invoke: tauri::ipc::Invoke<tauri::Wry>) -> bool {
    let commands: fn(tauri::ipc::Invoke<tauri::Wry>) -> bool = tauri::generate_handler![
        commands::pick_paths,
        commands::save_bytes,
        commands::read_file,
        commands::reveal_path,
        commands::open_in_browser,
        commands::open_url,
        commands::quit_app,
        commands::new_window,
        commands::close_window,
        commands::server_info,
        commands::take_pending_opens,
        commands::notify,
        commands::copy_image,
        commands::toggle_fullscreen,
        commands::is_fullscreen,
        commands::shell_action,
        commands::report_capabilities,
        updates::check_update,
        updates::install_update,
    ];
    commands(invoke)
}
