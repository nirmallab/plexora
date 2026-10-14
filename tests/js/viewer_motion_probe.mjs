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
 *                     Play's turn keeps its speed at any frame rate, never
 *                     ends on its own, and stops on a press, a preset, a
 *                     lock, a restored camera, a rendered frame, leaving 3-D.
 *   sectionPanes.js   a pane is the size chosen, either way: past its stack's
 *                     height z is stretched to fill it; it never covers the
 *                     plane's last MIN_XY_PX; a drag reloads nothing until it
 *                     ends, then saves once.
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
        getComputedStyle: (element) => ({ opacity: element._opacity ?? element.style.opacity ?? "1",
                                          backgroundColor: element._background ?? "rgba(0, 0, 0, 0)",
                                          filter: element._filter ?? "none" }),
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
    // How Play turns the stack: the user's, merged and held to its bounds.
    const puts = [];
    const box = sandbox();
    box.fetch = async (url, options) => {
        if (options?.method === "PUT") {
            puts.push(JSON.parse(options.body));
            return { ok: true, json: async () => ({}) };
        }
        return { ok: true, json: async () => ({ rotate: { speed: 60, axis: "x", direction: -1 } }) };
    };
    load(box, "services/viewerPrefs.js");
    const prefs = box.PlexoraViewerPrefs;
    await prefs.load();
    check("the turning speed, axis and direction are read back",
        prefs.rotate().speed === 60 && prefs.rotate().axis === "x" && prefs.rotate().direction === -1);
    prefs.set({ rotate: { speed: 500 } });
    prefs.set({ rotate: { axis: "sideways", direction: 1 } });
    check("a speed past the bounds is held to them; nonsense keeps what was there",
        prefs.rotate().speed === 90 && prefs.rotate().axis === "x" && prefs.rotate().direction === 1);
    advanceTimers(box, 500);
    await Promise.resolve();
    check("and the changes are saved as one, merged",
        puts.length === 1 && puts[0].rotate.speed === 500 && puts[0].rotate.direction === 1,
        JSON.stringify(puts));
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
    // The ground is CSS on the viewer's container, two levels up.
    const container = fakeElement("div");
    container._background = "rgb(0, 0, 0)";
    const between = fakeElement("div");
    between.parentElement = container;
    source.parentElement = between;
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
            filter: "none",
            fillStyle: "",
            drawImage(image) { draws.push({ canvas: element, image, alpha: this.globalAlpha }); },
            fillRect() { draws.push({ canvas: element, fill: this.fillStyle }); },
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
    const held = fade.capture();
    const ghost = fade.ghost;
    const onGhost = draws.filter((d) => d.canvas === ghost);
    const onPicture = draws.filter((d) => d.canvas === fade.picture);
    check("the fade holds the picture on screen: the ground, then the canvas over it",
        held === true && onPicture.length === 2 && onPicture[0].fill === "rgb(0, 0, 0)"
        && onPicture[1].image === source && onGhost.length === 1
        && onGhost[0].image === fade.picture && onGhost[0].alpha === 1);
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
    const onGhost = draws.filter((d) => d.canvas === ghost);
    check("a step during a fade blends into what is on screen, not back to the old plane",
        onGhost.length === 2 && onGhost[1].image === fade.picture && near(onGhost[1].alpha, 0.4)
        && draws.some((d) => d.canvas === fade.picture && d.image === source),
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

{
    // How far ahead follows the hand: two planes for a step now and then, more
    // for a held key, back to two once it has been still.
    const box = sandbox();
    load(box, "views/planeMotion.js");
    const { NeighbourPrefetch, STANDBY_AHEAD } = box.PlexoraPlaneMotion;
    const state = { mode: "plane", z: 10, t: 0 };
    const viewState = { depth: 40, get: () => ({ ...state }), spec: (s) => `z${s.z}` };
    const prefetch = new NeighbourPrefetch({ world: { getItemCount: () => 0 } }, viewState);
    const planes = () => prefetch.neighbourStates().map((s) => s.z);
    check("a plane at rest: the next two and the one behind", planes().join() === "11,12,9");
    for (let z = 10; z <= 20; z += 1) {     // a held key at 12 planes a second
        state.z = z;
        prefetch.onRequested({ detail: { state: { z } } });
        box.clock.now += 83;
    }
    const held = planes();
    check("a held key: as many ahead as it reaches in 0.4 s, within the bound",
        held.length === 6 && held[0] === 21 && held[4] === 25 && held[5] === 19
        && prefetch.ahead() === 5 && STANDBY_AHEAD[1] === 6, held.join());
    box.clock.now += 1500;
    check("still again: two ahead", prefetch.ahead() === 2);
    // A jump is not travel: 50 planes 100 ms after a step set no pace, nor
    // does the step that follows it.
    for (const z of [20, 21, 71]) {
        state.z = z;
        prefetch.onRequested({ detail: { state: { z } } });
        box.clock.now += 100;
    }
    check("a jump sets no pace: two ahead of it", prefetch.ahead() === 2 && prefetch.pace() === 0,
        `ahead ${prefetch.ahead()}, pace ${prefetch.pace()}`);
    state.z = 72;
    prefetch.onRequested({ detail: { state: { z: 72 } } });
    check("nor the step after a jump", prefetch.pace() === 0);
}

{
    // A drag down the rail of a deep stack: five planes a pointer event, 30 ms
    // apart. That is travel, not a string of jumps -- the hand has a pace and
    // six planes ahead are made ready -- and while it moves the tile scheduler
    // is asked to keep standby planes to their coarsest levels.
    const box = sandbox();
    load(box, "views/planeMotion.js");
    const { NeighbourPrefetch } = box.PlexoraPlaneMotion;
    const state = { mode: "plane", z: 10, t: 0 };
    const viewState = { depth: 80, get: () => ({ ...state }), spec: (s) => `z${s.z}` };
    const holds = [];
    const tileFetch = { setScrubbing: (until) => holds.push([box.clock.now, until]) };
    const prefetch = new NeighbourPrefetch({ world: { getItemCount: () => 0 } }, viewState,
                                           { viewerManager: { imageViewer: { tileFetch } } });
    const ask = (z, detail = {}) => {
        state.z = z;
        prefetch.onRequested({ detail: { state: { z }, ...detail } });
    };
    for (let z = 10; z <= 30; z += 5) {
        ask(z);                              // no `scrub` given: the gap decides
        box.clock.now += 30;
    }
    const planes = prefetch.neighbourStates().map((s) => s.z);
    check("a rail drag that moves five planes per event 30 ms apart is travel: pace > 0 and six ahead",
        prefetch.pace() > 0 && prefetch.ahead() === 6 && planes.join() === "31,32,33,34,35,36,29",
        `pace ${prefetch.pace()}, ahead ${prefetch.ahead()}, planes ${planes.join()}`);
    const dragHolds = holds.length;
    box.clock.now += 100;                    // the hand still, then a click far away
    ask(70);
    check("a far click after the hand rested is still a jump: no pace, two ahead",
        prefetch.pace() === 0 && prefetch.ahead() === 2, `pace ${prefetch.pace()}`);
    box.clock.now += 500;
    ask(71, { scrub: true });                // viewState says the hand moves
    box.clock.now += 20;
    ask(72, { scrub: false });               // ...and that it does not
    // viewState's SCRUB_HOLD_MS (100: just past the widest gap that is still
    // a scrub, SCRUB_GAP_MS 80) -- 250 until the drag benchmark showed the
    // hold delaying the final plane's sharpening by that much.
    check("a scrub request asks the scheduler to hold (setScrubbing called with a time ≥ now+100)",
        dragHolds === 4 && holds.length === 5
        && holds.every(([at, until]) => until >= at + 100), JSON.stringify(holds));
    const bare = new NeighbourPrefetch({ world: { getItemCount: () => 0 } }, viewState,
                                       { viewerManager: { imageViewer: { tileFetch: {} } } });
    let threw = null;
    try {
        bare.onRequested({ detail: { state: { z: 11 }, scrub: true } });
        new NeighbourPrefetch({ world: { getItemCount: () => 0 } }, viewState)
            .onRequested({ detail: { state: { z: 11 }, scrub: true } });
    } catch (error) {
        threw = error;
    }
    check("a scheduler without setScrubbing, or none at all, is left alone", threw === null, String(threw));
}

{
    // The hand let go (viewState.endScrub raises `plexora:scrub-ended`): the
    // scheduler's hold ends then, not SCRUB_HOLD_MS after the last request.
    const listeners = {};
    const box = sandbox({ addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); } });
    load(box, "views/planeMotion.js");
    const { NeighbourPrefetch } = box.PlexoraPlaneMotion;
    const calls = [];
    const tileFetch = { setScrubbing: (until) => calls.push(["hold", until]),
                        endScrubbing: () => calls.push(["end"]) };
    const viewState = { depth: 80, get: () => ({ mode: "plane", z: 10, t: 0 }), spec: (s) => `z${s.z}` };
    new NeighbourPrefetch({ world: { getItemCount: () => 0 } }, viewState,
                          { viewerManager: { imageViewer: { tileFetch } } }).attach();
    const raise = (name, detail = {}) => { for (const fn of listeners[name] || []) fn({ detail }); };
    raise("plexora:view-requested", { state: { z: 11 }, scrub: true });
    raise("plexora:scrub-ended");
    check("release ends the hold: endScrubbing called", calls.map(([kind]) => kind).join() === "hold,end",
          JSON.stringify(calls));
    let threw = null;
    try {
        delete tileFetch.endScrubbing;
        raise("plexora:scrub-ended");
    } catch (error) {
        threw = error;
    }
    check("...and a scheduler without endScrubbing is left alone", threw === null, String(threw));
}

{
    // A plane that covers the view but is not sharp yet: enough to start the
    // standby planes while the hand moves, not after a step or a jump.
    const box = sandbox();
    load(box, "views/planeMotion.js");
    const { NeighbourPrefetch } = box.PlexoraPlaneMotion;
    const loading = { source: {}, getOpacity: () => 1, getFullyLoaded: () => false };
    const viewer = { world: { getItemCount: () => 1, getItemAt: () => loading } };
    box.PlexoraChannelFade = { covers: (item) => item === loading };
    const state = { mode: "plane", z: 10, t: 0 };
    const viewState = { depth: 40, get: () => ({ ...state }), spec: (s) => `z${s.z}` };
    const prefetch = new NeighbourPrefetch(viewer, viewState);
    check("at rest a covering plane sharpens before any standby", prefetch.planeLoaded() === false);
    for (let z = 10; z <= 16; z += 1) {
        state.z = z;
        prefetch.onRequested({ detail: { state: { z } } });
        box.clock.now += 83;
    }
    check("while the hand moves, covering is enough", prefetch.planeLoaded() === true);
}

// == volumeCamera.js: views and locked turns ==================================

{
    const box = sandbox();
    load(box, "views/volumeCamera.js");
    const C = box.PlexoraVolumeCamera;
    const on = (q, axis) => C.axisOnScreen(q, axis).map((v) => Math.round(v * 1000) / 1000 + 0);
    const same = (a, b) => a.every((v, i) => near(v, b[i], 1e-9));
    check("XY is the plane viewer's picture: x right, y down, z into the screen",
        same(on(C.VIEWS.xy, "x"), [1, 0, 0]) && same(on(C.VIEWS.xy, "y"), [0, -1, 0])
        && same(on(C.VIEWS.xy, "z"), [0, 0, -1]));
    check("XZ is the XZ pane's: x right, z down, seen from the +y side",
        same(on(C.VIEWS.xz, "x"), [1, 0, 0]) && same(on(C.VIEWS.xz, "z"), [0, -1, 0])
        && same(on(C.VIEWS.xz, "y"), [0, 0, 1]));
    check("YZ is the YZ pane's: z right, y down, seen from the +x side",
        same(on(C.VIEWS.yz, "z"), [1, 0, 0]) && same(on(C.VIEWS.yz, "y"), [0, -1, 0])
        && same(on(C.VIEWS.yz, "x"), [0, 0, 1]));
    check("an axis's button looks along it: X gives YZ, Y gives XZ, Z gives XY",
        C.viewAlong("x") === C.VIEWS.yz && C.viewAlong("y") === C.VIEWS.xz
        && C.viewAlong("z") === C.VIEWS.xy);
    check("and from the far side when asked",
        near(on(C.viewAlong("x", { reverse: true }), "x")[2], -1, 1e-9));

    // A locked turn about y, seen face-on: dragging right carries the near
    // side right, as a free drag would.
    const nearPoint = [0, 0, -1];                 // the plane nearest the viewer (xy view)
    const across = C.lockedDrag(C.VIEWS.xy, "y");
    const turned = C.turnAbout(C.VIEWS.xy, "y", across.angle(20, 0));
    check("a locked axis lying in the screen turns across it, the near side following the hand",
        across.mode === "across" && C.quat.rotate(turned, nearPoint)[0] > 0.1,
        `near point now at x ${C.quat.rotate(turned, nearPoint)[0].toFixed(3)}`);
    check("...and only about that axis: y stays where it was",
        same(on(turned, "y"), [0, -1, 0]));
    // Locked about z, which points at the viewer: the pointer's sweep round
    // the centre. A point right of centre, pointer moving down from there.
    const round = C.lockedDrag(C.VIEWS.xy, "z");
    const angle = round.angle(0, 10, { x: 600, y: 410 }, { x: 500, y: 400 });
    const spun = C.turnAbout(C.VIEWS.xy, "z", angle);
    check("a locked axis facing the viewer turns round the centre with the pointer",
        round.mode === "round" && C.quat.rotate(spun, [1, 0, 0])[1] < -0.05,
        "the right-hand side moves down on screen, as the pointer did");
    check("...and z stays put", same(on(spun, "z"), [0, 0, -1]));
    check("a free drag turns about the screen's axes, the near side following",
        C.quat.rotate(C.turnFree(C.VIEWS.xy, 20, 0), nearPoint)[0] > 0.1
        && C.quat.rotate(C.turnFree(C.VIEWS.xy, 0, 20), nearPoint)[1] < -0.1);
}

// == planeMotion.js: the 3-D view's ghost ====================================

{
    const { box, source, draws } = fadeBox();
    const { VolumeGhost } = box.PlexoraPlaneMotion;
    source._background = "rgb(0, 0, 0)";
    let key = "cam-1";
    let motion = true;
    const renderer = { canvas: source, cameraKey: () => key, _pinnedSize: null };
    const ghost = new VolumeGhost(renderer, { prefs: { smoothMotion: () => motion } });
    const marks = [];
    box.PlexoraViewPerf = { mark: (...args) => marks.push(args) };
    const held = ghost.capture();
    check("the 3-D ghost holds the volume's canvas, opaque on its ground, laid over it",
        held === true && ghost.ghost.className === "viewer-volume-ghost"
        && source.nextSibling === ghost.ghost && draws.some((d) => d.fill === "rgb(0, 0, 0)")
        && marks.some((m) => m[0] === "vghost" && m[1] === "captured"),
        "its own mark, so the plane's fade count is not muddied");
    ghost.release();
    ghost.sync("cam-1");
    check("the same camera keeps the fade running", !ghost.idle());
    key = "cam-2";
    ghost.sync("cam-2");
    check("another camera ends it at once: the ghost would no longer line up", ghost.idle());
    renderer._pinnedSize = { width: 10, height: 10 };
    check("a pinned size (a video being rendered) never holds one", ghost.capture() === false);
    renderer._pinnedSize = null;
    motion = false;
    check("nor does motion off", ghost.capture() === false);
}

// == volumeRenderer.js: moving the 3-D view =================================

function volumeBox({ motion = true } = {}) {
    const box = sandbox();
    box.PlexoraViewerPrefs = { smoothMotion: () => motion };
    load(box, "views/volumeCamera.js", "views/volumeRenderer.js");
    const VolumeRenderer = box.PlexoraVolumeRenderer;
    const renderer = new VolumeRenderer({ viewState: null, datasource: "d",
                                          wrapper: fakeElement("div") });
    const canvas = fakeElement("canvas");
    canvas.clientHeight = 500;
    canvas.clientWidth = 1000;
    canvas.setPointerCapture = () => {};
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 500 });
    renderer.canvas = null;
    renderer.bindPointer(canvas);
    return { box, renderer, canvas, VolumeRenderer, C: box.PlexoraVolumeCamera,
             MOTION: VolumeRenderer.MOTION };
}

/** Run the renderer's frames for `ms` at `hz`. */
function runFrames(box, renderer, ms, hz) {
    const step = 1000 / hz;
    const end = box.clock.now + ms;
    while (box.clock.now < end) {
        box.clock.now += step;
        box.frames.splice(0);
        renderer.frame = 0;
        if (!renderer.animating()) break;
        renderer.onFrame(box.clock.now);
    }
}

{
    const { VolumeRenderer, MOTION } = volumeBox();
    const speed = VolumeRenderer.releaseSpeed;
    const samples = (total, overMs, endAt) => [
        { t: endAt - overMs, a: 0, b: 0 }, { t: endAt, a: 0, b: total }];
    check("a slow, placed release does not coast",
        speed(samples(0.004, 80, 1000), 1000) === null,
        "positioning precisely must not end in a nudge");
    check("a hand that stopped before letting go does not coast",
        speed(samples(0.4, 80, 1000), 1000 + MOTION.RELEASE_STILL_MS + 10) === null);
    const flick = speed(samples(0.16, 80, 1000), 1005);
    check("a moving release coasts at the hand's speed",
        flick && near(flick.b, 0.002, 1e-12) && flick.a === 0);
    const hard = speed(samples(2, 50, 1000), 1000);
    check("a hard flick is capped", hard && near(Math.hypot(hard.a, hard.b), MOTION.SPIN_MAX));
}

{
    // The same release at 60 and at 120 frames a second.
    const travel = (hz) => {
        const { box, renderer, C } = volumeBox();
        const start = renderer.camera.q.slice();
        renderer.spin = { screen: [0, 0.003] };
        renderer.lastStep = box.clock.now;
        runFrames(box, renderer, 3000, hz);
        return { angle: C.quat.angleBetween(start, renderer.camera.q), ended: renderer.spin === null };
    };
    const at60 = travel(60);
    const at120 = travel(120);
    const { MOTION } = volumeBox();
    const expected = MOTION.SPIN_TAU_MS * (0.003 - MOTION.SPIN_STOP);
    check("a coast ends on its own", at60.ended && at120.ended);
    check("the coast is the same at 60 and 120 frames a second",
        near(at60.angle, at120.angle, 0.01),
        `60 Hz ${at60.angle.toFixed(4)} rad vs 120 Hz ${at120.angle.toFixed(4)} rad`);
    check("and as far as the release was fast (speed x decay time)",
        near(at60.angle, expected, 0.02), `${at60.angle.toFixed(4)} vs ${expected.toFixed(4)}`);
}

{
    // Play in 3-D: the stack turns at the speed asked, the same at 60 and 120
    // frames a second, and never stops on its own.
    const turned = (hz, motion = true) => {
        const { box, renderer, C } = volumeBox({ motion });
        const start = renderer.camera.q.slice();
        renderer.setAutoRotate(true, { speed: 30, axis: "screen", direction: 1 });
        renderer.lastStep = box.clock.now;
        runFrames(box, renderer, 1000, hz);
        return { angle: C.quat.angleBetween(start, renderer.camera.q), on: Boolean(renderer.autoRotate) };
    };
    const at60 = turned(60);
    const at120 = turned(120);
    const quiet = turned(60, false);
    const expected = (30 * Math.PI) / 180;
    check("Play turns the stack at its speed: 30 degrees in a second",
        near(at60.angle, expected, 0.01), `${at60.angle.toFixed(4)} vs ${expected.toFixed(4)}`);
    check("...the same at 60 and 120 frames a second", near(at60.angle, at120.angle, 0.005));
    check("...and keeps turning: it never slows or ends on its own", at60.on && at120.on);
    check("it is the user's action, so it turns with motion off too", quiet.on && near(quiet.angle, expected, 0.01));
}

{
    const { box, renderer, canvas, C } = volumeBox();
    const events = [];
    box.dispatchEvent = (event) => { events.push(event); return true; };
    let rested = 0;
    renderer.onRest = () => { rested += 1; };
    renderer.setAutoRotate(true, { speed: 45, axis: "z" });
    const zBefore = C.axisOnScreen(renderer.camera.q, "z");
    renderer.lastStep = box.clock.now;
    runFrames(box, renderer, 500, 60);
    const zAfter = C.axisOnScreen(renderer.camera.q, "z");
    check("about an image axis, that axis holds still on screen while the stack turns",
        zBefore.every((v, i) => near(v, zAfter[i], 1e-6)), JSON.stringify({ zBefore, zAfter }));
    check("the camera is not remembered while it turns (one save, once it stops)", rested === 0);
    check("starting is announced", events.some((e) => e.type === "plexora:volume-rotate" && e.detail.on === true));
    canvas.fire("pointerdown", { clientX: 10, clientY: 10, button: 0, pointerId: 1 });
    check("a press stops it, and that is announced", renderer.autoRotate === null
        && events.some((e) => e.type === "plexora:volume-rotate" && e.detail.on === false));
    canvas.fire("pointerup", { clientX: 10, clientY: 10, button: 0, pointerId: 1 });
    const stops = {
        turnTo: () => renderer.turnTo({ q: C.VIEWS.xy.slice() }),
        setLock: () => renderer.setLock("x"),
        restoreCamera: () => renderer.restoreCamera({ q: C.VIEWS.iso.slice(), dist: 2 }),
        renderFrame: () => { renderer.canvas = canvas; renderer.draw = () => {}; renderer.renderFrame(); },
        "leaving 3-D": () => renderer.show(false),
    };
    for (const [name, stop] of Object.entries(stops)) {
        renderer.setLock(null);
        renderer.setAutoRotate(true, { speed: 30 });
        stop();
        check(`${name} stops it`, renderer.autoRotate === null);
    }
    renderer.canvas = canvas;
    renderer.draw = () => {};
    renderer.setAutoRotate(true, { speed: 30 });
    renderer.snapshot();
    check("a plain capture records the pose and leaves it turning", renderer.autoRotate !== null);
    renderer.setAutoRotate(true, { speed: 500, direction: -1 });
    check("its speed is held to the bounds, its direction kept",
        near(renderer.autoRotate.rate, -((90 * Math.PI) / 180 / 1000), 1e-12));
}

{
    // The frame a coast ends in is drawn as the moving ones were: the full
    // picture follows at that same camera, so it can dissolve in (draw).
    const { box, renderer } = volumeBox();
    const drawn = [];
    renderer.draw = function draw() {
        this.drawnFast = this.moving() || Boolean(this._motionTail);
        drawn.push(this.drawnFast);
    };
    renderer.spin = { screen: [0, 0.003] };
    renderer.lastStep = box.clock.now;
    runFrames(box, renderer, 3000, 60);
    check("the frame a coast ends in is drawn as a moving one",
        renderer.spin === null && drawn.length > 2 && drawn.every(Boolean));
    check("...and the full frame is asked for once the view rests",
        box.timers.some((timer) => !timer.done && timer.at > box.clock.now));
}

{
    const { box, renderer, canvas, C } = volumeBox();
    const start = renderer.camera.q.slice();
    canvas.fire("pointerdown", { clientX: 400, clientY: 300, button: 0, pointerId: 1 });
    for (let i = 1; i <= 5; i += 1) {
        box.clock.now += 16;
        canvas.fire("pointermove", { clientX: 400 - 6 * i, clientY: 300 });
    }
    check("a drag turns the view under the hand",
        near(C.quat.angleBetween(start, renderer.camera.q), 30 * C.ROTATE_PER_PX, 1e-9));
    canvas.fire("pointerup", { clientX: 370, clientY: 300 });
    check("let go while moving, it keeps turning", renderer.spin && renderer.spin.screen);
    const released = renderer.camera.q.slice();
    runFrames(box, renderer, 100, 60);
    check("...for a while after the hand is gone",
        C.quat.angleBetween(released, renderer.camera.q) > 0.01);
    canvas.fire("pointerdown", { clientX: 370, clientY: 300, button: 0, pointerId: 1 });
    const pressed = renderer.camera.q.slice();
    runFrames(box, renderer, 100, 60);
    check("a press stops it dead", renderer.spin === null
        && C.quat.angleBetween(pressed, renderer.camera.q) === 0);
    canvas.fire("pointerup", { clientX: 370, clientY: 300 });
    check("and a click (no movement) does not start it again", renderer.spin === null);
}

{
    // Locked to the image's y: a drag and its coast turn about y alone.
    const { box, renderer, canvas, C } = volumeBox();
    renderer.camera.q = C.VIEWS.xy.slice();
    renderer.setLock("y");
    canvas.fire("pointerdown", { clientX: 400, clientY: 300, button: 0, pointerId: 1 });
    for (let i = 1; i <= 5; i += 1) {
        box.clock.now += 16;
        canvas.fire("pointermove", { clientX: 400 + 8 * i, clientY: 300 + 5 * i });
    }
    canvas.fire("pointerup", { clientX: 440, clientY: 325 });
    runFrames(box, renderer, 500, 60);
    const y = C.axisOnScreen(renderer.camera.q, "y");
    check("a locked drag, and its coast, turn about that image axis only",
        near(y[0], 0, 1e-9) && near(y[1], -1, 1e-9) && near(y[2], 0, 1e-9)
        && C.quat.angleBetween(C.VIEWS.xy, renderer.camera.q) > 0.1);
    renderer.setLock("y");
    check("the same key again lets go", renderer.lock === null);
    renderer.setLock("x");
    renderer.setLock("z");
    check("another axis key switches the lock at once", renderer.lock === "z");
}

{
    const { box, renderer, C } = volumeBox();
    renderer.camera.q = C.turnFree(C.VIEWS.iso, 900, 0);
    renderer.preset("xz");
    runFrames(box, renderer, 1000, 60);
    check("a named view is turned to and reached",
        renderer.tween === null && C.quat.angleBetween(renderer.camera.q, C.VIEWS.xz) < 1e-6);
    renderer.lookAlong("y");
    runFrames(box, renderer, 1000, 60);
    check("asking for the view already shown turns to its far side",
        C.quat.angleBetween(renderer.camera.q, C.viewAlong("y", { reverse: true })) < 1e-6);
}

{
    const { box, renderer, canvas, C } = volumeBox({ motion: false });
    canvas.fire("pointerdown", { clientX: 400, clientY: 300, button: 0, pointerId: 1 });
    for (let i = 1; i <= 5; i += 1) {
        box.clock.now += 16;
        canvas.fire("pointermove", { clientX: 400 - 10 * i, clientY: 300 });
    }
    canvas.fire("pointerup", { clientX: 350, clientY: 300 });
    check("with motion off a release does not coast", renderer.spin === null);
    renderer.preset("xz");
    check("...a named view lands at once", renderer.tween === null
        && C.quat.angleBetween(renderer.camera.q, C.VIEWS.xz) < 1e-9);
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
    renderer.spin = { screen: [0, 0.004] };
    renderer.lastStep = box.clock.now;
    for (let i = 0; i < 12; i += 1) {     // a coast drawn at 25 frames a second
        box.clock.now += 40;
        box.frames.splice(0);
        renderer.frame = 0;
        renderer.onFrame(box.clock.now);
    }
    check("slow frames while coasting lower the resolution", renderer.fastScale < 1);
}

{
    // On a 120 Hz screen: a coast that keeps the display's pace keeps full
    // resolution; one that misses a frame in four steps down, though it
    // averages well under SLOW_FRAME_MS.
    const { box, renderer } = volumeBox();
    const coast = (gaps) => {
        renderer.spin = { screen: [0, 0.02] };
        renderer.lastStep = box.clock.now;
        for (const gap of gaps) {
            box.clock.now += gap;
            box.frames.splice(0);
            renderer.frame = 0;
            renderer.onFrame(box.clock.now);
        }
    };
    coast(Array(24).fill(8.3));
    check("a coast at the screen's pace keeps full resolution", renderer.fastScale === 1,
        String(renderer.fastScale));
    coast(Array(6).fill([8.3, 8.3, 8.3, 16.6]).flat());
    check("...one that misses a frame in four steps down", renderer.fastScale < 1,
        String(renderer.fastScale));
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
    const asked = [];
    const owner = new SectionPanes({ viewState: { get: () => ({ z: 20, t: 0 }), depth: 40,
                                                  set: (patch) => asked.push(patch) } });
    owner.wrapper = fakeElement("div");
    const followed = [];
    owner.follow = (settled) => followed.push(settled);
    const pane = new Pane(owner, "xz");
    pane.geometry = { height: 100, width: 2000 };
    owner.frame = { width: 1000, height: 800, scale: 2 };
    let view = pane.layout();
    check("by default a pane is as tall as its stack",
        view.size === 200 && view.rows === 100 && view.top === 0 && view.whole === true && view.factor === 1);
    owner.frame.scale = 6;
    view = pane.layout();
    check("...up to its default share of the plane, scrolled to the plane on screen",
        view.size === 256 && view.whole === false && near(view.rows, 256 / 6) && near(view.top, 51.25 - 128 / 6, 1e-9),
        "plane 20 of 40 is row 51.25 of 100: the pane is centred on it");
    shares.xz = 0.5;
    view = pane.layout();
    check("a chosen size is honoured", view.size === 400 && view.factor === 1);
    owner.frame.scale = 2;
    view = pane.layout();
    check("a pane bigger than its stack shows every plane, stretched along z to fill it",
        view.size === 400 && near(view.factor, 2) && view.inner === 200 && view.rows === 100
        && view.top === 0 && view.whole === true,
        "the size chosen is the size shown: no dead drag past the stack's height");
    check("...by a transform on the pane's own viewer, which keeps its natural size",
        pane.canvasHost.style.height === "200px" && pane.canvasHost.style.transform === "scaleY(2)"
        && pane.root.style.height === "400px" && pane.root.classList.contains("is-stretched"));
    check("the divider says how big the pane is",
        pane.divider.getAttribute("aria-valuenow") === "50"
        && pane.divider.getAttribute("role") === "separator");
    pane.paintZLine();
    check("the plane's line is drawn on the stretched rows",
        near(parseFloat(pane.zline.style.top), 51.25 * 2 * 2, 1e-6), String(pane.zline.style.top));
    pane.onClick({ clientY: 300 });
    check("a click reads the row under the pointer through the stretch",
        asked.at(-1)?.z === 30, "300 px down a 2x stretch at scale 2 is row 75 of 100: plane 30 of 40");
    shares.xz = 202 / 800;
    view = pane.layout();
    check("a size within a few pixels of the stack's height snaps to it",
        view.size === 200 && view.factor === 1 && !pane.root.classList.contains("is-stretched"));
    shares.xz = 0.5;
    owner.panes = { xz: pane, yz: pane };
    pane.layout();
    owner.paintLabels({ x: 10, y: 20 });
    check("the label says how far z is stretched", /z ×2\.0/.test(pane.label.textContent));

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
    check("a size past what leaves the plane room is clamped before it is saved",
        near(shares.xz, (800 - 240) / 800), "the share saved is one that was on screen");
    owner.resetSize("xz");
    check("double-click gives the default back", shares.xz === null && owner.shareOf("xz") === null);
}

{
    // A divider dragged: the cut holds still, nothing is asked for while the
    // hand moves, and the sections are asked for once when it lets go.
    const box = sandbox();
    const shares = { xz: null, yz: null };
    box.PlexoraViewerPrefs = {
        sectionShare: (axis) => shares[axis],
        set(patch) { Object.assign(shares, patch.sections || {}); },
    };
    load(box, "views/sectionPanes.js");
    const { SectionPanes, Pane } = box.PlexoraSectionPanes;
    const owner = new SectionPanes({ viewState: { get: () => ({ z: 20, t: 0 }), depth: 40 } });
    owner.wrapper = fakeElement("div");
    owner.enabled = true;
    const panes = { xz: new Pane(owner, "xz"), yz: new Pane(owner, "yz") };
    for (const pane of Object.values(panes)) {
        pane.geometry = { height: 100, width: 2000 };
        pane.apply = () => {};
    }
    owner.panes = panes;
    let cuts = 0;
    owner.measure = () => ({ width: 1000, height: 800, scale: 2, x0: 0, x1: 500, y0: 0, y1: 400,
                             cx: 250, cy: 200, cutX: 500, cutY: 400 });
    owner.locateCut = () => {
        cuts += 1;
        Object.assign(owner.frame, { cy: 200 - cuts, cutY: 400 - cuts });
    };
    owner.paintCrosshair = () => {};
    owner.prepareArea = () => {};
    const shows = [];
    owner.showAt = (frame) => shows.push(frame.cy);
    owner.follow(true);
    shows.length = 0;
    owner.bindDivider(panes.xz);
    const divider = panes.xz.divider;
    const startSize = panes.xz.view.size;
    divider.fire("pointerdown", { button: 0, clientY: 600, pointerId: 1 });
    const heldCut = owner.frame.cy;
    for (let i = 1; i <= 10; i += 1) divider.fire("pointermove", { clientY: 600 - i * 20 });
    check("a divider drag asks for no section while it moves, the cut held still",
        shows.length === 0 && owner.frame.cy === heldCut, `${shows.length} show(s) during the drag`);
    check("...while the pane follows the hand, either way",
        panes.xz.view.size === startSize + 200);
    divider.fire("pointerup", {});
    check("...and once let go, the sections are asked for once, where the cut ends up",
        shows.length === 1 && owner.resizing === null && near(shares.xz, (startSize + 200) / 800));
}

console.log(failures.length ? `\n${failures.length} of ${checks} check(s) failed`
                            : `\nall ${checks} checks passed`);
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
