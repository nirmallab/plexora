/**
 * One scheduler for a volume's channel and section tiles (views/tileFetch.js).
 *
 * Run against a fake fetch whose responses arrive when the probe says, OSD
 * jobs that record how they were finished, and a decoder pool that records
 * what it was handed. Pinned:
 *
 * - one request per address, however many items ask for it (a channel's
 *   cover and paint, a handover, a prefetch); every job gets the plane;
 * - in order of need: on screen before a handover before a prefetch, the
 *   plane before the panes, coarse levels before fine, near before far;
 *   never more than MAX_IN_FLIGHT at once;
 * - decoded once, the response's buffer TRANSFERRED to the worker (8-bit) and
 *   the job finished as a `context2d` the size of the tile -- so OSD's own
 *   image decode never runs;
 * - an item removed from its world gives up its jobs: finished at once (its
 *   loader slot freed), and a request nobody wants any more is aborted;
 * - an aborted download is never retried; a failed one is, by OSD;
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
    check("at most one prefetch request on the wire, however many are queued",
        net.calls.length === 1);
    s.start(job("/t/visible", item()));
    check("and a tile on screen goes out at once beside it",
        net.calls.length === 2 && net.calls[1].url === "/t/visible");
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
