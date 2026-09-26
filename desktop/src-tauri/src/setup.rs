//! Startup: splash, server, menu, main window -- in that order.

use std::path::PathBuf;
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_deep_link::DeepLinkExt;

use crate::{lifecycle, menu, opens, server, windows, ServerState};

pub fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let handle = app.handle().clone();

    let args: Vec<String> = std::env::args().skip(1).collect();
    opens::queue(&handle, opens::from_args(&args, std::env::current_dir().ok().as_deref()));

    // Installed builds register the scheme through the bundler; this covers a
    // development build on Windows and Linux, and is harmless otherwise.
    #[cfg(any(windows, target_os = "linux"))]
    if cfg!(debug_assertions) {
        let _ = app.deep_link().register_all();
    }
    let for_links = handle.clone();
    app.deep_link().on_open_url(move |event| {
        let opened: Vec<_> = event.urls().iter().filter_map(opens::from_url).collect();
        opens::queue(&for_links, opened);
    });

    windows::create_splash(&handle)?;
    start_server(handle, true);
    Ok(())
}

pub fn log_path(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_log_dir().ok().map(|dir| dir.join("server.log"))
}

/// Start the server on a thread of its own, which then watches it for life.
///
/// `first` opens the main window when it is ready; otherwise (a restart after
/// the server died) every open window is pointed at the new server instead.
pub fn start_server(app: AppHandle, first: bool) {
    let shared = app.state::<ServerState>().0.clone();
    let resource_dir = app.path().resource_dir().ok();
    let log = log_path(&app);
    let spawned = std::thread::Builder::new().name("plexora-server".into()).spawn(move || {
        let launch = match server::find_launch(resource_dir) {
            Ok(launch) => launch,
            Err(error) => return lifecycle::start_failed(&app, &error.to_string(), ""),
        };
        log::info!("starting the server with {}", launch.python().display());
        let progress = |message: &str| {
            let _ = app.emit_to("splash", "plexora://splash-status", message.to_string());
        };
        let ready = match shared.start(&launch, log.as_deref(), &progress) {
            Ok(ready) => ready,
            Err(error) => {
                return lifecycle::start_failed(&app, &error.to_string(), &shared.tail(40))
            }
        };
        log::info!("server {} ready on {}", ready.version, ready.origin);
        progress("Opening…");

        let info = server::http_get(
            &ready.origin,
            &format!("/desktop/info?token={}", ready.token),
            Duration::from_secs(20),
        )
        .ok()
        .and_then(|(status, body)| if status == 200 { serde_json::from_str(&body).ok() } else { None })
        .unwrap_or(serde_json::Value::Null);
        if let Err(error) = menu::install(&app, &info) {
            log::warn!("could not build the menu: {error}");
        }

        if first {
            match windows::create_main(&app, "main", &ready.url) {
                Ok(_) => {
                    windows::close_splash(&app);
                    opens::window_ready(&app);
                }
                Err(error) => {
                    return lifecycle::start_failed(
                        &app,
                        &format!("Could not open the Plexora window: {error}"),
                        "",
                    )
                }
            }
        } else {
            windows::renavigate_all(&app, &ready.url);
        }

        let code = shared.monitor();
        if !shared.exit_expected() {
            lifecycle::server_died(&app, code, &shared.tail(40));
        }
    });
    if let Err(error) = spawned {
        log::error!("could not start the server thread: {error}");
    }
}
