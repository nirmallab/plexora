/**
 * How the 3-D viewer moves: plane fades, the 3-D view's coast, pane sizes.
 *
 * Each module is run as the classic script the page loads, in a sandbox with
 * a clock this probe owns, so the properties a user FEELS can be pinned as
 * numbers:
 *
 *   viewerPrefs.js    motion off means off (and so does the system's
 *                     reduced-motion setting); a drag saves once, merged.
 *   planeMotion.js    a fade is held from the picture on screen, blends into a
 *                     fade still running instead of jumping back, follows the
 *                     pace of the steps, ends at once on a pan, and never
 *                     happens with motion off; the prefetch warms the plane
 *                     the hand is heading to first.
 *   volumeRenderer.js a release coasts as far as it was fast, the same at 60
 *                     and 120 frames a second, never from a hand that stopped
 *                     first; a press stops it dead; presets turn the short
 *                     way round; with motion off nothing coasts or eases.
 *   sectionPanes.js   a pane is never bigger than its stack at the plane's
 *                     scale, honours the size chosen, and a drag saves once.
 *
 * Run directly:  node tests/js/viewer_motion_probe.mjs
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
const near = (a, b, tolerance = 1e-9) => Math.abs(a - b) <= tolerance;

// -- a small DOM ------------------------------------------------------------

function fakeElement(tag) {
    const attributes = {};
    const classes = new Set();
    const listeners = {};
    const element = {
        tagName: String(tag).toUpperCase(),
        children: [],
        style: { setProperty(name, value) { this[name] = value; } },
        hidden: false,
        width: 0,
        height: 0,
        isConnected: true,
        listeners,
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
        get className() { return [...classes].join(" "); },
        classList: {
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
            toggle: (name, on) => { const want = on === undefined ? !classes.has(name) : on; if (want) classes.add(name); else classes.delete(name); return want; },
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) { attributes[name] = String(value); },
        getAttribute(name) { return attributes[name] ?? null; },
        appendChild(child) { element.children.push(child); return child; },
        append(...nodes) { element.children.push(...nodes); },
        remove() { element.isConnected = false; },
        after(node) { element.nextSibling = node; },
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
        removeEventListener(name, fn) { listeners[name] = (listeners[name] || []).filter((f) => f !== fn); },
        fire(name, event = {}) { for (const fn of listeners[name] || []) fn({ type: name, preventDefault() {}, stopPropagation() {}, ...event }); },
        getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    };
    return element;
}

function sandbox(extra = {}) {
    const clock = { now: 0 };
    const frames = [];
    const timers = [];
    const box = {
        console,
        performance: { now: () => clock.now },
        requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
        cancelAnimationFrame: () => {},
        setTimeout: (fn, ms) => { timers.push({ fn, at: clock.now + (ms || 0) }); return timers.length; },
        clearTimeout: () => {},
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
        dispatchEvent: (event) => { (box.events ||= []).push(event); return true; },
        addEventListener: () => {},
        removeEventListener: () => {},
        getComputedStyle: (element) => ({ opacity: element._opacity ?? element.style.opacity ?? "1" }),
        document: {
            createElement: (tag) => fakeElement(tag),
            getElementById: () => null,
            documentElement: fakeElement("html"),
        },
        matchMedia: () => ({ matches: false, addEventListener() {} }),
        ...extra,
    };
    box.window = box;
    box.globalThis = box;
    box.clock = clock;
    box.frames = frames;
    box.timers = timers;
    return box;
}

function load(box, ...files) {
    const context = createContext(box);
    for (const file of files) runInContext(readFileSync(join(JS, file), "utf8"), context);
    return box;
}

/** Fire every timer due by `ms` from now, in order. */
function advanceTimers(box, ms) {
    box.clock.now += ms;
    let ran = true;
    while (ran) {
        ran = false;
        for (const timer of [...box.timers]) {
            if (timer.at <= box.clock.now && !timer.done) {
                timer.done = true;
                ran = true;
                timer.fn();
            }
        }
    }
}

// == viewerPrefs.js ==========================================================

{
    const puts = [];
    const box = sandbox();
    box.fetch = async (url, options) => {
        if (options?.method === "PUT") {
            puts.push(JSON.parse(options.body));
            return { ok: true, json: async () => ({}) };
        }
        return { ok: true, json: async () => ({ sections: { xz: 0.4, yz: null }, motion: "on" }) };
    };
    load(box, "services/viewerPrefs.js");
    const prefs = box.PlexoraViewerPrefs;
    await prefs.load();
    check("saved pane sizes are read back", prefs.sectionShare("xz") === 0.4
        && prefs.sectionShare("yz") === null);
    check("motion is on by default", prefs.smoothMotion() === true
        && !box.document.documentElement.classList.contains("plx-reduced-motion"));

    prefs.set({ sections: { yz: 0.3 } }, { save: false });
    prefs.set({ sections: { yz: 0.35 } });
    prefs.set({ motion: "off" });
    check("motion off is off", prefs.smoothMotion() === false
        && box.document.documentElement.classList.contains("plx-reduced-motion"),
        "the root class is what stops the CSS transitions with it");
    advanceTimers(box, 500);
    await Promise.resolve();
    check("a run of changes is saved as one request, merged",
        puts.length === 1 && puts[0].sections.yz === 0.35 && puts[0].motion === "off"
        && !("xz" in puts[0].sections),
        "a pane dragged through a hundred sizes must not be a hundred writes");
    check("a change is announced", (box.events || []).some(
        (event) => event.type === "plexora:viewer-prefs-changed"));
}

{
    const box = sandbox({ matchMedia: () => ({ matches: true, addEventListener() {} }) });
    box.fetch = async () => ({ ok: false });
    load(box, "services/viewerPrefs.js");
    const prefs = box.PlexoraViewerPrefs;
    await prefs.load();
    check("the system's reduced-motion setting turns motion off",
        prefs.smoothMotion() === false && prefs.get().motion === "on");
}

// == planeMotion.js: the fade ================================================

function fadeBox({ motion = true } = {}) {
    const box = sandbox();
    load(box, "views/planeMotion.js");
    const { PlaneFade, FADE_MS, MIN_FADE_MS } = box.PlexoraPlaneMotion;
    const handlers = {};
    const draws = [];
    const source = fakeElement("canvas");
    source.width = 800;
    source.height = 600;
    //: The view, as OSD reports it; a probe moves it to stand for a pan.
    const view = { x: 0, y: 0, width: 1, height: 0.75 };
    const viewer = {
        drawer: { canvas: source },
        addHandler: (name, fn) => { handlers[name] = fn; },
        viewport: {
            getBounds: () => ({ ...view }),
            getContainerSize: () => ({ x: 800, y: 600 }),
            getRotation: () => 0,
            getFlip: () => false,
        },
    };
    const wrapper = fakeElement("div");
    box.document.createElement = (tag) => {
        const element = fakeElement(tag);
        element.getContext = () => ({
            set globalAlpha(value) { this._alpha = value; },
            get globalAlpha() { return this._alpha ?? 1; },
            drawImage(image) { draws.push({ image, alpha: this.globalAlpha }); },
            clearRect() {},
        });
        element.animate = (keyframes, options) => {
            const animation = { keyframes, options, cancelled: false,
                                cancel() { animation.cancelled = true; } };
            element._animation = animation;
            element._opacity = "1";
            return animation;
        };
        return element;
    };
    const fade = new PlaneFade(viewer, {
        prefs: { smoothMotion: () => motion }, wrapper });
    return { box, fade, source, draws, handlers, wrapper, view, FADE_MS, MIN_FADE_MS };
}

{
    const { fade, source, draws, FADE_MS } = fadeBox();
    check("the fade holds the picture on screen", fade.capture() === true
        && draws.length === 1 && draws[0].image === source && draws[0].alpha === 1);
    const ghost = fade.ghost;
    check("in a canvas the size of the picture, laid directly over it",
        ghost.width === 800 && ghost.height === 600 && source.nextSibling === ghost
        && ghost.hidden === false);
    fade.release();
    check("a lone step fades over the full length",
        ghost._animation.options.duration === FADE_MS
        && ghost._animation.keyframes[0].opacity === 1
        && ghost._animation.keyframes[1].opacity === 0);
}

{
    const { box, fade, source, draws, MIN_FADE_MS } = fadeBox();
    fade.capture();
    fade.release();
    const ghost = fade.ghost;
    ghost._opacity = "0.6";               // 40 % of the way through
    box.clock.now += 50;                  // the next step, 50 ms later
    fade.capture();
    check("a step during a fade blends into what is on screen, not back to the old plane",
        draws.length === 2 && draws[1].image === source && near(draws[1].alpha, 0.4),
        "ghost = 0.6 x old + 0.4 x new is exactly the mix that was showing");
    fade.release();
    check("and a fast scrub's fades are short, never trailing the hand",
        ghost._animation.options.duration === MIN_FADE_MS);
}

{
    const { fade, handlers, view } = fadeBox();
    fade.capture();
    fade.release();
    handlers.animation();
    check("an OSD 'animation' with nothing moved does not end a fade",
        fade.ghost.hidden === false && fade.ghost._animation.cancelled === false,
        "every swap adds and removes items, which starts one: the fade lasted a frame");
    view.x += 0.01;                       // 8 screen pixels
    handlers.animation();
    check("a pan during a fade ends it at once",
        fade.ghost.hidden === true && fade.ghost._animation.cancelled === true,
        "the ghost is a picture of the screen; moved under, it no longer lines up");
}

{
    const { fade, handlers, view } = fadeBox();
    fade.capture();
    fade.release();
    view.width *= 1.01;                   // a zoom of 8 screen pixels across
    handlers.animation();
    check("so does a zoom", fade.ghost.hidden === true);
}

{
    const { fade, draws } = fadeBox({ motion: false });
    check("with motion off there is no fade at all",
        fade.capture() === false && draws.length === 0 && fade.ghost === null);
}

{
    const { fade, wrapper } = fadeBox();
    wrapper.classList.add("is-volume-mode");
    check("nor under the 3-D view, where the tiles are hidden", fade.capture() === false);
}

// == planeMotion.js: the prefetch ============================================

{
    const box = sandbox();
    load(box, "views/planeMotion.js");
    const { NeighbourPrefetch, withView } = box.PlexoraPlaneMotion;
    check("a tile address changes plane by its view token only",
        withView("/t/ch0/2/3_4.png?q=hd&view=z12", "z13") === "/t/ch0/2/3_4.png?q=hd&view=z13"
        && withView("/t/ch0/2/3_4.png", "z13") === null);
    const tile = (x) => ({ tile: { getUrl: () => `/t/ch0/2/${x}_0.png?view=z12` } });
    const item = (view, tiles) => ({ source: { view }, getOpacity: () => 1, _lastDrawn: tiles });
    const world = [item("z12", [tile(0), tile(1)]), item("z12", [tile(0), tile(1)]),
                   item("z11", [tile(9)])];
    const viewer = { world: { getItemCount: () => world.length, getItemAt: (i) => world[i] } };
    const state = { mode: "plane", z: 12, t: 0 };
    const viewState = { depth: 40, get: () => ({ ...state }), spec: (s) => `z${s.z}` };
    const prefetch = new NeighbourPrefetch(viewer, viewState, { limit: 3 });
    const plan = prefetch.plan();
    check("the tiles on screen are warmed at the next plane first",
        plan.length === 3 && plan[0].endsWith("view=z13") && plan[1].endsWith("view=z13")
        && plan[2].endsWith("view=z11"),
        "two tiles (the pair shares them), then the plane before, up to the limit");
    prefetch.onRequested({ detail: { state: { z: 12 } } });
    prefetch.onRequested({ detail: { state: { z: 11 } } });
    check("moving down the stack warms the plane below first",
        prefetch.plan()[0].endsWith("view=z11"));
    state.mode = "slab";
    check("nothing is warmed for a projection", prefetch.plan().length === 0);
}

// == volumeRenderer.js: moving the 3-D view =================================

function volumeBox({ motion = true } = {}) {
    const box = sandbox();
    box.PlexoraViewerPrefs = { smoothMotion: () => motion };
    load(box, "views/volumeRenderer.js");
    const VolumeRenderer = box.PlexoraVolumeRenderer;
    const renderer = new VolumeRenderer({ viewState: null, datasource: "d",
                                          wrapper: fakeElement("div") });
    const canvas = fakeElement("canvas");
    canvas.clientHeight = 500;
    canvas.setPointerCapture = () => {};
    renderer.bindPointer(canvas);
    return { box, renderer, canvas, VolumeRenderer, MOTION: VolumeRenderer.MOTION };
}

/** Run the renderer's frames for `ms` at `hz`. */
function runFrames(box, renderer, ms, hz) {
    const step = 1000 / hz;
    const end = box.clock.now + ms;
    while (box.clock.now < end) {
        box.clock.now += step;
        const pending = box.frames.splice(0);
        renderer.frame = 0;
        if (!pending.length && !renderer.animating()) break;
        renderer.onFrame(box.clock.now);
    }
}

{
    const { VolumeRenderer, MOTION } = volumeBox();
    const speed = VolumeRenderer.releaseSpeed;
    const samples = (total, overMs, endAt) => [
        { t: endAt - overMs, yaw: 0, pitch: 0 }, { t: endAt, yaw: total, pitch: 0 }];
    check("a slow, placed release does not coast",
        speed(samples(0.004, 80, 1000), 1000) === null,
        "positioning precisely must not end in a nudge");
    check("a hand that stopped before letting go does not coast",
        speed(samples(0.4, 80, 1000), 1000 + MOTION.RELEASE_STILL_MS + 10) === null);
    const flick = speed(samples(0.16, 80, 1000), 1005);
    check("a moving release coasts at the hand's speed",
        flick && near(flick.yaw, 0.002, 1e-12) && flick.pitch === 0);
    const hard = speed(samples(2, 50, 1000), 1000);
    check("a hard flick is capped", hard && near(Math.hypot(hard.yaw, hard.pitch), MOTION.SPIN_MAX));
}

{
    // The same release at 60 and at 120 frames a second.
    const travel = (hz) => {
        const { box, renderer } = volumeBox();
        renderer.camera.yaw = 0;
        renderer.spin = { yaw: 0.003, pitch: 0 };
        renderer.lastStep = box.clock.now;
        renderer.invalidate();
        runFrames(box, renderer, 3000, hz);
        return { yaw: renderer.camera.yaw, ended: renderer.spin === null };
    };
    const at60 = travel(60);
    const at120 = travel(120);
    const { MOTION } = volumeBox();
    const expected = MOTION.SPIN_TAU_MS * (0.003 - MOTION.SPIN_STOP);
    check("a coast ends on its own", at60.ended && at120.ended);
    check("the coast is the same at 60 and 120 frames a second",
        near(at60.yaw, at120.yaw, 0.01),
        `60 Hz ${at60.yaw.toFixed(4)} rad vs 120 Hz ${at120.yaw.toFixed(4)} rad`);
    check("and as far as the release was fast (speed x decay time)",
        near(at60.yaw, expected, 0.02), `${at60.yaw.toFixed(4)} vs ${expected.toFixed(4)}`);
}

{
    const { box, renderer, canvas } = volumeBox();
    const startYaw = renderer.camera.yaw;
    canvas.fire("pointerdown", { clientX: 400, clientY: 300, button: 0, pointerId: 1 });
    for (let i = 1; i <= 5; i += 1) {
        box.clock.now += 16;
        canvas.fire("pointermove", { clientX: 400 - 6 * i, clientY: 300 });
    }
    check("a drag turns the view under the hand",
        near(renderer.camera.yaw, startYaw + 30 * 0.008, 1e-12));
    canvas.fire("pointerup", { clientX: 370, clientY: 300 });
    check("let go while moving, it keeps turning", renderer.spin && renderer.spin.yaw > 0);
    runFrames(box, renderer, 100, 60);
    const coasted = renderer.camera.yaw;
    check("...for a while after the hand is gone", coasted > startYaw + 30 * 0.008);
    canvas.fire("pointerdown", { clientX: 370, clientY: 300, button: 0, pointerId: 1 });
    runFrames(box, renderer, 100, 60);
    check("a press stops it dead", renderer.spin === null && renderer.camera.yaw === coasted);
    canvas.fire("pointerup", { clientX: 370, clientY: 300 });
    check("and a click (no movement) does not start it again", renderer.spin === null);
}

{
    const { box, renderer } = volumeBox();
    renderer.camera.yaw = 3.0;
    renderer.preset("iso");                  // iso is yaw -0.6: 2.68 rad the short way
    runFrames(box, renderer, 1000, 60);
    check("a preset turns there the short way round",
        renderer.tween === null
        && near(Math.atan2(Math.sin(renderer.camera.yaw + 0.6), Math.cos(renderer.camera.yaw + 0.6)), 0, 1e-9)
        && renderer.camera.yaw > 3.0,
        `ended at ${renderer.camera.yaw.toFixed(3)} rad`);
}

{
    const { box, renderer, canvas } = volumeBox({ motion: false });
    canvas.fire("pointerdown", { clientX: 400, clientY: 300, button: 0, pointerId: 1 });
    for (let i = 1; i <= 5; i += 1) {
        box.clock.now += 16;
        canvas.fire("pointermove", { clientX: 400 - 10 * i, clientY: 300 });
    }
    canvas.fire("pointerup", { clientX: 350, clientY: 300 });
    check("with motion off a release does not coast", renderer.spin === null);
    renderer.preset("xz");
    check("...a preset lands at once", renderer.tween === null
        && near(renderer.camera.pitch, 0) && near(renderer.camera.yaw, 0));
    const before = renderer.camera.dist;
    canvas.fire("wheel", { deltaY: 100, deltaMode: 0, shiftKey: false });
    check("...and the wheel zooms without easing",
        renderer.zoomTarget === null && renderer.camera.dist > before);
}

{
    const { box, renderer, canvas } = volumeBox();
    const before = renderer.camera.dist;
    canvas.fire("wheel", { deltaY: 100, deltaMode: 0, shiftKey: false });
    check("the wheel's zoom eases toward its target",
        renderer.zoomTarget > before && renderer.camera.dist === before);
    runFrames(box, renderer, 1000, 60);
    check("...and arrives", renderer.zoomTarget === null
        && near(renderer.camera.dist, before * Math.exp(100 * 0.0012), 1e-6));
}

{
    // Resolution while moving: learnt from frames the view draws itself.
    const { box, renderer, canvas } = volumeBox();
    canvas.fire("pointerdown", { clientX: 400, clientY: 300, button: 0, pointerId: 1 });
    for (let i = 1; i <= 20; i += 1) {    // a drag whose pointer moves every 40 ms
        box.clock.now += 40;
        canvas.fire("pointermove", { clientX: 400 - 2 * i, clientY: 300 });
        box.frames.splice(0);
        renderer.frame = 0;
        renderer.onFrame(box.clock.now);
    }
    check("a drag's pointer pace is not read as slow frames", renderer.fastScale === 1,
        "frames under a drag come as fast as the pointer moves");
    canvas.fire("pointercancel", {});
    renderer.spin = { yaw: 0.004, pitch: 0 };
    renderer.lastStep = box.clock.now;
    for (let i = 0; i < 12; i += 1) {     // a coast drawn at 25 frames a second
        box.clock.now += 40;
        box.frames.splice(0);
        renderer.frame = 0;
        renderer.onFrame(box.clock.now);
    }
    check("slow frames while coasting lower the resolution", renderer.fastScale < 1);
}

// == sectionPanes.js: sizes ==================================================

{
    const box = sandbox();
    const saved = [];
    const shares = { xz: null, yz: null };
    box.PlexoraViewerPrefs = {
        sectionShare: (axis) => shares[axis],
        set(patch) { saved.push(patch); Object.assign(shares, patch.sections || {}); },
    };
    load(box, "views/sectionPanes.js");
    const { SectionPanes, Pane } = box.PlexoraSectionPanes;
    const owner = new SectionPanes({ viewState: { get: () => ({ z: 20, t: 0 }), depth: 40 } });
    owner.wrapper = fakeElement("div");
    const followed = [];
    owner.follow = (settled) => followed.push(settled);
    const pane = new Pane(owner, "xz");
    pane.geometry = { height: 100, width: 2000 };
    owner.frame = { width: 1000, height: 800, scale: 2 };
    let view = pane.layout();
    check("by default a pane is as tall as its stack",
        view.size === 200 && view.rows === 100 && view.top === 0 && view.whole === true);
    owner.frame.scale = 6;
    view = pane.layout();
    check("...up to its default share of the plane, scrolled to the plane on screen",
        view.size === 256 && view.whole === false && near(view.rows, 256 / 6) && near(view.top, 51.25 - 128 / 6, 1e-9),
        "plane 20 of 40 is row 51.25 of 100: the pane is centred on it");
    shares.xz = 0.5;
    view = pane.layout();
    check("a chosen size is honoured", view.size === 400);
    owner.frame.scale = 2;
    view = pane.layout();
    check("but a pane is never bigger than its stack at the plane's scale",
        view.size === 200, "the section keeps the plane's proportions: more planes, never stretched ones");
    check("the divider says how big the pane is",
        pane.divider.getAttribute("aria-valuenow") === "25"
        && pane.divider.getAttribute("role") === "separator");

    saved.length = 0;
    followed.length = 0;
    owner.resize("xz", 320);
    owner.resize("xz", 480);
    check("a drag moves the pane without saving or recutting",
        saved.length === 0 && owner.shareOf("xz") === 0.6 && followed.every((s) => s === false));
    owner.resize("xz", 480, { save: true });
    check("...and its end saves the size once",
        saved.length === 1 && saved[0].sections.xz === 0.6 && followed.at(-1) === true);
    owner.resize("xz", 5000, { save: true });
    check("a size past the bounds is clamped", shares.xz === 0.75);
    owner.resetSize("xz");
    check("double-click gives the default back", shares.xz === null && owner.shareOf("xz") === null);
}

console.log(failures.length ? `\n${failures.length} of ${checks} check(s) failed`
                            : `\nall ${checks} checks passed`);
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
