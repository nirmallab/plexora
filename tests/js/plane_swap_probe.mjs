/**
 * A plane swap changes every channel in the same frame, behind one fade.
 *
 * `ViewerManager.setView` rebuilds each reference channel at the new plane
 * while the old keeps drawing. What is pinned here is what happens when the
 * new pairs become ready at different moments -- which is always:
 *
 *   **Nothing moves until every channel can draw.** One channel committed on
 *   its own is a picture with that channel at the new plane over the others at
 *   the old: a mixed frame no file holds.
 *
 *   **Then all of them move at once**, and the picture on screen is held for
 *   the fade (`planeFade.capture`) BEFORE the first old pair leaves -- held
 *   after, the ghost would be a copy of half a swap.
 *
 *   **A channel switched off mid-swap does not hold the others.** Its
 *   replacement leaves the world and never becomes loaded; without the guard
 *   every other channel would sit at the old plane for as long as it waits.
 *
 *   **No deadline hands over to nothing.** A plane that never covers the
 *   view keeps the old one on screen (it used to be dropped at 8 s, and the
 *   ground -- black -- showed until tiles came); the status says "slow" at
 *   2 s, and the swap goes the moment the new plane covers. Nor does a
 *   plane whose every tile failed for good (all holes): the old plane stays,
 *   the status says "error", and `retry()` hands over once a tile lands.
 *
 *   **While the hand moves, a preview.** A revision asked for mid-drag
 *   (`rev.scrub`) commits as soon as any level covers the view; at rest the
 *   same coverage waits out the 140 ms grace.
 *
 *   **It says how it is going** (`plexora:surface-status`, surface "main"):
 *   fetching -> preview -> refining -> idle, counted over each channel's
 *   paint item, at most once a frame; "error" when what is left failed for
 *   good, kept until `retry()` (tileFetch.retryFailed); the revealed items
 *   listened to only until then, and never past the next swap.
 *
 * Run directly:  node tests/js/plane_swap_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

const failures = [];
function check(label, ok, why) {
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

const module = await import(pathToFileURL(join(VIEWS, "viewerManager.js")).href);
const ViewerManager = module.default || module.ViewerManager;

/**
 * OpenSeadragon's two maps for one item (as tests/js/coverage_ready_probe.mjs
 * fakes them): `coverage[level][x][y]` for the tiles each level needs on
 * screen, `tilesMatrix[level][x][y]` for the tiles. Level 0 is coarse (one
 * tile), level 2 fine (four) -- the level the view is shown at.
 */
function tileModel(item) {
    const tile = () => ({ exists: true, loaded: false });
    item.coverage = { 0: { 0: { 0: false } },
                      2: { 0: { 0: false, 1: false }, 1: { 0: false, 1: false } } };
    item.tilesMatrix = { 0: { 0: { 0: tile() } },
                         2: { 0: { 0: tile(), 1: tile() }, 1: { 0: tile(), 1: tile() } } };
    const fine = () => [[0, 0], [0, 1], [1, 0], [1, 1]].map(([x, y]) => item.tilesMatrix[2][x][y]);
    item.loadCoarse = () => { item.tilesMatrix[0][0][0].loaded = true; };
    item.loadFine = (count) => { for (const one of fine().slice(0, count)) one.loaded = true; };
    // The last fine tile failed with its retries spent (tileFetch.js `fail`).
    item.failFine = () => {
        const one = fine()[3];
        one.loaded = false;
        one.exists = false;
        one._plexoraFailed = "final";
    };
    item.loadAll = () => {
        item.loadCoarse();
        for (const one of fine()) {
            one.loaded = true;
            one.exists = true;
            delete one._plexoraFailed;
        }
        item.ready();
    };
}

/** A world whose items load when told to, and that says when one leaves.
 *  `tiles`: each item also has OSD's coverage maps (tileModel). */
function makeManager(channels, { tiles = false } = {}) {
    const items = [];
    const handlers = new Set();
    //: The viewer's own events (`tile-loaded`, `tile-load-failed`).
    const viewerHandlers = new Map();
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
        addHandler(name, fn) {
            if (!viewerHandlers.has(name)) viewerHandlers.set(name, new Set());
            viewerHandlers.get(name).add(fn);
        },
        removeHandler(name, fn) { viewerHandlers.get(name)?.delete(fn); },
        raise(name, event) { for (const fn of [...(viewerHandlers.get(name) || [])]) fn(event); },
        handlerCount: (name) => viewerHandlers.get(name)?.size || 0,
        addTiledImage(options) {
            const own = new Map();
            const item = {
                source: deepCopy(options.tileSource),
                options,
                opacity: options.opacity,
                blend: options.compositeOperation,
                loaded: false,
                setOpacity(value) { item.opacity = value; },
                setPreload() {},
                allowZombieCache() { item.zombie = true; },
                getFullyLoaded: () => item.loaded,
                addHandler(name, fn) {
                    if (!own.has(name)) own.set(name, new Set());
                    own.get(name).add(fn);
                },
                removeHandler(name, fn) { own.get(name)?.delete(fn); },
                handlerCount: (name) => own.get(name)?.size || 0,
                ready() {
                    item.loaded = true;
                    for (const fn of [...(own.get("fully-loaded-change") || [])]) fn({ fullyLoaded: true });
                },
            };
            if (tiles) tileModel(item);
            items.push(item);
            options.success?.({ item });
        },
    };
    const imageData = channels.map((name) => ({ src: `/tiles/${name}/` }));
    const channelList = { currentChannels: {}, rangeConnector: {}, colorConnector: {} };
    channels.forEach((name, srcIdx) => {
        channelList.rangeConnector[srcIdx] = [0, 255];
        channelList.colorConnector[srcIdx] = { color: "#ffffff" };
    });
    const manager = new ViewerManager(
        { viewer, config: { width: 1000, height: 1000, extraZoomLevels: 0, maxLevel: 3,
                            tileWidth: 256, tileHeight: 256, imageData },
          layerStack: new globalThis.PlexoraLayerStack.LayerStack({ viewer }),
          numericData: { bitRange: [0, 255] } },
        channelList);
    manager.viewer = viewer;
    manager.presetView("z10");
    channels.forEach((_, srcIdx) => manager.channel_add(srcIdx));
    for (const item of items) (tiles ? item.loadAll() : item.ready());
    return { manager, world, items, viewer };
}

// The real timer, kept: the blocks at the end run the page's on a fake clock.
const realSetTimeout = globalThis.setTimeout;
const tick = () => new Promise((resolve) => realSetTimeout(resolve, 0));
const at = (items, view) => items.filter((item) => item.source.view === view);

{
    const { manager, world, items } = makeManager(["dapi", "cd3", "cd8"]);
    const log = [];
    manager.planeFade = {
        capture() {
            log.push({ what: "capture", old: at(world.items, "z10").length,
                       fresh: at(world.items, "z11").length });
            return true;
        },
        release() { log.push({ what: "release", old: at(world.items, "z10").length }); },
    };
    const swap = manager.setView("z11");
    await tick();
    const fresh = at(items, "z11");
    check("a swap adds a new pair per channel, invisible, beside the old",
        fresh.length === 6 && at(items, "z10").length === 6
        && fresh.every((item) => item.opacity === 0));

    // Two of three channels ready: still the old plane everywhere.
    for (const item of fresh.slice(0, 4)) item.ready();
    await tick();
    check("two channels ready of three is not a swap",
        at(world.items, "z10").length === 6 && fresh.every((item) => item.opacity === 0)
        && log.length === 0,
        "committed alone, a channel shows the new plane over the others at the old");

    for (const item of fresh.slice(4)) item.ready();
    await swap;
    check("the last channel ready swaps them all in one go",
        world.items.length === 6 && at(world.items, "z11").length === 6
        && at(world.items, "z11").every((item) => item.opacity === 1));
    check("the picture is held for the fade before any old pair leaves",
        log[0]?.what === "capture" && log[0].old === 6,
        "a ghost taken after the first commit is a copy of half a swap");
    check("and the fade starts after every pair is in",
        log[1]?.what === "release" && log[1].old === 0 && log.length === 2);
    check("the plane left behind keeps its cache (zombie) for a step back",
        at(items, "z10").every((item) => item.zombie === true));
}

{
    const { manager, world, items } = makeManager(["dapi", "cd3"]);
    let captured = 0;
    manager.planeFade = { capture: () => { captured += 1; return true; }, release() {} };
    const started = Date.now();
    const swap = manager.setView("z11");
    await tick();
    const fresh = at(items, "z11");
    for (const item of fresh.slice(0, 2)) item.ready();
    // cd3 switched off while its new pair is still loading.
    manager.channel_remove(1);
    await swap;
    check("a channel switched off mid-swap does not hold the others",
        Date.now() - started < 2000
        && world.items.length === 2 && at(world.items, "z11").length === 2,
        "its replacement never loads; waiting on it holds every channel at the "
        + "old plane until the deadline");
    check("...and the fade still runs once", captured === 1);
}

{
    // No fade wanted (motion off): the swap is exactly the old handover.
    const { manager, world, items } = makeManager(["dapi"]);
    manager.planeFade = { capture: () => false, release() { throw new Error("no"); } };
    const swap = manager.setView("z12");
    await tick();
    for (const item of at(items, "z12")) item.ready();
    await swap;
    check("with motion off the swap needs no fade",
        world.items.length === 2 && at(world.items, "z12").length === 2);
}

{
    // A fade that throws must not cost the swap.
    const { manager, world, items } = makeManager(["dapi"]);
    manager.planeFade = { capture: () => { throw new Error("canvas gone"); }, release() {} };
    const quiet = console.error;
    console.error = () => {};
    const swap = manager.setView("z9");
    await tick();
    for (const item of at(items, "z9")) item.ready();
    await swap;
    console.error = quiet;
    check("a fade that fails still swaps the plane",
        world.items.length === 2 && at(world.items, "z9").length === 2);
}

// == as a navigation revision (services/viewState.js) ======================

/** A revision as viewState makes one: an id, a spec, and a way to supersede
 *  it; `scrub` when it was asked for while the hand moved. */
function revision(id, spec, { scrub = false } = {}) {
    const listeners = [];
    const rev = {
        id, spec, scrub, superseded: false,
        onSupersede(fn) { if (rev.superseded) fn(rev); else listeners.push(fn); },
        supersede() { rev.superseded = true; for (const fn of listeners.splice(0)) fn(rev); },
    };
    return rev;
}

{
    // Prepared, not performed: the commit is handed back to run with the
    // other surfaces, in their one task.
    const { manager, world, items } = makeManager(["dapi", "cd3"]);
    const log = [];
    manager.planeFade = { capture() { log.push("capture"); return true; },
                          release() { log.push("release"); } };
    const rev = revision(1, "z11");
    const prepared = manager.setView("z11", rev);
    await tick();
    for (const item of at(items, "z11")) item.ready();
    const commit = await prepared;
    check("for a revision the swap is prepared and its commit handed back, not run",
        typeof commit === "function" && at(world.items, "z10").length === 4
        && at(world.items, "z11").every((item) => item.opacity === 0) && log.length === 0);
    commit();
    check("running it swaps every channel at once, behind one fade",
        world.items.length === 4 && at(world.items, "z11").every((item) => item.opacity === 1)
        && log.join() === "capture,release");
}

{
    // Superseded while loading: nothing of it ever shows, and the token the
    // next channel is built at is the plane on screen.
    const { manager, world, items } = makeManager(["dapi", "cd3"]);
    manager.planeFade = { capture: () => true, release() {} };
    const rev = revision(2, "z12");
    const prepared = manager.setView("z12", rev);
    await tick();
    at(items, "z12")[0].ready();
    rev.supersede();
    const commit = await prepared;
    check("a superseded swap commits nothing and drops what it built",
        commit === null && at(world.items, "z12").length === 0
        && at(world.items, "z10").length === 4 && at(world.items, "z10").every((i) => i.opacity === 1));
    check("and the view the next channel is built at is the one on screen",
        manager.currentView() === "z10");
    // A channel switched off and on again is drawn at the plane on screen.
    manager.channel_remove(1);
    manager.channel_add(1);
    await tick();
    check("a channel switched on afterwards is built at the plane on screen, not the abandoned one",
        at(world.items, "z10").length === 4 && at(world.items, "z12").length === 0);
}

{
    // Superseded after it was ready but before its commit ran.
    const { manager, world, items } = makeManager(["dapi"]);
    manager.planeFade = { capture: () => true, release() {} };
    const rev = revision(3, "z13");
    const prepared = manager.setView("z13", rev);
    await tick();
    for (const item of at(items, "z13")) item.ready();
    const commit = await prepared;
    rev.supersede();
    check("a swap superseded between ready and commit drops its pairs",
        typeof commit === "function" && at(world.items, "z13").length === 0
        && at(world.items, "z10").length === 2);
    commit();
    check("and running the stale commit afterwards changes nothing",
        at(world.items, "z10").length === 2 && at(world.items, "z13").length === 0);
}

{
    // Units pinned: an item knows the window its bytes are in.
    const { manager, items } = makeManager(["dapi"]);
    manager.planeFade = { capture: () => false, release() {} };
    const swap = manager.setView("z2-8std");
    await tick();
    for (const item of at(items, "z2-8std")) item.ready();
    await swap;
    check("a standard-deviation projection's items say their bytes are in its own window",
        at(items, "z2-8std").every((item) => item.source.windowClass === "std")
        && at(items, "z10").every((item) => item.source.windowClass === "volume"));
}

// == preview first: no deadline, a preview while the hand moves, a status ===
//
// From here the page's timers and frames run on a fake clock, and the channel
// fade's coverage test (services/channelFade.js) is loaded, so a swap waits on
// coverage as it does in the browser: whenItemCanDraw with a grace.

const realClearTimeout = globalThis.clearTimeout;
const clock = { now: 0, timers: [], frames: [] };
const fakeSetTimeout = (fn, ms) => {
    clock.timers.push({ fn, at: clock.now + (Number(ms) || 0) });
    return clock.timers.length;
};
const fakeClearTimeout = (id) => {
    if (typeof id !== "number") realClearTimeout(id);
    else if (clock.timers[id - 1]) clock.timers[id - 1].fn = null;
};
const fakeFrame = (fn) => { clock.frames.push(fn); return clock.frames.length; };
/** Run the clock forward: due timers, then a frame, every `step` ms. */
function advance(ms, step = 16) {
    const end = clock.now + ms;
    while (clock.now < end) {
        clock.now = Math.min(end, clock.now + step);
        for (const timer of clock.timers) {
            if (timer.fn && timer.at <= clock.now) {
                const fn = timer.fn;
                timer.fn = null;
                fn();
            }
        }
        const due = clock.frames;
        clock.frames = [];
        for (const fn of due) fn(clock.now);
    }
}
const fadeBox = {
    console,
    performance: { now: () => clock.now },
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
    requestAnimationFrame: fakeFrame,
    cancelAnimationFrame: () => {},
    matchMedia: () => ({ matches: false }),
    // A channel switched on lands at once: its fade is not what is pinned here.
    PlexoraViewerPrefs: { smoothMotion: () => false },
};
fadeBox.window = fadeBox;
runInContext(readFileSync(join(REPO, "plexora/client/src/js/services/channelFade.js"), "utf8"),
             createContext(fadeBox));
globalThis.PlexoraChannelFade = fadeBox.PlexoraChannelFade;
globalThis.setTimeout = fakeSetTimeout;
globalThis.clearTimeout = fakeClearTimeout;
globalThis.requestAnimationFrame = fakeFrame;
const marks = [];
globalThis.PlexoraViewPerf = { mark: (surface, name) => { marks.push(`${surface}:${name}`); } };
const paintOf = (list) => list.filter((item) => item.blend === "lighter");

{
    // A plane that will not cover the view keeps the old one on screen past
    // the deadline that used to hand over to nothing, and says it is slow.
    const { manager, world, items } = makeManager(["dapi", "cd3"], { tiles: true });
    manager.planeFade = { capture: () => true, release() {} };
    const seen = [];
    manager.onPlaneStatus((status) => seen.push(status));
    marks.length = 0;
    let commit;
    manager.setView("z30", revision(21, "z30")).then((held) => { commit = held; });
    await tick();
    advance(1900);
    await tick();
    const fresh = at(items, "z30");
    check("a swap waiting for its plane says fetching, and is not slow before 2 s",
        seen.at(-1)?.phase === "fetching" && !marks.includes("main:slow"), JSON.stringify(seen.at(-1)));
    advance(6600);
    await tick();
    check("pairs that never cover the view are not handed over at the old 8 s deadline",
        commit === undefined && at(world.items, "z10").length === 4 && fresh.length === 4
        && fresh.every((item) => item.opacity === 0 && world.items.includes(item)),
        "handed over, the commit drops the old pairs and the ground -- black -- shows until tiles land");
    check("...the old plane stays and the status says slow (once, with a perf mark)",
        seen.at(-1)?.phase === "slow" && seen.at(-1).rev === 21 && seen.at(-1).spec === "z30"
        && marks.filter((mark) => mark === "main:slow").length === 1, JSON.stringify(seen.at(-1)));
    for (const item of fresh) item.loadCoarse();
    advance(200);
    await tick();
    check("it hands over once the new plane covers the view", typeof commit === "function");
    commit?.();
    check("...and then the new plane is on screen and the old one gone",
        at(world.items, "z30").length === 4 && at(world.items, "z10").length === 0
        && at(world.items, "z30").every((item) => item.opacity === 1));
}

{
    // A revision asked for while the hand moves (`rev.scrub`) commits the
    // moment any level covers the view: a preview at the hand's pace.
    const { manager, items } = makeManager(["dapi"], { tiles: true });
    manager.planeFade = { capture: () => false, release() {} };
    let scrubbed;
    manager.setView("z31", revision(22, "z31", { scrub: true })).then((held) => { scrubbed = held; });
    await tick();
    for (const item of at(items, "z31")) item.loadCoarse();
    advance(48);
    await tick();
    check("a revision asked for mid-drag commits on coverage without the 140 ms grace",
        typeof scrubbed === "function");
    scrubbed?.();
}

{
    // At rest the same coverage waits out the grace: a warm plane still
    // arrives sharp.
    const { manager, items } = makeManager(["dapi"], { tiles: true });
    manager.planeFade = { capture: () => false, release() {} };
    let rested;
    manager.setView("z32", revision(23, "z32")).then((held) => { rested = held; });
    await tick();
    for (const item of at(items, "z32")) item.loadCoarse();
    advance(48);
    await tick();
    const early = rested;
    advance(144);
    await tick();
    check("...while at rest the same coverage waits out the grace first",
        early === undefined && typeof rested === "function");
    rested?.();
}

{
    // How the plane is loading, as the page hears it: fetching while the new
    // plane is on its way, a preview once it is on screen covering the view
    // coarsely, refining as its own level lands, idle once it has.
    const { manager, items, viewer } = makeManager(["dapi", "cd3"], { tiles: true });
    manager.planeFade = { capture: () => true, release() {} };
    const events = [];
    const quiet = globalThis.dispatchEvent;
    globalThis.dispatchEvent = (event) => { events.push(event); return true; };
    const seen = [];
    manager.onPlaneStatus((status) => seen.push(status));
    marks.length = 0;
    let commit;
    manager.setView("z33", revision(24, "z33", { scrub: true })).then((held) => { commit = held; });
    await tick();
    advance(16);
    const fetching = seen.at(-1);
    check("it starts at fetching: the revision, the plane, since when, and counts over each channel's paint item",
        fetching?.surface === "main" && fetching.phase === "fetching" && fetching.rev === 24
        && fetching.spec === "z33" && fetching.needed === 8 && fetching.loaded === 0
        && fetching.level === 2 && typeof fetching.since === "number", JSON.stringify(fetching));
    const fresh = at(items, "z33");
    for (const item of fresh) item.loadCoarse();
    advance(48);
    await tick();
    commit();
    advance(16);
    const preview = seen.at(-1);
    check("committed while only a coarse level covers the view: preview (and a perf mark)",
        preview.phase === "preview" && preview.loaded === 0 && preview.needed === 8
        && preview.since === fetching.since && marks.includes("main:preview")
        && viewer.handlerCount("tile-loaded") === 1, JSON.stringify(preview));
    for (const item of fresh) item.loadFine(1);
    viewer.raise("tile-loaded", { tiledImage: paintOf(fresh)[0], promise: Promise.resolve() });
    await tick();
    advance(16);
    check("a tile of the view's own level landing: refining, counted",
        seen.at(-1).phase === "refining" && seen.at(-1).loaded === 2 && seen.at(-1).needed === 8,
        JSON.stringify(seen.at(-1)));
    for (const item of fresh) item.loadAll();
    viewer.raise("tile-loaded", { tiledImage: paintOf(fresh)[1], promise: Promise.resolve() });
    await tick();
    advance(16);
    const idle = seen.at(-1);
    check("every tile in view loaded: idle, and its items are no longer listened to",
        idle.phase === "idle" && idle.loaded === 8 && idle.needed === 8 && idle.failed === 0
        && viewer.handlerCount("tile-loaded") === 0 && viewer.handlerCount("tile-load-failed") === 0
        && fresh.every((item) => item.handlerCount("fully-loaded-change") === 0),
        JSON.stringify(idle));
    const said = events.filter((event) => event.type === "plexora:surface-status"
                                          && event.detail?.spec === "z33").map((event) => event.detail);
    check("one plexora:surface-status per frame at most: fetching, preview, refining, idle",
        seen.map((status) => status.phase).join() === "fetching,preview,refining,idle"
        && said.length === seen.length && said.every((detail, i) => detail === seen[i]),
        seen.map((status) => status.phase).join());
    globalThis.dispatchEvent = quiet;
}

{
    // A tile that failed for good: the plane is on screen with a hole, and
    // the status says error -- and keeps saying it -- until a retry.
    const { manager, items, viewer } = makeManager(["dapi", "cd3"], { tiles: true });
    manager.planeFade = { capture: () => false, release() {} };
    const asked = [];
    manager.imageViewer.tileFetch = {
        attach() {},
        retryFailed(list) {
            asked.push(list);
            let count = 0;
            for (const item of list) {
                for (const level of Object.values(item.tilesMatrix)) {
                    for (const column of Object.values(level)) {
                        for (const tile of Object.values(column)) {
                            if (tile._plexoraFailed !== "final") continue;
                            delete tile._plexoraFailed;
                            tile.exists = true;
                            count += 1;
                        }
                    }
                }
            }
            return count;
        },
    };
    const seen = [];
    manager.onPlaneStatus((status) => seen.push(status));
    let commit;
    manager.setView("z34", revision(25, "z34", { scrub: true })).then((held) => { commit = held; });
    await tick();
    const fresh = at(items, "z34");
    for (const item of fresh) item.loadCoarse();
    advance(48);
    await tick();
    commit();
    for (const item of fresh) {
        item.loadFine(3);
        item.failFine();
    }
    viewer.raise("tile-load-failed", { tiledImage: paintOf(fresh)[0] });
    advance(16);
    const error = seen.at(-1);
    check("what is left failed for good: error, the holes counted, a retry offered",
        error.phase === "error" && error.failed === 2 && error.loaded === 6 && error.needed === 8
        && typeof error.retry === "function", JSON.stringify(error));
    viewer.raise("tile-loaded", { tiledImage: paintOf(fresh)[0], promise: Promise.resolve() });
    await tick();
    advance(16);
    check("...and it stays error: nothing is listened to until a retry",
        seen.at(-1) === error && viewer.handlerCount("tile-loaded") === 0);
    const count = error.retry();
    advance(16);
    check("retry() asks tileFetch.retryFailed again for this swap's revealed items, both halves",
        asked.length === 1 && asked[0].length === 4 && fresh.every((item) => asked[0].includes(item))
        && count === 4 && seen.at(-1).phase === "refining", JSON.stringify(seen.at(-1)));
    for (const item of fresh) item.loadAll();
    viewer.raise("tile-loaded", { tiledImage: paintOf(fresh)[1], promise: Promise.resolve() });
    await tick();
    advance(16);
    check("...and once the holes land, idle",
        seen.at(-1).phase === "idle" && seen.at(-1).failed === 0 && seen.at(-1).loaded === 8);
}

{
    // Every tile of the new plane failed for good: OSD calls its items fully
    // loaded and every level is "covered" by holes. Handed over, it is black
    // where a picture was -- so it is never handed over: the old plane stays,
    // the status says error, and a retry hands over once a tile lands.
    const { manager, world, items } = makeManager(["dapi", "cd3"], { tiles: true });
    manager.planeFade = { capture: () => false, release() {} };
    const asked = [];
    manager.imageViewer.tileFetch = {
        attach() {},
        retryFailed(list) {
            asked.push(list);
            let count = 0;
            for (const item of list) {
                for (const level of Object.values(item.tilesMatrix)) {
                    for (const column of Object.values(level)) {
                        for (const tile of Object.values(column)) {
                            if (tile._plexoraFailed !== "final") continue;
                            delete tile._plexoraFailed;
                            tile.exists = true;
                            count += 1;
                        }
                    }
                }
            }
            return count;
        },
    };
    const seen = [];
    manager.onPlaneStatus((status) => seen.push(status));
    let commit;
    manager.setView("z37", revision(28, "z37")).then((held) => { commit = held; });
    await tick();
    const fresh = at(items, "z37");
    advance(100);
    for (const item of fresh) {
        for (const level of Object.values(item.tilesMatrix)) {
            for (const column of Object.values(level)) {
                for (const tile of Object.values(column)) {
                    Object.assign(tile, { loaded: false, exists: false, _plexoraFailed: "final" });
                }
            }
        }
        item.ready();                    // nothing is loading any more
    }
    advance(48);
    await tick();
    const early = seen.at(-1);
    advance(3000);
    await tick();
    const error = seen.at(-1);
    check("a plane whose every tile fails for good is never shown: the old plane stays, the status says error",
        early?.phase === "error" && error?.phase === "error" && typeof error.retry === "function"
        && error.loaded === 0 && commit === undefined && at(world.items, "z10").length === 4
        && at(world.items, "z10").every((item) => item.opacity === 1)
        && fresh.every((item) => item.opacity === 0 && world.items.includes(item)),
        JSON.stringify({ early: early?.phase, error: error?.phase, commit: typeof commit }));
    const count = error.retry();
    advance(100);
    await tick();
    check("...retry() asks again for the waiting pairs' tiles; it waits again, not handed over",
        asked.length === 1 && asked[0].length === 4 && fresh.every((item) => asked[0].includes(item))
        && count === 20 && seen.at(-1).phase === "slow" && commit === undefined,
        JSON.stringify({ count, phase: seen.at(-1).phase, commit: typeof commit }));
    for (const item of fresh) item.loadCoarse();
    advance(200);
    await tick();
    const handed = typeof commit === "function";
    commit?.();
    check("...and hands over once a tile lands, the old plane gone",
        handed && at(world.items, "z37").length === 4 && at(world.items, "z10").length === 0
        && at(world.items, "z37").every((item) => item.opacity === 1));
}

{
    // The next swap ends the last one's listening; one superseded with
    // nothing after it leaves the plane on screen, idle.
    const { manager, world, items, viewer } = makeManager(["dapi"], { tiles: true });
    manager.planeFade = { capture: () => false, release() {} };
    const seen = [];
    manager.onPlaneStatus((status) => seen.push(status));
    let commit;
    manager.setView("z35", revision(26, "z35", { scrub: true })).then((held) => { commit = held; });
    await tick();
    for (const item of at(items, "z35")) item.loadCoarse();
    advance(48);
    await tick();
    commit();
    advance(16);
    const watching = viewer.handlerCount("tile-loaded");
    const next = revision(27, "z36");
    manager.setView("z36", next);
    await tick();
    advance(16);
    check("the next swap stops the last one listening to its items",
        seen.at(-2)?.phase === "preview" && watching === 1 && viewer.handlerCount("tile-loaded") === 0
        && seen.at(-1).rev === 27 && seen.at(-1).phase === "fetching", JSON.stringify(seen.at(-1)));
    next.supersede();
    advance(16);
    check("a swap superseded with nothing after it says idle, and the plane on screen stays",
        seen.at(-1).rev === 27 && seen.at(-1).phase === "idle" && seen.at(-1).needed === 0
        && at(world.items, "z35").length === 2 && at(world.items, "z36").length === 0,
        JSON.stringify(seen.at(-1)));
}

check("no swap leaves a timer or a frame behind (the slow timer, coverage polls)",
    clock.timers.every((timer) => !timer.fn) && clock.frames.length === 0,
    `${clock.timers.filter((timer) => timer.fn).length} timer(s) still armed`);
globalThis.setTimeout = realSetTimeout;
globalThis.clearTimeout = realClearTimeout;
delete globalThis.requestAnimationFrame;

console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
