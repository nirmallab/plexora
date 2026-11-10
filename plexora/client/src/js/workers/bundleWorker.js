/**
 * The website's `.plexora` files (server/models/bundles.py, format 2): the
 * bytes moved here, every decision taken in Python.
 *
 * The website's Python has a 4 GiB heap and a bundle can carry gigabytes (an
 * included OME-TIFF, a 200 MB cell table), so Python only decides --
 * `/projects/export/plan` lists every member with where it is, and
 * `/projects/import/preview` says where each large member of an import goes --
 * and this dedicated worker (services/bundleWeb.js starts one per job) moves
 * them: out, `File.stream()` through CRC-32 and SHA-256 and, unless the member
 * is stored, `CompressionStream("deflate-raw")`, into a file written by
 * position; in, `File.slice` on the central directory and
 * `DecompressionStream("deflate-raw")`, straight into the project store's
 * folder. Nothing holds a whole member: memory stays at a few chunks.
 *
 * The file is what bundles.ZipSink writes, so either app reads the other's:
 *  - the manifest first and stored, its room kept (Python's `_reserve`) and
 *    filled in at the end, padded with spaces (still valid JSON), its CRC
 *    patched in the local header;
 *  - every other member's local header patched with its CRC and sizes once it
 *    has streamed -- what Python's `zipfile` writes to a seekable file, and
 *    what every unzip tool reads. The file is written by position anyway (the
 *    manifest goes back at the end), so there are no data descriptors. A
 *    member of ZIP64_FROM bytes or more has ZIP64 sizes from the start;
 *  - the central directory, ZIP64 end records where a count or an offset
 *    needs them, then the end record.
 * Each member's method is bundles._choose_method's: stored for what is
 * compressed already; else the first 4 MiB deflated as a sample, and stored
 * when that keeps more than 90% of its size. The browser deflates at one
 * level, zlib's default 6 (Python samples at 1 and writes at 6).
 *
 * Messages ({op, job, ...}; every answer carries `job`):
 *   export {members: [{bundle_path, size, mtime?, source}], reserve, out, stores}
 *          -> {progress}..., {packed: {sums, left_out}}, then waits for
 *   finish {manifest: text}   -> {done: {size, file?}}   (the file read back first)
 *   list {file}               -> {done: {entries, manifest}}
 *   extract {file, members: [{path, store, rel, size, sha256?}], stores}
 *          -> {progress}..., {done: {placed: [{store, rel}], made: [{store, rel}]}}
 *          (`made`: the folders it made; on failure nothing is left)
 *   remove {paths: [{store, rel}], made?, stores}  -> {done: {removed}}
 *   cancel                    the job stopped; what it wrote is removed
 *   -> {error, cancelled?} when one fails.
 * A member's `source` is {store, rel} (a file in a project store's folder),
 * {opfs: [parts]} (one in this site's own storage), {blob} (a File or Blob
 * the page has), or {gone: why} (left out, and said so in the manifest). `stores` names each store's folder: {key: {handle} | {opfs:
 * [parts]}}. `out` is {handle} (the Save dialog's file) or {opfs: [parts],
 * name} (a file in this site's storage, downloaded from there).
 *
 * The codec below (crc32, Sha256, chooseMethod, ZipWriter, readCentral,
 * pumpMember) uses no DOM: tests/js/bundle_codec_probe.mjs runs it in Node.
 */

export const MANIFEST = "plexora-bundle.json";
export const STORED = 0;
export const DEFLATED = 8;
//: Bytes of a member deflated to decide how it is stored (bundles.SAMPLE).
export const SAMPLE = 4 << 20;
//: Stored whole: compressed already (bundles.STORED_SUFFIXES).
export const STORED_SUFFIXES = [".png", ".jpg", ".jpeg", ".webp", ".parquet", ".gz", ".zst", ".zip", ".mp4",
                                ".h5ad", ".npz", ".jp2"];
//: A sample that deflates to more than this share of its size is stored.
export const INCOMPRESSIBLE = 0.9;
//: A member at least this large has ZIP64 sizes from the start (bundles.ZIP64_FROM).
export const ZIP64_FROM = 2 ** 30;
//: A member larger than RATIO_FLOOR that inflates more than MAX_RATIO times is refused.
export const MAX_RATIO = 1000;
export const RATIO_FLOOR = 1 << 20;

const LOCAL = 0x04034b50;
const CENTRAL = 0x02014b50;
const END = 0x06054b50;
const END64 = 0x06064b50;
const LOCATOR64 = 0x07064b50;
const U32 = 0xffffffff;
//: Python's `ZipInfo.external_attr = 0o644 << 16`, made on Unix (3).
const EXTERNAL = (0o644 << 16) >>> 0;
const MADE_ON = 3;

// -- CRC-32 (zlib's), eight bytes a step ------------------------------------

const CRC = (() => {
    const table = new Int32Array(256 * 8);
    for (let n = 0; n < 256; n += 1) {
        let c = n;
        for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        table[n] = c;
    }
    for (let n = 0; n < 256; n += 1) {
        let c = table[n];
        for (let k = 1; k < 8; k += 1) {
            c = table[c & 0xff] ^ (c >>> 8);
            table[k * 256 + n] = c;
        }
    }
    return table;
})();

/** zlib.crc32(bytes, crc): the running CRC-32 of `bytes` after `crc`. */
export function crc32(bytes, crc = 0) {
    const t = CRC;
    let c = ~crc;
    let i = 0;
    const n = bytes.length;
    for (; i + 8 <= n; i += 8) {
        c ^= bytes[i] | (bytes[i + 1] << 8) | (bytes[i + 2] << 16) | (bytes[i + 3] << 24);
        c = t[1792 + (c & 0xff)] ^ t[1536 + ((c >>> 8) & 0xff)] ^ t[1280 + ((c >>> 16) & 0xff)] ^ t[1024 + (c >>> 24)]
            ^ t[768 + bytes[i + 4]] ^ t[512 + bytes[i + 5]] ^ t[256 + bytes[i + 6]] ^ t[bytes[i + 7]];
    }
    for (; i < n; i += 1) c = t[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return ~c >>> 0;
}

// -- SHA-256, fed a chunk at a time ------------------------------------------
//
// WebCrypto digests a whole buffer only; a member is hashed as it streams
// (bundles.py takes every sha256 that way, never in a second read).

const K = new Int32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

export class Sha256 {
    constructor() {
        this.h = new Int32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
        this.w = new Int32Array(64);
        this.tail = new Uint8Array(64);
        this.held = 0;
        this.length = 0;
        this.done = false;
    }

    update(bytes) {
        if (this.done) throw new Error("this digest is finished");
        const n = bytes.length;
        let i = 0;
        this.length += n;
        if (this.held) {
            const take = Math.min(64 - this.held, n);
            this.tail.set(bytes.subarray(0, take), this.held);
            this.held += take;
            i = take;
            if (this.held < 64) return this;
            this.block(this.tail, 0);
            this.held = 0;
        }
        for (; i + 64 <= n; i += 64) this.block(bytes, i);
        if (i < n) {
            this.tail.set(bytes.subarray(i), 0);
            this.held = n - i;
        }
        return this;
    }

    block(p, at) {
        const w = this.w;
        const h = this.h;
        for (let t = 0; t < 16; t += 1, at += 4) w[t] = (p[at] << 24) | (p[at + 1] << 16) | (p[at + 2] << 8) | p[at + 3];
        for (let t = 16; t < 64; t += 1) {
            const x = w[t - 15];
            const y = w[t - 2];
            const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
            const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
            w[t] = (w[t - 16] + s0 + w[t - 7] + s1) | 0;
        }
        let a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], k = h[7];
        for (let t = 0; t < 64; t += 1) {
            const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
            const t1 = (k + S1 + ((e & f) ^ (~e & g)) + K[t] + w[t]) | 0;
            const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
            const t2 = (S0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
            k = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
        }
        h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
        h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0; h[6] = (h[6] + g) | 0; h[7] = (h[7] + k) | 0;
    }

    /** The 32-byte digest; the object is finished after. */
    digest() {
        const blocks = this.held + 9 > 64 ? 2 : 1;
        const last = new Uint8Array(blocks * 64);
        last.set(this.tail.subarray(0, this.held));
        last[this.held] = 0x80;
        const view = new DataView(last.buffer);
        view.setUint32(last.length - 8, Math.floor(this.length / 0x20000000));
        view.setUint32(last.length - 4, (this.length * 8) >>> 0);
        for (let b = 0; b < blocks; b += 1) this.block(last, b * 64);
        this.done = true;
        const out = new Uint8Array(32);
        const put = new DataView(out.buffer);
        for (let i = 0; i < 8; i += 1) put.setUint32(i * 4, this.h[i]);
        return out;
    }

    hex() {
        return Array.from(this.digest(), (byte) => byte.toString(16).padStart(2, "0")).join("");
    }
}

// -- little-endian fields -----------------------------------------------------

function put16(view, at, value) { view.setUint16(at, value, true); }
function put32(view, at, value) { view.setUint32(at, value >>> 0, true); }
function put64(view, at, value) {
    view.setUint32(at, value % 0x100000000, true);
    view.setUint32(at + 4, Math.floor(value / 0x100000000), true);
}
function get64(view, at) {
    return view.getUint32(at, true) + view.getUint32(at + 4, true) * 0x100000000;
}

const encoder = new TextEncoder();

//: A ZIP's DOS date and time: local time, as Python's `time.localtime`,
//: never before 1980.
function dosStamp(ms) {
    const d = new Date(Number.isFinite(ms) && ms > 0 ? ms : Date.now());
    if (d.getFullYear() < 1980) return { time: 0, date: (1 << 5) | 1 };
    return { time: (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1),
             date: ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate() };
}

/** bundles._choose_method: STORED or DEFLATED for a member's bytes (a Blob). */
export async function chooseMethod(name, blob) {
    const lower = String(name).toLowerCase();
    if (STORED_SUFFIXES.some((suffix) => lower.endsWith(suffix))) return STORED;
    const sample = blob.slice(0, SAMPLE);
    if (sample.size < 512) return DEFLATED;
    let packed = 0;
    const reader = sample.stream().pipeThrough(new CompressionStream("deflate-raw")).getReader();
    for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        packed += value.length;
    }
    return packed > INCOMPRESSIBLE * sample.size ? STORED : DEFLATED;
}

export class Cancelled extends Error {
    constructor() {
        super("Stopped.");
        this.name = "Cancelled";
    }
}

/**
 * A ZIP64 file written through `sink.write(bytes, at)` (a promise or not),
 * always by position: the manifest's room first (`reserve`), the members as
 * they stream (`add`), the manifest put in its room (`fill`), the central
 * directory and end records (`finish`). `zip64From` and `zip64End` are for
 * tests: ZIP64 headers and end records on a small file.
 */
export class ZipWriter {
    constructor(sink, { zip64From = ZIP64_FROM, zip64End = false } = {}) {
        this.sink = sink;
        this.at = 0;
        this.entries = [];
        this.zip64From = zip64From;
        this.zip64End = zip64End;
    }

    async put(bytes) {
        const at = this.at;
        this.at += bytes.length;
        await this.sink.write(bytes, at);
    }

    header(name, { method, zip64, mtime }) {
        const nameBytes = encoder.encode(name);
        const utf8 = nameBytes.some((byte) => byte > 0x7f);
        const head = new Uint8Array(30 + nameBytes.length + (zip64 ? 20 : 0));
        const view = new DataView(head.buffer);
        const stamp = dosStamp(mtime);
        put32(view, 0, LOCAL);
        put16(view, 4, zip64 ? 45 : 20);
        put16(view, 6, utf8 ? 0x800 : 0);
        put16(view, 8, method);
        put16(view, 10, stamp.time);
        put16(view, 12, stamp.date);
        put32(view, 14, 0);
        put32(view, 18, zip64 ? U32 : 0);
        put32(view, 22, zip64 ? U32 : 0);
        put16(view, 26, nameBytes.length);
        put16(view, 28, zip64 ? 20 : 0);
        head.set(nameBytes, 30);
        if (zip64) {
            const extra = 30 + nameBytes.length;
            put16(view, extra, 1);
            put16(view, extra + 2, 16);
        }
        return { head, nameBytes, utf8, stamp };
    }

    /** The first member's room, `room` spaces, filled in by `fill`. */
    async reserve(name, room, mtime) {
        const { head, nameBytes, utf8, stamp } = this.header(name, { method: STORED, zip64: false, mtime });
        const view = new DataView(head.buffer);
        put32(view, 18, room);
        put32(view, 22, room);
        const offset = this.at;
        await this.put(head);
        const spaces = new Uint8Array(Math.min(room, 1 << 20)).fill(0x20);
        for (let left = room; left > 0; left -= spaces.length) await this.put(left >= spaces.length ? spaces : spaces.subarray(0, left));
        this.entries.push({ name, nameBytes, utf8, method: STORED, crc: crc32(new Uint8Array(room).fill(0x20)),
                            csize: room, usize: room, offset, zip64: false, stamp, room });
    }

    /** The reserved member's bytes, padded with spaces to its room. */
    async fill(name, bytes) {
        const entry = this.entries.find((item) => item.name === name && item.room !== undefined);
        if (!entry) throw new Error(`${name} has no room kept`);
        if (bytes.length > entry.room) throw new Error("The project file's manifest outgrew its room.");
        const padded = new Uint8Array(entry.room).fill(0x20);
        padded.set(bytes);
        entry.crc = crc32(padded);
        await this.sink.write(padded, entry.offset + 30 + entry.nameBytes.length);
        const crc = new Uint8Array(4);
        put32(new DataView(crc.buffer), 0, entry.crc);
        await this.sink.write(crc, entry.offset + 14);
    }

    /**
     * One member from `stream` (a ReadableStream of its bytes, `size` of
     * them): its local header, its data (deflated or not), then the header
     * patched. -> {size, csize, crc, sha256}. `onBytes(n)` as it reads;
     * `cancelled()` is asked between chunks.
     */
    async add(name, stream, { size, method = DEFLATED, mtime, onBytes, cancelled } = {}) {
        const zip64 = Number(size) >= this.zip64From;
        const { head, nameBytes, utf8, stamp } = this.header(name, { method, zip64, mtime });
        const offset = this.at;
        await this.put(head);
        const start = this.at;
        const digest = new Sha256();
        let crc = 0;
        let count = 0;
        const tap = new TransformStream({
            transform(chunk, controller) {
                const bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
                crc = crc32(bytes, crc);
                digest.update(bytes);
                count += bytes.length;
                if (onBytes) onBytes(bytes.length);
                controller.enqueue(bytes);
            },
        });
        let out = stream.pipeThrough(tap);
        if (method === DEFLATED) out = out.pipeThrough(new CompressionStream("deflate-raw"));
        const reader = out.getReader();
        try {
            for (;;) {
                if (cancelled && cancelled()) throw new Cancelled();
                const { value, done } = await reader.read();
                if (done) break;
                await this.put(value);
            }
        } catch (error) {
            reader.cancel(error).catch(() => {});
            throw error;
        }
        const csize = this.at - start;
        if (!zip64 && (count >= U32 || csize >= U32)) {
            throw new Error(`${name} grew past 4 GB while it was read`);
        }
        const patch = new Uint8Array(12);
        const view = new DataView(patch.buffer);
        put32(view, 0, crc);
        put32(view, 4, zip64 ? U32 : csize);
        put32(view, 8, zip64 ? U32 : count);
        await this.sink.write(patch, offset + 14);
        if (zip64) {
            const sizes = new Uint8Array(16);
            const at = new DataView(sizes.buffer);
            put64(at, 0, count);
            put64(at, 8, csize);
            await this.sink.write(sizes, offset + 30 + nameBytes.length + 4);
        }
        const sha256 = digest.hex();
        this.entries.push({ name, nameBytes, utf8, method, crc, csize, usize: count, offset, zip64, stamp });
        return { size: count, csize, crc, sha256 };
    }

    /** The central directory and the end records. -> the file's size. */
    async finish() {
        const start = this.at;
        for (const entry of this.entries) {
            const sizes = entry.usize >= U32 || entry.csize >= U32;
            const far = entry.offset >= U32;
            const extraLength = (sizes ? 16 : 0) + (far ? 8 : 0);
            const record = new Uint8Array(46 + entry.nameBytes.length + (extraLength ? 4 + extraLength : 0));
            const view = new DataView(record.buffer);
            const version = entry.zip64 || sizes || far ? 45 : 20;
            put32(view, 0, CENTRAL);
            put16(view, 4, (MADE_ON << 8) | version);
            put16(view, 6, version);
            put16(view, 8, entry.utf8 ? 0x800 : 0);
            put16(view, 10, entry.method);
            put16(view, 12, entry.stamp.time);
            put16(view, 14, entry.stamp.date);
            put32(view, 16, entry.crc);
            put32(view, 20, sizes ? U32 : entry.csize);
            put32(view, 24, sizes ? U32 : entry.usize);
            put16(view, 28, entry.nameBytes.length);
            put16(view, 30, extraLength ? 4 + extraLength : 0);
            put16(view, 32, 0);
            put16(view, 34, 0);
            put16(view, 36, 0);
            put32(view, 38, EXTERNAL);
            put32(view, 42, far ? U32 : entry.offset);
            record.set(entry.nameBytes, 46);
            if (extraLength) {
                let at = 46 + entry.nameBytes.length;
                put16(view, at, 1);
                put16(view, at + 2, extraLength);
                at += 4;
                if (sizes) {
                    put64(view, at, entry.usize);
                    put64(view, at + 8, entry.csize);
                    at += 16;
                }
                if (far) put64(view, at, entry.offset);
            }
            await this.put(record);
        }
        const size = this.at - start;
        const count = this.entries.length;
        const big = this.zip64End || count >= 0xffff || size >= U32 || start >= U32;
        if (big) {
            const end64 = new Uint8Array(56 + 20);
            const view = new DataView(end64.buffer);
            const at64 = this.at;
            put32(view, 0, END64);
            put64(view, 4, 44);
            put16(view, 12, (MADE_ON << 8) | 45);
            put16(view, 14, 45);
            put32(view, 16, 0);
            put32(view, 20, 0);
            put64(view, 24, count);
            put64(view, 32, count);
            put64(view, 40, size);
            put64(view, 48, start);
            put32(view, 56, LOCATOR64);
            put32(view, 60, 0);
            put64(view, 64, at64);
            put32(view, 72, 1);
            await this.put(end64);
        }
        const end = new Uint8Array(22);
        const view = new DataView(end.buffer);
        put32(view, 0, END);
        put16(view, 8, Math.min(count, 0xffff));
        put16(view, 10, Math.min(count, 0xffff));
        put32(view, 12, Math.min(size, U32));
        put32(view, 16, Math.min(start, U32));
        await this.put(end);
        return this.at;
    }
}

// -- reading ------------------------------------------------------------------

export class BundleError extends Error {
    constructor(message) {
        super(message);
        this.name = "BundleError";
    }
}

async function bytesOf(blob, start, end) {
    return new Uint8Array(await blob.slice(start, end).arrayBuffer());
}

/**
 * The central directory of `blob` (a File or Blob), read from its end: one
 * slice for the end records and one for the directory, never the members.
 * -> [{name, method, crc, csize, usize, offset, external, flags, link}].
 */
export async function readCentral(blob) {
    const size = blob.size;
    const tailStart = Math.max(0, size - (22 + 0xffff + 20));
    const tail = await bytesOf(blob, tailStart, size);
    const view = new DataView(tail.buffer);
    let end = -1;
    for (let i = tail.length - 22; i >= 0; i -= 1) {
        if (view.getUint32(i, true) === END && i + 22 + view.getUint16(i + 20, true) <= tail.length) {
            end = i;
            break;
        }
    }
    if (end < 0) throw new BundleError("This is not a Plexora project file.");
    let count = view.getUint16(end + 10, true);
    let cdSize = view.getUint32(end + 12, true);
    let cdStart = view.getUint32(end + 16, true);
    if (end >= 20 && view.getUint32(end - 20, true) === LOCATOR64) {
        const at = get64(view, end - 20 + 8);
        const record = await bytesOf(blob, at, at + 56);
        const z = new DataView(record.buffer);
        if (record.length < 56 || z.getUint32(0, true) !== END64) throw new BundleError("This Plexora project file is damaged.");
        count = get64(z, 32);
        cdSize = get64(z, 40);
        cdStart = get64(z, 48);
    }
    if (cdStart + cdSize > size) throw new BundleError("This Plexora project file is incomplete.");
    const cd = await bytesOf(blob, cdStart, cdStart + cdSize);
    const dv = new DataView(cd.buffer);
    const decoder = new TextDecoder("utf-8");
    const entries = [];
    let at = 0;
    for (let n = 0; n < count; n += 1) {
        if (at + 46 > cd.length || dv.getUint32(at, true) !== CENTRAL) throw new BundleError("This Plexora project file is damaged.");
        const flags = dv.getUint16(at + 8, true);
        const nameLength = dv.getUint16(at + 28, true);
        const extraLength = dv.getUint16(at + 30, true);
        const commentLength = dv.getUint16(at + 32, true);
        const entry = {
            name: decoder.decode(cd.subarray(at + 46, at + 46 + nameLength)),
            flags,
            method: dv.getUint16(at + 10, true),
            crc: dv.getUint32(at + 16, true),
            csize: dv.getUint32(at + 20, true),
            usize: dv.getUint32(at + 24, true),
            external: dv.getUint32(at + 38, true),
            offset: dv.getUint32(at + 42, true),
        };
        let extra = at + 46 + nameLength;
        const extraEnd = extra + extraLength;
        while (extra + 4 <= extraEnd) {
            const tag = dv.getUint16(extra, true);
            const length = dv.getUint16(extra + 2, true);
            if (tag === 1) {
                let field = extra + 4;
                for (const key of ["usize", "csize", "offset"]) {
                    if (entry[key] !== U32) continue;
                    if (field + 8 > extra + 4 + length) break;
                    entry[key] = get64(dv, field);
                    field += 8;
                }
            }
            extra += 4 + length;
        }
        entry.link = ((entry.external >>> 16) & 0o170000) === 0o120000;
        entries.push(entry);
        at = extraEnd + commentLength;
    }
    return entries;
}

/** A member's stored bytes, inflated where it was deflated: a ReadableStream. */
export async function memberStream(blob, entry) {
    const head = await bytesOf(blob, entry.offset, entry.offset + 30);
    const view = new DataView(head.buffer);
    if (head.length < 30 || view.getUint32(0, true) !== LOCAL) throw new BundleError("This Plexora project file is damaged.");
    const start = entry.offset + 30 + view.getUint16(26, true) + view.getUint16(28, true);
    const raw = blob.slice(start, start + entry.csize).stream();
    if (entry.method === STORED) return raw;
    if (entry.method === DEFLATED) return raw.pipeThrough(new DecompressionStream("deflate-raw"));
    throw new BundleError(`${entry.name} is compressed in a way Plexora does not read.`);
}

/**
 * A member's bytes to `write(chunk)` as they inflate, checked: never past
 * its size (a member that inflates beyond what it says is refused as it
 * goes), then its size, CRC and -- when given -- sha256.
 */
export async function pumpMember(blob, entry, write, { sha256 = null, onBytes, cancelled } = {}) {
    if (entry.method !== STORED && entry.usize > RATIO_FLOOR
        && (entry.csize <= 0 || entry.usize / entry.csize > MAX_RATIO)) {
        throw new BundleError("This Plexora project file inflates far beyond its size, so it is not unpacked.");
    }
    const reader = (await memberStream(blob, entry)).getReader();
    const digest = sha256 ? new Sha256() : null;
    let crc = 0;
    let count = 0;
    try {
        for (;;) {
            if (cancelled && cancelled()) throw new Cancelled();
            const { value, done } = await reader.read();
            if (done) break;
            count += value.length;
            if (count > entry.usize) throw new BundleError(`This Plexora project file is damaged (${entry.name} is longer than it says).`);
            crc = crc32(value, crc);
            if (digest) digest.update(value);
            await write(value);
            if (onBytes) onBytes(value.length);
        }
    } catch (error) {
        reader.cancel(error).catch(() => {});
        throw error;
    }
    if (count !== entry.usize || crc !== entry.crc || (digest && digest.hex() !== sha256)) {
        throw new BundleError(`This Plexora project file is damaged (${entry.name} does not match).`);
    }
    return { size: count, crc };
}

/** A small member's bytes, whole (the manifest). */
export async function readMember(blob, entry, limit = 64 << 20) {
    if (entry.usize > limit) throw new BundleError("This Plexora project file's manifest is too large.");
    const parts = [];
    await pumpMember(blob, entry, (chunk) => { parts.push(chunk); });
    const out = new Uint8Array(entry.usize);
    let at = 0;
    for (const part of parts) {
        out.set(part, at);
        at += part.length;
    }
    return out;
}

// -- the worker ---------------------------------------------------------------
//
// Below is the browser's: folders, the Save dialog's file and this site's own
// storage. The codec above is all a Node test needs.

const inWorker = typeof self !== "undefined" && typeof self.postMessage === "function"
    && typeof WorkerGlobalScope !== "undefined" && self instanceof WorkerGlobalScope;

//: The answers of one job, and how it stops.
let job = null;
let state = null;

function post(message, transfer) {
    self.postMessage({ job, ...message }, transfer || []);
}

//: Why a write stopped, as the dialog says it: a full quota is a sentence.
async function sentence(error) {
    if (error && error.name === "QuotaExceededError") {
        let allows = "";
        try {
            const { quota } = await navigator.storage.estimate();
            if (quota) allows = `: this site may keep ${(quota / 1e9).toFixed(1)} GB here`;
        } catch (ignored) { /* no estimate */ }
        return `There is no room left for it${allows}.`;
    }
    if (error && error.name === "NotReadableError") {
        return "A file changed or went away while it was being read. Try again.";
    }
    return String((error && error.message) || error).slice(0, 500);
}

async function opfsDir(parts, create = false) {
    let dir = await navigator.storage.getDirectory();
    for (const part of parts) dir = await dir.getDirectoryHandle(part, { create });
    return dir;
}

async function storeRoot(spec) {
    if (!spec) throw new Error("That project store cannot be reached from here.");
    if (spec.handle) return spec.handle;
    if (spec.opfs) return opfsDir(spec.opfs);
    throw new Error("That project store cannot be reached from here.");
}

//: A store-relative path that stays inside it.
function safeRel(rel) {
    if (typeof rel !== "string" || !rel || rel.includes("\\") || rel.includes("\0") || rel.startsWith("/")) return false;
    return rel.split("/").every((part) => part && part !== "." && part !== "..");
}

async function fileAt(root, rel) {
    const parts = rel.split("/");
    let dir = root;
    try {
        for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part);
        return await (await dir.getFileHandle(parts[parts.length - 1])).getFile();
    } catch (error) {
        if (error && (error.name === "NotFoundError" || error.name === "TypeError" || error.name === "TypeMismatchError")) return null;
        throw error;
    }
}

//: A member's source as a Blob, or null when it is gone.
async function sourceBlob(source, stores) {
    if (source.blob) return source.blob;
    if (source.opfs) {
        try {
            const dir = await opfsDir(source.opfs.slice(0, -1));
            return await (await dir.getFileHandle(source.opfs[source.opfs.length - 1])).getFile();
        } catch (error) {
            return null;
        }
    }
    if (source.store) {
        if (!safeRel(source.rel)) return null;
        return fileAt(await storeRoot(stores[source.store]), source.rel);
    }
    return null;
}

//: Where the bundle is written: positional writes, then closed or dropped.
async function openOutput(out) {
    if (out.handle) {
        const handle = out.handle;
        const writable = await handle.createWritable();
        let chain = Promise.resolve();
        return {
            sink: { write: (bytes, at) => (chain = chain.then(() => writable.write({ type: "write", position: at, data: bytes }))) },
            async close() { await chain; await writable.close(); },
            async abort() {
                try { await writable.abort(); } catch (error) { /* closed */ }
                // The Save dialog made the file; an empty one is not left behind.
                try { if (typeof handle.remove === "function") await handle.remove(); } catch (error) { /* kept */ }
            },
            file: () => handle.getFile(),
        };
    }
    const dir = await opfsDir(out.opfs, true);
    const handle = await dir.getFileHandle(out.name, { create: true });
    const access = await handle.createSyncAccessHandle();
    access.truncate(0);
    let open = true;
    return {
        sink: {
            write(bytes, at) {
                let done = 0;
                while (done < bytes.length) {
                    const n = access.write(bytes.subarray(done), { at: at + done });
                    if (n <= 0) throw new Error("This browser stopped writing the file.");
                    done += n;
                }
            },
        },
        async close() { access.flush(); access.close(); open = false; },
        async abort() {
            if (open) try { access.close(); } catch (error) { /* closed */ }
            open = false;
            try { await dir.removeEntry(out.name); } catch (error) { /* gone */ }
        },
        file: () => handle.getFile(),
    };
}

function reporter(total, stage) {
    let done = 0;
    let last = 0;
    return (n) => {
        done += n;
        const now = Date.now();
        if (now - last >= 200 || done >= total) {
            last = now;
            post({ progress: { done, total, stage } });
        }
    };
}

async function exportBundle({ members, reserve, out, stores }) {
    const output = await openOutput(out);
    state.abort = () => output.abort();
    const writer = new ZipWriter(output.sink);
    await writer.reserve(MANIFEST, reserve);
    const total = members.reduce((sum, member) => sum + Number(member.size || 0), 0);
    const advance = reporter(total, "pack");
    const sums = {};
    const leftOut = [];
    for (const member of members) {
        if (state.cancelled) throw new Cancelled();
        const source = member.source || {};
        const blob = source.gone ? null : await sourceBlob(source, stores || {});
        if (!blob) {
            leftOut.push({ path: member.bundle_path, why: source.gone || "gone before it was packed" });
            continue;
        }
        const method = await chooseMethod(member.bundle_path, blob);
        const written = await writer.add(member.bundle_path, blob.stream(), {
            size: blob.size, method, mtime: member.mtime || blob.lastModified,
            onBytes: advance, cancelled: () => state.cancelled,
        });
        sums[member.bundle_path] = { sha256: written.sha256, size: written.size };
    }
    state.writer = writer;
    state.output = output;
    post({ packed: { sums, left_out: leftOut } });
}

//: The finished manifest in its room, the directory, the file closed and
//: its central directory read back against what was written.
async function finishBundle({ manifest }) {
    const { writer, output } = state;
    if (!writer) throw new Error("Nothing was packed.");
    post({ progress: { done: 0, total: 0, stage: "verify" } });
    await writer.fill(MANIFEST, encoder.encode(manifest));
    await writer.finish();
    await output.close();
    state.abort = null;
    const file = await output.file();
    const listed = new Map((await readCentral(file)).map((entry) => [entry.name, entry]));
    for (const entry of writer.entries) {
        const there = listed.get(entry.name);
        if (!there || there.crc !== entry.crc || there.usize !== entry.usize || there.csize !== entry.csize) {
            throw new BundleError(`The project file did not write ${entry.name} correctly.`);
        }
    }
    if (file.size !== writer.at) throw new BundleError("The project file was not written whole.");
    post({ done: { size: file.size, file } });
}

async function listBundle({ file }) {
    const entries = await readCentral(file);
    const found = entries.find((entry) => entry.name === MANIFEST);
    let manifest = null;
    if (found) {
        try {
            manifest = JSON.parse(new TextDecoder().decode(await readMember(file, found)));
        } catch (error) {
            throw new BundleError("This Plexora project file is damaged.");
        }
    }
    post({ done: { entries: entries.map(({ name, method, csize, usize, link }) => ({ name, method, csize, usize, link })),
                   manifest } });
}

//: A new file at `rel` under `root`, refusing one already there; the folders
//: it had to make are noted in `made`, so a failure can take them back.
async function createAt(root, rel, made) {
    const parts = rel.split("/");
    let dir = root;
    let path = "";
    for (const part of parts.slice(0, -1)) {
        path = path ? `${path}/${part}` : part;
        try {
            dir = await dir.getDirectoryHandle(part);
        } catch (error) {
            if (!error || error.name !== "NotFoundError") throw error;
            dir = await dir.getDirectoryHandle(part, { create: true });
            made.push(path);
        }
    }
    const name = parts[parts.length - 1];
    const there = await dir.getFileHandle(name).then(() => true, () => false);
    if (there) throw new BundleError(`${rel} is in the way.`);
    const handle = await dir.getFileHandle(name, { create: true });
    if (typeof handle.createWritable === "function") {
        const writable = await handle.createWritable();
        return {
            write: (bytes) => writable.write(bytes),
            close: () => writable.close(),
            abort: async () => {
                try { await writable.abort(); } catch (error) { /* closed */ }
                try { await dir.removeEntry(name); } catch (error) { /* gone */ }
            },
        };
    }
    // Safari's own storage: no writable streams, a sync access handle.
    const access = await handle.createSyncAccessHandle();
    let at = 0;
    return {
        write(bytes) {
            let done = 0;
            while (done < bytes.length) done += access.write(bytes.subarray(done), { at: at + done });
            at += bytes.length;
        },
        close: async () => { access.flush(); access.close(); },
        abort: async () => {
            try { access.close(); } catch (error) { /* closed */ }
            try { await dir.removeEntry(name); } catch (error) { /* gone */ }
        },
    };
}

//: Files removed from their stores, then the folders a job made, deepest
//: first, where they are empty.
async function removeAll(paths, stores, made = []) {
    let removed = 0;
    for (const { store, rel } of paths) {
        if (!safeRel(rel)) continue;
        try {
            const root = await storeRoot(stores[store]);
            const parts = rel.split("/");
            let dir = root;
            for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part);
            await dir.removeEntry(parts[parts.length - 1]);
            removed += 1;
        } catch (error) {
            // Already gone.
        }
    }
    for (const { store, rel } of [...made].sort((a, b) => b.rel.length - a.rel.length)) {
        try {
            const root = await storeRoot(stores[store]);
            const parts = rel.split("/");
            let dir = root;
            for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part);
            const inside = (await dir.getDirectoryHandle(parts[parts.length - 1])).keys();
            if ((await inside.next()).done) await dir.removeEntry(parts[parts.length - 1]);
        } catch (error) {
            // Not empty, or gone.
        }
    }
    return removed;
}

async function extractBundle({ file, members, stores }) {
    const listed = new Map((await readCentral(file)).map((entry) => [entry.name, entry]));
    const total = members.reduce((sum, member) => sum + Number(member.size || 0), 0);
    const advance = reporter(total, "unpack");
    const placed = [];
    const made = [];
    state.abort = () => removeAll(placed, stores, made);
    try {
        for (const member of members) {
            if (state.cancelled) throw new Cancelled();
            const entry = listed.get(member.path);
            if (!entry) throw new BundleError("This Plexora project file is incomplete.");
            if (entry.link) throw new BundleError("This Plexora project file holds a link, which Plexora does not unpack.");
            if (entry.usize !== Number(member.size)) throw new BundleError("This Plexora project file is damaged.");
            if (!safeRel(member.rel)) throw new BundleError("This Plexora project file names a place outside its project.");
            const root = await storeRoot(stores[member.store]);
            const dirs = [];
            const target = await createAt(root, member.rel, dirs);
            made.push(...dirs.map((rel) => ({ store: member.store, rel })));
            try {
                await pumpMember(file, entry, (bytes) => target.write(bytes), {
                    sha256: member.sha256 || null, onBytes: advance, cancelled: () => state.cancelled });
                await target.close();
            } catch (error) {
                await target.abort();
                throw error;
            }
            placed.push({ store: member.store, rel: member.rel });
        }
    } catch (error) {
        await removeAll(placed, stores, made);
        state.abort = null;
        throw error;
    }
    state.abort = null;
    post({ done: { placed, made } });
}

if (inWorker) {
    self.onmessage = async ({ data }) => {
        if (data.op === "cancel") {
            if (state) state.cancelled = true;
            // A running op stops at its next chunk and answers then. One
            // waiting between its answers (an export waiting for its
            // manifest), or none, is stopped here, its file taken away.
            if (state && state.busy) return;
            if (state && state.abort) {
                const abort = state.abort;
                state.abort = null;
                try { await abort(); } catch (ignored) { /* best effort */ }
            }
            post({ error: "Stopped.", cancelled: true });
            return;
        }
        if (data.op !== "finish") {
            job = data.job;
            state = { cancelled: false, abort: null, busy: false };
        }
        state.busy = true;
        try {
            if (data.op === "export") {
                await exportBundle(data);
            } else if (data.op === "finish") {
                if (state.cancelled) throw new Cancelled();
                await finishBundle(data);
            } else if (data.op === "list") {
                await listBundle(data);
            } else if (data.op === "extract") {
                await extractBundle(data);
            } else if (data.op === "remove") {
                post({ done: { removed: await removeAll(data.paths || [], data.stores || {}, data.made || []) } });
            }
        } catch (error) {
            const cancelled = error instanceof Cancelled || state.cancelled;
            if (state.abort) {
                const abort = state.abort;
                state.abort = null;
                try { await abort(); } catch (ignored) { /* best effort */ }
            }
            post({ error: cancelled ? "Stopped." : await sentence(error), cancelled });
        } finally {
            state.busy = false;
        }
    };
}
