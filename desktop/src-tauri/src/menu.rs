//! The native menu bar -- on macOS only.
//!
//! On macOS the menu bar is the system's, along the top of the screen, and an
//! app without one has no Quit, no Hide and no Edit menu for text fields to
//! cut and paste through. So it is built there, as below.
//!
//! On Windows and Linux a native menu is drawn INSIDE the window, as a strip
//! above the page -- and the page's own navbar (base.html) is already a menu
//! bar with the same File / View / Help, styled as the rest of Plexora is.
//! Two menu bars, one above the other, saying the same things in two looks,
//! is what the window showed. So there the page's navbar is the only one:
//! `install` sets no menu, and the few rows only the shell can carry out
//! (New Window, Full Screen, Interface Size, Show Data Folder, Show Logs) are
//! desktop-only rows in the navbar that reach the shell through commands
//! (`new_window`, `toggle_fullscreen`, `shell_action`). Their chords were
//! already run by the page there (desktopBridge.js, SHELL_CHORDS): a menu
//! accelerator never fires on Windows or WebKitGTK while the WebView has
//! focus, which is nearly always.
//!
//! On macOS, one rule keeps a key from firing twice: the page stays the only
//! thing that runs a chord it already binds (mod+I, mod+O, mod+, and every
//! tool's shortcut). The native items for those carry no accelerator and send
//! `plexora://menu` to the page, which clicks the same element its own
//! shortcut would. Accelerators here are only keys the page provably never
//! binds: Plexora's plugin API refuses mod+Q/W/N/T/H/M.

use tauri::menu::{
    AboutMetadataBuilder, Menu, MenuBuilder, MenuEvent, MenuItemBuilder, PredefinedMenuItem,
    SubmenuBuilder,
};
use tauri::{AppHandle, Emitter, Manager, WebviewWindow, Wry};
use tauri_plugin_opener::OpenerExt;

use crate::{windows, ServerState};

/// The documentation site. Must equal `DOCS_URL` in plexora/links.py, which
/// tests/test_docs_links.py checks, so the app and the browser UI agree.
const DOCS_URL: &str = "https://nirmallab.github.io/plexora/";

/// Whether this platform gets a native menu bar at all. See the module notes.
pub const NATIVE_MENU_BAR: bool = cfg!(target_os = "macos");

pub fn build(app: &AppHandle, info: &serde_json::Value) -> tauri::Result<Menu<Wry>> {
    let item = |id: &str, label: &str| MenuItemBuilder::with_id(id, label).build(app);
    let keyed = |id: &str, label: &str, accel: &str| {
        MenuItemBuilder::with_id(id, label).accelerator(accel).build(app)
    };

    let file = SubmenuBuilder::new(app, "File")
        .item(&keyed("new_window", "New Window", "CmdOrCtrl+N")?)
        .separator()
        .item(&item("page:import", "Import Sample…")?)
        .item(&item("page:open", "Samples…")?)
        .separator()
        .item(
            &SubmenuBuilder::new(app, "Export Image")
                .item(&item("page:export_png", "PNG…")?)
                .item(&item("page:export_pdf", "PDF…")?)
                .build()?,
        )
        .separator()
        .item(&keyed("open_in_browser", "Open in Browser", "CmdOrCtrl+Shift+B")?)
        .item(&item("page:settings", "Settings…")?)
        .separator()
        .item(&keyed("close_window", "Close Window", "CmdOrCtrl+W")?);

    let tools: Vec<(String, String, String)> = info
        .get("tools")
        .and_then(|tools| tools.as_array())
        .map(|tools| {
            tools
                .iter()
                .filter_map(|tool| {
                    Some((
                        tool.get("name")?.as_str()?.to_string(),
                        tool.get("label")?.as_str()?.to_string(),
                        tool.get("menu").and_then(|s| s.as_str()).unwrap_or("tools").to_string(),
                    ))
                })
                .collect()
        })
        .unwrap_or_default();

    let mut view = SubmenuBuilder::new(app, "View").item(&keyed("reload", "Reload", "CmdOrCtrl+R")?);
    let view_tools: Vec<_> = tools.iter().filter(|t| t.2 == "view").collect();
    if !view_tools.is_empty() {
        view = view.separator();
        for (name, label, _) in &view_tools {
            view = view.item(&item(&format!("page:tool:{name}"), label)?);
        }
    }
    view = view
        .separator()
        .item(&item("page:scalebar", "Scalebar")?)
        .separator()
        .item(&item("zoom_in", "Zoom In")?)
        .item(&item("zoom_out", "Zoom Out")?)
        .item(&item("zoom_reset", "Actual Size")?)
        .separator()
        .item(&keyed("fullscreen", "Toggle Full Screen", "Ctrl+Cmd+F")?);
    if cfg!(debug_assertions) {
        view = view.separator().item(&item("devtools", "Developer Tools")?);
    }

    let mut tools_menu = SubmenuBuilder::new(app, "Tools");
    let tool_rows: Vec<_> = tools.iter().filter(|t| t.2 != "view").collect();
    if tool_rows.is_empty() {
        tools_menu = tools_menu.item(&MenuItemBuilder::new("No tools installed").enabled(false).build(app)?);
    }
    for (name, label, _) in tool_rows {
        tools_menu = tools_menu.item(&item(&format!("page:tool:{name}"), label)?);
    }

    // The shortcut sheet and Report an Issue are the page's own Help rows
    // (helpMenu.js), reached the same way as every other `page:` item. Check
    // for Updates sits under About in the app menu, where Mac users look.
    let help = SubmenuBuilder::new(app, "Help")
        .item(&item("page:shortcuts", "Keyboard Shortcuts")?)
        .item(&item("docs", "Plexora Documentation")?)
        .item(&item("page:report_issue", "Report an Issue…")?)
        .separator()
        .item(&item("show_data", "Show Data Folder")?)
        .item(&item("show_logs", "Show Logs")?);

    let about = AboutMetadataBuilder::new()
        .name(Some("Plexora"))
        .version(Some(app.package_info().version.to_string()))
        .website(Some(DOCS_URL))
        .build();
    MenuBuilder::new(app)
        .item(
            &SubmenuBuilder::new(app, "Plexora")
                .item(&PredefinedMenuItem::about(app, Some("About Plexora"), Some(about))?)
                .item(&item("page:check_updates", "Check for Updates…")?)
                .separator()
                .item(&PredefinedMenuItem::services(app, None)?)
                .separator()
                .item(&PredefinedMenuItem::hide(app, None)?)
                .item(&PredefinedMenuItem::hide_others(app, None)?)
                .item(&PredefinedMenuItem::show_all(app, None)?)
                .separator()
                .item(&PredefinedMenuItem::quit(app, None)?)
                .build()?,
        )
        .item(&file.build()?)
        // WKWebView needs these for text fields to cut, copy and paste at
        // all. No Undo/Redo: ⌘Z belongs to ROI and Figure Builder.
        .item(
            &SubmenuBuilder::new(app, "Edit")
                .item(&PredefinedMenuItem::cut(app, None)?)
                .item(&PredefinedMenuItem::copy(app, None)?)
                .item(&PredefinedMenuItem::paste(app, None)?)
                .item(&PredefinedMenuItem::select_all(app, None)?)
                .build()?,
        )
        .item(&view.build()?)
        .item(&tools_menu.build()?)
        .item(
            &SubmenuBuilder::new(app, "Window")
                .item(&PredefinedMenuItem::minimize(app, None)?)
                .item(&PredefinedMenuItem::maximize(app, Some("Zoom"))?)
                .separator()
                .item(&PredefinedMenuItem::close_window(app, None)?)
                .build()?,
        )
        .item(&help.build()?)
        .build()
}

/// The menu bar where the platform has one outside the window; nowhere else.
pub fn install(app: &AppHandle, info: &serde_json::Value) -> tauri::Result<()> {
    if !NATIVE_MENU_BAR {
        return Ok(());
    }
    let menu = build(app, info)?;
    app.set_menu(menu)?;
    Ok(())
}

pub fn on_event(app: &AppHandle, event: MenuEvent) {
    let id = event.id().as_ref().to_string();
    if id.starts_with("page:") {
        if let Some(window) = windows::focused_window(app) {
            let _ = app.emit_to(window.label(), "plexora://menu", serde_json::json!({ "id": id }));
        }
        return;
    }
    run(app, &id, None);
}

/// One native action, for a menu item or for the page's own navbar row
/// (`shell_action`). `window` is the window that asked; a menu item acts on
/// the focused one.
pub fn run(app: &AppHandle, id: &str, window: Option<WebviewWindow>) {
    let target = || window.clone().or_else(|| windows::focused_window(app));
    match id {
        "new_window" => {
            // Off the main thread, where building a WebView would wait on the
            // very event loop this handler is running in.
            let app = app.clone();
            std::thread::spawn(move || {
                let _ = windows::new_window(&app);
            });
        }
        "open_in_browser" => {
            if let Some(ready) = app.state::<ServerState>().0.ready() {
                let _ = app.opener().open_url(&ready.url, None::<&str>);
            }
        }
        "close_window" => {
            if let Some(window) = target() {
                let _ = window.close();
            }
        }
        "reload" => {
            if let Some(window) = target() {
                let _ = window.eval("window.location.reload()");
            }
        }
        "fullscreen" => {
            if let Some(window) = target() {
                let now = window.is_fullscreen().unwrap_or(false);
                let _ = window.set_fullscreen(!now);
            }
        }
        "zoom_in" | "zoom_out" | "zoom_reset" => {
            if let Some(window) = target() {
                zoom(app, &window, id);
            }
        }
        "devtools" =>
        {
            #[cfg(debug_assertions)]
            if let Some(window) = target() {
                window.open_devtools();
            }
        }
        "docs" => {
            let _ = app.opener().open_url(DOCS_URL, None::<&str>);
        }
        "show_data" => {
            if let Some(ready) = app.state::<ServerState>().0.ready() {
                let _ = app.opener().open_path(&ready.data_root, None::<&str>);
            }
        }
        "show_logs" => {
            if let Ok(dir) = app.path().app_log_dir() {
                let _ = std::fs::create_dir_all(&dir);
                let _ = app.opener().open_path(dir.to_string_lossy(), None::<&str>);
            }
        }
        _ => {}
    }
}

/// The page's zoom, per window, in tenths: the whole interface larger or
/// smaller, which is not the image's zoom.
fn zoom(app: &AppHandle, window: &WebviewWindow, id: &str) {
    let state = app.state::<windows::WindowState>();
    let mut zooms = state.zoom.lock().unwrap();
    let current = *zooms.get(window.label()).unwrap_or(&1.0);
    let next = match id {
        "zoom_in" => (current + 0.1).min(3.0),
        "zoom_out" => (current - 0.1).max(0.5),
        _ => 1.0,
    };
    if window.set_zoom(next).is_ok() {
        zooms.insert(window.label().to_string(), next);
    }
}
