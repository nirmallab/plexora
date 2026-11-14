/**
 * Does a lock stop the Select tool, and does one refused save stop all saving?
 *
 * A region (or its category) can be locked from the tree. The lock "blocks
 * moving, reshaping and deleting" (RoiStore.isLocked), and the server enforces
 * it: operations.py's _require_unlocked refuses roi.update_geometry and
 * roi.set_solid on a locked region with a 400. The client must therefore never
 * send one. The vertex handles honour the lock (hitTest only offers a vertex of
 * an unlocked selection), but a drag on the region's BODY used to become
 * editing.move without asking -- the region slid across the image, and the
 * drag's end committed a roi.update_geometry the server then refused.
 *
 * The second half is what that refusal did next. RoiStore.flush keeps a
 * refused batch in the queue, and every later edit re-sends the whole queue.
 * The server applies a batch atomically, so the refused operation took every
 * later edit down with it: nothing the user did afterwards was ever saved.
 *
 * Run directly:  node tests/js/roi_locked_drag_probe.mjs
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/roi/static");

const SQUARE = (x, y, size) => ({
    type: "Polygon",
    coordinates: [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]],
});

const checks = [];
const failures = [];
function check(name, actual, expected) {
    const a = JSON.stringify(actual), e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

// -- the Select tool and a locked region -----------------------------------

const toolsContext = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Infinity,
    console, Uint8Array, RegExp,
    RoiStore: { newId: (prefix) => `${prefix}-1` },
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    OpenSeadragon: {
        Point: class Point { constructor(x, y) { this.x = x; this.y = y; } },
        MouseTracker: class MouseTracker { destroy() {} },
    },
    CustomEvent: class CustomEvent {
        constructor(type, init) { this.type = type; this.detail = init?.detail; }
    },
    document: { activeElement: null, addEventListener() {}, removeEventListener() {} },
    window: {
        addEventListener() {}, removeEventListener() {},
        dispatchEvent() {}, PlexoraToolLoader: null,
    },
};
const tctx = createContext(toolsContext);
runInContext(readFileSync(join(STATIC, "roiGeometry.js"), "utf8"), tctx);
runInContext(readFileSync(join(STATIC, "roiTools.js"), "utf8")
    + "\n;globalThis.__Tools = RoiInteraction;", tctx);

/** A store whose isLocked is RoiStore's own rule: the region's flag or its
 *  category's. Everything committed is recorded, as the save queue would. */
function makeStore({ regionLocked = false, categoryLocked = false } = {}) {
    return {
        image: "default",
        editable: true,
        selectionId: null,
        features: [
            { id: "r-A", category_id: "c", geometry: SQUARE(0, 0, 100), flags: {},
              locked: regionLocked },
            { id: "r-B", category_id: "c2", geometry: SQUARE(200, 0, 100), flags: {} },
        ],
        categories: [
            { id: "c", label: "Tumor", color: "#fff", visible: true, locked: categoryLocked },
            { id: "c2", label: "Stroma", color: "#fff", visible: true, locked: false },
        ],
        committed: [],
        category(id) { return this.categories.find((c) => c.id === id) || null; },
        feature(id) { return this.features.find((f) => f.id === id) || null; },
        get selected() { return this.feature(this.selectionId); },
        get activeCategory() { return this.categories[0]; },
        countFor() { return 0; },
        isLocked(feature) {
            if (!feature) return true;
            if (feature.locked) return true;
            const category = this.category(feature.category_id);
            return Boolean(category && category.locked);
        },
        isVisible() { return true; },
        visibleFeatures() { return this.features; },
        select(id) { this.selectionId = id; },
        changed() {},
        commit(entry) {
            this.committed.push(entry);
            for (const op of entry.redo) this.applyLocal(op);
            return true;
        },
        applyLocal(op) {
            if (op.op === "roi.update_geometry") this.feature(op.id).geometry = op.geometry;
            if (op.op === "roi.set_solid") this.feature(op.id).solid = op.solid;
        },
    };
}

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
        canvas: { style: {} }, world,
        viewport: { getZoom: () => 1 },
        addHandler() {}, removeHandler() {},
    };
}

function makeTools(store) {
    const renderer = { draft: null, schedule() {}, invalidate() {}, setEnabled() {} };
    const tools = new tctx.__Tools(
        { viewer: { viewer: fakeViewer() }, config: { width: 1000, height: 1000 },
          view: { planes: () => null } },
        store, renderer);
    tools.armed = true;
    tools.said = [];
    tools.onNotify = (message) => tools.said.push(message);
    return tools;
}

const at = (x, y) => ({ position: { x, y }, preventDefaultAction: false });

for (const [what, lock] of [["a locked region", { regionLocked: true }],
                            ["a region in a locked category", { categoryLocked: true }]]) {
    const store = makeStore(lock);
    const tools = makeTools(store);
    tools.setTool("select");
    store.select("r-A");

    tools.press(at(50, 50));            // the body of r-A, well inside it
    tools.dragging(at(60, 70));
    const middle = store.feature("r-A").geometry.coordinates[0][0];
    tools.dragging(at(70, 90));
    tools.dragEnd(at(70, 90));

    check(`dragging the body of ${what} does not move it on screen`, middle, [0, 0]);
    check(`...nor leaves it moved after the drag`,
        store.feature("r-A").geometry.coordinates[0][0], [0, 0]);
    check(`...and sends the server nothing it will refuse (${what})`,
        store.committed.map((e) => e.redo.map((o) => o.op)), []);
}

{
    // The unlocked neighbour still moves: the lock is per region.
    const store = makeStore({ regionLocked: true });
    const tools = makeTools(store);
    tools.setTool("select");
    tools.press(at(250, 50));
    tools.dragging(at(260, 70));
    tools.dragEnd(at(260, 70));
    check("an unlocked region beside a locked one still moves",
        store.feature("r-B").geometry.coordinates[0][0], [210, 20]);
}

// -- one refused save and every edit after it --------------------------------

const timers = [];
const stateContext = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Promise,
    Error, TypeError, console, Uint8Array, Infinity,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    Blob: class Blob { constructor(parts) { this.parts = parts; } },
    RoiApi: { saveBlob: () => {} },
    window: {
        crypto: { randomUUID: () => `id${timers.length}` },
        PlexoraStatus: { begin: () => ({ done() {}, fail() {} }) },
    },
};
const sctx = createContext(stateContext);
runInContext(readFileSync(join(STATIC, "roiGeometry.js"), "utf8"), sctx);
runInContext(readFileSync(join(STATIC, "roiState.js"), "utf8")
    + "\n;globalThis.__Store = RoiStore;", sctx);

/** The server's rule, as operations.py applies it: a batch that moves a locked
 *  region is refused whole (400) and nothing in it is stored. */
class LockingApi {
    constructor() { this.revision = 0; this.stored = []; this.sent = []; }
    async getState() {
        return { ok: true, status: 200, data: {
            success: true, schema_version: 1, revision: this.revision, image: "default",
            categories: [{ id: "c", label: "Tumor", color: "#fff", visible: true,
                           locked: false, sort_order: 0 }],
            features: [{ id: "r-locked", category_id: "c", locked: true,
                         geometry: SQUARE(0, 0, 10), flags: {} }],
            coordinate_space: { width: 100, height: 100 },
            image_size: [100, 100], stored_image_size: [100, 100],
            dimension_mismatch: false,
        } };
    }
    async postOperations(baseRevision, operations) {
        this.sent.push(operations.map((o) => o.op));
        if (operations.some((o) => o.op === "roi.update_geometry" && o.id === "r-locked")) {
            return { ok: false, status: 400, data: { success: false,
                error: "ROI 'r-locked' is locked and cannot be moved" } };
        }
        this.stored.push(...operations.map((o) => o.feature?.id || o.id));
        this.revision = baseRevision + 1;
        return { ok: true, status: 200, data: { success: true, revision: this.revision } };
    }
}

{
    const api = new LockingApi();
    const store = new sctx.__Store({ datasource: "probe" }, api);
    await store.load();

    // What the Select tool's drag committed for the locked region.
    store.commit({
        label: "Edit ROI",
        redo: [{ op: "roi.update_geometry", image: "default", id: "r-locked",
                 geometry: SQUARE(5, 5, 10), flags: {} }],
        undo: [{ op: "roi.update_geometry", image: "default", id: "r-locked",
                 geometry: SQUARE(0, 0, 10), flags: {} }],
    });
    await store.flush();
    check("the refused move is reported", store.status, "failed");

    // The user goes on working: a new region, on an unlocked category.
    store.commit({
        label: "Draw ROI",
        redo: [{ op: "roi.create", image: "default",
                 feature: { id: "r-new", category_id: "c", geometry: SQUARE(50, 50, 10) } }],
        undo: [{ op: "roi.delete", image: "default", id: "r-new" }],
    });
    for (let i = 0; i < 3 && store.queue.length; i++) await store.flush();

    check("an edit made after a refused one still reaches the server",
        api.stored.includes("r-new"), true);
    check("...and the refused operation is not sent again with it",
        api.sent.slice(1).filter((ops) => ops.includes("roi.update_geometry")).length, 0);
}

const report = { source: "plexora/plugins/roi/static", checked: checks.length, failures };
console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
