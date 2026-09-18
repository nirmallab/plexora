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
/**
 * A layer's colour and contrast window as a tile-url query.
 *
 * Built here rather than server-side because it is presentation and it
 * changes while the user drags a slider -- `render` is the per-kind bag the
 * server never acts on. Empty when the layer names no colour, which is what
 * asks for the plain uint16 a channel tile carries.
 */
function styleQuery(render) {
    const colour = String((render || {}).color || "").replace(/^#/, "");
    if (!/^[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(colour)) return "";
    const parts = [`color=${colour}`];
    const [lo, hi] = (render || {}).range || [];
    if (Number.isFinite(lo)) parts.push(`lo=${Math.round(lo)}`);
    if (Number.isFinite(hi)) parts.push(`hi=${Math.round(hi)}`);
    return parts.join("&");
}

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

    //: Where the viewer was looking when a layer rebuild started, held until
    //: the first rebuilt layer lands and puts it back. See rememberView.
    pendingView = null;
    pendingViewTimer = 0;

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
     *
     * What that rebuild costs, and why rememberView is here: emptying the
     * world makes the next add look to OpenSeadragon like a first open, so it
     * fits the whole slide again (see rememberView) -- flipping HD used to
     * throw away wherever the user had panned and zoomed to, which is exactly
     * the region they turned HD on to look at.
     * @param enabled - true for HD (16-bit), false for the fast/default WebP path
     */
    setHdMode(enabled) {
        tileQuality.hd = enabled;
        this.rememberView();
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
     * @function rememberView
     * Hold on to where the viewer is looking, for the first layer that lands
     * afterwards to put back (see restoreView).
     *
     * Called by anything that rebuilds tile layers by removing and re-adding
     * them -- the HD toggle here, main.js's rebuildTileLayers after a routing
     * repair. Such a rebuild empties `world`, and OpenSeadragon fits the whole
     * image whenever an item lands in an empty one: Viewer.processReadyItems
     * does `if (world.getItemCount() === 1 && !preserveViewport)
     * viewport.goHome(true)`. So the pan and zoom have to be carried across by
     * hand.
     *
     * Centre and zoom rather than bounds, because both mean the same thing on
     * either side of the rebuild: every layer is added at the same normalized
     * position, so the viewport coordinate system they are expressed in is
     * rebuilt identically. `true` asks for where the viewport IS, not where an
     * in-flight animation is heading.
     *
     * `preserveViewport` is the option OpenSeadragon offers for this, and it is
     * the wrong tool: it is viewer-wide and re-read on every add, so it would
     * have to be set across asynchronous adds and put back afterwards -- and
     * while set it also suppresses the goHome that frames the image on a
     * genuine first open.
     */
    rememberView() {
        const viewport = this.viewer?.viewport;
        if (!viewport) return;
        this.pendingView = {
            center: viewport.getCenter(true),
            zoom: viewport.getZoom(true),
        };
        // Only fires if no layer ever came back (every add failed): the adds
        // are a resolved promise away, so restoreView normally clears this
        // within the same frame. Without it a long-dead view would sit waiting
        // to be applied to some unrelated channel add minutes later.
        clearTimeout(this.pendingViewTimer);
        this.pendingViewTimer = setTimeout(() => {
            this.pendingView = null;
        }, 5000);
    }

    /**
     * @function restoreView
     * Put back what rememberView held, once a rebuilt layer has landed.
     *
     * Called from the success callback of every layer this class adds, which is
     * the first hook that runs after OpenSeadragon's own goHome -- both happen
     * inside Viewer.processReadyItems, goHome first, so restoring here wins and
     * no frame is ever drawn at the home position. A no-op unless a rebuild
     * asked for it, so an ordinary channel add still frames the image the way
     * it always has.
     */
    restoreView() {
        const view = this.pendingView;
        if (!view) return;
        this.pendingView = null;
        clearTimeout(this.pendingViewTimer);
        const viewport = this.viewer?.viewport;
        if (!viewport) return;
        // Immediately, in both cases: this is undoing something that never
        // should have been visible, not moving the user anywhere. Zoom first
        // and pan second -- zoomTo with no reference point zooms about the
        // current centre, whatever goHome left that as, and panTo then places
        // it exactly.
        viewport.zoomTo(view.zoom, null, true);
        viewport.panTo(view.center, true);
        this.viewer.forceRedraw();
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
                // Which layer this world item belongs to. Read back by
                // LayerStack.anchorIndex, which needs to find the reference
                // image rather than trust a position.
                layerId: PlexoraLayerStack.REFERENCE_LAYER_ID,
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
                this.restoreView();
                this.viewer.raiseEvent("open", e.item);
                this.claimWorldItem(PlexoraLayerStack.REFERENCE_LAYER_ID, e.item);
                this.applyWorldOrder();
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
                layerId: PlexoraLayerStack.REFERENCE_LAYER_ID,
            },
            // On the TiledImage rather than inside the tileSource: OSD reads
            // this one off the addTiledImage options, and the viewer-wide
            // default is `lighter`.
            compositeOperation: "source-over",
            index: 0,
            opacity: 1,
            preload: true,
            success: (e) => {
                this.restoreView();
                // Same reason channel_add raises it: 'open' is what wires up
                // the GL pipeline the label layer still needs, and initGL is
                // safe to run more than once.
                this.viewer.raiseEvent("open", e.item);
                this.claimWorldItem(PlexoraLayerStack.REFERENCE_LAYER_ID, e.item);
                this.applyWorldOrder();
            },
        });
    }

    /**
     * The reference frame of a sample that has no image, as one transparent
     * tiled image.
     *
     * A blank frame could in principle be drawn by putting nothing on screen
     * at all, and that does not work: OpenSeadragon's world has to hold at
     * least one item for `viewportImageBounds` to answer, for
     * `LayerStack.anchorIndex` to find the position registered layers stack
     * against, and for the viewer to have a home rectangle to fit to. So the
     * frame is a real world item every one of whose tiles is transparent --
     * which is also what makes a transcript density raster drawn on top of it
     * land in the right place, at the right zoom, with the scale bar reading
     * correctly.
     *
     * Its own route rather than a channel entry: there is no channel to serve,
     * and a placeholder in `imageData` would put itself in front of every
     * consumer that indexes that list -- which, as `load_brightfield_base`
     * found out the hard way, includes this one.
     *
     * @param src - the tile address, ending in a slash, built by the caller
     *   the way every other tile address is (`main.js`, through plexoraUrl).
     *
     * Idempotent for the same reason `load_brightfield_base` is: a routing
     * repair calls it again and OSD would otherwise stack a second copy.
     */
    load_blank_base(src) {
        if (!src) return;
        const world = this.viewer?.world;
        for (let i = 0; world && i < world.getItemCount(); i += 1) {
            if (world.getItemAt(i)?.source?.tileFormat === RGB_TILE_FORMAT) return;
        }

        const config = this.imageViewer.config;
        const { maxLevel, extraZoomLevels } = config;
        const magnification = 2 ** extraZoomLevels;
        this.viewer.addTiledImage({
            tileSource: {
                height: config.height * magnification,
                width: config.width * magnification,
                maxLevel: extraZoomLevels + maxLevel - 1,
                tileWidth: config.tileWidth,
                tileHeight: config.tileHeight,
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
                src: src,
                srcQuery: "",
                layerId: PlexoraLayerStack.REFERENCE_LAYER_ID,
            },
            // As the brightfield base: `lighter` is the viewer-wide default
            // and it is wrong for a ground layer. Transparent either way here,
            // but the frame is what registered layers composite against.
            compositeOperation: "source-over",
            index: 0,
            opacity: 1,
            preload: true,
            success: (e) => {
                this.restoreView();
                // Same reason channel_add raises it: 'open' is what wires up
                // the GL pipeline the label layer still needs.
                this.viewer.raiseEvent("open", e.item);
                this.claimWorldItem(PlexoraLayerStack.REFERENCE_LAYER_ID, e.item);
                this.applyWorldOrder();
            },
        });
    }

    /**
     * Draw one registered layer as its own tiled image, where its transform
     * says.
     *
     * THE MISSING LINK between the layer model and the picture. Everything
     * either side of this already existed and was tested -- `LayerSpec` stores
     * a layer with an affine, `/generated/layer/...` serves its tiles,
     * `placementFor` turns the affine into OSD's five controls, the Layer
     * Manager draws a card for it -- and nothing joined them, so a registered
     * image layer was a row in a panel and never a pixel on screen.
     *
     * A world item of its own rather than a channel, because a registered
     * layer is NOT in `config.imageData` and therefore not in the GL colorize
     * pass, which is keyed on those indices. Its tiles arrive already coloured
     * (see layer_sources.parse_style) and the browser composites them, exactly
     * as it does for a brightfield base -- `tileFormat: 24` is what tells
     * imageViewer.js to leave them alone.
     *
     * @param spec.layerId - the stack id, used to tag the world item so
     *   `applyWorldOrder` can place it and `anchorIndex` can avoid it
     * @param spec.src - tile address ending in a slash, per-channel
     * @param spec.style - `{color, lo, hi}` as the query string, or ""
     * @param spec.geometry - the LAYER's own {width, height, maxLevel,
     *   tileWidth, tileHeight} plus the `transform` into reference pixels
     * @returns `{remove, setStyle, setVisible, placement}`, or null when the
     *   transform is one OSD cannot express -- the card says so rather than
     *   this drawing something almost right.
     */
    addTiledLayer(spec) {
        const { layerId, src, geometry = {} } = spec || {};
        const config = this.imageViewer.config;
        if (!layerId || !src) return null;

        // Reference WIDTH in full-resolution pixels on both sides. Not the
        // magnified width: `extraZoomLevels` scales the reference item's tile
        // grid, not the world, and folding it in here would put every
        // registered layer at the wrong size by a power of two.
        const placement = PlexoraLayerStack.placementFor(
            geometry.transform || null, geometry.width, config.width);
        if (!placement) return null;

        let item = null;
        let style = spec.style || "";
        // `addTiledImage` is asynchronous, so a hide (or a style change) can
        // land while an add is still in flight. Without this the item arrives
        // after the removal and stays on screen forever -- visible as a layer
        // that will not switch off, which is exactly the bug a toggle exists
        // to avoid.
        let shown = true;
        const self = this;

        function add() {
            shown = true;
            self.viewer.addTiledImage({
                tileSource: {
                    // The LAYER's grid, not the reference's. A second slide is
                    // its own image with its own pyramid; borrowing the
                    // reference's dimensions is what would make it draw a
                    // corner of itself stretched over the whole frame.
                    height: geometry.height,
                    width: geometry.width,
                    maxLevel: Math.max(0, (geometry.maxLevel || 1) - 1),
                    tileWidth: geometry.tileWidth || 1024,
                    tileHeight: geometry.tileHeight || 1024,
                    // Zero, and not the viewer's: extra zoom levels are a
                    // property of how the REFERENCE image's tiles are being
                    // magnified, and a layer with its own pyramid has its own
                    // answer, which is "none".
                    extraZoomLevels: 0,
                    toMagnifiedBounds: toMagnifiedBounds,
                    toTileBoundary: toTileBoundary,
                    getImagePixel: getImagePixel,
                    toTileLevels: toTileLevels,
                    toIdealTile: toIdealTile,
                    toRealTile: toRealTile,
                    getTileUrl: getTileUrl,
                    getTileKey: getTileKey,
                    tileFormat: spec.tileFormat ?? RGB_TILE_FORMAT,
                    srcIdx: 0,
                    src: src,
                    srcQuery: style,
                    layerId: layerId,
                },
                compositeOperation: spec.compositeOperation || "lighter",
                x: placement.x,
                y: placement.y,
                width: placement.width,
                degrees: placement.degrees,
                flipped: placement.flipped,
                opacity: 1,
                success: (e) => {
                    if (!shown) {
                        // Hidden while this was loading. Drop it on arrival
                        // rather than keeping it invisible: an item nobody
                        // asked for still fetches every tile in view.
                        self.viewer.world.removeItem(e.item);
                        return;
                    }
                    item = e.item;
                    self.claimWorldItem(layerId, e.item);
                    self.applyWorldOrder();
                },
            });
        }

        function drop() {
            shown = false;
            if (!item) return;
            self.releaseWorldItem(item);
            self.viewer.world.removeItem(item);
            item = null;
        }

        add();

        return {
            placement,
            remove: drop,
            /**
             * A new colour or window. Remove and re-add rather than
             * invalidate, for the reason the HD toggle gives: invalidating in
             * place leaves the old canvases on screen until each tile happens
             * to be refetched.
             */
            setStyle(next) {
                style = next || "";
                if (!shown) return;
                drop();
                add();
            },
            /**
             * Hiding REMOVES the world item, rather than setting opacity to
             * zero. That is the whole performance argument for registered
             * layers: an item at opacity 0 still requests, decodes and draws
             * every tile in view. A hidden layer must cost nothing, or a scene
             * with six registered layers is unusable however few are shown.
             */
            setVisible(visible) {
                if (visible && !shown) add();
                else if (!visible) drop();
            },
        };
    }

    /**
     * Draw every registered image layer the config lists, and stop drawing the
     * ones it no longer does.
     *
     * Called after the viewer opens and again whenever layers are adopted
     * mid-session. Idempotent: a layer already on screen is left alone, so
     * adopting a newly imported layer does not restack or reload the others.
     *
     * Deliberately draws NO points layer. Density is a modality's picture, not
     * core's -- the transcripts plugin adds its own through
     * `ctx.layers.addTiled`, which is this same primitive handed out. Core
     * drawing it here would mean core deciding what a points layer looks like,
     * which is the boundary the transcripts plugin exists to keep.
     */
    syncLayerImages(layers) {
        const list = Array.isArray(layers) ? layers : [];
        this.tiledLayers = this.tiledLayers || new Map();
        const wanted = new Set();

        for (const spec of list) {
            if (!spec?.id || spec.kind !== "image") continue;
            if (spec.id === PlexoraLayerStack.REFERENCE_LAYER_ID) continue;
            // A layer whose tiles are still being built has a card and a
            // progress line, and nothing to fetch: adding it now would be a
            // wall of 404s and an empty rectangle.
            if (spec.status && spec.status !== "ready") continue;
            // Which channel of this layer is drawn. `render.channelIndex` is
            // the Layers panel's choice; zero is the answer for the single-
            // channel layers that are most of them.
            const channels = spec.channels || [];
            const channel = channels[
                Math.min((spec.render || {}).channelIndex ?? 0,
                         Math.max(0, channels.length - 1))];
            const src = channel?.src;
            if (!src) continue;
            wanted.add(spec.id);
            if (this.tiledLayers.has(spec.id)) continue;

            const handle = this.addTiledLayer({
                layerId: spec.id,
                src: src,
                style: styleQuery(spec.render),
                // An H&E or a second brightfield slide replaces what is under
                // it; a fluorescence layer adds to it. The same distinction
                // `load_brightfield_base` makes, for the same reason: `lighter`
                // on a white background washes the whole slide out.
                compositeOperation: (spec.render || {}).rgb ? "source-over" : "lighter",
                geometry: {
                    width: spec.width,
                    height: spec.height,
                    maxLevel: spec.maxLevel,
                    tileWidth: spec.tileWidth,
                    tileHeight: spec.tileHeight,
                    transform: spec.transform || null,
                },
            });
            if (handle) this.tiledLayers.set(spec.id, handle);
        }

        for (const [id, handle] of [...this.tiledLayers]) {
            if (wanted.has(id)) continue;
            handle.remove();
            this.tiledLayers.delete(id);
        }
        return this.tiledLayers;
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
                    const item = this.viewer.world.getItemAt(i);
                    this.releaseWorldItem(item);
                    this.viewer.world.removeItem(item);
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

    /** This viewer's layer stack, or null in a harness that has no viewer. */
    get layerStack() {
        return this.imageViewer?.layerStack || null;
    }

    /**
     * Say which layer a freshly added world item belongs to.
     *
     * The stack is the model and OSD's world is one of its surfaces; something
     * has to connect the two, and the only place that knows is whoever called
     * addTiledImage. Registering the layer here rather than from the config
     * means a channel added before the layer list arrives still stacks
     * correctly -- the layer is created on first claim.
     */
    claimWorldItem(layerId, item) {
        const stack = this.layerStack;
        if (!stack || !item) return;
        const layer = stack.get(layerId) || stack.register(layerId, {
            kind: layerId === PlexoraLayerStack.MASK_LAYER_ID ? "labels" : "image",
        });
        if (!layer.items.includes(item)) layer.items.push(item);
    }

    releaseWorldItem(item) {
        const stack = this.layerStack;
        if (!stack || !item) return;
        for (const layer of stack.layers()) {
            const at = layer.items.indexOf(item);
            if (at >= 0) layer.items.splice(at, 1);
        }
    }

    /**
     * Push the stack's order onto the world.
     *
     * Replaces raiseLabelLayer, which said the one thing it could say -- "the
     * mask goes on top" -- because there was nowhere to say anything else. The
     * result for a project with one image and a mask is identical; what is new
     * is that a second image layer now has somewhere to sit.
     */
    applyWorldOrder() {
        this.layerStack?.applyWorldOrder();
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
                    layerId: PlexoraLayerStack.MASK_LAYER_ID,
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
                    self.restoreView();
                    // The GL layer initializes on 'open', so raise it here.
                    self.viewer.raiseEvent("open", e.item);
                    self.claimWorldItem(PlexoraLayerStack.MASK_LAYER_ID, e.item);
                    self.applyWorldOrder();
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
