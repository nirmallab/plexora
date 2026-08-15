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

self.onmessage = async (event) => {
    const { id, buffer } = event.data;
    try {
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
        // Transfer the output (1 MB for a 1024x1024 tile) rather than copying.
        self.postMessage({ id, ok: true, array, width, height }, [array.buffer]);
    } catch (error) {
        self.postMessage({ id, ok: false, error: String(error) });
    }
};
