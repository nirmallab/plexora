/**
 * tileSource.js -- what makes an object one of Plexora's OpenSeadragon tile
 * sources: its pyramid arithmetic, its tile addresses and its cache keys.
 *
 * Every tile source the viewer builds -- a channel, the label mask, a
 * brightfield slide, a registered layer, a layer's ground -- is a plain
 * object carrying these functions, and they run with `this` bound to it: OSD
 * calls `getTileUrl` and `getTileKey` on the source, and the colorize pass,
 * tileDecode.js and labelGpu.js call `toTileLevels` and `toMagnifiedBounds`.
 *
 * Extracted verbatim from viewerManager.js, where they were module-private
 * functions inside the webpack bundle and nothing outside it could reach
 * them. Here they are one module both sides share: ViewerManager builds its
 * sources from it, and a source built elsewhere -- a browser node supplying
 * tiles in a static build of these pages -- carries the same functions, so the
 * shader sees the same addresses, keys and levels whoever served the bytes.
 * No address, key or level changed on the way.
 *
 * An ES module, bundled into vendor_bundle.js through viewerManager.js (which
 * re-exports `tileQuality`) and exposed there as `window.PlexoraTileSource`
 * for scripts outside the bundle (vendor.js): one instance, so the HD flag
 * the toggle writes is the one every address reads.
 */

// Global HD-toggle state, read by getTileUrl() below and written by
// ViewerManager.setHdMode. A plain module-scope flag (not a ViewerManager
// instance field) because getTileUrl runs with `this` bound to the tileSource
// object it's attached to, not the ViewerManager -- this is the one place in
// the tile-loading path that needs the toggle state and can't reach it via
// `this`.
const tileQuality = { hd: false };

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
    // A plane, projection or section of a volume, pinned on the source like
    // `hd`. A 2-D mask is the same picture on every plane and names none; a
    // 3-D label volume names the plane its labels are read from (`labelView`).
    const viewToken = this.tileFormat === 32 ? this.labelView : this.view;
    const view = viewToken ? `view=${encodeURIComponent(viewToken)}` : "";
    // The window a volume's 8-bit tile is quantized with, pinned like the
    // view (services/volumeWindows.js): a provisional window's bytes and the
    // exact window's never share an address.
    const window = this.tileFormat !== 32 && !hd && viewToken && this.windowStamp
        ? `w=${this.windowStamp}` : "";
    const query = [hd, this.srcQuery || "", version, view, window].filter(Boolean).join("&");
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
//: 3: an id's high byte (alpha) used to be dropped, so ids from 2^24 aliased.
const LABEL_TILE_REVISION = "3";

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
    // The view is part of the key (glInit.js caches GPU textures on it): plane
    // 13 of a channel must never bind plane 12's texture. Absent for a planar
    // image, so its keys are what they always were.
    const token = tileFormat === 32 ? this.labelView : this.view;
    const view = token ? `-${token}` : "";
    const window = tileFormat !== 32 && token && this.windowStamp && !this.hd
        ? `-w${this.windowStamp}` : "";
    return `${tileFormat}-${srcIdx}${view}${window}-${s.level}-${s.x}-${s.y}`;
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

export {
    tileQuality, groundTileUrl, keyOf, toIdealTile, toRealTile, toTileBoundary,
    toMagnifiedBounds, toTileLevels, getTileUrl, maskScale, LABEL_TILE_REVISION,
    getTileKey, getImagePixel,
};
