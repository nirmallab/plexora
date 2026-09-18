/**
 * Turning the bytes a tile request returned into a typed array the shader can
 * read: the worker pool, the three decoders, and the `tile-loaded` handler that
 * picks between them.
 *
 * Extracted verbatim from imageViewer.js, where all of this lived as closures in
 * the constructor. The decode paths are unchanged; the two values the handler read
 * off `this` -- renderTileLayers and forceRepaint -- are now named dependencies.
 *
 * Served as a classic script (see base.html) and must load BEFORE imageViewer.js.
 */

/**
 * Round-robin pool of tile-decode workers (see workers/tileDecoder.js).
 *
 * Decoding is per tile per channel, so the work scales with channel count: a
 * CPU profile put it at ~2% of a 7-channel pan but ~60% of a 15-channel one.
 * Spreading it across workers both takes it off the main thread and lets
 * several tiles decode in parallel.
 *
 * Falls back to null from create() if Worker/OffscreenCanvas are unavailable,
 * and callers then decode inline -- the inline path has to exist anyway for
 * the HD and segmentation formats.
 */
class TileDecoderPool {
    static create(url, size) {
        if (typeof Worker === "undefined" || typeof OffscreenCanvas === "undefined") {
            return null;
        }
        try {
            return new TileDecoderPool(url, size);
        } catch (err) {
            console.warn("Tile decode workers unavailable, decoding inline:", err);
            return null;
        }
    }

    constructor(url, size) {
        this.pending = new Map();
        this.nextId = 0;
        this.next = 0;
        this.workers = [];
        for (let i = 0; i < size; i++) {
            const worker = new Worker(url);
            worker.onmessage = (event) => {
                const { id, ok, array, width, height, error, unsupported } = event.data;
                const entry = this.pending.get(id);
                if (!entry) {
                    return;
                }
                this.pending.delete(id);
                if (ok) {
                    entry.resolve({ array, width, height });
                } else {
                    const err = new Error(error);
                    // Distinguishes "this worker cannot handle this input" from
                    // "the worker broke": the caller falls back rather than
                    // treating it as a failure.
                    err.unsupported = !!unsupported;
                    entry.reject(err);
                }
            };
            worker.onerror = (err) => {
                // A worker-level failure orphans everything queued on it, so
                // reject all outstanding work rather than leaving tiles hung on
                // promises OSD is awaiting.
                for (const [id, entry] of this.pending) {
                    entry.reject(err instanceof Error ? err : new Error(String(err && err.message)));
                    this.pending.delete(id);
                }
            };
            this.workers.push(worker);
        }
    }

    /**
     * @param buffer - the raw tile bytes as delivered by the server
     * @param kind - "webp" (default 8-bit path) or "gray16png" (HD path)
     * @returns Promise<{array: Uint8Array, width: number, height: number}>
     */
    decode(buffer, kind) {
        const id = this.nextId++;
        const worker = this.workers[this.next];
        this.next = (this.next + 1) % this.workers.length;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            // `buffer` is deliberately copied, not transferred: it belongs to
            // OpenSeadragon's XHR response and detaching it here would corrupt
            // whatever OSD does with the response afterwards. The decoded
            // result, which is the large one, IS transferred back.
            worker.postMessage({ id, buffer, kind });
        });
    }

    terminate() {
        this.workers.forEach((w) => w.terminate());
        this.workers = [];
        this.pending.clear();
    }
}


/** The already-decoded tile one pyramid level up, or {} when it is not resident. */
const matchTile = (e, { x, y, level }) => {
    const grid = e.tiledImage.tilesMatrix[level];
    return ((grid || {})[x] || {})[y] || {};
};


const decodeLabelTile = (responseArray) => {
    const upng = window.UPNG;
    if (upng) {
        const img = upng.decode(responseArray);
        if (img.ctype == 6 && img.depth == 8) {
            return {
                data: img.data.slice(0, 4 * img.width * img.height),
                width: img.width,
                height: img.height,
            };
        }
    }

    const pngBuffer = new Buffer(responseArray);
    const pngArray = PNG.sync.read(pngBuffer);
    return {
        data: pngArray.data.slice(0, 4 * pngArray.width * pngArray.height),
        width: pngArray.width,
        height: pngArray.height,
    };
};


// Same algorithm as the worker, for when workers aren't available.
const decodeWebpInline = async (responseArray) => {
    const blob = new Blob([responseArray], { type: "image/webp" });
    const bitmap = await createImageBitmap(blob, { premultiplyAlpha: "none", colorSpaceConversion: "none" });
    const { width, height } = bitmap; // capture before close() -- closing zeroes these
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0);
    const rgba = ctx.getImageData(0, 0, width, height).data;
    bitmap.close();
    const array = new Uint8Array(width * height);
    for (let i = 0; i < array.length; i++) {
        array[i] = rgba[i * 4];
    }
    return { array, width, height };
};


/**
 * @param decoderPool      - a TileDecoderPool, or null where Worker/OffscreenCanvas
 *                           are unavailable and everything decodes inline
 * @param renderTileLayers - ImageViewer.renderTileLayers, bound to the viewer
 * @param forceRepaint     - ImageViewer.forceRepaint, bound
 */
function createTileLoadedHandler({ decoderPool, renderTileLayers, forceRepaint }) {
    const handleTileLoaded = async (e) => {
        const { source } = e.tiledImage;
        const { tileFormat } = source;
        // Nothing to decode by hand: OSD turned the WebP into an image
        // already, and the drawer will blit it. Everything below unpacks
        // bytes into a typed array for the shader, which this tile never
        // reaches (see tileDrawingCustom).
        if (tileFormat === RGB_TILE_FORMAT) return;
        try {
            const responseArray = e.tileRequest?.response;
            if (tileFormat == 32) {
                e.tile._isLabel = true;
                if (!e.tile?._array && responseArray) {
                    const decoded = decodeLabelTile(responseArray);
                    e.tile._array = decoded.data;
                    e.tile._format = "u32";
                    // Kept so a layer turned back on later can be rendered
                    // into this tile without re-deriving the shape from the
                    // canvas it no longer has.
                    e.tile._labelWidth = decoded.width;
                    e.tile._labelHeight = decoded.height;
                    renderTileLayers(e.tile);
                }
                if (e.tile?._layerContexts) {
                    return;
                }
            }
            // Trigger loading of image
            const tileArgs = [e.tile.level, e.tile.x, e.tile.y];
            const tl = source.toTileLevels(...tileArgs);
            if (tl.relativeImageScale < 1) {
                const tile = matchTile(e, tl.outputTile);
                if (tile?._array && tile?._format) {
                    e.tile._format = tile._format;
                    e.tile._array = tile._array;
                }
                if (e.tile?._array) {
                    return;
                }
            }
            else if (tileFormat == 32) {
                return;
            }
            else if (responseArray) {
                const sig = new Uint8Array(responseArray, 0, 4);
                const isWebp = sig[0] === 0x52 && sig[1] === 0x49 && sig[2] === 0x46 && sig[3] === 0x46; // "RIFF"
                if (isWebp) {
                    // Fast/default tile path: quantized 8-bit, single value
                    // per pixel. Decoded via createImageBitmap (not
                    // UPNG.js, which can't parse WebP) -- confirmed safe
                    // for this opaque/single-channel case by an in-browser
                    // spike; segmentation tiles never take this path (see
                    // decodeLabelTile) because the same decode approach
                    // was found to corrupt RGB wherever alpha=0.
                    //
                    // Preferably in a worker: the canvas readback and the
                    // per-pixel RGBA->R strip cost ~60% of a 15-channel pan
                    // when run on the main thread. The inline branch is the
                    // fallback when workers or OffscreenCanvas are absent.
                    let decoded;
                    try {
                        decoded = decoderPool
                            ? await decoderPool.decode(responseArray, "webp")
                            : await decodeWebpInline(responseArray);
                    } catch (workerErr) {
                        // A worker failure must never cost us the tile --
                        // OSD is awaiting this handler, and a rejection here
                        // would leave the tile permanently blank.
                        console.warn("Worker tile decode failed, falling back inline:", workerErr);
                        decoded = await decodeWebpInline(responseArray);
                    }
                    e.tile._array = decoded.array;
                    e.tile._format = "u8";
                } else {
                    // HD (16-bit grayscale PNG) and segmentation tiles.
                    //
                    // The HD path goes to the worker, which inflates with the
                    // browser's native DecompressionStream rather than
                    // UPNG.js/pako. A CPU profile of a 7-channel HD pan put
                    // 81% of ALL time in pako's JS inflate. Segmentation
                    // tiles stay on UPNG here: they are RGBA8 rather than
                    // gray16, and there is only one label layer so the cost
                    // is not multiplied by the channel count.
                    let decoded = null;
                    if (decoderPool && tileFormat != 32) {
                        try {
                            decoded = await decoderPool.decode(responseArray, "gray16png");
                        } catch (workerErr) {
                            if (!workerErr.unsupported) {
                                console.warn("Worker HD decode failed, falling back to UPNG:", workerErr);
                            }
                        }
                    }
                    if (decoded) {
                        e.tile._array = decoded.array;
                        e.tile._format = "u16";
                    } else {
                        const img = window.UPNG.decode(responseArray);
                        if (img.ctype == 0 && img.depth == 16) {
                            e.tile._array = img.data.slice(0, 2 * img.width * img.height);
                            e.tile._format = "u16";
                        } else if (img.ctype == 6 && img.depth == 8) {
                            e.tile._array = img.data.slice(0, 4 * img.width * img.height);
                            e.tile._format = "u32";
                        }
                    }
                }
            }
        } catch (err) {
            console.log("Load Error, Refreshing", err, e.tile.getUrl());
            forceRepaint();
        }
    };

    return handleTileLoaded;
}


if (typeof window !== "undefined") {
    window.PlexoraTileDecode = {
        TileDecoderPool, matchTile, decodeLabelTile, decodeWebpInline, createTileLoadedHandler,
    };
}
if (typeof globalThis !== "undefined" && !globalThis.PlexoraTileDecode) {
    globalThis.PlexoraTileDecode = {
        TileDecoderPool, matchTile, decodeLabelTile, decodeWebpInline, createTileLoadedHandler,
    };
}
