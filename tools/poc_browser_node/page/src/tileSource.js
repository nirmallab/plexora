/**
 * The tile-source shape the product's colorize and decode passes read
 * (tileFormat, toTileLevels, toMagnifiedBounds, getTileKey ...).
 *
 * COPIED from plexora/client/src/js/views/viewerManager.js, where these are
 * module-private functions inside the webpack bundle: nothing outside the
 * bundle can reach them. That is a P0 finding (a seam to export), not a
 * change made here -- the copies are line-for-line, so the shaders see the
 * same uniforms they see in the product.
 */

function toIdealTile(fullScale, useY) {
    const { _tileWidth, _tileHeight } = this;
    return [_tileWidth, _tileHeight][useY] * fullScale;
}

function toRealTile(fullScale, v, useY) {
    const shape = [this.width, this.height][useY];
    const tileShape = this.toIdealTile(fullScale, useY);
    return Math.min(shape - v * tileShape, tileShape);
}

function toTileBoundary(fullScale, v, useY) {
    const start = v * this.toIdealTile(fullScale, useY);
    const size = this.toRealTile(fullScale, v, useY);
    return { start, size };
}

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

function getTileKey(level, x, y) {
    const { srcIdx, tileFormat } = this;
    const s = this.toTileLevels(level, x, y).inputTile;
    return `${tileFormat}-${srcIdx}-${s.level}-${s.x}-${s.y}`;
}

/**
 * One channel's (or the mask's) tile source.
 *
 * @param geometry  {width, height, levels: [...], tileWidth, tileHeight}
 * @param srcIdx    channel index (labels: "mask")
 * @param tileFormat 16 for a channel, 32 for labels
 * @param tileUrl   (level, x, y) -> url, in SERVER levels (0 = full size)
 * @param download  optional (job, {level, x, y}) -> void, replacing OSD's XHR
 */
export function makeTileSource({ geometry, srcIdx, tileFormat, tileUrl, download = null, channel = null }) {
    const tileWidth = geometry.tileWidth || 1024;
    const tileHeight = geometry.tileHeight || 1024;
    const source = {
        height: geometry.height,
        width: geometry.width,
        maxLevel: geometry.levels.length - 1,
        tileWidth,
        tileHeight,
        // OSD 6 keeps these as _tileWidth/_tileHeight once it builds the source;
        // toIdealTile reads them, so set them for a source used before then.
        _tileWidth: tileWidth,
        _tileHeight: tileHeight,
        extraZoomLevels: 0,
        toMagnifiedBounds, toTileBoundary, toTileLevels, toIdealTile, toRealTile, getTileKey,
        getTileUrl(level, x, y) {
            const s = this.toTileLevels(level, x, y).inputTile;
            return tileUrl(s.level, s.x, s.y);
        },
        tileFormat,
        srcIdx,
        // Registered-layer style: the colour and window ride on the source, so
        // the colorize pass needs no channel lookup by url.
        channel,
        coverageAlpha: false,
        hd: true,
    };
    if (download) {
        source.downloadTileStart = function (job) {
            const tile = job.tile;
            const s = this.toTileLevels(tile.level, tile.x, tile.y).inputTile;
            download(job, s);
        };
        source.downloadTileAbort = function () {};
    }
    return source;
}
