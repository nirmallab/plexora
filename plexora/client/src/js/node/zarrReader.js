/**
 * The browser node's OME-Zarr reader: an image's levels, read chunk by chunk
 * in a worker of this page, answering Plexora-grid tile requests with the
 * bytes node/tiffReader.js answers them with -- so services/browserNode.js
 * serves a picked OME-Zarr folder on the website, and a project's own store
 * on the desktop app (Browser rendering), exactly as it serves a TIFF.
 *
 * What it reads is what Plexora's Python reads (plexora/browser/node_spec.py
 * `zarr`, from server/utils/ome_zarr.py's NgffPyramid): the store's levels
 * that halve from level 0 (`arrays`: each array's path in the store, its full
 * shape and zarr format), the axes (some of t, c, z before y, x) and the plane
 * a tile is cut from (`t`, `z`). The page reads every array's own metadata
 * and refuses anything it would read differently (another shape or type,
 * Fortran order, a filter or codec it has no decoder for). Levels past these
 * (a pyramid Python derived into the project) stay Python's.
 *
 * Zarr v2 and v3 (NGFF 0.4 and 0.5): chunks keyed "." or "/" (v2; a
 * `.zarray` without `dimension_separator` is looked at, `separatorOf`), "c/..."
 * or v2-style (v3); compressors blosc (c-blosc in WebAssembly, numcodecs.js,
 * client/external/numcodecs-0.3.2, loaded on the first chunk that needs it),
 * zstd (zstddec, as geotiff.js uses it), zlib and gzip (the browser's
 * DecompressionStream), lz4 (numcodecs.js); v3's bytes (either endianness),
 * crc32c and sharding_indexed. A chunk that is not there is the array's fill
 * value, as zarr reads it.
 *
 *   createZarrReader() -> {open, tile}
 *   open({store, zarr: {arrays, axes, t, z}, record, seed})
 *       store: {base}  chunks by URL, `${base}/${key}` (the desktop shell's
 *                      folder grant), a missing one 404;
 *              {page}  chunks asked of the page by path (`ask`, the website's
 *                      picked folder: workers/tiffWorker.js relays to
 *                      services/browserNode.js), a missing one null
 *       record/seed    as node/tiffReader.js: the metadata documents one
 *                      worker read, for the pool's others
 *   tile({channel, level, tx, ty, tw, th, raw}) -> as node/tiffReader.js
 */
import { ZSTDDecoder } from "zstddec";

const now = () => performance.now();

// -- the store ----------------------------------------------------------------

/** A store's keys as `${base}/${key}` byte ranges by HTTP (the desktop's
 *  `plexora-file` folder grant). */
function urlStore(base) {
    const address = (key) => `${base}/${key.split("/").map(encodeURIComponent).join("/")}`;
    const read = async (key, headers) => {
        const response = await fetch(address(key), { headers });
        if (response.status === 404) return null;
        if (!response.ok) throw new Error(`${key}: the store answered ${response.status}`);
        return new Uint8Array(await response.arrayBuffer());
    };
    return {
        get: (key) => read(key, undefined),
        /** `length` bytes from `offset`, or the last `length` when offset < 0. */
        range: (key, offset, length) => read(key, {
            Range: offset < 0 ? `bytes=-${length}` : `bytes=${offset}-${offset + length - 1}`,
        }),
    };
}

/** A store whose files the page hands over by path (`ask(path)` -> a Blob or
 *  null): the website's picked folder. */
function pageStore(root, ask) {
    const files = new Map();
    const file = (key) => {
        if (!files.has(key)) files.set(key, ask(`${root}/${key}`));
        return files.get(key);
    };
    return {
        get: async (key) => {
            const blob = await file(key);
            return blob ? new Uint8Array(await blob.arrayBuffer()) : null;
        },
        range: async (key, offset, length) => {
            const blob = await file(key);
            if (!blob) return null;
            const from = offset < 0 ? Math.max(0, blob.size - length) : offset;
            return new Uint8Array(await blob.slice(from, from + length).arrayBuffer());
        },
    };
}

/** Another store answered first from `seed` ({key, buffer} documents another
 *  worker read), recording what it reads while `recorded` is an array. */
function seededStore(inner, seed) {
    const held = new Map((Array.isArray(seed) ? seed : []).map(({ key, buffer }) => [key, buffer]));
    const store = {
        recorded: null,
        get: async (key) => {
            if (held.has(key)) {
                const buffer = held.get(key);
                return buffer ? new Uint8Array(buffer.slice(0)) : null;
            }
            const bytes = await inner.get(key);
            if (store.recorded) store.recorded.push({ key, buffer: bytes ? bytes.slice().buffer : null });
            return bytes;
        },
        range: (key, offset, length) => inner.range(key, offset, length),
        /** Whether `key` is there, by one byte of it; recorded like a
         *  document, so the pool's other workers do not ask again. */
        exists: async (key) => {
            const mark = `exists:${key}`;
            if (held.has(mark)) return held.get(mark) !== null;
            const bytes = await inner.range(key, 0, 1).catch(() => null);
            const found = Boolean(bytes && bytes.length);
            if (store.recorded) store.recorded.push({ key: mark, buffer: found ? new ArrayBuffer(0) : null });
            return found;
        },
    };
    return store;
}

// -- the codecs ---------------------------------------------------------------

/** numcodecs.js, beside this worker's bundle (client/external). */
const NUMCODECS = "../external/numcodecs-0.3.2/";
const loaded = new Map();

function numcodecs(name) {
    if (!loaded.has(name)) {
        // Node's tests say where the files are (`PLEXORA_NUMCODECS_URL`).
        const base = globalThis.PLEXORA_NUMCODECS_URL || new URL(NUMCODECS, self.location.href).href;
        const url = new URL(`${name}.js`, base).href;
        loaded.set(name, import(/* webpackIgnore: true */ url).then((module) => new module.default()));
    }
    return loaded.get(name);
}

let zstd = null;
function zstdDecoder() {
    if (!zstd) {
        const decoder = new ZSTDDecoder();
        zstd = decoder.init().then(() => decoder);
    }
    return zstd;
}

async function through(bytes, format) {
    const stream = new DecompressionStream(format);
    const writer = stream.writable.getWriter();
    writer.write(bytes).catch(() => {});
    writer.close().catch(() => {});
    return new Uint8Array(await new Response(stream.readable).arrayBuffer());
}

/**
 * The decoder of one bytes-to-bytes codec, by its zarr name and
 * configuration (v2's compressor, or a v3 codec), or a reason it is not
 * read here. `size`: the bytes it decodes to, where a codec needs it.
 */
function bytesCodec(name, configuration = {}) {
    switch (name) {
    case "blosc":
        return async (bytes) => (await numcodecs("blosc")).decode(bytes);
    case "lz4":
        return async (bytes) => (await numcodecs("lz4")).decode(bytes);
    case "zstd":
        return async (bytes, size) => (await zstdDecoder()).decode(bytes, size);
    case "gzip":
        return (bytes) => through(bytes, "gzip");
    case "zlib":
        return (bytes) => through(bytes, "deflate");
    case "crc32c":
        // The checksum's 4 bytes at the end; the chunk is what precedes them.
        return async (bytes) => bytes.subarray(0, bytes.length - 4);
    default:
        return `the ${name} codec`;
    }
}

// -- the arrays ---------------------------------------------------------------

const KINDS = {
    u1: Uint8Array, u2: Uint16Array, u4: Uint32Array,
    i1: Int8Array, i2: Int16Array, i4: Int32Array,
    f4: Float32Array, f8: Float64Array,
};
const V3_TYPES = {
    uint8: "u1", uint16: "u2", uint32: "u4", int8: "i1", int16: "i2", int32: "i4",
    float32: "f4", float64: "f8",
};

const product = (values) => values.reduce((a, b) => a * b, 1);

/** Bytes in one order as samples in this machine's (little-endian) order. */
function samples(bytes, kind, little) {
    const Kind = KINDS[kind];
    const n = Kind.BYTES_PER_ELEMENT;
    const own = bytes.byteOffset % n === 0 ? bytes : bytes.slice();
    if (little || n === 1) return new Kind(own.buffer, own.byteOffset, own.byteLength / n);
    const view = new DataView(own.buffer, own.byteOffset, own.byteLength);
    const out = new Kind(own.byteLength / n);
    const get = Kind === Float32Array ? "getFloat32" : Kind === Float64Array ? "getFloat64"
        : `get${Kind.name.replace("Array", "")}`;
    for (let i = 0; i < out.length; i += 1) out[i] = view[get](i * n, false);
    return out;
}

/** The fill value as a sample (zarr's null and NaN spellings included). */
function fillOf(value, kind) {
    if (value === null || value === undefined) return 0;
    if (value === "NaN") return NaN;
    if (value === "Infinity") return Infinity;
    if (value === "-Infinity") return -Infinity;
    if (typeof value === "string" && value.startsWith("0x") && kind.startsWith("f")) {
        const bytes = new Uint8Array(KINDS[kind].BYTES_PER_ELEMENT);
        const hex = value.slice(2);
        for (let i = 0; i < bytes.length; i += 1) bytes[bytes.length - 1 - i] = parseInt(hex.substr(i * 2, 2), 16);
        return new KINDS[kind](bytes.buffer)[0];
    }
    return Number(value);
}

/** A v2 `.zarray` as this reader's array, or {refused}. */
function fromV2(path, doc) {
    const match = /^([<>|])([uif])(\d)$/.exec(doc.dtype || "");
    if (!match || !KINDS[match[2] + match[3]]) return { refused: `${path}: ${doc.dtype} samples` };
    if (doc.order && doc.order !== "C") return { refused: `${path}: ${doc.order} order` };
    if (Array.isArray(doc.filters) && doc.filters.length) return { refused: `${path}: filters` };
    const decoders = [];
    if (doc.compressor) {
        const decode = bytesCodec(doc.compressor.id, doc.compressor);
        if (typeof decode === "string") return { refused: `${path}: ${decode}` };
        decoders.push(decode);
    }
    const kind = match[2] + match[3];
    const array = {
        shape: doc.shape.map(Number), chunks: doc.chunks.map(Number), kind,
        little: match[1] !== ">", fill: fillOf(doc.fill_value, kind),
        // Absent before zarr 2.8, whose NestedDirectoryStore still wrote
        // "0/0/0": `open` looks (`separatorOf`) rather than assume ".".
        separator: doc.dimension_separator || null,
        key: (coords) => `${path}/${coords.length ? coords.join(array.separator || ".") : "0"}`,
        decoders, sharding: null,
    };
    return array;
}

/** How a v2 array whose `.zarray` names no separator stores its chunks, by
 *  looking for a chunk in both spellings (a byte of each, not the chunk), once
 *  per array at open: the first chunk, then -- a sparse array, its corner
 *  never written -- the centre one and the last. None of them there reads as
 *  ".", the metadata's own default. (Python lists the folder instead, which
 *  the page cannot.) */
async function separatorOf(source, array, path) {
    const grid = array.shape.map((n, i) => Math.max(1, Math.ceil(n / array.chunks[i])));
    const n = grid.length;
    const plane = (pick) => grid.map((g, i) => (i >= n - 2 ? pick(g) : 0));
    const candidates = [plane(() => 0), plane((g) => Math.floor(g / 2)), plane((g) => g - 1)];
    const seen = new Set();
    for (const coords of candidates) {
        const name = coords.join(".");
        if (seen.has(name)) continue;
        seen.add(name);
        const [dotted, nested] = await Promise.all([
            source.exists(`${path}/${coords.join(".")}`),
            source.exists(`${path}/${coords.join("/")}`),
        ]);
        if (dotted) return ".";
        if (nested) return "/";
    }
    return ".";
}

/** A v3 codec list's decoding: bytes-to-bytes decoders (in decode order)
 *  and the endianness of the samples, or a reason. */
function v3Codecs(path, codecs) {
    const list = Array.isArray(codecs) ? codecs : [];
    const at = list.findIndex((c) => c && (c.name === "bytes" || c.name === "sharding_indexed"));
    if (at < 0) return { refused: `${path}: no bytes codec` };
    if (at > 0) return { refused: `${path}: ${list[0].name} before the bytes` };
    const decoders = [];
    for (const codec of list.slice(at + 1).reverse()) {
        const decode = bytesCodec(codec.name, codec.configuration || {});
        if (typeof decode === "string") return { refused: `${path}: ${decode}` };
        decoders.push(decode);
    }
    const head = list[at];
    return { head, decoders };
}

/** A v3 `zarr.json` as this reader's array, or {refused}. */
function fromV3(path, doc) {
    if (doc.node_type !== "array") return { refused: `${path}: not an array` };
    const kind = V3_TYPES[doc.data_type];
    if (!kind) return { refused: `${path}: ${doc.data_type} samples` };
    const grid = doc.chunk_grid || {};
    if (grid.name !== "regular") return { refused: `${path}: a ${grid.name} chunk grid` };
    const chunks = (grid.configuration || {}).chunk_shape;
    const encoding = doc.chunk_key_encoding || { name: "default" };
    const separator = (encoding.configuration || {}).separator || (encoding.name === "v2" ? "." : "/");
    const key = encoding.name === "v2"
        ? (coords) => `${path}/${coords.length ? coords.join(separator) : "0"}`
        : (coords) => `${path}/${["c", ...coords].join(separator)}`;
    const outer = v3Codecs(path, doc.codecs);
    if (outer.refused) return outer;
    const array = { shape: doc.shape.map(Number), chunks: chunks.map(Number), kind,
                    fill: fillOf(doc.fill_value, kind), key, decoders: outer.decoders, little: true, sharding: null };
    if (outer.head.name === "bytes") {
        array.little = ((outer.head.configuration || {}).endian || "little") === "little";
        return array;
    }
    // sharding_indexed: the store's chunk is a shard of inner chunks, each
    // found through the shard's index of (offset, length) pairs.
    const config = outer.head.configuration || {};
    const inner = v3Codecs(path, config.codecs);
    if (inner.refused) return inner;
    if (inner.head.name !== "bytes") return { refused: `${path}: nested shards` };
    const indexCodecs = config.index_codecs || [{ name: "bytes", configuration: { endian: "little" } },
                                                { name: "crc32c" }];
    const index = v3Codecs(path, indexCodecs);
    if (index.refused) return index;
    if (index.head.name !== "bytes" || indexCodecs.slice(1).some((c) => c.name !== "crc32c")) {
        return { refused: `${path}: a shard index coded otherwise` };
    }
    if (outer.decoders.length) return { refused: `${path}: a codec over a shard` };
    const innerShape = config.chunk_shape.map(Number);
    const perShard = array.chunks.map((n, i) => n / innerShape[i]);
    if (perShard.some((n) => !Number.isInteger(n))) return { refused: `${path}: shards not whole inner chunks` };
    const checksum = indexCodecs.some((c) => c.name === "crc32c");
    array.sharding = {
        shardShape: array.chunks, perShard, at: config.index_location || "end",
        indexBytes: product(perShard) * 16 + (checksum ? 4 : 0),
        indexLittle: ((index.head.configuration || {}).endian || "little") === "little",
    };
    array.chunks = innerShape;
    array.decoders = inner.decoders;
    array.little = ((inner.head.configuration || {}).endian || "little") === "little";
    return array;
}

// -- the reader ---------------------------------------------------------------

export function createZarrReader({ ask = null } = {}) {
    let state = null;

    async function open({ store, zarr, record = false, seed = null }) {
        const t0 = now();
        if (!store || !zarr || !Array.isArray(zarr.arrays) || !zarr.arrays.length) {
            throw new Error("no OME-Zarr store to open");
        }
        const base = store.base ? urlStore(store.base)
            : store.page && ask ? pageStore(store.page, ask) : null;
        if (!base) throw new Error("no way to read the store");
        const source = seededStore(base, seed);
        if (record) source.recorded = [];
        const axes = zarr.axes.map((a) => String(a).toLowerCase());
        const yAxis = axes.indexOf("y");
        const xAxis = axes.indexOf("x");
        if (yAxis !== axes.length - 2 || xAxis !== axes.length - 1) throw new Error("the axes do not end y, x");
        const arrays = await Promise.all(zarr.arrays.map(async ({ path, shape, format }) => {
            const name = String(path).replace(/^\/+|\/+$/g, "");
            const v3 = Number(format) === 3;
            const bytes = await source.get(`${name}/${v3 ? "zarr.json" : ".zarray"}`);
            if (!bytes) throw new Error(`${name}: no array metadata`);
            const doc = JSON.parse(new TextDecoder().decode(bytes));
            const array = v3 ? fromV3(name, doc) : fromV2(name, doc);
            if (array.refused) throw new Error(array.refused);
            if (!v3 && !array.separator) {
                array.separator = array.shape.length > 1 ? await separatorOf(source, array, name) : ".";
            }
            if (array.shape.length !== axes.length) throw new Error(`${name}: ${array.shape.length} dimensions`);
            if (JSON.stringify(array.shape) !== JSON.stringify(shape.map(Number))) {
                throw new Error(`${name} is ${array.shape.join("x")}`);
            }
            return array;
        }));
        const kinds = new Set(arrays.map((a) => a.kind));
        if (kinds.size !== 1) throw new Error("levels of different sample types");
        const kind = arrays[0].kind;
        const cAxis = axes.indexOf("c");
        const pins = axes.map((axis) => (axis === "t" ? Number(zarr.t || 0) : axis === "z" ? Number(zarr.z || 0) : 0));
        state = { source, arrays, axes, yAxis, xAxis, cAxis, pins, kind, cache: new Map() };
        const first = arrays[0];
        const geometry = {
            layout: "zarr",
            width: first.shape[xAxis], height: first.shape[yAxis],
            numChannels: cAxis >= 0 ? first.shape[cAxis] : 1,
            levels: arrays.map((a) => ({ width: a.shape[xAxis], height: a.shape[yAxis] })),
            fileTile: { width: first.chunks[xAxis], height: first.chunks[yAxis], tiled: true },
            bitsPerSample: KINDS[kind].BYTES_PER_ELEMENT * 8,
            sampleFormat: kind[0] === "f" ? 3 : kind[0] === "i" ? 2 : 1,
            compression: "zarr",
            littleEndian: true,
            names: null,
        };
        const opened = { geometry, timing: { openMs: now() - t0 } };
        if (record) {
            opened.seed = source.recorded;
            source.recorded = null;
        }
        return opened;
    }

    /** A chunk's samples (its whole chunk shape, C order), its fill where the
     *  store has none. Kept a while: a tile and its neighbours share chunks
     *  larger than the tile grid. */
    function chunk(array, coords) {
        const key = array.key(coords);
        const cache = state.cache;
        if (cache.has(key)) {
            const hit = cache.get(key);
            cache.delete(key);
            cache.set(key, hit);
            return hit;
        }
        const size = product(array.chunks);
        const reading = (async () => {
            const bytes = array.sharding ? await shardChunk(array, coords) : await state.source.get(key);
            if (!bytes) return null;
            let out = bytes;
            const want = size * KINDS[array.kind].BYTES_PER_ELEMENT;
            for (const decode of array.decoders) out = await decode(out, want);
            if (out.byteLength !== want) throw new Error(`${key}: ${out.byteLength} bytes, not ${want}`);
            return samples(out, array.kind, array.little);
        })();
        cache.set(key, reading);
        reading.catch(() => cache.delete(key));
        while (cache.size > 24) cache.delete(cache.keys().next().value);
        return reading;
    }

    /** One inner chunk's bytes out of its shard, through the shard's index;
     *  null where the shard or the chunk is not written. */
    async function shardChunk(array, coords) {
        const { shardShape, perShard, at, indexBytes, indexLittle } = array.sharding;
        const shardCoords = coords.map((c, i) => Math.floor((c * array.chunks[i]) / shardShape[i]));
        const shardKey = array.key(shardCoords);
        const indexes = (state.indexes ||= new Map());
        if (!indexes.has(shardKey)) {
            const reading = state.source.range(shardKey, at === "start" ? 0 : -1, indexBytes)
                .then((bytes) => (bytes ? new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength) : null));
            indexes.set(shardKey, reading);
            reading.catch(() => indexes.delete(shardKey));
            while (indexes.size > 256) indexes.delete(indexes.keys().next().value);
        }
        const index = await indexes.get(shardKey);
        if (!index) return null;
        let slot = 0;
        coords.forEach((c, i) => { slot = slot * perShard[i] + (c % perShard[i]); });
        const lo = index.getUint32(slot * 16, indexLittle);
        const hi = index.getUint32(slot * 16 + 4, indexLittle);
        const nlo = index.getUint32(slot * 16 + 8, indexLittle);
        const nhi = index.getUint32(slot * 16 + 12, indexLittle);
        const empty = (indexLittle ? hi : lo) === 0xffffffff && (indexLittle ? lo : hi) === 0xffffffff;
        if (empty) return null;
        const offset = (indexLittle ? hi : lo) * 2 ** 32 + (indexLittle ? lo : hi);
        const length = (indexLittle ? nhi : nlo) * 2 ** 32 + (indexLittle ? nlo : nhi);
        return state.source.range(shardKey, offset, length);
    }

    /** The window [y0, y1) x [x0, x1) of one channel's plane of a level, as
     *  native samples. */
    async function window(level, channel, y0, y1, x0, x1) {
        const array = state.arrays[level];
        const { yAxis, xAxis, cAxis } = state;
        const at = state.pins.slice();
        if (cAxis >= 0) at[cAxis] = channel;
        else if (channel !== 0) throw new Error(`no channel ${channel}`);
        const Kind = KINDS[array.kind];
        const w = x1 - x0;
        const out = new Kind(w * (y1 - y0));
        const cy = array.chunks[yAxis];
        const cx = array.chunks[xAxis];
        // Strides of a chunk's samples (C order).
        const strides = array.chunks.map((_, i) => product(array.chunks.slice(i + 1)));
        const reads = [];
        for (let gy = Math.floor(y0 / cy); gy * cy < y1; gy += 1) {
            for (let gx = Math.floor(x0 / cx); gx * cx < x1; gx += 1) {
                const coords = at.map((value, i) => Math.floor(value / array.chunks[i]));
                coords[yAxis] = gy;
                coords[xAxis] = gx;
                reads.push(chunk(array, coords).then((values) => {
                    const ry0 = Math.max(y0, gy * cy);
                    const ry1 = Math.min(y1, (gy + 1) * cy);
                    const rx0 = Math.max(x0, gx * cx);
                    const rx1 = Math.min(x1, (gx + 1) * cx);
                    if (!values) {
                        if (array.fill !== 0) {
                            for (let y = ry0; y < ry1; y += 1) out.fill(array.fill, (y - y0) * w + rx0 - x0, (y - y0) * w + rx1 - x0);
                        }
                        return;
                    }
                    let lead = 0;
                    at.forEach((value, i) => {
                        if (i !== yAxis && i !== xAxis) lead += (value % array.chunks[i]) * strides[i];
                    });
                    for (let y = ry0; y < ry1; y += 1) {
                        const from = lead + (y - gy * cy) * strides[yAxis] + (rx0 - gx * cx);
                        out.set(values.subarray(from, from + (rx1 - rx0)), (y - y0) * w + (rx0 - x0));
                    }
                }));
            }
        }
        await Promise.all(reads);
        return out;
    }

    async function tile({ channel, level, tx, ty, tw = 1024, th = 1024, raw = false }) {
        if (!state) throw new Error("open a store first");
        const t0 = now();
        const array = state.arrays[level];
        if (!array) throw new Error(`no level ${level}`);
        const W = array.shape[state.xAxis];
        const H = array.shape[state.yAxis];
        const x0 = tx * tw;
        const y0 = ty * th;
        const x1 = Math.min(W, x0 + tw);
        const y1 = Math.min(H, y0 + th);
        if (x0 >= W || y0 >= H) {
            return { width: 0, height: 0, array: new Uint8Array(0), format: "empty", timing: { totalMs: now() - t0 } };
        }
        const values = await window(level, channel, y0, y1, x0, x1);
        const bytes = new Uint8Array(values.buffer, values.byteOffset, values.byteLength);
        const bits = values.BYTES_PER_ELEMENT * 8;
        let out;
        let format;
        if (raw) {
            out = bytes;
            format = `raw${bits}`;
        } else if (bits === 16) {
            // The big-endian bytes the HD PNG carries (node/tiffReader.js).
            out = new Uint8Array(bytes.length);
            for (let i = 0; i < bytes.length; i += 2) {
                out[i] = bytes[i + 1];
                out[i + 1] = bytes[i];
            }
            format = "u16";
        } else if (bits === 8) {
            out = bytes;
            format = "u8";
        } else if (bits === 32 && !(values instanceof Float32Array)) {
            out = bytes;
            format = "u32";
        } else {
            throw new Error(`${bits}-bit samples are not handled`);
        }
        return { width: x1 - x0, height: y1 - y0, array: out, format, timing: { totalMs: now() - t0 } };
    }

    return { open, tile };
}
