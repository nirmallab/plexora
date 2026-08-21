/**
 * Does a click on the image do what a click should?
 *
 * The bug this exists to catch, found by driving the real app:
 *
 * `canvas-press` decided the gesture immediately -- a press that landed on a
 * shape set the machine to `editing.move` there and then. But OpenSeadragon
 * fires press for a CLICK too, and `canvas-click` is guarded on `idle.select`,
 * so after clicking any shape the guard was false forever after. Clicking a
 * second shape did nothing; clicking empty space to deselect did nothing. The
 * machine sat in `editing.move` with no drag in progress.
 *
 * It was invisible in every obvious check. Selection LOOKED like it worked,
 * because drawing a shape selects it as a side effect -- so the panel showed a
 * selection, the outline was highlighted, and the first thing anyone tries
 * (draw, then look at the selected shape) behaved correctly. Only clicking a
 * SECOND shape reveals it. No Python test executes this file, and the state
 * probe next door covers saving rather than pointer handling.
 *
 * The fix is the state machine's shape: a press records what is under the
 * pointer, and the gesture is decided by whichever arrives next -- a drag
 * (move/edit) or a click (select). This drives that sequence directly.
 *
 * Run directly:  node tests/js/roi_interaction_probe.mjs
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

/** Two shapes side by side, which is the arrangement the bug needs. */
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
            if (op.op === "roi.update_geometry") this.feature(op.id).geometry = op.geometry;
            if (op.op === "roi.delete") this.features = this.features.filter((f) => f.id !== op.id);
        },
    };
}

const context = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Infinity,
    console, Uint8Array,
    setTimeout: () => 1, clearTimeout: () => {},
    document: { activeElement: null, addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {}, PlexoraToolLoader: null },
};
const ctx = createContext(context);
runInContext(readFileSync(join(STATIC, "roiGeometry.js"), "utf8"), ctx);
runInContext(readFileSync(SOURCE, "utf8") + "\n;globalThis.__Tools = RoiInteraction;", ctx);

/** A viewer whose world exists and whose pixels ARE image pixels, so the
 *  numbers in this file are the numbers the tools see. */
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

function makeTools(store, viewer = fakeViewer()) {
    const renderer = { draft: null, schedule() {}, invalidate() {}, setEnabled() {} };
    const tools = new ctx.__Tools(
        { viewer: { viewer }, config: { width: 1000, height: 1000 } },
        store, renderer);
    tools.armed = true;
    tools.said = [];
    tools.onNotify = (message) => tools.said.push(message);
    return tools;
}

const checks = [];
const failures = [];
function check(name, actual, expected) {
    const a = JSON.stringify(actual), e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

/** For the checks whose whole point is that nothing throws. Without this a
 *  regression kills the process and the report never reaches the caller. */
function survives(name, fn) {
    checks.push(name);
    try {
        fn();
    } catch (error) {
        failures.push({ check: name, expected: '"no error"', actual: JSON.stringify(String(error)) });
    }
}

const at = (x, y) => ({ position: { x, y }, preventDefaultAction: false });

// -- click to select, twice ---------------------------------------------

{
    const store = makeStore();
    const tools = makeTools(store);
    tools.setTool("select");

    // Click shape A.
    tools.press(at(50, 50));
    tools.click(at(50, 50));
    check("clicking a shape selects it", store.selectionId, "r-A");
    check("...and leaves the machine idle, not mid-drag", tools.state, "idle.select");

    // Click shape B. This is the one that was broken: the machine was stuck in
    // editing.move, so the click handler's guard was false and nothing happened.
    tools.press(at(250, 50));
    tools.click(at(250, 50));
    check("clicking a SECOND shape selects that one", store.selectionId, "r-B");

    // Click empty space.
    tools.press(at(700, 700));
    tools.click(at(700, 700));
    check("clicking empty space deselects", store.selectionId, null);
    check("...and still leaves the machine idle", tools.state, "idle.select");
}

// -- drag still moves ----------------------------------------------------

{
    const store = makeStore();
    const tools = makeTools(store);
    tools.setTool("select");

    tools.press(at(50, 50));
    check("a press alone does not commit to a gesture", tools.state, "idle.select");

    const drag = at(60, 70);
    tools.dragging(drag);
    check("the first movement commits to a move", tools.state, "editing.move");
    check("...and takes the pointer from the viewer", drag.preventDefaultAction, true);

    tools.dragging(at(70, 90));
    const end = at(70, 90);
    tools.dragEnd(end);
    check("the drag ends back at idle", tools.state, "idle.select");
    check("...having moved the shape", store.feature("r-A").geometry.coordinates[0][0], [20, 40]);
    check("...as exactly one undo step", store.committed.length, 1);
    check("...recorded as one geometry operation",
        store.committed[0].redo.map((o) => o.op), ["roi.update_geometry"]);
}

// -- dragging empty space is left to the viewer --------------------------

{
    const store = makeStore();
    const tools = makeTools(store);
    tools.setTool("select");

    tools.press(at(700, 700));
    const drag = at(720, 720);
    tools.dragging(drag);
    check("dragging empty image space stays idle", tools.state, "idle.select");
    // Not suppressed, so OpenSeadragon pans -- navigation keeps working without
    // having to leave the Select tool.
    check("...and lets the viewer pan", drag.preventDefaultAction, false);
}

// -- a vertex wins over the body it sits on ------------------------------

{
    const store = makeStore();
    const tools = makeTools(store);
    tools.setTool("select");
    store.select("r-A");

    tools.press(at(100, 100));           // a corner of shape A
    tools.dragging(at(110, 110));
    check("dragging a handle edits that vertex", tools.state, "editing.vertex");

    tools.dragEnd(at(110, 110));
    check("...moving only that corner",
        store.feature("r-A").geometry.coordinates[0][2], [110, 110]);
    check("...and leaving its neighbour alone",
        store.feature("r-A").geometry.coordinates[0][1], [100, 0]);
}

// -- Space hands the pointer back to the viewer --------------------------

{
    const store = makeStore();
    const tools = makeTools(store);
    tools.setTool("select");

    tools.keyDown({ key: " ", preventDefault() {}, ctrlKey: false, metaKey: false, shiftKey: false });
    check("holding Space enters temporary pan", tools.spaceHeld, true);

    tools.press(at(50, 50));
    check("...so a press on a shape pans instead of moving it", tools.state, "panning.temporary");
    const drag = at(60, 60);
    tools.dragging(drag);
    check("...and the viewer keeps the drag", drag.preventDefaultAction, false);

    tools.keyUp({ key: " " });
    check("releasing Space restores the previous tool", tools.spaceHeld, false);
    check("...and the previous state", tools.state, "idle.select");
}

// -- drawing waits for a category, selecting does not --------------------

{
    // A brand new project: no categories, because the user names their own.
    const store = makeStore();
    store.categories = [];
    store.features = [];
    const tools = makeTools(store);

    check("with no categories, nothing can be drawn", tools.canDraw, false);
    // The distinction that matters: the pointer still works, so a user can
    // still select, delete, and -- the case that would really hurt -- undo the
    // deletion of the last category.
    check("...but the pointer still works", tools.ready, true);

    tools.setTool("rectangle");
    tools.press(at(50, 50));
    tools.dragging(at(90, 90));
    check("a rectangle drag starts no draft", tools.draftPoints.length, 0);

    tools.setTool("polygon");
    tools.click(at(50, 50));
    check("a polygon click places no vertex", tools.draftPoints.length, 0);
    check("...and the user is told why rather than left guessing",
        tools.said.some((m) => m.includes("category")), true);
}

{
    // The same tools the moment a category exists.
    const store = makeStore();
    const tools = makeTools(store);
    check("with one, drawing is on", tools.canDraw, true);

    tools.setTool("rectangle");
    tools.press(at(300, 300));
    tools.dragging(at(400, 400));
    check("...and a rectangle drag builds a draft", tools.draftPoints.length, 4);
}

// -- a gesture cancelled with the button still down ----------------------

{
    // Esc, a tool shortcut and a panel switch all cancel mid-drag, and the
    // mouse goes on sending drags until the user lets go. The state string
    // outlives the gesture it described, so the handler has to cope with a
    // movement that has nothing behind it.
    const store = makeStore();
    const tools = makeTools(store);
    tools.setTool("rectangle");

    tools.press(at(300, 300));
    tools.dragging(at(400, 400));
    tools.cancelDraft();
    survives("dragging on after a cancelled RECTANGLE does not throw",
        () => tools.dragging(at(500, 500)));
    check("...and adds nothing", tools.draftPoints.length, 0);

    // The same shape of problem one state along.
    tools.setTool("select");
    tools.press(at(50, 50));
    tools.dragging(at(60, 60));
    check("a move is under way", tools.state, "editing.move");
    tools.cancelDraft();
    survives("dragging on after a cancelled MOVE does not throw",
        () => tools.dragging(at(400, 400)));
    check("...and leaves the shape where it was",
        store.feature("r-A").geometry.coordinates[0][0], [10, 10]);
}

// -- the first tile arriving is news the panel has to hear ---------------

{
    // Whether a shape can be drawn depends on the world holding an image, which
    // is not a fact about the annotations -- so no store change announces it. A
    // panel opened while the first tile is still on its way renders a disabled
    // toolbar, and on a project that already has categories nothing ever edits
    // the store to repaint it. The tools sit dead with no error anywhere.
    const store = makeStore();
    let repaints = 0;
    store.changed = () => { repaints += 1; };

    const viewer = fakeViewer();
    viewer.world.items = 0;                    // tiles still loading
    const tools = makeTools(store, viewer);
    tools.armed = false;
    tools.arm();

    check("with an empty world nothing is drawable", tools.canDraw, false);
    check("arming subscribes to the world", viewer.world.handlers.has("add-item"), true);

    repaints = 0;
    viewer.world.items = 1;
    viewer.world.handlers.get("add-item")?.();
    check("the image arriving repaints the panel", repaints > 0, true);
    check("...and now it is drawable", tools.canDraw, true);

    tools.disarm();
    check("disarming unsubscribes again", viewer.world.handlers.has("add-item"), false);
}

const report = { source: SOURCE.replace(REPO + "/", ""), checked: checks.length, failures };
console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
