/**
 * A pool of the browser node's workers opens a file once (services/
 * browserNode.js TiffPool.open): one reader opens it from the file with
 * `record`, and the others open from what it read (`seed`, node/tiffReader.js
 * SeededSource). Run in Node over the same module the workers load:
 *
 *   node tests/js/tiff_seed_probe.mjs FILE.tif
 *
 * Prints {first, seeded, geometrySame, tiles, differ, runs} as JSON: the reads
 * each open made, whether the two geometries are equal, and how many tiles
 * (the first and the last of every channel's every level, HD and raw) were
 * read by both readers and how many differ; `runs`, the first and last rows
 * of every level read as rows (`tiles`) by a reader seeded with another's
 * `index` of each level: rows, tiles, how many differ, and the file reads
 * they took. tests/test_tiff_writer.py holds it to "the seeded open reads
 * nothing, every tile is the same, and a row of a tiled level is one read".
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
// Tiles side by side in one read (`tiles`), by a reader that parses no level
// from the file: each level is parsed once by another (`index`, its reads
// recorded) and handed over (`seed`), as a pool of the page's workers does.
// A row's tiles sit back to back in these files, so each row is one read.
const indexer = createTiffReader();
const indexSource = new FileSource(file);
await indexer.open({ source: indexSource, seed: structuredClone(first.seed) });
const three = createTiffReader();
const runSource = new FileSource(file);
await three.open({ source: runSource, seed: structuredClone(first.seed) });
const runs = { rows: 0, tiles: 0, differ: 0, reads: 0, recorded: 0 };
for (let channel = 0; channel < geometry.numChannels; channel += 1) {
    for (let level = 0; level < geometry.levels.length; level += 1) {
        const { seed: recorded } = await indexer.index({ channel, level });
        runs.recorded += recorded.length;
        three.seed({ slices: structuredClone(recorded) });
        const { width, height } = geometry.levels[level];
        const across = Math.ceil(width / TILE);
        const before = runSource.reads;
        for (const ty of [...new Set([0, Math.ceil(height / TILE) - 1])]) {
            for (const raw of [false, true]) {
                const { tiles: row } = await three.tiles({ channel, level, tx0: 0, ty, count: across, tw: TILE, th: TILE, raw });
                runs.rows += 1;
                for (let tx = 0; tx < across; tx += 1) {
                    const a = await one.tile({ channel, level, tx, ty, tw: TILE, th: TILE, raw });
                    const b = row[tx];
                    runs.tiles += 1;
                    if (a.width !== b.width || a.height !== b.height || a.format !== b.format
                        || Buffer.compare(Buffer.from(a.array), Buffer.from(b.array)) !== 0) runs.differ += 1;
                }
            }
        }
        runs.reads += runSource.reads - before;
    }
}
indexSource.close();
runSource.close();
// The main IFDs' offsets as Python gives them (node_spec.main_ifds), and the
// same list spoiled: the first is taken, the second is not; the reads each
// open made.
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
        hints[name] = { hinted: opened.timing.hinted, reads: opened.timing.reads,
                        geometrySame: isDeepStrictEqual(opened.geometry, geometry),
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
    runs,
    hints,
}));
