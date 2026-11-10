/**
 * The website's browser node reading tiles side by side together (services/
 * browserNode.js TiffPool `flush`, `readRun`, `indexed`, and `group`). Run in
 * Node over the real module, its workers stood in for and logging every op:
 *
 *   node tests/js/browser_node_runs_probe.mjs
 *
 * Prints JSON; tests/test_browser_node_runs.py holds it to the expectations.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "browserNode.js"), "utf8");

const files = {
    "/picked/a/tiled.ome.tif": { name: "tiled.ome.tif", size: 10, lastModified: 1 },
    "/picked/b/flat.tif": { name: "flat.tif", size: 11, lastModified: 1 },
};
const geometries = {
    "tiled.ome.tif": { layout: "subifd", fileTile: { width: 256, height: 256, tiled: true }, compression: 5,
                       numChannels: 3, bitsPerSample: 16, levels: [{ width: 1536, height: 1024 }, { width: 768, height: 512 }] },
    "flat.tif": { layout: "flat", fileTile: { width: 1536, height: 16, tiled: false }, compression: 5,
                  numChannels: 3, bitsPerSample: 16, levels: [{ width: 1536, height: 1024 }] },
};

// -- the workers: every op logged, answered a moment later ---------------------
const log = [];          // {worker, op, ...message}
let workers = 0;
let failIndexOf = null;  // a channel whose `index` fails
class Worker {
    constructor() { this.id = workers++; this.onmessage = null; this.onerror = null; }

    postMessage(message) {
        const { id, op } = message;
        log.push({ worker: this.id, ...message, file: undefined, seed: undefined, slices: undefined,
                   slicesCount: message.slices ? message.slices.length : undefined });
        const reply = (result, ok = true) => setTimeout(() => this.onmessage && this.onmessage({
            data: ok ? { id, ok, result } : { id, ok, error: String(result) } }), 2);
        const tile = (tx, ty) => ({ width: 2, height: 2, format: "u16", array: new Uint8Array([message.channel, message.level, tx, ty, 0, 0, 0, 0]) });
        if (op === "open") {
            this.file = message.file;
            reply({ geometry: geometries[message.file.name], seed: message.record ? [] : null, timing: {} });
        } else if (op === "tile") {
            reply(tile(message.tx, message.ty));
        } else if (op === "tiles") {
            reply({ tiles: Array.from({ length: message.count }, (_, k) => tile(message.tx0 + k, message.ty)) });
        } else if (op === "index") {
            if (message.channel === failIndexOf) reply("Error: no such level", false);
            else reply({ seed: [{ offset: 100 * message.channel + message.level, length: 4, buffer: new ArrayBuffer(4) }] });
        } else if (op === "seed") {
            reply({});
        }
    }

    terminate() {}
}

const spec = (name, pathOf, levels) => ({
    served: true, name, generation: 1, tile: [256, 256], path: pathOf, format: "ome-tiff",
    levels, dtype: "uint16", channels: ["a", "b", "c"], labels: { served: false, reason: "none" },
});
const specs = {
    t: spec("t", "/picked/a/tiled.ome.tif", [[3, 1024, 1536], [3, 512, 768]]),
    f: spec("f", "/picked/b/flat.tif", [[3, 1024, 1536]]),
};
const window = {
    navigator: { hardwareConcurrency: 8 },
    location: { href: "https://viewer.example/p" },
    PlexoraPicks: { fileFor: (p) => files[p] || null },
    PlexoraPython: { nodeSpec: async (name) => JSON.parse(JSON.stringify(specs[name])) },
};
const context = vm.createContext({ window, Worker, setTimeout, clearTimeout, console, performance, Date,
                                   URL, Uint8Array, Uint16Array, Uint32Array, ArrayBuffer, DataView, TextEncoder, Map, Set,
                                   Promise, Math, Number, String, Object, Array, Error, JSON, Response: class {} });
vm.runInContext(SOURCE, context);
const node = window.PlexoraNode;

const url = (project, channel, level, tx, ty, q = "hd") => `/generated/data/${project}/c_${channel}/${level}/${tx}_${ty}.png?q=${q}`;
/** Ask for tiles in one turn, as the viewer's scheduler hands over a group. */
const ask = (urls) => Promise.all(urls.map((u) => node.decoded(u)));
const since = (mark, op) => log.slice(mark).filter((entry) => entry.op === op);
const tileOf = (answer) => (answer ? Array.from(answer.array.slice(0, 4)) : null);
const out = {};

// Opening: every worker of the pool opens the file.
await ask([url("t", 0, 1, 0, 0)]);

// 1. A row's three tiles asked together: one read of the three, each answer its own tile.
let mark = log.length;
let answers = await ask([url("t", 0, 0, 1, 2), url("t", 0, 0, 0, 2), url("t", 0, 0, 2, 2)]);
out.rowOfThree = {
    ops: since(mark, "tiles").map(({ channel, level, tx0, ty, count }) => ({ channel, level, tx0, ty, count })),
    single: since(mark, "tile").length,
    answers: answers.map(tileOf),
};

// 2. Five side by side: RUN_MAX (4) at most in one read.
mark = log.length;
await ask([0, 1, 2, 3, 4].map((tx) => url("t", 0, 0, tx, 3)));
out.rowOfFive = since(mark, "tiles").map(({ tx0, count }) => [tx0, count]);

// 3. Not side by side: another channel, another row, a gap.
mark = log.length;
answers = await ask([url("t", 1, 0, 0, 0), url("t", 2, 0, 1, 0), url("t", 1, 0, 0, 1), url("t", 1, 0, 2, 0)]);
out.apart = {
    ops: since(mark, "tiles").map(({ channel, tx0, ty, count }) => [channel, tx0, ty, count]),
    answers: answers.map(tileOf),
};

// 4. A level is parsed once a pool: the first read of channel 2 at level 1
// has one worker `index` it and every other worker `seed`ed before it reads
// anything of that level; a second read of it parses nothing.
mark = log.length;
await ask([url("t", 2, 1, 0, 0), url("t", 2, 1, 1, 0)]);
await ask([url("t", 2, 1, 0, 1)]);
const indexOps = since(mark, "index").filter((entry) => entry.channel === 2 && entry.level === 1);
const indexer = indexOps.length ? indexOps[0].worker : null;
const seededBefore = [];
for (let w = 0; w < workers; w += 1) {
    if (w === indexer) continue;
    const mine = log.slice(mark).filter((entry) => entry.worker === w);
    const seedAt = mine.findIndex((entry) => entry.op === "seed");
    const firstRead = mine.findIndex((entry) => entry.op === "tiles" && entry.channel === 2 && entry.level === 1);
    if (seedAt >= 0) seededBefore.push(firstRead < 0 || seedAt < firstRead);
}
out.index = { ops: indexOps.length, seeded: seededBefore.length, seededFirst: seededBefore.every(Boolean),
              workers: workers };

// 5. A level whose `index` fails is still read (each worker parses it itself).
failIndexOf = 1;
mark = log.length;
answers = await ask([url("t", 1, 1, 0, 0), url("t", 1, 1, 1, 0)]);
out.indexFailed = { answered: answers.every((answer) => answer && answer.width === 2), seeds: since(mark, "seed").length };
failIndexOf = null;

// 6. A file read in strips: one tile at a time, as before.
mark = log.length;
answers = await ask([url("f", 0, 0, 0, 0), url("f", 0, 0, 1, 0), url("f", 0, 0, 2, 0)]);
out.strips = { single: since(mark, "tile").length, runs: since(mark, "tiles").length,
               answered: answers.every((answer) => answer && answer.width === 2) };

// 7. `group`: which queued tiles go with one (views/tileFetch.js `localGroup`).
const queued = [
    url("t", 0, 0, 4, 1),            // 0: right of it
    url("t", 0, 0, 5, 1),            // 1: right of that
    url("t", 0, 0, 2, 1),            // 2: left of it
    url("t", 0, 0, 6, 1),            // 3: one past RUN_MAX
    url("t", 1, 0, 4, 1),            // 4: another channel
    url("t", 0, 1, 4, 1),            // 5: another level
    url("t", 0, 0, 4, 2),            // 6: another row
    url("t", 0, 0, 4, 1, "lo"),      // 7: another quality
    `${url("t", 0, 0, 4, 1)}&view=z3`, // 8: a volume's view: never the page's
    "/somewhere/else.png",           // 9
];
out.group = {
    picked: [...node.group(url("t", 0, 0, 3, 1), queued)].sort((a, b) => a - b),
    alone: [...node.group(url("t", 0, 0, 9, 9), queued)],
    notATile: [...node.group("/somewhere/else.png", queued)],
};

console.log(JSON.stringify(out));
process.exit(0);
