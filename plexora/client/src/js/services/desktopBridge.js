/**
 * The page's side of the Plexora desktop app.
 *
 * `window.PlexoraDesktop` is an object inside the app's window and `null`
 * everywhere else -- a browser tab, a notebook iframe, a browser tab of the
 * very server the app started ("Open in Browser"). Every caller feature-tests
 * it, so the same page works in all of them and the browser keeps exactly the
 * behaviour it had before the app existed.
 *
 * Loaded in <head>, before anything else that could ask, and never swapped
 * out by the app router: the shell's event subscriptions below live for the
 * whole page and have to survive a route change.
 *
 * What the shell provides is in desktop/src-tauri/src/commands.rs; this file
 * is only the translation. Nothing here reads a token -- the page is already
 * authenticated by the cookie its first request set.
 */
(function () {
    "use strict";

    const tauri = window.__TAURI__;
    const shell = window.__PLEXORA_DESKTOP__;
    if (!tauri || !tauri.core || typeof tauri.core.invoke !== "function" || !shell) {
        window.PlexoraDesktop = null;
        return;
    }

    const invoke = tauri.core.invoke;
    const listen = tauri.event && tauri.event.listen;
    document.documentElement.classList.add("is-desktop");

    function base() {
        return window.PLEXORA_BASE_URL || "";
    }

    function toast(options) {
        if (window.PlexoraToast) window.PlexoraToast.show(options);
    }

    // -- files ---------------------------------------------------------------

    /**
     * A native Open dialog, parented to this window.
     * @param mode "file" | "directory"
     * @param filter one of native_dialog.py's filter names ("image", "csv",
     *   "h5ad", "data", "channels", "any")
     * @returns the chosen paths, or [] if cancelled.
     */
    function pickPaths({mode = "file", multiple = false, filter = "any",
                        title = null, defaultPath = null} = {}) {
        return invoke("pick_paths", {
            mode: mode === "directory" ? "directory" : "file",
            multiple: Boolean(multiple),
            filter: filter || "any",
            title: title || null,
            defaultPath: defaultPath || null,
        });
    }

    /** Say where a file went, with the way to it. */
    function announceSaved(path, name) {
        toast({
            title: `Saved ${name || basename(path)}`,
            note: path,
            timeout: 8000,
            actions: [{label: shell.platform === "macos" ? "Show in Finder" : "Show in folder",
                       onSelect: () => { invoke("reveal_path", {path}).catch(() => {}); }}],
        });
    }

    /**
     * Save a Blob through a native Save dialog. Resolves the path, or null
     * when the user cancelled. A WebView cannot save a Blob by itself (a
     * `download` link does nothing in WKWebView), so every in-page export in
     * the app comes through here.
     */
    async function saveBlob(blob, filename, {announce = true} = {}) {
        const bytes = new Uint8Array(await blob.arrayBuffer());
        const name = filename || "download";
        const path = await invoke("save_bytes", bytes, {
            headers: {"x-plexora-name": encodeURIComponent(name)},
        });
        if (path && announce) announceSaved(path, name);
        return path;
    }

    /** The bytes of a local file the user dropped. */
    async function readFile(path) {
        const result = await invoke("read_file", {path});
        return result instanceof ArrayBuffer ? new Uint8Array(result) : new Uint8Array(result || []);
    }

    function basename(path) {
        return String(path).split(/[\\/]/).filter(Boolean).pop() || "file";
    }

    const MIME = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
                  webp: "image/webp", svg: "image/svg+xml", tif: "image/tiff", tiff: "image/tiff"};

    /** A dropped path as a `File`, for code written against the DOM's. */
    async function fileFromPath(path) {
        const name = basename(path);
        const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
        return new File([await readFile(path)], name, {type: MIME[ext] || ""});
    }

    // -- the app -------------------------------------------------------------

    function openInBrowser(path) {
        const here = path || (window.location.pathname + window.location.search);
        return invoke("open_in_browser", {path: here});
    }

    const api = {
        version: shell.version,
        platform: shell.platform,
        arch: shell.arch,
        webview: shell.webview,
        serverOrigin: shell.serverOrigin,

        /** The shell's dialogs pick one kind at a time on every platform. */
        browseCapability: () => "kinds",
        pickPaths,
        saveBlob,
        readFile,
        fileFromPath,
        revealPath: (path) => invoke("reveal_path", {path}),
        openInBrowser,
        openUrl: (url) => invoke("open_url", {url}),
        quit: () => invoke("quit_app"),
        newWindow: () => invoke("new_window"),
        serverInfo: () => invoke("server_info"),
        takePendingOpens: () => invoke("take_pending_opens"),
        toggleFullscreen: () => invoke("toggle_fullscreen"),
        isFullscreen: () => invoke("is_fullscreen"),
        notify: (title, body) => invoke("notify", {title, body: body || null}),
        /** A notification only when the user is not looking at this window:
         *  a finished export they are watching needs no second announcement. */
        notifyIfAway({title, body} = {}) {
            if (!title) return Promise.resolve(false);
            if (!document.hidden && document.hasFocus()) return Promise.resolve(false);
            return invoke("notify", {title, body: body || null}).then(() => true, () => false);
        },
        async copyImagePng(blob) {
            return invoke("copy_image", new Uint8Array(await blob.arrayBuffer()));
        },
    };

    // -- menus -------------------------------------------------------------------
    //
    // A native menu item for something the page already does clicks the same
    // element the page's own shortcut clicks, so the two can never disagree.

    const MENU_TARGETS = {
        "page:import": "#sample-import-menu",
        "page:open": 'a[data-shortcut="mod+o"]',
        "page:settings": "#nav_settings",
        "page:export_png": "#nav_export_image_png",
        "page:export_pdf": "#nav_export_image_pdf",
        "page:scalebar": "#nav_toggle_scalebar",
    };

    function menuTarget(id) {
        if (id.startsWith("page:tool:")) {
            const name = id.slice("page:tool:".length);
            return document.querySelector(`[data-tool="${CSS.escape(name)}"]`);
        }
        const selector = MENU_TARGETS[id];
        return selector ? document.querySelector(selector) : null;
    }

    function runMenu(id) {
        const event = new CustomEvent("plexora:menu", {detail: {id}, cancelable: true});
        if (!document.dispatchEvent(event)) return;
        const target = menuTarget(id);
        const unavailable = !target || target.disabled || target.closest(".disabled");
        if (unavailable) {
            if (id.startsWith("page:tool:") || id.startsWith("page:export") || id === "page:scalebar") {
                toast({title: "Open a sample first",
                       note: "That menu item works on the sample shown in the viewer."});
            }
            return;
        }
        target.click();
    }

    // -- the shell's own keys ----------------------------------------------------
    //
    // Windows and WebKitGTK never fire a menu accelerator while the WebView
    // has keyboard focus -- which is nearly always -- so the chords the
    // shell's menu owns are run from here instead. macOS hands key
    // equivalents to the menu before the page sees them, so there this stays
    // out of the way. None of these chords is one a page or plugin can bind
    // (plexora/api/plugin.py refuses mod+Q/W/N), so nothing else is displaced.

    const SHELL_CHORDS = [
        {key: "n", mod: true, run: () => api.newWindow()},
        {key: "w", mod: true, run: () => invoke("close_window")},
        {key: "q", mod: true, run: () => api.quit()},
        {key: "b", mod: true, shift: true, run: () => openInBrowser()},
        {key: "F11", run: () => api.toggleFullscreen()},
    ];

    function shellChord(event) {
        return SHELL_CHORDS.find((chord) =>
            (chord.key.length === 1 ? event.key.toLowerCase() === chord.key : event.key === chord.key)
            && Boolean(chord.mod) === (event.ctrlKey || event.metaKey)
            && Boolean(chord.shift) === event.shiftKey
            && !event.altKey);
    }

    if (shell.platform !== "macos") {
        document.addEventListener("keydown", (event) => {
            if (event.defaultPrevented || event.repeat) return;
            const chord = shellChord(event);
            if (!chord) return;
            event.preventDefault();
            Promise.resolve(chord.run()).catch(() => {});
        });
    }

    // -- things opened from outside ------------------------------------------------

    function navigate(url) {
        if (window.PlexoraRouter && typeof window.PlexoraRouter.go === "function") {
            window.PlexoraRouter.go(url);
        } else {
            window.location.href = url;
        }
    }

    /** Paths handed over by Explorer/Finder or a second launch: open them,
     *  importing first when they are not a sample yet. When that needs a
     *  decision, the Import dialog asks it. */
    async function openPaths(paths) {
        if (!paths || !paths.length) return;
        try {
            const response = await fetch(`${base()}/desktop/open`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({paths}),
            });
            const result = await response.json();
            if (response.ok && result.url) {
                navigate(result.url);
                return;
            }
        } catch (error) {
            // Falls through to the dialog, which says what went wrong.
        }
        if (window.PlexoraImportSample) {
            window.PlexoraImportSample.open({paths});
        } else {
            toast({title: "Plexora could not open that", note: paths.join("\n"), tone: "warning"});
        }
    }

    async function collectOpens() {
        let items = [];
        try {
            items = await api.takePendingOpens();
        } catch (error) {
            return;
        }
        for (const item of items || []) {
            if (item.kind === "project" && item.name) {
                navigate(`${base()}/${encodeURIComponent(item.name)}`);
            } else if (item.kind === "paths") {
                await openPaths(item.paths);
            }
        }
    }

    // -- native drag and drop ----------------------------------------------------
    //
    // The shell takes file drops natively, which is what yields PATHS rather
    // than bytes -- a 40 GB slide is opened where it lies, never uploaded.
    // Re-dispatched as DOM events so any view can claim a drop over itself
    // (the figure canvas does): `plexora:native-drop` is cancelable, and one
    // nobody cancels opens the Import dialog with the paths already in it.

    function cssPoint(position) {
        const ratio = window.devicePixelRatio || 1;
        return {x: (position?.x || 0) / ratio, y: (position?.y || 0) / ratio};
    }

    function onNativeDrag(phase, payload) {
        const point = cssPoint(payload?.position);
        document.documentElement.classList.toggle("is-native-dragging",
                                                   phase === "enter" || phase === "over");
        document.dispatchEvent(new CustomEvent("plexora:native-drag", {
            detail: {phase, paths: payload?.paths || [], x: point.x, y: point.y},
        }));
    }

    function onNativeDrop(payload) {
        document.documentElement.classList.remove("is-native-dragging");
        const paths = payload?.paths || [];
        if (!paths.length) return;
        const point = cssPoint(payload?.position);
        const event = new CustomEvent("plexora:native-drop", {
            detail: {paths, x: point.x, y: point.y}, cancelable: true,
        });
        if (!document.dispatchEvent(event)) return;
        if (window.PlexoraImportSample) {
            window.PlexoraImportSample.dropPaths(paths);
        } else {
            openPaths(paths);
        }
    }

    // -- downloads the server sent -------------------------------------------------

    function onDownloadFinished(payload) {
        if (!payload || payload.cancelled) return;
        if (!payload.success) {
            toast({title: `Could not save ${payload.name}`, tone: "warning"});
            return;
        }
        announceSaved(payload.path, payload.name);
        api.notifyIfAway({title: "Export saved", body: payload.name});
    }

    // -- what the WebView can draw -------------------------------------------------

    function reportCapabilities() {
        try {
            if (sessionStorage.getItem("plexora.desktop.capabilities")) return;
            sessionStorage.setItem("plexora.desktop.capabilities", "1");
        } catch (error) {
            // Storage refused: report anyway, once per page.
        }
        let webgl2 = false;
        try {
            webgl2 = Boolean(document.createElement("canvas").getContext("webgl2"));
        } catch (error) {
            webgl2 = false;
        }
        invoke("report_capabilities", {
            webgl2, offscreenCanvas: typeof OffscreenCanvas === "function",
        }).catch(() => {});
    }

    if (typeof listen === "function") {
        listen("plexora://menu", (event) => runMenu(String(event.payload?.id || "")));
        listen("plexora://opens-pending", () => collectOpens());
        listen("plexora://download-finished", (event) => onDownloadFinished(event.payload));
        listen("tauri://drag-enter", (event) => onNativeDrag("enter", event.payload));
        listen("tauri://drag-over", (event) => onNativeDrag("over", event.payload));
        listen("tauri://drag-leave", () => onNativeDrag("leave", null));
        listen("tauri://drag-drop", (event) => onNativeDrop(event.payload));
    }

    document.addEventListener("DOMContentLoaded", () => {
        collectOpens();
        reportCapabilities();
        document.getElementById("nav_open_in_browser")?.addEventListener("click", () => {
            openInBrowser();
        });
    });

    // Exposed for the probe in tests/js; not part of the page API.
    api._internal = {runMenu, menuTarget, cssPoint, openPaths, onNativeDrop, shellChord};

    window.PlexoraDesktop = Object.freeze(api);
})();
