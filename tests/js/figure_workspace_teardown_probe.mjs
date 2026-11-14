/**
 * Does leaving the figure canvas take its window listeners with it?
 *
 * The canvas is a routed page: appRouter.js mounts it over the viewer without a
 * document load, and the back arrow puts the viewer back by calling
 * PlexoraPage.unmount(), which runs FigureWorkspace.destroy(). Nothing else
 * tears the workspace down -- the window outlives it, and so does every
 * listener left on the window.
 *
 * So this builds a workspace the way its page does (setup() against a DOM
 * where every id resolves), destroys it, and then asks the window two things:
 *
 *   1. which listeners setup() put on it are still there (counted through a
 *      window whose addEventListener/removeEventListener really keep a list);
 *   2. whether a Space keydown with a button focused -- the key that presses a
 *      focused button anywhere in the app -- is still cancelled by somebody.
 *
 * and asks core's drop-target registry (attachDropTarget, stubbed to count)
 * whether the page that is gone is still registered as a place files go.
 *
 * Run directly:
 *   node tests/js/figure_workspace_teardown_probe.mjs figureBuilderApi.js ...
 * The Python driver passes PLUGIN.scripts, read off the descriptor.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = process.argv.slice(2);

/** An event target that keeps its listeners, so what is left can be counted. */
function eventTarget() {
    const listeners = [];
    return {
        listeners,
        addEventListener(type, fn) {
            if (typeof fn !== "function") return;
            if (listeners.some((l) => l.type === type && l.fn === fn)) return;
            listeners.push({ type, fn });
        },
        removeEventListener(type, fn) {
            const at = listeners.findIndex((l) => l.type === type && l.fn === fn);
            if (at >= 0) listeners.splice(at, 1);
        },
        dispatch(event) {
            for (const l of listeners.slice()) {
                if (l.type === event.type) l.fn(event);
            }
            return event;
        },
    };
}

/** A DOM wide enough for the workspace to wire itself to: every id resolves. */
function workspacePage() {
    const byId = new Map();
    const rect = () => ({ left: 0, top: 0, right: 900, bottom: 700,
                          width: 900, height: 700, x: 0, y: 0 });
    const context2d = () => ({
        setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
        stroke() {}, fill() {}, fillRect() {}, fillText() {}, save() {}, restore() {},
        translate() {}, rotate() {}, scale() {}, drawImage() {}, putImageData() {},
        createImageData: (w, h) => ({ width: w, height: h,
                                      data: new Uint8ClampedArray(w * h * 4) }),
        measureText: () => ({ width: 8 }),
    });
    const make = (id, tag) => {
        const classes = new Set();
        const attributes = {};
        const element = {
            id: id || "", tagName: String(tag || "div").toUpperCase(),
            className: "", innerHTML: "", textContent: "", value: "", title: "",
            hidden: false, disabled: false, checked: false, width: 0, height: 0,
            style: (() => {
                const custom = {};
                return {
                    setProperty: (name, value) => { custom[name] = String(value); },
                    getPropertyValue: (name) => custom[name] || "",
                    removeProperty: (name) => { delete custom[name]; },
                };
            })(),
            dataset: {}, files: [], children: [], options: [],
            clientWidth: 900, clientHeight: 700, scrollLeft: 0, scrollTop: 0,
            classList: {
                add: (...names) => names.forEach((name) => classes.add(name)),
                remove: (...names) => names.forEach((name) => classes.delete(name)),
                contains: (name) => classes.has(name),
                toggle: (name, on) => {
                    const want = on === undefined ? !classes.has(name) : !!on;
                    if (want) classes.add(name); else classes.delete(name);
                    return want;
                },
            },
            append: (...kids) => { element.children.push(...kids); },
            appendChild: (kid) => { element.children.push(kid); return kid; },
            insertAdjacentHTML() {}, remove() {}, blur() {}, click() {},
            focus() {}, select() {},
            scrollTo() {}, scrollIntoView() {}, showModal() {}, close() {},
            setAttribute: (name, value) => { attributes[name] = String(value); },
            getAttribute: (name) => (name in attributes ? attributes[name] : null),
            removeAttribute: (name) => { delete attributes[name]; },
            addEventListener() {}, removeEventListener() {},
            querySelector: () => null, querySelectorAll: () => [],
            closest: () => null, contains: () => false,
            getBoundingClientRect: rect,
            getContext: () => context2d(),
        };
        return element;
    };
    return {
        readyState: "complete",
        getElementById: (id) => {
            if (!byId.has(id)) byId.set(id, make(id));
            return byId.get(id);
        },
        querySelector: () => null, querySelectorAll: () => [],
        createElement: (tag) => make("", tag),
        addEventListener() {}, removeEventListener() {},
        body: make("body"),
        // A focused BUTTON: the case where Space is the key that presses it.
        activeElement: make("focused_button", "button"),
        title: "",
    };
}

const win = eventTarget();
const timers = new Map();
let nextTimer = 1;
const drops = { attached: 0, detached: 0 };

const globals = {
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Uint8Array, Uint8ClampedArray, Infinity,
    URLSearchParams,
    setTimeout: (fn, ms) => { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    Blob: class Blob {},
    URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
    CustomEvent: class CustomEvent {
        constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
    },
    OpenSeadragon: {
        Rect: class Rect {
            constructor(x, y, width, height) { Object.assign(this, { x, y, width, height }); }
        },
        Point: class Point { constructor(x, y) { this.x = x; this.y = y; } },
    },
    // Core's desktop drop-zone registry (services/browsePicker.js): returns the
    // function that unregisters the zone.
    attachDropTarget: () => {
        drops.attached += 1;
        let done = false;
        return () => { if (!done) { done = true; drops.detached += 1; } };
    },
    document: workspacePage(),
};
globals.window = {
    Plexora: { registerPlugin() {} },
    crypto: { randomUUID: (() => { let n = 0; return () => (n += 1).toString(16).padEnd(32, "0"); })() },
    PlexoraStatus: { begin: () => ({ done() {}, fail() {} }), track: async (l, p) => p },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
    plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
    setTimeout: (fn, ms) => globals.setTimeout(fn, ms),
    clearTimeout: (id) => globals.clearTimeout(id),
    addEventListener: (type, fn, opts) => win.addEventListener(type, fn, opts),
    removeEventListener: (type, fn, opts) => win.removeEventListener(type, fn, opts),
    alert() {}, confirm: () => false, prompt: () => null,
    location: { href: "" },
    devicePixelRatio: 1,
    // Mounting is driven by this probe, not by the files' own registration.
    PlexoraPage: { register() {}, boot() {}, unmount() {} },
    PlexoraRouter: { go() {}, canRoute: () => false, datasource: () => "" },
};
globals.crypto = globals.window.crypto;
globals.plexoraUrl = globals.window.plexoraUrl;
globals.PlexoraPage = globals.window.PlexoraPage;
globals.PlexoraRouter = globals.window.PlexoraRouter;
globals.addEventListener = globals.window.addEventListener;
globals.removeEventListener = globals.window.removeEventListener;

const ctx = createContext(globals);
const problems = [];
const report = { problems };

const CORE_SCENE = join(REPO, "plexora/client/src/js/services/viewerScene.js");
runInContext(readFileSync(CORE_SCENE, "utf8"), ctx, { filename: CORE_SCENE });
for (const name of SCRIPTS) {
    try {
        runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
    } catch (error) {
        problems.push(`${name} failed to load: ${error.message}`);
        break;
    }
}

if (!problems.length) {
    try {
        const before = win.listeners.slice();
        runInContext(
            "globalThis.__ws = new FigureWorkspace({ figureId: 'fig_probe' });"
            + " __ws.setup();", ctx);
        const added = win.listeners.filter((l) => !before.includes(l));
        report.addedBySetup = added.map((l) => l.type);
        if (!added.length) problems.push("setup() added no window listeners at all");
        report.droppedZones = drops.attached;

        runInContext("__ws.destroy();", ctx);

        const left = win.listeners.filter((l) => added.includes(l));
        report.leftAfterDestroy = left.map((l) => l.type);
        if (left.length) {
            problems.push("destroy() left these window listeners behind: "
                          + left.map((l) => l.type).join(", "));
        }

        // Space with a button focused, after the canvas has gone.
        let prevented = false;
        const event = {
            type: "keydown", code: "Space", key: " ", target: ctx.document.activeElement,
            preventDefault() { prevented = true; }, stopPropagation() {},
            stopImmediatePropagation() {},
        };
        win.dispatch(event);
        report.spacePreventedAfterDestroy = prevented;
        if (prevented) {
            problems.push("a Space keydown on a focused button was cancelled after"
                          + " the figure canvas was destroyed");
        }

        if (drops.attached && drops.detached !== drops.attached) {
            problems.push(`destroy() left ${drops.attached - drops.detached} drop`
                          + " zone(s) registered for a page that is gone");
        }
        report.detachedZones = drops.detached;
    } catch (error) {
        problems.push(`the workspace threw: ${error && error.stack || error}`);
    }
}

console.error(JSON.stringify(report, null, 2));
process.exit(problems.length ? 1 : 0);
