/**
 * PlexoraNode -- the website's browser node: a project's image and mask tiles
 * read by this page from the files the user picked, not by Plexora's Python.
 *
 * Python in the page answers every request one at a time, and a screenful of
 * tiles is dozens of them; the browser-native proof measured the page reading
 * the file itself, in workers, at about a third of the desktop's time per step
 * (docs/internal/bench/browser_node/P6.md). So the tile route is answered here
 * where it safely can be:
 *
 * - Python says, per project, what it reads (plexora/browser/node_spec.py: the
 *   image's and the mask's files, every level's shape, the channels, the tile
 *   grid), or why the page must not answer;
 * - the page opens the same files in pools of workers (workers/tiffWorker.js,
 *   node/tiffReader.js) and checks every level's shape, the channel count, the
 *   sample size and the compression against Python's before it answers a
 *   single tile;
 * - a tile is the window `data_model.read_tile` reads, as `encode_tile_array`
 *   makes it (stored, not deflated: the same pixels, fewer cycles): a channel's
 *   HD tile the 16-bit grey PNG, a mask's the RGBA PNG of its ids. Anything
 *   else -- the 8-bit preview, a volume's view, a mask level Python draws from
 *   a finer one, a tile past the edge -- returns null, and Python answers it.
 *
 *   answer(request) -> a promise of a Response, or of null (Python's to answer)
 *   decoded(url, {raster}) -> the same tile as the viewer's tile scheduler
 *                      takes it (views/tileFetch.js `local`): a channel's
 *                      {array, width, height, format: "u16"} -- the pixels
 *                      the HD PNG decodes to, with no PNG between -- or, for
 *                      a mask's raster tile, {raster: PNG bytes, status}; or
 *                      null (Python's to answer)
 *   project(name)   -> a promise of {spec, image, labels}, each part {file,
 *                      geometry, pool} once checked or {refused} saying why
 *                      Python answers instead
 *   revalidate()    -> check each project with Python again before its next
 *                      tile (services/shell.js, on every page of the app)
 *   forget(name)    -> drop what is held for a project (it changed)
 *   use({spec, files}) -> where specs and files come from (`source` below):
 *                      the desktop app's page sets its own
 *   windowOf({path, channel, shape, dtype}) -> {value}: a channel's largest
 *                      full-resolution sample, for Python ("window" question)
 *
 * The desktop app reads the same way (plan Phase 6, Settings > Advanced >
 * Browser rendering): services/resourceRouting.js's `local-browser` routing
 * loads this file into the app's window and `use`s the desktop's source --
 * the spec from `/resource_routing` (Python's own answer, as on the website)
 * and the files granted by the shell's `plexora-file` scheme, read by byte
 * range -- and the tile scheduler asks `decoded` before the server.
 *
 * Classic script, `window.PlexoraNode`; on the website it needs PlexoraPython
 * and PlexoraPicks.
 */
(function (global) {
    "use strict";

    // Under the site's base path (services/shell.js `BASE`; "" at its root).
    const WORKER = (globalThis.PLEXORA_SITE_BASE || "") + "/client/dist/tiffWorker_bundle.js";
    const WORKERS = Math.min(6, Math.max(2, Math.floor((global.navigator.hardwareConcurrency || 4) / 2)));
    //: Workers for a whole-plane scan (`windowOf`), which nothing else waits beside.
    const SCAN_WORKERS = Math.min(16, Math.max(2, (global.navigator.hardwareConcurrency || 4) - 2));
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

        /** Open `found` in every worker: `{file}` (a File the user picked) or
         *  `{url, size}` (a file read by byte range: the desktop's
         *  `plexora-file` scheme). */
        async open(found) {
            const results = await Promise.all(this.workers.map((w) => this.call(w, { op: "open", ...found })));
            return results[0].geometry;
        }

        least() {
            return this.workers.reduce((best, w) => (w.inflight < best.inflight ? w : best));
        }

        tile(channel, level, tx, ty, tw, th, raw = false) {
            return this.call(this.least(), { op: "tile", channel, level, tx, ty, tw, th, raw });
        }

        /** The largest sample of a channel's level, its plane cut into
         *  windows of whole file tiles (bands of a stripped file) shared out
         *  among the workers. */
        async max(channel, level, { width, height }, fileTile) {
            const windows = [];
            if (fileTile && fileTile.tiled) {
                const step = (dim) => dim * Math.max(1, Math.round(4096 / dim));
                const sx = step(fileTile.width);
                const sy = step(fileTile.height);
                for (let y = 0; y < height; y += sy) {
                    for (let x = 0; x < width; x += sx) {
                        windows.push([x, y, Math.min(width, x + sx), Math.min(height, y + sy)]);
                    }
                }
            } else {
                const rows = Math.max(1, Math.floor((16 << 20) / Math.max(1, width)));
                for (let y = 0; y < height; y += rows) windows.push([0, y, width, Math.min(height, y + rows)]);
            }
            const shares = this.workers.map(() => []);
            windows.forEach((window, i) => shares[i % shares.length].push(window));
            const found = await Promise.all(shares.map((share, i) => (share.length
                ? this.call(this.workers[i], { op: "max", channel, level, windows: share })
                : { max: -Infinity })));
            return found.reduce((top, part) => Math.max(top, part.max), -Infinity);
        }

        close() {
            for (const w of this.workers) w.terminate();
            this.workers = [];
        }
    }

    // -- the projects -----------------------------------------------------------

    //: What is open per project -- {spec, image, labels}, each part
    //: {file, geometry, pool} or {refused} -- and the checks made since the
    //: app last changed page.
    const held = new Map();
    const checks = new Map();

    //: Compressions the page decodes to Python's very samples (none, LZW,
    //: deflate both codes, PackBits, Zstandard). A lossy one (JPEG, JPEG
    //: 2000, WebP, ...) is decoded by another library than Python's, which
    //: rounds differently: those tiles stay Python's.
    const EXACT = new Set([1, 5, 8, 32946, 32773, 50000]);

    const BITS = { uint8: 8, uint16: 16, uint32: 32, int32: 32 };
    //: How often a mask still being prepared is asked about again.
    const RECHECK_MS = 3000;

    function levelDiffers(geometry, level, height, width) {
        const found = geometry.levels[level];
        if (!found) return `no level ${level}`;
        if (found.width !== width || found.height !== height) {
            return `level ${level} is ${found.width}x${found.height}`;
        }
        return null;
    }

    function imageDiffers(spec, geometry) {
        if (!EXACT.has(geometry.compression)) return `compression ${geometry.compression}`;
        if (geometry.numChannels !== spec.levels[0][0]) return "a different channel count";
        for (let i = 0; i < spec.levels.length; i += 1) {
            const [, height, width] = spec.levels[i];
            const differs = levelDiffers(geometry, i, height, width);
            if (differs) return differs;
        }
        if (geometry.bitsPerSample !== BITS[spec.dtype]) return `${geometry.bitsPerSample}-bit samples`;
        return null;
    }

    function labelsDiffer(labels, geometry) {
        if (!EXACT.has(geometry.compression)) return `compression ${geometry.compression}`;
        if (geometry.numChannels !== 1) return `${geometry.numChannels} planes`;
        for (const [level, height, width] of labels.levels) {
            const differs = levelDiffers(geometry, level, height, width);
            if (differs) return differs;
        }
        if (geometry.bitsPerSample !== BITS[labels.dtype]) return `${geometry.bitsPerSample}-bit labels`;
        if ((geometry.sampleFormat === 2) !== labels.dtype.startsWith("int")) return "signed and unsigned";
        return null;
    }

    //: Where a project's spec and files come from. The website's: Python in
    //: the page's worker says what it reads (plexora/browser/node_spec.py),
    //: and a file is the user's pick of that path. `use` replaces it -- the
    //: desktop app's page reads the spec from `/resource_routing` and the
    //: files through the shell's `plexora-file` scheme (services/
    //: resourceRouting.js `attachLocal`).
    //:   spec(name)        -> a promise of the spec
    //:   files(name, spec) -> a promise of `(path) => found | null`, `found`
    //:                        what a pool opens: {file} or {url, size}
    let source = {
        spec: (name) => global.PlexoraPython.nodeSpec(name),
        files: async () => (path) => {
            const file = global.PlexoraPicks ? global.PlexoraPicks.fileFor(path) : null;
            return file ? { file } : null;
        },
    };

    //: A new source: what was opened by the old one is let go (its workers
    //: closed), and every project is checked again before its next tile.
    function use(next) {
        if (!next || typeof next.spec !== "function" || typeof next.files !== "function") {
            throw new Error("PlexoraNode.use needs spec(name) and files(name, spec)");
        }
        source = next;
        checks.clear();
        for (const entry of held.values()) close(entry);
        held.clear();
    }

    //: What a part holds open, to tell whether a new check names the same file.
    const handleOf = (found) => (found ? found.file || found.url || null : null);

    //: One file's part of a project: its pool, once the page reads it as
    //: Python does.
    async function openPart(part, size, differs, files) {
        if (!part || !part.served) return { refused: part ? part.reason : "nothing to serve" };
        const found = files(part.path);
        const file = handleOf(found);
        if (!file) return { refused: "the page does not hold that file now" };
        const pool = new TiffPool(size);
        try {
            const geometry = await pool.open(found);
            const why = differs(part, geometry);
            if (why) {
                pool.close();
                return { file, geometry, refused: `the page reads it differently: ${why}` };
            }
            return { file, geometry, pool };
        } catch (error) {
            pool.close();
            return { file, refused: String(error && error.message || error).split(" | ")[0] };
        }
    }

    const sameFile = (part, path, files) => Boolean(part && part.file && handleOf(files(path)) === part.file);

    async function check(name) {
        const asked = performance.now();
        const spec = await source.spec(name);
        const answered = performance.now();
        const served = Boolean(spec && (spec.served || (spec.labels && spec.labels.served)));
        const files = served ? await source.files(name, spec) : () => null;
        const before = held.get(name);
        // The same load of the same files: what is open stays open.
        if (before && JSON.stringify(before.spec) === JSON.stringify(spec)
            && (!before.image.pool || sameFile(before.image, spec.path, files))
            && (!before.labels.pool || sameFile(before.labels, spec.labels && spec.labels.path, files))) {
            return before;
        }
        if (before) close(before);
        held.delete(name);
        const [image, labels] = await Promise.all([
            openPart(spec, WORKERS, imageDiffers, files),
            openPart(spec.labels, Math.max(2, Math.floor(WORKERS / 2)), labelsDiffer, files),
        ]);
        //: When the check was asked and answered, and when the files were
        //: open (performance.now), for the diagnostics.
        const entry = { spec, image, labels, checkedAt: Date.now(),
                        timing: { asked, answered, opened: performance.now() } };
        held.set(name, entry);
        return entry;
    }

    function close(entry) {
        for (const part of [entry.image, entry.labels]) if (part && part.pool) part.pool.close();
    }

    function project(name) {
        if (!checks.has(name)) {
            checks.set(name, check(name).catch((error) => {
                const refused = String(error && error.message || error);
                return { image: { refused }, labels: { refused } };
            }));
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
        if (entry) close(entry);
    }

    // -- the tiles --------------------------------------------------------------

    const png16 = (tile) => png(tile.width, tile.height, 16, 0,
                                tile.format === "u16" ? tile.array : widen(tile.array));

    /** A label tile as `read_tile` packs it: each id's low three bytes as RGB,
     * alpha 0 (the high byte dropped, as Python drops it). */
    function labelPng(tile, bits) {
        const n = tile.width * tile.height;
        const bytes = bits / 8;
        const rgba = new Uint8Array(n * 4);
        const from = tile.array;
        for (let i = 0; i < n; i += 1) {
            const at = i * bytes;
            rgba[i * 4] = from[at];
            if (bytes > 1) rgba[i * 4 + 1] = from[at + 1];
            if (bytes > 2) rgba[i * 4 + 2] = from[at + 2];
        }
        return png(tile.width, tile.height, 8, 6, rgba);
    }

    const respond = (bytes) => new Response(bytes, {
        status: 200,
        headers: { "Content-Type": "image/png", "Cache-Control": "private, max-age=31536000",
                   "X-Plexora-Served-By": "browser" },
    });

    /**
     * The tile `href` asks for, read from the file: `{label: false, tile}` for
     * a channel's HD tile, `{label: true, tile, bits}` for a mask's, or null
     * for anything Python answers (see the header).
     */
    async function read(href, method = "GET") {
        const url = new URL(href, global.location.href);
        const match = url.pathname.match(TILE);
        if (!match || method !== "GET" || url.searchParams.get("view")) return null;
        const [, name, channel, level, tx, ty] = match.map((part) => decodeURIComponent(part));
        // `_parse_channel`: a name ending `_<n>` is channel n, anything else
        // the mask (whose tiles ignore `q`).
        const index = (/_(\d+)$/.exec(channel) || [])[1];
        if (index !== undefined && url.searchParams.get("q") !== "hd") return null;
        let entry = await project(name);
        if (!entry.spec) return null;
        const levelNum = Number(level);
        const [tw, th] = entry.spec.tile;
        if (index === undefined) {
            if (!entry.labels.pool && entry.spec.labels.pending && Date.now() - entry.checkedAt > RECHECK_MS) {
                // The mask was being prepared; the viewer takes it on in
                // place, so ask Python again, for this tile too.
                checks.delete(name);
                entry = await project(name);
                if (!entry.spec) return null;
            }
            const part = entry.labels;
            if (!part.pool || !entry.spec.labels.levels.some(([l]) => l === levelNum)) return null;
            const tile = await part.pool.tile(0, levelNum, Number(tx), Number(ty), tw, th, true);
            if (!tile.width || !tile.height) return null;
            return { label: true, tile, bits: BITS[entry.spec.labels.dtype] };
        }
        const part = entry.image;
        const channelNum = Number(index);
        if (!part.pool) return null;
        if (channelNum >= entry.spec.levels[0][0] || levelNum >= entry.spec.levels.length) return null;
        const tile = await part.pool.tile(channelNum, levelNum, Number(tx), Number(ty), tw, th);
        if (!tile.width || !tile.height) return null;
        return { label: false, tile };
    }

    async function answer(request) {
        const got = await read(request.url, request.method);
        if (!got) return null;
        return respond(got.label ? labelPng(got.tile, got.bits) : png16(got.tile));
    }

    /**
     * A tile as the viewer's tile scheduler delivers it (views/tileFetch.js
     * `local`), for the desktop app's page: a channel's pixels exactly as the
     * HD PNG decodes to them -- 16-bit big-endian, 8-bit samples widened as
     * Python's `astype("uint16")` widens them -- with no PNG made or decoded;
     * a mask's tile as the RGBA PNG OpenSeadragon is handed as a raster (its
     * label ids read from it as from Python's). Null for anything Python
     * answers, and for a raster ask that is not a mask's (or the reverse).
     */
    async function decoded(href, { raster = false } = {}) {
        const got = await read(href, "GET");
        if (!got || got.label !== Boolean(raster)) return null;
        if (got.label) return { raster: labelPng(got.tile, got.bits).buffer, status: 200 };
        const { tile } = got;
        return { array: tile.format === "u16" ? tile.array : widen(tile.array),
                 width: tile.width, height: tile.height, format: "u16" };
    }

    // -- the window ------------------------------------------------------------

    /**
     * Python's "window" question: the largest full-resolution sample of a
     * picked image's channel (`data_model.quantization_window_of`, which
     * otherwise reads every pixel in Python, one at a time). Asked only of a
     * file the page reads as Python does -- checked here against the shape
     * and sample type Python reads -- by a pool opened for the question.
     */
    async function windowOf({ path, channel, shape, dtype }) {
        const file = global.PlexoraPicks ? global.PlexoraPicks.fileFor(path) : null;
        if (!file) throw new Error("the page does not hold that file now");
        // A pool of its own, wider than a project's: every pixel is read once,
        // and Python waits for the answer.
        const pool = new TiffPool(SCAN_WORKERS);
        try {
            const geometry = await pool.open({ file });
            const why = imageDiffers({ levels: [shape], dtype }, geometry);
            if (why) throw new Error(`the page reads it differently: ${why}`);
            const top = await pool.max(channel, 0, geometry.levels[0], geometry.fileTile);
            return { value: Number.isFinite(top) ? top : 0 };
        } finally {
            pool.close();
        }
    }

    if (global.PlexoraPython && global.PlexoraPython.answers) global.PlexoraPython.answers("window", windowOf);

    global.PlexoraNode = Object.freeze({ answer, decoded, project, revalidate, forget, use, png, windowOf });
})(window);
