//! The native menu bar.
//!
//! One rule keeps a key from firing twice: the page stays the only thing
//! that runs a chord it already binds (mod+I, mod+O, mod+, and every tool's
//! shortcut). The native items for those carry no accelerator -- only a hint
//! in the label -- and send `plexora://menu` to the page, which clicks the
//! same element its own shortcut would. Accelerators here are only keys the
//! page provably never binds: Plexora's plugin API refuses mod+Q/W/N/T/H/M.
//!
//! On Windows (and WebKitGTK) a menu accelerator never fires while the
//! WebView has keyboard focus, which is nearly always, so the bridge runs
//! the shell's own chords there as well (desktopBridge.js, SHELL_CHORDS).
//! Only one of the two can ever see a given key press.

use tauri::menu::{
    AboutMetadataBuilder, Menu, MenuBuilder, MenuEvent, MenuItemBuilder, PredefinedMenuItem,
    SubmenuBuilder,
};
use tauri::{AppHandle, Emitter, Manager, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use crate::{windows, ServerState};

const DOCS_URL: &str = "https://github.com/nirmallab/plexora";

/// `mod+shift+e` as this platform writes it, for a label hint.
fn chord_hint(spec: &str) -> String {
    let mac = cfg!(target_os = "macos");
    spec.split('+')
        .map(|part| match part {
            "mod" => if mac { "⌘".to_string() } else { "Ctrl".to_string() },
            "shift" => if mac { "⇧".to_string() } else { "Shift".to_string() },
            "alt" => if mac { "⌥".to_string() } else { "Alt".to_string() },
            key => key.to_uppercase(),
        })
        .collect::<Vec<_>>()
        .join(if mac { "" } else { "+" })
}

/// A label with the page's chord shown beside it. Windows draws the text
/// after a tab in the accelerator column; elsewhere it would be literal, so
/// the hint is left off rather than drawn badly.
fn hinted(label: &str, chord: Option<&str>) -> String {
    match chord {
        Some(chord) if cfg!(windows) => format!("{label}\t{}", chord_hint(chord)),
        _ => label.to_string(),
    }
}

pub fn build(app: &AppHandle, info: &serde_json::Value) -> tauri::Result<Menu<Wry>> {
    let item = |id: &str, label: &str| MenuItemBuilder::with_id(id, label).build(app);
    let keyed = |id: &str, label: &str, accel: &str| {
        MenuItemBuilder::with_id(id, label).accelerator(accel).build(app)
    };
    let mac = cfg!(target_os = "macos");

    let mut file = SubmenuBuilder::new(app, "File")
        .item(&keyed("new_window", "New Window", "CmdOrCtrl+N")?)
        .separator()
        .item(&item("page:import", &hinted("Import Sample…", Some("mod+i")))?)
        .item(&item("page:open", &hinted("Samples…", Some("mod+o")))?)
        .separator()
        .item(
            &SubmenuBuilder::new(app, "Export Image")
                .item(&item("page:export_png", "PNG…")?)
                .item(&item("page:export_pdf", "PDF…")?)
                .build()?,
        )
        .separator()
        .item(&keyed("open_in_browser", "Open in Browser", "CmdOrCtrl+Shift+B")?)
        .item(&item("page:settings", &hinted("Settings…", Some("mod+,")))?)
        .separator()
        .item(&keyed("close_window", "Close Window", "CmdOrCtrl+W")?);
    if !mac {
        file = file.separator().item(&keyed("quit", "Exit", "CmdOrCtrl+Q")?);
    }

    let tools: Vec<(String, String, Option<String>, String)> = info
        .get("tools")
        .and_then(|tools| tools.as_array())
        .map(|tools| {
            tools
                .iter()
                .filter_map(|tool| {
                    Some((
                        tool.get("name")?.as_str()?.to_string(),
                        tool.get("label")?.as_str()?.to_string(),
                        tool.get("shortcut").and_then(|s| s.as_str()).map(str::to_string),
                        tool.get("menu").and_then(|s| s.as_str()).unwrap_or("tools").to_string(),
                    ))
                })
                .collect()
        })
        .unwrap_or_default();

    let mut view = SubmenuBuilder::new(app, "View").item(&keyed("reload", "Reload", "CmdOrCtrl+R")?);
    let view_tools: Vec<_> = tools.iter().filter(|t| t.3 == "view").collect();
    if !view_tools.is_empty() {
        view = view.separator();
        for (name, label, shortcut, _) in &view_tools {
            view = view.item(&item(&format!("page:tool:{name}"), &hinted(label, shortcut.as_deref()))?);
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
        .item(&keyed("fullscreen", "Toggle Full Screen", if mac { "Ctrl+Cmd+F" } else { "F11" })?);
    if cfg!(debug_assertions) {
        view = view.separator().item(&item("devtools", "Developer Tools")?);
    }

    let mut tools_menu = SubmenuBuilder::new(app, "Tools");
    let tool_rows: Vec<_> = tools.iter().filter(|t| t.3 != "view").collect();
    if tool_rows.is_empty() {
        tools_menu = tools_menu.item(&MenuItemBuilder::new("No tools installed").enabled(false).build(app)?);
    }
    for (name, label, shortcut, _) in tool_rows {
        tools_menu = tools_menu.item(&item(&format!("page:tool:{name}"), &hinted(label, shortcut.as_deref()))?);
    }

    let mut help = SubmenuBuilder::new(app, "Help")
        .item(&item("docs", "Plexora Documentation")?)
        .separator()
        .item(&item("show_data", "Show Data Folder")?)
        .item(&item("show_logs", "Show Logs")?);
    if !mac {
        help = help.separator().item(&item("about", "About Plexora")?);
    }

    let mut menu = MenuBuilder::new(app);
    if mac {
        let about = AboutMetadataBuilder::new()
            .name(Some("Plexora"))
            .version(Some(app.package_info().version.to_string()))
            .website(Some(DOCS_URL))
            .build();
        menu = menu.item(
            &SubmenuBuilder::new(app, "Plexora")
                .item(&PredefinedMenuItem::about(app, Some("About Plexora"), Some(about))?)
                .separator()
                .item(&PredefinedMenuItem::services(app, None)?)
                .separator()
                .item(&PredefinedMenuItem::hide(app, None)?)
                .item(&PredefinedMenuItem::hide_others(app, None)?)
                .item(&PredefinedMenuItem::show_all(app, None)?)
                .separator()
                .item(&PredefinedMenuItem::quit(app, None)?)
                .build()?,
        );
    }
    menu = menu.item(&file.build()?);
    if mac {
        // WKWebView needs these for text fields to cut, copy and paste at
        // all. No Undo/Redo: ⌘Z belongs to ROI and Figure Builder.
        menu = menu.item(
            &SubmenuBuilder::new(app, "Edit")
                .item(&PredefinedMenuItem::cut(app, None)?)
                .item(&PredefinedMenuItem::copy(app, None)?)
                .item(&PredefinedMenuItem::paste(app, None)?)
                .item(&PredefinedMenuItem::select_all(app, None)?)
                .build()?,
        );
    }
    menu = menu.item(&view.build()?).item(&tools_menu.build()?);
    if mac {
        menu = menu.item(
            &SubmenuBuilder::new(app, "Window")
                .item(&PredefinedMenuItem::minimize(app, None)?)
                .item(&PredefinedMenuItem::maximize(app, Some("Zoom"))?)
                .separator()
                .item(&PredefinedMenuItem::close_window(app, None)?)
                .build()?,
        );
    }
    menu.item(&help.build()?).build()
}

pub fn install(app: &AppHandle, info: &serde_json::Value) -> tauri::Result<()> {
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
    match id.as_str() {
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
            if let Some(window) = windows::focused_window(app) {
                let _ = window.close();
            }
        }
        "quit" => app.exit(0),
        "reload" => {
            if let Some(window) = windows::focused_window(app) {
                let _ = window.eval("window.location.reload()");
            }
        }
        "fullscreen" => {
            if let Some(window) = windows::focused_window(app) {
                let now = window.is_fullscreen().unwrap_or(false);
                let _ = window.set_fullscreen(!now);
            }
        }
        "zoom_in" | "zoom_out" | "zoom_reset" => zoom(app, &id),
        "devtools" =>
        {
            #[cfg(debug_assertions)]
            if let Some(window) = windows::focused_window(app) {
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
        "about" => about(app),
        _ => {}
    }
}

fn zoom(app: &AppHandle, id: &str) {
    let Some(window) = windows::focused_window(app) else { return };
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

fn about(app: &AppHandle) {
    let shared = app.state::<ServerState>().0.clone();
    let mut lines = vec![format!("Plexora {}", app.package_info().version)];
    if let Some(ready) = shared.ready() {
        lines.push(format!("Server {} on {}", ready.version, ready.origin));
        lines.push(format!("Data folder: {}", ready.data_root));
    }
    if let Ok(dir) = app.path().app_log_dir() {
        lines.push(format!("Logs: {}", dir.display()));
    }
    app.dialog()
        .message(lines.join("\n"))
        .title("About Plexora")
        .kind(MessageDialogKind::Info)
        .show(|_| {});
}
