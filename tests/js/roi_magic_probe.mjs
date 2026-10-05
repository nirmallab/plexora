/**
 * Magic select in the ROI tool: roiTools.js's "magic" tool (key E).
 *
 * A click is a point, the server's model turns it into an outline, and the
 * outline is stored as a region -- one undo step, filed under the active
 * category, remembering that magic select drew it (`flags.method: "sam"`,
 * provenance the panels translate into words). Further clicks grow the
 * outline being made, Shift-clicks carve it, Esc ends it. What is pinned is
 * where that could go wrong without anything throwing:
 *
 *   - arming the tool not starting the one-time setup (ensureReady), or
 *     starting it more than once;
 *   - a second click making a SECOND region instead of refining the first;
 *   - a click with nowhere to put the region, or on a locked one, still
 *     costing a request -- and saying nothing;
 *   - an empty answer stored as a region;
 *   - E not toggling back to the tool that was in hand;
 *   - the refactor that sent createFrom through commitNew dropping the
 *     other tools' provenance, or a vertex drag wiping magic select's;
 *   - the floating bar's modes: in Add and Remove a drag must still pan
 *     (no box, the viewer keeps the drag), only Box draws a box, Remove (or
 *     Shift in Add) takes away; a mode change keeps the outline being made;
 *   - the seed: carving an existing region starts from its own outline
 *     (`maskGeometry`), and a plain refine click carries none;
 *   - the bar shown with the tool, hidden with any other or on disarm, and
 *     its close going back to the tool in hand.
 *
 * Built on roi_interaction_probe.mjs's harness (its store, viewer and
 * renderer stand-ins), with the REAL segmentService.js's planner and Session
 * and a fake `point()` that answers a square around the click, and a
 * recording PlexoraMagicBar.
 *
 * Run directly: `node tests/js/roi_magic_probe.mjs`
 * Prints `ok  <check>` per check held; exit 1 if any did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/roi/static");
const SERVICES = join(REPO, "plexora/client/src/js/services");

const SQUARE = (x, y, size) => ({
    type: "Polygon",
    coordinates: [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]],
});

/** roi_interaction_probe.mjs's store: two shapes side by side, one category. */
function makeStore() {
    return {
        image: "default",
        editable: true,
        selectionId: null,
        features: [
            { id: "r-A", category_id: "c", geometry: SQUARE(0, 0, 100), flags: {} },
            { id: "r-B", category_id: "c", geometry: SQUARE(200, 0, 100), flags: {} },
        ],
        categories: [{ id: "c", label: "Tumor", color: "#fff", visible: true, locked: false }],
        committed: [],
        category(id) { return this.categories.find((c) => c.id === id) || null; },
        feature(id) { return this.features.find((f) => f.id === id) || null; },
        get selected() { return this.feature(this.selectionId); },
        get activeCategory() { return this.categories[0]; },
        countFor() { return 0; },
        isLocked() { return false; },
        isVisible() { return true; },
        visibleFeatures() { return this.features; },
        select(id) { this.selectionId = id; },
        changed() {},
        commit(entry) { this.committed.push(entry); for (const op of entry.redo) this.applyLocal(op); return true; },
        applyLocal(op) {
            if (op.op === "roi.create") this.features.push(op.feature);
            if (op.op === "roi.update_geometry") {
                const feature = this.feature(op.id);
                feature.geometry = op.geometry;
                if (op.flags) feature.flags = op.flags;
            }
            if (op.op === "roi.delete") this.features = this.features.filter((f) => f.id !== op.id);
        },
    };
}

let ids = 0;
const window = {
    addEventListener() {}, removeEventListener() {},
    dispatchEvent() {}, PlexoraToolLoader: null,
};
const context = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Infinity,
    console, Uint8Array, RegExp, Promise, URLSearchParams,
    RoiStore: { newId: (prefix) => `${prefix}-${++ids}` },
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    OpenSeadragon: {
        Point: class Point { constructor(x, y) { this.x = x; this.y = y; } },
        MouseTracker: class MouseTracker { destroy() {} },
    },
    CustomEvent: class CustomEvent {
        constructor(type, init) { this.type = type; this.detail = init?.detail; }
    },
    document: { activeElement: null, addEventListener() {}, removeEventListener() {},
                querySelector: () => null },
    window,
};
const ctx = createContext(context);
runInContext(readFileSync(join(SERVICES, "segmentService.js"), "utf8"), ctx,
             { filename: "segmentService.js" });
const RealSegment = window.PlexoraSegment;
runInContext(readFileSync(join(STATIC, "roiGeometry.js"), "utf8"), ctx);
runInContext(readFileSync(join(STATIC, "roiTools.js"), "utf8")
             + "\n;globalThis.__Tools = RoiInteraction;", ctx, { filename: "roiTools.js" });

/**
 * The planner and Session are segmentService's own; the request is not.
 * `answer(call)` builds each reply -- by default a square around the last
 * point, a little larger every call so every refine is a real change.
 */
function fakeSegment(answer) {
    const fake = {
        ...RealSegment,
        calls: [],
        ready: 0,
        forgot: 0,
        ensureReady() { fake.ready += 1; return Promise.resolve(true); },
        forget() { fake.forgot += 1; },
        point(request) {
            const copy = JSON.parse(JSON.stringify({ ...request, retry: undefined }));
            fake.calls.push(copy);
            return Promise.resolve(answer(copy, fake.calls.length));
        },
    };
    return fake;
}

const around = (request, n) => {
    const b = request.box;
    const p = request.points.filter((q) => q.label === 1).at(-1) || request.points.at(-1)
        || { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    const half = 20 + 5 * n;
    return {
        ok: true, geometry: SQUARE(p.x - half, p.y - half, 2 * half),
        bbox: { x: p.x - half, y: p.y - half, width: 2 * half, height: 2 * half },
        flags: { empty: false, touches_edge: false, too_large: false },
    };
};

const SNAPSHOT = { sample: "s1", viewport: { x: 0, y: 0, width: 1000, height: 1000 },
                   channels: [] };
window.PlexoraViewSnapshot = { capture: () => SNAPSHOT, sameView: () => true };

/** PlexoraMagicBar, recording: what was shown, for whom, and what it was told. */
const bar = {
    log: [],
    current: null,
    show(options) {
        bar.current = options;
        bar.log.push(["show", options.mode]);
        return { setMode: (m) => bar.log.push(["mode", m]),
                 setBusy: (b) => bar.log.push(["busy", b]),
                 hide: () => { bar.log.push(["hide"]); bar.current = null; },
                 isShown: () => bar.current === options };
    },
    hide() { bar.log.push(["hide"]); bar.current = null; },
};
window.PlexoraMagicBar = bar;

function fakeViewer() {
    const world = {
        handlers: new Map(),
        items: 1,
        getItemCount() { return this.items; },
        getItemAt: () => ({ source: { getImagePixel: (_, p) => [p.x, p.y] } }),
        addHandler(name, fn) { this.handlers.set(name, fn); },
        removeHandler(name) { this.handlers.delete(name); },
    };
    return {
        canvas: { style: {} },
        world,
        viewport: { getZoom: () => 1 },
        addHandler() {}, removeHandler() {},
    };
}

function makeTools(store, segment = fakeSegment(around)) {
    window.PlexoraSegment = segment;
    const renderer = { draft: null, prompts: null, schedule() {}, invalidate() {},
                       setEnabled() {} };
    const tools = new ctx.__Tools(
        { viewer: { viewer: fakeViewer() }, config: { width: 1000, height: 1000 },
          datasource: "s1" },
        store, renderer);
    tools.armed = true;
    // Magic select is the tool a fresh panel holds; each check here turns it
    // on deliberately (setTool) to watch what that does, so start on the pen.
    tools.setTool("freehand");
    segment.ready = 0;
    bar.log = [];
    bar.current = null;
    tools.said = [];
    tools.onNotify = (message) => tools.said.push(message);
    return { tools, renderer, segment };
}

const click = (x, y, shift = false) => ({ position: { x, y }, quick: true,
                                          preventDefaultAction: false,
                                          originalEvent: { shiftKey: shift } });
const key = (k, extra = {}) => ({ key: k, preventDefault() {}, ctrlKey: false, metaKey: false,
                                  shiftKey: false, altKey: false, repeat: false, ...extra });
const at = (x, y) => ({ position: { x, y }, preventDefaultAction: false });

/** Let runMagic's awaits finish. */
const settled = () => new Promise((resolve) => setImmediate(resolve));

/** Equal values across the vm's realm (its objects have other prototypes). */
function same(actual, expected) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

const failures = [];
async function check(name, fn) {
    try {
        await fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failures.push(name);
        console.log(`FAIL  ${name}\n      ${String(error && error.message || error).split("\n").join("\n      ")}`);
    }
}

// -- the default tool, and the pan hint --------------------------------------

function freshTools(status) {
    const segment = fakeSegment(around);
    segment.status = () => Promise.resolve(status);
    window.PlexoraSegment = segment;
    const renderer = { draft: null, prompts: null, schedule() {}, invalidate() {},
                       setEnabled() {} };
    const store = makeStore();
    const tools = new ctx.__Tools(
        { viewer: { viewer: fakeViewer() }, config: { width: 1000, height: 1000 },
          datasource: "s1" },
        store, renderer);
    tools.said = [];
    tools.onNotify = (message) => tools.said.push(message);
    return { tools, segment, store };
}

const hintLog = [];
window.PlexoraCanvasHint = {
    show(owner, options) { hintLog.push(["show", options.key, options.text]); return true; },
    hide() { hintLog.push(["hide"]); },
};

await check("a fresh panel holds magic select in Box, and arming it starts the setup", async () => {
    const { tools, segment } = freshTools({ state: "weights_missing" });
    assert.equal(tools.tool, "magic");
    assert.equal(tools.magicMode, "box");
    tools.arm();
    await settled();
    assert.equal(tools.tool, "magic");
    assert.equal(segment.ready, 1);
    same(bar.current?.mode, "box");
    tools.disarm();
});

await check("...and a server that cannot run the model gets freehand instead", async () => {
    const { tools, segment } = freshTools({ state: "not_installed_runtime" });
    tools.arm();
    await settled();
    assert.equal(tools.tool, "freehand");
    assert.equal(segment.ready, 0);
    tools.disarm();
});

await check("'Hold Space to pan' is up while a drawing tool is in hand, and only then", async () => {
    const { tools } = freshTools({ state: "ready" });
    hintLog.length = 0;
    tools.arm();
    same(hintLog.at(-1), ["show", "Space", "Hold to pan"]);
    tools.setTool("select");
    same(hintLog.at(-1), ["hide"]);
    tools.setTool("freehand");
    same(hintLog.at(-1)[0], "show");
    tools.disarm();
    same(hintLog.at(-1), ["hide"]);
    await settled();
});

// -- one outline, made and refined -----------------------------------------

{
    const store = makeStore();
    const { tools, renderer, segment } = makeTools(store);

    await check("arming magic select starts the one-time setup, once", () => {
        tools.setTool("magic");
        tools.setMagicMode("add");   // Box is the default; this check clicks
        tools.setTool("magic");
        tools.setMagicMode("add");   // Box is the default; this check clicks
        assert.equal(tools.tool, "magic");
        assert.equal(tools.state, "drawing.magic");
        assert.equal(segment.ready, 1);
        same(tools.said, []);
    });

    await check("a click makes one region, filed as magic select's, and selects it", async () => {
        const event = click(500, 500);
        tools.click(event);
        assert.equal(event.preventDefaultAction, true);
        await settled();
        assert.equal(segment.calls.length, 1);
        same(segment.calls[0].points, [{ x: 500, y: 500, label: 1 }]);
        assert.equal(segment.calls[0].datasource, "s1");
        assert.equal(store.committed.length, 1);
        const [entry] = store.committed;
        assert.equal(entry.label, "Magic select");
        same(entry.redo.map((o) => o.op), ["roi.create"]);
        const feature = entry.redo[0].feature;
        assert.equal(feature.flags.method, "sam");
        assert.equal(feature.category_id, "c");
        assert.equal(feature.name, "Tumor 1");
        assert.equal(store.selectionId, feature.id);
        same(renderer.prompts, [{ x: 500, y: 500, label: 1 }]);
    });

    await check("a second click near it refines that region, not a new one", async () => {
        const made = store.committed[0].redo[0].feature.id;
        tools.click(click(530, 500));
        await settled();
        assert.equal(segment.calls.length, 2);
        same(segment.calls[1].points.map((p) => p.label), [1, 1]);
        assert.equal(segment.calls[1].usePrevious, true);
        assert.equal(store.committed.length, 2);
        const entry = store.committed[1];
        assert.equal(entry.label, "Refine ROI (magic select)");
        same(entry.redo.map((o) => o.op), ["roi.update_geometry"]);
        assert.equal(entry.redo[0].id, made);
        assert.equal(entry.redo[0].flags.method, "sam");
        assert.equal(store.features.filter((f) => f.flags.method === "sam").length, 1);
    });

    await check("a Shift-click adds an excluding point to the same outline", async () => {
        tools.click(click(510, 510, true));
        await settled();
        same(segment.calls[2].points.at(-1), { x: 510, y: 510, label: 0 });
        same(renderer.prompts.at(-1), { x: 510, y: 510, label: 0 });
        assert.equal(store.committed.at(-1).label, "Refine ROI (magic select)");
    });

    await check("Esc ends the outline being made", () => {
        const forgot = segment.forgot;
        tools.keyDown(key("Escape"));
        assert.equal(renderer.prompts, null);
        assert.equal(tools.magic, null);
        assert.ok(segment.forgot > forgot);
        assert.equal(tools.tool, "magic");
    });

    await check("...so the next click far away starts a new region", async () => {
        store.select(null);
        tools.click(click(800, 800));
        await settled();
        assert.equal(store.committed.at(-1).label, "Magic select");
        assert.equal(store.features.filter((f) => f.flags.method === "sam").length, 2);
    });
}

// -- refusals cost no request ------------------------------------------------

await check("with no category and nothing selected, a click says why and asks nothing", async () => {
    const store = makeStore();
    store.categories = [];
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.said.length = 0;
    tools.click(click(500, 500));
    await settled();
    assert.equal(segment.calls.length, 0);
    assert.equal(tools.said.length, 1);
    assert.ok(tools.said[0].includes("Add a category first"), tools.said[0]);
    assert.equal(store.committed.length, 0);
});

await check("a click inside a locked selected region says it is locked and asks nothing", async () => {
    const store = makeStore();
    store.isLocked = (feature) => feature.id === "r-A";
    store.select("r-A");
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.click(click(50, 50));
    await settled();
    assert.equal(segment.calls.length, 0);
    assert.ok(tools.said.some((m) => m.includes("locked")), JSON.stringify(tools.said));
    assert.equal(store.committed.length, 0);
});

await check("a click inside a selected region refines it, with no category needed", async () => {
    const store = makeStore();
    store.categories = [];
    store.select("r-A");
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    same(tools.said, []);
    tools.click(click(50, 50));
    await settled();
    assert.equal(segment.calls.length, 1);
    assert.ok(segment.calls[0].box, "the region's own box goes with the click");
    assert.equal(segment.calls[0].maskGeometry ?? null, null,
                 "a plain refine carries no seed");
    assert.equal(store.committed.length, 1);
    assert.equal(store.committed[0].label, "Refine ROI (magic select)");
    assert.equal(store.committed[0].redo[0].id, "r-A");
});

await check("an empty answer stores nothing and says so", async () => {
    const store = makeStore();
    const { tools, renderer, segment } = makeTools(store, fakeSegment((request, n) => ({
        ...around(request, n), flags: { empty: true } })));
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.click(click(500, 500));
    await settled();
    assert.equal(segment.calls.length, 1);
    assert.equal(store.committed.length, 0);
    assert.ok(tools.said.some((m) => m.startsWith("Nothing found there")),
              JSON.stringify(tools.said));
    assert.equal(tools.magic, null);
    assert.equal(renderer.prompts, null);
});

await check("a click while one is still outlining is ignored", async () => {
    const store = makeStore();
    const pending = fakeSegment(() => new Promise(() => {}));
    const { tools } = makeTools(store, pending);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.click(click(500, 500));
    await settled();
    tools.click(click(520, 500));
    await settled();
    assert.equal(pending.calls.length, 1);
    assert.equal(tools.magicBusy, true);
});

await check("Remove inside a selected region carves it, seeded with its own outline", async () => {
    const store = makeStore();
    store.select("r-A");
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    bar.current.onMode("remove");
    assert.equal(tools.magicMode, "remove");
    tools.click(click(50, 50));
    await settled();
    assert.equal(segment.calls.length, 1);
    same(segment.calls[0].points, [{ x: 50, y: 50, label: 0 }]);
    same(segment.calls[0].maskGeometry, SQUARE(0, 0, 100));
    assert.ok(segment.calls[0].box);
    assert.equal(store.committed.at(-1).redo[0].id, "r-A");
    assert.equal(store.committed.at(-1).label, "Refine ROI (magic select)");
});

await check("...and so does a Shift-click in Add", async () => {
    const store = makeStore();
    store.select("r-A");
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.click(click(50, 50, true));
    await settled();
    same(segment.calls[0].points, [{ x: 50, y: 50, label: 0 }]);
    same(segment.calls[0].maskGeometry, SQUARE(0, 0, 100));
});

await check("Remove with nothing to take from says so and asks nothing", async () => {
    const store = makeStore();
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("remove");
    tools.click(click(500, 500));
    await settled();
    assert.equal(segment.calls.length, 0);
    assert.ok(tools.said.some((m) => m.startsWith("Remove takes an area out")),
              JSON.stringify(tools.said));
});

// -- Box, and drags that pan ---------------------------------------------------

await check("in Add a drag pans: no box, and the viewer keeps the drag", async () => {
    const store = makeStore();
    const { tools, renderer, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");
    tools.press(at(300, 300));
    assert.equal(tools.drag, null);
    const drag = at(400, 400);
    tools.dragging(drag);
    assert.equal(drag.preventDefaultAction, false);
    assert.equal(tools.draftPoints.length, 0);
    assert.equal(renderer.draft, null);
    tools.dragEnd(at(400, 400));
    await settled();
    assert.equal(segment.calls.length, 0);
});

await check("in Box a drag draws a box and prompts with it", async () => {
    const store = makeStore();
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("box");
    tools.press(at(300, 300));
    const drag = at(400, 380);
    tools.dragging(drag);
    assert.equal(drag.preventDefaultAction, true);
    assert.equal(tools.draftPoints.length, 4);
    tools.dragEnd(at(400, 380));
    await settled();
    assert.equal(segment.calls.length, 1);
    same(segment.calls[0].box, { x: 300, y: 300, width: 100, height: 80 });
    same(segment.calls[0].points, []);
    assert.equal(store.committed.at(-1).label, "Magic select");
});

await check("...and a click in Box does nothing", async () => {
    const store = makeStore();
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("box");
    tools.click(click(500, 500));
    await settled();
    assert.equal(segment.calls.length, 0);
    assert.equal(store.committed.length, 0);
});

function scribble(tools, from, to, { y = 500, step = 10, shift = false } = {}) {
    tools.press({ ...at(from, y), originalEvent: { shiftKey: shift } });
    const moves = [];
    for (let x = from + step; x <= to; x += step) {
        const move = at(x, y);
        tools.dragging(move);
        moves.push(move);
    }
    const end = at(to, y);
    tools.dragEnd(end);
    return { moves, end };
}

await check("in Scribble a drag is a line, sent as up to eight include points along it", async () => {
    const store = makeStore();
    const { tools, renderer, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("scribble");
    tools.press({ ...at(300, 500), originalEvent: { shiftKey: false } });
    const move = at(400, 500);
    tools.dragging(move);
    assert.equal(move.preventDefaultAction, true);
    assert.equal(renderer.draft.tool, "scribble");
    for (let x = 410; x <= 650; x += 10) tools.dragging(at(x, 500));
    const end = at(650, 500);
    tools.dragEnd(end);
    assert.equal(end.preventDefaultAction, true);
    assert.equal(renderer.draft, null);
    await settled();
    assert.equal(segment.calls.length, 1);
    const points = segment.calls[0].points;
    assert.equal(points.length, 8);
    assert.ok(points.every((p) => p.label === 1 && p.y === 500), JSON.stringify(points));
    assert.equal(points[0].x, 300);
    assert.equal(points.at(-1).x, 650);
    assert.equal(store.committed.length, 1);
    assert.equal(store.committed[0].label, "Magic select");
    assert.equal(store.committed[0].redo[0].feature.flags.method, "sam");
});

await check("a Shift-scribble begun inside the outline being made takes its line away", async () => {
    const store = makeStore();
    store.features = [];
    const { tools, renderer, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.click(click(500, 500));
    await settled();
    tools.setMagicMode("scribble");
    scribble(tools, 490, 520, { shift: true });
    await settled();
    assert.equal(segment.calls.length, 2);
    const second = segment.calls[1].points;
    assert.equal(second[0].label, 1);
    assert.ok(second.slice(1).length >= 1 && second.slice(1).every((p) => p.label === 0),
              JSON.stringify(second));
    assert.equal(store.committed.at(-1).redo[0].op, "roi.update_geometry");
});

await check("a scribble that finds nothing is taken back whole", async () => {
    const store = makeStore();
    const { tools, renderer, segment } = makeTools(store, fakeSegment((request, n) => ({
        ...around(request, n), flags: { empty: true } })));
    tools.setTool("magic");
    tools.setMagicMode("scribble");
    scribble(tools, 300, 600);
    await settled();
    assert.equal(segment.calls.length, 1);
    assert.ok(segment.calls[0].points.length > 1);
    assert.equal(store.committed.length, 0);
    assert.equal(tools.magic, null);
    assert.equal(renderer.prompts, null);
});

await check("...and a tap with the brush is one point", async () => {
    const store = makeStore();
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("scribble");
    tools.click(click(700, 700));
    await settled();
    assert.equal(segment.calls.length, 1);
    same(segment.calls[0].points, [{ x: 700, y: 700, label: 1 }]);
});

// -- the bar ---------------------------------------------------------------------

await check("the bar shows with magic select (in Box) and goes with any other tool", () => {
    const store = makeStore();
    const { tools } = makeTools(store);
    tools.setMagicMode("scribble");
    tools.setTool("magic");
    assert.equal(tools.magicMode, "box");
    same(bar.log[0], ["show", "box"]);
    assert.equal(bar.current.owner, tools);
    tools.setTool("select");
    assert.equal(bar.current, null);
});

await check("a mode from the bar keeps the outline being made", async () => {
    const store = makeStore();
    const { tools, segment } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.click(click(500, 500));
    await settled();
    const made = tools.magic;
    assert.ok(made);
    bar.current.onMode("remove");
    assert.equal(tools.magic, made);
    assert.ok(bar.log.some(([kind, value]) => kind === "mode" && value === "remove"));
    tools.click(click(510, 500));
    await settled();
    same(segment.calls[1].points.map((p) => p.label), [1, 0]);
    assert.equal(store.committed.at(-1).label, "Refine ROI (magic select)");
});

await check("the bar's close goes back to the tool in hand", () => {
    const store = makeStore();
    const { tools } = makeTools(store);
    tools.setTool("rectangle");
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    bar.current.onClose();
    assert.equal(tools.tool, "rectangle");
    assert.equal(bar.current, null);
});

await check("disarming hides the bar; arming with magic in hand shows it again", () => {
    const store = makeStore();
    const { tools } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.disarm();
    assert.equal(bar.current, null);
    tools.arm();
    assert.ok(bar.current, "shown again on arm");
    assert.equal(bar.current.owner, tools);
});

await check("busy is shown on the bar while an outline is on its way", async () => {
    const store = makeStore();
    const { tools } = makeTools(store);
    tools.setTool("magic");
    tools.setMagicMode("add");   // Box is the default; this check clicks
    tools.click(click(500, 500));
    await settled();
    const busy = bar.log.filter(([kind]) => kind === "busy").map(([, value]) => value);
    same(busy.slice(-2), [true, false]);
});

// -- E ---------------------------------------------------------------------------

await check("E toggles magic select on and back to the tool in hand", () => {
    const store = makeStore();
    const { tools, segment } = makeTools(store);
    assert.equal(tools.tool, "freehand");
    tools.keyDown(key("e"));
    assert.equal(tools.tool, "magic");
    assert.equal(segment.ready, 1);
    tools.keyDown(key("E"));
    assert.equal(tools.tool, "freehand");
    tools.setTool("select");
    tools.keyDown(key("e"));
    assert.equal(tools.tool, "magic");
    tools.keyDown(key("e"));
    assert.equal(tools.tool, "select");
    tools.keyDown(key("e", { repeat: true }));
    assert.equal(tools.tool, "select");
});

// -- the other tools' provenance -----------------------------------------------

await check("a freehand stroke is still filed as freehand, self-intersection recorded", () => {
    const store = makeStore();
    store.features = [];
    const { tools } = makeTools(store);
    tools.press(at(10, 10));
    tools.dragging(at(110, 10));
    tools.dragging(at(110, 110));
    tools.dragging(at(10, 110));
    tools.dragEnd(at(10, 10));
    const feature = store.committed[0]?.redo?.[0]?.feature;
    assert.ok(feature, "a region was drawn");
    assert.equal(store.committed[0].label, "Draw ROI");
    assert.equal(feature.flags.method, "freehand");
    assert.equal("self_intersecting" in feature.flags, true);
    assert.equal(feature.flags.self_intersecting, false);
});

await check("...and a rectangle as rectangle", () => {
    const store = makeStore();
    store.features = [];
    const { tools } = makeTools(store);
    tools.setTool("rectangle");
    tools.press(at(300, 300));
    tools.dragging(at(400, 400));
    tools.dragEnd(at(400, 400));
    assert.equal(store.committed[0]?.redo?.[0]?.feature?.flags?.method, "rectangle");
});

await check("drawing into a hidden category shows it again, in the same undoable step", async () => {
    const store = makeStore();
    store.categories[0].visible = false;
    store.applyLocal = ((base) => function (op) {
        if (op.op === "category.update") Object.assign(this.category(op.id), op.changes);
        base.call(this, op);
    })(store.applyLocal);
    const { tools } = makeTools(store);
    tools.setTool("rectangle");
    tools.press(at(300, 300));
    tools.dragging(at(400, 400));
    tools.dragEnd(at(400, 400));
    const [entry] = store.committed;
    same(entry.redo.map((o) => o.op), ["category.update", "roi.create"]);
    same(entry.redo[0].changes, { visible: true });
    same(entry.undo.map((o) => o.op), ["roi.delete", "category.update"]);
    same(entry.undo[1].changes, { visible: false });
    assert.equal(store.categories[0].visible, true);
    assert.ok(tools.said.some((m) => /hidden/.test(m)), tools.said.join(" | "));
});

await check("dragging a vertex of a magic-select region keeps it magic select's", () => {
    const store = makeStore();
    store.features.push({ id: "r-S", category_id: "c", geometry: SQUARE(500, 500, 100),
                          flags: { self_intersecting: false, method: "sam" } });
    store.select("r-S");
    const { tools } = makeTools(store);
    tools.setTool("select");
    tools.press(at(600, 600));
    tools.dragging(at(610, 610));
    assert.equal(tools.state, "editing.vertex");
    tools.dragEnd(at(610, 610));
    const entry = store.committed.at(-1);
    assert.equal(entry.label, "Edit ROI");
    assert.equal(entry.redo[0].flags.method, "sam");
    assert.equal(entry.undo[0].flags.method, "sam");
});

process.exit(failures.length ? 1 : 0);
