/**
 * The depth strip and its cog popover follow the mode (views/depthStrip.js,
 * views/depthSettings.js, services/viewControls.js).
 *
 * Driven through a small DOM, as a user would: pinned here are the things the
 * strip used to get wrong in 3-D -- a rail that set a frozen plane, a band that
 * could not be dragged, a Play that stepped planes nobody could see -- and the
 * popover's manners: it shows the mode's settings, follows a mode change
 * without closing, and gives focus back on Escape. Painting never changes the
 * view state.
 *
 * Run directly:  node tests/js/depth_strip_probe.mjs
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
        let rest = one;
        const notDisabled = rest.endsWith(":not([disabled])");
        if (notDisabled) rest = rest.slice(0, -":not([disabled])".length);
        if (notDisabled && node.disabled) return false;
        const attr = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(rest);
        if (attr) return attr[2] === undefined ? node.getAttribute(attr[1]) !== null
            : node.getAttribute(attr[1]) === attr[2];
        if (rest.startsWith(".")) return node.classList.contains(rest.slice(1));
        return node.tagName === rest.toUpperCase();
    });
}

function fakeElement(tag) {
    const attributes = {};
    const classes = new Set();
    const listeners = {};
    const node = {
        tagName: String(tag).toUpperCase(),
        children: [],
        parentElement: null,
        style: { setProperty(name, value) { this[name] = value; }, removeProperty(name) { delete this[name]; } },
        hidden: false,
        disabled: false,
        value: "",
        checked: false,
        textContent: "",
        isConnected: true,
        dataset: {},
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
            if (name.startsWith("data-")) node.dataset[name.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase())] = String(value);
            if (name === "id") node.id = String(value);
        },
        getAttribute(name) { return attributes[name] ?? null; },
        appendChild(child) { child.parentElement = node; node.children.push(child); return child; },
        append(...nodes) { for (const child of nodes) node.appendChild(child); },
        replaceChildren(...nodes) { node.children = []; for (const child of nodes) node.appendChild(child); },
        remove() { node.isConnected = false; if (node.parentElement) node.parentElement.children = node.parentElement.children.filter((c) => c !== node); },
        contains(other) { for (let at = other; at; at = at.parentElement) if (at === node) return true; return false; },
        closest(selector) { for (let at = node; at; at = at.parentElement) if (matches(at, selector)) return at; return null; },
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
        getBoundingClientRect: () => ({ left: 900, top: 100, right: 936, bottom: 500, width: 36, height: 400 }),
        offsetWidth: 260,
        offsetHeight: 200,
    };
    return node;
}

const windowListeners = {};
const documentListeners = {};
const wrapper = fakeElement("div");
const documentElement = fakeElement("html");
const timers = [];
const intervals = new Map();
let nextInterval = 1;
const box = {
    console,
    innerWidth: 1200,
    innerHeight: 800,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    setInterval: (fn) => { const id = nextInterval++; intervals.set(id, fn); return id; },
    clearInterval: (id) => intervals.delete(id),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { for (const fn of [...(windowListeners[event.type] || [])]) fn(event); return true; },
    addEventListener: (name, fn) => { (windowListeners[name] ||= []).push(fn); },
    removeEventListener: (name, fn) => { windowListeners[name] = (windowListeners[name] || []).filter((f) => f !== fn); },
    document: {
        createElement: (tag) => fakeElement(tag),
        getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
        querySelector: () => null,
        documentElement,
        get activeElement() { return active; },
        addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
        removeEventListener: (name, fn) => { documentListeners[name] = (documentListeners[name] || []).filter((f) => f !== fn); },
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
for (const file of ["views/slider.js", "services/inspectGeometry.js", "services/inspectState.js",
                    "services/viewControls.js", "views/depthStrip.js", "views/depthSettings.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}
const G = box.PlexoraInspectGeometry;
const DIMS = G.dims({ width: 400, height: 300 }, 40);

// -- a view state that records what is asked of it -------------------------------

const asked = [];
let state = { mode: "plane", z: 10, t: 0, layout: "single",
              slab: { z0: 5, z1: 15, op: "max", follow: true, thickness: 11 }, volume: {},
              section: { x: 200, y: 150 }, inspect: G.clean({}, DIMS) };
const subscribers = new Set();
const vs = {
    depth: 40, frames: 1, color: false, info: { width: 400, height: 300 },
    enabled: () => true,
    get: () => JSON.parse(JSON.stringify(state)),
    set(patch) {
        asked.push(patch);
        state = { ...state, ...patch, slab: { ...state.slab, ...(patch.slab || {}) },
                  volume: { ...state.volume, ...(patch.volume || {}) },
                  inspect: G.clean(G.merge(state.inspect, patch.inspect || {}), DIMS) };
        for (const fn of subscribers) fn(state);
        return state;
    },
    step(dz) { asked.push({ step: dz }); return state; },
    stepTime() {},
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
    label: () => "label",
    spec: () => "a", appliedSpec: () => "a",
    persist() { asked.push({ persist: true }); },
    zStep: () => ({ value: 2, unit: "µm" }),
};

const rotations = [];
const volume = {
    rotating: false,
    renderer: { preset() {} },
    rotate(on, options) {
        rotations.push({ on, options });
        volume.rotating = Boolean(on);
        box.dispatchEvent(new box.CustomEvent("plexora:volume-rotate", { detail: { on: volume.rotating } }));
        return volume.rotating;
    },
};
const prefSets = [];
box.PlexoraViewerPrefs = {
    rotate: () => ({ speed: 45, axis: "z", direction: -1 }),
    set(patch, options) {
        prefSets.push(patch);
        // As the real one does: every change is announced.
        box.dispatchEvent(new box.CustomEvent("plexora:viewer-prefs-changed", { detail: patch }));
    },
};

box.PlexoraInspect = new box.PlexoraInspectState(vs);
const strip = new box.PlexoraDepthStrip(vs, { volume, sections: {}, openCard: () => {} });
strip.mount();
const shownIds = () => Object.entries(strip.nodes).filter(([, node]) => !node.hidden).map(([id]) => id);

check("one plane: the plane rail, its thumb and hover mark, Sections",
      ["rail", "thumb", "hover", "sections", "play", "cog"].every((id) => shownIds().includes(id))
      && !shownIds().includes("band"), shownIds().join());

// Stepping planes, then 3-D: the stepper stops, and nobody's plane is saved.
strip.play();
const stepping = intervals.size;
asked.length = 0;
vs.set({ mode: "volume" });
const afterEnter = asked.length;
check("entering 3-D while stepping planes stops the stepper without touching the view state",
      stepping === 1 && intervals.size === 0 && afterEnter === 1 && !asked.some((p) => p.persist),
      JSON.stringify(asked));
check("in 3-D, whole: no plane thumb, no hover mark, no Sections; the band (every plane) is there",
      !shownIds().includes("thumb") && !shownIds().includes("hover") && !shownIds().includes("sections")
      && shownIds().includes("band") && shownIds().includes("rail") && strip.readoutBottom.textContent === "all",
      shownIds().join());

const press = (clientY, extra = {}) => strip.rail.fire("pointerdown", { button: 0, clientY, ...extra });
const at = (z) => 100 + 400 * (z / 39);
const handle = (name) => { const node = fakeElement("span"); node.dataset.handle = name; return node; };
// The whole stack's band end: dragged, the stack is cropped along Z.
asked.length = 0;
press(at(39), { target: handle("z1") });
box.dispatchEvent({ type: "pointermove", clientY: at(30) });
check("while the hand is on it, nothing is saved: the cut is previewed",
      asked.length === 0 && box.PlexoraInspect.previewing && strip.band.style.height !== "100%");
box.dispatchEvent({ type: "pointerup" });
check("let go, the whole stack's end makes one Z crop, the plane left alone",
      asked.length === 1 && asked[0].inspect?.mode === "crop" && asked[0].inspect.crop.z.join() === "0,30"
      && asked[0].inspect.axes.x === false && asked[0].z === undefined, JSON.stringify(asked));

// The rail in 3-D: a press outside the planes drawn does nothing.
vs.set({ inspect: { crop: { z: [10, 20] } } });
asked.length = 0;
press(at(35));
check("a press on the rail outside the planes drawn sets nothing in 3-D", asked.length === 0 && !strip.drag);
press(at(15));
box.dispatchEvent({ type: "pointermove", clientY: at(18) });
box.dispatchEvent({ type: "pointerup" });
check("a press inside drags the Z crop whole, the plane left alone",
      asked.length === 1 && asked[0].inspect?.crop?.z?.join() === "13,23" && asked[0].z === undefined,
      JSON.stringify(asked));
asked.length = 0;
press(at(23), { target: handle("z1") });
box.dispatchEvent({ type: "pointermove", clientY: at(30) });
box.dispatchEvent({ type: "pointerup" });
check("its end resizes it, still without moving the plane",
      asked.length === 1 && asked[0].inspect?.crop?.z?.join() === "13,30" && asked[0].z === undefined,
      JSON.stringify(asked));
check("the readout says the planes kept", strip.readoutBottom.textContent === "13–30");
check("one gesture, one undo", box.PlexoraInspect.undo() && vs.get().inspect.crop.z.join() === "13,23");

// A Z section: the thumb is its plane; the rail picks it.
vs.set({ inspect: { mode: "section" } });
check("a Z section: the thumb and hover mark, no band", shownIds().includes("thumb")
      && shownIds().includes("hover") && !shownIds().includes("band"), shownIds().join());
asked.length = 0;
press(at(25));
box.dispatchEvent({ type: "pointermove", clientY: at(27) });
box.dispatchEvent({ type: "pointerup" });
check("a press and drag move the Z section (the plane), as one change",
      asked.length === 1 && asked[0].z === 27 && !asked[0].inspect, JSON.stringify(asked));
// A Z cutaway: one end moves.
vs.set({ inspect: { mode: "cutaway", cutaway: { z: { at: 20, keep: "hi" } } } });
check("a Z cutaway keeping the deep side: only its top end is a handle",
      !strip.handleTop.hidden && strip.handleBottom.hidden && shownIds().includes("band"));
asked.length = 0;
press(at(20), { target: handle("z0") });
box.dispatchEvent({ type: "pointermove", clientY: at(8) });
box.dispatchEvent({ type: "pointerup" });
check("dragging it moves the cut", asked.length === 1 && asked[0].inspect?.cutaway?.z?.at === 8, JSON.stringify(asked));
// Only X cut: nothing along Z for the rail.
vs.set({ inspect: { mode: "section", axes: { x: true, z: false } } });
asked.length = 0;
press(at(10));
check("an X section only: the rail is not the cut's, a press does nothing",
      asked.length === 0 && !shownIds().includes("band") && !shownIds().includes("thumb"));
vs.set({ inspect: { mode: "crop", axes: { x: false, z: true }, crop: { z: [10, 20] } } });

// `]` in 3-D moves what is cut along Z (viewState.step does), never a frozen plane.
asked.length = 0;
strip.onKey({ code: "BracketRight", shiftKey: false, altKey: false, preventDefault() {} });
check("] in 3-D asks for a step (the cut along Z moves)", asked.length === 1 && asked[0].step === 1);

// Play turns the stack.
asked.length = 0;
strip.playButton.fire("click");
check("Play in 3-D turns the stack at the speed and about the axis of the settings",
      rotations.at(-1)?.on === true && rotations.at(-1).options.speed === 45
      && rotations.at(-1).options.axis === "z" && intervals.size === 0 && asked.length === 0);
check("...and says so: Pause, pressed, 'Stop rotating'",
      strip.playButton.getAttribute("aria-pressed") === "true"
      && strip.playButton.getAttribute("aria-label") === "Stop rotating"
      && strip.playButton._glyph === "pause");
// A press on the 3-D view stops it (the renderer announces it).
volume.rotating = false;
box.dispatchEvent(new box.CustomEvent("plexora:volume-rotate", { detail: { on: false } }));
check("a turn stopped by the view (a press, Esc) gives Play back its play glyph",
      strip.playButton.getAttribute("aria-pressed") === "false" && strip.playButton._glyph === "play"
      && /Rotate the stack/.test(strip.playButton.getAttribute("aria-label")));

// -- the cog popover -------------------------------------------------------------------

strip.cog.fire("click");
const popover = strip.settings;
const rowIds = () => [...popover.el.querySelectorAll("[data-control]")].map((n) => n.getAttribute("data-control"));
check("the cog opens the settings for 3-D: composite, view, the cut, rotation, the Depth card",
      popover.isOpen() && ["volume.composite", "volume.view", "volume.clip", "rotate.speed", "rotate.axis",
                           "rotate.direction", "card"].every((id) => rowIds().includes(id))
      && !rowIds().some((id) => id.startsWith("projection.")), rowIds().join());
check("the cog says the popover is open", strip.cog.getAttribute("aria-expanded") === "true");
box.dispatchEvent(new box.CustomEvent("plexora:viewer-prefs-changed", {}));
check("opening it, and a prefs change while it is open, writes no preference (no echo loop)",
      prefSets.length === 0, JSON.stringify(prefSets));
check("it sits left of the strip, inside the window",
      parseFloat(popover.el.style.left) === 900 - 8 - 260 && parseFloat(popover.el.style.top) >= 8);
asked.length = 0;
vs.set({ mode: "slab" });
check("a mode change rebuilds its rows in place, open",
      popover.isOpen() && rowIds().includes("projection.op") && !rowIds().includes("rotate.speed"),
      rowIds().join());
const select = popover.el.querySelector("[data-control=\"projection.op\"]").querySelector("select");
check("...with the state's values in them", select.value === "max");
select.value = "mean";
select.fire("change");
check("a setting changed there is a view-state change like any other",
      asked.some((p) => p.mode === "slab" && p.slab?.op === "mean"));
const escape = documentListeners.keydown?.at(-1);
escape?.({ key: "Escape", stopPropagation() {}, preventDefault() {} });
check("Escape closes it and gives focus back to the cog",
      !popover.isOpen() && active === strip.cog && strip.cog.getAttribute("aria-expanded") === "false");

// Painting is reading.
asked.length = 0;
for (let i = 0; i < 5; i += 1) strip.paint();
check("painting never changes the view state", asked.length === 0);

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
