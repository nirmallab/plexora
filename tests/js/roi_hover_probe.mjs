/**
 * What does the ROI plugin say when the pointer crosses a region?
 *
 * `plexora:roi-hover` / `plexora:roi-unhover` are the entire seam between this
 * plugin and Cell Explorer's composition card. The ROI side owns geometry and
 * answers "which region, and where is it on screen"; the card owns metadata and
 * answers "what is inside it". Neither can check the other, and every way the
 * seam can be wrong is quiet:
 *
 *   - re-announcing a region the pointer is merely moving around INSIDE
 *     re-anchors the card on every frame, which is the cursor-chasing the whole
 *     design rules out;
 *   - failing to announce the leave leaves a card floating over the image
 *     describing a region the pointer left long ago;
 *   - a store change under a stationary pointer produces no pointer event at
 *     all, so a deleted region goes on being described, and a reshaped one is
 *     described by its old outline. Both look like a stale card, not a bug;
 *   - an anchor computed in the wrong coordinate space puts the card in the
 *     wrong place, which reads as a layout problem somewhere else entirely.
 *
 * None of that throws, and no Python test executes this file.
 *
 * Run directly:  node tests/js/roi_hover_probe.mjs
 *   --source <path>   probe a different roiTools.js
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/roi/static");

const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1 ? join(STATIC, "roiTools.js") : process.argv[sourceArg + 1];

const SQUARE = (x, y, size) => ({
    type: "Polygon",
    coordinates: [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]],
});

/** Two regions side by side, so the pointer can cross straight from one into
 *  the other -- the case where a leave and an enter arrive together. */
function makeStore() {
    return {
        image: "default",
        editable: true,
        selectionId: null,
        listeners: new Set(),
        features: [
            { id: "r-A", name: "Tumor 1", category_id: "c", geometry: SQUARE(0, 0, 100), flags: {} },
            { id: "r-B", name: "Stroma 2", category_id: "c", geometry: SQUARE(200, 0, 100), flags: {} },
        ],
        categories: [{ id: "c", label: "Tumor", color: "#fff", visible: true, locked: false }],
        hiddenCategories: new Set(),
        category(id) { return this.categories.find((c) => c.id === id) || null; },
        feature(id) { return this.features.find((f) => f.id === id) || null; },
        get selected() { return this.feature(this.selectionId); },
        get activeCategory() { return this.categories[0]; },
        countFor() { return 0; },
        isLocked() { return false; },
        isVisible(feature) { return !this.hiddenCategories.has(feature.category_id); },
        visibleFeatures() { return this.features.filter((f) => this.isVisible(f)); },
        select(id) { this.selectionId = id; },
        onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
        changed() { this.listeners.forEach((fn) => fn()); },
        commit() { return true; },
    };
}

/** Every window CustomEvent the tools dispatched, in order. */
const dispatched = [];
/** Frames requested but not yet run -- hover is throttled to one per frame,
 *  and running them by hand is how the throttle itself gets checked. */
const frames = [];

const context = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Infinity,
    console, Uint8Array,
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: (fn) => frames.push(fn),
    cancelAnimationFrame: () => { frames.length = 0; },
    CustomEvent: class CustomEvent {
        constructor(type, init) { this.type = type; this.detail = init?.detail; }
    },
    document: { activeElement: null, addEventListener() {}, removeEventListener() {} },
    OpenSeadragon: {
        Point: class Point { constructor(x, y) { this.x = x; this.y = y; } },
        MouseTracker: class MouseTracker {
            constructor(options) { this.options = options; this.destroyed = false; }
            destroy() { this.destroyed = true; }
        },
    },
    window: {
        addEventListener() {}, removeEventListener() {},
        dispatchEvent(event) { dispatched.push({ type: event.type, detail: event.detail }); },
        PlexoraToolLoader: null,
    },
};
const ctx = createContext(context);
runInContext(readFileSync(join(STATIC, "roiGeometry.js"), "utf8"), ctx);
runInContext(`${readFileSync(SOURCE, "utf8")}\n;globalThis.__Tools = RoiInteraction;`, ctx);

/**
 * A viewer whose screen pixels ARE image pixels, so the numbers in this file
 * are the numbers the tools see -- except for the canvas offset, which is left
 * non-zero on purpose. An anchor computed without it is off by exactly the
 * distance between the window and the image, which is the mistake worth having
 * a check for.
 */
const CANVAS_OFFSET = { left: 20, top: 10 };

function fakeViewer() {
    const world = {
        handlers: new Map(),
        items: 1,
        getItemCount() { return this.items; },
        getItemAt: () => ({
            source: { getImagePixel: (_, p) => [p.x, p.y] },
            imageToViewportRectangle: (x, y, width, height) => ({ x, y, width, height }),
        }),
        addHandler(name, fn) { this.handlers.set(name, fn); },
        removeHandler(name) { this.handlers.delete(name); },
    };
    return {
        canvas: {
            style: {},
            getBoundingClientRect: () => ({
                left: CANVAS_OFFSET.left,
                top: CANVAS_OFFSET.top,
                right: CANVAS_OFFSET.left + 1000,
                bottom: CANVAS_OFFSET.top + 800,
            }),
        },
        world,
        viewport: {
            getZoom: () => 1,
            pixelFromPoint: (point) => ({ x: point.x, y: point.y }),
        },
        addHandler() {}, removeHandler() {},
    };
}

function makeTools(store) {
    const renderer = {
        draft: null, hoverId: null, painted: 0,
        schedule() { this.painted += 1; }, invalidate() {}, setEnabled() {},
    };
    const tools = new ctx.__Tools(
        { viewer: { viewer: fakeViewer() }, config: { width: 1000, height: 1000 } },
        store, renderer);
    tools.armed = true;
    // arm() is not called: it attaches document and window listeners this probe
    // has no use for. The store subscription IS what revalidation runs on, so
    // it is made by hand.
    tools.__unsubscribe = store.onChange(() => tools.revalidateHover());
    return { tools, renderer };
}

const checks = [];
const failures = [];

function check(name, actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

/** Move the pointer and let the throttled frame run, the way a browser would. */
function moveTo(tools, x, y) {
    tools.pointerMove({ position: { x, y } });
    const pending = frames.splice(0, frames.length);
    pending.forEach((fn) => fn());
}

const types = () => dispatched.map((e) => e.type.replace("plexora:roi-", ""));
const ids = () => dispatched.map((e) => e.detail.id);

// -- entering, staying, leaving ------------------------------------------

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools, renderer } = makeTools(store);

    moveTo(tools, 50, 50);
    check("entering a region announces it", types(), ["hover"]);
    check("...naming it", dispatched[0].detail.name, "Tumor 1");
    check("...and its id", dispatched[0].detail.id, "r-A");
    check("...and hands over the geometry itself",
        dispatched[0].detail.geometry, store.features[0].geometry);
    check("...and the renderer is told to emphasise it", renderer.hoverId, "r-A");

    // The bounding box is 0,0..100,100 in image pixels; the anchor has to be
    // where that lands on the SCREEN, which is offset by the canvas.
    check("the anchor is in client pixels", dispatched[0].detail.anchorRect, {
        left: CANVAS_OFFSET.left, top: CANVAS_OFFSET.top,
        right: CANVAS_OFFSET.left + 100, bottom: CANVAS_OFFSET.top + 100,
    });
    check("...with the image's own bounds alongside it",
        dispatched[0].detail.viewportRect.left, CANVAS_OFFSET.left);

    dispatched.length = 0;
    moveTo(tools, 60, 60);
    moveTo(tools, 70, 40);
    check("moving around inside one region says nothing more", types(), []);

    dispatched.length = 0;
    moveTo(tools, 150, 50);
    check("leaving announces the leave", types(), ["unhover"]);
    check("...naming the region left", ids(), ["r-A"]);
    check("...and the emphasis goes with it", renderer.hoverId, null);
}

// -- crossing straight from one region into another ----------------------

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);

    moveTo(tools, 50, 50);
    dispatched.length = 0;
    moveTo(tools, 250, 50);
    check("crossing between regions is a leave then an enter", types(), ["unhover", "hover"]);
    check("...in that order, naming both", ids(), ["r-A", "r-B"]);
}

// -- one hit test per frame ----------------------------------------------

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);

    tools.pointerMove({ position: { x: 50, y: 50 } });
    tools.pointerMove({ position: { x: 51, y: 51 } });
    tools.pointerMove({ position: { x: 52, y: 52 } });
    check("a burst of moves asks for one frame, not three", frames.length, 1);
    frames.splice(0, frames.length).forEach((fn) => fn());
    check("...and resolves to one hover", types(), ["hover"]);
}

// -- mid-gesture there is no hovering ------------------------------------

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);
    moveTo(tools, 50, 50);
    dispatched.length = 0;

    // A committed drag: the pointer is moving a shape, not inspecting one.
    tools.drag = { id: "r-A", origin: [50, 50] };
    moveTo(tools, 250, 50);
    check("a drag ends the hover", types(), ["unhover"]);
    check("...and does not start another one over what it crosses", ids(), ["r-A"]);

    dispatched.length = 0;
    tools.drag = null;
    tools.draftPoints = [[0, 0]];
    moveTo(tools, 250, 50);
    check("nor does a half-drawn shape", types(), []);
}

// -- the store moves under a stationary pointer --------------------------

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);
    moveTo(tools, 50, 50);
    dispatched.length = 0;

    // Renamed from the panel: same shape, so the card has nothing to recompute.
    store.feature("r-A").name = "Tumor 1 (renamed)";
    store.changed();
    check("a rename does not re-announce the region", types(), []);

    // Reshaped. Geometry objects are REPLACED, never mutated, which is the only
    // signal there is that the outline moved.
    store.feature("r-A").geometry = SQUARE(0, 0, 60);
    store.changed();
    check("a reshape re-announces it", types(), ["hover"]);
    check("...with the new outline", dispatched[0].detail.geometry.coordinates[0][2], [60, 60]);
    check("...and a new anchor", dispatched[0].detail.anchorRect.right, CANVAS_OFFSET.left + 60);

    // Its category was hidden from the legend: still stored, no longer drawn.
    dispatched.length = 0;
    store.hiddenCategories.add("c");
    store.changed();
    check("hiding a region ends the hover", types(), ["unhover"]);
    check("...naming it", ids(), ["r-A"]);
    check("...and drops the emphasis", tools.hoverId, null);
}

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);
    moveTo(tools, 50, 50);
    dispatched.length = 0;

    store.features = store.features.filter((f) => f.id !== "r-A");
    store.changed();
    check("deleting the hovered region ends the hover", types(), ["unhover"]);
    check("...naming the region that went", ids(), ["r-A"]);
}

// -- a region drawn in this session is hoverable at once -----------------

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);

    // What createFrom() leaves behind: a feature in the store, nothing else.
    store.features.push({
        id: "r-C", name: "Region 3", category_id: "c",
        geometry: SQUARE(400, 0, 80), flags: {},
    });
    moveTo(tools, 440, 40);
    check("a region drawn just now is hoverable", ids(), ["r-C"]);
}

// -- the picture moves under a stationary pointer -------------------------

/** Run whatever viewportMoved() scheduled, the way a browser frame would. */
function runFrames() {
    frames.splice(0, frames.length).forEach((fn) => fn());
}

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);
    moveTo(tools, 50, 50);
    check("a region is hovered to begin with", ids(), ["r-A"]);
    dispatched.length = 0;

    // A pan or a zoom with the pointer at rest. The old behaviour was for the
    // listener to close on this and never hear another word, because no pointer
    // event follows a viewport change -- so the summary could not be brought
    // back without leaving the region and re-entering it, which reads as a hover
    // the tool missed.
    tools.viewportMoved();
    runFrames();
    check("the picture moving re-announces the hover", types(), ["hover"]);
    check("...still naming the same region", ids(), ["r-A"]);
    check("...with an anchor, which is the point of re-announcing",
        Boolean(dispatched[0]?.detail?.anchorRect), true);

    // Several frames of a settling spring must not become several summaries.
    dispatched.length = 0;
    tools.viewportMoved();
    tools.viewportMoved();
    tools.viewportMoved();
    runFrames();
    check("a whole spring's worth of changes is one announcement", types().length, 1);
}

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);
    moveTo(tools, 50, 50);
    dispatched.length = 0;

    // Zooming can carry a shape out from under a pointer that never moved, so
    // what is under it is asked again rather than assumed. Moving the region is
    // the same thing seen from the other side.
    store.features[0].geometry = SQUARE(600, 600, 100);
    tools.viewportMoved();
    runFrames();
    check("a region that slid out from under the pointer stops being hovered",
        types(), ["unhover"]);
    check("...and nothing is hovered afterwards", tools.hoverId, null);
}

{
    dispatched.length = 0;
    const store = makeStore();
    const { tools } = makeTools(store);
    moveTo(tools, 900, 900);
    check("nothing is hovered out here", tools.hoverId, null);
    dispatched.length = 0;

    tools.viewportMoved();
    runFrames();
    check("the picture moving with nothing hovered says nothing", types(), []);
}

const report = {
    source: SOURCE.replace(`${REPO}/`, ""),
    checked: checks.length,
    failures,
};

console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
