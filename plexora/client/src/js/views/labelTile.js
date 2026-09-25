/**
 * renderLabelTile -- one decoded segmentation tile, one cell layer, one canvas.
 *
 * Extracted verbatim from imageViewer.js, where it lived as a closure inside the
 * constructor. Nothing about what it draws changed in the move; the one closed-over
 * value it read off `this` (the datasource's segmentationMode) is now the last
 * argument, which is also what makes it testable without building a viewer.
 *
 * The label layer is NOT drawn by the WebGL shader. handleTileLoaded runs every
 * label tile through here once per drawn layer into tile._layerContexts, and the
 * tile-drawing handler blits those canvases in stack order -- so this is the only
 * place cell boundaries are derived for a datasource storing filled labels
 * (segmentationMode === "filled"), and the only place per-cell colour reaches the
 * mask.
 *
 * `layer` is one entry of the cell-layer registry, or coreLayerView() when no
 * plugin has registered one -- it carries the gate, the colours and the mode this
 * pass draws with. Passed in rather than read off a viewer because several layers
 * are rendered from the SAME decoded tile, once each, and they must not see each
 * other's state.
 *
 * Served as a classic script (see base.html) and must load BEFORE imageViewer.js.
 */

//: Outlines stop being readable once most of a cell is outline: at 8 px across
//: 44% of its pixels are boundary, at 4 px 75%, and a whole-slide view of a
//: Visium HD run is a carpet of nothing else. Measured per tile, as the share
//: of labelled pixels on a boundary -- which, because OSD picks the level
//: whose pixels are about the screen's, is how small the cells are ON SCREEN.
//: Up to OUTLINE_READABLE the tile is outlines; from OUTLINE_UNREADABLE it is
//: each cell's colour as a translucent fill; between, the one fades into the
//: other, so neighbouring tiles of different density do not meet at a seam.
const OUTLINE_READABLE = 0.5;
const OUTLINE_UNREADABLE = 0.75;
//: The fill's share of the layer's alpha. A tint over the tissue, not paint
//: on it: the morphology is what the cells are being looked at against.
const SMALL_CELL_TINT = 0.45;

/** Eight-neighbour boundary test on unpacked ids; the tile's own border counts
 *  as "same" (see the comment at the call site in renderLabelTile). */
function isBoundary(ids, p, width, height) {
    const cellId = ids[p];
    const x = p % width;
    const y = (p - x) / width;
    const up = y > 0;
    const down = y < height - 1;
    const left = x > 0;
    const right = x < width - 1;
    return (left && ids[p - 1] !== cellId)
        || (right && ids[p + 1] !== cellId)
        || (up && ids[p - width] !== cellId)
        || (down && ids[p + width] !== cellId)
        || (up && left && ids[p - width - 1] !== cellId)
        || (up && right && ids[p - width + 1] !== cellId)
        || (down && left && ids[p + width - 1] !== cellId)
        || (down && right && ids[p + width + 1] !== cellId);
}

/** How far this tile has gone from outlines (0) to fill (1): the share of its
 *  labelled pixels on a boundary, mapped between the two thresholds. Every
 *  cell counts, drawn or not -- the question is how small cells are here, and
 *  hiding most of a legend does not make them bigger. Sampled on every other
 *  row and column, a quarter of the work for the same answer. */
function smallCellWeight(ids, width, height) {
    let labelled = 0;
    let boundary = 0;
    for (let y = 0; y < height; y += 2) {
        for (let x = 0; x < width; x += 2) {
            const p = y * width + x;
            if (!ids[p]) continue;
            labelled += 1;
            if (isBoundary(ids, p, width, height)) boundary += 1;
        }
    }
    if (!labelled) return 0;
    const share = boundary / labelled;
    const t = (share - OUTLINE_READABLE) / (OUTLINE_UNREADABLE - OUTLINE_READABLE);
    return Math.min(1, Math.max(0, t));
}

function renderLabelTile(tileArray, width, height, layer, segmentationMode) {
    const allowedIds = layer.filterIds;
    // Per-cell colour from the plugin owning this layer, or null for the
    // plain white layer this has always drawn. Read once per tile rather
    // than per pixel, and destructured so the inner loop does no
    // property lookups at all.
    const lut = layer.lut;
    const lutColors = lut?.colors || null;
    const lutMaxId = lutColors ? lut.maxId : 0;
    const lutMap = lutColors ? null : (lut?.map || null);
    // Filled draws the whole cell rather than its boundary. Only
    // possible where the labels are stored whole -- a pyramid that was
    // pre-reduced to outlines has no interior pixels to paint, so there
    // is nothing to fill and asking for it changes nothing.
    const fillMode = layer.mode === "filled"
        && segmentationMode === "filled";
    // Datasources imported with "Outline cells while viewing" store the
    // labels whole rather than pre-reduced to boundaries (config
    // segmentationMode = "filled"), so the boundary is derived here --
    // once per tile at load, not per frame. Everything else arrives
    // already reduced, where every labelled pixel *is* an outline.
    // Filled skips the derivation entirely, which is also the cheaper
    // path: no neighbour unpacking and no eight-way test per pixel.
    const deriveOutlines = segmentationMode === "filled" && !fillMode;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    const imageData = context.createImageData(width, height);
    const output = imageData.data;

    // Unpacked up front so the neighbour lookups below are plain array
    // reads instead of re-decoding four bytes per tap.
    let ids = null;
    if (deriveOutlines) {
        ids = new Uint32Array(width * height);
        for (let p = 0; p < ids.length; p += 1) {
            const i = p * 4;
            ids[p] = tileArray[i]
                + tileArray[i + 1] * 256
                + tileArray[i + 2] * 65536
                + tileArray[i + 3] * 16777216;
        }
    }
    // 0 where the cells are big enough to outline, 1 where they are a few
    // screen pixels and only a fill can say anything -- see OUTLINE_READABLE.
    const fill = ids ? smallCellWeight(ids, width, height) : 0;

    for (let i = 0; i < tileArray.length; i += 4) {
        const cellId = ids
            ? ids[i >> 2]
            : tileArray[i]
                + tileArray[i + 1] * 256
                + tileArray[i + 2] * 65536
                + tileArray[i + 3] * 16777216;
        if (!cellId || (allowedIds && !allowedIds.has(cellId))) continue;
        // White at the layer's long-standing alpha unless a plugin says
        // otherwise. Looked up BEFORE the boundary derivation below: a
        // hidden category or a cell with no value is skipped here, so it
        // never pays for the eight-neighbour test -- which is what makes
        // hiding most of a legend faster to draw, not slower.
        let red = 255;
        let green = 255;
        let blue = 255;
        let alpha = 220;
        if (lutColors) {
            // Above maxId means the LUT simply does not describe this
            // cell -- a segmentation object with no row in the table.
            // Transparent, like every other kind of no-value.
            if (cellId > lutMaxId) continue;
            const offset = cellId * 4;
            alpha = lutColors[offset + 3];
            if (!alpha) continue;
            red = lutColors[offset];
            green = lutColors[offset + 1];
            blue = lutColors[offset + 2];
        } else if (lutMap) {
            const entry = lutMap.get(cellId);
            if (!entry || !entry[3]) continue;
            [red, green, blue, alpha] = entry;
        }
        if (ids) {
            const p = i >> 2;
            // Eight-neighbour, matching the "exact" method the offline
            // pyramid writer uses (segmentation_pyramid.py). Four-
            // neighbour leaves cells that meet only corner-to-corner
            // sharing an unbroken block of white, which reads as one
            // large cell; measured against a precomputed pyramid of the
            // same mask it also drew ~28% fewer boundary pixels overall.
            //
            // Compared against raw ids, not the gated subset: a cell's
            // edge against a filtered-out neighbour is still its edge.
            // A tile's own border counts as "same" rather than as a
            // boundary -- treating it as one would draw a bright grid
            // along every tile seam. The cost is that a real boundary
            // landing exactly on a seam loses that pixel; a cell simply
            // continuing across the seam stays correct, which is the
            // overwhelmingly common case.
            const tint = fill ? Math.round(alpha * SMALL_CELL_TINT * fill) : 0;
            if (!isBoundary(ids, p, width, height)) {
                if (!tint) continue;
                alpha = tint;
            } else if (fill) {
                // The outline fades out as the fill fades in, and never
                // draws fainter than the fill it sits on.
                alpha = Math.max(tint, Math.round(alpha * (1 - fill)));
            }
            if (!alpha) continue;
        }
        output[i] = red;
        output[i + 1] = green;
        output[i + 2] = blue;
        output[i + 3] = alpha;
    }
    context.putImageData(imageData, 0, 0);
    return context;
}

// Classic script, like every other file under client/src/js served straight to
// the page. The module shape is for the node probes, which load this file into a
// vm context and read the global back out.
if (typeof window !== "undefined") {
    window.PlexoraLabelTile = { renderLabelTile };
}
if (typeof globalThis !== "undefined" && !globalThis.PlexoraLabelTile) {
    globalThis.PlexoraLabelTile = { renderLabelTile };
}
