/**
 * The arithmetic of a whole-slide tile, as brightfield.py does it, for the
 * browser node (node/slideReader.js, services/browserSlides.js): the native
 * read behind a tile of a dyadic level (`_Level.read_rgb`), its level-0
 * origin (`_OpenSlideSource.read`, Python's `round()`), and RGBA over white.
 * Pure functions, no I/O: tests/test_browser_slide_tiles.py runs them in Node
 * against Python's own arithmetic.
 */
/** Python's `round()`: halves to even (`_OpenSlideSource.read`'s origin). */
export function roundHalfEven(value) {
    const floor = Math.floor(value);
    const diff = value - floor;
    if (diff > 0.5) return floor + 1;
    if (diff < 0.5) return floor;
    return floor % 2 === 0 ? floor : floor + 1;
}

/**
 * RGBA over white, as `brightfield._OpenSlideSource.read` composites it:
 * `(rgb * alpha + 255 * (255 - alpha)) // 255` per sample. An unscanned
 * area of an MRXS arrives fully transparent and becomes slide-white, not
 * black.
 */
export function overWhite(rgba, pixels) {
    const out = new Uint8Array(pixels * 3);
    for (let i = 0, j = 0, k = 0; i < pixels; i += 1, j += 4, k += 3) {
        const a = rgba[j + 3];
        if (a === 255) {
            out[k] = rgba[j];
            out[k + 1] = rgba[j + 1];
            out[k + 2] = rgba[j + 2];
        } else {
            const white = 255 * (255 - a);
            out[k] = Math.floor((rgba[j] * a + white) / 255);
            out[k + 1] = Math.floor((rgba[j + 1] * a + white) / 255);
            out[k + 2] = Math.floor((rgba[j + 2] * a + white) / 255);
        }
    }
    return out;
}

/**
 * The native read behind tile (tx, ty) of one served level, as
 * `brightfield._Level.read_rgb` computes it from the same doubles
 * (`brightfield.page_tile_layout`): {level, x, y, w, h} with x, y in level 0,
 * or null where Python resamples the rectangle (it is not the tile's size),
 * or the tile is past the edge.
 */
export function tileWindow(level, tx, ty, tw, th) {
    const y0 = ty * th;
    const x0 = tx * tw;
    const y1 = Math.min(level.height, y0 + th);
    const x1 = Math.min(level.width, x0 + tw);
    if (y1 <= y0 || x1 <= x0) return null;
    const sy0 = Math.min(level.sourceHeight, Math.floor(y0 * level.scaleY));
    const sy1 = Math.min(level.sourceHeight, Math.max(sy0 + 1, Math.ceil(y1 * level.scaleY)));
    const sx0 = Math.min(level.sourceWidth, Math.floor(x0 * level.scaleX));
    const sx1 = Math.min(level.sourceWidth, Math.max(sx0 + 1, Math.ceil(x1 * level.scaleX)));
    if (sy1 - sy0 !== y1 - y0 || sx1 - sx0 !== x1 - x0) return null;
    return { level: level.native, x: roundHalfEven(sx0 * level.downsample),
             y: roundHalfEven(sy0 * level.downsample), w: x1 - x0, h: y1 - y0 };
}
