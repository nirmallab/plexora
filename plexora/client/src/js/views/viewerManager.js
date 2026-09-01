import "regenerator-runtime/runtime.js";

// Global HD-toggle state, read by getTileUrl() below. A plain module-scope
// flag (not a ViewerManager instance field) because getTileUrl runs with
// `this` bound to the tileSource object it's attached to, not the
// ViewerManager -- this is the one place in the tile-loading path that
// needs the toggle state and can't reach it via `this`.
export const tileQuality = { hd: false };

//: `tileFormat` for a brightfield/H&E layer: one tiled image carrying all
//: three colour samples, drawn by OpenSeadragon itself. The other two are 16
//: (a quantized channel plane, colorized in WebGL) and 32 (a label mask,
//: rendered into per-layer canvases). Both of those exist because the bytes on
//: the wire are not a picture; these bytes are, so the whole decode-and-shade
//: path is skipped -- see the tileFormat 24 early returns in imageViewer.js.
export const RGB_TILE_FORMAT = 24;

//: The channel key a brightfield image's tiles are served under. The same
//: sentinel `server/utils/brightfield.py` names, and the only way to tell that
//: layer apart from a mask placeholder in `imageData` -- positions there
//: depend on whether the project has a segmentation.
export const RGB_CHANNEL_KEY = "rgb";

/** The tile key an `imageData` entry's address ends in.
 *
 *  Read off `origSrc` when routing has rewritten `src` to point at a data node:
 *  the two addresses end in the same key, but only while `src` is this
 *  server's. See main.js's applyRouting, which sets both.
 */
function keyOf(channel) {
    const address = channel?.origSrc || channel?.src || "";
    return String(address).replace(/\/+$/, "").split("/").pop();
}

/**
 * @function toIdealTile -- full tile dimension in full image pixels
 * @param fullScale - scale factor to full image
 * @param useY - 0 for x and 1 for y
 * @returns Number
 */
function toIdealTile(fullScale, useY) {
    const { _tileWidth, _tileHeight } = this;
    return [_tileWidth, _tileHeight][useY] * fullScale;
}

/**
 * @function toIdealTile -- clipped tile dimension in full image pixels
 * @param fullScale - scale factor to full image
 * @param v - x or y index of tile
 * @param useY - x=0 and y=1
 * @returns Number
 */
function toRealTile(fullScale, v, useY) {
    const shape = [this.width, this.height][useY];
    const tileShape = this.toIdealTile(fullScale, useY);
    return Math.min(shape - v * tileShape, tileShape);
}

/**
 * @function toTileBoundary -- tile start and size in full image pixels
 * @param fullScale - scale factor to full image
 * @param v - x or y index of tile
 * @param useY - x=0 and y=1
 * @typedef {object} Bound
 * @property {number} start - full image pixel start of tile
 * @property {number} size - full image pixel size of tile
 * @returns Bound
 */
function toTileBoundary(fullScale, v, useY) {
    const start = v * this.toIdealTile(fullScale, useY);
    const size = this.toRealTile(fullScale, v, useY);
    return { start, size };
}

/**
 * @function toMagnifiedBounds -- return bounds of magnified tile
 * @param _level - openseadragon tile level
 * @param _x - openseadragon tile x index
 * @param _y - openseadragon tile y index
 * @typedef {object} Bounds
 * @property {Array} x - start and end image x-coordinates
 * @property {Array} y - start and end image y-coordinates
 * @returns Bounds
 */
function toMagnifiedBounds(_level, _x, _y) {
    const tl = this.toTileLevels(_level, _x, _y);
    if (tl.relativeImageScale >= 1) {
        return { x: [0, 1], y: [0, 1] };
    }
    const ownScale = tl.outputFullScale;
    const parentScale = tl.inputFullScale;
    const [x, y] = [tl.outputTile.x, tl.outputTile.y].map((parentOffset, i) => {
        const hd = this.toTileBoundary(ownScale, [_x, _y][i], i);
        const sd = this.toTileBoundary(parentScale, parentOffset, i);
        const start = (hd.start - sd.start) / sd.size;
        const end = start + hd.size / sd.size;
        return [
            [start, end],
            [1 - end, 1 - start],
        ][i];
    });
    return { x, y };
}

/**
 * @function toTileLevels -- measure scaled/non-scaled tile details
 * @param level - openseadragon tile level
 * @param x - openseadragon tile x index
 * @param y - openseadragon tile y index
 * @typedef {object} TileLevels
 * @property {number} inputFullScale - full scale of source tile
 * @property {number} outputFullScale - full scale of renedered tile
 * @property {number} relativeImageScale - scale relative to image pixels
 * @property {object} inputTile - level, x, and y of source tile
 * @property {object} outputTile - level, x, and y of rendered tile
 * @returns TileLevels
 */
function toTileLevels(level, x, y) {
    const { extraZoomLevels } = this;
    const flipped = this.maxLevel - level;
    const relativeLevel = flipped - extraZoomLevels;
    const sourceLevel = Math.max(relativeLevel, 0);
    const extraZoom = sourceLevel - relativeLevel;
    const inputTile = {
        x: Math.floor(x / 2 ** extraZoom),
        y: Math.floor(y / 2 ** extraZoom),
        level: sourceLevel,
    };
    const outputTile = {
        ...inputTile,
        level: level - extraZoom,
    };
    return {
        inputFullScale: 2 ** (flipped + extraZoom),
        relativeImageScale: 2 ** relativeLevel,
        outputFullScale: 2 ** flipped,
        inputTile,
        outputTile,
    };
}

/**
 * @function getTileUrl -- return url for tile
 * @param level - openseadragon tile level
 * @param x - openseadragon tile x index
 * @param y - openseadragon tile y index
 * @returns string
 */
function getTileUrl(level, x, y) {
    const s = this.toTileLevels(level, x, y).inputTile;
    // Segmentation (tileFormat 32) always ignores the HD toggle -- it has
    // its own fixed encoding regardless -- so its URL (and OSD's URL-keyed
    // tile cache) never churns when HD is flipped.
    const hd = this.tileFormat !== 32 && tileQuality.hd ? "q=hd" : "";
    // Empty for a tile this server serves, which is every tile of an ordinary
    // project. A tile fetched straight from a data node carries its token and
    // the project's tile grid -- see services/resourceRouting.js. Joined with
    // `&` rather than concatenated blindly: the HD flag used to be written as
    // a bare "?q=hd", which is a second "?" the moment anything else is there.
    const query = [hd, this.srcQuery || ""].filter(Boolean).join("&");
    const suffix = query ? `?${query}` : "";
    return `${this.src}${s.level}/${s.x}_${s.y}.png${suffix}`;
}

/**
 * @function getTileKey -- return string key for tile
 * @param level - openseadragon tile level
 * @param x - openseadragon tile x index
 * @param y - openseadragon tile y index
 * @returns string
 */
function getTileKey(level, x, y) {
    const { srcIdx, tileFormat } = this;
    const s = this.toTileLevels(level, x, y).inputTile;
    return `${tileFormat}-${srcIdx}-${s.level}-${s.x}-${s.y}`;
}

/**
 * @function getImagePixel -- return image pixel for screen position
 * @param tiledImage - openseadragon tiled image
 * @param position - screen position
 * @returns array
 */
function getImagePixel(tiledImage, position) {
    const tileScale = 2 ** this.extraZoomLevels;
    const frac = tiledImage.viewport.pointFromPixel(position);
    const zoomed = tiledImage.viewportToImageCoordinates(frac);
    return [zoomed.x, zoomed.y].map((v) => v / tileScale);
}

/**
 * @class ViewerManager
 */
export class ViewerManager {
    colorConnector = {};
    rangeConnector = {};

    show_sel = true;
    sel_outlines = false;
    labelLayerRequested = false;

    /**
     * Constructs a ColorManager instance before delegating initialization.
     *
     * @param imageViewer - ImageViewer instance
     * @param channelList - ChannelList instance
     */
    constructor(imageViewer, channelList) {
        this.viewer = imageViewer.viewer;
        this.imageViewer = imageViewer;
        this.channelList = channelList;
    }

    /**
     * @function init
     * Setups up the color manager.
     */
    init() {
        // Load label image
        this.load_label_image();
    }

    /**
     * @function setHdMode
     * Toggle the global HD (full-precision 16-bit) tile quality on/off and
     * force OpenSeadragon to re-fetch currently-loaded tiles at the new
     * quality. Segmentation tiles are unaffected (see getTileUrl). Dispatches
     * a DOM event so the (unbundled) sidebar can remap its per-channel range
     * sliders between byte units ([0,255], default mode) and raw 16-bit
     * units (HD mode) -- a plain window event because viewerSidebar.js is a
     * raw <script>, not an ES module, so it can't import tileQuality here
     * directly.
     *
     * Just flipping the flag and clearing OpenSeadragon's shared tile cache
     * isn't enough: each TiledImage also keeps its own per-address Tile
     * cache (tilesMatrix), and invalidating that in place left stale/
     * recycled tile canvases on screen without OpenSeadragon ever
     * re-invoking tile-drawing to repaint them (visible as leftover static
     * from whatever was on screen at the moment of the toggle). Removing
     * and re-adding every active channel sidesteps that entirely by making
     * OpenSeadragon build a brand new TiledImage for each one -- the same
     * thing that already happens (and reliably works) when a channel gets
     * toggled off and back on.
     * @param enabled - true for HD (16-bit), false for the fast/default WebP path
     */
    setHdMode(enabled) {
        tileQuality.hd = enabled;
        Object.keys(this.channelList.currentChannels).map(Number).forEach((srcIdx) => {
            this.channel_remove(srcIdx);
            this.channel_add(srcIdx);
        });
        window.dispatchEvent(new CustomEvent("plexora:hd-mode-changed", { detail: { enabled } }));
    }

    /**
     * @function isHdMode
     * @returns {boolean} whether the global HD (full-precision 16-bit) tile
     * quality is currently enabled.
     */
    isHdMode() {
        return tileQuality.hd;
    }

    /**
     * @function channel_add
     * Add channel to multi-channel rendering
     * @param srcIdx - integer id of channel to add
     */
    channel_add(srcIdx) {
        // If already exists
        if (srcIdx in this.channelList.currentChannels) {
            return;
        }

        const url = this.imageViewer.config["imageData"][srcIdx]["src"];
        const { maxLevel, extraZoomLevels } = this.imageViewer.config;
        const magnification = 2 ** extraZoomLevels;

        // Define url and suburl
        const group = url.split("/");
        const sub_url = group[group.length - 2];
        const range = this.channelList.rangeConnector[srcIdx];
        const { color } = this.channelList.colorConnector[srcIdx] || {};
        const viewerChannel = {
            url: url,
            sub_url: sub_url,
            color: color || d3.color("white"),
            range: range || this.imageViewer.numericData.bitRange,
        };
        this.channelList.currentChannels[srcIdx] = viewerChannel;

        this.viewer.addTiledImage({
            tileSource: {
                height: this.imageViewer.config.height * magnification,
                width: this.imageViewer.config.width * magnification,
                maxLevel: extraZoomLevels + maxLevel - 1,
                compositeOperation: "lighter",
                tileWidth: this.imageViewer.config.tileWidth,
                tileHeight: this.imageViewer.config.tileHeight,
                toMagnifiedBounds: toMagnifiedBounds,
                extraZoomLevels: extraZoomLevels,
                toTileBoundary: toTileBoundary,
                getImagePixel: getImagePixel,
                toTileLevels: toTileLevels,
                toIdealTile: toIdealTile,
                toRealTile: toRealTile,
                getTileUrl: getTileUrl,
                getTileKey: getTileKey,
                tileFormat: 16,
                srcIdx: srcIdx,
                src: url,
                srcQuery: this.imageViewer.config["imageData"][srcIdx]["srcQuery"] || "",
            },
            // index: 0,
            opacity: 1,
            preload: true,
            // "open" is what wires up the GL colorize pipeline (see imageViewer.js's
            // initGL, bound to the "open" handler) -- previously only raised from
            // load_label_image()'s success callback, which never runs for a
            // datasource with no segmentation (noLabel short-circuits it). That left
            // image channels fetching real tile bytes successfully but never
            // getting GL-rendered: tiles loaded, nothing drew. initGL is safe to
            // run more than once, so raising it here too (redundant when a label
            // image is also present) is harmless.
            success: (e) => {
                this.viewer.raiseEvent("open", e.item);
                this.raiseLabelLayer();
            },
        });

    }

    /**
     * @function load_brightfield_base
     * Add the single true-colour layer an H&E / brightfield project draws.
     *
     * The counterpart of `channel_add` for an image that has no channels to
     * add. Three differences, and each is the whole reason this is not just
     * `channel_add` with a flag:
     *
     * - `source-over`, not the viewer's `lighter`. Additive blending is what
     *   makes several fluorescence channels stack into one picture; applied to
     *   a colour image with a white background it washes the whole slide out.
     * - index 0, so the label layer (and anything else added later) stays on
     *   top of it. A brightfield project still has masks, ROIs and centroids.
     * - `tileFormat` 24, which is what tells imageViewer.js to leave these
     *   tiles alone.
     *
     * Idempotent, because `rebuildTileLayers` calls it again after a routing
     * repair and OSD would otherwise stack a second copy behind the first.
     */
    load_brightfield_base() {
        // Found by its tile key, not by position. `imageData[0]` is the "Area"
        // mask placeholder whenever the project has a segmentation, so taking
        // the first entry drew the mask as the slide -- a blank viewer, with
        // every tile fetched successfully.
        const entry = (this.imageViewer.config["imageData"] || []).find(
            (channel) => keyOf(channel) === RGB_CHANNEL_KEY);
        if (!entry?.src) return;
        const world = this.viewer?.world;
        for (let i = 0; world && i < world.getItemCount(); i += 1) {
            if (world.getItemAt(i)?.source?.tileFormat === RGB_TILE_FORMAT) return;
        }

        const url = entry.src;
        const { maxLevel, extraZoomLevels } = this.imageViewer.config;
        const magnification = 2 ** extraZoomLevels;
        this.viewer.addTiledImage({
            tileSource: {
                height: this.imageViewer.config.height * magnification,
                width: this.imageViewer.config.width * magnification,
                maxLevel: extraZoomLevels + maxLevel - 1,
                tileWidth: this.imageViewer.config.tileWidth,
                tileHeight: this.imageViewer.config.tileHeight,
                toMagnifiedBounds: toMagnifiedBounds,
                extraZoomLevels: extraZoomLevels,
                toTileBoundary: toTileBoundary,
                getImagePixel: getImagePixel,
                toTileLevels: toTileLevels,
                toIdealTile: toIdealTile,
                toRealTile: toRealTile,
                getTileUrl: getTileUrl,
                getTileKey: getTileKey,
                tileFormat: RGB_TILE_FORMAT,
                srcIdx: 0,
                src: url,
                srcQuery: entry["srcQuery"] || "",
            },
            // On the TiledImage rather than inside the tileSource: OSD reads
            // this one off the addTiledImage options, and the viewer-wide
            // default is `lighter`.
            compositeOperation: "source-over",
            index: 0,
            opacity: 1,
            preload: true,
            success: (e) => {
                // Same reason channel_add raises it: 'open' is what wires up
                // the GL pipeline the label layer still needs, and initGL is
                // safe to run more than once.
                this.viewer.raiseEvent("open", e.item);
                this.raiseLabelLayer();
            },
        });
    }

    /**
     * @function channel_remove - remove channel from multichannel rendering
     * @param srcIdx - integer id of channel to remove
     */
    channel_remove(srcIdx) {
        const img_count = this.viewer.world.getItemCount();

        // remove channel
        if (srcIdx in this.channelList.currentChannels) {
            // remove channel - first find it
            for (let i = 0; i < img_count; i = i + 1) {
                const url = this.viewer.world.getItemAt(i).source.src;
                if (url === this.channelList.currentChannels[srcIdx]?.url) {
                    this.viewer.world.removeItem(this.viewer.world.getItemAt(i));
                    delete this.channelList.currentChannels[srcIdx];
                    break;
                }
            }
        }
    }


    /**
     * @function evaluateTF - finds color for value in transfer function
     * @param val - input to transfer function
     * @param tf - colors of transfer function
     * @returns object
     */
    evaluateTF(val, tf) {
        let lerpFactor = Math.round(((val - tf.min) / (tf.max - tf.min)) * (tf.num_bins - 1));

        if (lerpFactor >= tf.num_bins) {
            lerpFactor = tf.num_bins - 1;
        }

        if (lerpFactor < 0) {
            lerpFactor = 0;
        }

        return tf.tf[lerpFactor];
    }

    /**
     * @function force_repaint
     */
    force_repaint() {
        // Refilter, redraw
        // this.viewer.forceRefilter();
        this.viewer.forceRedraw();
    }

    /**
     * Keep the transparent segmentation layer above image channels.
     */
    raiseLabelLayer() {
        const world = this.viewer?.world;
        if (!world) return;
        for (let i = 0; i < world.getItemCount(); i += 1) {
            const item = world.getItemAt(i);
            if (item?.source?.tileFormat == 32) {
                world.setItemIndex(item, world.getItemCount() - 1);
                return;
            }
        }
    }

    /**
     * @function load_label_image
     */
    load_label_image() {
        if (this.labelLayerRequested) {
            return;
        }
        this.labelLayerRequested = true;
        const self = this;

        // Load label image in background if it exists. Gate on config.segmentation
        // (set only when a segmentation file was actually registered), not just
        // imageData[0].src -- imageData[0] is the label/"Area" channel only when
        // segmentation exists; otherwise it's just the first real image channel
        // (e.g. "DNA"), which always has a real src and would be wrongly loaded
        // as a label layer, silently, with no error to trigger the centroid
        // fallback below.
        if (this.imageViewer.config["segmentation"] && this.imageViewer.config["imageData"]?.[0]?.["src"]) {
            let url = this.imageViewer.config["imageData"][0]["src"];
            const { maxLevel, extraZoomLevels } = this.imageViewer.config;
            const magnification = 2 ** extraZoomLevels;
            this.viewer.addTiledImage({
                tileSource: {
                    height: this.imageViewer.config.height * magnification,
                    width: this.imageViewer.config.width * magnification,
                    maxLevel: extraZoomLevels + maxLevel - 1,
                    maxImageCacheCount: 50,
                    compositeOperation: "source-over",
                    tileWidth: this.imageViewer.config.tileWidth,
                    tileHeight: this.imageViewer.config.tileHeight,
                    toMagnifiedBounds: toMagnifiedBounds,
                    extraZoomLevels: extraZoomLevels,
                    toTileBoundary: toTileBoundary,
                    getImagePixel: getImagePixel,
                    toTileLevels: toTileLevels,
                    toIdealTile: toIdealTile,
                    toRealTile: toRealTile,
                    getTileUrl: getTileUrl,
                    getTileKey: getTileKey,
                    tileFormat: 32,
                    srcIdx: 0,
                    src: url,
                    srcQuery: this.imageViewer.config["imageData"][0]["srcQuery"] || "",
                },
                // On the TiledImage, where OSD actually reads it -- the copy
                // inside the tileSource above is inert, and the viewer-wide
                // default is `lighter`. Additive is right over a fluorescence
                // composite, whose ground is black; over a brightfield slide,
                // whose ground is white, adding a coloured outline to
                // near-white tissue saturates to white and the mask disappears
                // at exactly the moment it is switched on.
                compositeOperation:
                    this.imageViewer.config.image_kind === "brightfield"
                        ? "source-over" : "lighter",
                opacity: 1,
                success: (e) => {
                    // The GL layer initializes on 'open', so raise it here.
                    self.viewer.raiseEvent("open", e.item);
                    self.raiseLabelLayer();
                },
                error: () => {
                    this.imageViewer.noLabel = true;
                    if (PlexoraDataset.hasCentroids(this.imageViewer.config)) {
                        this.imageViewer.updateCentroidFallback(true);
                    }
                },
            });
        } else {
            this.imageViewer.noLabel = true;
            // A project with no table -- or one whose coordinate columns are
            // still unidentified -- has no per-cell positions. Falling back to
            // centroids there would round-trip to the server for a manifest
            // that was never going to have any points, delaying load for
            // nothing.
            if (PlexoraDataset.hasCentroids(this.imageViewer.config)) {
                this.imageViewer.updateCentroidFallback(true);
            }
        }
    }
}
