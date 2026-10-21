/**
 * The cut panel (views/volumeDock.js): Cut and Crop, opened beside the strip.
 *
 * Driven through a small DOM, as a user would. Pinned: the panel comes out
 * only from a tool, in 3-D; Cut starts on X and Crop on X, Y and Z, and each
 * keeps its own axes when the other is used; the same controls for both, the
 * flips only beside a Cut's axes; every control is one change of the cut (one
 * undo entry); collapsing leaves the cut as it is, x goes back to the whole
 * stack; O and Show/Hide hide the cut and show it again with its settings
 * kept, the panel left out, never while typing; a control used while the cut
 * is hidden shows it again; surfaces alone hide every control about voxels;
 * undo by button and by Cmd/Ctrl+Z.
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
        replaceWith(other) {
            const parent = node.parentElement;
            if (!parent) return;
            parent.children[parent.children.indexOf(node)] = other;
            other.parentElement = parent;
        },
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
                    "services/viewControls.js", "views/volumeDock.js", "services/savedAreas.js"]) {
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
              volume: { show: { volume: true, surfaces: true } }, inspect: G.clean({}, DIMS) };
const subscribers = new Set();
const vs = {
    depth: 40, frames: 1, color: false, info: INFO,
    enabled: () => true,
    isVolume: () => state.mode === "volume",
    get: () => JSON.parse(JSON.stringify(state)),
    set(patch) {
        asked.push(patch);
        const volume = { ...state.volume, ...(patch.volume || {}) };
        volume.area = G.cleanArea(volume.area, DIMS);
        state = { ...state, ...patch, slab: { ...state.slab, ...(patch.slab || {}) }, volume,
                  inspect: G.clean(G.merge(state.inspect, patch.inspect || {}), { ...DIMS, area: volume.area }) };
        for (const fn of subscribers) fn(state);
        return state;
    },
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    zStep: () => ({ value: 2, unit: "µm" }),
};
let engaged = false;
const volume = { renderer: { covered: [0, 0, 2000, 1000], status: "ready", engaged: () => engaged, resolved: () => true,
                             canvas: { hidden: false } } };
const cutter = new box.PlexoraInspectState(vs);
box.PlexoraInspect = cutter;
let handlePrefs = { visible: true, opacity: 0.55 };
const prefSets = [];
box.PlexoraViewerPrefs = {
    handles: () => ({ ...handlePrefs }),
    set(patch) {
        prefSets.push(patch);
        handlePrefs = { ...handlePrefs, ...(patch.handles || {}) };
        box.dispatchEvent(new box.CustomEvent("plexora:viewer-prefs-changed", { detail: patch }));
    },
};
// The depth strip the panel opens beside (views/depthStrip.js): its root and
// its Cut and Crop buttons.
const strip = { root: fakeElement("div"),
                nodes: { "tool.cutaway": fakeElement("button"), "tool.crop": fakeElement("button") } };
check("no panel without a strip to open beside", box.PlexoraVolumeDock.attach({ viewState: vs, volume, cutter }) === null);
const dock = box.PlexoraVolumeDock.attach({ viewState: vs, volume, cutter, strip,
                                             extras: () => ({ sectionsSupported: true }) });
check("the panel is built inside the strip, to open from its edge", Boolean(dock?.root)
      && strip.root.children.includes(dock.root));
const isOpen = () => dock.root.classList.contains("is-open");
const shown = () => [...dock.controls.entries()].filter(([, c]) => !c.node.hidden).map(([id]) => id);
const PANEL = ["cut.axes", "cut.face", "cut.handles", "cut.reset", "cut.history", "cut.interpolation",
               "cut.handleOpacity", "cut.toggle"];
check("in 2-D it is not out, and cannot be tabbed into", !isOpen() && dock.root.inert === true);

vs.set({ mode: "volume" });
check("in 3-D over the whole stack it stays in", !isOpen() && !strip.root.classList.contains("has-cut-panel"));

// -- Cut ---------------------------------------------------------------------------
asked.length = 0;
box.PlexoraVolumeDock.pick("cutaway");
check("Cut pressed: one change turns the tool on, on X", asked.length === 1 && state.inspect.mode === "cutaway"
      && JSON.stringify(state.inspect.axes) === JSON.stringify({ x: true, y: false, z: false }),
      JSON.stringify(state.inspect.axes));
check("...and the panel slides out beside the strip, joined to it", isOpen() && dock.root.inert === false
      && strip.root.classList.contains("has-cut-panel") && dock.title.textContent === "Cut");
check("every control of a Cut", JSON.stringify(shown()) === JSON.stringify(PANEL), shown().join());
const axes = dock.controls.get("cut.axes").node;
const chip = (axis) => axes.querySelectorAll(".volume-dock-chip")[["x", "y", "z"].indexOf(axis)];
const flip = (axis) => axes.querySelectorAll(".volume-dock-flip")[["x", "y", "z"].indexOf(axis)];
check("a Cut's axes are a radio: X checked, Y and Z not", axes.getAttribute("role") === "radiogroup"
      && chip("x").getAttribute("role") === "radio" && chip("x").getAttribute("aria-checked") === "true"
      && chip("y").getAttribute("aria-checked") === "false" && chip("x").getAttribute("aria-pressed") === null);
check("a flip beside the axis cut; beside the others its place is held, idle",
      axes.classList.contains("has-flip") && !flip("x").hidden && !flip("x").disabled
      && flip("y").classList.contains("is-idle") && flip("y").disabled);
check("the flip says which side is kept, its full half on the far side", /keeping the far side/.test(flip("x").title)
      && flip("x").classList.contains("is-flipped"), flip("x").title);
asked.length = 0;
flip("x").fire("click");
check("a flip is one change: the near side kept", asked.length === 1 && state.inspect.cutaway.x.keep === "lo"
      && !flip("x").classList.contains("is-flipped") && /keeping the near side/.test(flip("x").title));
asked.length = 0;
chip("y").fire("click");
check("Y chosen: one change, the cut along Y alone, its flip live, X's idle", asked.length === 1
      && JSON.stringify(state.inspect.axes) === JSON.stringify({ x: false, y: true, z: false })
      && !flip("y").disabled && flip("x").disabled && /Cut along Y/.test(chip("y").title), chip("y").title);
asked.length = 0;
chip("y").fire("click");
check("the axis cut again is no change: a Cut is always along one", asked.length === 0
      && state.inspect.axes.y === true);
chip("x").fire("click");
check("back on X", JSON.stringify(state.inspect.axes) === JSON.stringify({ x: true, y: false, z: false }));

const action = (id) => dock.controls.get(id).node;
action("cut.face").fire("click");
check("Solid cut faces on, pressed", state.inspect.face === true
      && action("cut.face").getAttribute("aria-pressed") === "true");
action("cut.handles").fire("click");
check("the eye hides the handles (a viewer pref)", prefSets.at(-1)?.handles?.visible === false
      && action("cut.handles").getAttribute("aria-pressed") === "false" && /hidden/.test(action("cut.handles").title));
action("cut.handles").fire("click");
vs.set({ inspect: { cutaway: { x: { at: 300 } } } });
asked.length = 0;
action("cut.reset").fire("click");
check("Reset puts the cut back where it starts, the axes and the tool kept",
      asked.length === 1 && state.inspect.cutaway.x.at === 1000 && state.inspect.cutaway.x.keep === "hi"
      && state.inspect.axes.x && !state.inspect.axes.y && state.inspect.mode === "cutaway",
      JSON.stringify(state.inspect.cutaway.x));
check("Undo is live and says its keys", !action("cut.history").disabled
      && /Undo the last change to the cut \(Ctrl\+Z; Shift\+Ctrl\+Z redoes\)/.test(action("cut.history").title),
      action("cut.history").title);
action("cut.history").fire("click");
check("Undo takes the reset back", state.inspect.cutaway.x.at === 300 && state.inspect.cutaway.x.keep === "lo");
const voxels = dock.controls.get("cut.interpolation").node.querySelectorAll("button");
voxels[1].fire("click");
check("Voxels: blocks, across the panel", state.volume.interpolation === "nearest"
      && voxels[1].getAttribute("aria-checked") === "true");
voxels[0].fire("click");

// -- Crop: the same panel ------------------------------------------------------------
asked.length = 0;
box.PlexoraVolumeDock.pick("crop");
check("Crop pressed: the tool switches in one change, on X, Y and Z", asked.length === 1
      && state.inspect.mode === "crop" && state.inspect.axes.x && state.inspect.axes.y && state.inspect.axes.z,
      JSON.stringify(state.inspect.axes));
check("...in place: the same panel, its title, no flips", isOpen() && dock.title.textContent === "Crop"
      && JSON.stringify(shown()) === JSON.stringify(PANEL) && !axes.classList.contains("has-flip")
      && flip("x").hidden);
check("the Cut's axis is kept for it", JSON.stringify(state.inspect.axesBy.cutaway)
      === JSON.stringify({ x: true, y: false, z: false }), JSON.stringify(state.inspect.axesBy));
check("a Crop's axes are toggles", axes.getAttribute("role") === "group" && chip("x").getAttribute("role") === null
      && chip("x").getAttribute("aria-pressed") === "true");
chip("y").fire("click");
box.PlexoraVolumeDock.pick("cutaway");
check("back to Cut: its own axis and its flip, as it was left",
      JSON.stringify(state.inspect.axes) === JSON.stringify({ x: true, y: false, z: false })
      && state.inspect.cutaway.x.keep === "lo" && state.inspect.face === true);
box.PlexoraVolumeDock.pick("crop");
check("and Crop's: X and Z, as it was left", JSON.stringify(state.inspect.axes)
      === JSON.stringify({ x: true, y: false, z: true }), JSON.stringify(state.inspect.axes));

// -- collapsing, closing, the key ----------------------------------------------------
asked.length = 0;
box.PlexoraVolumeDock.pick("crop");
check("the tool's button again folds the panel in; the crop stays on", !isOpen() && asked.length === 0
      && state.inspect.mode === "crop" && !strip.root.classList.contains("has-cut-panel"));
box.PlexoraVolumeDock.pick("crop");
check("...and again brings it back", isOpen());
dock.collapseButton.fire("click");
check("› folds it in too, the crop on, focus back on the strip's Crop", !isOpen() && state.inspect.mode === "crop"
      && active === strip.nodes["tool.crop"]);
box.PlexoraVolumeDock.open();
dock.root.fire("keydown", { key: "Escape" });
check("Esc in the panel folds it in", !isOpen() && state.inspect.mode === "crop");
box.PlexoraVolumeDock.open();

const press = (key, extra = {}) => documentListeners.keydown?.forEach((fn) => fn({ key, preventDefault() {},
    stopPropagation() {}, ...extra }));
const toggle = action("cut.toggle");
check("Show/Hide under the handles' strength, its key in a key cap",
      dock.root.children.at(-1) === toggle && toggle.children[0]?.tagName === "KBD"
      && toggle.children[0].textContent === "O" && toggle.children[1]?.textContent === "Show/Hide"
      && toggle.getAttribute("aria-pressed") === "true" && /\(O\): the cut is shown/.test(toggle.title), toggle.title);
asked.length = 0;
press("o");
check("O hides the cut: the whole stack, settings kept, one change", state.inspect.mode === "full"
      && state.inspect.tool === "crop" && asked.length === 1);
check("...and the panel stays out, quietened, still the Crop's", isOpen() && dock.root.classList.contains("is-off")
      && dock.title.textContent === "Crop" && JSON.stringify(shown()) === JSON.stringify(PANEL)
      && strip.root.classList.contains("has-cut-panel"), shown().join());
check("Show/Hide says the cut is hidden", toggle.getAttribute("aria-pressed") === "false"
      && /the cut is hidden\. Show it again/.test(toggle.title), toggle.title);
press("o");
check("O again: the crop back, exactly as it was, the panel never moved",
      state.inspect.mode === "crop" && JSON.stringify(state.inspect.axes) === JSON.stringify({ x: true, y: false, z: true })
      && isOpen() && !dock.root.classList.contains("is-off"));
asked.length = 0;
toggle.fire("click");
check("Show/Hide clicked hides it too, the panel out", state.inspect.mode === "full" && asked.length === 1 && isOpen());
toggle.fire("click");
check("...and shows it again", state.inspect.mode === "crop" && isOpen());
press("o");
asked.length = 0;
chip("y").fire("click");
check("an axis chosen while the cut is hidden shows it again, in one change", asked.length === 1
      && state.inspect.mode === "crop" && state.inspect.axes.y === true && !dock.root.classList.contains("is-off"));
chip("y").fire("click");
vs.set({ inspect: { crop: { x: [400, 1600] } } });
press("o");
asked.length = 0;
action("cut.reset").fire("click");
check("Reset while hidden: the crop back where it starts, shown, in one change", asked.length === 1
      && state.inspect.mode === "crop" && JSON.stringify(state.inspect.crop.x) === JSON.stringify([0, 2000]),
      JSON.stringify(state.inspect.crop.x));
action("cut.history").fire("click");
check("...which Undo takes back, hidden again as it was", state.inspect.mode === "full"
      && JSON.stringify(state.inspect.crop.x) === JSON.stringify([400, 1600]) && isOpen());
asked.length = 0;
box.PlexoraVolumeDock.pick("crop");
check("the strip's Crop while hidden shows the crop, the panel out", asked.length === 1
      && state.inspect.mode === "crop" && isOpen());
press("o");
box.PlexoraVolumeDock.pick("cutaway");
check("...and Cut while hidden switches to a shown Cut", state.inspect.mode === "cutaway" && isOpen()
      && dock.title.textContent === "Cut" && axes.classList.contains("has-flip"));
box.PlexoraVolumeDock.pick("crop");
check("back to the crop as it was", JSON.stringify(state.inspect.axes) === JSON.stringify({ x: true, y: false, z: true })
      && JSON.stringify(state.inspect.crop.x) === JSON.stringify([400, 1600]), JSON.stringify(state.inspect.crop.x));
asked.length = 0;
press("o", { ctrlKey: true });
press("o", { repeat: true });
check("not with a modifier, nor held down", asked.length === 0);
const field = fakeElement("input");
active = field;
press("o");
check("never while typing", asked.length === 0 && state.inspect.mode === "crop");
active = null;
vs.set({ mode: "plane" });
asked.length = 0;
press("o");
check("never outside 3-D", asked.length === 0);
vs.set({ mode: "volume" });

asked.length = 0;
dock.closeButton.fire("click");
check("× closes the panel and goes back to the whole stack", !isOpen() && state.inspect.mode === "full"
      && asked.length === 1);
press("o");
check("O after ×: the crop back, the panel left closed", state.inspect.mode === "crop" && !isOpen());
box.PlexoraVolumeDock.pick("crop");
check("its button opens the panel again", isOpen());

// -- over surfaces alone, and undo by key ---------------------------------------------
vs.set({ volume: { show: { volume: false, surfaces: true } } });
check("surfaces only: no cut faces, no reading of voxels",
      !shown().includes("cut.face") && !shown().includes("cut.interpolation") && shown().includes("cut.axes"));
vs.set({ volume: { show: { volume: true, surfaces: true } } });

const before = state.inspect.axes.y;
chip("y").fire("click");
engaged = true;
const chordKey = (shiftKey) => press("z", { metaKey: true, shiftKey });
chordKey(false);
check("Cmd+Z undoes a cut while the 3-D view is worked in", state.inspect.axes.y === before);
chordKey(true);
check("Shift+Cmd+Z redoes it", state.inspect.axes.y === !before);
engaged = false;
chordKey(false);
check("not while the user works elsewhere", state.inspect.axes.y === !before);

// -- Crop to area, and the area undone with the cut -----------------------------------
box.PlexoraVolumeDock.pick("crop");
box.PlexoraVolumeDock.open();
vs.set({ inspect: { mode: "crop", axes: { x: true, y: true, z: true }, crop: { x: [200, 600], y: [100, 300] } } });
dock.paint();
check("a crop narrower than the image offers Crop to area", shown().includes("cut.toArea"), shown().join());
asked.length = 0;
dock.controls.get("cut.toArea").node.fire("click");
check("Crop to area: one change, the area the crop's x and y, the crop the area",
      asked.length === 1 && JSON.stringify(state.volume.area) === JSON.stringify({ x: 200, y: 100, width: 400, height: 200 })
      && JSON.stringify(state.inspect.crop.x) === JSON.stringify([200, 600])
      && JSON.stringify(state.inspect.crop.y) === JSON.stringify([100, 300]), JSON.stringify(asked));
dock.paint();
check("...and then it is not narrower: the button goes", !shown().includes("cut.toArea"));
cutter.undo();
check("one undo puts back the area and the crop", state.volume.area === null
      && JSON.stringify(state.inspect.crop.x) === JSON.stringify([200, 600]));
cutter.slideArea({ x: 0, y: 0, width: 500, height: 500 }, { final: false });
cutter.slideArea({ x: 100, y: 0, width: 500, height: 500 }, { final: false });
cutter.slideArea({ x: 150, y: 0, width: 500, height: 500 }, { final: true });
cutter.undo();
check("a slide is one undo entry, however many moves", state.volume.area === null, JSON.stringify(state.volume.area));
cutter.redo();
cutter.slideArea({ x: 300, y: 0, width: 500, height: 500 }, { final: true });
cutter.undo();
check("a slide after the last one ended is its own entry",
      JSON.stringify(state.volume.area) === JSON.stringify({ x: 150, y: 0, width: 500, height: 500 }),
      JSON.stringify(state.volume.area));

// -- saved areas ------------------------------------------------------------------------
check("no Save area on a page that keeps none", !shown().includes("area.save"));
volume.renderer.cameraState = () => ({ q: [0, 0, 0, 1], dist: 2, pan: [0.1, 0] });
volume.renderer.snapshot = () => ({ toDataURL: () => "data:image/jpeg;base64,AAAA" });
let restoredCamera = null;
volume.renderer.restoreCamera = (camera) => { restoredCamera = camera; return true; };
const savedAreas = box.PlexoraSavedAreas.attach({ viewState: vs, volume, cutter });
vs.set({ volume: { area: { x: 100, y: 100, width: 500, height: 500 } },
         inspect: { mode: "crop", axes: { x: true, y: false, z: false }, crop: { x: [200, 300] } } });
dock.paint();
check("Save area on a page that keeps them; no list before one is saved",
      shown().includes("area.save") && !shown().includes("area.list"), shown().join());
asked.length = 0;
dock.controls.get("area.save").node.fire("click");
const kept = state.volume.saved || [];
check("Save area: one change, the area, the cut and the camera under a name",
      asked.length === 1 && kept.length === 1 && kept[0].name === "Area 1"
      && JSON.stringify(kept[0].area) === JSON.stringify({ x: 100, y: 100, width: 500, height: 500 })
      && JSON.stringify(kept[0].inspect.crop.x) === "[200,300]" && kept[0].camera?.dist === 2, JSON.stringify(kept));
dock.paint();
const list = dock.controls.get("area.list").node;
const items = () => list.querySelectorAll(".volume-dock-area");
check("the list shows it, its thumbnail this page's only", shown().includes("area.list") && items().length === 1
      && items()[0].querySelector(".volume-dock-area-thumb").getAttribute("src") === "data:image/jpeg;base64,AAAA"
      && !JSON.stringify(state).includes("base64"));
vs.set({ volume: { area: { x: 0, y: 0, width: 200, height: 200 } }, inspect: { mode: "full" } });
asked.length = 0;
items()[0].querySelector(".volume-dock-area-open").fire("click");
check("a click goes back: area, cut and camera in one change",
      asked.length === 1 && JSON.stringify(state.volume.area) === JSON.stringify({ x: 100, y: 100, width: 500, height: 500 })
      && state.inspect.mode === "crop" && JSON.stringify(state.inspect.crop.x) === "[200,300]"
      && restoredCamera?.dist === 2, JSON.stringify(asked));
items()[0].querySelector(".volume-dock-area-open").fire("dblclick");
const renaming = list.querySelector(".volume-dock-area-rename");
renaming.value = "Germinal centre";
renaming.fire("keydown", { key: "Enter" });
check("a double-click renames it", state.volume.saved[0].name === "Germinal centre");
dock.paint();
items()[0].querySelector(".volume-dock-area-remove").fire("click");
dock.paint();
check("× forgets it, and the list goes", (state.volume.saved || []).length === 0 && !shown().includes("area.list")
      && savedAreas.thumb(kept[0].id) === null);

vs.set({ mode: "plane" });
check("back in 2-D the panel goes", !isOpen() && dock.root.inert === true);

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}

console.log(`\nall ${checks} checks passed`);
