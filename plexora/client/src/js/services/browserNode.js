/**
 * PlexoraNode -- the website's browser node: a project's image tiles read by
 * this page from the file the user picked, not by Plexora's Python.
 *
 * Python in the page answers every request one at a time, and a screenful of
 * tiles is dozens of them; the browser-native proof measured the page reading
 * the file itself, in workers, at about a third of the desktop's time per step
 * (docs/internal/bench/browser_node/P6.md). So the tile route is answered here
 * where it safely can be:
 *
 * - Python says, per project, what it reads (plexora/browser/node_spec.py: the
 *   file, every level's shape, the channels, the tile grid), or why the page
 *   must not answer;
 * - the page opens the same file in a pool of workers (workers/tiffWorker.js,
 *   node/tiffReader.js) and checks every level's shape and the channel count
 *   against Python's before it answers a single tile;
 * - a tile is the window `data_model.read_tile` reads, as the 16-bit grey PNG
 *   `encode_tile_array` makes for the HD path (stored, not deflated: the same
 *   pixels, fewer cycles). Anything else -- the 8-bit preview, a volume's view,
 *   a label mask, a tile past the edge -- returns null, and Python answers it.
 *
 *   answer(request) -> a promise of a Response, or of null (Python's to answer)
 *   project(name)   -> a promise of {spec, geometry, pool} once checked, or of
 *                      {spec?, refused} saying why Python answers instead
 *   revalidate()    -> check each project with Python again before its next
 *                      tile (services/shell.js, on every page of the app)
 *   forget(name)    -> drop what is held for a project (it changed)
 *
 * Classic script, `window.PlexoraNode`; needs PlexoraPython and PlexoraPicks.
 */
(function (global) {
    "use strict";

    const WORKER = "/client/dist/tiffWorker_bundle.js";
    const WORKERS = Math.min(6, Math.max(2, Math.floor((global.navigator.hardwareConcurrency || 4) / 2)));
    const TILE = /^\/generated\/data\/([^/]+)\/([^/]+)\/(\d+)\/(\d+)_(\d+)\.png$/;

    // -- the PNG ----------------------------------------------------------------

    const CRC = (() => {
        const table = new Uint32Array(256);
        for (let n = 0; n < 256; n += 1) {
            let c = n;
            for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
            table[n] = c >>> 0;
        }
        return table;
    })();

    function crc32(bytes, start, end) {
        let c = 0xffffffff;
        for (let i = start; i < end; i += 1) c = CRC[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
        return (c ^ 0xffffffff) >>> 0;
    }

    /** PNG bytes for `rows` (each row's samples, filter byte 0 added here),
     * `colourType` 0 (grey) or 2 (RGB) or 6 (RGBA), deflate stored. */
    function png(width, height, bitDepth, colourType, samples) {
        const rowBytes = samples.length / height;
        const raw = height * (rowBytes + 1);
        const blocks = Math.max(1, Math.ceil(raw / 65535));
        const zlib = 2 + raw + blocks * 5 + 4;
        const out = new Uint8Array(8 + 25 + 12 + zlib + 12);
        const view = new DataView(out.buffer);
        out.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], 0);
        let at = 8;
        const chunk = (type, length, fill) => {
            view.setUint32(at, length);
            for (let i = 0; i < 4; i += 1) out[at + 4 + i] = type.charCodeAt(i);
            fill(at + 8);
            view.setUint32(at + 8 + length, crc32(out, at + 4, at + 8 + length));
            at += 12 + length;
        };
        chunk("IHDR", 13, (p) => {
            view.setUint32(p, width);
            view.setUint32(p + 4, height);
            out[p + 8] = bitDepth;
            out[p + 9] = colourType;
            out[p + 10] = 0;
            out[p + 11] = 0;
            out[p + 12] = 0;
        });
        chunk("IDAT", zlib, (p) => {
            // The scanlines, each after its filter byte (0: none).
            const lines = new Uint8Array(raw);
            for (let r = 0; r < height; r += 1) {
                lines.set(samples.subarray(r * rowBytes, (r + 1) * rowBytes), r * (rowBytes + 1) + 1);
            }
            out[p] = 0x78;
            out[p + 1] = 0x01;
            let w = p + 2;
            for (let from = 0; from < raw; from += 65535) {
                const size = Math.min(65535, raw - from);
                out[w] = from + size >= raw ? 1 : 0;
                view.setUint16(w + 1, size, true);
                view.setUint16(w + 3, ~size & 0xffff, true);
                out.set(lines.subarray(from, from + size), w + 5);
                w += 5 + size;
            }
            // Adler-32, reduced every 5552 bytes as zlib does (no overflow).
            let a = 1;
            let b = 0;
            for (let i = 0; i < raw;) {
                const end = Math.min(i + 5552, raw);
                for (; i < end; i += 1) {
                    a += lines[i];
                    b += a;
                }
                a %= 65521;
                b %= 65521;
            }
            view.setUint32(w, ((b << 16) | a) >>> 0);
        });
        chunk("IEND", 0, () => {});
        return out;
    }

    /** 8-bit samples as the 16-bit big-endian ones Python's HD tile sends
     * (`tile.astype("uint16")`). */
    function widen(values) {
        const out = new Uint8Array(values.length * 2);
        for (let i = 0; i < values.length; i += 1) out[i * 2 + 1] = values[i];
        return out;
    }

    // -- the readers ------------------------------------------------------------

    class TiffPool {
        constructor(size) {
            this.workers = [];
            this.pending = new Map();
            this.nextId = 1;
            for (let i = 0; i < size; i += 1) {
                const worker = new Worker(WORKER);
                worker.inflight = 0;
                worker.onmessage = ({ data }) => {
                    const entry = this.pending.get(data.id);
                    if (!entry) return;
                    this.pending.delete(data.id);
                    worker.inflight -= 1;
                    if (data.ok) entry.resolve(data.result);
                    else entry.reject(new Error(data.error));
                };
                this.workers.push(worker);
            }
        }

        call(worker, message) {
            const id = this.nextId++;
            worker.inflight += 1;
            return new Promise((resolve, reject) => {
                this.pending.set(id, { resolve, reject });
                worker.postMessage({ id, ...message });
            });
        }

        async open(file) {
            const results = await Promise.all(this.workers.map((w) => this.call(w, { op: "open", file })));
            return results[0].geometry;
        }

        least() {
            return this.workers.reduce((best, w) => (w.inflight < best.inflight ? w : best));
        }

        tile(channel, level, tx, ty, tw, th) {
            return this.call(this.least(), { op: "tile", channel, level, tx, ty, tw, th });
        }

        close() {
            for (const w of this.workers) w.terminate();
            this.workers = [];
        }
    }

    // -- the projects -----------------------------------------------------------

    //: What is open per project (resolved {spec, file, geometry, pool} or
    //: {refused}), and the checks made since the app last changed page.
    const held = new Map();
    const checks = new Map();

    //: Compressions the page decodes to Python's very samples (none, LZW,
    //: deflate both codes, PackBits, Zstandard). A lossy one (JPEG, JPEG
    //: 2000, WebP, ...) is decoded by another library than Python's, which
    //: rounds differently: those tiles stay Python's.
    const EXACT = new Set([1, 5, 8, 32946, 32773, 50000]);

    function matches(spec, geometry) {
        if (!EXACT.has(geometry.compression)) return `compression ${geometry.compression}`;
        if (geometry.numChannels !== spec.levels[0][0]) return "a different channel count";
        if (geometry.levels.length < spec.levels.length) return "fewer levels";
        for (let i = 0; i < spec.levels.length; i += 1) {
            const [, height, width] = spec.levels[i];
            if (geometry.levels[i].width !== width || geometry.levels[i].height !== height) {
                return `level ${i} is ${geometry.levels[i].width}x${geometry.levels[i].height}`;
            }
        }
        const bits = spec.dtype === "uint16" ? 16 : 8;
        if (geometry.bitsPerSample !== bits) return `${geometry.bitsPerSample}-bit samples`;
        return null;
    }

    async function check(name) {
        const spec = await global.PlexoraPython.nodeSpec(name);
        const before = held.get(name);
        const file = spec.served && global.PlexoraPicks ? global.PlexoraPicks.fileFor(spec.path) : null;
        // The same load of the same file: what is open stays open.
        if (before && before.pool && before.file === file
            && JSON.stringify(before.spec) === JSON.stringify(spec)) {
            return before;
        }
        if (before && before.pool) before.pool.close();
        held.delete(name);
        let entry;
        if (!spec.served) entry = { spec, refused: spec.reason };
        else if (!file) entry = { spec, refused: "the page does not hold that file now" };
        else {
            const pool = new TiffPool(WORKERS);
            const geometry = await pool.open(file);
            const differs = matches(spec, geometry);
            if (differs) {
                pool.close();
                entry = { spec, geometry, refused: `the page reads it differently: ${differs}` };
            } else {
                entry = { spec, file, geometry, pool };
            }
        }
        held.set(name, entry);
        return entry;
    }

    function project(name) {
        if (!checks.has(name)) {
            checks.set(name, check(name).catch((error) => ({ refused: String(error && error.message || error) })));
        }
        return checks.get(name);
    }

    //: Ask Python again, once per project, before the next tile: the app is
    //: on a new page, and the project may have been reloaded or replaced.
    function revalidate() {
        checks.clear();
    }

    function forget(name) {
        checks.delete(name);
        const entry = held.get(name);
        held.delete(name);
        if (entry && entry.pool) entry.pool.close();
    }

    async function answer(request) {
        const url = new URL(request.url);
        const match = url.pathname.match(TILE);
        if (!match || request.method !== "GET") return null;
        if (url.searchParams.get("q") !== "hd" || url.searchParams.get("view")) return null;
        const [, name, channel, level, tx, ty] = match.map((part) => decodeURIComponent(part));
        const index = (/_(\d+)$/.exec(channel) || [])[1];
        if (index === undefined) return null; // a label mask's tile
        const entry = await project(name);
        if (!entry || !entry.pool) return null;
        const channelNum = Number(index);
        const levelNum = Number(level);
        if (channelNum >= entry.spec.levels[0][0] || levelNum >= entry.spec.levels.length) return null;
        const [tw, th] = entry.spec.tile;
        const tile = await entry.pool.tile(channelNum, levelNum, Number(tx), Number(ty), tw, th);
        if (!tile.width || !tile.height) return null;
        const samples = tile.format === "u16" ? tile.array : widen(tile.array);
        return new Response(png(tile.width, tile.height, 16, 0, samples), {
            status: 200,
            headers: { "Content-Type": "image/png", "Cache-Control": "private, max-age=31536000",
                       "X-Plexora-Served-By": "browser" },
        });
    }

    global.PlexoraNode = Object.freeze({ answer, project, revalidate, forget, png });
})(window);
