/**
 * Picking a cell with the right button, and the base image's eye.
 *
 *   1. **A right or middle release throws nothing.** imageViewer.js's
 *      canvas tracker read `that.selectButton`, which nothing assigns, on
 *      every non-primary release.
 *   2. **The right-click pick reads the reference item's own source.**
 *      `viewer.source` is world item 0's (OpenSeadragon reassigns it on every
 *      add, remove and reorder), and item 0 can be a layer's ground, which
 *      has no getImagePixel.
 *   3. **Reopening the base image's eye keeps the view** however long it was
 *      closed: the view remembered at the hide expires after 5 s, and an
 *      emptied world fits the whole slide when the next item lands.
 *
 * The code is sliced from the shipped files and run with stubs.
 *
 * Run directly:  node tests/js/viewer_pick_reference_probe.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

const failures = [];
function check(label, ok, detail = "") {
    console.log(`${ok ? "ok" : "FAIL"} - ${label}${ok || !detail ? "" : `  ${detail}`}`);
    if (!ok) failures.push(label);
}

function between(source, startMarker, endMarker, file) {
    const start = source.indexOf(startMarker);
    if (start < 0) throw new Error(`${startMarker.trim()} not found in ${file}`);
    const end = source.indexOf(endMarker, start);
    if (end < 0) throw new Error(`could not find the end of ${startMarker.trim()} in ${file}`);
    return source.slice(start, end);
}

// -- 1 and 2: the pick handlers --------------------------------------------------------

const imageViewer = readFileSync(join(VIEWS, "imageViewer.js"), "utf8");
const handlers = between(imageViewer, "        // Add event mouse handler (cell selection)",
    "        // ONE pass, not one per world item.", "imageViewer.js");

const added = {};
let trackerOptions = null;
const asked = [];
const triggered = [];
const reference = {
    source: {
        getImagePixel(tiledImage, position) {
            asked.push({ tiledImage, position });
            return [position.x, position.y];
        },
    },
    viewportToImageCoordinates: (p) => p,
};
const self = {
    viewer: {
        canvas: {},
        addHandler(name, fn) { added[name] = fn; },
    },
    referenceItem: () => reference,
    numericData: { getNearestCell: (x, y) => Promise.resolve({ id: 7, x, y }) },
    eventHandler: { trigger: (name, payload) => triggered.push({ name, payload }) },
};
const OpenSeadragon = {
    MouseTracker: function MouseTracker(options) { trackerOptions = options; },
};
const ImageViewer = { events: { imageClickedMultiSel: "multi" } };
new Function("OpenSeadragon", "ImageViewer", "PlexoraViewTransform", "config", handlers)
    .call(self, OpenSeadragon, ImageViewer, { pointFromPixel: (_v, p) => p }, { extraZoomLevels: 0 });

// 1. A right release, with or without anything else going on.
let threw = null;
try {
    trackerOptions?.nonPrimaryReleaseHandler?.({ position: { x: 1, y: 1 }, button: 2,
                                                 originalEvent: { ctrlKey: false } });
} catch (error) {
    threw = error;
}
check("a right or middle release on the image throws nothing", threw === null, String(threw));

// 2. A right press while world item 0 is a layer's ground (no getImagePixel).
const groundSource = { layerId: "L1", srcIdx: "L1:ground" };
let pickError = null;
try {
    globalThis.event = undefined;
    await added["canvas-nonprimary-press"]?.({
        button: 2,
        position: { x: 10, y: 20 },
        eventSource: { source: groundSource },
        originalEvent: { ctrlKey: false },
    });
} catch (error) {
    pickError = error;
}
check("a right-click pick works when item 0 is a layer's ground", pickError === null, String(pickError));
check("...reading the pixel through the reference item's own source",
    asked.length === 1 && asked[0].tiledImage === reference);
check("...and selects the nearest cell", triggered.length === 1 && triggered[0].payload.item?.id === 7);

// -- 3: the base image's eye ------------------------------------------------------------

const viewerManager = readFileSync(join(VIEWS, "viewerManager.js"), "utf8");
const methods = [
    between(viewerManager, "    rememberView() {", "    /**", "viewerManager.js"),
    between(viewerManager, "    restoreView() {", "    /**", "viewerManager.js"),
    between(viewerManager, "    hideReference() {", "    /**", "viewerManager.js"),
    between(viewerManager, "    showReference() {", "    /**", "viewerManager.js"),
].join("\n");

let clock = 0;
const timeouts = [];
const fakeSetTimeout = (fn, ms) => { timeouts.push({ fn, at: clock + ms }); return timeouts.length; };
const fakeClearTimeout = (id) => { if (timeouts[id - 1]) timeouts[id - 1].fn = null; };
const advance = (ms) => {
    clock += ms;
    for (const t of timeouts) if (t.fn && t.at <= clock) { const fn = t.fn; t.fn = null; fn(); }
};

const Manager = new Function("setTimeout", "clearTimeout", "PlexoraLayerStack", `
    return class Manager {
${methods}
    };
`)(fakeSetTimeout, fakeClearTimeout, { REFERENCE_LAYER_ID: "reference" });

const world = [];
const view = { center: { x: 0.3, y: 0.4 }, zoom: 8 };
const home = { center: { x: 0.5, y: 0.5 }, zoom: 1 };
const manager = Object.create(Manager.prototype);
Object.assign(manager, {
    pendingView: null,
    pendingViewTimer: 0,
    referenceHidden: false,
    imageViewer: { config: { image_kind: "multiplex" } },
    channelList: { currentChannels: { 0: {} } },
    layerStack: { get: () => ({ items: world.slice() }) },
    dropStandby() {},
    dropWorldItems(items) { for (const item of items) world.splice(world.indexOf(item), 1); },
    viewer: {
        world: { getItemCount: () => world.length, getItemAt: (i) => world[i] },
        viewport: {
            getCenter: () => ({ ...view.center }),
            getZoom: () => view.zoom,
            zoomTo(z) { view.zoom = z; },
            panTo(c) { view.center = { ...c }; },
        },
        forceRedraw() {},
    },
    // channel_add: the item lands in an empty world, so OSD goes home first,
    // then this class's success callback runs restoreView.
    channel_add(srcIdx) {
        const empty = world.length === 0;
        world.push({ source: { layerId: "reference", srcIdx } });
        if (empty) { view.center = { ...home.center }; view.zoom = home.zoom; }
        this.restoreView();
    },
});
world.push({ source: { layerId: "reference", srcIdx: 0 } });

manager.hideReference();
check("closing the eye empties the world", world.length === 0);
advance(6000);
manager.showReference();
check("an eye reopened after more than 5 s keeps the view",
    view.zoom === 8 && view.center.x === 0.3 && view.center.y === 0.4, JSON.stringify(view));

view.center = { x: 0.3, y: 0.4 };
view.zoom = 8;
manager.hideReference();
advance(1000);
manager.showReference();
check("...and one reopened at once keeps it too",
    view.zoom === 8 && view.center.x === 0.3, JSON.stringify(view));

console.log(failures.length ? `\nFAILURES: ${failures.join(", ")}` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
