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


//: THE PAIR a registered layer's channel is drawn with -- see
//: `addLayerChannelSet`, which is the only place these mean anything.
//: `destination-out` takes the picture underneath away in proportion to this
//: channel's coverage; `lighter` then adds this channel's colour, which is the
//: same addition the reference image's own channels do.
const COVER_OPERATION = "destination-out";
const PAINT_OPERATION = "lighter";
//: Where each half sits WITHIN its layer. Every cover blit has to land below
//: every paint blit, or a later channel dims an earlier channel's colour
//: instead of the base's. LayerStack.applyWorldOrder sorts on this.
const COVER_Z = 0;
const PAINT_Z = 1;
//: And where a layer's GROUND sits: under both, because it is what the cover
//: blit takes away. Absent unless the layer has been given a background.
const GROUND_Z = -1;

//: How long a quality swap waits for its replacements before handing over
//: anyway. A ceiling, not a policy: the swap is gated on the new items being
//: able to draw, and a tile route that 404s or a data node that stops
//: answering would otherwise leave the HD toggle looking permanently dead.
//: Handing over late costs one gap -- which is the behaviour this replaced;
//: never handing over costs the feature.
const QUALITY_SWAP_TIMEOUT_MS = 20000;


/**
 * Call `done` once this world item can draw what the current view needs.
 *
 * "Can draw" is OpenSeadragon's own `fully-loaded` for a TiledImage, and it
 * means more here than its name suggests: `tile-loaded` is an AWAITING event,
 * so Plexora's decode (tileDecode.js) has run and every tile in view is
 * holding a decoded plane by the time the flag flips. That is exactly the
 * question a swap has to answer -- not "have the bytes arrived" but "would
 * removing the old item now leave a hole".
 *
 * An item that cannot answer is treated as ready at once. The probes under
 * tests/js drive ViewerManager over a hand-rolled world with no loading model
 * at all, and a swap that waited for a flag nothing will ever set would hang
 * the rebuild rather than test it.
 *
 * @returns a function that stops listening, for the caller that gave up
 *   waiting -- a handler left on a TiledImage fires for the rest of its life.
 */
function whenItemCanDraw(item, done) {
    if (typeof item?.getFullyLoaded !== "function"
        || typeof item?.addHandler !== "function") {
        done();
        return () => {};
    }
    if (item.getFullyLoaded()) {
        done();
        return () => {};
    }
    // `fully-loaded-change` fires in both directions -- a pan onto fresh
    // ground takes a loaded item back to false -- so the flag is re-read
    // rather than trusting that any change means ready.
    const stop = () => item.removeHandler?.("fully-loaded-change", onChange);
    const onChange = () => {
        if (!item.getFullyLoaded()) return;
        stop();
        done();
    };
    item.addHandler("fully-loaded-change", onChange);
    return stop;
}


/**
 * A 1x1 opaque tile of one colour, as a data url.
 *
 * What a layer's background is made of. It is stretched over the layer's whole
 * footprint by a tile source with exactly one tile, which is why one pixel is
 * enough -- there is nothing in it to interpolate.
 *
 * A data url rather than a route, and one tile rather than a grid, because
 * both halves of that are free: nothing is fetched, nothing is decoded, and
 * OpenSeadragon keys its tile cache on the url, so every layer asking for the
 * same colour shares one record. Memoised for the same reason: `toDataURL` is
 * a PNG encode, and a slider dragged through a colour picker would otherwise
 * run one per frame.
 */
const GROUND_TILES = new Map();
function groundTileUrl(hex) {
    const held = GROUND_TILES.get(hex);
    if (held) return held;
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const context = canvas.getContext("2d");
    context.fillStyle = hex;
    context.fillRect(0, 0, 1, 1);
    const url = canvas.toDataURL("image/png");
    GROUND_TILES.set(hex, url);
    return url;
}

/**
 * Whether this layer gets the reference image's channel controls.
 *
 * The rule is deliberately broad, because "the same controls" is the whole
 * feature: ANY image layer whose pixels are channel planes, one channel or
 * forty. There is no separate "multiplexed" case -- a single-channel layer
 * gets a slot, a colour and a contrast window like any other.
 *
 * The exception is an rgb layer. Its bytes are already the picture the scanner
 * recorded, so there is no channel to pick, no colour to assign and no window
 * to move; it keeps the one-item path and the Opacity its card already has.
 */
function channelSetLayer(spec) {
    if ((spec?.render || {}).rgb) return false;
    return (spec?.channels || []).some((channel) => channel?.name && channel?.src);
}

/**
 * The channels a layer is drawn with before its card has said anything.
 *
 * `render.channels` is the saved list -- the same shape the reference image's
 * saved channel list has, but per layer. Its ranges are in RAW 16-BIT UNITS,
 * which cannot be converted into the byte domain the shader works in without
 * the channel's quantization window, and that is a fetch away. So this seeds
 * the FULL window, exactly as a freshly enabled reference channel starts, and
 * the card's panel narrows it once the window arrives. The colour is
 * immediate, because it needs nothing.
 *
 * A layer saved before `render.channels` existed named one channel and one
 * colour (`channelIndex`/`color`), which is read here so an existing project
 * looks the way it did.
 */
function seedChannelsFor(spec, channels) {
    const render = spec?.render || {};
    const full = [0, 1];
    const at = (index) => channels[index];
    const rows = Array.isArray(render.channels) && render.channels.length
        ? render.channels
        : [{ index: Math.min(render.channelIndex ?? 0, Math.max(0, channels.length - 1)),
             color: render.color }];
    const seeded = [];
    for (const row of rows) {
        const channel = at(row?.index ?? 0) || (row?.name
            ? channels.find((c) => c?.name === row.name) : null);
        if (!channel?.name) continue;
        seeded.push({
            name: channel.name,
            color: hexToChannelColor(row?.color) || { r: 255, g: 255, b: 255 },
            range: full,
        });
    }
    return seeded;
}

/** `#rrggbb` as the 0-255 triple `toFloatColor` reads, or null. */
function hexToChannelColor(hex) {
    const cleaned = String(hex || "").trim().replace(/^#/, "");
    const full = cleaned.length === 3
        ? cleaned.split("").map((c) => c + c).join("") : cleaned;
    if (!/^[0-9a-fA-F]{6}$/.test(full)) return null;
    const value = parseInt(full, 16);
    return { r: (value >> 16) & 255, g: (value >> 8) & 255, b: value & 255 };
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
    //
    // PINNED ON THE SOURCE, falling back to the live flag for a source built
    // without one (the label layer, a plugin's own tiled layer). Reading the
    // global here moved every item's address space the instant the toggle
    // flipped, which was only ever safe because the flip tore every item down
    // in the same tick. It stopped being safe the moment the outgoing items
    // have to keep drawing until their replacements are ready (see
    // `handOverWhenReady`): an outgoing item would fetch the INCOMING quality
    // for any tile the user panned onto mid-swap, so the picture being held up
    // would be quietly rebuilt at the very quality it was there to cover for.
    const hd = this.tileFormat !== 32 && (this.hd ?? tileQuality.hd) ? "q=hd" : "";
    // Empty for a tile this server serves, which is every tile of an ordinary
    // project. A tile fetched straight from a data node carries its token and
    // the project's tile grid -- see services/resourceRouting.js. Joined with
    // `&` rather than concatenated blindly: the HD flag used to be written as
    // a bare "?q=hd", which is a second "?" the moment anything else is there.
    // A label tile's version. Tiles are cached by the browser for a year, and
    // a mask served before it was converted -- or by a server that drew every
    // level at full resolution -- is a different picture at the same address.
    // The server ignores the parameter; changing it is what changes the URL.
    const version = this.tileFormat === 32 && this.labelVersion
        ? `v=${encodeURIComponent(this.labelVersion)}` : "";
    const query = [hd, this.srcQuery || "", version].filter(Boolean).join("&");
    const suffix = query ? `?${query}` : "";
    return `${this.src}${s.level}/${s.x}_${s.y}.png${suffix}`;
}

/**
 * Mask pixels per image pixel for `config`: 1, or the power of two the server
 * drew a polygon mask at (`segmentationScale`). Anything else is read as 1 --
 * a tile source sized by a non-power-of-two would ask for levels that do not
 * exist.
 */
function maskScale(config) {
    const scale = Number(config?.segmentationScale) || 1;
    return scale >= 1 && Number.isInteger(Math.log2(scale)) ? scale : 1;
}

//: Bumped when label tiles already in browsers' caches must not be reused.
//: 2: single-level masks used to be served at full resolution at every level.
const LABEL_TILE_REVISION = "2";

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
    // Through the view transform when there is one: OSD's pointFromPixel
    // undoes a rotation but not a flip (services/viewTransform.js). Read off
    // window lazily, because this module is webpacked and that one is not.
    const transform = typeof window !== "undefined" ? window.PlexoraViewTransform : null;
    const frac = transform && tiledImage.viewer
        ? transform.pointFromPixel(tiledImage.viewer, position)
        : tiledImage.viewport.pointFromPixel(position);
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
        //: THE ONE PLACE the model reaches the renderers. Before this the
        //: Layers panel's eye and opacity slider wrote the stack and nothing
        //: read them back, so a card promised two controls that moved nothing.
        //: Everything now takes the same route -- a card, a plugin calling
        //: `ctx.layers.setVisible`, a restored `visible: false` from disk --
        //: and this is where it becomes a picture.
        this.layerStack?.subscribe(() => this.applyLayerState());
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
     * THE ORDER OF THAT REBUILD IS THE WHOLE OF WHETHER IT FLASHES. It used to
     * be remove-then-add, so every channel left the world before its
     * replacement had asked for a single tile: the canvas went black and the
     * new tiles then arrived one at a time, which is the checkerboard. It is
     * now add-then-remove, with the removal held until the new pair can draw
     * -- see `addChannelItems` and `handOverWhenReady`. Nothing is on screen
     * at two qualities and nothing is on screen at none.
     *
     * `rememberView` is gone with it, and that is a consequence rather than a
     * separate change: it was here because emptying the world makes the next
     * add look to OpenSeadragon like a first open, so it re-fits the whole
     * slide (Viewer.processReadyItems does `goHome` when the world reaches one
     * item). A world that never empties never goes home, so there is nothing
     * to put back -- and calling it anyway would snapshot the viewport
     * mid-animation and slam it back at the first item to land.
     * @param enabled - true for HD (16-bit), false for the fast/default WebP path
     * @returns a promise that settles once every layer is on screen at the new
     *   quality. The checkbox ignores it -- the status chip is what reports
     *   the wait -- and it is here so a test can assert on the END of a swap
     *   rather than on the moment one was asked for.
     */
    setHdMode(enabled) {
        tileQuality.hd = Boolean(enabled);
        // The swap is no longer instant -- it waits for a viewport of tiles at
        // the new quality -- so something has to say the click was heard. The
        // navbar chip is the one indicator the app has (services/appStatus.js);
        // a specific label beats the generic "Loading" its per-TiledImage tile
        // tracking would otherwise show on its own.
        const task = window.PlexoraStatus?.begin?.(
            tileQuality.hd ? "HD tiles" : "Fast tiles");
        const swaps = [];
        // Not while the base layer's eye is off. `currentChannels` is which
        // channels the user has chosen, not which are on the world (see
        // `hideReference`), so rebuilding off it here would put the whole
        // composite back on screen under a closed eye. There is nothing to
        // rebuild either: `showReference` re-adds every slot, and by then
        // `getTileUrl` is reading the new quality off a source built after
        // this flag moved.
        if (!this.referenceHidden) {
            for (const srcIdx of Object.keys(this.channelList.currentChannels).map(Number)) {
                const url = this.channelList.currentChannels[srcIdx]?.url;
                const outgoing = this.referenceItemsFor(url);
                swaps.push(this.addChannelItems(srcIdx, outgoing));
            }
        }
        // Registered layers too, for the same reason and by the same means.
        // `getTileUrl` reads the quality pinned on each tile source, so every
        // layer channel's address has just changed without anything on the
        // handle changing -- and a TiledImage's own per-address cache has to
        // be thrown away rather than invalidated in place (see the note
        // above). `setStyle` with no argument keeps the current style and
        // rebuilds, held the same way.
        for (const handle of this.tiledLayers?.values() || []) {
            swaps.push(handle.setStyle?.(undefined));
        }
        const done = Promise.all(swaps.filter(Boolean));
        if (task) done.then(() => task.done(), () => task.done());
        window.dispatchEvent(new CustomEvent("plexora:hd-mode-changed", { detail: { enabled } }));
        return done;
    }

    /**
     * Set HD before anything has been drawn, without the rebuild.
     *
     * For the one caller that turns HD on while the world is still EMPTY: a
     * sample opened by walking from a sibling that had HD on. `setHdMode` is
     * wrong there in a way that is invisible until it bites. It calls
     * `rememberView()`, which snapshots the viewport so the rebuild can put the
     * user back where they were -- but a viewer with no items has no meaningful
     * centre or zoom, and that snapshot stays armed for its five-second window.
     * The first channel then arrives, and `restoreView()` applies the nonsense
     * over OpenSeadragon's own fit-the-whole-slide open: the sample comes up
     * framed on nothing.
     *
     * There is also nothing to rebuild. `getTileUrl` reads the flag when it
     * builds each address, so every channel added after this point is already
     * an HD address.
     *
     * The event still fires: the HD checkbox, the mini-map and the channel
     * sliders' domain all listen for it, and they have to agree with the flag
     * whether or not any tile has been drawn yet.
     */
    presetHdMode(enabled) {
        tileQuality.hd = Boolean(enabled);
        window.dispatchEvent(new CustomEvent("plexora:hd-mode-changed",
            { detail: { enabled: tileQuality.hd } }));
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
     * Run `commit` once every one of `items` can draw what the view needs.
     *
     * THE WHOLE OF HOW A TILE QUALITY CHANGES WITHOUT THE PICTURE GOING AWAY.
     *
     * Switching between the default 8-bit tiles and the HD 16-bit ones changes
     * every tile ADDRESS (see getTileUrl), and OpenSeadragon has no way to
     * re-point a TiledImage at a new address space -- its per-address Tile
     * cache has to be thrown away rather than invalidated in place, or stale
     * canvases stay on screen until each tile happens to be refetched. So the
     * items are rebuilt. What used to happen next is what this exists to stop:
     * the old items were removed FIRST, so the world was empty for as long as
     * a viewport of tiles took to fetch and decode -- a black frame, then the
     * new tiles arriving one at a time as a checkerboard.
     *
     * Instead the replacements are added at opacity 0 (with `preload`, which
     * is what keeps OpenSeadragon fetching for an item it is not drawing --
     * see TiledImage.getDrawArea) while the old ones keep drawing, and the
     * caller's `commit` -- which reveals the new items and removes the old --
     * is held until every replacement says it can draw. Nothing is ever on
     * screen at two qualities and nothing is ever on screen at none.
     *
     * ONE `commit` FOR THE WHOLE GROUP, and that is the reason this takes a
     * list rather than being called per item. A channel is drawn as a
     * cover/paint pair (`destination-out` then `lighter`); revealing the new
     * cover before the new paint would put two cover blits over one paint and
     * punch the channel's own shape out of the picture, which is a worse
     * artifact than the gap this replaces, not a smaller one.
     *
     * The wait is bounded. See QUALITY_SWAP_TIMEOUT_MS.
     */
    handOverWhenReady(items, commit) {
        const waiting = (items || []).filter(Boolean);
        if (!waiting.length) {
            commit();
            return;
        }
        let outstanding = waiting.length;
        let handed = false;
        let timer = null;
        const stops = [];
        const hand = () => {
            if (handed) return;
            handed = true;
            if (timer !== null) clearTimeout(timer);
            for (const stop of stops) stop();
            commit();
        };
        const arrived = () => {
            outstanding -= 1;
            if (outstanding <= 0) hand();
        };
        for (const item of waiting) stops.push(whenItemCanDraw(item, arrived));
        // After the loop, because a harness world (and a warm tile cache)
        // answers synchronously -- arming a 20 s timer that is already spent
        // would hold a node probe's event loop open for exactly that long.
        if (!handed) timer = setTimeout(hand, QUALITY_SWAP_TIMEOUT_MS);
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

        // Chosen while the base layer is switched off: the slot is recorded
        // above and nothing is added, so turning the eye back on brings this
        // channel with it. Adding it now would put one channel on an
        // otherwise empty world and make the hide look broken.
        if (this.referenceHidden) return;

        this.addChannelItems(srcIdx);
    }

    /**
     * Put one channel's cover/paint pair on the world.
     *
     * Split out of `channel_add` so the HD toggle can ask for the same pair at
     * a different tile quality WITHOUT first taking down the one that is
     * drawing -- `channel_add` owns the slot in `currentChannels`, and a
     * rebuild must not touch that.
     *
     * @param srcIdx - integer id of the channel
     * @param outgoing - the pair currently drawing this channel, kept on
     *   screen and removed only once the new pair can draw (see
     *   `handOverWhenReady`). Null or empty for an ordinary add, which has
     *   nothing to protect and so lands visible straight away.
     * @returns a promise that settles when the pair is on screen, for a caller
     *   that wants to say so (the HD toggle's status chip). Never rejects: a
     *   channel that cannot be rebuilt keeps the pair it has.
     */
    addChannelItems(srcIdx, outgoing = null) {
        const url = this.imageViewer.config["imageData"][srcIdx]["src"];
        const { maxLevel, extraZoomLevels } = this.imageViewer.config;
        const magnification = 2 ** extraZoomLevels;
        const held = (outgoing || []).filter(Boolean);
        const replacing = held.length > 0;
        let settle = null;
        const settled = new Promise((resolve) => { settle = resolve; });

        const tileSource = {
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
            tileFormat: 16,
            srcIdx: srcIdx,
            src: url,
            srcQuery: this.imageViewer.config["imageData"][srcIdx]["srcQuery"] || "",
            // The alpha this channel's tiles carry is its coverage, which is
            // what lets the pair below mean anything. See u_alpha_mode.
            coverageAlpha: true,
            // Which layer this world item belongs to. Read back by
            // LayerStack.anchorIndex, which needs to find the reference
            // image rather than trust a position.
            layerId: PlexoraLayerStack.REFERENCE_LAYER_ID,
            //: WHICH TILE QUALITY THIS ITEM'S ADDRESSES ARE IN, fixed at the
            //: moment it is built. See getTileUrl for why it cannot be read
            //: off the live flag any more.
            hd: Boolean(tileQuality.hd),
        };

        // The base card's slider, carried onto every channel as it is added:
        // `applyReferenceState` can only reach items that already exist, and
        // these arrive a frame later.
        const opacity = this.referenceOpacity();
        //: The pair, as it lands. A replacement is held here rather than
        //: claimed on arrival: `applyReferenceState` pushes the base card's
        //: opacity onto every item the stack holds for this layer, so an
        //: unrevealed item claimed early would be faded up by any stack change
        //: that happened to land mid-swap -- and two qualities drawn at once
        //: is a doubly-bright flash rather than a black one.
        const landed = [];
        //: How many of the two adds have answered at all, which is not the
        //: same as how many arrived: an add that FAILS never reaches `claim`,
        //: and a handover gated on two arrivals would then wait out the full
        //: deadline before showing anything. Counted here so a failed half
        //: still closes the swap -- see `settleOne`.
        let answered = 0;
        const handOver = () => {
            // BOTH HALVES OR NEITHER, on the way in as much as on the way out.
            // One half of a pair is not a degraded picture, it is a wrong one:
            // a lone paint blit is this channel added over everything, and a
            // lone cover blit is its shape punched out of the picture. So a
            // half that failed to add takes the other half with it, and the
            // channel keeps the pair it already has -- still the old quality,
            // still correct, still on screen.
            if (landed.length !== 2) {
                this.dropWorldItems(landed.map((entry) => entry.item));
                settle();
                return;
            }
            this.handOverWhenReady(landed.map((entry) => entry.item), () => {
                const world = this.viewer?.world;
                // The slot may have gone while this was loading -- a channel
                // switched off mid-swap takes its replacement with it (see
                // channel_remove), and reviving it here would put a channel
                // nobody asked for back on screen with nothing able to remove
                // it.
                const alive = landed.every(
                    (entry) => !world || world.getIndexOfItem(entry.item) >= 0);
                if (!alive) {
                    this.dropWorldItems(landed.map((entry) => entry.item));
                    settle();
                    return;
                }
                for (const entry of landed) {
                    entry.item.setOpacity?.(this.referenceOpacity());
                    this.claimWorldItem(
                        PlexoraLayerStack.REFERENCE_LAYER_ID, entry.item, entry.z);
                }
                this.dropWorldItems(held);
                this.applyWorldOrder();
                this.viewer?.forceRedraw?.();
                settle();
            });
        };
        //: Called once per add, whether it arrived or failed. The handover
        //: starts when both have spoken, not when both have arrived.
        const settleOne = () => {
            answered += 1;
            if (answered === 2) handOver();
        };
        const claim = (e, z) => {
            this.restoreView();
            // "open" is what wires up the GL colorize pipeline (see
            // imageViewer.js's initGL, bound to the "open" handler) --
            // previously only raised from load_label_image()'s success
            // callback, which never runs for a datasource with no
            // segmentation (noLabel short-circuits it). That left image
            // channels fetching real tile bytes successfully but never
            // getting GL-rendered: tiles loaded, nothing drew. initGL is safe
            // to run more than once, so raising it here too (redundant when a
            // label image is also present) is harmless.
            this.viewer.raiseEvent("open", e.item);
            if (!replacing) {
                this.claimWorldItem(PlexoraLayerStack.REFERENCE_LAYER_ID, e.item, z);
                this.applyWorldOrder();
                settle();
                return;
            }
            landed.push({ item: e.item, z });
            settleOne();
        };

        // TWO BLITS PER CHANNEL, for the same reason a registered layer gets
        // them -- and this is the change that stopped the reference image
        // being a special kind of picture.
        //
        // It used to be one blit: an OPAQUE BLACK tile added with `lighter`,
        // which works only because black adds nothing, and only while there is
        // nothing underneath to add it to. That one fact was the whole of why
        // the reference could not be moved up the stack (additive over an H&E
        // saturates, so the layer beneath simply washes it out) and why the
        // ground it sits on could not be changed (a non-black fill would be
        // added once per channel, N times over).
        //
        // The pair says the same picture as a composite instead:
        //
        //     ground * PRODUCT(1 - v_i)  +  SUM(c_i * v_i)
        //
        // `destination-out` takes the ground away in proportion to this
        // channel's coverage, `lighter` adds its colour -- the same addition
        // the channels always did among themselves. Against a black ground
        // the two terms collapse back to SUM(c_i * v_i), which is pixel for
        // pixel what this drew before.
        //
        // EVERY cover blit has to land below EVERY paint blit or a later
        // channel dims an earlier channel's colour instead of the ground's;
        // `claimWorldItem`'s z is what says so, and applyWorldOrder keeps it.
        // The second blit costs one fetch and one decode of nothing: both
        // items address the same tile url, so OSD hands them one cache record
        // and the colorize pass runs once.
        for (const [operation, z] of [[COVER_OPERATION, COVER_Z],
                                      [PAINT_OPERATION, PAINT_Z]]) {
            this.viewer.addTiledImage({
                tileSource: tileSource,
                // ON THE TILEDIMAGE, not inside the tileSource, which is where
                // this used to be written. OSD reads it off these options; a
                // `compositeOperation` on the tile source is carried along by
                // the deep copy and never looked at, so the `lighter` that
                // stood there for years was decorative -- the blend that
                // actually ran was the viewer-wide default, which is also
                // `lighter`. Put in the wrong place here it costs the cover
                // blit, silently: both halves of the pair add, the second one
                // twice, and the picture is merely a little brighter.
                // `load_brightfield_base` already knew this and says so.
                compositeOperation: operation,
                //: A replacement loads INVISIBLY and is faded up by the
                //: handover, so the two qualities are never added together --
                //: which with `lighter` would read as a bright flash. OSD
                //: skips drawing an item at opacity 0 entirely (Drawer.draw),
                //: so the held pair costs the fetch and the decode and not
                //: the colorize pass.
                opacity: replacing ? 0 : opacity,
                //: What keeps an invisible item loading at all --
                //: TiledImage.getDrawArea answers `false` for opacity 0
                //: unless this is set, and a replacement that never fetches
                //: never becomes ready and the swap never completes.
                preload: true,
                success: (e) => claim(e, z),
                //: A half that never arrives still has to close the swap, or
                //: the pair it was replacing is held on screen until the
                //: deadline and the chip says HD for twenty seconds.
                error: () => { if (replacing) settleOne(); else settle(); },
            });
        }
        return settled;
    }

    /**
     * Take world items off the world and out of the stack.
     *
     * `getIndexOfItem` first, because a claimed item can outlive its place in
     * the world -- `rebuildTileLayers` removes the brightfield item directly
     * without telling the stack -- and asking OpenSeadragon to remove one
     * twice is its problem, not this layer's.
     */
    dropWorldItems(items) {
        const world = this.viewer?.world;
        for (const item of items || []) {
            if (!item) continue;
            this.releaseWorldItem(item);
            if (world && world.getIndexOfItem(item) >= 0) world.removeItem(item);
        }
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
        // Not while the base layer's eye is off. `rebuildTileLayers` calls
        // this after a routing repair, and without the guard a reconnecting
        // node would put the slide back on screen under a closed eye.
        if (this.referenceHidden) return;
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
            // As channel_add: the base card's slider has to be carried onto
            // the item, because it is added after the slider was set.
            opacity: this.referenceOpacity(),
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
     * ONE world item, at one placement. A layer with channel controls has
     * several of them and reaches this through `addLayerChannelSet`, which
     * calls this once per active channel; what is left here on its own is a
     * layer with nothing to colourise -- an rgb slide, a plugin's density
     * raster -- whose tiles arrive already coloured (see
     * `layer_sources.parse_style`) for the browser to composite, exactly as a
     * brightfield base does. `tileFormat: 24` is what tells imageViewer.js to
     * leave those bytes alone.
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
            geometry.transform || null, geometry.width, config.width,
            geometry.height);
        if (!placement) return null;

        let item = null;
        let style = spec.style || "";
        //: Held rather than read off the item, because `addTiledImage` is
        //: asynchronous and a slider dragged while one is in flight has to
        //: land on the item when it arrives.
        let opacity = spec.opacity === undefined ? 1 : Number(spec.opacity);
        //: Held for the same reason as `opacity`, and read by `add()` so a
        //: blend chosen while an add is in flight lands on the item.
        let blend = spec.compositeOperation || "lighter";
        // `addTiledImage` is asynchronous, so a hide (or a style change) can
        // land while an add is still in flight. Without this the item arrives
        // after the removal and stays on screen forever -- visible as a layer
        // that will not switch off, which is exactly the bug a toggle exists
        // to avoid.
        //: Starts false for an item whose layer is already hidden, so a
        //: channel switched on inside a hidden layer is never added at all --
        //: "a hidden layer costs nothing" has to hold for a layer that is
        //: gaining channels as well as one that is sitting there.
        let shown = spec.visible !== false;
        //: WHICH add is the live one. `shown` alone is not enough, and the
        //: gap it leaves is a leak rather than a stale flag: two style
        //: changes in quick succession give drop, add, drop, add, and the
        //: second `drop` has no `item` to remove yet because the first add is
        //: still in flight. That first item then arrives to find `shown` true
        //: again -- set by the second add -- so it is kept, and is
        //: immediately overwritten in `item` by the second. Nothing holds a
        //: reference to it any more and nothing can ever remove it: a density
        //: raster that stays on the screen after the layer it belongs to has
        //: been switched to points, drawing and fetching tiles forever.
        //:
        //: A counter settles it. Every add takes the next number and only the
        //: holder of the current one may keep its item; every drop burns the
        //: number, so an add already in flight when the layer is hidden knows
        //: it is stale even if another add has since made `shown` true.
        let generation = 0;
        //: A REPLACEMENT THAT HAS LANDED BUT NOT TAKEN OVER: on the world,
        //: invisible, and fetching every tile in view. The generation counter
        //: covers the window before it lands; this covers the one after, which
        //: only exists because the caller decides when the handover happens
        //: (see `prepareStyle`). Without it a layer switched off mid-swap
        //: keeps paying for a viewport of tiles until the swap's deadline.
        let pending = null;
        const self = this;

        /**
         * @param replacing - the item currently drawing this layer, to be kept
         *   on screen until the new one can draw. Null for an ordinary add.
         * @param onReady - called once the add has answered, with the item
         *   that arrived (or null) and a `commit` that reveals it and drops
         *   `replacing`. The caller owns WHEN commit runs, which is what lets
         *   a cover/paint pair hand over in the same frame -- see
         *   `addLayerChannelSet`'s setStyle.
         */
        function add(replacing = null, onReady = null) {
            shown = true;
            generation += 1;
            const mine = generation;
            const holding = replacing || null;
            const answer = (arrived, commit) => onReady?.(arrived, commit || (() => {}));
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
                    //: This item's channel record, for a layer drawn through
                    //: the GL colorize pass. Held BY REFERENCE and mutated in
                    //: place by whoever owns it (`LayerChannelSet`), so a new
                    //: colour or contrast window is a repaint and not a
                    //: refetch. Absent for a server-coloured RGB layer, whose
                    //: tiles arrive as the picture.
                    channel: spec.channel,
                    //: WHAT THIS ITEM'S ALPHA MEANS -- coverage, or the
                    //: constant over an opaque black tile. Set by whoever adds
                    //: the item, because it is a property of how the item is
                    //: composited (a cover/paint pair needs coverage) and not
                    //: of what kind of layer it belongs to. See u_alpha_mode.
                    coverageAlpha: Boolean(spec.coverageAlpha),
                    // WHO this plane is, for the GL texture cache. `getTileKey`
                    // interpolates srcIdx into the key glInit's
                    // GLTileTextureCache is keyed on, and every registered
                    // layer used to answer `0` -- the reference image's first
                    // channel. Harmless while layer tiles were RGB and never
                    // reached that cache; the moment one is drawn as a
                    // `tileFormat: 16` plane it would share a GPU texture with
                    // reference channel 0 and draw that channel's pixels.
                    // A string, because nothing reads this as a number -- it
                    // is only ever interpolated into the key.
                    srcIdx: spec.srcIdx ?? `${layerId}:${src}`,
                    src: src,
                    srcQuery: style,
                    layerId: layerId,
                    //: The tile quality this item's addresses are in, fixed
                    //: here so an outgoing item cannot start fetching the
                    //: incoming one's tiles mid-swap. See getTileUrl.
                    hd: Boolean(tileQuality.hd),
                },
                compositeOperation: blend,
                x: placement.x,
                y: placement.y,
                width: placement.width,
                degrees: placement.degrees,
                flipped: placement.flipped,
                //: A replacement loads INVISIBLY behind the item it replaces,
                //: and `preload` is what keeps an invisible item loading at
                //: all (TiledImage.getDrawArea). Both are turned off again at
                //: handover: a layer faded to zero with the opacity slider is
                //: still on, and must go back to costing nothing.
                opacity: holding ? 0 : opacity,
                preload: Boolean(holding),
                success: (e) => {
                    // Before anything else, and for the reason every other add
                    // in this class does it: an item landing in an emptied
                    // world makes OSD fit the whole slide again. A layer
                    // channel can now BE that first item -- the HD toggle and
                    // main.js's rebuildTileLayers both empty the world -- so
                    // without this, flipping HD re-frames the slide.
                    self.restoreView();
                    if (!shown || mine !== generation) {
                        // Hidden -- or superseded -- while this was loading.
                        // Drop it on arrival rather than keeping it
                        // invisible: an item nobody asked for still fetches
                        // every tile in view.
                        self.viewer.world.removeItem(e.item);
                        answer(null);
                        return;
                    }
                    // THE LIVE RECORD, PUT BACK. OpenSeadragon's TileSource
                    // constructor ends in `$.extend(true, this, options)` -- a
                    // DEEP COPY -- so the channel record handed to
                    // `addTiledImage` above is cloned on the way in, and the
                    // object the colorize pass reads back is a snapshot rather
                    // than the one `LayerChannelSet` holds. Mutating the record
                    // for a colour or a contrast window then changed nothing
                    // that was already on screen: the channel kept whatever it
                    // was added with, for as long as it stayed added. Which is
                    // also why it LOOKED like it worked -- a channel switched
                    // off and on again came back correct, because that is a
                    // fresh add with a fresh copy.
                    //
                    // Overwriting the copy here is what makes "held by
                    // reference" true. It has to be here rather than at the
                    // call site because `addTiledImage` is asynchronous and
                    // `e.item.source` does not exist until it lands.
                    if (spec.channel) e.item.source.channel = spec.channel;
                    if (!holding) {
                        item = e.item;
                        self.claimWorldItem(layerId, e.item, spec.z);
                        self.applyWorldOrder();
                        answer(null);
                        return;
                    }
                    // HELD. The item is on the world and loading, but at
                    // opacity 0 and unclaimed: `applyLayerState` pushes the
                    // card's opacity onto every item the stack holds for this
                    // layer, so claiming it now would let any stack change
                    // that lands mid-swap fade it up beside the one it is
                    // replacing -- two qualities drawn at once.
                    pending = e.item;
                    answer(e.item, () => {
                        if (pending === e.item) pending = null;
                        if (!shown || mine !== generation) {
                            self.dropWorldItems([e.item]);
                            return;
                        }
                        e.item.setOpacity?.(opacity);
                        e.item.setPreload?.(false);
                        // The blend may have moved while this was loading --
                        // `setBlend` sets it in place on the item it can see,
                        // which was the outgoing one.
                        e.item.setCompositeOperation?.(blend);
                        item = e.item;
                        self.claimWorldItem(layerId, e.item, spec.z);
                        self.dropWorldItems([holding]);
                        self.applyWorldOrder();
                    });
                },
                //: An add that fails still has to answer, or a pair waiting on
                //: it is held at the old quality until the deadline.
                error: () => answer(null),
            });
        }

        function drop() {
            shown = false;
            // Burn the number too, so an add still in flight cannot install
            // its item after this returns.
            generation += 1;
            // And take a replacement that has already landed with it. It is
            // invisible, so nothing on screen says it is there, and it is
            // preloading, so it costs exactly as much as the item it was going
            // to replace.
            if (pending) {
                self.dropWorldItems([pending]);
                pending = null;
            }
            if (!item) return;
            self.releaseWorldItem(item);
            self.viewer.world.removeItem(item);
            item = null;
        }

        /**
         * Start a refetch without taking the current item down.
         *
         * The first half of `setStyle`, separated so a CALLER can decide when
         * the handover happens. `addLayerChannelSet` needs that: its channels
         * are drawn as cover/paint pairs, and revealing one half before the
         * other punches the channel's own shape out of the picture. So it
         * prepares every handle, waits for all of them, and commits them
         * together.
         *
         * @returns `Promise<{item, commit}>` -- `item` is the replacement,
         *   already on the world, loading, and invisible; `commit` reveals it
         *   and removes the one it replaces. `item` is null when there was
         *   nothing to do or the add failed, and `commit` is then a no-op, so
         *   a caller never has to branch.
         */
        function prepareStyle(next) {
            if (next !== undefined) style = next || "";
            if (!shown) return Promise.resolve({ item: null, commit: () => {} });
            return new Promise((resolve) => {
                add(item, (arrived, commit) => resolve({ item: arrived, commit }));
            });
        }

        if (shown) add();

        return {
            placement,
            remove: drop,
            prepareStyle,
            /**
             * A new colour or window. Re-added rather than invalidated, for
             * the reason the HD toggle gives: invalidating in place leaves the
             * old canvases on screen until each tile happens to be refetched.
             *
             * ADD, THEN REMOVE. This used to be `drop(); add();`, which left
             * this layer's whole footprint empty for as long as a viewport of
             * tiles took to arrive -- on the HD toggle, every layer at once.
             * The replacement is loaded invisibly behind the item it replaces
             * and only takes over once it can draw.
             *
             * `undefined` keeps the style and refetches anyway, which is what
             * the HD toggle asks for: the url has changed underneath (see
             * getTileUrl) without anything here changing, and passing "" would
             * silently drop a server-side colour on the way past.
             *
             * @returns a promise that settles once the new item is on screen.
             */
            setStyle(next) {
                return prepareStyle(next).then(({ item: arrived, commit }) => {
                    if (!arrived) {
                        commit();
                        return undefined;
                    }
                    return new Promise((done) => {
                        self.handOverWhenReady([arrived], () => {
                            commit();
                            self.viewer?.forceRedraw?.();
                            done();
                        });
                    });
                });
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
            /**
             * How strongly it is drawn. A real blend and not a re-add: an
             * opacity slider is dragged, and tearing the world item down on
             * every pointer move would refetch the whole viewport per tick.
             *
             * Zero is left as a blend rather than turned into a hide, because
             * the two say different things -- a layer faded to nothing is
             * still on, and the eye is what turns it off.
             */
            setOpacity(value) {
                opacity = Math.max(0, Math.min(1, Number(value)));
                if (item) item.setOpacity(opacity);
            },
            /**
             * How this layer meets what is under it.
             *
             * `lighter` ADDS, which is what makes fluorescence channels stack
             * into one picture and what makes an H&E slide over them wash out
             * to white. `source-over` REPLACES, which is what an H&E wants and
             * what makes it hide the layer below until its opacity comes down.
             * So this is the control that makes "higher in the stack" mean
             * something for a brightfield layer.
             *
             * Set in place where OSD allows it -- a composite operation is a
             * canvas flag, not a reason to refetch a viewport of tiles. The
             * re-add is a fallback for a viewer that lacks the setter, kept
             * because `setStyle` already proves this class survives one.
             */
            setBlend(next) {
                blend = next || "lighter";
                if (!shown) return;
                if (item && typeof item.setCompositeOperation === "function") {
                    item.setCompositeOperation(blend);
                    return;
                }
                drop();
                add();
            },
        };
    }

    /**
     * A registered layer drawn as N channels rather than one picture.
     *
     * THE WHOLE POINT: a layer added with **+ Add Layer** gets the SAME
     * controls the reference image has -- several channels at once, a colour
     * each, a log contrast window each -- and gets them for free, because
     * colour and contrast are applied in the GL pass on the client rather than
     * baked into the tile url on the server. Moving a slider is a repaint; it
     * used to be a refetch of the viewport.
     *
     * A composite of `addTiledLayer` handles, and shaped like ONE of them on
     * the outside -- `placement`, `remove`, `setVisible`, `setOpacity`,
     * `setStyle` -- so everything that holds a handle (`tiledLayers`,
     * `applyLayerState`, `removeLayer`, main.js's `rebuildTileLayers`) keeps
     * working without knowing which kind it has. `setChannels` is the one
     * thing extra; `setBlend` is the one thing missing, because where a layer
     * sits is the stack order and the opacity slider.
     *
     * TWO WORLD ITEMS PER CHANNEL, and that pair is the whole of how a layer
     * manages to be a multichannel image AND sit over the picture beneath it
     * at the same time. OpenSeadragon composites every world item straight
     * onto one canvas -- there is no group -- so "these N channels add among
     * themselves, and the result of that goes over the image below" cannot be
     * said with one operation per channel. Said with two it can, exactly:
     *
     *   base . II(1 - v_i)  +  SUM c_i . v_i
     *
     * where `v_i` is channel i's windowed intensity and `c_i` its colour. The
     * COVER blit (`destination-out`) is the left-hand term -- it takes the
     * base away in proportion to how much of the pixel this channel occupies
     * -- and the PAINT blit (`lighter`) is the right-hand one, which is the
     * same addition the reference image's own channels do. Two co-located
     * channels still mix (red and green still read yellow); what they no
     * longer do is wash out into a bright H&E underneath them, because the
     * H&E has been taken away first.
     *
     * Both terms have to be complete before the other starts, so EVERY cover
     * blit sits below EVERY paint blit -- interleave them and a later
     * channel's cover dims an earlier channel's colour, which is the
     * one-channel-at-a-time bug wearing a different hat. `spec.z` is what says
     * so; LayerStack.applyWorldOrder keeps it.
     *
     * The pair costs one extra blit per tile and nothing else: both items
     * address the SAME tile url, so OpenSeadragon hands them one cache record,
     * one fetch and one decode, and the colorize pass runs once (the second
     * finds its signature already drawn). The alpha they share carries
     * coverage rather than the reference image's constant -- see u_alpha_mode
     * in frag.glsl.
     *
     * @param spec.layerId  - the stack id, tagged onto every item
     * @param spec.sources  - `{name -> src}` for every channel this layer has
     * @param spec.geometry - as `addTiledLayer`
     * @returns a handle, or null when the transform is one OSD cannot express
     */
    /**
     * The ground ONE layer's channels composite onto, as a world item.
     *
     * The reference image's ground is the canvas (see `applyViewerGround`),
     * because the reference is the scene's frame and what is behind it is
     * behind everything. A registered layer's is not: it covers part of the
     * scene, so its ground has to be the same shape it is -- an opaque fill of
     * its footprint, under its cover blits, which is the `ground` term in
     *
     *     ground * PRODUCT(1 - v_i)  +  SUM(c_i * v_i)
     *
     * With no background set there is no item at all and the term is whatever
     * is underneath, which is the default and the thing that lets a registered
     * multiplex layer be read against the slide below it.
     *
     * ITS OWN TILE SOURCE, and a deliberately tiny one: one tile, one level,
     * one pixel. It cannot share the channels' source, because two world items
     * on one url share one OpenSeadragon cache record and therefore one tile
     * canvas -- which is exactly what makes the cover/paint pair cost one
     * decode, and exactly what would make a ground and a channel overwrite
     * each other's pixels. None of the project's pyramid helpers are wired to
     * it either: at `tileFormat` RGB both the colorize pass and the decoder
     * hand the tile straight back to OSD, so nothing ever asks it a question
     * about levels.
     *
     * @returns a handle, or null when the layer's transform is one OSD cannot
     *   express -- the same answer `addTiledLayer` gives, for the same reason.
     */
    addLayerGround(spec) {
        const { layerId, geometry = {}, colour } = spec || {};
        const config = this.imageViewer.config;
        if (!layerId || !colour) return null;
        const placement = PlexoraLayerStack.placementFor(
            geometry.transform || null, geometry.width, config.width,
            geometry.height);
        if (!placement) return null;

        const self = this;
        let item = null;
        let dropped = false;
        const width = Math.max(1, Math.round(Number(geometry.width) || config.width));
        const height = Math.max(1, Math.round(Number(geometry.height) || config.height));

        this.viewer.addTiledImage({
            tileSource: {
                width: width,
                height: height,
                //: One tile, so there is no grid to seam and no level to pick.
                tileWidth: width,
                tileHeight: height,
                minLevel: 0,
                maxLevel: 0,
                getTileUrl: () => groundTileUrl(colour),
                //: "already the picture" -- see tileDrawingCustom's first
                //: branch, and createTileLoadedHandler's. Both return at once,
                //: which is the whole of why this needs no pyramid helpers.
                tileFormat: RGB_TILE_FORMAT,
                srcIdx: `${layerId}:ground`,
                layerId: layerId,
            },
            compositeOperation: "source-over",
            x: placement.x,
            y: placement.y,
            width: placement.width,
            degrees: placement.degrees,
            flipped: placement.flipped,
            opacity: spec.opacity === undefined ? 1 : Number(spec.opacity),
            success: (e) => {
                // A background switched off, or the layer removed, while the
                // add was in flight. Same hazard `addTiledLayer` counts
                // generations for; one boolean is enough here because a ground
                // is replaced rather than restyled.
                if (dropped) {
                    self.viewer.world.removeItem(e.item);
                    return;
                }
                self.restoreView();
                item = e.item;
                self.claimWorldItem(layerId, e.item, GROUND_Z);
                self.applyWorldOrder();
            },
        });

        return {
            remove() {
                dropped = true;
                if (!item) return;
                self.releaseWorldItem(item);
                if (self.viewer.world.getIndexOfItem(item) >= 0) {
                    self.viewer.world.removeItem(item);
                }
                item = null;
            },
            setOpacity(value) { item?.setOpacity?.(Number(value)); },
        };
    }

    addLayerChannelSet(spec) {
        const { layerId, sources = {}, geometry = {} } = spec || {};
        const config = this.imageViewer.config;
        if (!layerId) return null;
        const placement = PlexoraLayerStack.placementFor(
            geometry.transform || null, geometry.width, config.width,
            geometry.height);
        if (!placement) return null;

        const self = this;
        //: name -> { handles: [cover, paint], record }. The RECORD is what
        //: both tile sources hold by reference and what the colorize pass
        //: reads, so mutating it in place is the whole cost of a colour or
        //: window change.
        const entries = new Map();
        let shown = spec.visible !== false;
        let opacity = spec.opacity === undefined ? 1 : Number(spec.opacity);
        let style = "";
        //: The layer's own ground, or null for "whatever is underneath" --
        //: which is the default, and the thing that lets a multiplex layer be
        //: read against the slide below it. See `addLayerGround`.
        let groundColour = null;
        let ground = null;

        function addChannel(name, colour, range) {
            const src = sources[name];
            if (!src) return;
            const record = { color: colour, range: range };
            const common = {
                layerId,
                src,
                // Not a styled url: the bytes wanted here are the plain
                // quantized plane a reference channel gets, which is exactly
                // what the layer tile route serves when no colour is asked
                // for. `q=hd` still rides along, from getTileUrl.
                style: style,
                tileFormat: 16,
                channel: record,
                //: The pair is a cover blit and a paint blit, so the alpha
                //: has to be coverage. Said here rather than inferred from
                //: `channel` above -- see the note in tileColorize.js.
                coverageAlpha: true,
                //: Distinct per channel and SHARED by the pair, because it is
                //: what the GL texture cache is keyed on -- see the note in
                //: `addTiledLayer`. The two items want the same texture.
                srcIdx: `${layerId}:${name}`,
                opacity,
                geometry,
                // Never added while the layer's eye is off -- see the note on
                // `shown` in addTiledLayer. `setVisible(true)` brings every
                // channel back at once.
                visible: shown,
            };
            const handles = [
                self.addTiledLayer({ ...common,
                    compositeOperation: COVER_OPERATION, z: COVER_Z }),
                self.addTiledLayer({ ...common,
                    compositeOperation: PAINT_OPERATION, z: PAINT_Z }),
            ].filter(Boolean);
            if (handles.length !== 2) {
                for (const handle of handles) handle.remove();
                return;
            }
            entries.set(name, { handles, record });
        }

        function forEachHandle(run) {
            for (const held of entries.values()) {
                for (const handle of held.handles) run(handle);
            }
        }

        function repaint() {
            self.imageViewer?.scheduleRepaint?.();
        }

        /**
         * The colour this layer's channels are drawn on, or null for the
         * picture underneath.
         *
         * Dropped and re-added rather than recoloured, because the colour IS
         * the tile: `addLayerGround`'s source serves one pixel of it. A no-op
         * when nothing has changed, which is what lets `syncLayerImages` seed
         * it on every re-sync.
         *
         * A closure rather than a method reaching its sibling through `this`,
         * because `setVisible` has to call it and a handle is destructured in
         * more than one place.
         */
        function setGround(hex) {
            const next = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(String(hex || ""))
                ? String(hex) : null;
            if (next === groundColour && (!next || ground)) return;
            groundColour = next;
            ground?.remove();
            ground = null;
            if (!next || !shown) return;
            ground = self.addLayerGround({
                layerId, geometry, colour: next, opacity,
            });
        }

        return {
            placement,
            /**
             * Which channels are drawn, and in what colour and window.
             *
             * A DIFF, and that is the requirement rather than an optimisation:
             * a contrast slider emits an event per pointer move, and dropping
             * and re-adding a world item per tick would refetch every tile in
             * view dozens of times per drag. So a channel that is still here
             * keeps its item and has its record mutated, and only a channel
             * that arrived or left touches the world at all.
             *
             * @param list - `[{name, color: {r,g,b}, range: [lo, hi]}]`, the
             *   range in the SAME fractional units `rangeConnector` holds for
             *   the reference image (see imageViewer.updateChannelRange).
             */
            setChannels(list) {
                const wanted = new Map();
                for (const entry of list || []) {
                    if (entry && entry.name) wanted.set(entry.name, entry);
                }
                for (const [name, held] of [...entries]) {
                    if (wanted.has(name)) continue;
                    for (const handle of held.handles) handle.remove();
                    entries.delete(name);
                }
                for (const [name, entry] of wanted) {
                    const held = entries.get(name);
                    if (!held) {
                        addChannel(name, entry.color, entry.range);
                        continue;
                    }
                    held.record.color = entry.color;
                    held.record.range = entry.range;
                }
                repaint();
            },
            /** Every channel item goes, and the layer costs nothing. */
            remove() {
                forEachHandle((handle) => handle.remove());
                entries.clear();
                ground?.remove();
                ground = null;
            },
            setVisible(visible) {
                shown = Boolean(visible);
                forEachHandle((handle) => handle.setVisible(shown));
                // The ground is the layer as much as its channels are: a
                // hidden layer that left an opaque rectangle behind would be
                // an eye that did not switch anything off.
                if (shown) setGround(groundColour);
                else { ground?.remove(); ground = null; }
            },
            setGround,
            /**
             * How strongly the whole layer is drawn.
             *
             * Carried by BOTH halves of every channel's pair, which is what
             * makes it mean what the slider says it means: the cover blit
             * takes away `o . v` of the base and the paint blit puts back
             * `o . c . v`, so the layer fades towards the picture underneath
             * rather than towards black.
             */
            setOpacity(value) {
                opacity = Math.max(0, Math.min(1, Number(value)));
                forEachHandle((handle) => handle.setOpacity(opacity));
                ground?.setOpacity(opacity);
            },
            /**
             * Refetch every channel at the current url.
             *
             * The HD toggle is the only caller: the tile quality is pinned on
             * each tile source when it is built (`getTileUrl`), so a flipped
             * toggle changes every address without anything here changing, and
             * the items have to be rebuilt for OSD to notice.
             *
             * ONE HANDOVER FOR THE WHOLE LAYER, and that is the reason this is
             * not just `forEachHandle(h => h.setStyle(style))`. Each channel is
             * a cover blit and a paint blit; let each handle hand over on its
             * own schedule and there are frames with a new cover over an old
             * paint -- the base taken away twice and the colour added once,
             * which is this layer's shape punched out of the picture as a dark
             * patch. Every handle prepares its replacement, and they are all
             * revealed together once the last of them can draw.
             *
             * @returns a promise that settles once the layer is back on
             *   screen at the new quality.
             */
            setStyle(next) {
                if (next !== undefined) style = next || "";
                const prepared = [];
                forEachHandle((handle) => prepared.push(handle.prepareStyle(style)));
                if (!prepared.length) return Promise.resolve();
                return Promise.all(prepared).then((results) => new Promise((done) => {
                    const items = results.map((result) => result.item).filter(Boolean);
                    self.handOverWhenReady(items, () => {
                        for (const result of results) result.commit();
                        self.viewer?.forceRedraw?.();
                        done();
                    });
                }));
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
        const config = this.imageViewer?.config || {};
        this.tiledLayers = this.tiledLayers || new Map();
        const wanted = new Set();

        for (const spec of list) {
            if (!spec?.id || spec.kind !== "image") continue;
            if (spec.id === PlexoraLayerStack.REFERENCE_LAYER_ID) continue;
            // A layer whose tiles are still being built has a card and a
            // progress line, and nothing to fetch: adding it now would be a
            // wall of 404s and an empty rectangle.
            if (spec.status && spec.status !== "ready") continue;
            const channels = spec.channels || [];
            if (!channels.length || !channels[0]?.src) continue;
            wanted.add(spec.id);
            if (this.tiledLayers.has(spec.id)) continue;

            this.seedLayerState(spec);
            const geometry = {
                width: spec.width,
                height: spec.height,
                maxLevel: spec.maxLevel,
                tileWidth: spec.tileWidth,
                tileHeight: spec.tileHeight,
                transform: spec.transform || null,
            };
            // Off the STACK, not off the spec: by now the stack holds either
            // what the server stored (seeded just above) or what the user has
            // since chosen, and the item has to arrive wearing it rather than
            // flash at full strength and be corrected.
            const opacity = this.layerStack?.get(spec.id)?.opacity ?? 1;
            // Off the stack for the same reason the opacity is, and it saves
            // more: a layer restored with its eye off is never ADDED, rather
            // than added and dropped a moment later by `applyLayerState`. The
            // add is what starts a viewport of tile requests.
            const visible = this.layerStack?.get(spec.id)?.visible !== false;

            const handle = channelSetLayer(spec)
                ? this.addLayerChannelSet({
                    layerId: spec.id,
                    sources: Object.fromEntries(
                        channels.filter((c) => c?.name && c?.src)
                            .map((c) => [c.name, c.src])),
                    opacity, geometry, visible,
                })
                : this.addTiledLayer({
                    layerId: spec.id,
                    src: channels[
                        Math.min((spec.render || {}).channelIndex ?? 0,
                                 Math.max(0, channels.length - 1))]?.src
                        || channels[0].src,
                    style: styleQuery(spec.render),
                    // An rgb layer is the picture the scanner recorded, so it
                    // draws OVER what is beneath it and its opacity is what
                    // lets that show through. NOT A CHOICE: the card used to
                    // offer Add or Over and that control was the bug. A
                    // channelled layer needs a pair of operations rather than
                    // one and `addLayerChannelSet` owns them.
                    compositeOperation: "source-over",
                    opacity, geometry, visible,
                });
            if (!handle) continue;
            this.tiledLayers.set(spec.id, handle);
            // The channels this layer opens with, in the colours and windows
            // it was last left in. A set with nothing in it draws nothing, and
            // a layer that drew nothing until its card was built would be a
            // regression on the single-channel path this replaces.
            handle.setChannels?.(seedChannelsFor(spec, channels));
            // And the ground it was last left on. Absent for every layer that
            // has not been asked, which is what keeps "a registered layer is
            // transparent where it has no signal" the default.
            handle.setGround?.((spec.render || {}).background);
        }

        for (const [id, handle] of [...this.tiledLayers]) {
            if (wanted.has(id)) continue;
            handle.remove();
            this.tiledLayers.delete(id);
        }
        // A layer restored with its eye off was added above -- the handle has
        // to exist before anything can hide it -- so drop it now, on arrival,
        // rather than leaving the stack saying one thing and the world another.
        this.applyLayerState();
        return this.tiledLayers;
    }

    /**
     * @function channel_remove - remove channel from multichannel rendering
     * @param srcIdx - integer id of channel to remove
     */
    channel_remove(srcIdx) {
        if (!(srcIdx in this.channelList.currentChannels)) return;
        const url = this.channelList.currentChannels[srcIdx]?.url;

        // EVERY item drawn from this channel, not the first one found.
        //
        // A channel is two world items -- the cover blit and the paint blit
        // (see `channel_add`) -- and this used to `break` at the first match.
        // Stopping there leaves the other half in the world: the paint blit
        // alone is the channel drawn additively over everything, and the cover
        // blit alone is a channel-shaped hole punched in the picture. Both
        // survive a reload, because `currentChannels` has already forgotten
        // the channel that would remove them.
        //
        // Collected before anything is removed, because `removeItem` is what
        // the indices being walked are indices into.
        //
        // A pair MID-SWAP is four items rather than two -- the outgoing pair
        // and the invisible replacement loading behind it -- and this takes
        // all of them, which is what it has to do: the slot is going, so the
        // replacement has nothing left to replace.
        this.dropWorldItems(this.referenceItemsFor(url));
        delete this.channelList.currentChannels[srcIdx];
    }

    /**
     * Every world item currently drawing (or loading) one reference channel.
     *
     * By tile address and layer, not by position: the reference image's items
     * are interleaved with a mask, a ground and any registered layer, and
     * their order is the stack's to decide.
     */
    referenceItemsFor(url) {
        const world = this.viewer?.world;
        if (!world || !url) return [];
        const found = [];
        for (let i = 0; i < world.getItemCount(); i += 1) {
            const item = world.getItemAt(i);
            if (item?.source?.src === url
                && item?.source?.layerId === PlexoraLayerStack.REFERENCE_LAYER_ID) {
                found.push(item);
            }
        }
        return found;
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
    claimWorldItem(layerId, item, z) {
        const stack = this.layerStack;
        if (!stack || !item) return;
        //: Where this item sits WITHIN its layer, for the one layer that has
        //: more than one kind of item: a channel set's cover blits must stay
        //: below its paint blits however the adds happen to land. Everything
        //: else answers 0, which is what an untagged item is taken to be.
        if (z !== undefined) item[PlexoraLayerStack.ITEM_Z] = Number(z) || 0;
        const isMask = layerId === PlexoraLayerStack.MASK_LAYER_ID;
        const layer = stack.get(layerId) || stack.register(layerId, {
            kind: isMask ? "labels" : "image",
            // The mask has no card, because the Cells footer owns how cells
            // are drawn -- so nothing can drag it back up once a raster passes
            // it. Pinned here as well as in syncLayers: the label image is
            // often added before /config's layer list arrives.
            pinned: isMask,
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
     * Put what the server stored about a layer into the stack, once.
     *
     * The same first-registration rule `ImageViewer.syncLayers` follows, and
     * here for the same reason it is there: the stack is authoritative from
     * then on, so a /config poll or an adopt after an import must not undo the
     * eye the user just clicked. Repeated rather than shared because either
     * can be the first to see a layer -- this one runs for a layer imported
     * mid-session, that one at boot -- and the two files are on opposite sides
     * of the bundle seam.
     */
    seedLayerState(spec) {
        const stack = this.layerStack;
        if (!stack || !spec?.id || stack.has(spec.id)) return;
        const opacity = Number((spec.render || {}).opacity);
        stack.register(spec.id, {
            kind: spec.kind || "image",
            visible: spec.visible !== false,
            opacity: Number.isFinite(opacity) ? opacity : 1,
        });
    }

    /**
     * Make the tiles surface look like the stack says it should.
     *
     * Runs on every stack change, which is often -- so every step is
     * idempotent and cheap when nothing moved: `setVisible` is a no-op while
     * `shown` already agrees, and `setOpacity` is a blend, not a rebuild.
     *
     * The MASK is deliberately not touched. It is in the stack and it is on
     * the tiles surface, but the Cells footer owns whether boundaries are
     * drawn (`sel_outlines`) and how -- one control, in one place. Its kind is
     * `labels`, so the `image` filter below already passes it by; centroids
     * are `points` and draw on the overlay, which is not this surface.
     */
    applyLayerState() {
        const stack = this.layerStack;
        if (!stack || this._applyingLayerState) return;
        // showReference re-adds channels, whose success callbacks can register
        // a layer and so re-enter this through the subscription. Once through
        // is enough: the loop below reads the stack as it is now.
        this._applyingLayerState = true;
        try {
            for (const layer of stack.layers()) {
                if (layer.kind !== "image") continue;
                if (layer.id === PlexoraLayerStack.REFERENCE_LAYER_ID) {
                    this.applyReferenceState(layer);
                    continue;
                }
                const handle = this.tiledLayers?.get(layer.id);
                if (!handle) continue;
                handle.setVisible(layer.visible);
                handle.setOpacity(layer.opacity);
            }
        } finally {
            this._applyingLayerState = false;
        }
    }

    /**
     * The ground the whole scene composites onto.
     *
     * The reference image's background, and the viewer's, are the same colour
     * said once. The reference IS the scene's frame -- every other layer's
     * registration is expressed against it, and it has no transform of its
     * own because it is the one thing there is nothing to express it against
     * -- so the ground under it is the ground under everything, and that is a
     * property of the canvas rather than of a world item.
     *
     * A CSS custom property rather than a colour written straight onto the
     * element, so viewer.css keeps BOTH defaults: an unset variable falls
     * back to black for a composite and to the off-white a slide is read
     * against. Nothing here has to know which kind of image this is.
     *
     * @param hex - `render.background`, or anything falsy for the default.
     */
    applyViewerGround(hex) {
        //: `typeof` rather than a truthiness test, because a bare `document`
        //: in a context that has none is a ReferenceError and not undefined.
        //: Every probe in tests/js drives this class under node, and this
        //: method is reached from `applyReferenceState` -- which is to say
        //: from every stack change, not from some corner.
        const element = this.viewer?.element
            || (typeof document === "undefined" ? null
                : document.getElementById("openseadragon"));
        if (!element?.style) return;
        const value = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(String(hex || ""))
            ? String(hex) : "";
        if (value) element.style.setProperty("--plexora-viewer-ground", value);
        else element.style.removeProperty("--plexora-viewer-ground");
    }

    /** The base image's own eye and opacity, applied to every channel item. */
    applyReferenceState(layer) {
        // Before the blank-frame guard below: a project with no image still
        // has a ground, and it is the only thing it has.
        this.applyViewerGround(layer?.spec?.render?.background);
        // A blank frame's eye is inert on purpose: it is the only world item
        // there is, and an empty world has no home rectangle, no anchor for
        // the overlays and no answer for `viewportImageBounds`. Hiding nothing
        // to break everything is not a trade worth offering.
        if (this.imageViewer?.config?.image_kind === "blank") return;
        if (layer.visible && this.referenceHidden) this.showReference();
        else if (!layer.visible && !this.referenceHidden) this.hideReference();
        if (this.referenceHidden) return;
        const opacity = this.referenceOpacity();
        for (const item of layer.items || []) item.setOpacity?.(opacity);
    }

    /** The base image's opacity as a number in [0, 1]. */
    referenceOpacity() {
        const layer = this.layerStack?.get(PlexoraLayerStack.REFERENCE_LAYER_ID);
        const value = Number(layer?.opacity);
        if (!Number.isFinite(value)) return 1;
        return Math.max(0, Math.min(1, value));
    }

    /**
     * Take the base image off the world.
     *
     * Removal, not opacity 0, for the reason `addTiledLayer.setVisible` gives:
     * an item at zero opacity still requests, decodes and draws every tile in
     * view, and the base image is the most expensive thing on screen.
     *
     * The channel SLOTS are left alone. `currentChannels` is which channels
     * the user has chosen, not which are on the world; clearing it here would
     * make the sidebar forget the whole composite because somebody blinked.
     */
    hideReference() {
        const world = this.viewer?.world;
        if (!world) return;
        // Emptying the world makes OSD fit the whole slide again when the next
        // item lands (see rememberView), so where the user was looking has to
        // be carried across by hand.
        this.rememberView();
        this.referenceHidden = true;
        const layer = this.layerStack?.get(PlexoraLayerStack.REFERENCE_LAYER_ID);
        // OFF THE WORLD AS WELL AS OFF THE STACK, and the union rather than
        // either one. The stack's list is what is being DRAWN; a quality swap
        // in flight has a pair on the world that is not on it yet (see
        // `addChannelItems`), invisible and preloading, and an eye closed
        // between the add and the handover would leave it fetching every tile
        // in view for as long as the project stayed open -- and then reveal it
        // under a closed eye. The stack side is still needed for the other
        // direction: `rebuildTileLayers` drops the brightfield item with
        // `world.removeItem` directly, without telling the stack, so a claimed
        // item can outlive its place in the world.
        const doomed = new Set(layer?.items || []);
        for (let i = 0; i < world.getItemCount(); i += 1) {
            const item = world.getItemAt(i);
            if (item?.source?.layerId === PlexoraLayerStack.REFERENCE_LAYER_ID) {
                doomed.add(item);
            }
        }
        this.dropWorldItems([...doomed]);
    }

    /**
     * Put it back, rebuilt from the slots the hide left standing.
     *
     * Rebuilt rather than re-shown because there is nothing left to show: a
     * removed TiledImage is gone. Each channel is added exactly as it was --
     * `channel_add` reads its colour and window back out of `colorConnector`
     * and `rangeConnector`, which is where they live anyway.
     */
    showReference() {
        this.referenceHidden = false;
        const kind = this.imageViewer?.config?.image_kind;
        if (kind === "brightfield") {
            this.load_brightfield_base();
            return;
        }
        const slots = Object.keys(this.channelList?.currentChannels || {}).map(Number);
        for (const srcIdx of slots) {
            // Deleted first because channel_add refuses a slot it already
            // holds -- which, after a hide, is every one of them.
            delete this.channelList.currentChannels[srcIdx];
            this.channel_add(srcIdx);
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
     * Drop the label layer and load it again, optionally at a new version.
     *
     * For when the mask behind the same address has changed -- a routing
     * repair, or a node that has just finished converting the mask it was
     * serving raw. Both lazy-load guards are reset: `noLabel` is set by the
     * error callback when the layer failed to load, which during an outage it
     * did, and `labelLayerRequested` is what makes loading happen once.
     */
    reloadLabelLayer(version) {
        if (version !== undefined && version !== null) {
            this.imageViewer.config.segmentationVersion = String(version);
        }
        const world = this.viewer && this.viewer.world;
        if (!world || !this.imageViewer.config.segmentation) return;
        for (let i = world.getItemCount() - 1; i >= 0; i -= 1) {
            const item = world.getItemAt(i);
            if (item && item.source && item.source.tileFormat === 32) {
                world.removeItem(item);
            }
        }
        this.imageViewer.noLabel = false;
        this.labelLayerRequested = false;
        this.load_label_image();
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
            // A mask drawn from polygons can be a power of two FINER than the
            // image (boundary_mask.mask_scale): `scale` times the pixels and a
            // level per doubling past the image's finest, so the mask's level
            // j is the image's j - log2(scale). Same world rectangle -- OSD
            // gives both items width 1 -- so only the source's size changes,
            // and the viewer's zoom ceiling rises with it to where the cells
            // are readable.
            const scale = maskScale(this.imageViewer.config);
            const fine = Math.log2(scale);
            this.viewer.addTiledImage({
                tileSource: {
                    height: this.imageViewer.config.height * magnification * scale,
                    width: this.imageViewer.config.width * magnification * scale,
                    maxLevel: extraZoomLevels + maxLevel - 1 + fine,
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
                    labelVersion: [
                        LABEL_TILE_REVISION,
                        this.imageViewer.config.segmentationVersion,
                        // A redrawn mask keeps its path, and its level numbers
                        // change meaning with the scale -- so the scale is in
                        // the address, or a year-cached tile of the old grid
                        // answers for the new one.
                        scale > 1 ? `s${scale}` : null,
                    ].filter(Boolean).join("."),
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
