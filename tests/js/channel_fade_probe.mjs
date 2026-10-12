/**
 * A channel switched on comes in rather than lands (services/channelFade.js).
 *
 * Every surface the channel section drives adds a channel as OpenSeadragon
 * items, and added at full opacity they appear tile by tile as the bytes come
 * in. Pinned here:
 *
 *   **Held at nothing until it can draw.** Joined items sit at opacity 0 --
 *   whoever else sets their opacity meanwhile (a stack change pushing the base
 *   card's opacity) is put back in the same task -- until every one of them is
 *   fully loaded, or the ceiling passes.
 *
 *   **Then together, eased out, to what full presence means now.** One fade
 *   for a cover/paint pair: both halves carry the same presence, and the
 *   target (the base card's slider) is read every frame.
 *
 *   **Ends cleanly**: in full and its own once the fade has run, at once when
 *   its items leave the world (switched off mid-fade) or motion is off.
 *
 *   **On the plane, only a channel arriving fades.** `channel_add` joins both
 *   halves of the pair to one fade; the HD swap's replacements keep their
 *   handover and land in full the moment it commits.
 *
 * Run directly:  node tests/js/channel_fade_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext, runInThisContext } from "node:vm";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");
const SOURCE = readFileSync(join(JS, "services/channelFade.js"), "utf8");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

/** An item as OpenSeadragon's TiledImage behaves for this: opacity-change on
 *  a real change, fully-loaded-change when told it loaded. */
function makeItem(world = null) {
    const handlers = new Map();
    const item = {
        opacity: 0,
        loaded: false,
        preload: true,
        viewer: world ? { world } : undefined,
        setOpacity(value) {
            if (value === item.opacity) return;
            item.opacity = value;
            for (const fn of [...(handlers.get("opacity-change") || [])]) fn({ opacity: value });
        },
        setPreload(value) { item.preload = value; },
        getFullyLoaded: () => item.loaded,
        addHandler(name, fn) {
            if (!handlers.has(name)) handlers.set(name, new Set());
            handlers.get(name).add(fn);
        },
        removeHandler(name, fn) { handlers.get(name)?.delete(fn); },
        handlerCount: (name) => handlers.get(name)?.size || 0,
        ready() {
            item.loaded = true;
            for (const fn of [...(handlers.get("fully-loaded-change") || [])]) fn({});
        },
    };
    return item;
}

// -- the fade itself, on a clock and frames this probe turns ---------------------

let clock = 0;
let frames = [];
let timers = [];
let reduced = false;
const box = {
    console,
    performance: { now: () => clock },
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    cancelAnimationFrame: () => {},
    setTimeout: (fn, ms) => { timers.push({ fn, at: clock + ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    matchMedia: () => ({ matches: reduced }),
};
box.window = box;
const context = createContext(box);
runInContext(SOURCE, context);
const Fade = box.PlexoraChannelFade;

/** Move the clock on by `ms`, one display frame at a time. */
function advance(ms, step = 16) {
    const end = clock + ms;
    while (clock < end) {
        clock = Math.min(end, clock + step);
        for (const timer of timers) {
            if (timer.fn && timer.at <= clock) {
                const fn = timer.fn;
                timer.fn = null;
                fn();
            }
        }
        const due = frames;
        frames = [];
        for (const fn of due) fn(clock);
    }
}

{
    let target = 0.8;
    let done = null;
    const cover = makeItem();
    const paint = makeItem();
    cover.opacity = 0.8;              // landed at the slider's value before joining
    const fade = Fade.group(2, { target: () => target, onDone: (items) => { done = items; } });
    fade.join(cover);
    check("an item that joins is held at nothing", cover.opacity === 0, String(cover.opacity));
    cover.setOpacity(0.8);            // a stack change pushing the base card's opacity
    check("and whoever sets its opacity meanwhile is put back at once", cover.opacity === 0);
    fade.join(paint);
    advance(200);
    check("both halves wait for their tiles", cover.opacity === 0 && paint.opacity === 0);
    cover.ready();
    advance(64);
    check("until every half can draw", cover.opacity === 0 && paint.opacity === 0);
    paint.ready();
    advance(Fade.FADE_MS / 2);
    const half = 0.8 * Fade.ease(0.5);
    check("then they come in together, eased out",
          Math.abs(cover.opacity - paint.opacity) < 1e-9 && Math.abs(cover.opacity - half) < 0.05,
          `${cover.opacity} ${paint.opacity} want ~${half.toFixed(3)}`);
    target = 0.5;                      // the slider moved mid-fade
    advance(Fade.FADE_MS);
    check("to what full presence means at the end", cover.opacity === 0.5 && paint.opacity === 0.5,
          `${cover.opacity} ${paint.opacity}`);
    check("and then the items are their own again",
          cover.handlerCount("opacity-change") === 0 && done?.length === 2);
    cover.setOpacity(0.3);
    check("(a slider after the fade is not undone)", cover.opacity === 0.3);
}

{
    // Never fully loaded (a slow link): in with what it has, at the ceiling.
    const item = makeItem();
    Fade.fadeIn([item]);
    advance(Fade.READY_CEILING_MS - 32);
    check("a channel whose tiles are slow is held only until the ceiling", item.opacity === 0);
    advance(32 + Fade.FADE_MS + 32);
    check("and then comes in with what it has", item.opacity === 1, String(item.opacity));
}

{
    // Switched off mid-fade: its items leave the world.
    const items = [];
    const world = { getIndexOfItem: (it) => items.indexOf(it) };
    const item = makeItem(world);
    items.push(item);
    let ended = false;
    Fade.fadeIn([item], { onDone: () => { ended = true; } });
    item.ready();
    advance(64);
    items.length = 0;
    advance(32);
    check("a channel switched off while it came in ends its fade", ended && frames.length === 0);
}

{
    const item = makeItem();
    reduced = true;
    Fade.fadeIn([item], { target: () => 0.7 });
    check("with motion off a channel lands at once", item.opacity === 0.7 && frames.length === 0);
    reduced = false;
}

{
    const item = makeItem();
    const fade = Fade.group(2, {});
    fade.join(item);
    fade.join(null);                   // its other half failed to add
    item.ready();
    advance(Fade.FADE_MS + 32);
    check("a half that never arrived does not hold the other", item.opacity === 1);
    const late = makeItem();
    fade.join(late);
    check("and one that arrives after the fade lands in full", late.opacity === 1);
}

// -- the plane: a channel arriving fades, a quality swap does not ----------------

{
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
    globalThis.matchMedia = () => ({ matches: false });
    const stackContext = createContext({ console, window: {}, globalThis: {} });
    runInContext(readFileSync(join(JS, "views/layerStack.js"), "utf8"), stackContext);
    globalThis.PlexoraLayerStack = stackContext.window.PlexoraLayerStack
        || stackContext.PlexoraLayerStack;
    runInThisContext(SOURCE);
    const module = await import(pathToFileURL(join(JS, "views/viewerManager.js")).href);
    const ViewerManager = module.default || module.ViewerManager;

    const items = [];
    const world = {
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        getIndexOfItem: (item) => items.indexOf(item),
        setItemIndex: () => {},
        addHandler() {},
        removeHandler() {},
        removeItem(item) {
            const at = items.indexOf(item);
            if (at >= 0) items.splice(at, 1);
        },
    };
    const viewer = {
        world,
        viewport: { getCenter: () => ({ x: 0.5, y: 0.5 }), getZoom: () => 1, zoomTo() {}, panTo() {} },
        forceRedraw() {},
        raiseEvent() {},
        addTiledImage(options) {
            const item = makeItem(world);
            item.opacity = options.opacity;
            item.blend = options.compositeOperation;
            item.source = options.tileSource;
            items.push(item);
            options.success?.({ item });
        },
    };
    const channelList = { currentChannels: {}, rangeConnector: { 0: [0, 255], 1: [0, 255] },
                          colorConnector: { 0: { color: "#ffffff" }, 1: { color: "#ff0000" } } };
    const manager = new ViewerManager(
        { viewer, config: { width: 1000, height: 1000, extraZoomLevels: 0, maxLevel: 3,
                            tileWidth: 256, tileHeight: 256,
                            imageData: [{ src: "/tiles/dapi/" }, { src: "/tiles/cd3/" }] },
          layerStack: new globalThis.PlexoraLayerStack.LayerStack({ viewer }),
          numericData: { bitRange: [0, 255] } },
        channelList);
    manager.viewer = viewer;
    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    manager.channel_add(0);
    const pair = items.slice();
    check("a channel switched on is added as a pair held at nothing",
          pair.length === 2 && pair.every((item) => item.opacity === 0),
          JSON.stringify(pair.map((item) => item.opacity)));
    await wait(50);
    check("while its tiles load", pair.every((item) => item.opacity === 0));
    for (const item of pair) item.ready();
    await wait(Math.round(Fade.FADE_MS / 2));
    const [cover, paint] = pair;
    check("then both halves come in together",
          cover.opacity > 0 && cover.opacity < 1 && Math.abs(cover.opacity - paint.opacity) < 1e-9,
          `${cover.opacity} ${paint.opacity}`);
    await wait(Fade.FADE_MS + 80);
    check("to the base card's opacity", pair.every((item) => item.opacity === 1),
          JSON.stringify(pair.map((item) => item.opacity)));

    const swap = manager.setHdMode(true);
    const fresh = items.filter((item) => !pair.includes(item));
    check("an HD swap's replacements load invisibly", fresh.length === 2
          && fresh.every((item) => item.opacity === 0));
    for (const item of fresh) item.ready();
    await swap;
    check("and land in full the moment the swap commits -- a swap is not a channel arriving",
          fresh.every((item) => item.opacity === 1) && !items.some((item) => pair.includes(item)),
          JSON.stringify(fresh.map((item) => item.opacity)));
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
