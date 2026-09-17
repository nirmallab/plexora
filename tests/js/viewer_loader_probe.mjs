/**
 * When the viewer's spinner is on screen, and when it is not.
 *
 * The bug this file is the fence around: "in many instances I just see a blank
 * page without the loading animation in the center of the viewer". The loader
 * used to be hidden by default and shown only by ImageViewer.setLoading, which
 * is wired to the centroid and segmentation fetches -- nothing at all showed it
 * while the image tiles themselves were arriving, which is the wait the user
 * actually notices. It is now driven by what is in the viewer's world and
 * whether any of it has drawn, and that is a set of decisions that is invisible
 * in review and miserable to check by hand: you would have to open a project on
 * a slow disk and watch a black rectangle at exactly the right moment.
 *
 * The decisions, and what each one costs when it is wrong:
 *
 *   - **Up by default.** The markup renders visible so the spinner is there
 *     before a script runs; nothing in this file may hide it on the way in.
 *   - **A layer in the world with nothing drawn is loading.** This is the whole
 *     blank-screen case.
 *   - **The first drawn tile is the end of it**, not fully-loaded -- a spinner
 *     over an image the user can already see is worse than none.
 *   - **Explicit holds are ref-counted.** Two overlapping `setLoading` pairs
 *     used to cancel each other, which is the same blank screen with extra
 *     steps.
 *   - **Boot settles a turn late.** OSD queues tile-source construction in a
 *     bare setTimeout, so a synchronous settle sees an empty world and blinks
 *     the spinner off and straight back on.
 *
 * Run against the shipped file in a stand-in small enough to read: a fake
 * element that records every visibility change, and a fake viewer whose events
 * this file fires by hand.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/viewerLoader.js");

/**
 * A loader element, a viewer, and the service loaded fresh against both.
 *
 * Fresh per check because the service holds module state -- how many holds are
 * out, whether anything has drawn -- and that state is the thing under test.
 *
 * @param present false for a page with no loader markup at all: the quick-view
 *   landing, Settings, any plugin page. Every entry point has to be a no-op
 *   there rather than a thrown error that takes the page down with it.
 * @param tileDrawn what viewer.addHandler("tile-drawn", ...) answers. OSD
 *   returns false from a drawer that does not raise the event.
 */
function boot({ present = true, tileDrawn = true } = {}) {
    const classes = new Set();
    //: Every change of visibility, in order. A blink is two entries where
    //: there should be none, and is invisible to a check that only looks at
    //: the final state.
    const shown = [];
    const loader = {
        id: "openseadragon_loader",
        classList: {
            add(name) {
                if (classes.has(name)) return;
                classes.add(name);
                if (name === "is-hidden") shown.push(false);
            },
            remove(name) {
                if (!classes.has(name)) return;
                classes.delete(name);
                if (name === "is-hidden") shown.push(true);
            },
            contains: (name) => classes.has(name),
        },
    };

    let items = 0;
    const handlers = { viewer: {}, world: {} };
    const viewer = {
        addHandler(type, fn) {
            handlers.viewer[type] = fn;
            return type === "tile-drawn" ? tileDrawn : true;
        },
        world: {
            getItemCount: () => items,
            addHandler(type, fn) { handlers.world[type] = fn; },
        },
    };

    const globals = {
        setTimeout,
        console: { error() {}, warn() {}, log() {} },
        document: {
            getElementById: (id) =>
                (present && id === "openseadragon_loader" ? loader : null),
        },
    };
    globals.window = {};
    globals.globalThis = globals;
    const context = createContext(globals);
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });

    return {
        loader: globals.window.PlexoraViewerLoader,
        viewer,
        shown,
        visible: () => !classes.has("is-hidden"),
        addItem() { items += 1; handlers.world["add-item"]?.({}); },
        removeItem() { items -= 1; handlers.world["remove-item"]?.({}); },
        drawTile() { handlers.viewer["tile-drawn"]?.({}); },
        failTile() { handlers.viewer["tile-load-failed"]?.({}); },
    };
}

/** One macrotask turn, which is all settle() defers by. */
const turn = () => new Promise((resolve) => setTimeout(resolve, 0));

const said = [];
const check = (label, fn) => { fn(); said.push(label); console.log(label); };
const checkAsync = async (label, fn) => { await fn(); said.push(label); console.log(label); };

// -- the decisions ------------------------------------------------------------

check("the spinner the page rendered is left up", () => {
    const app = boot();
    assert.equal(app.visible(), true, "loading the file hid the spinner");
    app.loader.watch(app.viewer);
    assert.equal(app.visible(), true,
        "watching an empty, un-booted viewer hid the spinner the page rendered");
    assert.deepEqual(app.shown, [], "the spinner was touched before anything happened");
});

check("a layer that has not drawn yet keeps it up", () => {
    const app = boot();
    app.loader.watch(app.viewer);
    app.loader.settle();          // even once boot is on its way out
    app.addItem();
    assert.equal(app.visible(), true, "a channel with no tiles on screen showed nothing");
});

check("the first drawn tile takes it down", () => {
    const app = boot();
    app.loader.watch(app.viewer);
    app.addItem();
    app.drawTile();
    assert.equal(app.visible(), false, "the spinner stayed over a visible image");
    app.drawTile();
    assert.deepEqual(app.shown, [false],
        "every subsequent tile re-hid an already hidden spinner");
});

check("explicit work puts it back up, and releasing takes it down", () => {
    const app = boot();
    app.loader.watch(app.viewer);
    app.addItem();
    app.drawTile();
    const release = app.loader.hold();
    assert.equal(app.visible(), true, "work started with the image up showed nothing");
    release();
    assert.equal(app.visible(), false, "the spinner outlived the work");
});

check("two overlapping holds do not cancel each other", () => {
    // The old bare boolean: setLoading(false) from the inner pair hid the
    // spinner while the outer one was still waiting.
    const app = boot();
    app.loader.watch(app.viewer);
    app.addItem();
    app.drawTile();
    const outer = app.loader.hold();
    const inner = app.loader.hold();
    inner();
    assert.equal(app.visible(), true, "the inner release ended the outer work's spinner");
    outer();
    assert.equal(app.visible(), false, "the spinner outlived both");
});

check("releasing twice counts once", () => {
    const app = boot();
    app.loader.watch(app.viewer);
    app.addItem();
    app.drawTile();
    const first = app.loader.hold();
    const second = app.loader.hold();
    first();
    first();
    assert.equal(app.visible(), true,
        "a release called twice cancelled somebody else's hold");
    second();
    assert.equal(app.visible(), false);
});

await checkAsync("an emptied world shows it again until the next layer draws", async () => {
    // A resolution swap tears every layer down and rebuilds it. Without this,
    // the second build is as blank and as silent as the first one used to be.
    const app = boot();
    app.loader.watch(app.viewer);
    app.loader.settle();
    await turn();
    app.addItem();
    app.drawTile();
    assert.equal(app.visible(), false);

    app.removeItem();
    assert.equal(app.visible(), false, "an empty world on purpose showed a spinner");
    app.addItem();
    assert.equal(app.visible(), true, "the rebuild was as silent as the first build");
    app.drawTile();
    assert.equal(app.visible(), false);
});

await checkAsync("an empty world after boot is not loading", async () => {
    // Every channel switched off is the viewer showing exactly what was asked
    // for. A spinner there would never stop.
    const app = boot();
    app.loader.watch(app.viewer);
    app.loader.settle();
    assert.equal(app.visible(), true, "boot settled synchronously, before OSD's own timers");
    await turn();
    assert.equal(app.visible(), false, "an empty viewer span forever");
});

await checkAsync("boot settling under a layer that has not drawn leaves it up", async () => {
    const app = boot();
    app.loader.watch(app.viewer);
    app.addItem();
    app.loader.settle();
    await turn();
    assert.equal(app.visible(), true, "boot finishing hid the spinner over a blank canvas");
});

await checkAsync("a layer arriving in the same turn as boot never blinks it", async () => {
    // The ordering this defends: OSD builds a tile source inside a bare
    // setTimeout, so the add-item for every channel viewerSidebar asked for is
    // already queued when main.js's promise settles. A synchronous settle
    // hides the spinner in that gap and shows it again milliseconds later --
    // and under prefers-reduced-motion, where the fade is 0ms, that is a
    // visible flash.
    const app = boot();
    app.loader.watch(app.viewer);
    setTimeout(() => app.addItem(), 0);   // queued first, exactly like OSD's
    app.loader.settle();
    await turn();
    await turn();
    assert.equal(app.visible(), true, "the spinner left the screen with nothing drawn");
    assert.deepEqual(app.shown, [], "the spinner blinked off and back on");
});

check("a tile that fails to load does not leave it spinning", () => {
    // The navbar chip and the resource banner report the failure. A spinner on
    // top of that message says the app is still trying when it is not.
    const app = boot();
    app.loader.watch(app.viewer);
    app.addItem();
    app.failTile();
    assert.equal(app.visible(), false, "a failed tile left the spinner running forever");
});

check("a drawer that never draws tiles hides on the layer arriving", () => {
    // The RGB quick view uses OSD's default drawer list, which is WebGL in
    // every browser that has it, and WebGL raises no tile-drawn. Second best,
    // and wrong only for as long as the first tile takes.
    const app = boot({ tileDrawn: false });
    app.loader.watch(app.viewer);
    assert.equal(app.visible(), true);
    app.addItem();
    assert.equal(app.visible(), false, "a drawer with no tile-drawn span forever");
});

await checkAsync("a page with no loader in it is left alone", async () => {
    const app = boot({ present: false });
    app.loader.watch(app.viewer);
    app.addItem();
    app.drawTile();
    const release = app.loader.hold();
    release();
    app.loader.settle();
    await turn();
    assert.deepEqual(app.shown, [], "a page with no viewer had its markup touched");
});

console.log(`\n${said.length} checks passed`);
