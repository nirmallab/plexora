//! Help > Check for Updates, for the app itself.
//!
//! The pip route the browser uses (update_routes.py) cannot work here: the
//! app's Python is a runtime inside a signed, read-only bundle, and upgrading
//! Plexora means replacing the whole bundle. So the shell does it, with
//! Tauri's updater: it reads `latest.json` from the newest GitHub release,
//! downloads the artifact for this platform (the `.app.tar.gz` on a Mac, the
//! NSIS installer on Windows, the `.deb` on Linux), checks its minisign
//! signature against the public key compiled into this build, installs it
//! and relaunches.
//!
//! The server is stopped before the install, never during the download: the
//! window keeps working while bytes arrive, and the files the install is about
//! to replace are the ones the server is running from. A failed install starts
//! the server again and says why.

use std::sync::Mutex;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_updater::{Update, Updater, UpdaterExt};

use crate::{setup, ServerState};

/// Where to send somebody whose build cannot update itself.
const RELEASES_URL: &str = "https://github.com/nirmallab/plexora/releases/latest";

/// The update the last check found, so Install downloads exactly what the
/// dialog showed rather than whatever a second check might answer.
#[derive(Default)]
pub struct PendingUpdate(pub Mutex<Option<Update>>);

#[derive(Serialize)]
pub struct UpdateInfo {
    current: String,
    latest: Option<String>,
    available: bool,
    notes: String,
    date: Option<String>,
    /// "inplace": the button installs it. "unsupported": this build has no
    /// update key (a local or unsigned build), so the dialog links to the
    /// release page instead.
    kind: &'static str,
    url: String,
}

#[derive(Clone, Serialize)]
struct Progress {
    phase: &'static str,
    downloaded: u64,
    total: Option<u64>,
}

/// Whether this build carries the public key the updater verifies with.
/// A build made without `TAURI_SIGNING_PRIVATE_KEY` has none, and asking the
/// updater anyway would only fail later with a signature error.
fn signing_configured(app: &AppHandle) -> bool {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|config| config.get("pubkey"))
        .and_then(|key| key.as_str())
        .map(|key| !key.trim().is_empty())
        .unwrap_or(false)
}

fn updater(app: &AppHandle) -> Result<Updater, String> {
    #[allow(unused_mut)]
    let mut builder = app.updater_builder();
    // A development build can be pointed at a local manifest, to try an
    // update end to end without publishing one. Release builds ignore it:
    // where updates come from is not something the environment may change.
    #[cfg(debug_assertions)]
    if let Ok(endpoint) = std::env::var("PLEXORA_UPDATE_ENDPOINT") {
        let url = endpoint
            .parse()
            .map_err(|_| "PLEXORA_UPDATE_ENDPOINT is not a URL".to_string())?;
        builder = builder.endpoints(vec![url]).map_err(|e| e.to_string())?;
    }
    builder.build().map_err(|e| e.to_string())
}

fn unsupported(current: String) -> UpdateInfo {
    UpdateInfo {
        current,
        latest: None,
        available: false,
        notes: String::new(),
        date: None,
        kind: "unsupported",
        url: RELEASES_URL.into(),
    }
}

#[tauri::command]
pub async fn check_update(app: AppHandle) -> Result<UpdateInfo, String> {
    let current = app.package_info().version.to_string();
    if !signing_configured(&app) {
        return Ok(unsupported(current));
    }
    let found = updater(&app)?
        .check()
        .await
        .map_err(|e| format!("Could not reach the update server: {e}"))?;
    let info = match &found {
        Some(update) => UpdateInfo {
            current,
            latest: Some(update.version.clone()),
            available: true,
            notes: update.body.clone().unwrap_or_default(),
            date: update.date.map(|d| d.to_string()),
            kind: "inplace",
            url: format!("https://github.com/nirmallab/plexora/releases/tag/v{}", update.version),
        },
        None => UpdateInfo {
            current: current.clone(),
            latest: Some(current),
            available: false,
            notes: String::new(),
            date: None,
            kind: "inplace",
            url: RELEASES_URL.into(),
        },
    };
    *app.state::<PendingUpdate>().0.lock().unwrap() = found;
    Ok(info)
}

#[tauri::command]
pub async fn install_update(app: AppHandle) -> Result<(), String> {
    let pending = app.state::<PendingUpdate>().0.lock().unwrap().clone();
    let update = match pending {
        Some(update) => update,
        None => updater(&app)?
            .check()
            .await
            .map_err(|e| format!("Could not reach the update server: {e}"))?
            .ok_or_else(|| "Plexora is already up to date.".to_string())?,
    };
    log::info!("downloading Plexora {}", update.version);

    let emitter = app.clone();
    let mut downloaded: u64 = 0;
    let bytes = update
        .download(
            |chunk, total| {
                downloaded += chunk as u64;
                let _ = emitter.emit(
                    "plexora://update-progress",
                    Progress { phase: "downloading", downloaded, total },
                );
            },
            || {},
        )
        .await
        .map_err(|e| format!("The download failed: {e}"))?;
    let _ = app.emit(
        "plexora://update-progress",
        Progress { phase: "installing", downloaded: bytes.len() as u64, total: None },
    );

    // Stopped the way Quit stops it, so the crash dialog stays quiet: the
    // monitor thread sees `exit_expected` and returns.
    let shared = app.state::<ServerState>().0.clone();
    let _ = tauri::async_runtime::spawn_blocking(move || shared.shutdown()).await;

    let installing = update.clone();
    let installed = tauri::async_runtime::spawn_blocking(move || installing.install(bytes))
        .await
        .map_err(|e| e.to_string())
        .and_then(|result| result.map_err(|e| e.to_string()));
    match installed {
        Ok(()) => {
            // Windows never gets here: the NSIS installer takes over and
            // relaunches the app itself. macOS and Linux have replaced the
            // files and need the new ones started.
            log::info!("installed Plexora {}; relaunching", update.version);
            app.restart();
        }
        Err(error) => {
            log::error!("installing Plexora {} failed: {error}", update.version);
            setup::start_server(app.clone(), false);
            let message = format!(
                "Plexora {} could not be installed:\n\n{error}\n\nThis version is still \
                 installed and has been started again. The new version can be downloaded \
                 from {RELEASES_URL}.",
                update.version
            );
            // Native, because restarting the server reloads every window and
            // takes the page's own dialog with it.
            app.dialog()
                .message(message.clone())
                .title("Update failed")
                .kind(MessageDialogKind::Error)
                .show(|_| {});
            Err(message)
        }
    }
}
