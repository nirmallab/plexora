/**
 * A pool of the browser node's workers opens a file once (services/
 * browserNode.js TiffPool.open): one reader opens it from the file with
 * `record`, and the others open from what it read (`seed`, node/tiffReader.js
 * SeededSource). Run in Node over the same module the workers load:
 *
 *   node tests/js/tiff_seed_probe.mjs FILE.tif
 *
 * Prints {first, seeded, geometrySame, tiles, differ} as JSON: the reads each
 * open made, whether the two geometries are equal, and how many tiles (the
 * first and the last of every channel's every level, HD and raw) were read by
 * both readers and how many differ. tests/test_tiff_writer.py holds it to
 * "the seeded open reads nothing and every tile is the same".
 */

import { closeSync, fstatSync, openSync, readSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { isDeepStrictEqual } from "node:util";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const NODE = path.join(REPO, "plexora", "client", "src", "js", "node");
const { createTiffReader } = await import(pathToFileURL(path.join(NODE, "tiffReader.js")).href);

/** geotiff.js's source over a file descriptor, counting its reads. */
class FileSource {
    constructor(file) {
        this.fd = openSync(file, "r");
        this.size = fstatSync(this.fd).size;
        this.bytes = 0;
        this.reads = 0;
        this.readMs = 0;
    }

    get fileSize() {
        return this.size;
    }

    async fetch(slices) {
        return slices.map(({ offset, length }) => {
            // As a range request answers past the end: what there is.
            const out = new Uint8Array(Math.max(0, Math.min(length, this.size - offset)));
            readSync(this.fd, out, 0, out.length, offset);
            this.reads += 1;
            this.bytes += out.length;
            return out.buffer;
        });
    }

    close() {
        closeSync(this.fd);
    }
}

const file = process.argv[2];
const one = createTiffReader();
const firstSource = new FileSource(file);
const first = await one.open({ source: firstSource, record: true });
// What a worker receives: a structured clone of the recorded slices.
const seed = structuredClone(first.seed);

const two = createTiffReader();
const seededSource = new FileSource(file);
const seeded = await two.open({ source: seededSource, seed });
const seededReads = seededSource.reads;

const { geometry } = first;
const tiles = [];
const TILE = 256;
for (let channel = 0; channel < geometry.numChannels; channel += 1) {
    geometry.levels.forEach(({ width, height }, level) => {
        const last = [Math.ceil(width / TILE) - 1, Math.ceil(height / TILE) - 1];
        for (const [tx, ty] of [[0, 0], last]) {
            for (const raw of [false, true]) tiles.push({ channel, level, tx, ty, tw: TILE, th: TILE, raw });
        }
    });
}
let differ = 0;
for (const ask of tiles) {
    const a = await one.tile(ask);
    const b = await two.tile(ask);
    if (a.width !== b.width || a.height !== b.height || a.format !== b.format
        || Buffer.compare(Buffer.from(a.array), Buffer.from(b.array)) !== 0) differ += 1;
}
// The main IFDs' offsets as Python gives them (node_spec.main_ifds), and the
// same list spoiled: the first is taken, the second is not.
const hints = {};
if (process.argv[3]) {
    const ifds = JSON.parse(process.argv[3]);
    const spoiled = ifds.length > 1 ? [ifds[0], ...ifds.slice(1).reverse(), ifds[1]].slice(0, ifds.length)
        : [ifds[0] + 8];
    for (const [name, list] of Object.entries({ right: ifds, spoiled, wrongFirst: [ifds[0] + 2, ...ifds.slice(1)] })) {
        const reader = createTiffReader();
        const source = new FileSource(file);
        const opened = await reader.open({ source, ifds: list });
        const a = await reader.tile({ channel: geometry.numChannels - 1, level: 0, tx: 0, ty: 0, tw: TILE, th: TILE });
        const b = await one.tile({ channel: geometry.numChannels - 1, level: 0, tx: 0, ty: 0, tw: TILE, th: TILE });
        hints[name] = { hinted: opened.timing.hinted, geometrySame: isDeepStrictEqual(opened.geometry, geometry),
                        tileSame: Buffer.compare(Buffer.from(a.array), Buffer.from(b.array)) === 0 };
        source.close();
    }
}
firstSource.close();
seededSource.close();
console.log(JSON.stringify({
    first: { reads: first.timing.reads, recorded: first.seed.length },
    seeded: { reads: seededReads },
    geometrySame: isDeepStrictEqual(first.geometry, seeded.geometry),
    layout: geometry.layout,
    levels: geometry.levels.length,
    tiles: tiles.length,
    differ,
    hints,
}));
