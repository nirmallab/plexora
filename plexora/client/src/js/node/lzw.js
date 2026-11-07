/**
 * TIFF LZW (compression 5), decoded as libtiff decodes it: codes MSB-first,
 * 9 to 12 bits wide, the width growing one code early (TIFF 6.0 section 13),
 * the table cleared by code 256 and ended by 257.
 *
 * geotiff.js's own decoder keeps a 4093-entry table: a strip whose encoder
 * fills the table to 4094 entries before its clear code (tifffile's, through
 * imagecodecs, does) walks off its end and grows its output until the array
 * length runs out. This one keeps the full 4096 and writes into one typed
 * array, sized from the tile when the caller knows it.
 *
 *   lzwDecode(bytes, expected?) -> Uint8Array
 */

const CLEAR = 256;
const END = 257;
const MAX_CODES = 4096;

export function lzwDecode(input, expected = 0) {
    const src = input instanceof Uint8Array ? input : new Uint8Array(input);
    const prefix = new Int16Array(MAX_CODES);
    const suffix = new Uint8Array(MAX_CODES);
    const lengths = new Uint16Array(MAX_CODES);
    for (let i = 0; i < 256; i += 1) {
        prefix[i] = -1;
        suffix[i] = i;
        lengths[i] = 1;
    }
    let out = new Uint8Array(Math.max(expected, src.length * 2, 1024));
    let o = 0;
    const room = (n) => {
        if (o + n <= out.length) return;
        const grown = new Uint8Array(Math.max(out.length * 2, o + n));
        grown.set(out.subarray(0, o));
        out = grown;
    };
    //: The string `code` stands for, written at `o` (the table keeps each
    //: entry's length, so it is written back to front with no reversal).
    const write = (code) => {
        const length = lengths[code];
        room(length);
        let c = code;
        for (let k = o + length - 1; k >= o; k -= 1) {
            out[k] = suffix[c];
            c = prefix[c];
        }
        o += length;
        return length;
    };

    let next = 258;
    let width = 9;
    let old = -1;
    let bits = 0;
    let held = 0;
    let at = 0;
    for (;;) {
        while (held < width) {
            if (at >= src.length) return out.subarray(0, o); // no end code: what there is
            bits = ((bits << 8) | src[at]) & 0xffffff;
            at += 1;
            held += 8;
        }
        const code = (bits >>> (held - width)) & ((1 << width) - 1);
        held -= width;
        if (code === END) break;
        if (code === CLEAR) {
            next = 258;
            width = 9;
            old = -1;
            continue;
        }
        if (old === -1) {
            if (code > 255) throw new Error(`LZW: code ${code} right after a clear`);
            room(1);
            out[o] = code;
            o += 1;
            old = code;
            continue;
        }
        if (code < next) {
            const start = o;
            write(code);
            if (next < MAX_CODES) {
                prefix[next] = old;
                suffix[next] = out[start];
                lengths[next] = lengths[old] + 1;
                next += 1;
            }
        } else if (code === next && next < MAX_CODES) {
            // The string being defined: the last one and its own first byte.
            const start = o;
            write(old);
            room(1);
            out[o] = out[start];
            o += 1;
            prefix[next] = old;
            suffix[next] = out[start];
            lengths[next] = lengths[old] + 1;
            next += 1;
        } else {
            throw new Error(`LZW: code ${code} past the table (${next})`);
        }
        old = code;
        if (next >= (1 << width) - 1 && width < 12) width += 1;
    }
    return out.subarray(0, o);
}
