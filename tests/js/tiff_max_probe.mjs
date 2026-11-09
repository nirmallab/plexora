/**
 * The page's scan of a channel's full-resolution maximum (node/tiffReader.js
 * `max`, what data_model.quantization_window_of reads every pixel for): the
 * tiled fast path held to the window-by-window `region` reads it replaced.
 * Run in Node over the module the workers load:
 *
 *   node tests/js/tiff_max_probe.mjs FILE.tif CHANNELS
 *
 * For each channel of level 0, the maximum over the whole plane in the
 * plane's own windows (as services/browserNode.js plans them) and over a few
 * windows that do not fall on tiles, by `max` and by `region`. Prints
 * {channels: [{channel, tiled, whole, regionWhole, odd, regionOdd}], regions:
 * [{channel, level, box, width, height, values | refused}]} as JSON: the
 * rectangles `regions` reads for Python (data_model's cell crops).
 * tests/test_tiff_writer.py holds both to each other and to numpy.
 */

import { closeSync, fstatSync, openSync, readSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const NODE = path.join(REPO, "plexora", "client", "src", "js", "node");
const { createTiffReader } = await import(pathToFileURL(path.join(NODE, "tiffReader.js")).href);

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
            const out = new Uint8Array(Math.max(0, Math.min(length, this.size - offset)));
            readSync(this.fd, out, 0, out.length, offset);
            return out.buffer;
        });
    }
}

/** services/browserNode.js `planeWindows`, for a tiled file. */
function planeWindows({ width, height }, tile) {
    const step = (dim) => dim * Math.max(1, Math.round(4096 / dim));
    const windows = [];
    for (let y = 0; y < height; y += step(tile.height)) {
        for (let x = 0; x < width; x += step(tile.width)) {
            windows.push([x, y, Math.min(width, x + step(tile.width)), Math.min(height, y + step(tile.height))]);
        }
    }
    return windows;
}

const [file, count] = process.argv.slice(2);
const reader = createTiffReader();
const { geometry } = await reader.open({ source: new FileSource(file) });
const level = geometry.levels[0];
const regionMax = async (channel, windows) => {
    let top = -Infinity;
    for (const [x0, y0, x1, y1] of windows) {
        const { values } = await reader.region({ channel, level: 0, x0, y0, x1, y1 });
        for (let i = 0; i < values.length; i += 1) if (values[i] > top) top = values[i];
    }
    return top;
};
const odd = [[3, 5, Math.min(level.width, 141), Math.min(level.height, 77)],
             [Math.max(0, level.width - 97), Math.max(0, level.height - 61), level.width, level.height]];
const channels = [];
for (let channel = 0; channel < Number(count); channel += 1) {
    const windows = planeWindows(level, geometry.fileTile);
    const whole = await reader.max({ channel, level: 0, windows });
    const part = await reader.max({ channel, level: 0, windows: odd });
    channels.push({ channel, tiled: Boolean(whole.timing.tiled), whole: whole.max,
                    regionWhole: await regionMax(channel, windows), odd: part.max, regionOdd: await regionMax(channel, odd) });
}
// `regions` (data_model's cell crops, read by the page): a few rectangles of
// level 0 -- inside one tile, across tile edges, at the image's edge.
const boxes = [[3, 5, 41, 37], [100, 100, 190, 170], [level.width - 50, level.height - 33, level.width, level.height]];
const asked = [];
for (let channel = 0; channel < Number(count); channel += 1) for (const box of boxes) asked.push({ channel, level: 0, box });
const { regions } = await reader.regions({ reads: asked });
const read = regions.map((one, i) => ({ ...asked[i], width: one.width, height: one.height, refused: one.refused || null,
                                       values: one.values ? Array.from(one.values) : null }));
console.log(JSON.stringify({ channels, regions: read }));
