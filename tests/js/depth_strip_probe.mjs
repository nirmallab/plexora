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
for (const file of ["views/slider.js", "services/viewControls.js", "views/depthStrip.js",
                    "views/depthSettings.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}

// -- a view state that records what is asked of it -------------------------------

const asked = [];
let state = { mode: "plane", z: 10, t: 0, layout: "single",
              slab: { z0: 5, z1: 15, op: "max", follow: true, thickness: 11 }, volume: {} };
const subscribers = new Set();
const vs = {
    depth: 40, frames: 1, color: false,
    enabled: () => true,
    get: () => JSON.parse(JSON.stringify(state)),
    set(patch) {
        asked.push(patch);
        state = { ...state, ...patch, slab: { ...state.slab, ...(patch.slab || {}) },
                  volume: { ...state.volume, ...(patch.volume || {}) } };
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
vs.set({ mode: "volume", slab: { z0: 0, z1: 39, follow: false } });
const afterEnter = asked.length;
check("entering 3-D while stepping planes stops the stepper without touching the view state",
      stepping === 1 && intervals.size === 0 && afterEnter === 1 && !asked.some((p) => p.persist),
      JSON.stringify(asked));
check("in 3-D: no plane thumb, no hover mark, no Sections; the band (the clip range) is there",
      !shownIds().includes("thumb") && !shownIds().includes("hover") && !shownIds().includes("sections")
      && shownIds().includes("band") && shownIds().includes("rail"), shownIds().join());

// The rail in 3-D: a press outside the clip range does nothing.
vs.set({ slab: { z0: 10, z1: 20 } });
asked.length = 0;
const press = (clientY, extra = {}) => strip.rail.fire("pointerdown", { button: 0, clientY, ...extra });
press(100 + 400 * (35 / 39));
check("a press on the rail outside the clip range sets nothing in 3-D", asked.length === 0 && !strip.drag);
press(100 + 400 * (15 / 39));
box.dispatchEvent({ type: "pointermove", clientY: 100 + 400 * (18 / 39) });
check("a press inside it drags the clip range, the plane left alone",
      asked.length === 1 && asked[0].slab?.z0 === 13 && asked[0].slab?.z1 === 23 && asked[0].z === undefined,
      JSON.stringify(asked));
box.dispatchEvent({ type: "pointerup" });
asked.length = 0;
const handle = fakeElement("span");
handle.dataset.handle = "z1";
strip.rail.fire("pointerdown", { button: 0, clientY: 100 + 400 * (23 / 39), target: handle });
box.dispatchEvent({ type: "pointermove", clientY: 100 + 400 * (30 / 39) });
box.dispatchEvent({ type: "pointerup" });
check("its end resizes it, still without moving the plane",
      asked.length === 1 && asked[0].slab?.z1 === 30 && asked[0].z === undefined, JSON.stringify(asked));

// `]` in 3-D moves the clip range (viewState.step does), never a frozen plane.
asked.length = 0;
strip.onKey({ code: "BracketRight", shiftKey: false, altKey: false, preventDefault() {} });
check("] in 3-D asks for a step (the clip range moves)", asked.length === 1 && asked[0].step === 1);

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
check("the cog opens the settings for 3-D: composite, view, planes, rotation, the Depth card",
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
