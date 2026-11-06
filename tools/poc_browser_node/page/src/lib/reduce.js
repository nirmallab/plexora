/**
 * The two ways Plexora shrinks a level, transcribed from Python so a pyramid
 * built here is the pyramid Python builds, value for value:
 *
 *  - sample(): segmentation_pyramid's FILLED mode -- nearest neighbour, level
 *    L's pixel (y, x) is level 0's (y * 2^L, x * 2^L), so every id survives.
 *  - reduce2(): image_pyramid.reduce2 -- a 2x2 mean, an odd last row or column
 *    replicated, rows summed first and then column pairs, integers rounded
 *    half to even in integer arithmetic, floats summed in float64 and cast.
 *    Level k is made from level k-1 (a cascade), never from level 0 again.
 *
 * Both are local to a 1024-aligned block of level 0: a block's rows and
 * columns are even except at the image's own edge, so the cascade over one
 * block is the cascade over the whole level (up to 2^10 = one tile).
 */

/** pyramid_factors: 1, 2, 4, ... until one tile covers the level. */
export function factorsFor(height, width, tile = 1024) {
    const factors = [1];
    for (;;) {
        const f = factors[factors.length - 1];
        if (Math.max(Math.ceil(height / f), Math.ceil(width / f)) <= tile) return factors;
        factors.push(f * 2);
    }
}

/** A typed array of `n` of the same kind as `like`. */
export function sameKind(like, n) {
    return new like.constructor(n);
}

/**
 * Nearest-neighbour sample of a level-0 block at factor f. The block starts
 * at (y0, x0), both multiples of f; the result is the level's pixels whose
 * level-0 position falls inside the block.
 */
export function sample(block, bh, bw, f) {
    const oh = Math.ceil(bh / f);
    const ow = Math.ceil(bw / f);
    const out = sameKind(block, oh * ow);
    for (let i = 0; i < oh; i += 1) {
        const base = i * f * bw;
        const row = i * ow;
        for (let j = 0, x = 0; j < ow; j += 1, x += f) out[row + j] = block[base + x];
    }
    return { array: out, height: oh, width: ow };
}

/**
 * image_pyramid.reduce2 over one block (h x w), for the block's own typed
 * array kind. Integer kinds of up to 32 bits sum exactly in float64 (four
 * values below 2^34), so one path serves uint8/16/32 and int8/16/32; floats
 * are summed in the same order as numpy (rows, then column pairs) and cast.
 */
export function reduce2(block, h, w) {
    const oh = (h + 1) >> 1;
    const ow = (w + 1) >> 1;
    const out = sameKind(block, oh * ow);
    const isFloat = block instanceof Float32Array || block instanceof Float64Array;
    // One row of row-pair sums at a time (numpy's `rows`), in float64.
    const rows = new Float64Array(w);
    for (let i = 0; i < oh; i += 1) {
        const a = 2 * i * w;
        const b = 2 * i + 1 < h ? a + w : a; // odd last row: counted twice
        for (let x = 0; x < w; x += 1) rows[x] = block[a + x] + block[b + x];
        const o = i * ow;
        for (let j = 0; j < ow; j += 1) {
            const x = 2 * j;
            const sum = x + 1 < w ? rows[x] + rows[x + 1] : rows[x] * 2;
            if (isFloat) {
                out[o + j] = sum * 0.25;
            } else {
                // sum / 4 rounded half to even: q = floor(sum / 4), r = sum - 4q;
                // up when r is 3, or when r is 2 and q is odd.
                const q = Math.floor(sum / 4);
                const r = sum - 4 * q;
                out[o + j] = r + (q % 2 === 0 ? 0 : 1) > 2 ? q + 1 : q;
            }
        }
    }
    return { array: out, height: oh, width: ow };
}

/**
 * Every derived level's part of one level-0 block: `factors[1..]`, sampled
 * (labels) or cascaded (image). Returns [{level, array, height, width}].
 */
export function derive(block, bh, bw, factors, kind) {
    const out = [];
    let cur = { array: block, height: bh, width: bw };
    for (let L = 1; L < factors.length; L += 1) {
        cur = kind === "labels" ? sample(block, bh, bw, factors[L]) : reduce2(cur.array, cur.height, cur.width);
        out.push({ level: L, ...cur });
    }
    return out;
}
