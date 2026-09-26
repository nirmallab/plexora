/**
 * The desktop app's bridge (services/desktopBridge.js), against a stubbed
 * shell.
 *
 * What has to hold:
 *
 *   - **Null outside the app.** A browser tab -- including a tab of the very
 *     server the app started -- gets `PlexoraDesktop === null` and no
 *     `is-desktop` class, so every caller's feature test takes the browser
 *     path it always took.
 *   - **A native menu item clicks what the page's own shortcut clicks.** One
 *     executor per action, so a key and a menu row can never disagree.
 *   - **A drop nobody claims opens the Import dialog with the paths in it;**
 *     one a view claims (the figure canvas) goes nowhere else.
 *   - **The shell's chords run from the page off macOS**, where menu
 *     accelerators never fire while the WebView has focus -- and never on
 *     macOS, where they would fire twice.
 *   - **Bytes go to the Save dialog raw**, the name percent-encoded.
 *
 * Run directly:  node tests/js/desktop_bridge_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/desktopBridge.js"), "utf8");

let failures = 0;
function check(label, condition) {
    if (condition) {
        console.log(`ok - ${label}`);
    } else {
        failures += 1;
        console.log(`FAIL - ${label}`);
    }
}

class CustomEvent {
    constructor(type, init = {}) {
        this.type = type;
        this.detail = init.detail;
        this.cancelable = Boolean(init.cancelable);
        this.defaultPrevented = false;
    }
    preventDefault() {
        if (this.cancelable) this.defaultPrevented = true;
    }
}

function makeElement(id, extra = {}) {
    return {
        id, clicks: 0, disabled: false, listeners: {},
        click() { this.clicks += 1; },
        closest() { return null; },
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
        ...extra,
    };
}

function makeWorld({ desktop = true, platform = "windows" } = {}) {
    const listeners = {};
    const classes = new Set();
    const elements = {
        "#sample-import-menu": makeElement("sample-import-menu"),
        'a[data-shortcut="mod+o"]': makeElement("samples"),
        "#nav_settings": makeElement("nav_settings"),
        "#nav_export_image_png": makeElement("png"),
        "#nav_export_image_pdf": makeElement("pdf"),
        "#nav_toggle_scalebar": makeElement("scalebar"),
        '[data-tool="roi"]': makeElement("roi"),
    };
    const document = {
        hidden: false,
        documentElement: {
            classList: {
                add: (c) => classes.add(c), remove: (c) => classes.delete(c),
                contains: (c) => classes.has(c),
                toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
            },
        },
        hasFocus: () => true,
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        dispatchEvent(event) {
            (listeners[event.type] || []).forEach((fn) => fn(event));
            return !event.defaultPrevented;
        },
        querySelector: (selector) => elements[selector] || null,
        getElementById: (id) => Object.values(elements).find((el) => el.id === id) || null,
        createElement: () => ({ getContext: () => ({}) }),
    };
    const invoked = [];
    const tauriListeners = {};
    const toasts = [];
    const drops = [];
    const navigations = [];
    const pending = [];
    const window = {
        document,
        devicePixelRatio: 2,
        PLEXORA_BASE_URL: "",
        location: { pathname: "/tonsil", search: "?tool=roi", href: "" },
        PlexoraToast: { show: (options) => toasts.push(options) },
        PlexoraImportSample: { dropPaths: (paths) => drops.push(paths), open: () => {} },
        PlexoraRouter: { go: (url) => navigations.push(url) },
    };
    if (desktop) {
        window.__TAURI__ = {
            core: {
                invoke: (command, args, options) => {
                    invoked.push({ command, args, options });
                    if (command === "take_pending_opens") return Promise.resolve(pending.splice(0));
                    if (command === "save_bytes") return Promise.resolve("C:\\out\\view.png");
                    return Promise.resolve(null);
                },
            },
            event: {
                listen: (name, fn) => { (tauriListeners[name] ||= []).push(fn); return Promise.resolve(() => {}); },
            },
        };
        window.__PLEXORA_DESKTOP__ = { version: "0.0.23", platform, arch: "x86_64",
                                       webview: "webview2", serverOrigin: "http://127.0.0.1:8420" };
    }
    window.window = window;
    const context = createContext({
        window, document, CustomEvent, CSS: { escape: (s) => s }, Promise, Uint8Array,
        ArrayBuffer, Blob, File: class {}, sessionStorage: { getItem: () => "1", setItem() {} },
        fetch: () => Promise.reject(new Error("no network in the probe")),
        OffscreenCanvas: function () {}, console, setTimeout, encodeURIComponent,
    });
    runInContext(SOURCE, context);
    return {
        window, document, classes, elements, invoked, toasts, drops, navigations, pending,
        emit(name, payload) { (tauriListeners[name] || []).forEach((fn) => fn({ payload })); },
        key(init) {
            const event = { type: "keydown", repeat: false, defaultPrevented: false,
                            ctrlKey: false, metaKey: false, shiftKey: false, altKey: false,
                            preventDefault() { this.defaultPrevented = true; }, ...init };
            (listeners.keydown || []).forEach((fn) => fn(event));
            return event;
        },
    };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

// -- outside the app -----------------------------------------------------------

{
    const world = makeWorld({ desktop: false });
    check("a browser tab gets PlexoraDesktop === null", world.window.PlexoraDesktop === null);
    check("and no is-desktop class", !world.classes.has("is-desktop"));
}

// -- inside it -----------------------------------------------------------------

{
    const world = makeWorld();
    const api = world.window.PlexoraDesktop;
    check("the app's window gets the bridge", api && typeof api.pickPaths === "function");
    check("and the is-desktop class", world.classes.has("is-desktop"));
    check("the bridge's methods cannot be swapped out from under its callers", Object.isFrozen(api));
    check("the shell's dialogs are one kind at a time", api.browseCapability() === "kinds");

    world.emit("plexora://menu", { id: "page:settings" });
    check("File > Settings clicks the navbar's Settings row", world.elements["#nav_settings"].clicks === 1);
    world.emit("plexora://menu", { id: "page:tool:roi" });
    check("a Tools row clicks that tool's own menu row", world.elements['[data-tool="roi"]'].clicks === 1);
    world.emit("plexora://menu", { id: "page:tool:gating" });
    check("a tool with no row on this page says to open a sample",
          world.toasts.some((t) => /Open a sample/.test(t.title)));

    world.window.document.addEventListener("plexora:menu", (event) => {
        if (event.detail.id === "page:import") event.preventDefault();
    });
    world.emit("plexora://menu", { id: "page:import" });
    check("a view that claims a menu item stops the default click",
          world.elements["#sample-import-menu"].clicks === 0);

    const point = api._internal.cssPoint({ x: 800, y: 400 });
    check("drop positions are converted from physical to CSS pixels", point.x === 400 && point.y === 200);

    world.emit("tauri://drag-drop", { paths: ["C:\\data\\slide.ome.tif"], position: { x: 10, y: 10 } });
    check("an unclaimed drop opens the Import dialog with the paths",
          world.drops.length === 1 && world.drops[0][0] === "C:\\data\\slide.ome.tif");

    world.document.addEventListener("plexora:native-drop", (event) => event.preventDefault());
    world.emit("tauri://drag-drop", { paths: ["C:\\data\\figure.png"], position: { x: 10, y: 10 } });
    check("a drop a view claims goes nowhere else", world.drops.length === 1);

    world.emit("tauri://drag-enter", { paths: ["x"], position: { x: 0, y: 0 } });
    check("dragging a file over the window marks the page", world.classes.has("is-native-dragging"));
    world.emit("tauri://drag-leave", null);
    check("and leaving unmarks it", !world.classes.has("is-native-dragging"));

    const ctrlN = world.key({ key: "n", ctrlKey: true });
    await tick();
    check("Ctrl+N opens a window from the page on Windows",
          ctrlN.defaultPrevented && world.invoked.some((c) => c.command === "new_window"));
    world.key({ key: "w", ctrlKey: true });
    check("Ctrl+W closes this window", world.invoked.some((c) => c.command === "close_window"));
    world.key({ key: "b", ctrlKey: true, shiftKey: true });
    check("Ctrl+Shift+B opens this page in the browser",
          world.invoked.some((c) => c.command === "open_in_browser" && c.args.path === "/tonsil?tool=roi"));
    const plain = world.key({ key: "e", ctrlKey: true });
    check("a chord the page owns is left to the page", !plain.defaultPrevented);

    const blob = { arrayBuffer: () => Promise.resolve(new Uint8Array([1, 2, 3]).buffer) };
    const saved = await api.saveBlob(blob, "café view.png");
    const call = world.invoked.find((c) => c.command === "save_bytes");
    check("saveBlob sends raw bytes", call && call.args instanceof Uint8Array && call.args.length === 3);
    check("with the name percent-encoded in a header",
          call && call.options.headers["x-plexora-name"] === "caf%C3%A9%20view.png");
    check("and says where the file went", saved === "C:\\out\\view.png"
          && world.toasts.some((t) => /Saved café view\.png/.test(t.title)));

    world.pending.push({ kind: "project", name: "tonsil 2" });
    world.emit("plexora://opens-pending", null);
    await tick(); await tick();
    check("a project handed over by a second launch is opened",
          world.navigations.includes("/tonsil%202"));
}

// -- on macOS ------------------------------------------------------------------

{
    const world = makeWorld({ platform: "macos" });
    const event = world.key({ key: "n", metaKey: true });
    await tick();
    check("on macOS the menu owns Cmd+N and the page leaves it alone",
          !event.defaultPrevented && !world.invoked.some((c) => c.command === "new_window"));
}

if (failures) {
    console.log(`${failures} failed`);
    process.exit(1);
}
