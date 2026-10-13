/**
 * The 3-D dock (views/volumeDock.js): cutting the stack from the canvas.
 *
 * Driven through a small DOM, as a user would. Pinned: the dock is there only
 * in 3-D; each spatial mode shows its rows and no other (the registry's
 * "dock" surface); X+Y shows exactly the X and Y rows; a slider previews while
 * dragged and commits once on release, one undo entry; a mode's settings are
 * kept while another is used; lengths are in microns only where the image
 * states its spacing; surfaces alone hide every row about voxels; undo and
 * redo by button and by Cmd/Ctrl+Z.
 *
 * Run directly:  node tests/js/volume_dock_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

// -- a small DOM ---------------------------------------------------------------

let active = null;
function matches(node, selector) {
    return selector.split(",").map((s) => s.trim()).some((one) => {
        if (one === ":hover") return Boolean(node.hovered);
        const attr = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(one);
        if (attr) return attr[2] === undefined ? node.getAttribute(attr[1]) !== null
            : node.getAttribute(attr[1]) === attr[2];
        if (one.startsWith(".")) return one.slice(1).split(".").every((c) => node.classList.contains(c));
        return node.tagName === one.toUpperCase();
    });
}
function fakeElement(tag) {
    const attributes = {};
    const classes = new Set();
    const listeners = {};
    const node = {
        tagName: String(tag).toUpperCase(),
        children: [], parentElement: null, hidden: false, disabled: false, value: "", checked: false,
        textContent: "", isConnected: true, dataset: {}, title: "",
        style: { setProperty(name, value) { this[name] = value; }, removeProperty(name) { delete this[name]; } },
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
        get className() { return [...classes].join(" "); },
        classList: {
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
            toggle: (name, on) => { const want = on === undefined ? !classes.has(name) : on; if (want) classes.add(name); else classes.delete(name); return want; },
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) {
            attributes[name] = String(value);
            if (name === "id") node.id = String(value);
            if (name === "title") node.title = String(value);
        },
        removeAttribute(name) { delete attributes[name]; },
        getAttribute(name) { return attributes[name] ?? null; },
        hasAttribute(name) { return name in attributes; },
        appendChild(child) { child.parentElement = node; node.children.push(child); return child; },
        append(...nodes) { for (const child of nodes) node.appendChild(child); },
        insertBefore(child) { return node.appendChild(child); },
        replaceChildren(...nodes) { node.children = []; for (const child of nodes) node.appendChild(child); },
        remove() { node.isConnected = false; },
        contains(other) { for (let at = other; at; at = at.parentElement) if (at === node) return true; return false; },
        matches(selector) { return matches(node, selector); },
        querySelectorAll(selector) {
            const out = [];
            const walk = (n) => { for (const child of n.children) { if (matches(child, selector)) out.push(child); walk(child); } };
            walk(node);
            return out;
        },
        querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
        removeEventListener(name, fn) { listeners[name] = (listeners[name] || []).filter((f) => f !== fn); },
        fire(name, event = {}) { for (const fn of [...(listeners[name] || [])]) fn({ type: name, target: node, preventDefault() {}, stopPropagation() {}, ...event }); },
        focus() { active = node; },
        blur() { if (active === node) active = null; },
        getBoundingClientRect: () => ({ left: 0, top: 0, right: 200, bottom: 20, width: 200, height: 20 }),
        get firstChild() { return node.children[0] || null; },
    };
    return node;
}

const windowListeners = {};
const documentListeners = {};
const wrapper = fakeElement("div");
const box = {
    console, Math, JSON, Number, String, Array, Object, Boolean, Set, Map, Promise,
    setTimeout: () => 0, clearTimeout: () => {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { for (const fn of [...(windowListeners[event.type] || [])]) fn(event); return true; },
    addEventListener: (name, fn) => { (windowListeners[name] ||= []).push(fn); },
    removeEventListener: (name, fn) => { windowListeners[name] = (windowListeners[name] || []).filter((f) => f !== fn); },
    document: {
        createElement: (tag) => fakeElement(tag),
        getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
        get activeElement() { return active; },
        addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
        removeEventListener: (name, fn) => { documentListeners[name] = (documentListeners[name] || []).filter((f) => f !== fn); },
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
for (const file of ["views/slider.js", "services/inspectGeometry.js", "services/inspectState.js",
                    "services/viewControls.js", "views/volumeDock.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}
const G = box.PlexoraInspectGeometry;
const INFO = { width: 2000, height: 1000,
               spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
                          z: { value: 2, unit: "µm", source: "metadata" } } };
const DIMS = G.dims(INFO, 40);

// -- a view state that records what is asked of it -------------------------------

const asked = [];
let state = { mode: "plane", z: 12, t: 0, layout: "single", slab: { z0: 5, z1: 15, op: "max", follow: true },
              volume: { show: { volume: true, surfaces: true } }, section: { x: 500, y: 250 },
              inspect: G.clean({}, DIMS, G.spacing(INFO)) };
const subscribers = new Set();
const vs = {
    depth: 40, frames: 1, color: false, info: INFO,
    enabled: () => true,
    isVolume: () => state.mode === "volume",
    get: () => JSON.parse(JSON.stringify(state)),
    set(patch) {
        asked.push(patch);
        state = { ...state, ...patch, slab: { ...state.slab, ...(patch.slab || {}) },
                  volume: { ...state.volume, ...(patch.volume || {}) },
                  section: { ...state.section, ...(patch.section || {}) },
                  inspect: G.clean(G.merge(state.inspect, patch.inspect || {}), DIMS, G.spacing(INFO)) };
        for (const fn of subscribers) fn(state);
        return state;
    },
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    zStep: () => ({ value: 2, unit: "µm" }),
};
let engaged = false;
const volume = { renderer: { covered: [0, 0, 2000, 1000], status: "ready", engaged: () => engaged, resolved: () => true } };
const cutter = new box.PlexoraInspectState(vs);
box.PlexoraInspect = cutter;
box.PlexoraViewerPrefs = {
    handles: () => ({ visible: true, opacity: 0.55 }),
    set(patch) { box.dispatchEvent(new box.CustomEvent("plexora:viewer-prefs-changed", { detail: patch })); },
};
const dock = box.PlexoraVolumeDock.attach({ viewState: vs, volume, cutter,
                                             extras: () => ({ sectionsSupported: true }) });
check("the dock is built for a z-stack's 3-D view", Boolean(dock?.root));
const rows = () => [...dock.rows.entries()].filter(([id, row]) => id !== "cut.tools" && !row.node.hidden)
    .map(([id]) => id);
check("in 2-D it is not there", dock.root.hidden === true);

vs.set({ mode: "volume" });
check("in 3-D, whole: the mode switch and how voxels are read", !dock.root.hidden
      && JSON.stringify(rows()) === JSON.stringify(["cut.mode", "cut.interpolation"]), rows().join());
check("...and undo and redo, disabled with nothing to undo",
      !dock.rows.get("cut.tools").node.hidden
      && dock.rows.get("cut.tools").node.querySelectorAll("button").some((b) => /Undo/.test(b.title) && b.disabled));

const modeButton = (mode) => dock.rows.get("cut.mode").node.querySelectorAll("button")
    .find((b) => b.getAttribute("aria-label") && b.getAttribute("aria-label").toLowerCase().startsWith(
        { full: "the whole", cutaway: "cutaway", section: "sections", slab: "slabs", crop: "crop" }[mode]));
asked.length = 0;
modeButton("crop").fire("click");
check("a mode button is one change of the cut", asked.length === 1 && asked[0].inspect?.mode === "crop");
check("Crop along Z: the axes, Z's range, the cut faces", JSON.stringify(rows())
      === JSON.stringify(["cut.mode", "cut.axes", "cut.z", "cut.face", "cut.interpolation", "cut.handleOpacity"]),
      rows().join());
const chip = (axis) => dock.rows.get("cut.axes").node.querySelectorAll("button")
    .find((b) => b.textContent === axis.toUpperCase());
chip("x").fire("click");
check("X added: exactly the X and Z rows", rows().filter((id) => /^cut\.[xyz]$/.test(id)).join() === "cut.x,cut.z");
chip("z").fire("click");
chip("y").fire("click");
check("X+Y: exactly the X and Y rows", rows().filter((id) => /^cut\.[xyz]$/.test(id)).join() === "cut.x,cut.y");
chip("x").fire("click");
chip("y").fire("click");
check("the last axis stays on", state.inspect.axes.y === true && !state.inspect.axes.x);
chip("x").fire("click");

const slider = (id) => dock.rows.get(id).slider;
check("a crop row is a range over the area the 3-D view draws",
      slider("cut.x").mode === "range" && slider("cut.x").min === 0 && slider("cut.x").max === 2000);
asked.length = 0;
slider("cut.x").handlers.onInput([200, 1200]);
check("dragging previews: nothing saved, the cut on screen moves",
      asked.length === 0 && cutter.previewing && cutter.resolved().box.x.join() === "200,1200");
slider("cut.x").handlers.onChange([200, 1200]);
check("let go: one change", asked.length === 1 && state.inspect.crop.x.join() === "200,1200" && !cutter.previewing);
check("its fields are in microns (the image states its pixel size)",
      slider("cut.x").unit === "µm" && slider("cut.x").formatValue(200) === "100.0", slider("cut.x").unit);
check("a Z range says how many planes and how thick",
      dock.rows.get("cut.z") && (() => { chip("z").fire("click"); const t = dock.rows.get("cut.z").node
          .querySelector(".volume-dock-note").textContent; chip("z").fire("click"); return /planes · .*µm$/.test(t); })());

modeButton("slab").fire("click");
check("Slab: how it combines and how thick, the surroundings",
      ["cut.combine", "cut.thickness", "cut.context"].every((id) => rows().includes(id))
      && !rows().includes("cut.face"), rows().join());
check("a slab's axis row moves its plane", slider("cut.x").mode === "single");
const thickness = dock.rows.get("cut.thickness").node.querySelector(".volume-dock-note").textContent;
check("the thickness in microns", /µm$/.test(thickness), thickness);
const combine = dock.rows.get("cut.combine").node.querySelector("select");
asked.length = 0;
combine.value = "mean";
combine.fire("change");
check("Combine sets how the slab's voxels combine", asked.length === 1 && state.inspect.slab.composite === "mean");
combine.value = "single";
combine.fire("change");
check("one plane: no thickness", !rows().includes("cut.thickness"));
modeButton("section").fire("click");
check("Section: the planes' opacity and the surroundings, no slab rows",
      rows().includes("cut.opacity") && rows().includes("cut.context") && !rows().includes("cut.combine"));
modeButton("crop").fire("click");
check("back to Crop, its range is where it was left", JSON.stringify(slider("cut.x").get()) === "[200,1200]",
      JSON.stringify(slider("cut.x").get()));
vs.set({ volume: { show: { volume: false, surfaces: true } } });
check("surfaces only: no cut faces, no reading of voxels",
      !rows().includes("cut.face") && !rows().includes("cut.interpolation"));
vs.set({ volume: { show: { volume: true, surfaces: true } } });

// Undo and redo.
const undo = dock.rows.get("cut.tools").node.querySelectorAll("button").find((b) => /Undo/.test(b.title));
const before = state.inspect.mode;
modeButton("cutaway").fire("click");
undo.fire("click");
check("Undo takes the last cut back", state.inspect.mode === before, state.inspect.mode);
engaged = true;
const key = (shiftKey) => documentListeners.keydown?.forEach((fn) => fn({ key: "z", metaKey: true, shiftKey,
    preventDefault() {}, stopPropagation() {} }));
key(true);
check("Shift+Cmd+Z redoes it, while the 3-D view is worked in", state.inspect.mode === "cutaway");
key(false);
check("Cmd+Z undoes it", state.inspect.mode === before);
engaged = false;
key(true);
check("not while the user works elsewhere", state.inspect.mode === before);

// No stated spacing: pixels, never invented microns.
vs.info = { width: 2000, height: 1000 };
modeButton("crop").fire("click");
check("with no stated spacing, pixels, never invented microns",
      slider("cut.x").unit === "px" && slider("cut.x").formatValue(200) === "200", slider("cut.x").unit);
vs.info = INFO;

// Folding.
dock.setFolded(true, { remember: false });
check("folded, only the head shows", dock.body.hidden && dock.root.classList.contains("is-folded"));
box.PlexoraVolumeDock.open();
check("open() unfolds it", !dock.body.hidden);

vs.set({ mode: "plane" });
check("back in 2-D the dock goes", dock.root.hidden);

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
