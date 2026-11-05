/**
 * P2b: a flat label mask turned into a label pyramid in the browser, the way
 * server/utils/segmentation_pyramid.py's FILLED mode builds one: level L's
 * pixel (y, x) is level 0's pixel (y * 2^L, x * 2^L) -- nearest-neighbour, so
 * every id survives exactly -- and levels until one tile covers the image
 * (pyramid_factors).
 *
 * Streamed: level 0 is read one band of tile rows at a time, every coarser
 * level takes its sampled rows from that band, and a level's tiles are cut,
 * deflated and appended to that level's OPFS file the moment it has a full
 * tile row. Peak memory is one band plus one tile row per level, whatever the
 * mask's size. Level 0 is NOT copied: the viewer reads it from the user's own
 * file (a tiled TIFF is already random-access); only derived levels are kept.
 *
 * Output, under OPFS labels/<key>/: L<n>.bin (deflated tiles back to back),
 * manifest.json {levels: [{width, height, tiles: [[offset, length], ...]}]}.
 *
 * {op: "build", file, key, tile = 1024, deflate = true} -> progress messages,
 * then {done, manifest, timings}.
 */
import { createTiffReader } from "../lib/tiffReader.js";

const now = () => performance.now();

/** pyramid_factors: 1, 2, 4, ... until one tile covers the level. */
function factorsFor(height, width, tile) {
    const factors = [1];
    for (;;) {
        const f = factors[factors.length - 1];
        if (Math.max(Math.ceil(height / f), Math.ceil(width / f)) <= tile) return factors;
        factors.push(f * 2);
    }
}

async function deflate(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
}

self.onmessage = async (event) => {
    const { op, file, key, tile = 1024, compress = true } = event.data;
    if (op !== "build") return;
    try {
        const t0 = now();
        const reader = createTiffReader();
        const { geometry } = await reader.open({ file, decoder: "native" });
        if (geometry.bitsPerSample !== 32) throw new Error(`a label mask of ${geometry.bitsPerSample}-bit ids`);
        const { width, height } = geometry;
        const factors = factorsFor(height, width, tile);
        const root = await navigator.storage.getDirectory();
        const labels = await root.getDirectoryHandle("labels", { create: true });
        try { await labels.removeEntry(key, { recursive: true }); } catch (e) { /* none */ }
        const dir = await labels.getDirectoryHandle(key, { create: true });

        // One output per derived level: its row buffer and its file.
        const levels = [];
        for (let L = 1; L < factors.length; L += 1) {
            const f = factors[L];
            const w = Math.ceil(width / f);
            const h = Math.ceil(height / f);
            const handle = await (await dir.getFileHandle(`L${L}.bin`, { create: true })).createSyncAccessHandle();
            handle.truncate(0);
            levels.push({ L, f, width: w, height: h, rows: new Uint32Array(tile * w), filled: 0, nextRow: 0,
                          handle, offset: 0, tiles: [] });
        }
        const timings = { readMs: 0, sampleMs: 0, deflateMs: 0, writeMs: 0, bands: 0 };
        let bytesOut = 0;

        const flush = async (lv) => {
            // Cut lv.filled rows into tiles, left to right, and append them.
            const rows = lv.filled;
            for (let x0 = 0; x0 < lv.width; x0 += tile) {
                const tw = Math.min(tile, lv.width - x0);
                const out = new Uint32Array(tw * rows);
                for (let r = 0; r < rows; r += 1) out.set(lv.rows.subarray(r * lv.width + x0, r * lv.width + x0 + tw), r * tw);
                let bytes = new Uint8Array(out.buffer);
                let s = now();
                if (compress) bytes = await deflate(bytes);
                timings.deflateMs += now() - s;
                s = now();
                lv.handle.write(bytes, { at: lv.offset });
                timings.writeMs += now() - s;
                lv.tiles.push([lv.offset, bytes.byteLength, tw, rows]);
                lv.offset += bytes.byteLength;
                bytesOut += bytes.byteLength;
            }
            lv.filled = 0;
        };

        const bandRows = tile;
        // One band of level 0 (a tile row, full width), stitched into one
        // buffer so every level samples it with a plain strided read.
        const band = new Uint32Array(bandRows * width);
        for (let y0 = 0; y0 < height; y0 += bandRows) {
            const bh = Math.min(bandRows, height - y0);
            let s = now();
            const pieces = await Promise.all(Array.from({ length: Math.ceil(width / tile) }, (_, tx) =>
                reader.tile({ channel: 0, level: 0, tx, ty: y0 / tile, tw: tile, th: tile, raw: true })));
            pieces.forEach((t, tx) => {
                const values = new Uint32Array(t.array.buffer, t.array.byteOffset, t.array.byteLength / 4);
                for (let r = 0; r < t.height; r += 1) {
                    band.set(values.subarray(r * t.width, (r + 1) * t.width), r * width + tx * tile);
                }
            });
            timings.readMs += now() - s;
            s = now();
            for (const lv of levels) {
                // Rows y of level 0 with y % f == 0 land in this level, in order.
                for (let y = Math.ceil(y0 / lv.f) * lv.f; y < y0 + bh; y += lv.f) {
                    const row = lv.rows.subarray(lv.filled * lv.width, (lv.filled + 1) * lv.width);
                    const base = (y - y0) * width;
                    for (let x = 0, i = 0; x < width; x += lv.f, i += 1) row[i] = band[base + x];
                    lv.filled += 1;
                    if (lv.filled === tile) {
                        timings.sampleMs += now() - s;
                        await flush(lv);
                        s = now();
                    }
                }
            }
            timings.sampleMs += now() - s;
            timings.bands += 1;
            self.postMessage({ progress: Math.min(1, (y0 + bh) / height) });
        }
        for (const lv of levels) if (lv.filled) await flush(lv);
        const manifest = {
            version: 1, source: file.name, sourceBytes: file.size, width, height, tile, compress: compress ? "deflate" : null,
            factors, levels: [{ width, height, fromSource: true },
                              ...levels.map((lv) => ({ width: lv.width, height: lv.height, file: `L${lv.L}.bin`, tiles: lv.tiles }))],
        };
        for (const lv of levels) { lv.handle.flush(); lv.handle.close(); }
        const m = await (await dir.getFileHandle("manifest.json", { create: true })).createSyncAccessHandle();
        const text = new TextEncoder().encode(JSON.stringify(manifest));
        m.truncate(0);
        m.write(text, { at: 0 });
        m.flush();
        m.close();
        timings.totalMs = now() - t0;
        timings.bytesOut = bytesOut;
        timings.rawDerivedBytes = levels.reduce((a, lv) => a + lv.width * lv.height * 4, 0);
        self.postMessage({ done: true, manifest: { ...manifest, levels: manifest.levels.map(({ tiles, ...rest }) => ({ ...rest, tileCount: tiles?.length })) }, timings });
    } catch (error) {
        self.postMessage({ error: String(error?.stack || error) });
    }
};
