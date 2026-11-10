/**
 * The website's browser node when a project's spec changes under tiles being
 * read (services/browserNode.js `check`, `recheck`, TiffPool): the mask the
 * page built is attached (its `labels` served, a new `generation`) while the
 * viewer reads the image's tiles. Run in Node over the real module, its
 * workers stood in for and answering when the probe says:
 *
 *   node tests/js/browser_node_swap_probe.mjs
 *
 * Prints JSON; tests/test_browser_node_swap.py holds it to the expectations.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "browserNode.js"), "utf8");

const W = 1024;
const H = 1024;
const files = {
    "/picked/a/image.ome.tif": { name: "image.ome.tif", size: 10, lastModified: 1 },
    "/picked/c/other.ome.tif": { name: "other.ome.tif", size: 11, lastModified: 1 },
    "/picked/b/cell.labels.pyramid.ome.tiff": { name: "cell.labels.pyramid.ome.tiff", size: 20, lastModified: 1 },
};
const isMask = (file) => file && file.name.startsWith("cell");

// -- the workers: each tile read is held until the probe lets it go -----------------
const held = [];
const counts = { opens: { image: 0, mask: 0 }, terminated: 0, alive: 0 };
class Worker {
    constructor() { this.onmessage = null; this.onerror = null; counts.alive += 1; }

    postMessage(message) {
        if (this.dead) return;
        const reply = (result) => { if (!this.dead && this.onmessage) this.onmessage({ data: { id: message.id, ok: true, result } }); };
        if (message.op === "open") {
            this.file = message.file;
            const mask = isMask(message.file);
            if (message.record) counts.opens[mask ? "mask" : "image"] += 1;
            setTimeout(() => reply({ geometry: mask
                ? { compression: 1, numChannels: 1, bitsPerSample: 32, sampleFormat: 1, levels: [{ width: W, height: H }] }
                : { compression: 1, numChannels: 3, bitsPerSample: 16, levels: [{ width: W, height: H }] }, seed: null, timing: {} }), 1);
            return;
        }
        if (message.op === "tile") {
            const tile = { width: 4, height: 4, format: isMask(this.file) ? "u32" : "u16",
                           array: isMask(this.file) ? new Uint32Array(16).fill(7) : new Uint16Array(16).fill(9) };
            held.push({ message, mask: isMask(this.file), release: () => reply(tile) });
        }
    }

    terminate() { this.dead = true; counts.terminated += 1; counts.alive -= 1; }
}

// -- Python's spec, and how long it takes to give it -------------------------------
const imageSpec = (generation, labels, path = "/picked/a/image.ome.tif") => ({
    served: true, name: "p", generation, tile: [256, 256], path, format: "ome-tiff",
    levels: [[3, H, W]], dtype: "uint16", channels: ["a", "b", "c"], labels,
});
const pending = { served: false, pending: true, reason: "the mask is being prepared" };
const servedMask = { served: true, path: "/picked/b/cell.labels.pyramid.ome.tiff", format: "ome-tiff",
                     levels: [[0, H, W]], dtype: "uint32" };
let spec = imageSpec(1, pending);
let specGate = null;   // a promise Python's answer waits on, when set
let specAsked = 0;

const clock = { now: 1_000_000 };
class FakeDate extends Date { static now() { return clock.now; } }

const window = {
    navigator: { hardwareConcurrency: 4 },
    location: { href: "https://viewer.example/p" },
    PlexoraPicks: { fileFor: (p) => files[p] || null },
    PlexoraPython: { nodeSpec: async () => { specAsked += 1; if (specGate) await specGate; return JSON.parse(JSON.stringify(spec)); } },
};
const context = vm.createContext({ window, Worker, setTimeout, clearTimeout, console, performance, Date: FakeDate,
                                   URL, Uint8Array, Uint16Array, Uint32Array, DataView, TextEncoder, Map, Set,
                                   Promise, Math, Number, String, Object, Array, Error, JSON, Response: class {} });
vm.runInContext(SOURCE, context);
const node = window.PlexoraNode;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const tileUrl = (channel, x) => `/generated/data/p/p_${channel}/0/${x}_0.png?q=hd`;
const maskUrl = (x) => `/generated/data/p/cell/0/${x}_0.png?v=2`;
/** How `promise` ends within `ms`: {ok, value} | {ok: false, error} | "pending". */
async function within(promise, ms) {
    return Promise.race([promise.then((value) => ({ ok: true, value }), (error) => ({ ok: false, error: String(error.message || error) })),
                         sleep(ms).then(() => "pending")]);
}
const releaseAll = () => { while (held.length) held.shift().release(); };
/** Let go the reads `which` picks; the others stay held. */
const release = (which) => { for (const read of held.filter(which)) { held.splice(held.indexOf(read), 1); read.release(); } };
const out = {};

// 1. The image's tiles are read; the mask is being prepared.
const first = node.decoded(tileUrl(0, 0));
await sleep(20);
releaseAll();
out.firstTile = (await within(first, 200)).ok;
out.maskWhilePending = await within(node.decoded(maskUrl(0), { raster: true }), 200);

// 2. Tiles in flight when the mask arrives: Python attaches it (served, a new
// generation) and answers a moment later. A mask tile asks again (after
// RECHECK_MS); the image's tiles asked meanwhile do not wait for the answer.
const inFlight = [node.decoded(tileUrl(0, 1)), node.decoded(tileUrl(1, 1)), node.decoded(tileUrl(2, 1))];
await sleep(10);
spec = imageSpec(2, servedMask);
let openSpec;
specGate = new Promise((resolve) => { openSpec = resolve; });
clock.now += 3500;
const maskTile = node.decoded(maskUrl(1), { raster: true });
await sleep(10);
const duringRecheck = node.decoded(tileUrl(1, 2));
await sleep(20);
release((read) => read.message.tx === 2);
out.duringRecheck = (await within(duringRecheck, 200)).ok === true;
openSpec();
specGate = null;
await sleep(30);
release((read) => read.mask);   // the mask tile's read, from the mask's new readers
const maskAnswer = await within(maskTile, 300);
out.maskAfterAttach = maskAnswer.ok === true && Boolean(maskAnswer.value && maskAnswer.value.raster);
// The tiles that were being read when the spec changed, answered only now:
// from the image's readers, which stayed open.
release((read) => read.message.tx === 1);
const settled = await Promise.all(inFlight.map((p) => within(p, 300)));
out.inFlightAnswered = settled.map((s) => (s === "pending" ? "pending" : s.ok ? "ok" : `failed: ${s.error}`));
releaseAll();
out.imageOpens = counts.opens.image;
out.maskOpens = counts.opens.mask;

// 3. A different image under the same project: its old readers close, and a
// read they had not answered fails (the viewer then asks Python) rather than
// waiting for ever.
const stranded = node.decoded(tileUrl(0, 5));
stranded.catch(() => null);   // the viewer's scheduler catches it (views/tileFetch.js answerLocally)
await sleep(10);
spec = imageSpec(3, servedMask, "/picked/c/other.ome.tif");
node.revalidate();
const next = node.decoded(tileUrl(0, 6));
await sleep(30);
const strandedEnd = await within(stranded, 300);
out.strandedRead = strandedEnd === "pending" ? "pending" : strandedEnd.ok ? "ok" : "failed";
releaseAll();
out.nextAfterSwap = (await within(next, 300)).ok === true;
out.imageOpensAfterSwap = counts.opens.image;
out.specAsked = specAsked;

console.log(JSON.stringify(out));
process.exit(0);
