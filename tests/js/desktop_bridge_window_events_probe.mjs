/**
 * The desktop bridge (services/desktopBridge.js) with two app windows open:
 * an event the shell sends to ONE window is handled by that window only.
 *
 * The shell targets three events at a single window with `emit_to(label)`:
 * `plexora://menu` (the focused window, menu.rs), `plexora://download-finished`
 * (the window whose download finished, downloads.rs) and
 * `plexora://opens-pending` (the main window, opens.rs). How Tauri 2 routes
 * such an event to a page's listeners (tauri-2.12.5 event/listener.rs
 * emit_js_filter + match_any_or_filter, manager/mod.rs emit_to): a listener
 * registered with the global `event.listen` has target `{kind: "Any"}` and
 * hears an event whatever window it was sent to; one registered with
 * `getCurrentWebview().listen` has target `{kind: "Webview", label}` and hears
 * only events sent to its own label. The stub router below routes exactly so,
 * with one vm context per window.
 *
 * Run directly:  node tests/js/desktop_bridge_window_events_probe.mjs
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

/** Tauri 2's JS listener table, as emit_to routes through it. */
function makeShell() {
    const handlers = []; // {webview, event, target, fn}
    return {
        handlers,
        register(webview, event, target, fn) {
            handlers.push({ webview, event, target, fn });
            return Promise.resolve(() => {});
        },
        /** app.emit_to(label, event, payload): every webview's handlers whose
         *  target is Any, or names that label. */
        emitTo(label, event, payload) {
            for (const h of handlers) {
                if (h.event !== event) continue;
                const t = h.target;
                const matches = t.kind === "Any" || (t.label === label);
                if (matches) h.fn({ event, payload });
            }
        },
    };
}

function makeWindow(shell, label) {
    const listeners = {};
    const clicks = {};
    const element = (id) => ({
        id, disabled: false, click() { clicks[id] = (clicks[id] || 0) + 1; },
        closest() { return null; }, addEventListener() {},
    });
    const elements = {
        "#nav_settings": element("nav_settings"),
        "#nav_export_project": element("nav_export_project"),
    };
    const document = {
        hidden: true,
        documentElement: { classList: { add() {}, remove() {}, contains: () => false, toggle() {} } },
        hasFocus: () => false,
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
    const toasts = [];
    const window = {
        document,
        devicePixelRatio: 1,
        PLEXORA_BASE_URL: "",
        location: { pathname: "/tonsil", search: "", href: "", reload() {} },
        PlexoraToast: { show: (options) => toasts.push(options) },
        PlexoraRouter: { go() {} },
        __TAURI__: {
            core: {
                invoke: (command, args) => {
                    invoked.push({ command, args });
                    return Promise.resolve(command === "take_pending_opens" ? [] : null);
                },
            },
            event: {
                // The global listen: Tauri registers {kind: "Any"}.
                listen: (name, fn) => shell.register(label, name, { kind: "Any" }, fn),
            },
            webview: {
                getCurrentWebview: () => ({
                    label,
                    listen: (name, fn) => shell.register(label, name, { kind: "Webview", label }, fn),
                }),
            },
        },
        __PLEXORA_DESKTOP__: { version: "0.0.55", platform: "macos", arch: "aarch64",
                               webview: "wkwebview", serverOrigin: "http://127.0.0.1:8420" },
    };
    window.window = window;
    const context = createContext({
        window, document, CustomEvent, CSS: { escape: (s) => s }, Promise, Uint8Array,
        ArrayBuffer, Blob, File: class {}, sessionStorage: { getItem: () => "1", setItem() {} },
        fetch: () => Promise.reject(new Error("no network in the probe")),
        OffscreenCanvas: function () {}, console, setTimeout, encodeURIComponent,
    });
    runInContext(SOURCE, context);
    return { label, clicks, invoked, toasts };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

{
    const shell = makeShell();
    const main = makeWindow(shell, "main");
    const second = makeWindow(shell, "main-1");

    // File > Export Project... with the second window focused (menu.rs).
    shell.emitTo("main-1", "plexora://menu", { id: "page:export_project" });
    check("a menu item runs in the focused window",
          (second.clicks.nav_export_project || 0) === 1);
    check("and not in the other open window",
          (main.clicks.nav_export_project || 0) === 0);

    // A download the second window started has finished (downloads.rs).
    shell.emitTo("main-1", "plexora://download-finished",
                 { name: "tonsil.plexora", path: "C:\\out\\tonsil.plexora", success: true, cancelled: false });
    await tick();
    check("a finished download is announced in the window that downloaded it",
          second.toasts.some((t) => /Saved tonsil\.plexora/.test(t.title)));
    check("and not toasted or notified in the other open window",
          !main.toasts.some((t) => /Saved/.test(t.title))
          && !main.invoked.some((c) => c.command === "notify"));

    // A file opened from Finder/Explorer is waiting (opens.rs pings main).
    const before = second.invoked.filter((c) => c.command === "take_pending_opens").length;
    shell.emitTo("main", "plexora://opens-pending", null);
    await tick();
    check("pending opens are collected by the window they were sent to",
          main.invoked.some((c) => c.command === "take_pending_opens"));
    check("and not by the other open window",
          second.invoked.filter((c) => c.command === "take_pending_opens").length === before);
}

if (failures) {
    console.log(`${failures} check(s) failed`);
    process.exit(1);
}
