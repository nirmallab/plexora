/**
 * Bookmarks (services/bookmarks.js, views/bookmarkTray.js, and Figure
 * Builder's store, plugins/figure_builder/static/figureBookmarkStore.js), in
 * node, with stand-ins for the viewer and the store's server.
 *
 * Pinned: the store registers whichever script runs first; a bookmark is
 * read from the viewer at once -- before the picture or the write is awaited
 * -- listed at once, written as a capture with `bookmarked: true` and its
 * label, then its picture; a restore puts the scene back KEPT (`persist`)
 * and rings the bookmark until the view moves on; removing one only takes
 * the flag off; renaming sends the label. The icon only opens and closes the
 * tray and is hidden without a store; K bookmarks with the tray closed, and
 * not while typing, with a modifier, held, with a dialog up, or once taken.
 *
 * Run directly:  node tests/js/bookmarks_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");
const FB = join(REPO, "plexora/plugins/figure_builder/static");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

// -- a small DOM ------------------------------------------------------------------
class Node {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.attrs = {};
        this.listeners = {};
        this.hidden = false;
        this.style = {};
        this.textContent = "";
        this.dataset = {};
        const classes = new Set();
        this.classList = {
            add: (...n) => n.forEach((c) => classes.add(c)), remove: (...n) => n.forEach((c) => classes.delete(c)),
            contains: (c) => classes.has(c),
            toggle: (c, on) => { if (on ?? !classes.has(c)) classes.add(c); else classes.delete(c); },
        };
        Object.defineProperty(this, "className", {
            get: () => [...classes].join(" "),
            set: (v) => { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
        });
    }
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === "title") this.title = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    removeAttribute(k) { delete this.attrs[k]; }
    appendChild(c) { c.remove(); c.parentElement = this; this.children.push(c); return c; }
    append(...cs) { cs.forEach((c) => this.appendChild(c)); }
    insertBefore(c, before) {
        c.remove();
        c.parentElement = this;
        const at = before ? this.children.indexOf(before) : -1;
        if (at >= 0) this.children.splice(at, 0, c); else this.children.push(c);
        return c;
    }
    get firstChild() { return this.children[0] || null; }
    get nextSibling() {
        const list = this.parentElement?.children || [];
        return list[list.indexOf(this) + 1] || null;
    }
    replaceWith(other) {
        const parent = this.parentElement;
        if (!parent) return;
        const at = parent.children.indexOf(this);
        other.remove();
        parent.children.splice(at, 1, other);
        other.parentElement = parent;
        this.parentElement = null;
    }
    remove() {
        if (!this.parentElement) return;
        const list = this.parentElement.children;
        list.splice(list.indexOf(this), 1);
        this.parentElement = null;
    }
    contains(other) { for (let n = other; n; n = n.parentElement) if (n === this) return true; return false; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] || []).filter((f) => f !== fn); }
    fire(name, extra = {}) {
        const event = { type: name, target: this, preventDefault() {}, stopPropagation() {}, ...extra };
        for (const fn of [...(this.listeners[name] || [])]) fn(event);
        return event;
    }
    focus() {}
    select() {}
    getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 60 }; }
    get offsetWidth() { return 100; }
    querySelectorAll(sel) {
        const cls = sel.replace(/^\./, "");
        const out = [];
        const walk = (n) => n.children.forEach((c) => { if (c.classList.contains(cls)) out.push(c); walk(c); });
        walk(this);
        return out;
    }
}

const wrapper = new Node("div");
const bookmarkButton = new Node("button");
bookmarkButton.hidden = true;
const documentListeners = {};
let typingNow = false;
let dialogOpen = false;
const toasts = [];
const windowListeners = {};
const box = {
    console, Math, JSON, Number, String, Array, Object, Boolean, Set, Map, Promise, Uint8Array, Date,
    setTimeout, clearTimeout,
    performance: { now: () => Date.now() },
    crypto: { getRandomValues: (a) => { for (let i = 0; i < a.length; i += 1) a[i] = (i * 37 + 11) % 256; return a; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { for (const fn of [...(windowListeners[event.type] || [])]) fn(event); return true; },
    addEventListener: (name, fn, options) => {
        const wrapped = options?.once ? (event) => { box.removeEventListener(name, wrapped); fn(event); } : fn;
        (windowListeners[name] ||= []).push(wrapped);
    },
    removeEventListener: (name, fn) => { windowListeners[name] = (windowListeners[name] || []).filter((f) => f !== fn); },
    URL: { createObjectURL: () => "blob:picture", revokeObjectURL() {} },
    sessionStorage: (() => { const m = new Map(); return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)) }; })(),
    flaskVariables: { datasource: "demo" },
    PlexoraShortcuts: { isTyping: () => typingNow },
    PlexoraToast: { show: (o) => toasts.push(o) },
    PlexoraViewerPrefs: { smoothMotion: () => false },
    document: {
        documentElement: new Node("html"),
        body: new Node("body"),
        createElement: (tag) => new Node(tag),
        getElementById: (id) => ({ openseadragon_wrapper: wrapper, viewer_bookmark_button: bookmarkButton }[id] || null),
        querySelector: (sel) => (sel === "dialog[open]" && dialogOpen ? {} : null),
        addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
        removeEventListener: () => {},
        activeElement: null,
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
const run = (path) => runInContext(readFileSync(path, "utf8"), context, { filename: path });

// -- the store registers whichever runs first -------------------------------------------
// Figure Builder's store runs first (it is in <head>), with only its own
// stand-ins: nothing of core's bookmarks yet.
const requests = [];
runInContext(`
    class FigureBuilderApi {
        async listCaptures(ds, o) { __requests.push(["list", ds, o]); return { ok: true, data: { captures: __rows } }; }
        async addCapture(entry) { __requests.push(["add", entry]); return { ok: true, data: {} }; }
        async putCapturePreview(id, blob, size) { __requests.push(["preview", id, size]); return { ok: true }; }
        async patchCapture(id, changes) {
            __requests.push(["patch", id, changes]);
            // As the bin would: the label kept, an unbookmarked row no longer listed.
            const row = __rows.find((r) => r.capture_id === id);
            if (row && changes.label !== undefined) row.label = changes.label;
            if (changes.bookmarked === false) __rows = __rows.filter((r) => r.capture_id !== id);
            return { ok: true };
        }
        async describeCaptureSource(ds) { __requests.push(["describe", ds]); return { pixel_size: null }; }
        capturePreviewUrl(id) { return "/preview/" + id; }
        capturesHref() { return "/plugins/figure_builder/captures"; }
    }
    const FigureSchema = { physicalWidthUm: () => 650, formatMicrons: (v) => v + " µm",
                           frameSize: (v) => ({ w: v.w }) };
    const FigureScene = {
        currentViewport: () => ({ x: 1, y: 2, w: 300, h: 200 }),
        capture: (ctx, id, viewport) => { __captured.push(viewport); return { viewport, channels: ["now"] }; },
        restore: async (ctx, scene, options) => { __restored.push({ scene, options }); return { missing_channels: [] }; },
    };
`, context);
box.__requests = requests;
box.__rowsInit = [{ capture_id: "cap_old", label: "Old one", created_at: "2026-10-01", has_preview: true,
                scene: { viewport: { x: 0 } } }];
runInContext("var __rows = __rowsInit;", context);
box.__captured = [];
box.__restored = [];
run(join(FB, "figureBookmarkStore.js"));
check("the store, run before core, waits for it", !box.PlexoraBookmarks
      && (windowListeners["plexora:bookmarks-ready"] || []).length === 1);
run(join(JS, "services/bookmarks.js"));
const B = box.PlexoraBookmarks;
check("core announces itself, and the store that waited registers", B.available()
      && (windowListeners["plexora:bookmarks-ready"] || []).length === 0);
const reasons = [];
const offReasons = B.onChange((detail) => reasons.push(detail.reason));
run(join(FB, "figureBookmarkStore.js"));
offReasons();
check("a store that runs after core registers at once", reasons.includes("store") && B.available());
check("and something short of a store is refused", B.registerStore({ list() {} }) === false && B.available());

// -- the tray and its icon ----------------------------------------------------------------
const viewportFns = [];
const imageViewer = { viewer: { canvas: new Node("canvas") }, config: {},
                      onViewportChange: (fn) => { viewportFns.push(fn); return () => {}; } };
box.__plexora = { seaDragonViewer: imageViewer, dataLayer: {} };
let pictureResolve;
box.PlexoraViewRaster = {
    fullViewRect: () => ({ x: 0, y: 0, width: 800, height: 600 }),
    blob: () => new Promise((resolve) => { pictureResolve = resolve; }),
};
run(join(JS, "views/bookmarkTray.js"));
const tray = box.PlexoraBookmarkTray.attach({ imageViewer });
check("the tray mounts over the canvas, closed; the icon shows where bookmarks can be kept",
      tray && wrapper.contains(tray.root) && tray.root.hidden === true && bookmarkButton.hidden === false);
bookmarkButton.fire("click");
await settle();
check("the icon opens the tray, and only that", tray.root.hidden === false
      && bookmarkButton.getAttribute("aria-pressed") === "true" && requests.filter((r) => r[0] === "add").length === 0);
check("the tray lists this image's bookmarks, asked for as bookmarks",
      requests.some((r) => r[0] === "list" && r[1] === "demo" && r[2]?.bookmarked === true)
      && tray.items.has("cap_old") && tray.items.get("cap_old").img.getAttribute("src") === "/preview/cap_old");
bookmarkButton.fire("click");
check("again, it closes", tray.root.hidden === true && bookmarkButton.getAttribute("aria-pressed") === "false");

// -- K --------------------------------------------------------------------------------------
const keydown = documentListeners.keydown.at(-1);
const press = (extra = {}) => {
    const event = { key: "k", repeat: false, defaultPrevented: false, prevented: false,
                    preventDefault() { this.prevented = true; }, ...extra };
    keydown(event);
    return event;
};
for (const [label, extra] of [["Ctrl+K", { ctrlKey: true }], ["Cmd+K", { metaKey: true }],
                              ["Shift+K", { shiftKey: true }], ["Alt+K", { altKey: true }],
                              ["a held K", { repeat: true }], ["a K already taken", { defaultPrevented: true }]]) {
    check(`${label} bookmarks nothing`, !press(extra).prevented && box.__captured.length === 0);
}
typingNow = true;
check("nor K while typing", !press().prevented && box.__captured.length === 0);
typingNow = false;
dialogOpen = true;
check("nor with a dialog up", !press().prevented && box.__captured.length === 0);
dialogOpen = false;

const before = requests.length;
const taken = press();
check("K with the tray closed bookmarks the view: read at once, before anything is awaited",
      taken.prevented && box.__captured.length === 1 && requests.length === before);
const fresh = B.cached()[0];
check("listed at once, under the next free name, waiting to be written",
      fresh.label === "View 2" && fresh.pending === true && fresh.scene.channels[0] === "now");
check("the tray closed: a short notice says where it went", toasts.some((t) => /View 2 bookmarked/.test(t.title)));
pictureResolve({ blob: { type: "image/webp" }, width: 800, height: 600 });
await settle(); await settle(); await settle();
const add = requests.find((r) => r[0] === "add");
check("written as a capture of this image, bookmarked, with its label and a caption",
      add && add[1].capture_id === fresh.id && add[1].datasource === "demo" && add[1].bookmarked === true
      && add[1].label === "View 2" && add[1].caption === "650 µm wide", JSON.stringify(add));
check("then its picture", requests.some((r) => r[0] === "preview" && r[1] === fresh.id && r[2].width === 800)
      && fresh.pending === false && fresh.preview_url === "blob:picture");
check("the icon, not the tray, is marked where it went", bookmarkButton.classList.contains("is-arrived"));

// -- going back to one ---------------------------------------------------------------------
bookmarkButton.fire("click");
await settle();
tray.items.get("cap_old").open.fire("click");
await settle();
check("a click restores it, KEPT: the scene put back with persist",
      box.__restored.length === 1 && box.__restored[0].options.persist === true
      && box.__restored[0].scene.viewport.x === 0);
check("and it is ringed as the view on screen", B.active() === "cap_old"
      && tray.items.get("cap_old").node.classList.contains("is-active"));
B.viewMoved();
check("its own motion does not take the ring off", B.active() === "cap_old");

// -- renaming, removing, locating --------------------------------------------------------------
tray.rename("cap_old");
const input = tray.items.get("cap_old").node.querySelectorAll("plx-bookmark-rename")[0];
input.value = "Germinal centre";
input.fire("keydown", { key: "Enter" });
await settle();
check("a rename sends the label", requests.some((r) => r[0] === "patch" && r[1] === "cap_old"
      && r[2].label === "Germinal centre") && B.find("cap_old").label === "Germinal centre");
check("Show in Figure Builder is the capture on the captures page, bookmarks shown",
      B.locateUrl("cap_old") === "/plugins/figure_builder/captures?bookmarked=1#cap_old");
tray.items.get("cap_old").remove.fire("click");
await settle();
check("× takes the flag off, nothing else, and it leaves the row",
      requests.some((r) => r[0] === "patch" && r[1] === "cap_old" && r[2].bookmarked === false
                    && Object.keys(r[2]).length === 1)
      && !B.find("cap_old") && !tray.items.has("cap_old"));

// -- a routed page over the viewer ---------------------------------------------------------------
box.dispatchEvent(new box.CustomEvent("plexora:viewer-hidden"));
const hiddenCount = box.__captured.length;
check("with Settings over the viewer, K is not the viewer's", !press().prevented && box.__captured.length === hiddenCount);
box.dispatchEvent(new box.CustomEvent("plexora:viewer-shown"));

// -- + Save View, the tray open -------------------------------------------------------------------
tray.saveButton.fire("click");
check("+ Save View bookmarks too, into the open tray", box.__captured.length === 2
      && tray.items.has(B.cached()[0].id));

// -- the row: its count, the wheel, a drag, the chevrons ---------------------------------------
const list = tray.list;
check("the head counts the saved views", tray.count.textContent === String(B.cached().length),
      tray.count.textContent);
// Two more thumbnails' worth of row, to scroll along and snap between.
list.append(new Node("li"), new Node("li"));
list.children.forEach((node, i) => { node.offsetLeft = i * 148; });
Object.assign(list, { scrollLeft: 0, scrollWidth: 1000, clientWidth: 400 });
list.scrollBy = ({ left }) => { list.scrollLeft += left; };
list.scrollTo = ({ left }) => { list.scrollLeft = left; };
tray.edges();
check("more to the right: a chevron and a fade there, none on the left", !tray.next.hidden && tray.prev.hidden
      && tray.body.classList.contains("has-after") && !tray.body.classList.contains("has-before"));
const wheel = list.fire("wheel", { deltaY: 120, deltaX: 0, deltaMode: 0, ctrlKey: false,
                                   preventDefault() { this.prevented = true; } });
check("a mouse wheel moves the row sideways", list.scrollLeft === 120 && wheel.prevented === true);
const swipe = list.fire("wheel", { deltaY: 4, deltaX: 60, deltaMode: 0, ctrlKey: false,
                                   preventDefault() { this.prevented = true; } });
check("a sideways swipe is the browser's own", list.scrollLeft === 120 && !swipe.prevented);
tray.edges();
check("moved along: a chevron on each side", !tray.next.hidden && !tray.prev.hidden);
list.fire("pointerdown", { button: 0, pointerType: "mouse", pointerId: 7, clientX: 300 });
list.fire("pointermove", { pointerId: 7, clientX: 298 });
check("a press that barely moves is still a click", list.scrollLeft === 120);
list.fire("pointermove", { pointerId: 7, clientX: 250 });
check("past a few pixels it drags the row", list.scrollLeft === 170);
list.fire("pointerup", { pointerId: 7, clientX: 250 });
check("let go, it snaps to the nearest thumbnail", list.scrollLeft === 148, String(list.scrollLeft));
const ending = list.fire("click", { stopPropagation() { this.stopped = true; } });
check("and the click that ends a drag opens nothing", ending.stopped === true);
tray.next.fire("click");
check("the chevron pages along", list.scrollLeft > 148, String(list.scrollLeft));
tray.saveButton.fire("click");
check("saving goes back to the start, where the new one is", list.scrollLeft === 0);

// -- opened at one from another page ("Go to view") ------------------------------------------------
const stored = new Map();
box.sessionStorage = { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, String(v)),
                       removeItem: (k) => stored.delete(k) };
imageViewer.viewer.world = { getItemCount: () => 1 };
runInContext(`__rows.push({ capture_id: "cap_far", label: "Far", created_at: "2026-10-02", has_preview: false,
                           scene: { viewport: { x: 9 } } });`, context);
stored.set(B.OPEN_KEY, JSON.stringify({ datasource: "elsewhere", id: "cap_far" }));
check("a note for another image is not this one's", await B.openNoted() === null && stored.has(B.OPEN_KEY));
stored.set(B.OPEN_KEY, JSON.stringify({ datasource: "demo", id: "cap_far" }));
box.__restored.length = 0;
box.dispatchEvent(new box.CustomEvent("plexora:viewer-shown"));
for (let i = 0; i < 6; i += 1) await settle();
check("back in the viewer with a note for it: that bookmark is put back, kept, and the note taken",
      box.__restored.length === 1 && box.__restored[0].scene.viewport.x === 9
      && box.__restored[0].options.persist === true && !stored.has(B.OPEN_KEY) && B.active() === "cap_far",
      JSON.stringify(box.__restored));

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
