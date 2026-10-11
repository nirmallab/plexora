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
 *   every other channel would sit at the old plane until the deadline.
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

/** A world whose items load when told to, and that says when one leaves. */
function makeManager(channels) {
    const items = [];
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
        addTiledImage(options) {
            const fullyLoaded = new Set();
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
                addHandler(name, fn) { if (name === "fully-loaded-change") fullyLoaded.add(fn); },
                removeHandler(name, fn) { fullyLoaded.delete(fn); },
                ready() {
                    item.loaded = true;
                    for (const fn of [...fullyLoaded]) fn({ fullyLoaded: true });
                },
            };
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
    for (const item of items) item.ready();
    return { manager, world, items };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
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

/** A revision as viewState makes one: an id, a spec, and a way to supersede it. */
function revision(id, spec) {
    const listeners = [];
    const rev = {
        id, spec, superseded: false,
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

console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
