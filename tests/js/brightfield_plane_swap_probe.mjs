/**
 * A colour (brightfield / H&E) z-stack changes plane the way a channel stack
 * does: the revision waits for the slide, and the slide on screen is the plane
 * the revision asked for.
 *
 * A colour stack has no channel pairs (`currentChannels` is empty); its plane
 * is the one true-colour base item, rebuilt at the new plane by
 * `ViewerManager.swapBrightfieldBase` and handed over by
 * `handOverBrightfieldBase` once it can draw. What is pinned here:
 *
 *   **A navigation revision waits for the slide.** `setView(spec, rev)`
 *   resolves to its commit only once the new plane's base item can draw, so
 *   the readout, the fade and the 3-D outlines settle with the slide rather
 *   than before it (the comment above `baseSwap` in setView says so).
 *
 *   **A quick second step leaves the newest plane on screen.** OpenSeadragon
 *   answers `addTiledImage` later (its tile source resolves in a timer), so a
 *   second step can start while the first plane's item has not landed. The
 *   superseded plane's item, landing late, must not be handed over in the
 *   newer build's place, and two base items must never end up on screen.
 *
 * The world here answers `addTiledImage` only when told to (`land`), the way
 * OSD does a task later.
 *
 * Run directly:  node tests/js/brightfield_plane_swap_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

function deepCopy(value) {
    if (Array.isArray(value)) return value.map(deepCopy);
    if (!value || typeof value !== "object") return value;
    if (Object.getPrototypeOf(value) !== Object.prototype) return value;
    return Object.fromEntries(Object.entries(value).map(([key, held]) => [key, deepCopy(held)]));
}

const stackContext = createContext({ console, window: {}, globalThis: {} });
runInContext(readFileSync(join(VIEWS, "layerStack.js"), "utf8"), stackContext);
globalThis.PlexoraLayerStack = stackContext.window.PlexoraLayerStack
    || stackContext.PlexoraLayerStack;
globalThis.window = globalThis;
globalThis.CustomEvent = class CustomEvent {
    constructor(type, options) { this.type = type; this.detail = options?.detail; }
};
globalThis.dispatchEvent = () => true;
globalThis.addEventListener = () => {};
globalThis.document = {
    getElementById: () => null,
    createElement: () => ({ width: 0, height: 0, getContext: () => ({ fillRect() {} }),
                            toDataURL: () => "data:image/png;base64,stub" }),
};

// The base hand-over's 8 s deadline would keep node alive (and fire after the
// checks); every wait here is driven by the items' own loaded flags.
const realSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms) => (ms && ms > 1000 ? 0 : realSetTimeout(fn, ms));

const module = await import(pathToFileURL(join(VIEWS, "viewerManager.js")).href);
const ViewerManager = module.default || module.ViewerManager;
const RGB = module.RGB_TILE_FORMAT;

const tick = () => new Promise((resolve) => realSetTimeout(resolve, 0));

/** A revision as viewState makes one (tests/js/plane_swap_probe.mjs). */
function revision(id, spec) {
    const listeners = [];
    const rev = {
        id, spec, scrub: false, superseded: false,
        onSupersede(fn) { if (rev.superseded) fn(rev); else listeners.push(fn); },
        supersede() { rev.superseded = true; for (const fn of listeners.splice(0)) fn(rev); },
    };
    return rev;
}

/** A brightfield project at plane z10, its base on screen and drawn. Items
 *  added later wait in `queued` until `land(view)` puts them on the world
 *  and answers `success`, as OSD does a task after addTiledImage. */
function makeBrightfield() {
    const items = [];
    const queued = [];
    const handlers = new Set();
    const world = {
        items,
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        getIndexOfItem: (item) => items.indexOf(item),
        setItemIndex: () => {},
        addHandler(name, fn) { if (name === "remove-item") handlers.add(fn); },
        removeHandler(name, fn) { if (name === "remove-item") handlers.delete(fn); },
        removeItem(item) {
            const at = items.indexOf(item);
            if (at < 0) return;
            items.splice(at, 1);
            for (const fn of [...handlers]) fn({ item });
        },
    };
    const viewer = {
        world,
        viewport: { getCenter: () => ({ x: 0.5, y: 0.5 }), getZoom: () => 1,
                    zoomTo() {}, panTo() {} },
        forceRedraw() {},
        raiseEvent() {},
        addHandler() {},
        removeHandler() {},
        addTiledImage(options) {
            const own = new Map();
            const item = {
                source: deepCopy(options.tileSource),
                options,
                opacity: options.opacity,
                loaded: false,
                setOpacity(value) { item.opacity = value; },
                getFullyLoaded: () => item.loaded,
                addHandler(name, fn) {
                    if (!own.has(name)) own.set(name, new Set());
                    own.get(name).add(fn);
                },
                removeHandler(name, fn) { own.get(name)?.delete(fn); },
                ready() {
                    item.loaded = true;
                    for (const fn of [...(own.get("fully-loaded-change") || [])]) fn({ fullyLoaded: true });
                },
            };
            queued.push(item);
        },
    };
    const land = (view) => {
        const at = queued.findIndex((item) => item.source.view === view);
        const [item] = queued.splice(at, 1);
        items.push(item);
        item.options.success?.({ item });
        return item;
    };
    const channelList = { currentChannels: {}, rangeConnector: {}, colorConnector: {} };
    const manager = new ViewerManager(
        { viewer, config: { width: 1000, height: 1000, extraZoomLevels: 0, maxLevel: 3,
                            tileWidth: 256, tileHeight: 256, image_kind: "brightfield",
                            imageData: [{ src: "/tiles/rgb/" }] },
          layerStack: new globalThis.PlexoraLayerStack.LayerStack({ viewer }),
          numericData: { bitRange: [0, 255] } },
        channelList);
    manager.viewer = viewer;
    manager.presetView("z10");
    manager.load_brightfield_base();
    land("z10").ready();
    return { manager, world, land };
}

const bases = (world) => world.items.filter((item) => item.source?.tileFormat === RGB);

{
    const { manager, world, land } = makeBrightfield();
    check("the colour stack's base is on screen at z10",
        bases(world).length === 1 && bases(world)[0].source.view === "z10");

    let answered = false;
    const prepared = manager.setView("z11", revision(1, "z11"));
    prepared.then(() => { answered = true; });
    await tick();
    await tick();
    check("a revision on a colour stack waits for the slide's new plane",
        !answered,
        "setView resolved its commit while the z11 base had not even landed: the "
        + "readout, the fade and the 3-D outlines move before the slide does");

    const fresh = land("z11");
    fresh.ready();
    await prepared;
    check("...and the new plane replaces the old once it can draw",
        bases(world).length === 1 && bases(world)[0].source.view === "z11");
}

{
    // Two quick steps: z10 -> z11 -> z12, the second asked for before the
    // z11 base has landed. viewState starts the second revision once the
    // first has answered (and commits it), or supersedes the first at its
    // deadline if it has not: either way the second begins now.
    const { manager, world, land } = makeBrightfield();
    const rev1 = revision(1, "z11");
    let commit1;
    manager.setView("z11", rev1).then((commit) => { commit1 = commit; });
    await tick();
    if (typeof commit1 === "function") commit1(rev1);
    else rev1.supersede();
    const rev2 = revision(2, "z12");
    let commit2;
    const second = manager.setView("z12", rev2).then((commit) => { commit2 = commit; });

    // OSD answers the first plane's addTiledImage late, then the second's.
    land("z11").ready();
    await tick();
    land("z12").ready();
    await second;
    await tick();
    if (typeof commit2 === "function") commit2(rev2);
    const shown = bases(world).map((item) => item.source.view);
    check("a quick second step leaves the newer plane's slide on screen",
        shown.length === 1 && shown[0] === "z12",
        `the slide shows ${JSON.stringify(shown)} while the view says z12: the `
        + "superseded z11 item was handed over in the z12 build's place");
}

{
    // The same two steps, both items landing before either can draw.
    const { manager, world, land } = makeBrightfield();
    const rev1 = revision(1, "z11");
    let commit1;
    manager.setView("z11", rev1).then((commit) => { commit1 = commit; });
    await tick();
    if (typeof commit1 === "function") commit1(rev1);
    else rev1.supersede();
    const rev2 = revision(2, "z12");
    let commit2;
    const second = manager.setView("z12", rev2).then((commit) => { commit2 = commit; });

    const first = land("z11");
    const newer = land("z12");
    first.ready();
    newer.ready();
    await second;
    await tick();
    if (typeof commit2 === "function") commit2(rev2);
    const shown = bases(world).map((item) => item.source.view);
    check("two quick steps never leave two base images on screen",
        shown.length === 1 && shown[0] === "z12",
        `base items on the world: ${JSON.stringify(shown)}`);
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks failed:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nbrightfield plane swap probe: all ${checks} checks passed`);
