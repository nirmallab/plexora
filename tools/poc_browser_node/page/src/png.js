/**
 * The two PNG shapes a Plexora node sends -- 16-bit grey (the HD channel tile)
 * and RGBA8 (the label tile) -- decoded to their raw sample bytes, in the
 * byte order the PNG stores them (big-endian for 16-bit). For identity checks
 * only: the viewer path decodes with the product's own worker.
 *
 * Handles every PNG filter type, not only the 0 that fast_png.py writes, so
 * a check can never pass because a filter was skipped.
 */
export async function decodePng(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (view.getUint32(0) !== 0x89504e47) throw new Error("not a PNG");
    let pos = 8;
    let width = 0, height = 0, depth = 0, ctype = 0, interlace = 0;
    const idat = [];
    while (pos + 8 <= bytes.length) {
        const length = view.getUint32(pos);
        const type = String.fromCharCode(...bytes.subarray(pos + 4, pos + 8));
        if (type === "IHDR") {
            width = view.getUint32(pos + 8);
            height = view.getUint32(pos + 12);
            depth = bytes[pos + 16];
            ctype = bytes[pos + 17];
            interlace = bytes[pos + 20];
        } else if (type === "IDAT") {
            idat.push(bytes.subarray(pos + 8, pos + 8 + length));
        } else if (type === "IEND") {
            break;
        }
        pos += 12 + length;
    }
    if (interlace) throw new Error("interlaced PNG");
    const channels = { 0: 1, 2: 3, 4: 2, 6: 4 }[ctype];
    if (!channels) throw new Error(`PNG colour type ${ctype}`);
    const bpp = Math.max(1, (channels * depth) >> 3);
    const stride = (width * channels * depth + 7) >> 3;
    const stream = new Blob(idat).stream().pipeThrough(new DecompressionStream("deflate"));
    const inflated = new Uint8Array(await new Response(stream).arrayBuffer());
    const out = new Uint8Array(height * stride);
    let prev = new Uint8Array(stride);
    for (let row = 0; row < height; row += 1) {
        const src = row * (stride + 1);
        const filter = inflated[src];
        const line = inflated.subarray(src + 1, src + 1 + stride);
        const cur = out.subarray(row * stride, (row + 1) * stride);
        for (let i = 0; i < stride; i += 1) {
            const a = i >= bpp ? cur[i - bpp] : 0;
            const b = prev[i];
            const c = i >= bpp ? prev[i - bpp] : 0;
            let v = line[i];
            if (filter === 1) v += a;
            else if (filter === 2) v += b;
            else if (filter === 3) v += (a + b) >> 1;
            else if (filter === 4) {
                const p = a + b - c;
                const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
                v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
            } else if (filter !== 0) throw new Error(`PNG filter ${filter}`);
            cur[i] = v & 0xff;
        }
        prev = cur;
    }
    return { width, height, depth, ctype, data: out };
}

/** SHA-256 of a byte array, hex. */
export async function sha256(bytes) {
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Index of the first differing byte, or -1. */
export function firstDifference(a, b) {
    if (a.length !== b.length) return Math.min(a.length, b.length);
    for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return i;
    return -1;
}
