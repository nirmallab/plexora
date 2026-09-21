/**
 * What a tile's alpha means, and who is allowed to blank its canvas.
 *
 * A registered layer sits OVER the image beneath it, and the only way to say
 * that in OpenSeadragon -- which composites every world item straight onto one
 * canvas, with no notion of a group -- is to blit each of the layer's channels
 * twice: once with `destination-out` to take the base away in proportion to
 * how much of the pixel the channel covers, once with `lighter` to add the
 * channel's colour. That is `ViewerManager.addLayerChannelSet`, and it rests on
 * two facts this file is about, both of which fail silently:
 *
 *   **A layer's tile carries coverage in its alpha, and so must NOT be filled
 *   black first.** Black is opaque. A black-backed tile has alpha 1
 *   everywhere, so the `destination-out` blit would erase the whole footprint
 *   of every tile rather than just the signal in it -- the H&E vanishes and
 *   the layer looks like it is being drawn on a black card. Nothing throws.
 *
 *   **The two blits share one tile.** They address the same url, so
 *   OpenSeadragon gives them one cache record, one fetch and one decode -- and
 *   raises `tile-loaded` with a request for only the first of them, so the
 *   second item's Tile never gets the decoded plane hung on it. Left alone it
 *   would take the "missing array" path and blank the canvas its twin had just
 *   filled, on every frame, which reads as a layer that flickers or never
 *   appears at all.
 *
 * The reference image must come through all of this byte-identical: a constant
 * alpha over an opaque black tile, which is how a multichannel image has
 * always been drawn here.
 *
 * Run directly:  node tests/js/tile_colorize_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

const failures = [];
function check(label, ok, why) {
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

/** The globals these two classic scripts read off the page. */
function sandbox() {
    const ctx = {
        console,
        RGB_TILE_FORMAT: 24,
        PlexoraLayerStack: { REFERENCE_LAYER_ID: "__image__" },
        //: Only `_.get` is used, and only as "this key or a default".
        _: { get: (object, key, fallback) => (object && object[key] !== undefined
            ? object[key] : fallback) },
        d3: { color: () => ({ r: 255, g: 255, b: 255 }) },
        toFloatColor: (colour) => [colour.r / 255, colour.g / 255, colour.b / 255],
    };
    ctx.window = ctx;
    ctx.globalThis = ctx;
    createContext(ctx);
    return ctx;
}

const ctx = sandbox();
runInContext(readFileSync(join(VIEWS, "tileColorize.js"), "utf8"), ctx);
runInContext(readFileSync(join(VIEWS, "tileDecode.js"), "utf8"), ctx);
const { createTileDrawing } = ctx.PlexoraTileColorize;
const { shareDecoded } = ctx.PlexoraTileDecode;

/** A tile's own 2D context, recording what was done to it. */
function renderedContext() {
    const calls = [];
    return {
        calls,
        canvas: { width: 256, height: 256 },
        fillStyle: "",
        globalAlpha: 1,
        fillRect() { calls.push(`fill:${this.fillStyle}`); },
        clearRect() { calls.push("clear"); },
        drawImage() { calls.push("draw"); },
    };
}

/** The colorize pass, with the GL renderer replaced by a recorder. */
function drawing() {
    const renderer = {
        width: 256,
        height: 256,
        gl_arguments: null,
        updateShape(w, h) { this.width = w; this.height = h; },
    };
    const { tileDrawingCustom } = createTileDrawing({
        renderer,
        floatRange: [0, 1],
        findCurrentChannel: () => ({ color: { r: 0, g: 0, b: 255 }, range: [0, 1] }),
        selectCenterProps: () => ({}),
        labelOutlinesEnabled: () => false,
        modeFlags: () => ({ edge: false, or: false }),
        maskDrawList: () => [],
    });
    const drawn = [];
    return {
        renderer,
        drawn,
        run(event) { tileDrawingCustom(() => drawn.push(event), event); },
    };
}

function tile({ cacheKey = "t0", array = new Uint8Array(4), format = "u8",
                cache = null } = {}) {
    return {
        cacheKey,
        _array: array,
        _format: format,
        getUrl: () => `/tiles/${cacheKey}`,
        getCache: () => cache,
    };
}

const LAYER_SOURCE = (record) => ({
    tileFormat: 16,
    layerId: "mx",
    channel: record,
    coverageAlpha: true,
});
/** The reference image as `channel_add` now adds it: its colour still comes
 *  from the channel table rather than from a record on the source, and its
 *  alpha now carries coverage, because it is composited as a pair too. */
const REFERENCE_SOURCE = { tileFormat: 16, layerId: "__image__",
                           coverageAlpha: true };
/** An item that has not asked for coverage: the opaque, black-backed tile
 *  every multichannel image was drawn as before there was anything to
 *  composite one over. */
const OPAQUE_SOURCE = { tileFormat: 16, layerId: "__image__" };


// -- an item that does not ask for coverage is untouched -------------------

{
    const pass = drawing();
    const rendered = renderedContext();
    pass.run({ tiledImage: { source: OPAQUE_SOURCE }, tile: tile(), rendered });

    check("a tile that has not asked for coverage is backed with opaque black",
        rendered.calls.includes("fill:black") && !rendered.calls.includes("clear"),
        "the shader emits a partial alpha and composites over this");
    check("...and its alpha stays the constant it has always been",
        pass.renderer.gl_arguments.alpha_mode_1i === 0,
        "u_alpha_mode 0 is the path that draws onto its own black ground");
}


// -- the reference image composites as a group ----------------------------

{
    // The reference stopped being the one thing guaranteed to sit at the
    // bottom of the stack, so it is drawn the same way a registered layer is:
    // cover, then paint, over whatever ground is beneath it. Which means its
    // tiles must NOT be opaque -- a black fill reads as full coverage, and the
    // cover blit would punch the ground out over the whole footprint.
    const pass = drawing();
    const rendered = renderedContext();
    pass.run({ tiledImage: { source: REFERENCE_SOURCE }, tile: tile(), rendered });

    check("the reference image's tile carries coverage in its alpha",
        pass.renderer.gl_arguments.alpha_mode_1i === 1,
        "without it the cover blit erases the whole footprint, not the signal");
    check("...and is cleared rather than filled black",
        rendered.calls.includes("clear")
        && !rendered.calls.some((c) => c.startsWith("fill:")),
        "black is opaque, and an opaque tile has nothing under it to show");
    check("...while its colour still comes from the channel table",
        pass.renderer.gl_arguments.color_3fv[2] === 1
        && pass.renderer.gl_arguments.color_3fv[0] === 0,
        "the reference has no record on its source -- it is looked up by url");
}


// -- a registered layer's channel carries coverage ------------------------

{
    const pass = drawing();
    const rendered = renderedContext();
    const record = { color: { r: 255, g: 0, b: 0 }, range: [0, 1] };
    pass.run({
        tiledImage: { source: LAYER_SOURCE(record) }, tile: tile(), rendered,
    });

    check("a layer's channel tile is cleared, never filled black",
        rendered.calls.includes("clear") && !rendered.calls.some((c) => c.startsWith("fill:")),
        "black is opaque: the clearing blit would erase the whole footprint");
    check("...and its alpha is told to carry coverage",
        pass.renderer.gl_arguments.alpha_mode_1i === 1,
        "without the uniform the alpha is a constant and 'over' means 'erase'");
    check("...and the colour still comes from the record the set holds",
        pass.renderer.gl_arguments.color_3fv[0] === 1
        && pass.renderer.gl_arguments.color_3fv[2] === 0,
        "the reference channel table has no entry for a layer at all");
}

{
    // A layer item with no record of its own must draw NOTHING rather than
    // fall through to the reference image's channel table: both sides key
    // channels as `<stem>_<N>`, so two exports of one slide collide.
    const pass = drawing();
    const rendered = renderedContext();
    pass.run({
        tiledImage: { source: { tileFormat: 16, layerId: "mx" } },
        tile: tile(),
        rendered,
    });
    check("a layer item with no channel record draws nothing",
        pass.drawn.length === 0 && rendered.calls.length === 0,
        "the fallback lookup would find the REFERENCE image's channel");
}

{
    // The signature is what lets a tile skip the whole GL pass when its
    // pixels are already right. It has to separate the two alpha meanings, or
    // a layer tile could inherit a reference tile's cached colorization.
    const record = { color: { r: 0, g: 0, b: 255 }, range: [0, 1] };
    const first = drawing();
    const rendered = renderedContext();
    first.run({ tiledImage: { source: OPAQUE_SOURCE }, tile: tile(), rendered });
    const afterReference = rendered.calls.length;
    first.run({
        tiledImage: { source: LAYER_SOURCE(record) }, tile: tile(), rendered,
    });
    check("the alpha mode is part of a tile's drawn signature",
        rendered.calls.length > afterReference,
        "same tile, same colour, same window -- and a different meaning");
}


// -- two world items, one tile --------------------------------------------

{
    // What tileDecode leaves behind for the second of a pair.
    const cache = {};
    const loaded = tile({ cache, array: new Uint8Array([7]), format: "u8" });
    shareDecoded(loaded);
    check("a decoded plane is left on the tile's cache record",
        cache._plexoraArray === loaded._array && cache._plexoraFormat === "u8",
        "the pixels belong to the cached tile, not to either Tile object");

    const pass = drawing();
    const rendered = renderedContext();
    const twin = tile({ cache, array: null, format: null });
    const record = { color: { r: 255, g: 0, b: 0 }, range: [0, 1] };
    pass.run({ tiledImage: { source: LAYER_SOURCE(record) }, tile: twin, rendered });

    check("...and the second item of a pair draws from it",
        pass.drawn.length === 1 && twin._array === loaded._array,
        "OSD raises tile-loaded with a request for only the first of the two");
    check("...rather than blanking the canvas its twin just filled",
        !rendered.calls.includes("fill:black"),
        "a black blank on every frame reads as a layer that never appears");
}

{
    // And with nothing anywhere to draw from, a layer tile must cost the
    // picture nothing -- NOT a black rectangle, which the clearing blit would
    // read as full coverage and punch the image below straight out.
    const pass = drawing();
    const rendered = renderedContext();
    const record = { color: { r: 255, g: 0, b: 0 }, range: [0, 1] };
    pass.run({
        tiledImage: { source: LAYER_SOURCE(record) },
        tile: tile({ array: null, format: null, cache: {} }),
        rendered,
    });
    check("a layer tile with no pixels yet clears instead of going black",
        rendered.calls.includes("clear")
        && !rendered.calls.some((c) => c.startsWith("fill:"))
        && pass.drawn.length === 0,
        "an opaque blank would take the image underneath away for that frame");
    check("...and says so, so the next frame tries again",
        rendered._plexoraSig === null,
        "a kept signature would make the miss permanent");
}


console.log(failures.length ? `\n${failures.length} check(s) failed`
                            : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
