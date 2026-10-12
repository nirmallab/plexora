/**
 * A plane or channel shows as soon as it covers the view at ANY level
 * (services/channelFade.js `covers` / `whenCanDraw`'s grace), and the plane
 * the pointer rests on along the Z strip is made ready (views/planeMotion.js).
 *
 * Pinned here:
 *
 *   **`covers` reads which tiles a level needs against which have loaded.**
 *   OpenSeadragon's own coverage needs a tile DRAWN at full opacity, which an
 *   item held at nothing never is, so a hidden item's readiness is read off
 *   `coverage` (the tiles each level needs on screen) and `tilesMatrix`
 *   (whether each has loaded). A tile past the image's edge needs nothing; a
 *   level with no tiles at all proves nothing.
 *
 *   **Full resolution if it is quick, the view covered if it is not.** With a
 *   grace, an item that covers the view only at a coarse level is held for
 *   the grace and then let go; one that loads in full inside the grace goes
 *   at once, sharp. Without a grace (an HD swap), coverage never counts.
 *
 *   **A channel's fade starts from coverage**, after the grace rather than
 *   at the 800 ms ceiling.
 *
 *   **The hovered plane joins the prefetched ones**, and leaves with the
 *   pointer.
 *
 * Run directly:  node tests/js/coverage_ready_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

let clock = 0;
let frames = [];
let timers = [];
const listeners = new Map();
const box = {
    console,
    performance: { now: () => clock },
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    cancelAnimationFrame: () => {},
    setTimeout: (fn, ms) => { timers.push({ fn, at: clock + (ms || 0) }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    matchMedia: () => ({ matches: false }),
    addEventListener: (name, fn) => {
        if (!listeners.has(name)) listeners.set(name, []);
        listeners.get(name).push(fn);
    },
    dispatchEvent: (event) => {
        for (const fn of listeners.get(event.type) || []) fn(event);
        return true;
    },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    document: { getElementById: () => null },
};
box.window = box;
const context = createContext(box);
runInContext(readFileSync(join(JS, "services/channelFade.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/planeMotion.js"), "utf8"), context);
const Fade = box.PlexoraChannelFade;
const Motion = box.PlexoraPlaneMotion;

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

/**
 * An item with OpenSeadragon's two maps: `coverage[level][x][y]` for the
 * tiles each level needs on screen, `tilesMatrix[level][x][y]` for the tiles
 * themselves. Level 0 is coarse (one tile), level 2 fine (four).
 */
function makeItem() {
    const handlers = new Map();
    const tile = () => ({ exists: true, loaded: false });
    const item = {
        opacity: 0,
        loaded: false,
        coverage: { 0: { 0: { 0: false } }, 2: { 0: { 0: false, 1: false }, 1: { 0: false, 1: false } } },
        tilesMatrix: { 0: { 0: { 0: tile() } },
                       2: { 0: { 0: tile(), 1: tile() }, 1: { 0: tile(), 1: tile() } } },
        setOpacity(value) {
            if (value === item.opacity) return;
            item.opacity = value;
            for (const fn of [...(handlers.get("opacity-change") || [])]) fn({ opacity: value });
        },
        getFullyLoaded: () => item.loaded,
        addHandler(name, fn) {
            if (!handlers.has(name)) handlers.set(name, new Set());
            handlers.get(name).add(fn);
        },
        removeHandler(name, fn) { handlers.get(name)?.delete(fn); },
        loadCoarse() { item.tilesMatrix[0][0][0].loaded = true; },
        loadAll() {
            for (const level of Object.values(item.tilesMatrix)) {
                for (const column of Object.values(level)) {
                    for (const one of Object.values(column)) one.loaded = true;
                }
            }
            item.loaded = true;
            for (const fn of [...(handlers.get("fully-loaded-change") || [])]) fn({});
        },
    };
    return item;
}

// -- covers ----------------------------------------------------------------

{
    check("an item OSD has not updated does not cover", !Fade.covers({}));
    const item = makeItem();
    check("nothing loaded covers nothing", !Fade.covers(item));
    item.tilesMatrix[2][0][0].loaded = true;
    check("part of a level is not the view", !Fade.covers(item));
    item.loadCoarse();
    check("one coarse tile that covers the view does", Fade.covers(item));
    const edge = makeItem();
    edge.tilesMatrix[2][1][1].exists = false;
    for (const [x, y] of [[0, 0], [0, 1], [1, 0]]) edge.tilesMatrix[2][x][y].loaded = true;
    check("a tile past the image's edge needs nothing", Fade.covers(edge));
    const empty = makeItem();
    empty.coverage = { 3: {} };
    empty.tilesMatrix[3] = {};
    check("a level with no tiles proves nothing", !Fade.covers(empty));
}

// -- whenCanDraw's grace -----------------------------------------------------

{
    const item = makeItem();
    item.loadCoarse();
    let ready = false;
    Fade.whenCanDraw(item, () => { ready = true; }, { grace: 140 });
    advance(100);
    check("covered coarsely, it waits out the grace for full resolution", !ready);
    advance(80);
    check("and then goes with what covers the view", ready);
}

{
    const item = makeItem();
    let ready = false;
    Fade.whenCanDraw(item, () => { ready = true; }, { grace: 140 });
    advance(48);
    item.loadAll();
    check("full resolution inside the grace goes at once", ready);
}

{
    const item = makeItem();
    let ready = false;
    Fade.whenCanDraw(item, () => { ready = true; }, { grace: 140 });
    advance(400);
    check("nothing covering the view is never let go on coverage", !ready);
    item.loadCoarse();
    advance(48);
    check("until it does", ready);
}

{
    const item = makeItem();
    item.loadCoarse();
    let ready = false;
    Fade.whenCanDraw(item, () => { ready = true; });
    advance(2000);
    check("with no grace (an HD swap) coverage never counts", !ready);
    item.loadAll();
    check("only full resolution does", ready);
}

{
    const item = makeItem();
    item.loadCoarse();
    let ready = false;
    const stop = Fade.whenCanDraw(item, () => { ready = true; }, { grace: 140 });
    stop();
    advance(400);
    check("a wait stopped early never answers", !ready);
}

// -- the channel fade starts from coverage -------------------------------------

{
    const cover = makeItem();
    const paint = makeItem();
    cover.loadCoarse();
    paint.loadCoarse();
    const begun = clock;
    const fade = Fade.group(2);
    fade.join(cover);
    fade.join(paint);
    advance(Fade.GRACE_MS - 40);
    check("a coarsely covered channel is held for the grace", cover.opacity === 0);
    advance(80 + Fade.FADE_MS);
    check("then fades in, long before the ceiling",
          cover.opacity === 1 && paint.opacity === 1 && clock - begun < Fade.READY_CEILING_MS,
          `${cover.opacity} after ${clock - begun} ms`);
}

// -- the hovered plane is made ready ------------------------------------------

{
    let state = { mode: "plane", z: 10, t: 0 };
    const viewState = {
        depth: 50,
        enabled: () => true,
        get: () => state,
        spec: (s = state) => `z${s.z}`,
    };
    const prefetch = new Motion.NeighbourPrefetch(null, viewState, {}).attach();
    const planes = () => prefetch.neighbourStates().map((s) => s.z);
    check("the neighbours are the planes ahead and behind", planes().join() === "11,12,9", planes().join());
    box.dispatchEvent(new box.CustomEvent("plexora:plane-hover", { detail: { z: 37 } }));
    check("the plane the pointer rests on joins them", planes().includes(37), planes().join());
    box.dispatchEvent(new box.CustomEvent("plexora:plane-hover", { detail: { z: 11 } }));
    check("a hovered neighbour is not asked for twice",
          planes().filter((z) => z === 11).length === 1, planes().join());
    box.dispatchEvent(new box.CustomEvent("plexora:plane-hover", { detail: { z: 10 } }));
    check("nor the plane on screen", !planes().includes(10), planes().join());
    box.dispatchEvent(new box.CustomEvent("plexora:plane-hover", { detail: { z: null } }));
    check("and it leaves with the pointer", planes().join() === "11,12,9", planes().join());
    state = { mode: "slab", z: 10, t: 0, slab: { follow: false } };
    box.dispatchEvent(new box.CustomEvent("plexora:plane-hover", { detail: { z: 30 } }));
    check("a projection that does not follow the plane prefetches nothing", planes().length === 0);
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  ${failure}`);
    process.exit(1);
}
console.log(`\n${checks} checks passed`);
