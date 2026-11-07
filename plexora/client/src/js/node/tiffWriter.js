/**
 * The file `segmentation_pyramid.write_tiled_pyramid` writes, written by the
 * page: a little-endian BigTIFF with one main IFD per channel at full
 * resolution, each holding its reduced levels as SubIFDs (NewSubfileType 1),
 * square deflated tiles, `minisblack`, and the OME-XML Python made as the
 * first IFD's ImageDescription. tifffile reads it as the same series with the
 * same levels (tests/test_tiff_writer.py), and so does node/tiffReader.js.
 *
 * Tiles are written first, wherever the builder puts them (node/pyramid.js);
 * the directories go after them, in one piece, and the header's pointer to
 * the first is written last -- a file cut short has no first IFD and opens
 * as nothing rather than as half an image.
 *
 *   header(firstIfd)            -> the 16 bytes at offset 0
 *   directories({start, ...})   -> {bytes, first}: every IFD, laid out at `start`
 *   DTYPES[name]                -> {bits, format, Array}
 */

export const HEADER_BYTES = 16;

export const DTYPES = {
    uint8: { bits: 8, format: 1, Array: Uint8Array },
    uint16: { bits: 16, format: 1, Array: Uint16Array },
    uint32: { bits: 32, format: 1, Array: Uint32Array },
    int8: { bits: 8, format: 2, Array: Int8Array },
    int16: { bits: 16, format: 2, Array: Int16Array },
    int32: { bits: 32, format: 2, Array: Int32Array },
    float32: { bits: 32, format: 3, Array: Float32Array },
    float64: { bits: 64, format: 3, Array: Float64Array },
};

const ASCII = 2;
const SHORT = 3;
const LONG = 4;
const LONG8 = 16;
const IFD8 = 18;
const SIZE = { [ASCII]: 1, [SHORT]: 2, [LONG]: 4, [LONG8]: 8, [IFD8]: 8 };

const align8 = (n) => n + ((8 - (n % 8)) % 8);

/** The BigTIFF header, pointing at the first IFD. */
export function header(firstIfd) {
    const bytes = new Uint8Array(HEADER_BYTES);
    const view = new DataView(bytes.buffer);
    bytes[0] = 0x49; // "II": little-endian
    bytes[1] = 0x49;
    view.setUint16(2, 43, true); // BigTIFF
    view.setUint16(4, 8, true); // offsets are 8 bytes
    view.setUint16(6, 0, true);
    view.setBigUint64(8, BigInt(firstIfd), true);
    return bytes;
}

/** One IFD's entries, in tag order: [tag, type, values (numbers or bytes)]. */
function entriesFor({ level, width, height, tile, dtype, description, offsets, counts, subifds }) {
    const entries = [
        [254, LONG, [level ? 1 : 0]],
        [256, LONG, [width]],
        [257, LONG, [height]],
        [258, SHORT, [dtype.bits]],
        [259, SHORT, [8]], // deflate (zlib)
        [262, SHORT, [1]], // minisblack
    ];
    if (description) entries.push([270, ASCII, description]);
    entries.push(
        [277, SHORT, [1]],
        [284, SHORT, [1]],
        [322, LONG, [tile]],
        [323, LONG, [tile]],
        [324, LONG8, offsets],
        [325, LONG, counts],
    );
    if (subifds) entries.push([330, IFD8, subifds]);
    entries.push([339, SHORT, [dtype.format]]);
    return entries;
}

/** Bytes an IFD takes: its entries and the values too large to sit in one. */
function sizeOf(entries) {
    let data = 0;
    for (const [, type, values] of entries) {
        const bytes = values.length * SIZE[type];
        if (bytes > 8) data += align8(bytes);
    }
    return { head: 8 + entries.length * 20 + 8, data };
}

function put(view, at, type, values) {
    for (let i = 0; i < values.length; i += 1) {
        const value = values[i];
        const where = at + i * SIZE[type];
        if (type === ASCII) view.setUint8(where, value);
        else if (type === SHORT) view.setUint16(where, value, true);
        else if (type === LONG) view.setUint32(where, value, true);
        else view.setBigUint64(where, BigInt(value), true);
    }
}

/**
 * Every IFD of the pyramid, laid out from file offset `start` (rounded up to
 * 8): channel 0's main IFD, its SubIFDs, channel 1's, and so on; main IFDs
 * chained in channel order (the order tifffile and OME's TiffData count).
 *
 * `tiles[level][channel]` is that level's tiles of that channel in row-major
 * order, each [offset, byteCount]; `description` is the OME-XML, written
 * UTF-8 and NUL-terminated as tifffile writes it.
 */
export function directories({ start, width, height, factors, tile, channels, dtype: name, description, tiles }) {
    const dtype = DTYPES[name];
    if (!dtype) throw new Error(`no TIFF sample type for ${name}`);
    const text = description ? new TextEncoder().encode(`${description}\0`) : null;
    const levels = factors.map((f) => ({ width: Math.ceil(width / f), height: Math.ceil(height / f) }));

    const plan = [];
    let cursor = align8(start);
    for (let c = 0; c < channels; c += 1) {
        for (let level = 0; level < levels.length; level += 1) {
            const list = tiles[level][c];
            const spec = {
                level, tile, dtype, ...levels[level],
                description: level === 0 && c === 0 ? text : null,
                offsets: list.map((t) => t[0]),
                counts: list.map((t) => t[1]),
                subifds: level === 0 && levels.length > 1 ? new Array(levels.length - 1).fill(0) : null,
            };
            const size = sizeOf(entriesFor(spec));
            plan.push({ c, level, spec, at: cursor, size });
            cursor += align8(size.head) + size.data;
        }
    }
    // Now that every IFD has a place: each main IFD's SubIFDs, and the chain.
    const mains = plan.filter((p) => p.level === 0);
    for (const main of mains) {
        if (main.spec.subifds) {
            main.spec.subifds = plan.filter((p) => p.c === main.c && p.level > 0).map((p) => p.at);
        }
    }

    const base = align8(start);
    const bytes = new Uint8Array(cursor - base);
    const view = new DataView(bytes.buffer);
    for (const item of plan) {
        const entries = entriesFor(item.spec);
        let at = item.at - base;
        let data = at + align8(item.size.head);
        view.setBigUint64(at, BigInt(entries.length), true);
        at += 8;
        for (const [tag, type, values] of entries) {
            view.setUint16(at, tag, true);
            view.setUint16(at + 2, type, true);
            view.setBigUint64(at + 4, BigInt(values.length), true);
            if (values.length * SIZE[type] <= 8) {
                put(view, at + 12, type, values);
            } else {
                view.setBigUint64(at + 12, BigInt(base + data), true);
                put(view, data, type, values);
                data += align8(values.length * SIZE[type]);
            }
            at += 20;
        }
        const next = item.level === 0 ? mains[mains.indexOf(item) + 1] : null;
        view.setBigUint64(at, BigInt(next ? next.at : 0), true);
    }
    return { bytes, first: mains[0].at };
}
