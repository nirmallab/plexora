/**
 * P2b / P7: a flat TIFF -- a label mask or a multichannel image -- made into
 * a pyramid in the browser, into OPFS, the pyramid Python builds:
 *
 *  - kind "labels": server/utils/segmentation_pyramid.py's FILLED mode,
 *    nearest neighbour (every id survives);
 *  - kind "image": server/utils/image_pyramid.py's pyramidize_image, a 2x2
 *    mean cascade rounded half to even;
 *
 * and levels until one tile covers the image (pyramid_factors).
 *
 * The work is spread over `workers` band workers (workers/pyramidBandWorker.js),
 * each opening the file itself: a job is one band of level 0 (a tile row and a
 * run of tiles for a tiled file; for a stripped file the full width and as many
 * rows as `jobBytes` holds, in steps of the coarsest factor), read, decoded and
 * reduced on its own core, every level's part of it sent back here. This worker
 * owns the OPFS files (sync access handles live only in a dedicated worker): it
 * pastes parts into tiles, hands each finished tile to a band worker to
 * deflate, and appends the bytes to its level's file. Memory is bounded by the
 * bands in flight (no more than ~1.5 GB of them), the tiles being pasted (one
 * tile row per level per channel) and the deflate backlog: while more than
 * `backlogBytes` wait, no band is read.
 *
 * Level 0 is copied only when the source cannot serve tiles itself (a
 * stripped file): a tiled flat file is already random-access, and the viewer
 * reads level 0 from it. A source that is already a pyramid is rebuilt from
 * its level 0.
 *
 * Output, under OPFS pyramids/<key>/: L<n>.bin per level (deflated tiles of
 * every channel, back to back, in the order they finished) and manifest.json
 * {kind, dtype, width, height, tile, channels, factors,
 *  levels: [{width, height, fromSource | file, tiles: [channel][ty * across + tx]
 *            = [offset, length, w, h]}]}.
 *
 * {op: "build", file, key, kind, tile = 1024, compress = true, workers,
 *  copyLevel0 = "auto", channels, jobBytes = 16 MB, backlogBytes = 128 MB}
 *  -> progress messages, then {done, manifest, timings}.
 */
import { createTiffReader } from "../lib/tiffReader.js";
import { factorsFor } from "../lib/reduce.js";

const now = () => performance.now();

const KINDS = {
    "8:1": Uint8Array, "16:1": Uint16Array, "32:1": Uint32Array,
    "8:2": Int8Array, "16:2": Int16Array, "32:2": Int32Array,
    "32:3": Float32Array, "64:3": Float64Array,
};

/** A pool of band workers, each told the build once. */
function startHands(count, openMessage) {
    const url = new URL("pyramidBandWorker.js", self.location.href).href;
    const hands = [];
    const pending = new Map();
    let nextId = 1;
    for (let i = 0; i < count; i += 1) {
        const worker = new Worker(url);
        const hand = { worker, blocks: 0, deflates: 0 };
        worker.onmessage = (event) => {
            const msg = event.data;
            const id = msg.opened ? `open${i}` : msg.id;
            const entry = pending.get(id);
            if (!entry) return;
            pending.delete(id);
            if (msg.error) entry.reject(new Error(msg.error));
            else entry.resolve(msg);
        };
        hands.push(hand);
    }
    const call = (hand, key, message, transfer = []) => new Promise((resolve, reject) => {
        pending.set(key, { resolve, reject });
        hand.worker.postMessage(message, transfer);
    });
    const opened = Promise.all(hands.map((hand, i) => call(hand, `open${i}`, openMessage)));
    return {
        hands,
        opened,
        block(hand, job) {
            const id = nextId++;
            hand.blocks += 1;
            return call(hand, id, { op: "block", id, ...job }).finally(() => { hand.blocks -= 1; });
        },
        deflate(buffer) {
            // The hand with the fewest deflates waiting.
            const hand = hands.reduce((a, b) => (b.deflates < a.deflates ? b : a));
            const id = nextId++;
            hand.deflates += 1;
            return call(hand, id, { op: "deflate", id, buffer }, [buffer]).finally(() => { hand.deflates -= 1; });
        },
        stop() { for (const hand of hands) hand.worker.terminate(); },
    };
}

self.onmessage = async (event) => {
    const { op, file, key, kind = "labels", tile = 1024, compress = true, copyLevel0 = "auto",
            workers = Math.max(1, Math.min(12, (navigator.hardwareConcurrency || 4) - 2)),
            jobBytes = 16 * 1024 * 1024, backlogBytes = 128 * 1024 * 1024, channels: only = null } = event.data;
    if (op !== "build") return;
    let pool = null;
    try {
        const t0 = now();
        const reader = createTiffReader();
        const { geometry } = await reader.open({ file, decoder: "native" });
        const { width, height } = geometry;
        const Kind = KINDS[`${geometry.bitsPerSample}:${geometry.sampleFormat || 1}`];
        if (!Kind) throw new Error(`${geometry.bitsPerSample}-bit samples of format ${geometry.sampleFormat}`);
        if (kind === "labels" && Kind.BYTES_PER_ELEMENT !== 4) throw new Error(`a label mask of ${geometry.bitsPerSample}-bit ids`);
        const factors = factorsFor(height, width, tile);
        if (factors[factors.length - 1] > tile) throw new Error("more levels than one tile can hold");
        const channels = only || Array.from({ length: geometry.numChannels }, (_, c) => c);
        const copy0 = copyLevel0 === "auto" ? !geometry.fileTile.tiled : Boolean(copyLevel0);
        const bytesPer = Kind.BYTES_PER_ELEMENT;

        const root = await navigator.storage.getDirectory();
        const top = await root.getDirectoryHandle("pyramids", { create: true });
        try { await top.removeEntry(key, { recursive: true }); } catch (e) { /* none */ }
        const dir = await top.getDirectoryHandle(key, { create: true });

        // Every level's geometry and output file.
        const levels = [];
        for (let L = 0; L < factors.length; L += 1) {
            const f = factors[L];
            const w = Math.ceil(width / f);
            const h = Math.ceil(height / f);
            const lv = { L, f, width: w, height: h, across: Math.ceil(w / tile),
                         fromSource: L === 0 && !copy0, handle: null, offset: 0,
                         tiles: channels.map(() => []) };
            if (!lv.fromSource) {
                lv.handle = await (await dir.getFileHandle(`L${L}.bin`, { create: true })).createSyncAccessHandle();
                lv.handle.truncate(0);
            }
            levels.push(lv);
        }
        const timings = { assembleMs: 0, writeMs: 0, jobs: 0, deflates: 0, hand: {} };
        const handTime = (t) => { for (const [k, v] of Object.entries(t)) timings.hand[k] = (timings.hand[k] || 0) + v; };
        let bytesOut = 0;

        const write = (lv, ci, tx, ty, w, h, bytes) => {
            const s = now();
            lv.handle.write(bytes, { at: lv.offset });
            lv.tiles[ci][ty * lv.across + tx] = [lv.offset, bytes.byteLength, w, h];
            lv.offset += bytes.byteLength;
            bytesOut += bytes.byteLength;
            timings.writeMs += now() - s;
        };

        // Tiles being pasted together: `${ci}:${L}:${tx}:${ty}`. A finished
        // tile waits for a hand to deflate it; while too many bytes wait, no
        // new band is read (a fast reader would otherwise pile the image up).
        const open = new Map();
        const finishing = [];
        const backlogCap = backlogBytes;
        let backlog = 0;
        let waiters = [];
        const room = () => (backlog <= backlogCap ? Promise.resolve() : new Promise((r) => waiters.push(r)));
        const finish = (lv, ci, tx, ty, acc) => {
            if (!compress) { write(lv, ci, tx, ty, acc.w, acc.h, new Uint8Array(acc.array.buffer)); return; }
            const size = acc.array.byteLength;
            backlog += size;
            timings.backlogPeak = Math.max(timings.backlogPeak || 0, backlog);
            finishing.push(pool.deflate(acc.array.buffer).then((r) => {
                timings.deflates += 1;
                handTime({ deflateMs: r.ms });
                write(lv, ci, tx, ty, acc.w, acc.h, r.bytes);
                backlog -= size;
                if (backlog <= backlogCap && waiters.length) { const ready = waiters; waiters = []; ready.forEach((r) => r()); }
            }));
        };
        const paste = (ci, part) => {
            const s = now();
            const lv = levels[part.level];
            const src = new Kind(part.buffer);
            const r1 = part.row + part.height;
            const c1 = part.col + part.width;
            for (let ty = Math.floor(part.row / tile); ty * tile < r1; ty += 1) {
                for (let tx = Math.floor(part.col / tile); tx * tile < c1; tx += 1) {
                    const id = `${ci}:${part.level}:${tx}:${ty}`;
                    let acc = open.get(id);
                    if (!acc) {
                        const w = Math.min(tile, lv.width - tx * tile);
                        const h = Math.min(tile, lv.height - ty * tile);
                        acc = { array: new Kind(w * h), w, h, filled: 0 };
                        open.set(id, acc);
                    }
                    // The overlap of this part and this tile, row by row.
                    const y0 = Math.max(part.row, ty * tile), y1 = Math.min(r1, ty * tile + acc.h);
                    const x0 = Math.max(part.col, tx * tile), x1 = Math.min(c1, tx * tile + acc.w);
                    for (let y = y0; y < y1; y += 1) {
                        const from = (y - part.row) * part.width + (x0 - part.col);
                        acc.array.set(src.subarray(from, from + (x1 - x0)), (y - ty * tile) * acc.w + (x0 - tx * tile));
                    }
                    acc.filled += (y1 - y0) * (x1 - x0);
                    if (acc.filled === acc.w * acc.h) {
                        open.delete(id);
                        finish(lv, ci, tx, ty, acc);
                    }
                }
            }
            timings.assembleMs += now() - s;
        };

        // Jobs: per channel, band by band. A tiled file: one tile row high, a
        // run of tiles across. A stripped file: the whole width (a strip spans
        // every column), so fewer rows -- as many as `jobBytes` holds, in steps
        // of the coarsest factor so every level's part of a band is whole.
        const tiled = geometry.fileTile.tiled;
        const granule = factors[factors.length - 1];
        const span = tiled ? tile * Math.max(1, Math.floor(jobBytes / (tile * tile * bytesPer))) : width;
        const rows = tiled ? tile
            : Math.min(tile, Math.max(granule, Math.floor(jobBytes / (width * bytesPer) / granule) * granule));
        const jobs = [];
        channels.forEach((channel, ci) => {
            for (let y0 = 0; y0 < height; y0 += rows) {
                for (let x0 = 0; x0 < width; x0 += span) {
                    jobs.push({ ci, channel, y0, y1: Math.min(height, y0 + rows), x0, x1: Math.min(width, x0 + span) });
                }
            }
        });
        // No more bands in flight than ~1.5 GB holds, counting each band, its
        // decode buffers and its parts.
        const bandBytes = rows * span * bytesPer;
        const hands = Math.max(1, Math.min(workers, jobs.length, Math.floor((1536 * 1024 * 1024) / (4 * bandBytes)) || 1));
        timings.bandRows = rows;
        timings.bandColumns = span;
        pool = startHands(hands, { op: "open", file, kind, factors, tile, copyLevel0: copy0, compress });
        await pool.opened;
        const tOpened = now();

        let done = 0;
        let next = 0;
        // Each hand takes the next band as soon as it is free and there is room.
        await Promise.all(pool.hands.map(async (hand) => {
            while (next < jobs.length) {
                await room();
                if (next >= jobs.length) break;
                const job = jobs[next++];
                const r = await pool.block(hand, { channel: job.channel, y0: job.y0, y1: job.y1, x0: job.x0, x1: job.x1 });
                handTime(r.timing);
                for (const part of r.parts) paste(job.ci, part);
                done += 1;
                timings.jobs += 1;
                if (done % 8 === 0 || done === jobs.length) self.postMessage({ progress: done / jobs.length });
            }
        }));
        const tBlocks = now();
        await Promise.all(finishing);
        if (open.size) throw new Error(`${open.size} tiles never completed`);
        const tDeflated = now();

        const manifest = {
            version: 2, kind, dtype: Kind.name.replace("Array", "").toLowerCase(), bytesPerSample: bytesPer,
            source: file.name, sourceBytes: file.size, width, height, tile, channels,
            names: geometry.names, compress: compress ? "deflate" : null, factors,
            levels: levels.map((lv) => (lv.fromSource
                ? { width: lv.width, height: lv.height, fromSource: true }
                : { width: lv.width, height: lv.height, file: `L${lv.L}.bin`, tiles: lv.tiles })),
        };
        for (const lv of levels) if (lv.handle) { lv.handle.flush(); lv.handle.close(); }
        const m = await (await dir.getFileHandle("manifest.json", { create: true })).createSyncAccessHandle();
        const text = new TextEncoder().encode(JSON.stringify(manifest));
        m.truncate(0);
        m.write(text, { at: 0 });
        m.flush();
        m.close();
        timings.totalMs = now() - t0;
        timings.openMs = tOpened - t0;
        timings.blocksMs = tBlocks - tOpened;
        timings.drainMs = tDeflated - tBlocks;
        timings.hands = hands;
        timings.bytesOut = bytesOut;
        timings.copiedLevel0 = copy0;
        // A source that is already a pyramid is rebuilt from its level 0 (a test can ask for that).
        timings.sourceLayout = geometry.layout;
        timings.rawDerivedBytes = levels.reduce((a, lv) => a + (lv.fromSource ? 0 : lv.width * lv.height * bytesPer * channels.length), 0);
        self.postMessage({
            done: true, timings,
            manifest: { ...manifest, levels: manifest.levels.map(({ tiles, ...rest }) => ({ ...rest, tileCount: tiles ? tiles[0].length : 0 })) },
        });
    } catch (error) {
        self.postMessage({ error: `${error}${error?.stack ? ` | ${error.stack}` : ""}` });
    } finally {
        pool?.stop();
    }
};
