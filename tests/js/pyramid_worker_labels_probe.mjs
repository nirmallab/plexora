/**
 * The website's pyramid build worker (client src/js/workers/pyramidWorker.js)
 * run in Node on a flat label mask, the way services/browserPyramid.js asks
 * for one: {op: "build", kind: "labels", ...}. Its hands (the browser node's
 * tiff workers) are stood in by in-process ones that answer with the real
 * node/pyramid.js `bandParts` over the mask's raw samples, and deflate with
 * zlib (what CompressionStream("deflate") writes).
 *
 * Where the copy goes is the probe's `mode`:
 *   "memory": no `folder`, and the site's storage gives no sync access handle
 *             (a private window, WebKit on Windows), so the worker puts the
 *             copy together in memory (`kept: "memory"`).
 *   "folder": a folder the user granted (a writable stream).
 *
 * Run:  node tests/js/pyramid_worker_labels_probe.mjs SPEC.json
 *   SPEC: {mode, raw, out, name, kind, shape: [1, H, W], dtype, factors, tile, description}
 * Writes the copy to `out` and prints {done, kept, bytes} or {error} as JSON;
 * tests/test_pyramid_worker_labels.py reads the copy with tifffile.
 */

import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { deflateSync } from "node:zlib";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = path.join(REPO, "plexora", "client", "src", "js");
const { bandParts } = await import(pathToFileURL(path.join(JS, "node", "pyramid.js")).href);
const { DTYPES } = await import(pathToFileURL(path.join(JS, "node", "tiffWriter.js")).href);

const spec = JSON.parse(readFileSync(process.argv[2], "utf-8"));
const [, height, width] = spec.shape;
const Kind = DTYPES[spec.dtype].Array;
const raw = readFileSync(spec.raw);
const samples = new Kind(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength));

/** A reader over the raw samples, as node/tiffReader.js `region` answers. */
const reader = {
    async region({ level, x0, y0, x1, y1 }) {
        if (level !== 0) throw new Error("only level 0 is read");
        const w = x1 - x0;
        const values = new Kind(w * (y1 - y0));
        for (let y = y0; y < y1; y += 1) values.set(samples.subarray(y * width + x0, y * width + x1), (y - y0) * w);
        return { values, timing: { decodeMs: 0 } };
    },
};
const geometry = { numChannels: 1, width, height, bitsPerSample: DTYPES[spec.dtype].bits,
                   sampleFormat: DTYPES[spec.dtype].format, fileTile: { tiled: false } };

/** One of the browser node's tiff workers, answering in this process. */
globalThis.Worker = class {
    constructor() { this.onmessage = null; this.onerror = null; }
    postMessage({ id, op, ...args }) {
        const answer = async () => {
            if (op === "open") return { geometry };
            if (op === "block") return bandParts(reader, args);
            if (op === "deflate") return { bytes: new Uint8Array(deflateSync(new Uint8Array(args.buffer))) };
            throw new Error(`no op ${op}`);
        };
        answer().then((result) => this.onmessage({ data: { id, ok: true, result } }),
                      (error) => this.onmessage({ data: { id, ok: false, error: String(error && error.message) } }));
    }
    terminate() {}
};

// The site's storage: none that takes a sync access handle (a private window).
Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { storage: { getDirectory: () => Promise.reject(new DOMException("no storage here", "SecurityError")) } },
});

/** A folder the user granted: one file written through a writable stream. */
function grantedFolder() {
    let bytes = new Uint8Array(0);
    let created = false;
    const handle = {
        async createWritable() {
            return {
                async write({ position, data }) {
                    const end = position + data.byteLength;
                    if (end > bytes.byteLength) {
                        const grown = new Uint8Array(end);
                        grown.set(bytes);
                        bytes = grown;
                    }
                    bytes.set(data, position);
                },
                async close() {},
                async abort() {},
            };
        },
        async getFile() { return new File([bytes], spec.name); },
    };
    return {
        async getFileHandle(name, options = {}) {
            if (!options.create && !created) throw new DOMException("not found", "NotFoundError");
            created = true;
            return handle;
        },
    };
}

const posted = [];
let settle;
const finished = new Promise((resolve) => { settle = resolve; });
globalThis.self = {
    postMessage(message) {
        posted.push(message);
        if (message.done || message.error || message.exists) settle(message);
    },
};
await import(pathToFileURL(path.join(JS, "workers", "pyramidWorker.js")).href);

const file = new File([raw], "cell.ome.tif");
await self.onmessage({ data: {
    op: "build", file, key: "k", name: spec.name, kind: spec.kind, shape: spec.shape, dtype: spec.dtype,
    factors: spec.factors, tile: spec.tile, description: spec.description, hands: "tiffWorker.js", workers: 2,
    folder: spec.mode === "folder" ? grantedFolder() : undefined,
} });
const message = await finished;
if (message.error) {
    console.log(JSON.stringify({ error: message.error }));
} else {
    writeFileSync(spec.out, new Uint8Array(await message.file.arrayBuffer()));
    console.log(JSON.stringify({ done: Boolean(message.done), kept: message.kept || null, bytes: message.bytes }));
}
process.exit(0);
