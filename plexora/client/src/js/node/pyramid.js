/**
 * A flat image made into Plexora's pyramid by the page: the file
 * image_pyramid.pyramidize_image writes (node/tiffWriter.js), with the levels
 * it computes (node/reduce.js), from the browser-native investigation's P7
 * (0.1-0.9x Python's time over 0.03-9.6 GB, tile-identical).
 *
 * Level 0 is cut into bands, per channel: a tiled source one tile row high and
 * a run of tiles across; a stripped one the full width (a strip spans every
 * column) and as many rows as `jobBytes` holds, in steps of the coarsest
 * factor, so every level's part of a band is whole (reduce.js says why bands
 * are then independent). A hand reads a band, decodes it and derives every
 * level's part of it on its own core; here the parts are pasted into tiles,
 * each finished tile goes back to a hand to be deflated, and its bytes are
 * appended to the file. Memory is the bands in flight, the tiles being pasted
 * (one tile row per level per channel) and the deflate backlog: while more
 * than `backlogBytes` wait, no band is read.
 *
 *   build({geometry, dtype, channels, factors, tile, description, hands, sink,
 *          progress, cancelled}) -> {bytes, tiles, timings}
 *   bandParts(reader, job) -> {parts, timing}: a hand's job
 *
 * `hands` is {count, block(i, job) -> {parts, timing}, deflate(bytes) -> bytes}
 * (workers/pyramidWorker.js: the browser node's tiff workers; a Node test: one
 * in-process hand). `sink.write(bytes, at)` writes at a file offset (an OPFS
 * sync access handle, a file descriptor).
 */
import { derive } from "./reduce.js";
import { DTYPES, HEADER_BYTES, directories, header } from "./tiffWriter.js";

const now = () => performance.now();
const align8 = (n) => n + ((8 - (n % 8)) % 8);

/** The dtype name of a source's samples (tiffReader's geometry), or null. */
export function dtypeOf({ bitsPerSample, sampleFormat }) {
    const found = Object.entries(DTYPES).find(([, t]) => t.bits === Number(bitsPerSample)
                                                  && t.format === Number(sampleFormat || 1));
    return found ? found[0] : null;
}

/**
 * A hand's job: one band of level 0 read through `reader` (node/tiffReader.js)
 * and every level's part of it, each part's own buffer (to transfer).
 */
export async function bandParts(reader, { channel, y0, y1, x0, x1, factors }) {
    const t0 = now();
    const { values, timing } = await reader.region({ channel, level: 0, x0, y0, x1, y1 });
    const height = y1 - y0;
    const width = x1 - x0;
    const own = values.byteOffset === 0 && values.buffer.byteLength === values.byteLength ? values : values.slice();
    const t1 = now();
    const parts = [{ level: 0, row: y0, col: x0, height, width, buffer: own.buffer }];
    for (const part of derive(own, height, width, factors)) {
        parts.push({ level: part.level, row: y0 / factors[part.level], col: x0 / factors[part.level],
                     height: part.height, width: part.width, buffer: part.array.buffer });
    }
    return { parts, timing: { readMs: t1 - t0, decodeMs: timing.decodeMs, deriveMs: now() - t1 } };
}

/** Every band of level 0 to read: [{channel, y0, y1, x0, x1}], channel-major. */
export function bands({ width, height, channels, factors, tile, bytesPer, tiled, jobBytes = 16 << 20 }) {
    const granule = factors[factors.length - 1];
    if (granule > tile) throw new Error(`a coarsest factor of ${granule} is wider than one tile`);
    const span = tiled ? tile * Math.max(1, Math.floor(jobBytes / (tile * tile * bytesPer))) : width;
    const rows = tiled ? tile
        : Math.min(tile, Math.max(granule, Math.floor(jobBytes / (width * bytesPer) / granule) * granule));
    const out = [];
    for (let channel = 0; channel < channels; channel += 1) {
        for (let y0 = 0; y0 < height; y0 += rows) {
            for (let x0 = 0; x0 < width; x0 += span) {
                out.push({ channel, y0, y1: Math.min(height, y0 + rows), x0, x1: Math.min(width, x0 + span) });
            }
        }
    }
    return out;
}

export async function build({ geometry, dtype: name, channels, factors, tile = 1024, description,
                              hands, sink, progress = () => {}, cancelled = () => false,
                              jobBytes = 16 << 20, backlogBytes = 128 << 20 }) {
    const t0 = now();
    const dtype = DTYPES[name];
    if (!dtype) throw new Error(`no TIFF sample type for ${name}`);
    const Kind = dtype.Array;
    const { width, height } = geometry;
    const levels = factors.map((f) => {
        const w = Math.ceil(width / f);
        const h = Math.ceil(height / f);
        return { width: w, height: h, across: Math.ceil(w / tile), down: Math.ceil(h / tile) };
    });
    const tiles = levels.map((lv) => Array.from({ length: channels }, () => new Array(lv.across * lv.down)));
    const total = channels * levels.reduce((sum, lv) => sum + lv.across * lv.down, 0);
    const timings = { pasteMs: 0, writeMs: 0, hand: {}, backlogPeak: 0 };
    const handTime = (t) => { for (const [k, v] of Object.entries(t || {})) timings.hand[k] = (timings.hand[k] || 0) + v; };

    // Not a TIFF until the very end: the header's IFD pointer is written last.
    await sink.write(new Uint8Array(HEADER_BYTES), 0);
    let offset = HEADER_BYTES;
    let written = 0;
    let failure = null;
    const write = async (level, channel, index, bytes) => {
        const s = now();
        const at = offset;
        offset += bytes.byteLength;
        await sink.write(bytes, at);
        tiles[level][channel][index] = [at, bytes.byteLength];
        written += 1;
        timings.writeMs += now() - s;
    };

    // Tiles being pasted together, `${channel}:${level}:${tx}:${ty}`: always a
    // whole tile, zero past the image's edge, as tifffile pads one.
    const open = new Map();
    const finishing = [];
    let backlog = 0;
    let waiters = [];
    const room = () => (backlog <= backlogBytes ? Promise.resolve() : new Promise((r) => waiters.push(r)));
    const finish = (level, channel, index, acc) => {
        const size = acc.array.byteLength;
        backlog += size;
        timings.backlogPeak = Math.max(timings.backlogPeak, backlog);
        finishing.push(hands.deflate(new Uint8Array(acc.array.buffer))
            .then((bytes) => write(level, channel, index, bytes))
            .catch((error) => { failure ||= error; })
            .finally(() => {
                backlog -= size;
                if (backlog <= backlogBytes && waiters.length) {
                    const ready = waiters;
                    waiters = [];
                    ready.forEach((r) => r());
                }
            }));
    };
    const paste = (channel, part) => {
        const s = now();
        const lv = levels[part.level];
        const src = new Kind(part.buffer);
        const r1 = part.row + part.height;
        const c1 = part.col + part.width;
        for (let ty = Math.floor(part.row / tile); ty * tile < r1; ty += 1) {
            for (let tx = Math.floor(part.col / tile); tx * tile < c1; tx += 1) {
                const id = `${channel}:${part.level}:${tx}:${ty}`;
                let acc = open.get(id);
                if (!acc) {
                    acc = { array: new Kind(tile * tile), w: Math.min(tile, lv.width - tx * tile),
                            h: Math.min(tile, lv.height - ty * tile), filled: 0 };
                    open.set(id, acc);
                }
                // The overlap of this part and this tile, row by row.
                const y0 = Math.max(part.row, ty * tile);
                const y1 = Math.min(r1, ty * tile + acc.h);
                const x0 = Math.max(part.col, tx * tile);
                const x1 = Math.min(c1, tx * tile + acc.w);
                for (let y = y0; y < y1; y += 1) {
                    const from = (y - part.row) * part.width + (x0 - part.col);
                    acc.array.set(src.subarray(from, from + (x1 - x0)), (y - ty * tile) * tile + (x0 - tx * tile));
                }
                acc.filled += (y1 - y0) * (x1 - x0);
                if (acc.filled === acc.w * acc.h) {
                    open.delete(id);
                    finish(part.level, channel, ty * lv.across + tx, acc);
                }
            }
        }
        timings.pasteMs += now() - s;
    };

    const jobs = bands({ width, height, channels, factors, tile, bytesPer: Kind.BYTES_PER_ELEMENT,
                         tiled: Boolean(geometry.fileTile && geometry.fileTile.tiled), jobBytes });
    let next = 0;
    let done = 0;
    let lastReport = 0;
    const report = () => {
        const t = now();
        if (t - lastReport < 200 && done < jobs.length) return;
        lastReport = t;
        progress({ tiles: written, total, bands: done, bandsTotal: jobs.length });
    };
    await Promise.all(Array.from({ length: hands.count }, async (_, i) => {
        while (next < jobs.length && !failure) {
            await room();
            if (next >= jobs.length || failure) break;
            if (cancelled()) { failure ||= new Error("cancelled"); break; }
            const job = jobs[next++];
            try {
                const answer = await hands.block(i, { ...job, factors });
                handTime(answer.timing);
                for (const part of answer.parts) paste(job.channel, part);
            } catch (error) {
                failure ||= error;
                break;
            }
            done += 1;
            report();
        }
    }));
    await Promise.all(finishing);
    if (failure) throw failure;
    if (open.size) throw new Error(`${open.size} tiles were never completed`);
    progress({ tiles: written, total, bands: done, bandsTotal: jobs.length });

    const start = align8(offset);
    const laid = directories({ start, width, height, factors, tile, channels, dtype: name, description, tiles });
    await sink.write(laid.bytes, start);
    await sink.write(header(laid.first), 0);
    timings.totalMs = now() - t0;
    timings.bands = jobs.length;
    return { bytes: start + laid.bytes.byteLength, tiles: total, timings };
}
