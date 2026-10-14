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
 *   level with no tiles at all proves nothing. A tile that failed and is
 *   being asked for again is awaited; one whose retries are spent
 *   (tileFetch.js tags it "final") is a hole, not a wait. `progress` counts
 *   the display level's tiles for a loading indicator.
 *
 *   **Full resolution if it is quick, the view covered if it is not.** With a
 *   grace, an item that covers the view only at a coarse level is held for
 *   the grace and then let go; one that loads in full inside the grace goes
 *   at once, sharp. Without a grace (an HD swap), coverage never counts. With
 *   grace 0 (a preview while the hand moves) coverage counts at once.
 *
 *   **Fully loaded is not believed while a tile is retrying.** OpenSeadragon
 *   6 stops counting a failed tile as loading between its retries, so an
 *   item whose tiles all failed says fully loaded with nothing drawn (a jump
 *   to a plane whose tiles answered 500 swapped in black). At any grace it
 *   is ready once the retry lands, or -- with a hole -- once it is spent,
 *   neither of which need move OSD's flag again.
 *
 *   **But never all holes.** An item with nothing loaded is never ready, not
 *   even with every tile failed for good (`failedOut`: `covers` and OSD both
 *   say yes); `onFailed` hears it once, and after `retryFailed` the wait
 *   goes on -- through the moment OSD's flag still says loaded -- and lands
 *   when a tile does.
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
        /** OSD's update moving its fully-loaded flag (and saying so). */
        setLoaded(value) {
            item.loaded = value;
            for (const fn of [...(handlers.get("fully-loaded-change") || [])]) fn({});
        },
        tiles() {
            return Object.values(item.tilesMatrix)
                .flatMap((level) => Object.values(level).flatMap((column) => Object.values(column)));
        },
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

{
    // OSD marks a tile that failed `exists` false, as it does one past the
    // edge; the tile scheduler says which (tileFetch.js `fail`).
    const item = makeItem();
    for (const [x, y] of [[0, 0], [0, 1], [1, 0]]) item.tilesMatrix[2][x][y].loaded = true;
    const failed = item.tilesMatrix[2][1][1];
    failed.exists = false;
    failed._plexoraFailed = "retrying";
    const retrying = Fade.covers(item);
    failed._plexoraFailed = "aborted";
    check("a tile that failed and is retrying is not coverage (nor an aborted one)",
          !retrying && !Fade.covers(item) && Fade.coveringLevel(item) === null);
    failed._plexoraFailed = "final";
    check("one whose retries are spent is a hole, not a wait",
          Fade.covers(item) && Fade.coveringLevel(item) === 2);
}

{
    const nothing = Fade.progress({});
    check("an item OSD has not updated has no progress",
          nothing.level === null && nothing.needed === 0 && nothing.loaded === 0
          && nothing.failed === 0 && nothing.covering === null, JSON.stringify(nothing));
    const item = makeItem();
    item.loadCoarse();
    item.tilesMatrix[2][0][0].loaded = true;
    item.tilesMatrix[2][1][1].exists = false;
    item.tilesMatrix[2][1][1]._plexoraFailed = "final";
    const p = Fade.progress(item);
    check("progress counts the display level's tiles: loaded, failed for good, and the level that covers",
          p.level === 2 && p.needed === 4 && p.loaded === 1 && p.failed === 1 && p.covering === 0,
          JSON.stringify(p));
    // OSD visited only level 0 on its last update (zoomed out): level 2's
    // coverage is left over from before.
    item._tilesToDraw = [[]];
    const q = Fade.progress(item);
    check("at the level OSD last visited, not one a zoom out left behind",
          q.level === 0 && q.needed === 1 && q.loaded === 1 && q.failed === 0, JSON.stringify(q));
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
    Fade.whenCanDraw(item, () => { ready = true; }, { grace: 0 });
    check("grace 0 answers on the first check when the view is covered", ready);
}

{
    const item = makeItem();
    let ready = false;
    Fade.whenCanDraw(item, () => { ready = true; }, { grace: 0 });
    advance(100);
    const early = ready;
    item.loadCoarse();
    advance(Fade.COVER_POLL_MS);
    check("grace 0 on a view not yet covered answers within a poll of it covering", !early && ready,
          `early ${early}, ready ${ready}`);
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

// -- fully loaded is not believed while a tile is retrying ---------------------

/** An item OSD calls fully loaded whose coarse tile and one fine tile failed
 *  and are being asked for again (tileFetch.js `fail`): nothing covers. */
function makeRetrying() {
    const item = makeItem();
    for (const [x, y] of [[0, 0], [0, 1], [1, 0]]) item.tilesMatrix[2][x][y].loaded = true;
    const failed = [item.tilesMatrix[0][0][0], item.tilesMatrix[2][1][1]];
    for (const tile of failed) Object.assign(tile, { exists: false, _plexoraFailed: "retrying" });
    item.loaded = true;
    return { item, failed };
}
const pendingTimers = () => timers.filter((timer) => timer.fn).length;

{
    const { item, failed } = makeRetrying();
    const retrying = Fade.settledLoaded(item);
    failed[1]._plexoraFailed = "final";
    const oneLeft = Fade.settledLoaded(item);
    failed[0]._plexoraFailed = "final";
    check("settledLoaded: not while any tile is retrying; once each is spent, loaded with holes",
          !retrying && !oneLeft && Fade.settledLoaded(item) && !Fade.settledLoaded(makeItem()));
}

for (const grace of [null, 0, Fade.GRACE_MS]) {
    const name = grace === null ? "no grace" : `grace ${grace}`;
    {
        const { item, failed } = makeRetrying();
        let ready = false;
        Fade.whenCanDraw(item, () => { ready = true; }, { grace });
        advance(500);
        const early = ready;
        // The retry lands (tileFetch.deliver). OSD's flag was already true:
        // no `fully-loaded-change`.
        for (const tile of failed) {
            Object.assign(tile, { exists: true, loaded: true });
            delete tile._plexoraFailed;
        }
        advance(2 * Fade.COVER_POLL_MS);
        check(`${name}: an item that says fully loaded while a tile is retrying is not ready; `
              + "it is once the tile loads", !early && ready, `early ${early}, ready ${ready}`);
    }
    {
        const { item, failed } = makeRetrying();
        let ready = false;
        Fade.whenCanDraw(item, () => { ready = true; }, { grace });
        advance(500);
        const early = ready;
        for (const tile of failed) tile._plexoraFailed = "final";
        advance(2 * Fade.COVER_POLL_MS);
        check(`${name}: ...and ready, with a hole, once the tile's retries are spent`,
              !early && ready, `early ${early}, ready ${ready}`);
    }
}

{
    // An HD swap's wait polls only while a retry is out; otherwise OSD's
    // `fully-loaded-change` says, and stopping it leaves nothing behind.
    const before = pendingTimers();
    const loading = makeItem();
    const stopLoading = Fade.whenCanDraw(loading, () => {});
    const idle = pendingTimers() - before;
    const { item } = makeRetrying();
    const stopRetrying = Fade.whenCanDraw(item, () => {});
    advance(100);
    const polling = pendingTimers() - before;
    stopLoading();
    stopRetrying();
    check("no grace: a wait polls only while a retry is out, and leaves no poll once stopped",
          idle === 0 && polling === 1 && pendingTimers() === before,
          `idle ${idle}, polling ${polling}, after ${pendingTimers() - before}`);
}

// -- never all holes: an item with nothing to draw is never ready ---------------

/** An item every tile of which failed for good: OSD calls it fully loaded,
 *  and `covers` would (every tile a hole). */
function makeFailedOut() {
    const item = makeItem();
    for (const tile of item.tiles()) Object.assign(tile, { exists: false, _plexoraFailed: "final" });
    item.loaded = true;
    return item;
}
/** As tileFetch.retryFailed: each spent tile an unloaded tile again. */
function retryFailed(item) {
    for (const tile of item.tiles()) {
        if (tile._plexoraFailed !== "final") continue;
        delete tile._plexoraFailed;
        Object.assign(tile, { exists: true, loaded: false });
    }
}

{
    const item = makeFailedOut();
    const partial = makeRetrying().item;
    partial.tiles().forEach((tile) => { if (tile._plexoraFailed) tile._plexoraFailed = "final"; });
    check("failedOut: nothing loaded, nothing retrying, a tile failed for good -- not a partial failure",
          Fade.failedOut(item) && Fade.blank(item) && Fade.covers(item)
          && !Fade.failedOut(partial) && !Fade.blank(partial) && !Fade.failedOut(makeItem()));
}

for (const grace of [null, 0, Fade.GRACE_MS]) {
    const name = grace === null ? "no grace" : `grace ${grace}`;
    const item = makeFailedOut();
    let ready = false;
    const told = [];
    Fade.whenCanDraw(item, () => { ready = true; }, { grace, onFailed: (one) => told.push(one) });
    advance(500);
    check(`${name}: an item whose every tile is final is not ready, and onFailed hears it once`,
          !ready && told.length === 1 && told[0] === item, `ready ${ready}, told ${told.length}`);
    retryFailed(item);
    // Before OSD's next update its flag still says loaded: nothing to draw.
    advance(100);
    const gap = ready;
    item.setLoaded(false);
    advance(100);
    const asking = ready;
    if (grace === null) {
        item.loadAll();
    } else {
        item.loadCoarse();
        advance(grace + 2 * Fade.COVER_POLL_MS);
    }
    check(`${name}: after retryFailed it waits again, and lands when a tile loads`,
          !gap && !asking && ready && told.length === 1,
          `gap ${gap}, asking ${asking}, ready ${ready}, told ${told.length}`);
}

{
    // Failed out, retried, failed out again: said again.
    const item = makeFailedOut();
    const told = [];
    const stop = Fade.whenCanDraw(item, () => {}, { grace: Fade.GRACE_MS, onFailed: () => told.push(clock) });
    advance(200);
    retryFailed(item);
    advance(100);
    const between = told.length;
    for (const tile of item.tiles()) Object.assign(tile, { exists: false, _plexoraFailed: "final" });
    advance(100);
    stop();
    check("onFailed hears each failure: again after a retry that fails too",
          between === 1 && told.length === 2, `${between} then ${told.length}`);
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
