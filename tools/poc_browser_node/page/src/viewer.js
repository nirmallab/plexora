/**
 * A minimal viewer made of the product's own rendering modules, loaded
 * unmodified from the checkout (index.html): glInit.js, tileColorize.js,
 * tileDecode.js, the shaders and workers/tileDecoder.js.
 *
 * What ImageViewer contributes in the product -- texture-unit bookkeeping,
 * the centre/mode uniforms with no cells, the channel record -- is supplied
 * here in a few lines each, and every one of them is listed in P0's record as
 * a seam: something a static build has to get from somewhere other than
 * imageViewer.js.
 *
 * Two ways to feed a channel:
 *  - "node": OSD's own XHR to a Plexora node's HD tile url, decoded by the
 *    product's tile-loaded handler in its worker pool (today's direct route);
 *  - "browser": a tile source whose downloadTileStart asks the browser node
 *    (a TIFF worker pool) and hands the u16 plane to the tile the way
 *    tileFetch.js's `deliver` does -- no PNG, no decode on the page.
 */
import { makeTileSource } from "./tileSource.js";

/** Globals the rendering modules expect that this page has to supply. */
export const SHIMS = {};

export function installShims() {
    // tileColorize.js calls toFloatColor, a function declared in imageViewer.js.
    if (typeof globalThis.toFloatColor !== "function") {
        globalThis.toFloatColor = (color) => [color.r / 255, color.g / 255, color.b / 255];
        SHIMS.toFloatColor = "declared in imageViewer.js; copied (one line)";
    }
    // tileColorize.js reads lodash's `_.get`. The vendor bundle imports lodash
    // for its side effects only; whether that leaves a global is recorded.
    if (typeof globalThis._ === "undefined") {
        globalThis._ = { get: (o, k, d) => (o && o[k] !== undefined ? o[k] : d) };
        SHIMS._ = "lodash global absent outside the product page; `_.get` shimmed";
    }
}

const SELECT_TEXTURE = (gl, texture, idx) => {
    // ImageViewer.selectTexture, verbatim.
    if (texture === undefined) {
        throw new TypeError(`Cannot bind undefined to texture ${idx}.`);
    }
    gl.activeTexture(gl["TEXTURE" + idx]);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 1);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
};

/**
 * @param container  element id
 * @param geometry   {width, height, levels, tileWidth, tileHeight}
 * @param channels   [{index, color: "#rrggbb", range: [lo, hi] in 0..1 of 65535}]
 * @param feed       "node" | "browser"
 * @param nodeTileUrl (channel, level, x, y) -> url           (feed "node")
 * @param browserTile (channel, level, x, y) -> Promise<tile> (feed "browser")
 * @param labels     optional {feed, tileUrl, browserTile, layer} for P2
 */
export async function createViewer({ container, geometry, channels, feed, nodeTileUrl, browserTile,
                                     labels = null, decodeWorkers = 4 }) {
    installShims();
    let resolveGLReady;
    const glReady = new Promise((resolve) => { resolveGLReady = resolve; });
    const viewer = OpenSeadragon({
        id: container,
        // The product's choices (imageViewer.js) that bear on what is drawn.
        compositeOperation: "lighter",
        loadTilesWithAjax: true,
        immediateRender: false,
        blendTime: 0,
        maxTilesPerFrame: 1,
        maxImageCacheCount: 512,
        imageLoaderLimit: 10,
        timeout: 90000,
        tileRetryMax: 3,
        collectionMode: false,
        preload: false,
        homeFillsViewer: true,
        visibilityRatio: 0,
        drawer: "canvas",
        minZoomImageRatio: 0.1,
        maxZoomPixelRatio: 15,
        showNavigationControl: false,
        animationTime: 0,
        springStiffness: 100,
        crossOriginPolicy: "Anonymous",
    });

    const constantTextures = ["ids", "centers", "ranges", "pickings"];
    let renderer = null;
    const indexOfTexture = (label, scope = null) => {
        // ImageViewer.indexOfTexture, against this renderer.
        if (scope === "M") {
            const index = renderer._markerTextures.indexOf(label);
            if (index > -1) return index;
            const next = renderer._nextMarkerTexture;
            renderer._nextMarkerTexture = (next + 1) % renderer._markerTextures.length;
            renderer._markerTextures[next] = label;
            return next + renderer._markerOffset;
        }
        const index = constantTextures.indexOf(label);
        return index > -1 ? index + renderer._otherOffset : -1;
    };
    renderer = PlexoraGL.createGLRenderer({ indexOfTexture, selectTexture: SELECT_TEXTURE, resolveGLReady });

    const labelGpu = labels && typeof PlexoraLabelGpu !== "undefined"
        ? PlexoraLabelGpu.createLabelGpu({
            renderer,
            vShaderUrl: plexoraUrl("client/src/shaders/vert.glsl"),
            fShaderUrl: plexoraUrl("client/src/shaders/label.frag.glsl"),
            labelTile: PlexoraLabelTile,
        })
        : null;

    // ImageViewer.selectCenterProps with no cells (idCount 0, nothing picked).
    const selectCenterProps = (tile, source) => {
        const tl = source.toTileLevels(tile.level, tile.x, tile.y);
        const bounds = source.toMagnifiedBounds(tile.level, tile.x, tile.y);
        const w = geometry.tileWidth || 1024;
        const h = geometry.tileHeight || 1024;
        return {
            pie_radius_1f: 8.5,
            magnitude_2iv: [1, 1],
            id_end_1i: 0,
            picked_end_1i: -1,
            modes_2i: [false, false],
            key_end_1i: 0,
            marker_sample_4iv: [-1, -1, -1, -1],
            tile_scale_1f: Math.max(tl.relativeImageScale, 1.0),
            tile_fraction_1f: Math.min(tl.relativeImageScale, 1.0),
            x_bounds_2fv: new Float32Array(bounds.x),
            y_bounds_2fv: new Float32Array(bounds.y),
            origin_2fv: new Float32Array([tl.outputTile.x * w, tl.outputTile.y * h]),
        };
    };

    const state = { maskLayers: labels ? [labels.layer] : [], outlines: Boolean(labels) };
    const { tileDrawingDefault, tileDrawingCustom } = PlexoraTileColorize.createTileDrawing({
        renderer,
        floatRange: [0, 1],
        findCurrentChannel: () => undefined,
        selectCenterProps,
        labelOutlinesEnabled: () => state.outlines,
        modeFlags: () => ({ edge: state.outlines, or: false }),
        maskDrawList: () => state.maskLayers,
        labelGpu,
        labelGpuMode: () => Boolean(labelGpu?.active),
        segmentationMode: () => labels?.segmentationMode || "filled",
    });

    const decoderPool = PlexoraTileDecode.TileDecoderPool.create(
        plexoraUrl("client/src/js/workers/tileDecoder.js"), decodeWorkers);
    const handleTileLoaded = PlexoraTileDecode.createTileLoadedHandler({
        decoderPool,
        renderTileLayers: () => {},
        forceRepaint: () => viewer.forceRedraw(),
    });
    const glInit = PlexoraGL.createGLInit({
        viewer, renderer,
        config: { tileWidth: geometry.tileWidth || 1024, tileHeight: geometry.tileHeight || 1024 },
        handleTileLoaded, tileDrawingCustom, tileDrawingDefault,
    });
    viewer.addHandler("open", glInit);
    // Built and wired BEFORE any item exists, as in the product (the viewer
    // opens before ViewerManager adds channels): a tile that loaded before
    // the tile-loaded handler was on would never be decoded.
    glInit();
    await glReady;
    await renderer.ready();

    // Timing of every browser-fed tile, for the record.
    const tileLog = [];
    const deliver = (job, decoded) => {
        // tileFetch.js's TileFetchScheduler.deliver, minus its bookkeeping.
        const tile = job.tile;
        tile._array = decoded.array;
        tile._format = decoded.format;
        if (decoded.format === "u32") {
            tile._labelWidth = decoded.width;
            tile._labelHeight = decoded.height;
        }
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, decoded.width);
        canvas.height = Math.max(1, decoded.height);
        job.finish(canvas.getContext("2d"), null, "context2d");
    };
    const browserDownload = (fetchTile, channel) => (job, s) => {
        const t0 = performance.now();
        fetchTile(channel, s.level, s.x, s.y).then((decoded) => {
            tileLog.push({ channel, level: s.level, x: s.x, y: s.y, ms: performance.now() - t0,
                           worker: decoded.timing });
            if (!decoded.width) {
                job.fail("past the image edge", null);
                return;
            }
            deliver(job, decoded);
        }, (error) => job.fail(String(error), null));
    };

    const items = [];
    const add = (tileSource, extra = {}) => new Promise((resolve, reject) => {
        viewer.addTiledImage({
            tileSource,
            compositeOperation: "lighter",
            success: (e) => { items.push(e.item); resolve(e.item); },
            error: (e) => reject(new Error(e?.message || "addTiledImage failed")),
            ...extra,
        });
    });

    for (const ch of channels) {
        const channel = { color: d3.color(ch.color), range: ch.range };
        const source = makeTileSource({
            geometry, srcIdx: ch.index, tileFormat: 16, channel,
            // ".png" on the browser address too, and not for show: OSD infers
            // a tile's transparency from its url (TileSource.hasTransparency
            // matches ".png") and clears the tile's rectangle before drawing
            // one -- without it the two arms composite differently (P0).
            tileUrl: (level, x, y) => (feed === "node"
                ? nodeTileUrl(ch.index, level, x, y)
                : `browser://c${ch.index}/${level}/${x}_${y}.png`),
            download: feed === "browser" ? browserDownload(browserTile, ch.index) : null,
        });
        await add(source);
    }
    if (labels) {
        const source = makeTileSource({
            geometry, srcIdx: "mask", tileFormat: 32,
            tileUrl: (level, x, y) => (labels.feed === "node"
                ? labels.tileUrl(level, x, y)
                : `browser://mask/${level}/${x}_${y}.png`),
            download: labels.feed === "browser" ? browserDownload(labels.browserTile, "mask") : null,
        });
        await add(source, { compositeOperation: "source-over" });
    }
    // The product re-raises `open` after adding items (viewerManager.js);
    // createGLInit's handler only redraws from here on.
    viewer.raiseEvent("open", {});

    return {
        viewer, renderer, labelGpu, items, tileLog, state, decoderPool,
        setChannel(i, { color, range }) {
            const source = items[i].source;
            if (color) source.channel.color = d3.color(color);
            if (range) source.channel.range = range;
            items[i]._needsDraw = true;
            viewer.world.draw();
        },
        destroy() {
            decoderPool?.terminate?.();
            viewer.destroy();
        },
    };
}

/** Resolves when every item has its best tiles for the current view loaded. */
export async function waitFull(viewer, { timeoutMs = 60000 } = {}) {
    const t0 = performance.now();
    const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
    for (;;) {
        await frame();
        const world = viewer.world;
        let full = world.getItemCount() > 0;
        for (let i = 0; i < world.getItemCount(); i += 1) {
            if (!world.getItemAt(i).getFullyLoaded()) { full = false; break; }
        }
        if (full && !world.needsDraw()) {
            await frame();
            await frame();
            return { ms: performance.now() - t0, full: true };
        }
        if (performance.now() - t0 > timeoutMs) return { ms: performance.now() - t0, full: false };
    }
}

/** The drawer canvas's pixels. */
export function canvasPixels(viewer) {
    const canvas = viewer.drawer.canvas;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    return ctx.getImageData(0, 0, canvas.width, canvas.height).data;
}
