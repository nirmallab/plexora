/// Every command the page may call. Declared so tauri-build generates an
/// `allow-<command>` permission for each: the page is served from a remote
/// (loopback) origin, and Tauri gives a remote origin nothing that its
/// capability (capabilities/main.json) does not name.
const COMMANDS: &[&str] = &[
    "pick_paths",
    "save_bytes",
    "read_file",
    "reveal_path",
    "open_in_browser",
    "open_url",
    "quit_app",
    "new_window",
    "close_window",
    "server_info",
    "take_pending_opens",
    "notify",
    "copy_image",
    "toggle_fullscreen",
    "is_fullscreen",
    "report_capabilities",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to run tauri-build");
}
