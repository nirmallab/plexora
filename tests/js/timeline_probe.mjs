/**
 * The timeline: a time series' frames in a movable pill (views/timeline.js).
 *
 * Driven through a small DOM and a hand-wound clock. Pinned here: it exists
 * only on a series; it is docked and movable; Play asks for the next frame
 * only once the one asked for is on screen and the rate allows it -- so no
 * frame is skipped, and playback slows (and says so) rather than piling up
 * requests -- loops or stops at the end, and tells the prefetch its rate; a
 * hand on the slider or a key stops it; it steps aside for Video Studio's own
 * timeline; and time since the first frame reads in the series' own units.
 *
 * Run directly:  node tests/js/timeline_probe.mjs
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
        if (one.startsWith(".")) return node.classList.contains(one.slice(1));
        return node.tagName === one.toUpperCase();
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
        _text: "",
        get textContent() { return node._text; },
        set textContent(value) { node._text = String(value); },
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
        appendChild(child) {
            if (child.parentElement) child.parentElement.children = child.parentElement.children.filter((c) => c !== child);
            child.parentElement = node; node.children.push(child); return child;
        },
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
        fire(name, event = {}) { for (const fn of [...(listeners[name] || [])]) fn({ type: name, target: node, button: 0, preventDefault() {}, stopPropagation() {}, ...event }); },
        focus() { active = node; },
        // The track: 500 px wide from x = 100.
        getBoundingClientRect: () => ({ left: 100, top: 100, right: 600, bottom: 106, width: 500, height: 6 }),
    };
    return node;
}

const windowListeners = {};
const documentListeners = {};
let clock = 1000;
const frames = [];
const box = {
    console,
    performance: { now: () => clock },
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    cancelAnimationFrame: (id) => { frames[id - 1] = null; },
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { for (const fn of [...(windowListeners[event.type] || [])]) fn(event); return true; },
    addEventListener: (name, fn) => { (windowListeners[name] ||= []).push(fn); },
    removeEventListener: (name, fn) => { windowListeners[name] = (windowListeners[name] || []).filter((f) => f !== fn); },
    document: {
        createElement: (tag) => fakeElement(tag),
        querySelector: () => null,
        get activeElement() { return active; },
        addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
        removeEventListener: (name, fn) => { documentListeners[name] = (documentListeners[name] || []).filter((f) => f !== fn); },
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
runInContext(readFileSync(join(JS, "views/timeline.js"), "utf8"), context);
const T = box.PlexoraTimeline;

/** Run the animation frames queued now (the player's ticks). */
function tick(ms = 0) {
    clock += ms;
    const queued = frames.splice(0);
    for (const fn of queued) fn?.();
}

function keyDown(code, extra = {}) {
    const event = { code, key: code, defaultPrevented: false, shiftKey: false, altKey: false,
                    metaKey: false, ctrlKey: false, repeat: false, preventDefault() { this.defaultPrevented = true; },
                    ...extra };
    for (const fn of documentListeners.keydown || []) fn(event);
    return event;
}

// -- the canvas layout, the prefs, the prefetch ----------------------------------

const placed = [];
const bands = new Map();
const movables = [];
box.PlexoraCanvasLayout = {
    get: () => ({ bands }),
    place(element, options) { placed.push({ element, options }); },
    movable(element, options) {
        const reset = fakeElement("button");
        const record = { element, options, reset, disposed: false, dispose() { this.disposed = true; } };
        element.appendChild(reset);
        movables.push(record);
        return record;
    },
};
function layoutChanged() {
    box.dispatchEvent(new box.CustomEvent("plexora:canvas-layout", { detail: {} }));
}

let playback = { fps: 10, loop: true };
const prefSets = [];
box.PlexoraViewerPrefs = {
    playback: () => ({ ...playback }),
    set(patch) {
        prefSets.push(patch);
        if (patch.playback) playback = { ...playback, ...patch.playback };
        box.dispatchEvent(new box.CustomEvent("plexora:viewer-prefs-changed", { detail: patch }));
    },
};
const leads = [];
const prefetch = { lead(playing) { leads.push(playing); } };
let activeTool = null;
box.PlexoraToolLoader = { activeTool: () => activeTool };

// -- a view state whose frames land when the test says ----------------------------

function series({ frames: count = 100, depth = 1, step = { value: 30, unit: "s" } } = {}) {
    let state = { mode: "plane", z: 0, t: 0 };
    let applied = "";
    const subscribers = new Set();
    const vs = {
        depth, frames: count, info: { spacing: { t: step } },
        asked: [], ended: 0, persisted: 0, steps: [],
        enabled: () => true,
        get: () => ({ ...state }),
        spec: (s = state) => (s.t ? `t${s.t}` : ""),
        appliedSpec: () => applied,
        busy: () => false,
        set(patch, options = {}) {
            vs.asked.push({ patch, options });
            state = { ...state, ...patch };
            for (const fn of subscribers) fn(state);
            return state;
        },
        stepTime(dt) { vs.steps.push(dt); return vs.set({ t: Math.min(count - 1, Math.max(0, state.t + dt)) }); },
        subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
        endScrub() { vs.ended += 1; },
        persist() { vs.persisted += 1; },
        /** The frame asked for is on screen now. */
        land() { applied = vs.spec(); },
    };
    return vs;
}

// -- only on a series ----------------------------------------------------------------

check("an image with one frame gets no timeline",
      T.attach(series({ frames: 1 }), { prefetch }) === null && placed.length === 0);

const vs = series();
const timeline = T.attach(vs, { prefetch });
check("a series gets one, in the bottom-centre dock above the bookmark tray",
      timeline && placed.length === 1 && placed[0].options.corner === "bottom-center"
      && placed[0].options.order > 20, JSON.stringify(placed[0]?.options));
check("...movable, remembered as \"timeline\", its x at the end of the pill",
      movables.length === 1 && movables[0].options.id === "timeline"
      && timeline.root.children.at(-1) === movables[0].reset);
check("it reads frame 1 of 100, time 0 s, and says so to assistive tech",
      timeline.frameText.textContent === "1 / 100" && timeline.timeText.textContent === "0 s"
      && timeline.slider.getAttribute("aria-valuetext") === "Frame 1 of 100, 0 s"
      && timeline.slider.getAttribute("aria-valuemax") === "100");
vs.set({ t: 11 });
check("frame 12 of a 30 s series is 5:30 in", timeline.frameText.textContent === "12 / 100"
      && timeline.timeText.textContent === "5:30", timeline.timeText.textContent);
check("the chip says the rate asked for", timeline.rateButton.textContent === "10 fps");

// -- elapsed time ------------------------------------------------------------------

check("time in the series' own units: ms, s, minutes, hours, and what it does not know as given",
      T.elapsed(5, { value: 0.1, unit: "s" }) === "500 ms"
      && T.elapsed(3, { value: 2.5, unit: "second" }) === "7.5 s"
      && T.elapsed(3, { value: 2, unit: "min" }) === "6:00"
      && T.elapsed(2, { value: 1, unit: "hour" }) === "2:00:00"
      && T.elapsed(4, { value: 3, unit: "frames" }) === "12 frames"
      && T.elapsed(4, null) === "" && T.elapsed(4, { value: 0 }) === "",
      [T.elapsed(5, { value: 0.1, unit: "s" }), T.elapsed(3, { value: 2, unit: "min" })].join());

// -- Play shows every frame ----------------------------------------------------------

vs.set({ t: 0 });
vs.land();
vs.asked.length = 0;
timeline.toggle();
check("Play: the button says Pause, and the prefetch is told the rate and the loop",
      timeline.playing && timeline.playButton.getAttribute("aria-pressed") === "true"
      && leads.at(-1)?.rate === 10 && leads.at(-1)?.loop === true);
tick(50);
check("not before the rate allows", vs.asked.length === 0);
tick(60);
check("the next frame once it does, on a clock (steady), never saved as a choice",
      vs.asked.length === 1 && vs.asked[0].patch.t === 1 && vs.asked[0].options.steady === true
      && vs.asked[0].options.save === false, JSON.stringify(vs.asked));
tick(400);
tick(400);
check("while that frame is still on its way, nothing more is asked for: no frame is skipped",
      vs.asked.length === 1);
check("...and the thumb says it is waiting", timeline.root.classList.contains("is-waiting"));
vs.land();
tick(0);
check("it lands: the next one is asked for at once", vs.asked.length === 2 && vs.asked[1].patch.t === 2,
      JSON.stringify(vs.asked.map((a) => a.patch)));
check("...and the thumb is solid again", !timeline.root.classList.contains("is-waiting"));
// Slow frames for a while: the chip says what it is achieving.
for (let i = 0; i < 6; i += 1) {
    tick(250);
    vs.land();
    tick(0);
}
check("frames arriving at 4 a second: the chip says so, amber",
      /^\d of 10 fps$/.test(timeline.rateButton.textContent) && timeline.rateButton.classList.contains("is-slow"),
      timeline.rateButton.textContent);
// Back to keeping up.
for (let i = 0; i < 12; i += 1) {
    vs.land();
    tick(100);
}
check("keeping up again: the chip goes back to the rate", timeline.rateButton.textContent === "10 fps",
      timeline.rateButton.textContent);

// -- the end of the series ------------------------------------------------------------

vs.set({ t: 99 });
vs.land();
vs.asked.length = 0;
tick(200);
check("Loop on: after the last frame, the first", vs.asked.at(-1)?.patch.t === 0);
timeline.setPlayback({ loop: false });
check("Loop is a preference", prefSets.at(-1)?.playback?.loop === false
      && leads.at(-1)?.loop === false);
vs.set({ t: 99 });
vs.land();
tick(200);
check("Loop off: it stops on the last frame, and keeps it",
      !timeline.playing && vs.get().t === 99 && leads.at(-1) === null && vs.persisted >= 1);
vs.asked.length = 0;
timeline.toggle();
check("Play again from the last frame starts at the first", vs.asked[0]?.patch.t === 0
      && timeline.playing);
timeline.toggle();
timeline.setPlayback({ loop: true });

// -- the rate --------------------------------------------------------------------

timeline.rateButton.fire("click");
check("the chip opens the rates", !timeline.ratePanel.hidden
      && timeline.rateButton.getAttribute("aria-expanded") === "true");
timeline.rateChoices.find((b) => b.dataset.fps === "20").fire("click");
check("a rate is chosen, kept, shown, and the popover closes",
      prefSets.at(-1)?.playback?.fps === 20 && timeline.rateButton.textContent === "20 fps"
      && timeline.ratePanel.hidden
      && timeline.rateChoices.find((b) => b.dataset.fps === "20").getAttribute("aria-pressed") === "true");

// -- a hand on it ----------------------------------------------------------------

timeline.toggle();
vs.ended = 0;
timeline.slider.fire("pointerdown", { clientX: 350, pointerId: 1 });
check("a press on the slider stops Play and goes to the frame under it",
      !timeline.playing && vs.get().t === 50 && timeline.root.classList.contains("is-scrubbing"),
      String(vs.get().t));
for (const fn of windowListeners.pointermove || []) fn({ clientX: 600, pointerId: 1 });
check("dragged to the end, the last frame", vs.get().t === 99);
for (const fn of [...(windowListeners.pointerup || [])]) fn({ pointerId: 1 });
check("let go: the scrub ends, once", vs.ended === 1 && !timeline.root.classList.contains("is-scrubbing"));
const hovers = [];
box.addEventListener("plexora:frame-hover", (event) => hovers.push(event.detail.t));
timeline.slider.fire("pointermove", { clientX: 225, pointerType: "mouse" });
check("resting on the slider names the frame and its time, and starts it loading",
      timeline.hoverTip.textContent === "26 · 12:30" && hovers.at(-1) === 25, timeline.hoverTip.textContent);
timeline.slider.fire("pointerleave");
check("...and lets it go when the pointer leaves", hovers.at(-1) === null);

// -- keys -----------------------------------------------------------------------

timeline.toggle();
vs.steps.length = 0;
keyDown("Period");
check("`.` stops Play and steps a frame", !timeline.playing && vs.steps.join() === "1");
for (const fn of documentListeners.keyup || []) fn({ code: "Period" });
keyDown("Comma");
check("`,` steps back", vs.steps.join() === "1,-1");
keyDown("Space", { shiftKey: true });
check("Shift+Space plays", timeline.playing);
keyDown("Space", { shiftKey: true });
check("...and pauses", !timeline.playing);
keyDown("Space");
check("Space alone is not ours", !timeline.playing);
activeTool = "roi";
keyDown("Space", { shiftKey: true });
check("not while the ROI tool, whose Space pans, is open", !timeline.playing);
activeTool = null;

// -- Video Studio -----------------------------------------------------------------

timeline.toggle();
bands.set("timeline", { edge: "bottom", size: 168 });
layoutChanged();
check("Video Studio's timeline arrives: this one steps aside and stops",
      timeline.root.hidden && !timeline.playing);
keyDown("Space", { shiftKey: true });
check("...and its keys are not ours meanwhile", !timeline.playing);
bands.delete("timeline");
layoutChanged();
check("it goes: this one is back", !timeline.root.hidden);

// -- the loaded stretch --------------------------------------------------------------

const loadedItem = { getFullyLoaded: () => true };
const loadingItem = { getFullyLoaded: () => false };
const standby = new Map([
    ["t11", { pending: 0, pairs: new Map([[0, [{ item: loadedItem }]]]) }],
    ["t12", { pending: 0, pairs: new Map([[0, [{ item: loadedItem }]]]) }],
    ["t13", { pending: 0, pairs: new Map([[0, [{ item: loadingItem }]]]) }],
    ["t14", { pending: 0, pairs: new Map([[0, [{ item: loadedItem }]]]) }],
]);
timeline.viewerManager = { standby };
vs.set({ t: 10 });
check("the frames loaded ahead, in a row from the thumb", timeline.readyAhead() === 2,
      String(timeline.readyAhead()));

// -- going away ------------------------------------------------------------------

timeline.destroy();
check("destroyed: the pill and its movable go, its keys with them",
      timeline.root === null && movables[0].disposed && (documentListeners.keydown || []).length === 0);

if (failures.length) {
    console.error(`\n${failures.length} of ${checks} checks failed:\n  ${failures.join("\n  ")}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
