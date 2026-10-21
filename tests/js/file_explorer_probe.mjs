/**
 * The file explorer, run against a DOM small enough to read.
 *
 * The explorer is the only way to find a file on a compute node -- the native
 * dialog needs a screen and there is not one -- and since it became every
 * Browse's first answer, the way to find one anywhere. The things that break
 * here break quietly:
 *
 *   1. **Every path it navigates to came from the server.** Rows carry
 *      `entry.path`, breadcrumbs carry theirs, Up uses `payload.parent`. The
 *      version that joined names with "/" was correct until the node on the
 *      other end was a Windows box, and then Up from `C:\data` went to "".
 *   2. **A listing that fails changes nothing.** `state.here` is assigned in
 *      exactly one place, from a server answer.
 *   3. **Remembering places may not block browsing.** /picker_prefs and
 *      /fs_places are conveniences.
 *   4. **Esc inside a text box does not close the modal.**
 *   5. **What is chosen is what the caller asked for**: one path, or an array
 *      when `multiple` is set, in the order the rows are drawn.
 *   6. **Mode "any" takes either kind**, and a dataset row selects in one
 *      click while "›" still goes inside it.
 *   7. **Fifty thousand entries are a scrollbar, not fifty thousand rows.**
 *
 * The six explorer scripts are loaded in base.html's order.
 *
 * Run directly:  node tests/js/file_explorer_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCES = [
    "explorerTypes.js",
    "floatingWindow.js",
    "explorerListing.js",
    "explorerCrumbs.js",
    "explorerSidebar.js",
    "fileExplorer.js",
].map((name) => join(REPO, "plexora/client/src/js/services/explorer", name));

// -- a DOM small enough to read ---------------------------------------------

//: Events that stay on their target, as they do in a browser.
const NON_BUBBLING = new Set(["close", "cancel", "focus", "blur", "scroll",
                              "pointerenter", "pointerleave"]);

let activeElement = null;

function parseSelector(text) {
    return text.split(",").map((part) => part.trim()).filter(Boolean).map((part) => {
        const tests = [];
        const re = /([a-zA-Z][\w-]*)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]|:not\(\[([\w-]+)\]\)/g;
        let match;
        while ((match = re.exec(part))) {
            if (match[1]) { const tag = match[1].toUpperCase(); tests.push((n) => n.tagName === tag); }
            else if (match[2]) { const name = match[2]; tests.push((n) => n.classList.contains(name)); }
            else if (match[3]) {
                const name = match[3];
                const value = match[4];
                tests.push((n) => {
                    if (name === "open" && n.tagName === "DIALOG") return n.open === true;
                    if (name === "disabled") return n.disabled === true;
                    const has = n.getAttribute(name);
                    return value === undefined ? has !== null : has === value;
                });
            } else if (match[5]) {
                const name = match[5];
                tests.push((n) => (name === "disabled" ? n.disabled !== true : n.getAttribute(name) === null));
            }
        }
        return (node) => tests.every((test) => test(node));
    });
}

function matchesSelector(node, selector) {
    return parseSelector(selector).some((test) => test(node));
}

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const styleValues = new Map();
    const style = {
        setProperty(name, value) { styleValues.set(name, String(value)); },
        removeProperty(name) { styleValues.delete(name); },
        getPropertyValue(name) { return styleValues.get(name) || ""; },
    };
    const element = {
        tagName: String(tag).toUpperCase(),
        nodeType: 1,
        type: "",
        value: "",
        textContent: "",
        hidden: false,
        disabled: false,
        checked: false,
        open: false,
        children: [],
        parentNode: null,
        dataset: {},
        style,
        scrollTop: 0,
        scrollLeft: 0,
        clientWidth: 0,
        clientHeight: 0,
        scrollWidth: 0,
        scrollHeight: 0,
        rect: null,
        captured: new Set(),
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
            toggle: (name, on) => {
                const want = on === undefined ? !classes.has(name) : Boolean(on);
                if (want) classes.add(name); else classes.delete(name);
                return want;
            },
            contains: (name) => classes.has(name),
        },
        get isConnected() {
            let node = element;
            while (node.parentNode) node = node.parentNode;
            return node === body || node === documentElement;
        },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) {
            return attributes.has(name) ? attributes.get(name) : null;
        },
        removeAttribute(name) { attributes.delete(name); },
        hasAttribute(name) { return attributes.has(name); },
        appendChild(child) {
            if (child.parentNode) child.remove();
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        append(...nodes) { nodes.forEach((n) => element.appendChild(n)); },
        replaceChildren(...nodes) {
            element.children.forEach((child) => { child.parentNode = null; });
            element.children = [];
            nodes.forEach((n) => element.appendChild(n));
        },
        remove() {
            const parent = element.parentNode;
            if (!parent) return;
            parent.children = parent.children.filter((c) => c !== element);
            element.parentNode = null;
        },
        contains(other) {
            let node = other;
            while (node) {
                if (node === element) return true;
                node = node.parentNode;
            }
            return false;
        },
        matches(selector) { return matchesSelector(element, selector); },
        closest(selector) {
            let node = element;
            while (node && node.nodeType === 1) {
                if (matchesSelector(node, selector)) return node;
                node = node.parentNode;
            }
            return null;
        },
        querySelectorAll(selector) {
            return walk(element, []).slice(1).filter((n) => matchesSelector(n, selector));
        },
        querySelector(selector) { return element.querySelectorAll(selector)[0] || null; },
        getBoundingClientRect() {
            return element.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
        },
        addEventListener(type, handler, options) {
            const capture = options === true || Boolean(options && options.capture);
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push({ handler, capture });
        },
        removeEventListener(type, handler, options) {
            const capture = options === true || Boolean(options && options.capture);
            if (!listeners.has(type)) return;
            listeners.set(type, listeners.get(type)
                .filter((l) => !(l.handler === handler && l.capture === capture)));
        },
        _fire(event, phase) {
            (listeners.get(event.type) || []).slice().forEach((l) => {
                if (event._stopped) return;
                if (phase === "capture" && !l.capture) return;
                if (phase === "bubble" && l.capture) return;
                l.handler.call(element, event);
            });
        },
        dispatchEvent(event) { return dispatch(element, event); },
        setPointerCapture(id) { element.captured.add(id); },
        releasePointerCapture(id) { element.captured.delete(id); },
        hasPointerCapture(id) { return element.captured.has(id); },
        focus() { activeElement = element; element.focused = true; },
        blur() { if (activeElement === element) activeElement = null; element.focused = false; },
        scrollIntoView() {},
        focused: false,
        select() { element.selected = true; },
        selected: false,
        showModal() { element.open = true; },
        close() {
            if (!element.open) return;
            element.open = false;
            element.dispatchEvent({ type: "close" });
        },
    };
    return element;
}

/** Capture down from the document, then bubble back up, as a browser does. */
function dispatch(origin, event) {
    if (!event.target) event.target = origin;
    let prevented = false;
    event._stopped = false;
    const ownPrevent = event.preventDefault;
    const ownStop = event.stopPropagation;
    event.preventDefault = function () { prevented = true; if (ownPrevent) ownPrevent.call(event); };
    event.stopPropagation = function () { event._stopped = true; if (ownStop) ownStop.call(event); };
    Object.defineProperty(event, "defaultPrevented", { get: () => prevented, configurable: true });
    const path = [];
    let node = origin;
    while (node) { path.push(node); node = node.parentNode; }
    const chain = [documentStub, ...path.slice().reverse()];
    for (const each of chain) {
        if (event._stopped) break;
        each._fire(event, "capture");
    }
    if (!event._stopped) origin._fire(event, "bubble");
    if (!NON_BUBBLING.has(event.type) && event.bubbles !== false) {
        for (const each of path.slice(1)) {
            if (event._stopped) break;
            each._fire(event, "bubble");
        }
        if (!event._stopped) documentStub._fire(event, "bubble");
    }
    return !prevented;
}

const body = makeElement("body");
const documentElement = makeElement("html");
const documentListeners = new Map();
const documentStub = {
    createElement: makeElement,
    body,
    documentElement,
    get activeElement() { return activeElement; },
    addEventListener(type, handler, options) {
        const capture = options === true || Boolean(options && options.capture);
        if (!documentListeners.has(type)) documentListeners.set(type, []);
        documentListeners.get(type).push({ handler, capture });
    },
    removeEventListener(type, handler, options) {
        const capture = options === true || Boolean(options && options.capture);
        if (!documentListeners.has(type)) return;
        documentListeners.set(type, documentListeners.get(type)
            .filter((l) => !(l.handler === handler && l.capture === capture)));
    },
    _fire(event, phase) {
        (documentListeners.get(event.type) || []).slice().forEach((l) => {
            if (event._stopped) return;
            if (phase === "capture" && !l.capture) return;
            if (phase === "bubble" && l.capture) return;
            l.handler(event);
        });
    },
    querySelectorAll(selector) {
        return walk(body, []).filter((n) => matchesSelector(n, selector));
    },
};

// -- finding things -----------------------------------------------------------

function walk(node, out) {
    out.push(node);
    (node.children || []).forEach((child) => walk(child, out));
    return out;
}

function allByClass(root, name) {
    return walk(root, []).filter((n) => n.classList.contains(name));
}

function byClass(root, name) {
    return allByClass(root, name)[0] || null;
}

function byText(root, text) {
    return walk(root, []).find((n) => n.textContent === text) || null;
}

function byAttr(root, name, value) {
    return walk(root, []).find((n) => n.getAttribute && n.getAttribute(name) === value) || null;
}

function action(dialog, name) {
    return byAttr(dialog, "data-action", name);
}

function rows(dialog) {
    return allByClass(dialog, "plx-explorer-row");
}

function rowNames(dialog) {
    return rows(dialog).map((row) => byClass(row, "plx-explorer-name").textContent);
}

function rowCells(dialog, cell) {
    return rows(dialog).map((row) => (byClass(row, cell) || { textContent: "?" }).textContent);
}

function press(target, key, extra = {}) {
    const event = { type: "keydown", key, ...extra };
    const on = extra.dispatchOn || target;
    if (!extra.target) event.target = target;
    delete event.dispatchOn;
    on.dispatchEvent(event);
    return { defaultPrevented: event.defaultPrevented, propagationStopped: event._stopped };
}

function click(node, extra = {}) {
    node.dispatchEvent({ type: "click", button: 0, ...extra });
}

function clickRow(dialog, index, modifiers = {}) {
    const row = rows(dialog)[index];
    row.dispatchEvent({ type: "click", ...modifiers });
    return row;
}

function pointer(node, type, x, y, extra = {}) {
    node.dispatchEvent({ type, button: 0, pointerId: 1, clientX: x, clientY: y, ...extra });
}

// -- a server that answers whatever this test says --------------------------

const calls = [];
let listings = {};
let prefs = null;
let places = null;
let statTable = {};
let searchAnswer = null;
let listDelay = {};
//: When set, /stat_entries answers wait for it: the chunks are otherwise all
//: back before a check can look at the listing between them.
let statGate = null;

function body_(options) {
    return options && options.body ? JSON.parse(options.body) : null;
}

function respond(status, payload) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(payload),
    };
}

function fetchStub(url, options = {}) {
    const method = options.method || "GET";
    const sent = body_(options);
    calls.push({ url, method, body: sent });
    if (url.indexOf("picker_prefs") >= 0) {
        if (prefs === null) return Promise.resolve(respond(500, { error: "no settings file" }));
        return Promise.resolve(respond(200, prefs));
    }
    if (url.indexOf("fs_places") >= 0) {
        if (places === null) return Promise.resolve(respond(500, { error: "no places" }));
        return Promise.resolve(respond(200, places));
    }
    if (url.indexOf("stat_entries") >= 0) {
        const entries = {};
        (sent.names || []).forEach((name) => { if (statTable[name]) entries[name] = statTable[name]; });
        const reply = respond(200, { path: sent.path, entries });
        return statGate ? statGate.then(() => reply) : Promise.resolve(reply);
    }
    if (url.indexOf("search_dir") >= 0) {
        const answer = searchAnswer ? searchAnswer(sent) : { status: 200, payload: { matches: [] } };
        return Promise.resolve(respond(answer.status || 200, answer.payload));
    }
    const asked = sent.path;
    const found = listings[asked];
    const reply = found
        ? respond(200, typeof found === "function" ? found(sent) : found)
        : respond(400, { error: `Not a folder: ${asked}` });
    const delay = listDelay[asked];
    return delay ? new Promise((resolve) => setTimeout(() => resolve(reply), delay))
        : Promise.resolve(reply);
}

// -- one small filesystem, drawn the way the server draws it ----------------

function crumbsFor(path) {
    if (path.indexOf("\\") >= 0) {
        const parts = path.split("\\").filter(Boolean);
        const trail = [{ label: `${parts[0]}\\`, path: `${parts[0]}\\` }];
        parts.slice(1).forEach((part, index) => {
            trail.push({
                label: part,
                path: `${parts[0]}\\${parts.slice(1, index + 2).join("\\")}`,
            });
        });
        return trail;
    }
    const parts = path.split("/").filter(Boolean);
    const trail = [{ label: "/", path: "/" }];
    parts.forEach((part, index) => {
        trail.push({ label: part, path: `/${parts.slice(0, index + 1).join("/")}` });
    });
    return trail;
}

function folder(path, parent, entries, truncated = false, extra = {}) {
    return { path, parent, crumbs: crumbsFor(path), entries, truncated, ...extra };
}

function dir(path, extra = {}) {
    return { name: path.split(/[\\/]/).pop(), is_dir: true, size: null, path, ...extra };
}

function file(path, size, extra = {}) {
    return { name: path.split(/[\\/]/).pop(), is_dir: false, size, path, ...extra };
}

const HOME = "/home/aj";
const STUDY = "/home/aj/study";

function posixFilesystem() {
    return {
        "": folder(HOME, "/home", [
            dir(`${HOME}/store.zarr`), dir(STUDY),
            file(`${HOME}/image.ome.tif`, 2048), file(`${HOME}/notes.txt`, 12),
        ]),
        [HOME]: folder(HOME, "/home", [
            dir(`${HOME}/store.zarr`), dir(STUDY),
            file(`${HOME}/image.ome.tif`, 2048), file(`${HOME}/notes.txt`, 12),
        ]),
        "/home": folder("/home", "/", [dir(HOME)]),
        "/": folder("/", null, [dir("/home")]),
        [STUDY]: folder(STUDY, HOME, [
            dir(`${STUDY}/runs`),
            file(`${STUDY}/cells.csv`, 812, { mtime: 1700000000 }),
            file(`${STUDY}/cells2.csv`, 900, { mtime: 1710000000 }),
            file(`${STUDY}/scan.svs`, 4096, { mtime: 1690000000 }),
        ]),
        [`${STUDY}/runs`]: folder(`${STUDY}/runs`, STUDY, []),
        [`${HOME}/store.zarr`]: folder(`${HOME}/store.zarr`, HOME, [dir(`${HOME}/store.zarr/0`)]),
        [`${STUDY}/cells.csv`]: folder(STUDY, HOME, [
            dir(`${STUDY}/runs`), file(`${STUDY}/cells.csv`, 812),
        ]),
    };
}

/** A folder of `n` files, optionally sent without sizes or dates. */
function bigFolder(n, partial) {
    const big = "/scratch/big";
    const entries = [];
    for (let i = 0; i < n; i += 1) {
        const name = `f${i}.csv`;
        entries.push(partial
            ? { name, is_dir: false, size: null, mtime: null, kind: "file", path: `${big}/${name}` }
            : { name, is_dir: false, size: i, mtime: 1700000000 + i, kind: "file", path: `${big}/${name}` });
        statTable[name] = { size: n - i, mtime: 1700000000 + i, kind: "file", is_dir: false };
    }
    return folder(big, "/scratch", entries, false, { partial, total: n });
}

function defaultPlaces() {
    return { platform: "linux", hostname: "login01", home: HOME, known: [], volumes: [],
             project: [], last_dir: "", recent: [], pinned: [] };
}

// -- load the shipped files -------------------------------------------------

const storageValues = new Map();
const sessionStorage = {
    getItem: (key) => (storageValues.has(key) ? storageValues.get(key) : null),
    setItem: (key, value) => storageValues.set(key, String(value)),
    removeItem: (key) => storageValues.delete(key),
};
const menus = [];
let menuOpen = false;
const PlexoraMenu = {
    open(anchor, items, options) {
        menus.push({ anchor, items, options });
        menuOpen = true;
        return () => { menuOpen = false; };
    },
    close() { menuOpen = false; },
    isOpen: () => menuOpen,
};
let clipboard = "";
const windowListeners = new Map();

const context = {
    console,
    setTimeout,
    clearTimeout,
    fetch: fetchStub,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    document: documentStub,
    encodeURIComponent,
    innerWidth: 1440,
    innerHeight: 900,
    sessionStorage,
    matchMedia: () => ({ matches: false }),
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    AbortController,
    Intl,
    Date,
    Math,
    JSON,
    Number,
    navigator: { clipboard: { writeText: async (text) => { clipboard = text; } } },
    PlexoraMenu,
    addEventListener(type, handler) {
        if (!windowListeners.has(type)) windowListeners.set(type, []);
        windowListeners.get(type).push(handler);
    },
    removeEventListener(type, handler) {
        if (!windowListeners.has(type)) return;
        windowListeners.set(type, windowListeners.get(type).filter((h) => h !== handler));
    },
};
context.window = context;
createContext(context);
SOURCES.forEach((source) => runInContext(readFileSync(source, "utf-8"), context, { filename: source }));

const Explorer = context.window.PlexoraFileExplorer;
const Picker = context.window.PlexoraPathPicker;
const Types = context.window.PlexoraExplorerTypes;

// -- checks -----------------------------------------------------------------

const failures = [];

function check(label, condition) {
    if (condition) {
        console.log(`ok  ${label}`);
    } else {
        failures.push(label);
        console.log(`FAIL ${label}`);
    }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function settled(times = 4) {
    for (let i = 0; i < times; i += 1) await settle();
}

/** Open an explorer and wait for its first listing to land. */
async function open(options = {}, {
    filesystem = posixFilesystem(),
    saved = { last_dir: "", recent: [], pinned: [] },
    fsPlaces = defaultPlaces(),
} = {}) {
    listings = filesystem;
    prefs = saved;
    places = fsPlaces;
    calls.length = 0;
    menus.length = 0;
    menuOpen = false;
    const promise = Picker.pick(options);
    await settled(5);
    const dialog = body.children[body.children.length - 1];
    return { dialog, promise };
}

async function cancel(dialog) {
    click(byText(dialog, "Cancel"));
    await settled(2);
}

function listCalls() {
    return calls.filter((c) => c.url.indexOf("list_dir") >= 0);
}

function prefCalls(method) {
    return calls.filter((c) => c.url.indexOf("picker_prefs") >= 0 && c.method === method);
}

function crumbLabels(dialog) {
    return allByClass(dialog, "plx-explorer-crumb").map((c) => c.textContent);
}

function placeLabels(dialog) {
    return allByClass(dialog, "plx-explorer-place")
        .map((p) => byClass(p, "plx-explorer-place-label").textContent);
}

function isOpen(dialog) {
    return body.children.indexOf(dialog) >= 0;
}

function primaryOf(dialog) {
    return byClass(dialog, "plx-explorer-primary");
}

function summaryOf(dialog) {
    return byClass(dialog, "plx-explorer-summary").textContent;
}

function rect(dialog) {
    const px = (value) => Number(String(value || "").replace("px", ""));
    return { left: px(dialog.style.left), top: px(dialog.style.top),
             width: px(dialog.style.width), height: px(dialog.style.height) };
}

async function main() {
    // -- 0. what a name is, before any server is asked ------------------------

    {
        const file_ = Types.normalizeAccept({ mode: "file", filter: "csv" });
        const dir_ = Types.normalizeAccept({ mode: "directory" });
        const any = Types.normalizeAccept({ mode: "any", filter: "image" });
        const many = Types.normalizeAccept({ mode: "file", multiple: true });
        const custom = Types.normalizeAccept({ accept: { files: [".CSV"], datasets: ["xenium"] },
                                               select: "many" });
        check("legacy file mode takes the filter's files and no folders",
              file_.kind === "file" && file_.files.join() === ".csv" && file_.directories === false);
        check("legacy directory mode takes the open folder and no files",
              dir_.kind === "directory" && dir_.files === false);
        check("legacy any mode takes files, folders and the filter's datasets",
              any.kind === "any" && any.directories === true
              && any.datasets.indexOf("zarr") >= 0 && any.datasets.indexOf("xenium") < 0);
        check("`multiple` in file mode is many; elsewhere it is one",
              many.many === true
              && Types.normalizeAccept({ mode: "any", multiple: true }).many === false);
        check("a new caller's accept is read as given, suffixes lower-cased",
              custom.files.join() === ".csv" && custom.datasets.join() === "xenium"
              && custom.many === true && custom.kind === "any");

        const csvOnly = Types.normalizeAccept({ mode: "file", filter: "csv" });
        const greyed = Types.usable({ name: "scan.svs", is_dir: false }, csvOnly);
        check("a file the field cannot take says why, in words",
              greyed.ok === false && greyed.why === "This field takes a .csv table.");
        check("a broken link and a locked folder are never usable",
              Types.usable({ name: "x", is_dir: false, kind: "broken" }, csvOnly).ok === false
              && Types.usable({ name: "x", is_dir: true, kind: "denied" }, csvOnly).ok === false);
        check("a dataset the server marked selects in one click where datasets are wanted",
              Types.usable({ name: "run1", is_dir: true, store: { type: "xenium", label: "Xenium run" } },
                           Types.normalizeAccept({ mode: "any", filter: "any" })).role === "dataset"
              && Types.usable({ name: "run1", is_dir: true, store: { type: "xenium" } },
                              Types.normalizeAccept({ mode: "any", filter: "image" })).role === "folder");
        check("the Type column names a dataset by what it is",
              Types.typeLabel({ name: "run1", is_dir: true, store: { type: "visium_hd" } })
                  === "Visium HD run");
        check("a row's glyph says what it is",
              Types.iconFor({ name: "a.csv", is_dir: false }) === "fas fa-file-csv"
              && Types.iconFor({ name: "x", is_dir: true, kind: "denied" }) === "fas fa-lock"
              && Types.iconFor({ name: "s.zarr", is_dir: true }) === "fas fa-cubes");
        const now = new Date(2026, 2, 12, 15, 0).getTime();
        check("a date today says Today",
              Types.readableDate(new Date(2026, 2, 12, 9, 5).getTime() / 1000, now).indexOf("Today") === 0
              && Types.readableDate(null) === "");
        check("`accepts` is still there for fileLocation.filterFor",
              Picker.accepts("a.CSV", "csv") === true && Picker.accepts("a.svs", "csv") === false
              && Picker === Explorer);
    }

    // -- 1. the client does no path arithmetic ------------------------------

    {
        const { dialog } = await open();
        const before = listCalls().length;
        clickRow(dialog, 1);              // "study"
        await settled(2);
        const asked = listCalls()[before].body.path;
        check("opening a folder asks for the path the SERVER gave that row",
              asked === STUDY);
        check("...and the listing that comes back is what is drawn",
              rowNames(dialog).join(",") === "runs,cells.csv,cells2.csv,scan.svs");
        await cancel(dialog);
    }

    {
        const windows = {
            "": folder("C:\\Users\\aj", "C:\\Users", []),
            "C:\\data": folder("C:\\data", "C:\\", [dir("C:\\data\\runs")]),
            "C:\\": folder("C:\\", null, [dir("C:\\data")]),
        };
        const { dialog } = await open({ start: "C:\\data" }, { filesystem: windows });
        check("a Windows node opens where it was asked to",
              crumbLabels(dialog).join("|") === "C:\\|data");
        const before = listCalls().length;
        click(action(dialog, "up"));
        await settled(2);
        check("Up follows the server's own parent, separators and all",
              listCalls()[before].body.path === "C:\\");
        check("...and at the top of the tree Up is not offered",
              action(dialog, "up").disabled === true);
        await cancel(dialog);
    }

    // -- 2. history, refresh, and staying put on failure --------------------

    {
        const { dialog } = await open();
        check("Back is dead until there is somewhere to go back to",
              action(dialog, "back").disabled === true);
        clickRow(dialog, 1);
        await settled(2);
        check("...and live once a folder has been opened",
              action(dialog, "back").disabled === false);
        const before = listCalls().length;
        click(action(dialog, "back"));
        await settled(2);
        check("Back returns to the previous directory",
              listCalls()[before].body.path === HOME
              && crumbLabels(dialog).join("|") === "/|home|aj");
        check("...and having gone back, Back is dead again",
              action(dialog, "back").disabled === true);
        check("Forward is live after Back",
              action(dialog, "forward").disabled === false);
        click(action(dialog, "forward"));
        await settled(2);
        check("...and goes forward to where Back came from",
              crumbLabels(dialog).join("|") === "/|home|aj|study"
              && action(dialog, "forward").disabled === true);
        click(action(dialog, "back"));
        await settled(2);
        clickRow(dialog, 0);   // store.zarr: a folder in file mode
        await settled(2);
        check("going somewhere new forgets the way forward",
              action(dialog, "forward").disabled === true);
        click(action(dialog, "back"));
        await settled(2);

        const beforeRefresh = listCalls().length;
        click(action(dialog, "refresh"));
        await settled(2);
        check("Refresh re-reads where you are without adding a step back",
              listCalls()[beforeRefresh].body.path === HOME
              && action(dialog, "back").disabled === true);
        await cancel(dialog);
    }

    {
        const { dialog } = await open();
        const edit = byClass(dialog, "plx-explorer-crumb-edit");
        click(byClass(dialog, "plx-explorer-crumb-open"));
        edit.value = "/nowhere";
        press(edit, "Enter");
        await settled(2);
        check("a folder that cannot be read leaves you where you were",
              crumbLabels(dialog).join("|") === "/|home|aj"
              && rowNames(dialog).length === 4);
        const error = byClass(dialog, "plx-explorer-address-error");
        check("...and says why, in the picker rather than the console",
              error.hidden === false && error.textContent.indexOf("/nowhere") >= 0);
        check("...with the box still holding what was typed, to fix",
              edit.hidden === false && edit.value === "/nowhere");
        await cancel(dialog);
    }

    // -- 3. the address bar --------------------------------------------------

    {
        const { dialog } = await open({ start: STUDY });
        check("the crumb trail is the server's, drawn as buttons",
              crumbLabels(dialog).join("|") === "/|home|aj|study");
        check("...with the folder you are in marked as such",
              allByClass(dialog, "plx-explorer-crumb")[3]
                  .getAttribute("aria-current") === "location");
        const before = listCalls().length;
        click(allByClass(dialog, "plx-explorer-crumb")[1]);
        await settled(2);
        check("clicking a crumb goes to the path that crumb carries",
              listCalls()[before].body.path === "/home");

        const edit = byClass(dialog, "plx-explorer-crumb-edit");
        const strip = byClass(dialog, "plx-explorer-address");
        check("the box is hidden while the crumbs are showing",
              edit.hidden === true);
        click(strip);
        check("clicking the address bar turns the trail into a box, pre-filled",
              edit.hidden === false && edit.value === "/home");
        check("...with the path selected, so a pasted one replaces it",
              edit.selected === true);

        edit.selected = false;
        click(strip);
        check("clicking inside the box being edited leaves the text alone",
              edit.selected === false && edit.hidden === false);

        const escape = press(edit, "Escape");
        check("Esc in the path box restores the crumbs",
              edit.hidden === true && isOpen(dialog));
        check("...and is stopped, or the browser closes the whole dialog",
              escape.defaultPrevented && escape.propagationStopped);

        const beforeCrumb = listCalls().length;
        click(allByClass(dialog, "plx-explorer-crumb")[0]);
        await settled(2);
        check("a crumb inside the bar navigates rather than opening the box",
              listCalls()[beforeCrumb].body.path === "/"
              && byClass(dialog, "plx-explorer-crumb-edit").hidden === true);

        click(byClass(dialog, "plx-explorer-crumb-open"));
        edit.value = `  ${STUDY}  `;
        const beforeTyped = listCalls().length;
        press(edit, "Enter");
        await settled(2);
        check("a typed path is trimmed and gone to -- the HPC gesture",
              listCalls()[beforeTyped].body.path === STUDY
              && crumbLabels(dialog).join("|") === "/|home|aj|study");

        const list = byClass(dialog, "plx-explorer-scroller");
        const modL = press(list, "l", { metaKey: true, dispatchOn: list });
        check("Cmd/Ctrl+L opens the path box from anywhere",
              edit.hidden === false && modL.defaultPrevented);
        press(edit, "Escape");
        await cancel(dialog);
    }

    {
        // A trail too long for its bar folds its leading folders into "…".
        const deep = "/n/groups/lab/projects/2024-03-scans/run7/region3";
        const fs = posixFilesystem();
        fs[deep] = folder(deep, "/n/groups/lab/projects/2024-03-scans/run7", []);
        const { dialog } = await open({ start: deep }, { filesystem: fs });
        const trail = byClass(dialog, "plx-explorer-crumbs");
        trail.clientWidth = 300;
        // Each visible crumb is 80px wide in this stub's idea of layout.
        Object.defineProperty(trail, "scrollWidth", {
            get: () => allByClass(trail, "plx-explorer-crumb").filter((c) => !c.hidden).length * 80,
            configurable: true,
        });
        click(action(dialog, "refresh"));
        await settled(2);
        const shown = allByClass(trail, "plx-explorer-crumb").filter((c) => !c.hidden)
            .map((c) => c.textContent);
        const more = byClass(trail, "plx-explorer-crumb-more");
        check("a long trail keeps the root and the open folder and folds the rest",
              shown[0] === "/" && shown[shown.length - 1] === "region3"
              && shown.length * 80 <= 300 && more.hidden === false);
        click(more);
        const folded = menus[menus.length - 1];
        check("...into a menu of the folders it hid, in order",
              folded && folded.items[0].label === "n" && folded.items[0].title === "/n");
        await cancel(dialog);
    }

    // -- 4. filtering this folder -------------------------------------------

    {
        const { dialog } = await open({ start: STUDY });
        const search = byClass(dialog, "plx-explorer-search-input");
        check("with no filter typed the footer counts the folder",
              summaryOf(dialog) === "4 items");
        const before = calls.length;
        search.value = "cells";
        search.dispatchEvent({ type: "input" });
        check("typing narrows the listing in place, no request",
              rowNames(dialog).join(",") === "cells.csv,cells2.csv" && calls.length === before);
        check("...and says how much of the folder that is",
              summaryOf(dialog) === "2 of 4 shown");

        const escape = press(search, "Escape");
        check("Esc empties the filter instead of closing the picker",
              search.value === "" && isOpen(dialog)
              && rowNames(dialog).length === 4);
        check("...and is stopped from reaching the dialog",
              escape.defaultPrevented && escape.propagationStopped);

        search.value = "cells";
        search.dispatchEvent({ type: "input" });
        clickRow(dialog, 0);   // cells.csv, first of the two shown
        await settle();
        check("a filtered row is the row it says it is",
              primaryOf(dialog).disabled === false
              && summaryOf(dialog).indexOf("cells.csv") === 0);
        await cancel(dialog);
    }

    {
        const { dialog } = await open();
        const search = byClass(dialog, "plx-explorer-search-input");
        search.value = "study";
        search.dispatchEvent({ type: "input" });
        clickRow(dialog, 0);
        await settled(2);
        check("a filter belongs to the folder it was typed in",
              search.value === ""
              && rowNames(dialog).join(",") === "runs,cells.csv,cells2.csv,scan.svs");
        await cancel(dialog);
    }

    {
        const truncated = posixFilesystem();
        truncated[STUDY] = folder(STUDY, HOME, truncated[STUDY].entries, true);
        const { dialog } = await open({ start: STUDY }, { filesystem: truncated });
        check("a cut-off folder says so, and says how to get past it",
              byClass(dialog, "plx-explorer-note").textContent
                  .indexOf("first 4 entries") >= 0);
        const search = byClass(dialog, "plx-explorer-search-input");
        search.value = "zzz";
        search.dispatchEvent({ type: "input" });
        check("...and a filter over a cut-off folder says WHICH entries it read",
              summaryOf(dialog) === "0 of the first 4 shown");
        check("an empty filter says so where the rows would be",
              byClass(dialog, "plx-explorer-overlay").textContent
                  === "No matches for “zzz” in this folder");
        await cancel(dialog);
    }

    // -- 5. hidden files -----------------------------------------------------

    {
        const { dialog } = await open({ start: STUDY });
        check("hidden files are off to begin with",
              listCalls()[0].body.show_hidden === false);
        const before = listCalls().length;
        click(action(dialog, "hidden"));
        await settled(2);
        const asked = listCalls()[before];
        check("asking for them re-reads the same folder, hidden ones included",
              asked.body.show_hidden === true && asked.body.path === STUDY);
        check("...without adding a step to go back through",
              action(dialog, "back").disabled === true);
        const list = byClass(dialog, "plx-explorer-scroller");
        const beforeKey = listCalls().length;
        press(list, ">", { metaKey: true, shiftKey: true, code: "Period", dispatchOn: list });
        await settled(2);
        check("Cmd/Ctrl+Shift+. toggles them too",
              listCalls()[beforeKey].body.show_hidden === false);
        await cancel(dialog);
        const written = prefCalls("POST");
        check("...and the choice is remembered with the view, in the closing write",
              written.length === 1 && written[0].body.view
              && written[0].body.view.show_hidden === false);
    }

    // -- 6. where it opens ---------------------------------------------------

    {
        const { dialog } = await open({ start: `${STUDY}/cells.csv` });
        check("a field's current value is handed over as-is; the server "
              + "turns a file into its folder",
              listCalls()[0].body.path === `${STUDY}/cells.csv`
              && crumbLabels(dialog).join("|") === "/|home|aj|study");
        await cancel(dialog);
    }

    {
        const { dialog } = await open({ start: "node://laptop/cells-7f3a91c2" });
        check("a node locator is not a path, and is not opened as one",
              listCalls()[0].body.path === "");
        await cancel(dialog);
    }

    {
        const { dialog } = await open({}, {
            saved: { last_dir: STUDY, recent: [STUDY], pinned: [] },
        });
        check("with nothing to go on it opens where this machine was left",
              listCalls()[0].body.path === STUDY);
        await cancel(dialog);
    }

    {
        const { dialog } = await open({ start: "/gone" });
        check("a remembered folder that has since been deleted falls back home",
              listCalls().map((c) => c.body.path).join("|") === "/gone|"
              && crumbLabels(dialog).join("|") === "/|home|aj");
        check("...and says that is what happened",
              byClass(dialog, "plx-explorer-note").textContent
                  .indexOf("/gone") >= 0);
        await cancel(dialog);
    }

    {
        const { dialog, promise } = await open({}, { saved: null, fsPlaces: null });
        check("a preferences file that cannot be read costs the Recent list, "
              + "not the picker",
              rowNames(dialog).length === 4
              && allByClass(dialog, "plx-explorer-place").length === 1);
        await cancel(dialog);
        await promise;
    }

    // -- 7. remembering places ----------------------------------------------

    {
        const { dialog, promise } = await open({ start: STUDY });
        clickRow(dialog, 1);   // cells.csv
        await settle();
        click(primaryOf(dialog));
        const picked = await promise;
        await settle();
        check("choosing a file resolves with its server-given path",
              picked === `${STUDY}/cells.csv`);
        const written = prefCalls("POST");
        check("...and records the folder once, with both facts in one write",
              written.length === 1
              && written[0].body.last_dir === STUDY
              && written[0].body.add_recent === STUDY);
    }

    {
        const { dialog, promise } = await open({ start: STUDY });
        click(byText(dialog, "Cancel"));
        const picked = await promise;
        await settle();
        const written = prefCalls("POST");
        check("cancelling resolves with nothing, and still remembers the folder",
              picked === null
              && written.length === 1
              && written[0].body.last_dir === STUDY);
        check("...but nothing was taken from it, so Recent does not claim it was",
              written[0].body.add_recent === undefined);
    }

    {
        const { dialog, promise } = await open({}, {
            saved: { last_dir: STUDY, recent: [STUDY], pinned: [] },
        });
        click(byText(dialog, "Cancel"));
        await promise;
        await settle();
        check("opening where it was left and closing again writes nothing",
              prefCalls("POST").length === 0);
    }

    {
        const { dialog } = await open({}, {
            saved: { last_dir: "", recent: [`${STUDY}/runs`], pinned: [STUDY] },
        });
        check("saved places are offered by name, home first",
              placeLabels(dialog).join("|") === "Home|study|runs");
        const before = listCalls().length;
        click(allByClass(dialog, "plx-explorer-place")[1]);
        await settled(2);
        check("...and clicking one goes to the whole path behind it",
              listCalls()[before].body.path === STUDY);
        check("a folder that is already pinned says so",
              byClass(dialog, "plx-explorer-pin").getAttribute("aria-pressed") === "true");
        check("...and the sidebar marks the place that is open",
              allByClass(dialog, "plx-explorer-place")[1].classList.contains("is-current"));

        prefs = { last_dir: "", recent: [`${STUDY}/runs`], pinned: [] };
        click(byClass(dialog, "plx-explorer-pin"));
        await settled(2);
        check("un-pinning asks the server to forget it",
              prefCalls("POST").length === 1
              && prefCalls("POST")[0].body.unpin === STUDY);

        click(action(dialog, "back"));
        await settled(2);
        prefs = { last_dir: "", recent: [`${STUDY}/runs`], pinned: [HOME] };
        click(byClass(dialog, "plx-explorer-pin"));
        await settled(2);
        const written = prefCalls("POST");
        check("pinning asks the server to keep it, per machine",
              written.length === 2 && written[1].body.pin === HOME
              && written[1].body.node === "");
        check("...and the sidebar redraws from what the server came back with",
              placeLabels(dialog).join("|") === "Home|aj|runs");
        await cancel(dialog);
    }

    {
        const { dialog } = await open({ node: "hpc" }, {
            fsPlaces: { ...defaultPlaces(), hostname: "login01" },
        });
        check("browsing a node lists THAT machine, and nothing else would do",
              listCalls()[0].body.node === "hpc"
              && calls[0].url.indexOf("node=hpc") >= 0
              && calls.some((c) => c.url.indexOf("fs_places?node=hpc") >= 0));
        check("...and says which machine it is, in the title and the sidebar",
              byClass(dialog, "plx-explorer-subtitle").textContent === "on hpc — login01"
              && byClass(dialog, "plx-explorer-machine-name").textContent === "hpc — login01");
        await cancel(dialog);
    }

    {
        const fsPlaces = {
            ...defaultPlaces(),
            platform: "windows",
            known: [{ id: "desktop", label: "Desktop", path: "C:\\Users\\aj\\Desktop" }],
            volumes: [
                { label: "Local Disk (C:)", path: "C:\\", kind: "local", fstype: "NTFS",
                  total: 1099511627776, free: 442381631488 },
                { label: "Data (D:)", path: "D:\\", kind: "local", fstype: "NTFS", total: null, free: null },
                { label: "Network Drive (Z:)", path: "Z:\\", kind: "network", fstype: "", total: null, free: null },
            ],
            project: [{ label: "Plexora data", path: "C:\\Users\\aj\\plexora" }],
        };
        const { dialog } = await open({}, { fsPlaces });
        check("drives are listed by their own names, with how full they are",
              placeLabels(dialog).join("|")
                  === "Home|Desktop|Local Disk (C:)|Data (D:)|Network Drive (Z:)|Plexora data"
              && byClass(allByClass(dialog, "plx-explorer-place")[2], "plx-explorer-place-caption")
                  .textContent === "412.0 GB free of 1.0 TB");
        const before = listCalls().length;
        click(allByClass(dialog, "plx-explorer-place")[3]);
        await settled(2);
        check("...and a drive opens at the path the server gave it",
              listCalls()[before].body.path === "D:\\");
        const pinRow = allByClass(dialog, "plx-explorer-place")[2];
        pinRow.dispatchEvent({ type: "contextmenu", clientX: 10, clientY: 20 });
        const menu = menus[menus.length - 1];
        check("a place's menu opens it, pins it, or copies its path",
              menu && menu.items.map((i) => i.label).join("|") === "Open|Pin to Favorites|Copy path");
        await cancel(dialog);
    }

    // -- 8. keyboard, and what a listing is to a screen reader --------------

    {
        const { dialog, promise } = await open({ start: STUDY });
        const list = byClass(dialog, "plx-explorer-scroller");
        check("the listing is a listbox of options",
              list.getAttribute("role") === "listbox"
              && rows(dialog).every((r) => r.getAttribute("role") === "option"));

        press(dialog, "Enter", { target: byText(dialog, "Cancel") });
        check("Enter on a focused button belongs to the browser, which clicks "
              + "it -- answering it here too closed the picker from anywhere",
              isOpen(dialog));

        press(list, "ArrowDown");
        press(list, "ArrowDown");
        check("arrows walk the listing and say where they are",
              list.getAttribute("aria-activedescendant")
                  === rows(dialog)[1].getAttribute("id"));
        check("...selecting the file they land on, as a desktop dialog does",
              rows(dialog)[1].classList.contains("is-selected"));
        press(list, "End");
        check("End goes to the last row, Home to the first",
              list.getAttribute("aria-activedescendant") === rows(dialog)[3].getAttribute("id")
              && (press(list, "Home"), list.getAttribute("aria-activedescendant")
                  === rows(dialog)[0].getAttribute("id")));

        press(list, "Backspace");
        await settled(2);
        check("Backspace is Up",
              crumbLabels(dialog).join("|") === "/|home|aj");

        const search = byClass(dialog, "plx-explorer-search-input");
        search.value = "stu";
        const typed = press(dialog, "Backspace", { target: search });
        check("...but not while somebody is typing in a box",
              typed.defaultPrevented === false
              && crumbLabels(dialog).join("|") === "/|home|aj");
        search.value = "";

        const modF = press(list, "f", { ctrlKey: true });
        check("Cmd/Ctrl+F goes to the filter box",
              modF.defaultPrevented && search.focused === true);

        press(list, "ArrowDown");
        press(list, "ArrowDown");
        press(list, "Enter");
        await settled(2);
        check("Enter on a folder opens it",
              crumbLabels(dialog).join("|") === "/|home|aj|study");

        press(list, "s");
        check("typing a letter jumps to the next name that starts with it",
              list.getAttribute("aria-activedescendant") === rows(dialog)[3].getAttribute("id"));

        press(list, "Home");
        press(list, "ArrowDown");
        press(list, "Enter");
        const picked = await promise;
        check("Enter on a file chooses it and closes",
              picked === `${STUDY}/cells.csv`);
        await settle();
    }

    {
        const { dialog } = await open({ start: STUDY, filter: "h5ad" });
        const drawn = rows(dialog);
        check("a file the field cannot take is shown, greyed",
              drawn[1].classList.contains("is-muted") === true
              && drawn[0].classList.contains("is-muted") === false);
        clickRow(dialog, 1);
        await settle();
        check("...and clicking it selects nothing",
              primaryOf(dialog).disabled === true);
        check("...but says why, in the footer",
              summaryOf(dialog) === "This field takes an .h5ad file."
              && byClass(dialog, "plx-explorer-summary").classList.contains("is-explaining"));
        await cancel(dialog);
    }

    // -- 9. what each row is -------------------------------------------------

    {
        const { dialog } = await open();
        check("the Type column names the thing, not the extension",
              rowCells(dialog, "is-kind").join("|") === "Zarr store|Folder|OME-TIFF|Text");
        check("...and a size is only ever on a file",
              rowCells(dialog, "is-size").join("|") === "||2.0 KB|12 B");
        await cancel(dialog);
    }

    {
        const fs = posixFilesystem();
        fs["/odd"] = folder("/odd", "/", [
            dir("/odd/locked", { kind: "denied" }),
            file("/odd/gone.csv", null, { kind: "broken" }),
            file("/odd/alias.csv", 10, { kind: "link" }),
        ]);
        const { dialog } = await open({ start: "/odd" }, { filesystem: fs });
        const drawn = rows(dialog);   // locked, alias.csv, gone.csv
        check("a locked folder and a broken link are drawn, and say so",
              drawn[0].classList.contains("is-denied") && drawn[2].classList.contains("is-broken")
              && rowCells(dialog, "is-kind")[2] === "Broken link");
        const before = listCalls().length;
        clickRow(dialog, 0);
        await settled(2);
        check("...and neither can be opened or chosen",
              listCalls().length === before && primaryOf(dialog).disabled === true
              && summaryOf(dialog) === "You do not have permission to open this folder.");
        check("a shortcut carries a link badge",
              byClass(drawn[1], "plx-explorer-badge") !== null);
        await cancel(dialog);
    }

    // -- 10. directory mode --------------------------------------------------

    {
        const { dialog, promise } = await open({ mode: "directory", start: STUDY });
        check("standing in a folder IS choosing it",
              primaryOf(dialog).disabled === false
              && primaryOf(dialog).textContent === "Use Folder");
        check("files are greyed in a folder picker",
              rows(dialog)[1].classList.contains("is-muted"));
        click(primaryOf(dialog));
        check("...which is how a .zarr store is picked", await promise === STUDY);
        await settle();
    }

    {
        const { dialog, promise } = await open({ mode: "directory", start: HOME });
        rows(dialog)[1].dispatchEvent({ type: "contextmenu", clientX: 5, clientY: 5 });
        const menu = menus[menus.length - 1];
        check("a folder's menu in a folder picker can choose it without going in",
              menu.items.map((i) => i.label).slice(0, 2).join("|") === "Open|Choose this folder");
        menu.items[1].onSelect();
        check("...and choosing it answers with that folder", await promise === STUDY);
        await settle();
    }

    // -- 11. one button, either kind (mode "any") ---------------------------

    {
        const { dialog, promise } = await open({ mode: "any" });
        const drawn = rows(dialog);
        check("a store row is marked as one, and only it",
              drawn[0].classList.contains("is-store") === true
              && drawn[1].classList.contains("is-store") === false);

        const before = listCalls().length;
        clickRow(dialog, 0);              // store.zarr
        await settle();
        check("one click on a store selects it rather than opening it",
              listCalls().length === before
              && primaryOf(dialog).disabled === false
              && primaryOf(dialog).textContent === "Select");
        click(primaryOf(dialog));
        check("...and Choose answers with the store itself",
              await promise === `${HOME}/store.zarr`);
        await settle();
    }

    {
        const { dialog, promise } = await open({ mode: "any" });
        const before = listCalls().length;
        clickRow(dialog, 1);              // study, an ordinary folder
        await settled(2);
        check("an ordinary folder is still a place, and one click goes there",
              listCalls()[before].body.path === STUDY
              && crumbLabels(dialog).join("|") === "/|home|aj|study");
        clickRow(dialog, 1);              // cells.csv
        await settle();
        click(primaryOf(dialog));
        check("...and a file in it is chosen exactly as in file mode",
              await promise === `${STUDY}/cells.csv`);
        await settle();
    }

    {
        const { dialog, promise } = await open({ mode: "any" });
        rows(dialog)[0].dispatchEvent({ type: "dblclick" });
        check("double-clicking a store is 'this one', as it is on a file",
              await promise === `${HOME}/store.zarr`);
        await settle();
    }

    {
        const { dialog } = await open({ mode: "any" });
        const store = rows(dialog)[0];
        const enter = byClass(store, "plx-explorer-enter");
        const before = listCalls().length;
        click(enter);
        await settled(2);
        check("the › beside a store is the way into it, for the rare time "
              + "somebody wants what is inside",
              listCalls()[before].body.path === `${HOME}/store.zarr`);
        await cancel(dialog);
    }

    {
        const { dialog, promise } = await open({ mode: "any", start: STUDY });
        const useFolder = byClass(dialog, "plx-explorer-use-folder");
        check("'Use this folder' is live once a folder has been listed",
              useFolder.disabled === false && useFolder.textContent === "Use this folder");
        click(useFolder);
        check("...and answers with the folder that is open, whatever it is called",
              await promise === STUDY);
        await settle();
    }

    {
        const { dialog } = await open({ mode: "file" });
        check("none of that exists in file mode: no store rows",
              rows(dialog).every((r) => r.classList.contains("is-store") === false)
              && allByClass(dialog, "plx-explorer-enter").length === 0);
        check("...and no folder button to press by accident",
              byText(dialog, "Use this folder") === null);
        const before = listCalls().length;
        clickRow(dialog, 0);              // store.zarr, a plain folder here
        await settled(2);
        check("...so a .zarr opens, which is what file mode always did",
              listCalls()[before].body.path === `${HOME}/store.zarr`);
        await cancel(dialog);
    }

    {
        // The server marks datasets itself, by their markers, whatever the name.
        const fs = posixFilesystem();
        fs["/runs"] = folder("/runs", "/", [
            dir("/runs/output-XETG", { store: { type: "xenium", label: "Xenium run" } }),
            dir("/runs/plain"),
        ]);
        fs["/runs/output-XETG"] = folder("/runs/output-XETG", "/runs", [file("/runs/output-XETG/a.csv", 1)],
                                         false, { dataset: { type: "xenium", label: "Xenium run", licensed: true } });
        const { dialog, promise } = await open({ mode: "any", start: "/runs" }, { filesystem: fs });
        check("a run folder the server recognised selects in one click",
              rows(dialog)[0].classList.contains("is-store")
              && rowCells(dialog, "is-kind")[0] === "Xenium run");
        click(byClass(rows(dialog)[0], "plx-explorer-enter"));
        await settled(2);
        const banner = byClass(dialog, "plx-explorer-banner");
        check("inside one, a banner says what it is and offers it whole",
              banner.hidden === false
              && byClass(banner, "plx-explorer-banner-text").textContent === "This folder is a Xenium run.");
        click(byClass(banner, "plx-explorer-banner-use"));
        check("...and its button answers with the run", await promise === "/runs/output-XETG");
        await settle();
    }

    {
        const { dialog, promise } = await open({ accept: { files: [".csv"] }, start: STUDY,
                                                 primary: "Add table" });
        check("a caller's own suffixes grey the rest",
              rows(dialog)[3].classList.contains("is-muted")
              && !rows(dialog)[1].classList.contains("is-muted"));
        check("...and its own words are on the button",
              primaryOf(dialog).textContent === "Add table");
        clickRow(dialog, 2);
        click(primaryOf(dialog));
        check("...which answers as any pick does", await promise === `${STUDY}/cells2.csv`);
        await settle();
    }

    // -- 12. more than one file ---------------------------------------------

    {
        const { dialog, promise } = await open({ start: STUDY, multiple: true });
        check("a multi-select listing says so",
              byClass(dialog, "plx-explorer-scroller")
                  .getAttribute("aria-multiselectable") === "true");
        clickRow(dialog, 1);
        clickRow(dialog, 3, { metaKey: true });
        await settle();
        check("the button and the footer count what is chosen",
              primaryOf(dialog).textContent === "Open 2 files"
              && summaryOf(dialog).indexOf("2 items selected") === 0);
        click(primaryOf(dialog));
        check("cmd-click adds to the selection, and the answer is an array "
              + "in the order the rows are drawn",
              JSON.stringify(await promise)
                  === JSON.stringify([`${STUDY}/cells.csv`, `${STUDY}/scan.svs`]));
        await settle();
    }

    {
        const { dialog, promise } = await open({ start: STUDY, multiple: true });
        clickRow(dialog, 1);
        clickRow(dialog, 3, { shiftKey: true });
        await settle();
        click(primaryOf(dialog));
        check("shift-click takes the run between them",
              JSON.stringify(await promise) === JSON.stringify([
                  `${STUDY}/cells.csv`, `${STUDY}/cells2.csv`, `${STUDY}/scan.svs`]));
        await settle();
    }

    {
        const { dialog, promise } = await open({ start: STUDY, select: "many" });
        const list = byClass(dialog, "plx-explorer-scroller");
        press(list, "a", { metaKey: true });
        click(primaryOf(dialog));
        check("Cmd/Ctrl+A takes every file the field can take, and no folder",
              JSON.stringify(await promise) === JSON.stringify([
                  `${STUDY}/cells.csv`, `${STUDY}/cells2.csv`, `${STUDY}/scan.svs`]));
        await settle();
    }

    {
        const { dialog, promise } = await open({ start: STUDY });
        clickRow(dialog, 1);
        clickRow(dialog, 3, { metaKey: true });
        await settle();
        click(primaryOf(dialog));
        check("without `multiple` the answer is one path, as it always was",
              await promise === `${STUDY}/scan.svs`);
        await settle();
    }

    // -- 13. a node too old to send paths or crumbs -------------------------

    {
        const legacy = {
            "": { path: HOME, parent: "/home", truncated: false,
                  entries: [{ name: "study", is_dir: true, size: null }] },
            [`${HOME}/study`]: { path: STUDY, parent: HOME, truncated: false,
                                 entries: [] },
        };
        const { dialog } = await open({}, { filesystem: legacy });
        check("a node that sends no crumbs leaves the path box in place of them",
              byClass(dialog, "plx-explorer-crumb-edit").hidden === false
              && crumbLabels(dialog).length === 0);
        const before = listCalls().length;
        clickRow(dialog, 0);
        await settled(2);
        check("...and a row with no path of its own is still openable",
              listCalls()[before].body.path === `${HOME}/study`);
        await cancel(dialog);
    }

    // -- 14. sorting ---------------------------------------------------------

    {
        const { dialog } = await open({ start: STUDY });
        const header = (name) => byAttr(byClass(dialog, "plx-explorer-head"), "data-col", name);
        const sortBy = (name) => click(allByClass(dialog, "plx-explorer-col")
            .find((c) => c.getAttribute("data-col") === name));
        sortBy("size");
        check("a column sorts on a click, largest first, folders still first",
              rowNames(dialog).join(",") === "runs,scan.svs,cells2.csv,cells.csv"
              && header("size").getAttribute("aria-sort") === "descending");
        sortBy("size");
        check("...and the other way on a second click",
              rowNames(dialog).join(",") === "runs,cells.csv,cells2.csv,scan.svs"
              && header("size").getAttribute("aria-sort") === "ascending");
        sortBy("mtime");
        check("Date Modified sorts newest first",
              rowNames(dialog).join(",") === "runs,cells2.csv,cells.csv,scan.svs");
        sortBy("name");
        sortBy("name");
        check("Name sorts Z to A on its second click",
              rowNames(dialog).join(",") === "runs,scan.svs,cells2.csv,cells.csv");
        click(action(dialog, "sort"));
        const menu = menus[menus.length - 1];
        menu.items.find((i) => i.label === "Folders first").onSelect();
        check("folders first can be switched off from the Sort menu",
              rowNames(dialog).join(",") === "scan.svs,runs,cells2.csv,cells.csv");
        await wait(450);
        const written = prefCalls("POST");
        check("the view is remembered in one write, not one per click",
              written.length === 1 && written[0].body.view.sort_by === "name"
              && written[0].body.view.sort_dir === "desc"
              && written[0].body.view.dirs_first === false);
        await cancel(dialog);
    }

    {
        const fs = posixFilesystem();
        const { dialog } = await open({ start: STUDY }, {
            filesystem: fs,
            saved: { last_dir: "", recent: [], pinned: [],
                     view: { sort_by: "size", sort_dir: "asc", layout: "grid" } },
        });
        check("a remembered view is applied before the first listing is drawn",
              rowNames(dialog).join(",") === "runs,cells.csv,cells2.csv,scan.svs"
              && byClass(dialog, "plx-explorer-listing").classList.contains("is-grid")
              && rows(dialog)[0].classList.contains("plx-explorer-tile"));
        click(action(dialog, "layout-list"));
        check("...and the list/grid switch changes it",
              !byClass(dialog, "plx-explorer-listing").classList.contains("is-grid")
              && action(dialog, "layout-list").getAttribute("aria-pressed") === "true");
        await cancel(dialog);
    }

    // -- 15. a big folder ------------------------------------------------------

    {
        const fs = posixFilesystem();
        fs["/scratch/big"] = bigFolder(10000, false);
        const { dialog } = await open({ start: "/scratch/big" }, { filesystem: fs });
        const spacer = byClass(dialog, "plx-explorer-spacer");
        check("ten thousand entries draw a window of rows, not ten thousand",
              rows(dialog).length <= 56 && rows(dialog).length > 0
              && spacer.style.height === `${10000 * 28}px`);
        check("...with every row saying where it sits in the whole",
              rows(dialog)[0].getAttribute("aria-setsize") === "10000");
        const list = byClass(dialog, "plx-explorer-scroller");
        list.scrollTop = 5000 * 28;
        list.dispatchEvent({ type: "scroll" });
        await settled(2);
        const first = Number(rows(dialog)[0].getAttribute("data-index"));
        check("scrolling moves the window",
              first > 4900 && first <= 5000 && rows(dialog).length <= 56);
        check("names sort naturally: f9 before f10",
              Types.collator && (() => {
                  const names = Array.from({ length: 12 }, (_, i) => `f${i}`);
                  const key = context.window.PlexoraExplorerListing.naturalKey;
                  return names.slice().sort((a, b) => (key(a) < key(b) ? -1 : 1)).join() === names.join();
              })());
        press(list, "End");
        check("End reaches the last of ten thousand, scrolling it into view",
              list.getAttribute("aria-activedescendant").endsWith("-row-9999")
              && list.scrollTop > 9000 * 28);
        await cancel(dialog);
    }

    {
        const fs = posixFilesystem();
        fs["/scratch/big"] = bigFolder(1200, true);
        const { dialog } = await open({ start: "/scratch/big" }, { filesystem: fs });
        await settled(8);
        const asks = calls.filter((c) => c.url.indexOf("stat_entries") >= 0);
        check("a partial listing asks for sizes and dates in chunks of 500",
              asks.length === 3 && asks[0].body.names.length === 500
              && asks[2].body.names.length === 200);
        check("...the rows on screen first",
              asks[0].body.names[0] === "f0.csv" && asks[0].body.path === "/scratch/big");
        check("...and draws them as they land",
              rowCells(dialog, "is-size")[0] === "1.2 KB");
        await cancel(dialog);
    }

    {
        const fs = posixFilesystem();
        fs["/scratch/big"] = bigFolder(1200, true);
        let release;
        statGate = new Promise((resolve) => { release = resolve; });
        const { dialog } = await open({ start: "/scratch/big" }, {
            filesystem: fs,
            saved: { last_dir: "", recent: [], pinned: [], view: { sort_by: "size", sort_dir: "asc" } },
        });
        const caret = () => byClass(byAttr(byClass(dialog, "plx-explorer-head"), "data-col", "size"),
                                    "plx-explorer-caret");
        check("a size sort on a partial listing keeps names in order until the sizes are in",
              rowNames(dialog)[0] === "f0.csv" && caret().classList.contains("is-spinning"));
        statGate = null;
        release();
        await settled(8);
        check("...then sorts, and the header stops waiting",
              rowNames(dialog)[0] === "f1199.csv" && !caret().classList.contains("is-spinning"));
        await cancel(dialog);
    }

    {
        const fs = posixFilesystem();
        fs["/scratch/big"] = bigFolder(1200, true);
        fs["/scratch"] = folder("/scratch", "/", [dir("/scratch/big")]);
        let release;
        statGate = new Promise((resolve) => { release = resolve; });
        const { dialog } = await open({ start: "/scratch/big" }, { filesystem: fs });
        click(action(dialog, "up"));
        await settled(2);
        statGate = null;
        release();
        await settled(8);
        const asked = calls.filter((c) => c.url.indexOf("stat_entries") >= 0).length;
        check("leaving a folder stops asking about it, and a late answer is dropped",
              asked === 1 && rowNames(dialog).join() === "big"
              && rowCells(dialog, "is-size").join() === "");
        await cancel(dialog);
    }

    // -- 16. searching below the folder -----------------------------------------

    {
        const fs = posixFilesystem();
        searchAnswer = (sent) => ({ status: 200, payload: {
            path: sent.path, query: sent.query, visited: 12, stopped: "budget",
            matches: [
                { name: "cells.csv", is_dir: false, size: null, mtime: null, kind: "file",
                  path: `${STUDY}/runs/r1/cells.csv`, rel: "runs/r1/cells.csv", depth: 3 },
            ],
        } });
        const { dialog, promise } = await open({ start: STUDY }, { filesystem: fs });
        const search = byClass(dialog, "plx-explorer-search-input");
        search.value = "cells";
        search.dispatchEvent({ type: "input" });
        const deepButton = byClass(dialog, "plx-explorer-search-deep");
        check("a typed filter offers to search the subfolders too",
              deepButton.hidden === false);
        press(search, "Enter");
        await settled(3);
        const asked = calls.filter((c) => c.url.indexOf("search_dir") >= 0);
        check("Enter in the box searches below the open folder, within a budget",
              asked.length === 1 && asked[0].body.path === STUDY && asked[0].body.query === "cells"
              && asked[0].body.budget_ms === 3000 && asked[0].body.limit === 500);
        check("...and draws the matches with where each one is",
              rowNames(dialog).join() === "cells.csv"
              && rowCells(dialog, "is-where")[0] === "runs/r1"
              && byClass(dialog, "plx-explorer-listing").classList.contains("is-search"));
        check("...saying when the search stopped short",
              byClass(dialog, "plx-explorer-searchbar-text").textContent.indexOf("stopped") >= 0);
        search.value = "cell";
        search.dispatchEvent({ type: "input" });
        search.value = "cel";
        search.dispatchEvent({ type: "input" });
        await wait(150);
        const early = calls.filter((c) => c.url.indexOf("search_dir") >= 0).length;
        await wait(250);
        await settled(2);
        const later = calls.filter((c) => c.url.indexOf("search_dir") >= 0);
        check("typing while searching waits for a pause before asking again",
              early === 1 && later.length === 2 && later[1].body.query === "cel");
        clickRow(dialog, 0);
        click(primaryOf(dialog));
        check("a match is chosen by its own path", await promise === `${STUDY}/runs/r1/cells.csv`);
        await settle();
        searchAnswer = null;
    }

    {
        const fs = posixFilesystem();
        let release;
        searchAnswer = (sent) => ({ status: 200, payload: { matches: [
            { name: `${sent.query}.csv`, is_dir: false, path: `${STUDY}/${sent.query}.csv`,
              rel: `${sent.query}.csv`, depth: 1 }], stopped: null } });
        const { dialog } = await open({ start: STUDY }, { filesystem: fs });
        const search = byClass(dialog, "plx-explorer-search-input");
        search.value = "cells";
        search.dispatchEvent({ type: "input" });
        press(search, "Enter");
        await settled(3);
        click(action(dialog, "up"));
        await settled(3);
        check("going somewhere else leaves the search behind",
              crumbLabels(dialog).join("|") === "/|home|aj" && search.value === ""
              && byClass(dialog, "plx-explorer-searchbar").hidden === true
              && rowNames(dialog).length === 4);
        await cancel(dialog);
        searchAnswer = null;
        void release;
    }

    {
        const fs = posixFilesystem();
        searchAnswer = () => ({ status: 200, payload: { unsupported: true, matches: [], visited: 0,
                                                       stopped: null } });
        const { dialog } = await open({ start: STUDY, node: "old" }, { filesystem: fs });
        const search = byClass(dialog, "plx-explorer-search-input");
        search.value = "cells";
        search.dispatchEvent({ type: "input" });
        press(search, "Enter");
        await settled(3);
        check("a node too old to search says so and keeps the filter",
              byClass(dialog, "plx-explorer-searchbar-text").textContent.indexOf("not available") >= 0
              && rowNames(dialog).join() === "cells.csv,cells2.csv");
        await cancel(dialog);
        searchAnswer = null;
    }

    // -- 17. the window ----------------------------------------------------------

    {
        storageValues.clear();
        const { dialog } = await open();
        const start = rect(dialog);
        check("the window opens at 1000x650, centred",
              start.width === 1000 && start.height === 650
              && start.left === 220 && start.top === 125 && dialog.open === true);
        const bar = byClass(dialog, "plx-explorer-titlebar");
        pointer(bar, "pointerdown", 300, 140);
        pointer(bar, "pointermove", 302, 141);
        check("a press that barely moves is not a drag", rect(dialog).left === 220);
        pointer(bar, "pointermove", 400, 200);
        pointer(bar, "pointerup", 400, 200);
        check("dragging the title bar moves the window",
              rect(dialog).left === 320 && rect(dialog).top === 185);
        const close = byAttr(dialog, "data-win", "close");
        pointer(bar, "pointerdown", 600, 140, { target: close });
        pointer(bar, "pointermove", 700, 300, { target: close });
        pointer(bar, "pointerup", 700, 300, { target: close });
        check("...but a press on its buttons does not",
              rect(dialog).left === 320 && rect(dialog).top === 185);
        pointer(bar, "pointerdown", 400, 200);
        pointer(bar, "pointermove", 5000, 5000);
        pointer(bar, "pointerup", 5000, 5000);
        check("...and a drag cannot lose the window off the edge",
              rect(dialog).left === 1440 - 80 && rect(dialog).top === 900 - 36);
        pointer(bar, "pointerdown", 1400, 880);
        pointer(bar, "pointermove", 1100, 500);
        press(dialog, "Escape", { dispatchOn: dialog });
        check("Esc mid-drag puts it back",
              rect(dialog).left === 1440 - 80 && dialog.open === true);
        pointer(bar, "pointerup", 1100, 500);
        await cancel(dialog);
    }

    {
        storageValues.clear();
        const { dialog } = await open();
        const handle = (edge) => byAttr(dialog, "data-edge", edge);
        check("every edge and corner has a handle",
              ["n", "s", "e", "w", "ne", "nw", "se", "sw"].every((edge) => handle(edge) !== null));
        pointer(handle("e"), "pointerdown", 1220, 400);
        pointer(handle("e"), "pointermove", 1300, 400);
        pointer(handle("e"), "pointerup", 1300, 400);
        check("the right edge widens the window", rect(dialog).width === 1080 && rect(dialog).left === 220);
        pointer(handle("w"), "pointerdown", 220, 400);
        pointer(handle("w"), "pointermove", 1000, 400);
        pointer(handle("w"), "pointerup", 1000, 400);
        check("...the left edge narrows it no further than the minimum, keeping the right edge",
              rect(dialog).width === 640 && rect(dialog).left === 220 + 1080 - 640);
        pointer(handle("nw"), "pointerdown", 660, 125);
        pointer(handle("nw"), "pointermove", -500, -500);
        pointer(handle("nw"), "pointerup", -500, -500);
        check("...and a corner stops at the viewport's margin",
              rect(dialog).left === 12 && rect(dialog).top === 12);
        pointer(handle("s"), "pointerdown", 400, 775);
        pointer(handle("s"), "pointermove", 400, 0);
        pointer(handle("s"), "pointerup", 400, 0);
        check("...and the bottom edge stops at the minimum height", rect(dialog).height === 420);

        click(byAttr(dialog, "data-win", "maximize"));
        check("maximize fills the viewport bar a 12px margin",
              JSON.stringify(rect(dialog)) === JSON.stringify({ left: 12, top: 12, width: 1416, height: 876 })
              && dialog.classList.contains("is-maximized")
              && byAttr(dialog, "data-win", "maximize").getAttribute("title") === "Restore");
        click(byAttr(dialog, "data-win", "maximize"));
        check("...and restore puts it back",
              rect(dialog).height === 420 && rect(dialog).left === 12
              && !dialog.classList.contains("is-maximized"));

        click(byAttr(dialog, "data-win", "rollup"));
        check("minimize rolls the window up to its title bar",
              dialog.classList.contains("is-rolled") && rect(dialog).height === 38);
        const list = byClass(dialog, "plx-explorer-scroller");
        press(list, "ArrowDown");
        check("...where the listing's keys do nothing",
              list.getAttribute("aria-activedescendant") === "" || list.getAttribute("aria-activedescendant") === null);
        click(byClass(dialog, "plx-explorer-titlebar"));
        check("...and a click on the bar brings it back",
              !dialog.classList.contains("is-rolled") && rect(dialog).height === 420);
        await cancel(dialog);

        const saved = JSON.parse(storageValues.get("plexora:explorer-window:file-explorer") || "null");
        check("the window's place is kept for the session",
              saved && saved.left === 12 && saved.height === 420 && saved.maximized === false);
        const again = await open();
        check("...and the next one opens there",
              rect(again.dialog).left === 12 && rect(again.dialog).height === 420);
        await cancel(again.dialog);
    }

    {
        storageValues.clear();
        context.innerWidth = 700;
        context.innerHeight = 500;
        const { dialog } = await open();
        check("a viewport narrower than the window gets the viewport, minus the margin",
              rect(dialog).width === 676 && rect(dialog).height === 476 && rect(dialog).left === 12);
        await cancel(dialog);
        context.innerWidth = 1440;
        context.innerHeight = 900;
        storageValues.clear();
    }

    {
        const opener = makeElement("button");
        body.appendChild(opener);
        opener.focus();
        const { dialog, promise } = await open();
        check("the listing has focus once the window is open",
              documentStub.activeElement === byClass(dialog, "plx-explorer-scroller"));
        menuOpen = true;
        const escape = { type: "keydown", key: "Escape" };
        byClass(dialog, "plx-explorer-scroller").dispatchEvent(escape);
        check("Esc while a menu is open is kept from closing the window",
              escape.defaultPrevented === true && dialog.open === true);
        menuOpen = false;
        dialog.close();   // what the browser does with Esc otherwise
        check("Esc closes with nothing chosen", await promise === null);
        check("...and focus goes back to what opened it", documentStub.activeElement === opener);
        opener.remove();
        await settle();
    }

    // -- 18. the machine's own dialog ----------------------------------------------

    {
        let asked = null;
        const systemDialog = {
            label: "Use Finder…",
            available: () => Promise.resolve("hybrid"),
            open: (kind) => { asked = kind; return Promise.resolve(`${HOME}/from-finder.tif`); },
        };
        const { dialog, promise } = await open({ mode: "any", systemDialog });
        click(action(dialog, "more"));
        const menu = menus[menus.length - 1];
        check("the overflow offers the system dialog where there is one",
              menu.items[0].label === "Use Finder…");
        menu.items[0].onSelect();
        check("...and its answer is the explorer's answer",
              await promise === `${HOME}/from-finder.tif` && asked === "any");
        await settle();
    }

    {
        const systemDialog = {
            label: "Use system dialog…",
            available: () => Promise.resolve("none"),
            open: () => Promise.resolve(null),
        };
        const { dialog } = await open({ mode: "file", systemDialog });
        click(action(dialog, "more"));
        const menu = menus[menus.length - 1];
        check("...and not where there is none",
              !menu.items.some((item) => item.label === "Use system dialog…"));
        await cancel(dialog);
    }

    {
        const systemDialog = {
            label: "Use system dialog…",
            available: () => Promise.resolve("kinds"),
            open: () => Promise.resolve(null),
        };
        const { dialog } = await open({ mode: "file", systemDialog });
        click(action(dialog, "more"));
        menus[menus.length - 1].items[0].onSelect();
        await settled(2);
        check("cancelling the system dialog leaves the explorer open", isOpen(dialog));
        await cancel(dialog);
    }

    // -- 19. menus ------------------------------------------------------------------

    {
        const { dialog } = await open({ start: STUDY }, {
            saved: { last_dir: "", recent: [], pinned: [STUDY] },
        });
        rows(dialog)[1].dispatchEvent({ type: "contextmenu", clientX: 30, clientY: 40 });
        let menu = menus[menus.length - 1];
        check("a file's menu chooses it or copies its path",
              menu.items.filter((i) => !i.separator).map((i) => i.label).join("|") === "Open|Copy path"
              && menu.anchor.getBoundingClientRect().left === 30);
        menu.items.find((i) => i.label === "Copy path").onSelect();
        await settle();
        check("...and Copy path puts the server's path on the clipboard",
              clipboard === `${STUDY}/cells.csv`);
        rows(dialog)[0].dispatchEvent({ type: "contextmenu", clientX: 1, clientY: 1 });
        menu = menus[menus.length - 1];
        check("a folder's menu opens it or pins it",
              menu.items.filter((i) => !i.separator).map((i) => i.label).join("|")
                  === "Open|Pin to Favorites|Copy path");
        const pin = allByClass(dialog, "plx-explorer-pinrow")[0];
        byClass(pin, "plx-explorer-place").dispatchEvent({ type: "contextmenu", clientX: 1, clientY: 1 });
        menu = menus[menus.length - 1];
        check("a pin's menu unpins it",
              menu.items.map((i) => i.label).join("|") === "Open|Unpin from Favorites|Copy path");
        allByClass(dialog, "plx-explorer-crumb")[1].dispatchEvent({ type: "contextmenu", clientX: 1, clientY: 1 });
        menu = menus[menus.length - 1];
        check("a crumb's menu opens, pins or copies that folder",
              menu.items.map((i) => i.label).join("|") === "Open|Pin to Favorites|Copy path");
        await cancel(dialog);
    }

    // -- and nothing is left on the page ------------------------------------

    await settle();
    check("every picker that closed took its dialog with it",
          body.children.length === 0);
    check("...and its document listeners",
          (documentListeners.get("keydown") || []).length === 0);

    console.log("");
    if (failures.length) {
        console.log(`${failures.length} check(s) failed`);
        process.exitCode = 1;
    } else {
        console.log("all checks passed");
    }
    process.exit();
}

main().catch((error) => {
    console.log(`FAIL the probe threw: ${error && error.stack}`);
    process.exitCode = 1;
    process.exit();
});
