/**
 * Blosc and LZ4 chunks decoded in plain JavaScript, for node/zarrReader.js.
 *
 * Why not numcodecs.js alone: its Emscripten build makes functions from
 * strings (embind's `new Function`), which the website's Content Security
 * Policy refuses (no 'unsafe-eval'), so on the website no Blosc or LZ4 chunk
 * could be read in the page -- the most common OME-Zarr compressors (zarr's
 * and bioformats2raw's default is Blosc with LZ4). This decodes what such
 * stores hold: c-blosc 1.x frames, memcpyed or compressed with LZ4 (and
 * LZ4HC), Zstandard or zlib inside, unshuffled or byte-shuffled; and
 * numcodecs' LZ4 codec (a 4-byte size, then an LZ4 block). Anything else --
 * BloscLZ, Snappy, bit-shuffle, a frame that does not add up -- answers
 * null, and the caller falls back to numcodecs.js (which runs wherever eval
 * is allowed: the desktop app, Node).
 *
 *   bloscDecode(bytes, {zstd(bytes, size), zlib(bytes)}) -> Promise<Uint8Array | null>
 *   lz4Codec(bytes)                                     -> Uint8Array | null
 *   lz4Block(src, dst)                                  -> bytes written (throws on bad data)
 */

const HEADER = 16;
const MAX_SPLITS = 16;
const MIN_BUFFERSIZE = 128;
const FLAG_SHUFFLE = 0x1;
const FLAG_MEMCPYED = 0x2;
const FLAG_BITSHUFFLE = 0x4;
const FLAG_DONT_SPLIT = 0x10;
const LZ4 = 1;
const ZLIB = 3;
const ZSTD = 4;

/** An LZ4 block (no frame) into `dst`; -> the bytes written. */
export function lz4Block(src, dst) {
    let ip = 0;
    let op = 0;
    const end = src.length;
    while (ip < end) {
        const token = src[ip++];
        let literals = token >> 4;
        if (literals === 15) {
            let b;
            do { b = src[ip++]; literals += b; } while (b === 255 && ip < end);
        }
        if (op + literals > dst.length || ip + literals > end) throw new Error("lz4: literals past the end");
        dst.set(src.subarray(ip, ip + literals), op);
        ip += literals;
        op += literals;
        if (ip >= end) break;
        const offset = src[ip] | (src[ip + 1] << 8);
        ip += 2;
        let length = token & 15;
        if (length === 15) {
            let b;
            do { b = src[ip++]; length += b; } while (b === 255 && ip < end);
        }
        length += 4;
        let ref = op - offset;
        if (!offset || ref < 0 || op + length > dst.length) throw new Error("lz4: a bad match");
        if (offset >= length) {
            dst.copyWithin(op, ref, ref + length);
            op += length;
        } else {
            // Overlapping: byte by byte, as the format means it.
            for (let k = 0; k < length; k += 1) dst[op++] = dst[ref++];
        }
    }
    return op;
}

/** numcodecs' LZ4 codec: the decoded size (4 bytes, little-endian), then one block. */
export function lz4Codec(bytes) {
    if (!bytes || bytes.length < 4) return null;
    const size = bytes[0] | (bytes[1] << 8) | (bytes[2] << 16) | (bytes[3] * 2 ** 24);
    try {
        const out = new Uint8Array(size);
        return lz4Block(bytes.subarray(4), out) === size ? out : null;
    } catch (error) {
        return null;
    }
}

const u32 = (b, at) => (b[at] | (b[at + 1] << 8) | (b[at + 2] << 16)) + b[at + 3] * 2 ** 24;

/** Byte-unshuffle one block of `typesize`-byte items (a trailing part is as it is). */
function unshuffle(src, dst, typesize) {
    const items = Math.floor(src.length / typesize);
    for (let j = 0; j < typesize; j += 1) {
        const base = j * items;
        for (let i = 0; i < items; i += 1) dst[i * typesize + j] = src[base + i];
    }
    const tail = items * typesize;
    if (tail < src.length) dst.set(src.subarray(tail), tail);
}

/**
 * A c-blosc 1.x frame, or null when it is not one this reads. `inner`:
 * `zstd(bytes, size)` and `zlib(bytes)`, each -> a Promise of the decoded bytes.
 */
export async function bloscDecode(bytes, inner = {}) {
    if (!bytes || bytes.length < HEADER) return null;
    const flags = bytes[2];
    const typesize = bytes[3] || 1;
    const nbytes = u32(bytes, 4);
    const blocksize = u32(bytes, 8);
    const cbytes = u32(bytes, 12);
    if (cbytes > bytes.length || bytes[0] < 1 || bytes[0] > 3) return null;
    const out = new Uint8Array(nbytes);
    if (flags & FLAG_MEMCPYED) {
        if (bytes.length < HEADER + nbytes) return null;
        out.set(bytes.subarray(HEADER, HEADER + nbytes));
        return out;
    }
    if (flags & FLAG_BITSHUFFLE) return null;
    const codec = flags >> 5;
    if (codec !== LZ4 && codec !== ZSTD && codec !== ZLIB) return null;
    if ((codec === ZSTD && !inner.zstd) || (codec === ZLIB && !inner.zlib)) return null;
    if (!blocksize) return nbytes ? null : out;
    const shuffle = (flags & FLAG_SHUFFLE) && typesize > 1;
    const dontSplit = Boolean(flags & FLAG_DONT_SPLIT);
    const blocks = Math.ceil(nbytes / blocksize);
    try {
        for (let b = 0; b < blocks; b += 1) {
            const size = Math.min(blocksize, nbytes - b * blocksize);
            const leftover = size < blocksize;
            let at = u32(bytes, HEADER + 4 * b);
            const splits = !dontSplit && !leftover && typesize <= MAX_SPLITS && size / typesize >= MIN_BUFFERSIZE
                ? typesize : 1;
            const part = size / splits;
            if (!Number.isInteger(part)) return null;
            const block = shuffle ? new Uint8Array(size) : out.subarray(b * blocksize, b * blocksize + size);
            for (let s = 0; s < splits; s += 1) {
                const length = u32(bytes, at);
                at += 4;
                const src = bytes.subarray(at, at + length);
                at += length;
                const dst = block.subarray(s * part, (s + 1) * part);
                if (length === part) {
                    dst.set(src);
                } else if (codec === LZ4) {
                    if (lz4Block(src, dst) !== part) return null;
                } else {
                    const got = codec === ZSTD ? await inner.zstd(src, part) : await inner.zlib(src);
                    if (!got || got.length !== part) return null;
                    dst.set(got);
                }
            }
            if (shuffle) unshuffle(block, out.subarray(b * blocksize, b * blocksize + size), typesize);
        }
    } catch (error) {
        return null;
    }
    return out;
}
