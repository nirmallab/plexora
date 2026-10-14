/**
 * One scheduler for a volume's channel and section tiles (views/tileFetch.js).
 *
 * Run against a fake fetch whose responses arrive when the probe says, OSD
 * jobs that record how they were finished, and a decoder pool that records
 * what it was handed. Pinned:
 *
 * - one request per address, however many items ask for it (a channel's
 *   cover and paint, a handover, a prefetch); every job gets the plane;
 * - in order of need: on screen before a handover before a prefetch -- the
 *   next plane covering the view before the plane on screen sharpening -- the
 *   plane before the panes, coarse levels before fine, near before far;
 *   never more than MAX_IN_FLIGHT at once;
 * - while the hand moves (`setScrubbing`) what only sharpens waits in line,
 *   not dropped: a covering plane past its cover, a plane coming in past its
 *   coarsest level, a prefetched plane past its two coarsest; still shared, still
 *   cancelled with its item, never batched; and it goes when the hand stops,
 *   with no new job asking -- at once when it lets go (`endScrubbing`);
 * - decoded once, the response's buffer TRANSFERRED to the worker (8-bit) and
 *   the job finished as a `context2d` the size of the tile -- so OSD's own
 *   image decode never runs;
 * - an item removed from its world gives up its jobs: finished at once (its
 *   loader slot freed), and a request nobody wants any more is aborted;
 * - an aborted download is never retried; a failed one is, by OSD, and its
 *   tile says so: "retrying" while OSD has retries left (its viewer's
 *   `tileRetryMax`), "final" after; `retryFailed` makes a final one a tile
 *   OSD asks for again;
 * - batched only on a slow link: the wait for a first byte, less the
 *   server's own time, over BATCH_ENTER_MS -- a slow server is not a slow
 *   link; a batch carries one server's tiles of one tier in order of need,
 *   each tile its own part, and a server without the route goes back to
 *   single tiles for good.
 *
 * Run directly:  node tests/js/tile_fetch_probe.mjs
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

const box = { console, setTimeout, clearTimeout, performance, AbortController, Promise, URL,
              TextDecoder, location: { href: "http://127.0.0.1:8792/proj" } };
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(JS, "views/tileFetch.js"), "utf8"), createContext(box));
const { TileFetchScheduler, MAX_IN_FLIGHT } = box.PlexoraTileFetch;

const tick = () => new Promise((resolve) => setImmediate(resolve));
const webp = () => new Uint8Array([0x52, 0x49, 0x46, 0x46, 1, 2, 3, 4]).buffer;   // "RIFF..."
const png = () => new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3, 4]).buffer;

/** A fetch whose responses the probe releases. */
function fakeFetch() {
    const calls = [];
    const fetchImpl = (url, options) => new Promise((resolve, reject) => {
        const call = { url, options, aborted: false,
                       respond(buffer = webp(), status = 200) {
                           resolve({ ok: status < 400, status, arrayBuffer: async () => buffer });
                       } };
        options?.signal?.addEventListener?.("abort", () => {
            call.aborted = true;
            const error = new Error("aborted");
            error.name = "AbortError";
            reject(error);
        });
        calls.push(call);
    });
    return { calls, fetchImpl };
}

function pool() {
    const seen = [];
    return {
        seen,
        decode: async (buffer, kind, options) => {
            seen.push({ buffer, kind, transfer: options?.transfer });
            return { array: new Uint8Array(6), width: 3, height: 2 };
        },
    };
}

let items = 0;
function item({ opacity = 1, owner = "main", standby = false } = {}) {
    items += 1;
    return { id: items, opacity, getOpacity() { return this.opacity; },
             source: { plexoraOwner: owner, plexoraStandby: standby } };
}

/** An OSD job as ImageLoader makes one. */
function job(url, tiledImage, { level = 3, distance = 0 } = {}) {
    const tile = { level, squaredDistance: distance, tiledImage };
    return {
        src: url, tile, source: tiledImage.source, userData: {}, tries: 1,
        finished: null, failed: null,
        finish(data, request, dataType) { this.finished = { data, dataType }; },
        fail(message) { this.failed = message; },
    };
}

function scheduler(extra = {}) {
    const net = fakeFetch();
    const decoder = pool();
    const contexts = [];
    const s = new TileFetchScheduler({
        decoderPool: decoder, fetchImpl: net.fetchImpl,
        makeContext: (w, h) => { const c = { canvas: { width: w, height: h } }; contexts.push(c); return c; },
        ...extra,
    });
    return { s, net, decoder, contexts };
}

// -- one request per address; every job gets the plane ---------------------------------

{
    const { s, net, decoder } = scheduler();
    const cover = item();
    const paint = item();
    const a = job("/t/0/3/0_0.png?view=z5", cover);
    const b = job("/t/0/3/0_0.png?view=z5", paint);
    s.start(a);
    s.start(b);
    check("the cover and the paint of a channel share one request", net.calls.length === 1);
    net.calls[0].respond();
    await tick(); await tick();
    check("and one decode", decoder.seen.length === 1);
    check("both jobs are finished with the decoded plane on their tiles",
        a.finished && b.finished && a.tile._array && a.tile._array === b.tile._array
        && a.tile._format === "u8");
    check("as an empty context2d the size of the tile: OSD decodes nothing",
        a.finished.dataType === "context2d" && a.finished.data.canvas.width === 3
        && a.finished.data.canvas.height === 2);
    check("an 8-bit tile's buffer is transferred to the worker, not copied",
        decoder.seen[0].kind === "webp" && decoder.seen[0].transfer === true);
}

{
    const { s, net, decoder } = scheduler();
    const a = job("/t/0/3/0_0.png?q=hd&view=z5", item());
    s.start(a);
    net.calls[0].respond(png());
    await tick(); await tick();
    check("an HD tile goes to the PNG decoder, copied (a worker may refuse it)",
        decoder.seen[0].kind === "gray16png" && decoder.seen[0].transfer === false
        && a.tile._format === "u16");
}

// -- in order of need -------------------------------------------------------------

{
    const { s, net } = scheduler();
    const blockers = [];
    for (let i = 0; i < MAX_IN_FLIGHT; i += 1) {
        blockers.push(job(`/t/blocker/${i}`, item()));
        s.start(blockers[blockers.length - 1]);
    }
    check(`never more than ${MAX_IN_FLIGHT} in flight`, net.calls.length === MAX_IN_FLIGHT);
    const standby = job("/t/standby", item({ opacity: 0, standby: true }), { level: 0 });
    const handover = job("/t/handover", item({ opacity: 0 }), { level: 0 });
    const pane = job("/t/pane", item({ owner: "pane" }), { level: 0 });
    const fine = job("/t/fine", item(), { level: 4 });
    const coarseFar = job("/t/coarse-far", item(), { level: 1, distance: 9 });
    const coarseNear = job("/t/coarse-near", item(), { level: 1, distance: 1 });
    for (const j of [standby, handover, pane, fine, coarseFar, coarseNear]) s.start(j);
    for (const call of net.calls.slice(0, MAX_IN_FLIGHT)) call.respond();
    for (let i = 0; i < 6; i += 1) await tick();
    const order = net.calls.slice(MAX_IN_FLIGHT).map((call) => call.url.split("/").pop());
    check("on screen (coarse, near first) > the panes > a handover > a prefetch",
        order.join() === "coarse-near,coarse-far,fine,pane,handover,standby", order.join());
}

{
    // Coverage before sharpness: the plane on screen already covers the view
    // (channelFade.covers), so its finer tiles only sharpen it; the next
    // plane's tiles that would cover the view go first.
    box.PlexoraChannelFade = { covers: (it) => Boolean(it.covering) };
    const { s, net } = scheduler();
    for (let i = 0; i < MAX_IN_FLIGHT; i += 1) s.start(job(`/t/blocker/${i}`, item()));
    const onScreen = item();
    onScreen.covering = true;
    const coming = item({ opacity: 0 });
    const comingCovered = item({ opacity: 0 });
    comingCovered.covering = true;
    const sharpen = job("/t/sharpen", onScreen, { level: 5 });
    const cover = job("/t/next-cover", coming, { level: 2 });
    const nextSharpen = job("/t/next-sharpen", comingCovered, { level: 5 });
    const prefetch = job("/t/prefetch", item({ opacity: 0, standby: true }), { level: 0 });
    for (const j of [prefetch, nextSharpen, sharpen, cover]) s.start(j);
    for (const call of net.calls.slice(0, MAX_IN_FLIGHT)) call.respond();
    for (let i = 0; i < 6; i += 1) await tick();
    const order = net.calls.slice(MAX_IN_FLIGHT).map((call) => call.url.split("/").pop());
    check("the next plane covering the view goes before the plane on screen sharpening",
        order.join() === "next-cover,sharpen,next-sharpen,prefetch", order.join());
    delete box.PlexoraChannelFade;
}

{
    // A prefetched item that became visible (adopted by a step) is visible.
    const { s, net } = scheduler({ maxInFlight: 1 });
    s.start(job("/t/first", item()));
    const adopted = item({ opacity: 0, standby: true });
    const hidden = item({ opacity: 0 });
    s.start(job("/t/handover", hidden));
    s.start(job("/t/adopted", adopted));
    adopted.opacity = 1;
    net.calls[0].respond();
    for (let i = 0; i < 4; i += 1) await tick();
    check("a prefetched plane adopted by a step jumps the queue",
        net.calls[1]?.url === "/t/adopted", net.calls.map((c) => c.url).join());
}

{
    // Prefetches never fill the wire: a tile on screen must not wait behind one.
    const { s, net } = scheduler();
    for (let i = 0; i < 4; i += 1) s.start(job(`/t/standby/${i}`, item({ opacity: 0, standby: true })));
    check("at most two prefetch requests on the wire, and only while nothing else is",
        net.calls.length === 2);
    s.start(job("/t/visible", item()));
    check("and a tile on screen goes out at once beside them",
        net.calls.length === 3 && net.calls[2].url === "/t/visible");
    net.calls[0].respond();
    for (let i = 0; i < 4; i += 1) await tick();
    check("with a tile on screen on the wire, prefetches are back to one",
        net.calls.length === 3, net.calls.map((c) => c.url).join());
}

// -- while the hand moves -----------------------------------------------------------

/** A clock the probe moves, and the timers set on it (setScrubbing's). */
function fakeTime(start = 1000) {
    const real = { performance: box.performance, setTimeout: box.setTimeout,
                   clearTimeout: box.clearTimeout };
    const timers = new Map();
    let ids = 0;
    const time = {
        at: start,
        timers,
        /** Move to `to`, running every timer due by then, earliest first. */
        advance(to) {
            time.at = to;
            for (;;) {
                let due = null;
                for (const entry of timers) {
                    if (entry[1].at <= time.at && (!due || entry[1].at < due[1].at)) due = entry;
                }
                if (!due) return;
                timers.delete(due[0]);
                due[1].run();
            }
        },
        restore() { Object.assign(box, real); },
    };
    box.performance = { now: () => time.at };
    box.setTimeout = (run, ms = 0) => {
        ids += 1;
        timers.set(ids, { run, at: time.at + Math.max(0, Number(ms) || 0) });
        return ids;
    };
    box.clearTimeout = (id) => { timers.delete(id); };
    return time;
}
/** channelFade's answers, read off a fake item's `covering` level. */
const coveringFade = {
    covers: (it) => Number.isFinite(it.covering),
    coveringLevel: (it) => (Number.isFinite(it.covering) ? it.covering : null),
};

{
    // The plane on screen covers the view at level 2 (OSD counts levels up
    // from the coarsest): while the hand moves it does not sharpen -- level
    // 3 and 4 wait. A plane that does not cover the view yet, whose
    // coarsest visited level (the least key of OSD's `coverage`) is 5,
    // loads that level, which covers it; its level 6 waits too. (Before
    // the drag benchmark, a covering plane sharpened one level past its
    // cover and a plane coming in was never held: at a stack's home view
    // that is every level, and each preview waited behind full-resolution
    // tiles.)
    box.PlexoraChannelFade = coveringFade;
    const time = fakeTime();
    const { s, net } = scheduler();
    for (let i = 0; i < MAX_IN_FLIGHT; i += 1) s.start(job(`/t/blocker/${i}`, item()));
    const onScreen = Object.assign(item(), { covering: 2 });
    const comingCovered = Object.assign(item({ opacity: 0 }), { covering: 2 });
    const coming = Object.assign(item({ opacity: 0 }), { coverage: { 5: {}, 6: {} } });
    s.setScrubbing(time.at + 250);
    const one = job("/t/sharpen-3", onScreen, { level: 3 });
    const two = job("/t/sharpen-4", onScreen, { level: 4 });
    const nextTwo = job("/t/next-sharpen-4", comingCovered, { level: 4 });
    const fine = job("/t/cover-6", coming, { level: 6 });
    const cover = job("/t/cover-5", coming, { level: 5 });
    for (const j of [two, nextTwo, one, fine, cover]) s.start(j);
    for (const call of net.calls.slice(0, MAX_IN_FLIGHT)) call.respond();
    for (let i = 0; i < 6; i += 1) await tick();
    const order = net.calls.slice(MAX_IN_FLIGHT).map((call) => call.url.split("/").pop());
    check("while the hand moves, a covering plane does not sharpen and a plane coming in loads only its coarsest level",
        order.join() === "cover-5", order.join());
    check("the held tiles wait in line, not dropped",
        s.queue.length === 4 && ["/t/sharpen-3", "/t/sharpen-4", "/t/next-sharpen-4", "/t/cover-6"]
            .every((url) => s.requests.has(url))
        && s.stats.aborted === 0 && !one.finished && !two.finished && !nextTwo.finished && !fine.finished,
        JSON.stringify({ queued: s.queue.map((r) => r.url), stats: s.stats }));
    // The same tile asked for by a plane that needs it to cover the view
    // (its coarsest level): still one request, keyed by the job that is not
    // held -- it goes.
    const shared = s.stats.shared;
    s.start(job("/t/sharpen-4", Object.assign(item({ opacity: 0 }), { coverage: { 4: {} } }),
                { level: 4 }));
    const sent = net.calls.filter((call) => call.url === "/t/sharpen-4").length;
    check("a held tile that another plane needs to cover the view shares its request, and goes",
        s.stats.shared === shared + 1 && sent === 1 && s.queue.length === 3,
        JSON.stringify({ shared: s.stats.shared, sent, queued: s.queue.map((r) => r.url) }));
    s.cancelItem(comingCovered);
    check("a held request is cancelled with its item, never having gone",
        nextTwo.finished?.dataType === "context2d" && s.queue.length === 2
        && !s.requests.has("/t/next-sharpen-4")
        && !net.calls.some((call) => call.url === "/t/next-sharpen-4"),
        JSON.stringify({ queued: s.queue.map((r) => r.url), finished: nextTwo.finished }));
    // The hand stops: what was held goes, with no new job asking.
    time.advance(time.at + 300);
    const after = net.calls.map((call) => call.url.split("/").pop());
    check("when the hand stops, the plane on screen sharpens and the one coming in loads its finer level",
        !s.scrubbing() && after.includes("sharpen-3") && after.includes("cover-6") && s.queue.length === 0,
        after.join());
    time.restore();
    delete box.PlexoraChannelFade;
}

{
    // A prefetched plane whose coarsest visited level (the least key of
    // OSD's `coverage`) is 3: while the hand moves only 3 and 4 load; when
    // it stops, the rest go -- no new job asks, setScrubbing's timer pumps.
    const time = fakeTime(1000);
    const { s, net } = scheduler();
    const ahead = Object.assign(item({ opacity: 0, standby: true }),
                                { coverage: { 3: {}, 4: {}, 5: {}, 6: {} } });
    s.setScrubbing(time.at + 250);
    for (const level of [6, 5, 4, 3]) s.start(job(`/t/ahead-${level}`, ahead, { level }));
    const urls = () => net.calls.map((call) => call.url.split("/").pop()).sort().join();
    check("while the hand moves, a prefetched plane loads only its two coarsest levels",
        urls() === "ahead-3,ahead-4" && s.queue.length === 2, urls());
    for (const call of net.calls) call.respond();
    for (let i = 0; i < 6; i += 1) await tick();
    time.advance(1100);
    s.setScrubbing(1350);                   // the hand moves on
    s.setScrubbing(1200);                   // an earlier end does not shorten it
    time.advance(1260);                     // past the first end
    check("a later request keeps the hold, and an earlier end does not shorten it",
        urls() === "ahead-3,ahead-4" && s.scrubbing() && s.scrubUntil === 1350
        && time.timers.size === 1, JSON.stringify({ sent: urls(), until: s.scrubUntil }));
    time.advance(1360);
    check("when the hand stops the rest go, with no new job asking",
        urls() === "ahead-3,ahead-4,ahead-5,ahead-6" && !s.scrubbing() && s.queue.length === 0,
        urls());
    time.restore();
}

{
    // Before OSD's first update has written `coverage`: the level its walk
    // down stops at (`savedCutOffLevel`, the finest one-tile level).
    const time = fakeTime();
    const { s, net } = scheduler();
    const fresh = Object.assign(item({ opacity: 0, standby: true }), { savedCutOffLevel: 4 });
    s.setScrubbing(time.at + 250);
    for (const level of [6, 5]) s.start(job(`/t/fresh-${level}`, fresh, { level }));
    check("a prefetched plane with nothing visited yet counts from its one-tile level",
        net.calls.map((call) => call.url).join() === "/t/fresh-5" && s.queue.length === 1,
        net.calls.map((call) => call.url).join());
    time.restore();
}

{
    // The hand let go (`plexora:scrub-ended`, planeMotion.js): what was held
    // goes at once, not when setScrubbing's time runs out -- which never
    // shortens, so the end is its own call -- and its timer is gone.
    box.PlexoraChannelFade = coveringFade;
    const time = fakeTime();
    const { s, net } = scheduler();
    const onScreen = Object.assign(item(), { covering: 2 });
    s.setScrubbing(time.at + 100);
    s.start(job("/t/end-3", onScreen, { level: 3 }));
    const heldBefore = net.calls.length === 0 && s.queue.length === 1;
    s.endScrubbing();
    check("endScrubbing releases held tiles at once, its timer gone",
        heldBefore && !s.scrubbing() && net.calls.map((call) => call.url).join() === "/t/end-3"
        && s.queue.length === 0 && time.timers.size === 0,
        JSON.stringify({ heldBefore, sent: net.calls.map((call) => call.url), timers: time.timers.size }));
    s.setScrubbing(time.at + 100);
    check("...and the next drag holds again", s.scrubbing() && time.timers.size === 1);
    time.restore();
    delete box.PlexoraChannelFade;
}

{
    // On a slow (batched) link: the held tiles of the same server and tier
    // stay behind.
    box.PlexoraChannelFade = coveringFade;
    const time = fakeTime();
    const { s, net } = scheduler();
    const base = "/generated/data/held/held_0";
    s.linkOf(`${base}/0/0_0.png`).link.batching = true;
    const onScreen = Object.assign(item(), { covering: 2 });
    s.setScrubbing(time.at + 250);
    // Its covering level goes (a tile of it still missing at an edge); the
    // next one only sharpens, and waits (SCRUB_SHARPEN_SPAN 0).
    for (const [level, x] of [[2, 0], [3, 0], [2, 1], [3, 1]]) {
        s.start(job(`${base}/${level}/${x}_0.png`, onScreen, { level }));
    }
    await tick();
    const posts = net.calls.filter((call) => call.options?.method === "POST");
    const tiles = posts.flatMap((call) => JSON.parse(call.options.body).tiles);
    check("a held tile never rides in a batch",
        posts.length === 1 && net.calls.length === 1 && s.queue.length === 2
        && tiles.join() === `${base}/2/0_0.png,${base}/2/1_0.png`,
        JSON.stringify({ calls: net.calls.length, tiles }));
    time.restore();
    delete box.PlexoraChannelFade;
}

// -- cancelled with the item that wanted it ----------------------------------------

{
    const { s, net } = scheduler();
    const gone = item({ opacity: 0 });
    const kept = item();
    const lonely = job("/t/only-gone", gone);
    const shared = job("/t/shared", gone);
    const sharedKept = job("/t/shared", kept);
    s.start(lonely);
    s.start(shared);
    s.start(sharedKept);
    s.cancelItem(gone);
    await tick();
    const onlyGone = net.calls.find((c) => c.url === "/t/only-gone");
    check("a removed item's jobs are finished at once, freeing their loader slots",
        lonely.finished?.dataType === "context2d" && shared.finished?.dataType === "context2d");
    check("a request nobody wants any more is aborted", onlyGone?.aborted === true);
    const sharedCall = net.calls.find((c) => c.url === "/t/shared");
    check("one still wanted by another item goes on", sharedCall && !sharedCall.aborted);
    sharedCall.respond();
    await tick(); await tick();
    check("and that item gets its plane", sharedKept.finished && sharedKept.tile._array);
}

{
    const { s, net } = scheduler();
    const a = job("/t/timeout", item());
    s.start(a);
    s.abort(a);                          // OSD's own timeout
    await tick();
    check("OSD giving up on a job aborts the request nobody else wants",
        net.calls[0].aborted === true);
}

// -- failures ------------------------------------------------------------------------

{
    const { s, net } = scheduler();
    const a = job("/t/500", item());
    s.start(a);
    net.calls[0].respond(webp(), 500);
    await tick(); await tick();
    check("a server error fails the job, and OSD may retry it",
        typeof a.failed === "string" && a.tries === 1);
    const b = job("/t/500", item());
    s.start(b);
    check("a retry is a new request", net.calls.length === 2);
}

{
    // OSD asks again with the same job, `tries` counted up by ImageJob.start,
    // while tries < 1 + tileRetryMax; between asks the tile is `exists` false.
    const { s, net } = scheduler();
    s.watch({ tileRetryMax: 3, world: { addHandler() {} } });
    const shown = item();
    const a = job("/t/flaky", shown);
    s.start(a);
    net.calls[0].respond(webp(), 500);
    await tick(); await tick();
    check("a 500 tags the tile retrying while OSD has retries left",
        a.tile._plexoraFailed === "retrying" && s.stats.retried === 1, JSON.stringify(s.stats));
    for (let tries = 2; tries <= 4; tries += 1) {
        a.tries = tries;
        s.start(a);
        net.calls[net.calls.length - 1].respond(webp(), 500);
        await tick(); await tick();
        if (tries === 3) check("still retrying on the third ask", a.tile._plexoraFailed === "retrying");
    }
    check("past tileRetryMax it is final: a hole, not a wait",
        a.tile._plexoraFailed === "final" && s.stats.retried === 3 && net.calls.length === 4,
        `${a.tile._plexoraFailed} after ${net.calls.length} asks`);
    // OSD's own mark for it; retryFailed makes it a tile OSD will ask for.
    Object.assign(a.tile, { exists: false, loaded: false, loading: false });
    let redraws = 0;
    shown.viewer = { forceRedraw() { redraws += 1; } };
    const untouched = { exists: false, loaded: false, loading: false };   // past the edge
    shown.tilesMatrix = { 3: { 0: { 0: a.tile, 1: untouched } } };
    const asked = s.retryFailed([shown, item()]);
    check("retryFailed clears it and has the tile asked for again",
        asked === 1 && !("_plexoraFailed" in a.tile) && a.tile.exists === true
        && a.tile.loaded === false && a.tile.loading === false && untouched.exists === false
        && shown._needsUpdate === true && shown._needsDraw === true && redraws === 1,
        JSON.stringify({ asked, tile: a.tile._plexoraFailed, exists: a.tile.exists, redraws }));
    const again = job("/t/flaky", shown);
    again.tile = a.tile;
    s.start(again);
    net.calls[net.calls.length - 1].respond();
    await tick(); await tick();
    check("and asked again, it loads with nothing left to say",
        net.calls.length === 5 && again.finished && !("_plexoraFailed" in a.tile));
    // One scheduler, several viewers: each job's own loader decides.
    const pane = item({ owner: "pane" });
    pane.viewer = { tileRetryMax: 0 };
    const once = job("/t/never-again", pane);
    s.start(once);
    net.calls[net.calls.length - 1].respond(webp(), 500);
    await tick(); await tick();
    check("a viewer that never retries makes the first failure final",
        once.tile._plexoraFailed === "final");
}

// -- batched on a slow link ---------------------------------------------------------------

const { BATCH } = box.PlexoraTileFetch;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/** A response with headers, as fetch gives one. */
function answer(buffer, { status = 200, timing = "" } = {}) {
    return { ok: status < 400, status, arrayBuffer: async () => buffer,
             headers: { get: (name) => (name === "Server-Timing" ? timing : null) } };
}
/** A batch body: a tile per path, its bytes "RIFF" and its index. */
function batchBody(paths, status = 200) {
    const parts = paths.map((_, i) => new Uint8Array([0x52, 0x49, 0x46, 0x46, i]));
    const header = new TextEncoder().encode(JSON.stringify({
        tiles: parts.map((part) => ({ status, bytes: part.length })) }));
    const out = new Uint8Array(4 + header.length + parts.reduce((n, part) => n + part.length, 0));
    new DataView(out.buffer).setUint32(0, header.length, true);
    out.set(header, 4);
    let at = 4 + header.length;
    for (const part of parts) {
        out.set(part, at);
        at += part.length;
    }
    return out.buffer;
}
/** A fetch whose single tiles take `wait` ms, `server` of them the server's. */
function slowFetch(wait, server) {
    const calls = [];
    const fetchImpl = async (url, options) => {
        const call = { url, options };
        calls.push(call);
        await sleep(wait);
        if (options?.method === "POST") {
            if (call.missing) return answer(new ArrayBuffer(0), { status: 404 });
            const { tiles } = JSON.parse(options.body);
            call.tiles = tiles;
            return answer(batchBody(tiles), { timing: `total;dur=${server}` });
        }
        return answer(webp(), { timing: `total;dur=${server}` });
    };
    return { calls, fetchImpl };
}
async function warm(s, prefix, count = BATCH.SAMPLES) {
    for (let i = 0; i < count; i += 1) {
        s.start(job(`${prefix}/warm/0/${i}_0.png`, item()));
        await sleep(1);
    }
    for (let i = 0; i < 40 && s.inFlight; i += 1) await sleep(10);
}

{
    const net = slowFetch(90, 5);
    const decoder = pool();
    const s = new TileFetchScheduler({ decoderPool: decoder, fetchImpl: net.fetchImpl,
                                       makeContext: () => ({ canvas: { width: 1, height: 1 } }) });
    await warm(s, "/generated/data/ds/ds_0");
    const link = s.linkOf("/generated/data/ds/ds_0/0/0_0.png").link;
    check("a link that keeps a tile waiting 85 ms past the server's time is batched",
          link.batching === true && link.wait > BATCH.ENTER_MS, JSON.stringify(link));
    const shown = item();
    const jobs = Array.from({ length: 20 }, (_, i) =>
        job(`/generated/data/ds/ds_0/2/${i}_1.png?view=z3`, shown, { level: 2, distance: i }));
    net.calls.length = 0;
    for (const j of jobs) s.start(j);
    await tick();
    const posts = net.calls.filter((call) => call.options?.method === "POST");
    check("queued tiles go BATCH_MAX at a time, in one POST each, to the server's batch route",
          posts.length === 2 && posts.every((call) => call.url === "http://127.0.0.1:8792/generated/batch"),
          JSON.stringify(net.calls.map((c) => [c.options?.method, c.url])));
    for (let i = 0; i < 40 && s.inFlight; i += 1) await sleep(10);
    await tick(); await tick();
    const sent = posts.flatMap((call) => call.tiles || []);
    check("in order of need, each as its path from the route",
          sent.length === 20 && sent[0] === "/generated/data/ds/ds_0/2/0_1.png?view=z3"
          && sent[16] === "/generated/data/ds/ds_0/2/16_1.png?view=z3", JSON.stringify(sent.slice(0, 2)));
    check("every job is finished with its own tile",
          jobs.every((j) => j.finished?.dataType === "context2d")
          && decoder.seen.length >= 20 && s.stats.batches === 2 && s.stats.batchedTiles === 20,
          JSON.stringify(s.stats));
    const indices = decoder.seen.slice(-20).map((seen) => new Uint8Array(seen.buffer)[4]).sort((a, b) => a - b);
    check("each part its own buffer",
          // a batch of 16 and one of 4: parts 0-15 and 0-3
          indices.join() === [0, 0, 1, 1, 2, 2, 3, 3].concat(Array.from({ length: 12 }, (_, i) => i + 4)).join(),
          indices.join());
}

{
    const net = slowFetch(90, 85);
    const s = new TileFetchScheduler({ decoderPool: pool(), fetchImpl: net.fetchImpl,
                                       makeContext: () => ({ canvas: { width: 1, height: 1 } }) });
    await warm(s, "/generated/data/slow/slow_0");
    check("a slow server on a fast link is not batched",
          s.linkOf("/generated/data/slow/slow_0/0/0_0.png").link.batching === false);
}

{
    // A busy page: most tiles answer at once, some after a long stall.
    let n = 0;
    const busy = async (url, options) => {
        n += 1;
        await sleep(n % 3 ? 2 : 150);
        return answer(webp(), { timing: "total;dur=1" });
    };
    const s = new TileFetchScheduler({ decoderPool: pool(), fetchImpl: busy,
                                       makeContext: () => ({ canvas: { width: 1, height: 1 } }) });
    await warm(s, "/generated/data/busy/busy_0", 12);
    check("stalls among quick tiles are a busy page, not a slow link",
          s.linkOf("/generated/data/busy/busy_0/0/0_0.png").link.batching === false);
}

{
    const net = slowFetch(90, 0);
    const s = new TileFetchScheduler({ decoderPool: pool(), fetchImpl: net.fetchImpl,
                                       makeContext: () => ({ canvas: { width: 1, height: 1 } }) });
    const node = "http://node.example:7000/node/v1/image/r1/tile/r1_0";
    await warm(s, node);
    const where = s.linkOf(`${node}/0/0_0.png?tw=1024&th=1024&t=SECRET`);
    check("a node's batch route is its own, behind its token",
          where.link.url === "http://node.example:7000/node/v1/batch?t=SECRET"
          && where.tilePath === "/node/v1/image/r1/tile/r1_0/0/0_0.png?tw=1024&th=1024&t=SECRET",
          JSON.stringify(where));
    // A node without the route: 404 once, then single tiles for good.
    const old = slowFetch(90, 0);
    const posts404 = [];
    const t = new TileFetchScheduler({ decoderPool: pool(), fetchImpl: async (url, options) => {
        if (options?.method === "POST") {
            posts404.push(url);
            return answer(new ArrayBuffer(0), { status: 404 });
        }
        return old.fetchImpl(url, options);
    }, makeContext: () => ({ canvas: { width: 1, height: 1 } }) });
    await warm(t, "/generated/data/old/old_0");
    const late = Array.from({ length: 3 }, (_, i) => job(`/generated/data/old/old_0/1/${i}_0.png`, item()));
    for (const j of late) t.start(j);
    for (let i = 0; i < 60 && (t.inFlight || t.queue.length); i += 1) await sleep(10);
    await tick();
    const link = t.linkOf("/generated/data/old/old_0/0/0_0.png").link;
    check("a server without the batch route is asked singly from then on",
          link.unbatchable && !link.batching && late.every((j) => j.finished) && posts404.length === 1,
          JSON.stringify({ link, posts: posts404.length, done: late.map((j) => Boolean(j.finished)) }));
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
