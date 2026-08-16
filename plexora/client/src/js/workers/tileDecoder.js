/**
 * Tile decode worker.
 *
 * Turns one compressed WebP channel tile into the single-channel Uint8Array the
 * WebGL colorize pass uploads as an R8UI texture.
 *
 * This runs off the main thread because at high channel counts the decode is
 * the dominant cost of panning. A CPU profile of a 15-channel pan attributed
 * ~60% of all time to this work when it ran inline on the main thread:
 * handleTileLoaded 24.6%, getImageData 23.5%, plus Blob/createImageBitmap/
 * bitmap.close and the garbage collection of the ~3 MB of intermediates each
 * tile allocates. At 7 channels the same work was only ~2%, which is why it
 * initially looked not worth moving -- it scales with the number of tiles
 * streaming in, i.e. with the channel count.
 *
 * createImageBitmap alone was already off-thread; what was not is everything
 * after it -- the canvas draw, the getImageData readback, and the per-pixel
 * loop over 1,048,576 pixels for a 1024x1024 tile.
 *
 * Only the default 8-bit WebP path lives here. The HD 16-bit and segmentation
 * paths decode via UPNG.js, which is loaded as a plain global script and is not
 * reachable from a worker; they also run far less often (HD is a parked-view
 * mode, segmentation is a single layer rather than one per channel).
 */

/**
 * Decode a 16-bit grayscale PNG (the HD channel path) into its raw big-endian
 * sample bytes, using the browser's native inflate instead of UPNG.js/pako.
 *
 * A CPU profile of a 7-channel HD pan put 81% of ALL time in pako's JavaScript
 * inflate -- inflate_fast alone was 71% -- with UPNG's unfiltering another 7%.
 * DecompressionStream does the same job in native code, and doing it here keeps
 * it off the main thread entirely.
 *
 * Only handles what fast_png.py actually emits: non-interlaced, filter type 0
 * on every scanline. Anything else returns null so the caller can fall back to
 * UPNG on the main thread.
 */
async function decodeGray16Png(buffer) {
    const bytes = new Uint8Array(buffer);
    const view = new DataView(buffer);
    // PNG signature
    if (bytes.length < 8 || bytes[0] !== 0x89 || bytes[1] !== 0x50) {
        return null;
    }
    let pos = 8;
    let width = 0, height = 0, bitDepth = 0, colorType = 0, interlace = 0;
    const idat = [];
    while (pos + 8 <= bytes.length) {
        const length = view.getUint32(pos);
        const type = String.fromCharCode(bytes[pos + 4], bytes[pos + 5], bytes[pos + 6], bytes[pos + 7]);
        if (type === "IHDR") {
            width = view.getUint32(pos + 8);
            height = view.getUint32(pos + 12);
            bitDepth = bytes[pos + 16];
            colorType = bytes[pos + 17];
            interlace = bytes[pos + 20];
        } else if (type === "IDAT") {
            idat.push(bytes.subarray(pos + 8, pos + 8 + length));
        } else if (type === "IEND") {
            break;
        }
        pos += 12 + length;
    }
    if (bitDepth !== 16 || colorType !== 0 || interlace !== 0 || !idat.length) {
        return null;
    }

    // IDAT carries a zlib-wrapped stream, so "deflate" (not "deflate-raw").
    const stream = new Blob(idat).stream().pipeThrough(new DecompressionStream("deflate"));
    const inflated = new Uint8Array(await new Response(stream).arrayBuffer());

    const stride = width * 2; // 16-bit grayscale = 2 bytes per pixel
    if (inflated.length < height * (stride + 1)) {
        return null;
    }
    const array = new Uint8Array(height * stride);
    for (let row = 0; row < height; row++) {
        const src = row * (stride + 1);
        if (inflated[src] !== 0) {
            return null; // a real filter we are not equipped to undo
        }
        array.set(inflated.subarray(src + 1, src + 1 + stride), row * stride);
    }
    return { array, width, height };
}

async function decodeWebp(buffer) {
    const blob = new Blob([buffer], { type: "image/webp" });
    // premultiplyAlpha/colorSpaceConversion "none" keep the stored byte
    // values exact -- these tiles are quantized measurements, not photos,
    // so any colour management would corrupt them.
    const bitmap = await createImageBitmap(blob, {
        premultiplyAlpha: "none",
        colorSpaceConversion: "none",
    });
    const { width, height } = bitmap; // capture before close() zeroes them
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0);
    const rgba = ctx.getImageData(0, 0, width, height).data;
    bitmap.close();

    // The server writes a grayscale value into every colour channel, so
    // taking R reconstructs the original single-channel tile.
    const array = new Uint8Array(width * height);
    for (let i = 0; i < array.length; i++) {
        array[i] = rgba[i * 4];
    }
    return { array, width, height };
}

self.onmessage = async (event) => {
    const { id, buffer, kind } = event.data;
    try {
        const decoded = kind === "gray16png"
            ? await decodeGray16Png(buffer)
            : await decodeWebp(buffer);
        if (!decoded) {
            // Unsupported PNG shape -- tell the caller to decode it itself
            // rather than guessing.
            self.postMessage({ id, ok: false, unsupported: true, error: "unsupported PNG variant" });
            return;
        }
        // Transfer the output (1-2 MB for a 1024x1024 tile) rather than copying.
        self.postMessage({ id, ok: true, ...decoded }, [decoded.array.buffer]);
    } catch (error) {
        self.postMessage({ id, ok: false, error: String(error) });
    }
};
