/**
 * The transcript layer's arithmetic, run.
 *
 * Four things in this plugin's client are wrong in ways no Python test and no
 * `node --check` can see, and every one of them produces a picture that looks
 * like data rather than like a bug:
 *
 *   **The record stride.** The wire format is 11 bytes for a molecule and 14
 *   for an aggregate, both packed. Decoded at the wrong one every point lands
 *   somewhere else on the slide, plausibly distributed, with nothing on
 *   screen to say so.
 *
 *   **The tile set.** Fetch one tile too few and the edge of the view is
 *   empty; fetch the whole grid and a pan is forty megabytes.
 *
 *   **The LOD.** Points mode stays points at every zoom and merges molecules
 *   instead, so what the level decides is how much of the slide one dot
 *   stands for. Choose it a step too fine and a whole-slide view is millions
 *   of dots; a step too coarse and a scatter is a lattice. It is also where
 *   the two cache properties diverge -- a level-0 tile is good for any gene
 *   selection and an aggregate is not -- and a tag that does not carry the
 *   selection is a gene that goes on being drawn after it was switched off.
 *
 *   **The gene groups.** They are the whole of what makes a forty-gene
 *   selection readable, and they are pure bookkeeping over arrays.
 *
 * Run directly:  node tests/js/transcript_points_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/transcripts/static");

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, URLSearchParams,
    Uint8Array, Uint16Array, Float32Array, DataView, ArrayBuffer,
    encodeURIComponent,
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    fetch: async () => ({ ok: false }),
    document: { getElementById: () => null, querySelectorAll: () => [],
                createElement: () => ({ style: {}, classList: { add() {} },
                                        getContext: () => null, remove() {} }) },
    window: { devicePixelRatio: 2, addEventListener() {},
              setTimeout: () => 1, clearTimeout: () => {},
              requestAnimationFrame: () => 1 },
});

// Core's ramps and gradient control, which base.html loads before any plugin
// script and which `TranscriptLayer.RAMPS` now reads from rather than keeping
// a second copy of. slider.js first, in base.html's order: the gradient's two
// handles are one of its range sliders.
for (const name of ["views/slider.js", "views/gradientRange.js", "views/geneList.js"]) {
    runInContext(readFileSync(join(REPO, "plexora/client/src/js", name), "utf8"),
                 ctx, { filename: name });
}

for (const name of ["transcriptsApi.js", "transcriptPoints.js",
                    "transcriptLayer.js"]) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

const failures = [];
function check(what, condition, detail = "") {
    const ok = Boolean(condition);
    if (!ok) failures.push(`${what}${detail ? ` -- ${detail}` : ""}`);
    console.log(`${ok ? "PASS" : "FAIL"} ${what}${detail ? `  ${detail}` : ""}`);
}

const { TranscriptLayer, TranscriptPointRenderer } = ctx;


// -- the wire format ------------------------------------------------------

{
    // Three records, written exactly as the server writes them.
    const points = [
        { gene: 0, x: 1.5, y: 2.5, q: 40 },
        { gene: 7, x: 1000.25, y: 30.75, q: 19 },
        { gene: 511, x: 44000, y: 26000, q: 255 },
    ];
    const buffer = new ArrayBuffer(points.length * 11);
    const view = new DataView(buffer);
    points.forEach((point, index) => {
        const at = index * 11;
        view.setUint16(at, point.gene, true);
        view.setFloat32(at + 2, point.x, true);
        view.setFloat32(at + 6, point.y, true);
        view.setUint8(at + 10, point.q);
    });

    const packed = TranscriptPointRenderer.repack(buffer);
    const floats = new Float32Array(packed.data);
    const shorts = new Uint16Array(packed.data);
    const bytes = new Uint8Array(packed.data);

    check("every record is decoded", packed.count === 3, String(packed.count));
    check("the GPU stride is 16, not the wire's 11",
        TranscriptPointRenderer.STRIDE === 16
        && packed.data.byteLength === 48,
        "a float attribute has to sit on a 4-byte boundary, and the padding "
        + "is paid here rather than on 19 million rows of disk");

    const ok = points.every((point, index) =>
        floats[index * 4] === point.x
        && floats[index * 4 + 1] === point.y
        && shorts[index * 8 + 4] === point.gene
        && bytes[index * 16 + 10] === point.q);
    check("gene, position and quality all survive the repack", ok,
        `${floats[4]},${shorts[12]},${bytes[26]}`);
    check("a gene index past 255 is not truncated",
        shorts[2 * 8 + 4] === 511,
        "the index is u2 because a 5000-gene panel exists");
    check("a molecule stands for exactly one molecule",
        [0, 1, 2].every((index) => floats[index * 4 + 3] === 1),
        "the shader sizes every point by the square root of this, so a raw "
        + "molecule has to come out at exactly the size the slider asks for");
}


// -- the wire format, aggregated ------------------------------------------

{
    // 14 bytes, packed: u2 gene, f4 x, f4 y, u4 count. What
    // `transcript_tiles.AGGREGATE_DTYPE` writes.
    const groups = [
        { gene: 3, x: 10.5, y: 20.25, count: 1 },
        { gene: 3, x: 900.5, y: 40.75, count: 274 },
        { gene: 4, x: 1.25, y: 2.5, count: 70_000 },
    ];
    const buffer = new ArrayBuffer(groups.length * 14);
    const view = new DataView(buffer);
    groups.forEach((group, index) => {
        const at = index * 14;
        view.setUint16(at, group.gene, true);
        view.setFloat32(at + 2, group.x, true);
        view.setFloat32(at + 6, group.y, true);
        view.setUint32(at + 10, group.count, true);
    });

    const packed = TranscriptPointRenderer.repack(buffer, true);
    const floats = new Float32Array(packed.data);
    const shorts = new Uint16Array(packed.data);
    const bytes = new Uint8Array(packed.data);

    check("an aggregate tile decodes at 14 bytes a record",
        packed.count === 3, String(packed.count));
    check("gene, position and count all survive the repack",
        groups.every((group, index) =>
            floats[index * 4] === group.x
            && floats[index * 4 + 1] === group.y
            && shorts[index * 8 + 4] === group.gene
            && floats[index * 4 + 3] === group.count),
        `${floats[7]},${floats[11]}`);
    check("a count past 65,535 is not truncated",
        floats[2 * 4 + 3] === 70_000,
        "the count is u4 because one bin of a whole-slide view holds a lot");
    check("an aggregate passes whatever quality threshold is set",
        [0, 1, 2].every((index) => bytes[index * 16 + 10] === 255),
        "the server applied the threshold BEFORE merging, so what came back "
        + "has already passed it -- and the shader would otherwise discard it");
}


// -- the layer ------------------------------------------------------------

function layerWith(manifest, state = {}) {
    const layer = new TranscriptLayer({
        datasource: "demo",
        url: (path) => `/${path}`,
        layers: { viewport: () => null },
    }, "tx");
    layer.manifest = { status: "ready", ...manifest };
    Object.assign(layer.state, state);
    return layer;
}

const GRID = {
    tile_size: 100, columns: 10, rows: 10,
    width: 1000, height: 1000,
    // Ten bins across a tile, so a bin is 10 image pixels at level 0 and the
    // arithmetic below can be checked by hand. The server sends 64.
    aggregate_bins: 10,
    genes: ["A", "B", "C"],
    gene_counts: [100, 800, 100],
    point_count: 1000,
    // One row of ten tiles holding everything, so a view's estimate is easy
    // to reason about by hand.
    tile_counts: Array.from({ length: 10 }, (_, y) =>
        Array.from({ length: 10 }, () => (y === 0 ? 100 : 0))),
};


// -- which tiles a view needs ---------------------------------------------

{
    const layer = layerWith(GRID);
    const keys = layer.tilesInView({ minX: 250, minY: 250, maxX: 340, maxY: 340 });

    // The view spans tiles 2..3 in each axis, and the ring makes it 1..4.
    check("the view's own tiles are fetched, plus one ring",
        keys.length === 16 && keys.includes("2_2") && keys.includes("3_3")
        && keys.includes("1_1") && keys.includes("4_4")
        && !keys.includes("5_5"),
        keys.join(" "));
    check("the ring stops at the edge of the grid",
        layerWith(GRID).tilesInView({ minX: 0, minY: 0, maxX: 10, maxY: 10 })
            .every((key) => key.split("_").every((n) => Number(n) >= 0)),
        "a negative tile address is a 404 on every pan from the top-left");

    // A LEVEL-L TILE COVERS 2^L OF THEM, which is the whole reason a
    // zoomed-out view costs about the same as a zoomed-in one. Without it
    // the fetch grows as the square of how far out the user goes, and a
    // whole-slide view is the entire cache over the wire.
    const whole = { minX: 0, minY: 0, maxX: 1000, maxY: 1000 };
    check("a coarser level covers the same view in fewer tiles",
        layer.tilesInView(whole, 2).length < layer.tilesInView(whole, 0).length,
        `${layer.tilesInView(whole, 0).length} -> `
        + `${layer.tilesInView(whole, 2).length}`);
    check("the coarsest level is a single tile plus its ring",
        layer.tilesInView(whole, layer.maxLevel()).length === 1,
        "one tile covers the layer, so there is no ring to fetch");
    check("a bin doubles in image pixels with every level",
        layer.binPixelsAt(0) === 10 && layer.binPixelsAt(3) === 80);
}


// -- how many molecules that is -------------------------------------------

{
    const all = layerWith(GRID, { selected: ["A", "B", "C"] });
    const one = layerWith(GRID, { selected: ["A"] });
    //: The whole of the one row that holds anything -- ten tiles of a
    //: hundred pixels.
    const bounds = { minX: 0, minY: 0, maxX: 1000, maxY: 100 };

    check("the estimate reads the manifest rather than fetching",
        all.estimateInView(bounds) === 1000, String(all.estimateInView(bounds)));
    // A VIEW INSIDE ONE TILE COUNTS ITS SHARE OF IT, and counting the whole
    // tile had a floor nobody could get under: on a slide whose busiest tile
    // holds 80,000 molecules the estimate never dropped however far in the
    // user zoomed, so individual molecules could not be drawn at 40x -- the
    // view stayed density and the panel said "too many to draw".
    check("a view inside one tile counts its share of it",
        all.estimateInView({ minX: 0, minY: 0, maxX: 25, maxY: 50 }) === 12.5,
        String(all.estimateInView({ minX: 0, minY: 0, maxX: 25, maxY: 50 })));
    check("...and a view over two tiles counts a share of each",
        all.estimateInView({ minX: 50, minY: 0, maxX: 150, maxY: 100 }) === 100,
        "half of tile 0 and half of tile 1, which between them is one tile");
    check("it is scaled by the SELECTION's own share of the panel",
        one.estimateInView(bounds) === 100,
        "one rare gene out of five hundred has to draw as points, not fall "
        + "back to density because the panel as a whole is dense");
    check("a gene switched off does not count",
        layerWith(GRID, { selected: ["A", "B"], hidden: ["B"] })
            .estimateInView(bounds) === 100);
    check("nothing selected estimates nothing",
        layerWith(GRID).estimateInView(bounds) === 0);
}


// -- how big one dot is, which is a question about the zoom --------------

{
    const layer = layerWith(GRID, { selected: ["A"] });
    const full = layer.fullZoom();
    const MIN = TranscriptPointRenderer.MIN_DOT;

    check("at full zoom the slider is taken literally",
        layer.restingSize(full) === layer.baseSize()
        && layer.restingSize(full * 8) === layer.baseSize(),
        "past the zoom where a dot is one molecule there is nothing left "
        + "to resolve, so the size stops growing");
    check("and pulling back shrinks it",
        [full, full / 4, full / 16, full / 64].every(
            (zoom, index, all) => index === 0
                || layer.restingSize(zoom) < layer.restingSize(all[index - 1])),
        [full, full / 4, full / 16, full / 64]
            .map((zoom) => layer.restingSize(zoom).toFixed(2)).join(" "));
    check("...by a HALF power, so four-fold out is half the dot",
        Math.abs(layer.restingSize(full / 4)
                 / layer.restingSize(full) - 0.5) < 1e-9,
        "shrinking in proportion to the zoom hits the floor after about one "
        + "screenful and stays there, which throws away the whole "
        + "progression from one cell to a whole section");
    check("and it stops at a pixel rather than vanishing",
        layer.restingSize(full / 1e6) === MIN,
        "a dot nobody can see is not restraint, it is an empty overlay");
    check("the slider still moves the whole range",
        layerWith(GRID, { selected: ["A"], size: 14 }).restingSize(full)
            > layerWith(GRID, { selected: ["A"], size: 3 }).restingSize(full),
        "zoom-dependent scaling goes ON TOP of the user's setting, not "
        + "instead of it");

    // THE REGRESSION THIS PASS EXISTS FOR. A dot held at the slider's size
    // in SCREEN pixels, spaced just far enough to clear its neighbours,
    // covers the same fraction of the screen at every zoom -- so a whole
    // section came out as a lattice of discs with the tissue invisible
    // under it. Asserted as ink: how much of the patch a dot stands for it
    // actually puts colour on.
    const zooms = [full, full / 4, full / 16, full / 64, full / 256];
    check("a dot is a speck in the patch it stands for AT EVERY ZOOM",
        zooms.every((zoom) => {
            const level = layer.pickLevel(
                { minX: 0, minY: 0, maxX: 1000 / zoom, maxY: 1000 / zoom },
                1000);
            if (level >= layer.maxLevel()) return true;
            return layer.restingSize(zoom)
                / (layer.binPixelsAt(level) * zoom) < 0.3;
        }),
        "under a tenth of the ground even where every bin is full, which "
        + "is a tint of gene colour rather than a mat of circles -- and it "
        + "has to hold at every zoom, because a dot 62% of the way to its "
        + "neighbour reads as a disc laid over the tissue however small "
        + "the disc is");
    check("the floor on the bin is the rule applied to the smallest dot",
        TranscriptLayer.AGGREGATE_SPACING
            === MIN * TranscriptPointRenderer.MAX_GROWTH
                * TranscriptLayer.AGGREGATE_SPREAD,
        "a floor that disagreed with the rule it floors is a bin size "
        + "nothing chose");

    const lift = layer.baseSize() * TranscriptPointRenderer.EMPHASIS_SCALE;
    check("but a hovered gene is lifted clear of the field at every zoom",
        lift >= layer.restingSize(full) * 1.5
        && lift >= layer.restingSize(full / 64) * 4,
        "1.8x of a one-pixel resting dot is two pixels, which nobody could "
        + "find -- the lift comes off the size the SLIDER says so that one "
        + "gene stays traceable across a whole section");
}


// -- how far out the view is, and what one dot then stands for -----------

{
    const layer = layerWith(GRID, { selected: ["A", "B", "C"] });
    const whole = { minX: 0, minY: 0, maxX: 1000, maxY: 1000 };
    const level = (width) => layer.pickLevel(
        { minX: 0, minY: 0, maxX: width, maxY: width }, 1000);

    // THE RULE, not a number: the level chosen is the FINEST whose bin is
    // wide enough on screen to give the dots room -- the widest a dot can
    // be, times AGGREGATE_SPREAD. Asserted as a property over a range of
    // zooms rather than as a handful of expected levels, so the numbers can
    // be retuned without the test having to be rewritten to agree.
    //
    // The room a dot needs is read AT THAT ZOOM, because the dot shrinks
    // with it: the same view under a fixed-size dot would merge harder and
    // draw fewer, heavier dots, which is the thing this replaced.
    const spacing = (zoom) => Math.max(TranscriptLayer.AGGREGATE_SPACING,
        layer.dotSize(zoom) * TranscriptLayer.AGGREGATE_SPREAD);
    const widths = [400, 700, 1000, 1800, 4000, 9000, 30000];
    check("every level chosen gives the dots the room they need",
        widths.every((width) => {
            const chosen = level(width);
            const zoom = 1000 / width;
            return chosen === 0 || chosen === layer.maxLevel()
                || layer.binPixelsAt(chosen) * zoom >= spacing(zoom);
        }),
        widths.map((w) => `${w}:${level(w)}`).join(" "));
    check("...and none coarser than they need",
        widths.every((width) => {
            const chosen = level(width);
            const zoom = 1000 / width;
            return chosen === 0
                || layer.binPixelsAt(chosen - 1) * zoom < spacing(zoom);
        }),
        "a level coarser than the crowding calls for throws away detail "
        + "that would have cost nothing to draw");
    check("the level never goes finer than the molecules themselves",
        level(100) === 0,
        "there is no level below 0 and nothing to merge at that zoom");
    check("zooming out never returns a finer level",
        [1000, 1500, 2500, 4000, 8000, 30000].every(
            (width, index, all) => index === 0
                || level(width) >= level(all[index - 1])),
        [1000, 1500, 2500, 4000, 8000, 30000].map(level).join(" "));
    check("the level stops at the one tile that holds everything",
        level(1e9) === layer.maxLevel(), String(level(1e9)));

    // A BIGGER DOT NEEDS MORE ROOM -- less than proportionally, because the
    // fade takes a square root of it too, so the whole range of the slider
    // moves the level by a couple of steps rather than continuously.
    const wide = { minX: 0, minY: 0, maxX: 2000, maxY: 2000 };
    layer.state.size = 2;
    const small = layer.pickLevel(wide, 1000);
    layer.state.size = 40;
    check("a larger point size asks for more room between dots",
        layer.pickLevel(wide, 1000) > small,
        `${small} -> ${layer.pickLevel(wide, 1000)}`);
    layer.state.size = 6;
}


// -- the budget, which now coarsens rather than changing the picture ------

{
    const layer = layerWith(GRID, { selected: ["A"] });
    const whole = { minX: 0, minY: 0, maxX: 1000, maxY: 1000 };
    // Four million molecules of one gene, all in the top row of tiles.
    layer.manifest.point_count = 4_000_000;
    layer.manifest.gene_counts = [4_000_000, 0, 0];
    layer.manifest.tile_counts = Array.from({ length: 10 }, (_, y) =>
        Array.from({ length: 10 }, () => (y === 0 ? 400_000 : 0)));

    check("far more molecules than the frame budget allows",
        layer.estimateInView(whole) > TranscriptLayer.MAX_POINTS_ON_SCREEN);
    check("a level's dot count is capped by its BINS, not by its molecules",
        layer.estimateAggregates(whole, 1) === 2500,
        "a bin can only ever contribute one dot per gene, and a 1000-pixel "
        + "view at 20-pixel bins is 50 by 50 of them");
    check("the cap is per gene and not in total",
        layerWith({ ...layer.manifest, gene_counts: [2e6, 1e6, 1e6] },
                  { selected: ["A", "B", "C"] })
            .estimateAggregates(whole, 1) === 7500,
        "three genes over the same bins is up to three dots a bin");

    // THE REPLACEMENT FOR THE DENSITY FALLBACK. The zoom asks for level 0
    // here and level 0 would be four million dots, so the answer is to merge
    // harder -- not to draw something else.
    check("a selection too big to draw is merged harder",
        layer.pickLevel(whole, 1000) === 1,
        String(layer.pickLevel(whole, 1000)));
    check("...and what it is merged into fits the budget",
        layer.estimateAggregates(whole, layer.pickLevel(whole, 1000))
            <= TranscriptLayer.MAX_POINTS_ON_SCREEN);
}


// -- points is points, and density is asked for ---------------------------

{
    const layer = layerWith(GRID, { selected: ["A"] });
    // Fifty million molecules of one gene in view: whatever the old coverage
    // test would have said, Points mode draws points.
    layer.manifest.point_count = 50_000_000;
    layer.manifest.gene_counts = [50_000_000, 0, 0];
    check("no number of molecules turns Points into a density map",
        layer.showsDensity() === false,
        "zooming out used to change what was being shown rather than how "
        + "finely, which is the one thing a level of detail must not do");

    layer.state.viewAs = "density";
    check("density is drawn when it is asked for and only then",
        layer.showsDensity() === true);

    // The switch itself, which is the bug the user reported: asking for
    // density and then asking for points again has to end with the raster
    // gone, and the other way round.
    const shown = [];
    layer.visible = true;
    layer._density = { setVisible: (on) => shown.push(["density", on]),
                       setOpacity() {}, setStyle() {} };
    layer.renderer = { setVisible: (on) => shown.push(["points", on]),
                       invalidate() {}, setGeneTable() {}, set() {} };
    layer.showMode(true);
    check("density up means points down",
        shown.some(([what, on]) => what === "points" && on === false)
        && shown.some(([what, on]) => what === "density" && on === true));
    shown.length = 0;
    layer.showMode(false);
    check("...and points up means density down",
        shown.some(([what, on]) => what === "density" && on === false)
        && shown.some(([what, on]) => what === "points" && on === true),
        "switching back has to REMOVE the raster, not leave it under the dots");

    // A SELECTION WITH EVERY EYE OFF DRAWS NOTHING. Points empty themselves
    // -- they are drawn per gene -- but the density raster is one summed
    // field, and the server reads "no genes named" as the whole panel: right
    // for a panel nobody has picked from, and exactly wrong for one somebody
    // has just switched off. The heading's eye made that one click away.
    shown.length = 0;
    layer.state.selected = ["A"];
    layer.setAllGenesHidden(true);
    layer.showMode(true);
    check("every gene off means the density map comes down too",
        shown.some(([what, on]) => what === "density" && on === false),
        "or turning your genes off makes the map BRIGHTER: 480 of them");
}


// -- the whole list at once -----------------------------------------------
//
// The two buttons in the Genes heading. Both are toggles that read their next
// state off the layer, so a list changed from anywhere else cannot leave them
// arguing with what is under them.

{
    const layer = layerWith(GRID, { selected: ["A", "B", "C"] });

    check("nothing is hidden to begin with", !layer.allGenesHidden());
    layer.setAllGenesHidden(true);
    check("one click takes every gene off",
        layer.allGenesHidden() && layer.drawnGenes().length === 0
        && layer.state.hidden.length === 3);
    layer.setAllGenesHidden(false);
    check("...and the next one puts them all back",
        !layer.allGenesHidden() && layer.state.hidden.length === 0,
        "hiding is not removing -- a gene keeps its colour and its place");

    const empty = layerWith(GRID, {});
    check("a list with nothing in it is not 'all hidden'",
        !empty.allGenesHidden(),
        "or the heading's eye would offer to show genes that do not exist");
}

{
    const layer = layerWith(GRID, {
        selected: ["A", "B"],
        groups: [{ name: "Tumour", genes: ["A"] }, { name: "Stroma", genes: ["B"] }],
    });

    check("no group starts rolled up", !layer.allCollapsed());
    layer.collapseAll(true);
    check("collapse all rolls up every group",
        layer.allCollapsed() && layer.isCollapsed("Tumour")
        && layer.isCollapsed("Stroma"));
    layer.setGroupCollapsed("Tumour", false);
    check("...and one of them opening is enough to un-say it",
        !layer.allCollapsed() && layer.isCollapsed("Stroma"));
    layer.collapseAll(false);
    check("expand all opens every one", layer.state.collapsed.length === 0);

    layer.collapseAll(true);
    layer.deleteGroup("Stroma");
    check("a deleted group takes its fold with it",
        !layer.isCollapsed("Stroma"),
        "or a group made again under the same name comes back rolled up");

    const none = layerWith(GRID, { selected: ["A"] });
    check("a list with no groups is not 'all collapsed'",
        !none.allCollapsed(),
        "the heading's chevron is hidden then, and must not claim otherwise");
}


// -- what a cached tile is good for ---------------------------------------

{
    const layer = layerWith(GRID, { selected: ["A", "B"], minQ: 20 });

    // LEVEL 0 HOLDS EVERY GENE AND EVERY SCORE, so one of those tiles is
    // good for any selection and any threshold -- which is what keeps
    // toggling a gene free at the zoom where somebody is comparing genes
    // molecule by molecule.
    const raw = layer.tagFor(0);
    const aggregate = layer.tagFor(2);
    layer.state.hidden = ["B"];
    check("a level-0 tile survives a change of selection",
        layer.tagFor(0) === raw);
    check("an aggregate does not",
        layer.tagFor(2) !== aggregate,
        "a count is a count OF the genes that were asked for, so a tile for "
        + "two genes is not the tile for one of them");

    layer.state.hidden = [];
    layer.state.minQ = 25;
    check("nor does it survive a change of threshold",
        layer.tagFor(2) !== aggregate,
        "a count is a count of what survived the filter");
    check("...while level 0 still does",
        layer.tagFor(0) === raw,
        "the score is in the record and the shader discards, which is the "
        + "whole reason the byte is there");
}


// -- keeping the useful tiles and forgetting the rest ---------------------

{
    const layer = layerWith(GRID, { selected: ["A"] });
    const dropped = [];
    layer.renderer = { dropTile: (key) => dropped.push(key), invalidate() {},
                       setActive() {}, setTile() {} };
    const current = layer.signature();
    layer.tiles.set("0#0_0", { level: 0, tag: "0", signature: "" });
    layer.tiles.set("2|gone#0_0",
                    { level: 2, tag: "2|gone", signature: "B|20" });
    layer.tiles.set(`3|${current}#0_0`,
                    { level: 3, tag: `3|${current}`, signature: current });
    layer.evict([]);

    check("a tile for a selection that is gone is dropped",
        !layer.tiles.has("2|gone#0_0") && dropped.includes("2|gone#0_0"),
        "nothing can ever draw it again, whatever the view does next");
    check("another LEVEL of the current selection is kept",
        layer.tiles.has(`3|${current}#0_0`),
        "zooming back out has to find its tiles already uploaded");
    check("and so is level 0, which is good for anything",
        layer.tiles.has("0#0_0"));
}


// -- and not holding more of them than fits -------------------------------

{
    const layer = layerWith(GRID, { selected: ["A"] });
    layer.renderer = { dropTile() {}, invalidate() {}, setActive() {},
                       setTile() {}, setGeneTable() {}, set() {} };
    const current = layer.signature();
    // Eight megabytes each: eleven of them is past the budget and none of
    // them is on screen.
    for (let n = 0; n < 11; n += 1) {
        layer.tiles.set(`0#${n}_0`, { level: 0, tag: "0", signature: "",
                                      data: new ArrayBuffer(8 * 1024 * 1024) });
    }
    layer.evict([]);

    // A TILE COUNT IS NOT A MEMORY BUDGET: eleven tiles is well under
    // MAX_TILES, and on a dense section eleven level-0 tiles is most of a
    // graphics card.
    const held = [...layer.tiles.values()]
        .reduce((sum, tile) => sum + tile.data.byteLength, 0);
    check("the cache is trimmed by weight and not only by count",
        layer.tiles.size < 11
        && held <= TranscriptLayer.MAX_TILE_BYTES,
        `${layer.tiles.size} tiles, ${(held / 1048576).toFixed(0)} MB`);
    check("...oldest first",
        !layer.tiles.has("0#0_0") && layer.tiles.has("0#10_0"),
        "insertion order is the closest thing to a use order there is here");
}


// -- switching level without a blank frame --------------------------------

{
    const layer = layerWith(GRID, { selected: ["A"] });
    const active = [];
    layer.renderer = { setActive: (tag) => active.push(tag), invalidate() {},
                       dropTile() {}, setTile() {}, setGeneTable() {}, set() {} };

    layer.pending.add("2|x#0_0");
    layer.settle(["2|x#0_0", "2|x#1_0"], 2, "2|x");
    check("a level is not shown until every tile it needs has arrived",
        active.length === 0 && layer.drawnTag === null,
        "switching early leaves a frame with nothing drawn on it, and "
        + "drawing both levels at once doubles every dot where they overlap");

    layer.pending.delete("2|x#0_0");
    layer.settle(["2|x#0_0", "2|x#1_0"], 2, "2|x");
    check("...and is shown the moment they have",
        active[0] === "2|x" && layer.drawnTag === "2|x"
        && layer.drawnLevel === 2);
    active.length = 0;
    layer.settle(["2|x#0_0"], 2, "2|x");
    check("settling again on the same level is not a switch",
        active.length === 0, "every completed fetch calls this");
}


// -- size is a weak channel, on purpose -----------------------------------

{
    // Mirrors the vertex shader's growth. Written out here because the
    // shader cannot be run in node and the NUMBERS are the thing at risk:
    // the first version of this was sqrt(count), which is correct as an
    // encoding and turned a whole-slide view of an abundant gene into a mat
    // of overlapping bubbles with the tissue invisible under it.
    const grow = (count) => Math.min(
        1 + TranscriptPointRenderer.AGGREGATE_GROWTH * Math.log2(count),
        TranscriptPointRenderer.MAX_GROWTH);

    check("a dot standing for a thousand molecules is not a bubble",
        grow(1000) <= 1.7 && grow(1000) > 1,
        `${grow(1000).toFixed(2)}x the radius of one standing for a single `
        + "molecule -- more, and the transcript layer stops being an overlay "
        + "on the tissue and becomes the picture");
    check("crossing a level does not visibly jump the dot size",
        grow(4) / grow(1) < 1.25 && grow(400) / grow(100) < 1.25,
        "a level boundary quadruples the count, and under sqrt that DOUBLED "
        + "every radius -- a visible step at every stage of a zoom");
    check("but more molecules is still visibly more",
        grow(64) > grow(1) * 1.2,
        "subtle is not the same as absent");
}


// -- hovering a gene picks it out of the field ----------------------------

{
    const layer = layerWith(GRID, { selected: ["A", "B"] });
    const tables = [];
    const pushed = [];
    layer.renderer = {
        setGeneTable: (entries) => tables.push(entries),
        set: (values) => pushed.push(values),
        invalidate() {}, setActive() {}, dropTile() {}, setTile() {},
    };

    layer.emphasize(["B"]);
    const table = tables.at(-1);
    check("the hovered gene is marked in the table the shader already reads",
        table[GRID.genes.indexOf("B")].emphasis === true
        && table[GRID.genes.indexOf("A")].emphasis === false,
        "no tile is refetched, no aggregation recomputed and no geometry "
        + "rebuilt -- a hover is a few hundred bytes of texture and one "
        + "uniform, which is what lets the pointer run down a 480-gene list");
    check("...and the lift is switched on as a fraction, not as a size",
        pushed.at(-1).emphasis === 1,
        "the renderer eases this from 0 to 1 and reads the SIZE off the "
        + "slider, so somebody at size 2 and somebody at size 14 get the "
        + "same relative jump and neither gets a hard-coded pixel count");

    const written = tables.length;
    layer.emphasize([]);
    check("leaving the row eases the lift back rather than snapping",
        pushed.at(-1).emphasis === 0 && tables.length === written,
        "the mask is deliberately LEFT in place so the shrink can animate; "
        + "a mask with no lift behind it draws exactly as no mask at all");

    layer.emphasize(["A", "B"]);
    check("a group lifts every gene in it",
        tables.at(-1).filter((entry) => entry.emphasis).length === 2);
}


// -- but a gene that is everywhere is not lifted into a flood fill --------

{
    const bounds = { minX: 0, minY: 0, maxX: 1000, maxY: 1000 };
    // A million molecules over the whole grid, all but a hundred of them
    // one gene -- which at 20-pixel bins is a dot in every one of the 2500
    // bins in view, against a hundred scattered among them.
    const build = (hovered, level = 1) => {
        const layer = layerWith({
            ...GRID,
            point_count: 1_000_000,
            gene_counts: [999_900, 100, 0],
            tile_counts: Array.from({ length: 10 }, () =>
                Array.from({ length: 10 }, () => 10_000)),
        }, { selected: ["A", "B"] });
        layer.ctx.layers.viewport = () => bounds;
        layer.drawnLevel = level;
        layer.emphasized = new Set([hovered]);
        return layer;
    };

    check("a sparse gene keeps the whole bin when it is hovered",
        build("B").emphasisFill() > 0.95,
        "the case hover exists for -- separate dots against a field of "
        + "specks, and nothing to give back");
    check("...and one with a dot in every bin gives most of it back",
        Math.abs(build("A").emphasisFill()
                 - (1 - TranscriptLayer.EMPHASIS_CROWDING)) < 1e-9,
        "lifting a gene expressed everywhere to the full bin paints the "
        + "section solid and answers the question by erasing the picture it "
        + "was asked about -- a stipple says 'everywhere' just as well and "
        + "leaves the tissue on screen");
    check("and at level 0 there is no bin to give back",
        build("A", 0).emphasisFill() === 1,
        "a molecule stands for itself, so nothing can be flooded");
    check("nothing hovered asks nothing of the estimate",
        (() => { const l = build("A"); l.emphasized = new Set();
                 return l.emphasisFill() === 1; })(),
        "the common case is no hover at all, and it must not cost a pass "
        + "over the manifest on every style change");
}


// -- and the no-WebGL2 path draws the same picture ------------------------

{
    // A fallback that sized its dots differently would make what the
    // picture MEANS depend on which browser was open, so the 2-D path runs
    // the same arithmetic in image units that the shader runs in screen
    // ones. Nothing else here executes a line of it.
    const layer = layerWith(GRID, { selected: ["A", "B"] });
    const buffer = new ArrayBuffer(2 * 14);
    const view = new DataView(buffer);
    [[0, 10, 10], [1, 20, 20]].forEach(([gene, x, y], index) => {
        const at = index * 14;
        view.setUint16(at, gene, true);
        view.setFloat32(at + 2, x, true);
        view.setFloat32(at + 6, y, true);
        view.setUint32(at + 10, 1, true);
    });
    const packed = TranscriptPointRenderer.repack(buffer, true);
    layer.tiles.set("3|x#0_0", { ...packed, tag: "3|x", level: 3 });
    layer.drawnTag = "3|x";
    layer.drawnLevel = 3;

    // Far enough out that the fade has bottomed out on MIN_DOT.
    const zoom = 0.05;
    const radii = () => {
        const seen = [];
        layer.drawFallback({
            px: 1 / zoom,
            context: {
                globalAlpha: 1, fillStyle: "",
                beginPath() {}, moveTo() {}, fill() {},
                arc: (x, y, r) => seen.push(+r.toFixed(6)),
            },
        });
        return seen;
    };

    const rest = +(layer.restingSize(zoom) * 0.475 / zoom).toFixed(6);
    check("the fallback fades its dot with the zoom exactly as the shader does",
        radii().every((r) => r === rest),
        `${radii().join(" ")} against ${rest}`);

    layer.emphasize(["A"]);
    const lifted = radii();
    check("...and lifts a hovered gene off the slider's size, held to the bin",
        lifted[1] === Math.min(
            layer.baseSize() * TranscriptPointRenderer.EMPHASIS_SCALE
                * 0.475 / zoom,
            layer.binPixelsAt(3) / 2 * layer.emphasisFill())
        && lifted[0] === rest,
        lifted.join(" "));
    check("...drawn LAST, so it lands on top of the field it is picked out of",
        lifted[1] > lifted[0],
        "an enlarged dot with a neighbour's small one punched out of its "
        + "middle reads as a ring, not as a highlight");
    layer.emphasize([]);
}


// -- and the lift is animated, not switched -------------------------------

{
    // No viewer, so no WebGL context and no canvas -- which is exactly the
    // state the ease has to survive, since it runs off the draw loop.
    const renderer = new TranscriptPointRenderer({ canvas: null });
    check("a renderer with nowhere to draw reports itself unsupported",
        renderer.isSupported() === false);

    renderer.set({ emphasis: 1 });
    check("setting the target does not move the value",
        renderer.emphasis === 0,
        "a jump straight to the target is the thing the ease exists to avoid");

    const frame = () => { renderer._eased = 0; renderer.ease(); };
    frame();
    const first = renderer.emphasis;
    check("one frame moves part of the way",
        first > 0 && first < 1, String(first.toFixed(3)));

    for (let n = 0; n < 40; n += 1) frame();
    check("and it settles rather than creeping",
        renderer.emphasis === 1,
        "an ease that never reaches its target repaints for ever");

    renderer.set({ emphasis: 0 });
    for (let n = 0; n < 40; n += 1) frame();
    check("...both ways", renderer.emphasis === 0);
}


// -- how big a dot standing for a bin is allowed to get -------------------

{
    const layer = layerWith(GRID, { selected: ["A"] });
    const pushed = [];
    layer.renderer = { set: (values) => pushed.push(values), setGeneTable() {},
                       setActive() {}, invalidate() {}, dropTile() {},
                       setTile() {} };

    layer.applyStyle();
    check("a molecule stands only for itself and is not capped",
        pushed.at(-1).binPixels === 0);
    check("and the zoom the fade is anchored to goes over with it",
        pushed.at(-1).fullZoom === layer.fullZoom()
        && layer.fullZoom() > 0,
        "the renderer refades the dot every frame and cannot ask the "
        + "manifest how big a level-0 bin is");

    layer.settle([], 3, "3|x");
    check("an aggregate is capped at the bin it stands for",
        pushed.at(-1).binPixels === 80
        && pushed.at(-1).binPixels === layer.binPixelsAt(3),
        "area-proportional sizing conserves ink per molecule, so on an "
        + "abundant gene at whole-slide zoom the dots are BOUND to overrun "
        + "their bins -- held to the bin that is a field of touching dots, "
        + "unheld it is a smear with crescents where one colour cuts another");
    check("the cap follows the level that is on screen, not the one being fetched",
        layer.drawnLevel === 3,
        "until settle the old picture is still up, and capping it to the new "
        + "bin would resize every dot a frame before its data arrived");
}


// -- the density style string ----------------------------------------------

{
    const layer = layerWith(GRID, {
        selected: ["A", "B"], colors: { A: "#ff0000", B: "#00ff00" }, minQ: 25,
    });
    const style = layer.densityStyle();

    check("the selection and its colours ride the tile URL",
        style.includes("genes=A,B") && style.includes("colors=ff0000,00ff00"),
        style);
    check("the quality floor is minq and never q",
        style.includes("minq=25") && !/(^|&)q=/.test(style),
        "`q` is already the encoding-quality parameter on that route, and the "
        + "collision would have silently switched the tiles to lossless");
    layer.setGeneHidden("B", true);
    check("a gene switched off leaves the density too",
        !layer.densityStyle().includes("A,B"), layer.densityStyle());
}


// -- genes and groups ------------------------------------------------------

{
    const layer = layerWith(GRID);
    layer.addGene("A");
    layer.addGene("B");
    layer.addGene("A");

    check("a gene is added once", layer.state.selected.join(",") === "A,B");
    check("each gets its own colour from the palette",
        layer.colorFor("A") !== layer.colorFor("B"),
        `${layer.colorFor("A")} / ${layer.colorFor("B")}`);

    layer.createGroup("T cells");
    layer.assignToGroup("A", "T cells");
    check("a group holds what was put in it",
        layer.state.groups[0].genes.join(",") === "A");
    check("the tree's top level is what is in no group",
        layer.ungrouped().join(",") === "B", layer.ungrouped().join(","));

    layer.removeGene("A");
    check("removing a gene takes it out of its group too",
        layer.state.groups[0].genes.length === 0,
        "a group counting a gene nobody can see is a count nobody can explain");

    layer.deleteGroup("T cells");
    check("deleting a group keeps its genes selected",
        layer.state.groups.length === 0 && layer.state.selected.join(",") === "B");

    layer.addGene("C");
    layer.setColor("B", "#123456");
    layer.setIcon("C", "star");
    layer.resetAppearance();
    check("reset puts the palette and the positional icons back",
        layer.colorFor("B") === TranscriptLayer.PALETTE[0]
        && !layer.state.icons.C,
        layer.colorFor("B"));
}


// -- the state that is saved ------------------------------------------------

{
    const layer = layerWith(GRID);
    const keys = Object.keys(TranscriptLayer.defaultState()).sort().join(",");

    check("the saved state is everything the panel can change",
        keys === "binMicrons,collapsed,colormap,colors,densityHigh,"
               + "densityLow,groups,hidden,icons,minQ,opacity,pointStyle,"
               + "selected,size,viewAs",
        keys);
    check("the quality floor defaults to Xenium's own recommendation",
        TranscriptLayer.defaultState().minQ === 20);
    check("the layer does not start at full strength",
        TranscriptLayer.defaultState().opacity === 0.6,
        "a ramp that paints every bin and REPLACES what is under it is opaque "
        + "paint over the morphology at full strength");
    check("a bin starts one step up the ladder, not at the bottom",
        TranscriptLayer.defaultState().binMicrons === 40
        && TranscriptLayer.BIN_LADDER.includes(40),
        "20 microns is about two cells, and a grid that fine reads as texture "
        + "rather than as boxes somebody can point at");

    Object.assign(layer.state, { selected: ["A"], minQ: 33, viewAs: "density" });
    const restored = layerWith(GRID);
    Object.assign(restored.state, JSON.parse(JSON.stringify(layer.state)));
    check("it round-trips through JSON unchanged",
        JSON.stringify(restored.state) === JSON.stringify(layer.state));
}



// -- the glyphs are one size ------------------------------------------------
//
// The claim the icon control rests on: a shape covers the same area as the dot
// it replaces, so one slider means one thing whichever style is on. Before
// this the radii were chosen by eye and were not close -- an equal-area
// triangle reaches 1.55x as far as the dot and the one in the shader reached
// 0.66x, which on screen is "Icons made everything smaller and the size
// slider will not fix it".
//
// Tested against the GLSL THE GPU ACTUALLY GETS, not against a second copy of
// the shapes: `glyphSource()` is parsed, each branch's expression is
// translated into JavaScript, and the coverage is integrated over the sprite.
// A shape written one way in the table and another in the shader would fail
// here, which is the whole reason the shader is generated from the table.
{
    const source = TranscriptPointRenderer.glyphSource();
    const branches = new Map();
    for (const line of source.split("\n")) {
        const hit = /if \(icon == (\d+)\) return (.+?);\s*\/\//.exec(line);
        if (hit) branches.set(Number(hit[1]), hit[2]);
        const fallback = /^ {4}return (.+?);\s*\/\//.exec(line);
        if (fallback) branches.set(0, fallback[1]);
    }

    //: GLSL to JavaScript in one pass, so nothing a substitution introduced
    //: can be substituted again. `band` is the antialiased step the shader
    //: uses; inside/outside is all an area needs.
    const NAMES = {
        band: "BAND(", length: "LEN(", atan: "ATAN(",
        max: "Math.max(", min: "Math.min(",
        abs: "Math.abs(", cos: "Math.cos(",
    };
    const compile = (expr) => new Function(
        "p", "r", "BAND", "LEN", "ATAN",
        `return ${expr.replace(/\b(band|length|atan|max|min|abs|cos)\(/g,
                               (_m, name) => NAMES[name])};`);

    const BAND = (d) => (d < 0 ? 1 : 0);
    const LEN = (v) => Math.hypot(v.x, v.y);
    const ATAN = (a, b) => Math.atan2(a, b);

    const target = Math.PI * 0.95 * 0.95;
    const STEPS = 601;
    TranscriptPointRenderer.GEOMETRY.forEach((entry, icon) => {
        const drawn = compile(branches.get(icon));
        const span = entry.span;
        let inside = 0;
        let furthest = 0;
        for (let iy = 0; iy < STEPS; iy += 1) {
            const y = -span + (2 * span * (iy + 0.5)) / STEPS;
            for (let ix = 0; ix < STEPS; ix += 1) {
                const x = -span + (2 * span * (ix + 0.5)) / STEPS;
                // The fragment shader flips y and derives `r` by rotating p
                // 45 degrees; both are reproduced here because they are the
                // sprite's frame rather than any one shape's.
                const p = { x, y };
                const r = { x: (x + y) * 0.7071068, y: (x - y) * 0.7071068 };
                if (drawn(p, r, BAND, LEN, ATAN) > 0.5) {
                    inside += 1;
                    furthest = Math.max(furthest, Math.abs(x), Math.abs(y));
                }
            }
        }
        const area = ((2 * span) ** 2 * inside) / (STEPS * STEPS);
        check(`a ${entry.name} covers what the dot covers`,
            Math.abs(area - target) / target < 0.015,
            `${area.toFixed(3)} against ${target.toFixed(3)}`);
        // And the sprite is big enough to hold it. A shape that reached the
        // sprite's edge would be drawn with its corners cut off, which is a
        // different bug with the same symptom.
        check(`...and fits in the sprite it is drawn into`,
            furthest <= span + 1e-6 && furthest > span * 0.8,
            `reaches ${furthest.toFixed(3)} of ${span.toFixed(3)}`);
    });

    check("the dot itself is untouched",
        TranscriptPointRenderer.GEOMETRY[0].span === 1,
        "a sprite the size of the point size, as it always was");
}


// -- the density map's own controls -----------------------------------------

// HOW THE RASTER MEETS THE IMAGE UNDER IT, which is the other half of the
// server's decision to paint every bin: a ramp covers the layer edge to edge,
// so it has to replace what it covers, and the gaps between its boxes are the
// only place the morphology comes through.
{
    const layer = layerWith(GRID);

    check("a ramp REPLACES what is under it",
        layer.densityBlend() === "source-over",
        "added with `lighter` instead, a dark end painted over every empty "
        + "bin would be a flat wash on top of the whole slide");

    layer.state.colormap = TranscriptLayer.GENE_COLOURS;
    check("...and a gene-per-colour composite ADDS to it",
        layer.densityBlend() === "lighter",
        "that picture is a handful of coloured clouds on nothing, which is a "
        + "fluorescence channel and composites like one");
}


{
    const layer = layerWith({ ...GRID, pixel_size: 0.2125, density_stretch: 8 },
                            { selected: ["A"], colors: { A: "#ff0000" } });

    layer.state.binMicrons = 20;
    check("a bin size is asked in microns and sent in pixels",
        layer.binPixels() === 94,
        `20um at 0.2125um/px -> ${layer.binPixels()}px`);

    layer.state.binMicrons = 40;
    check("...and doubling the microns doubles the pixels",
        layer.binPixels() === 188, String(layer.binPixels()));

    const bare = layerWith({ ...GRID }, {});
    check("a project with no pixel size reads the slider as pixels",
        bare.pixelSize() === null && bare.binPixels() === 40,
        "rather than converting with a made-up scale");

    const style = new URLSearchParams(layer.densityStyle());
    check("the bin size rides the tile url", style.get("bin") === "188");
    check("...with the ramp", style.get("ramp") === "viridis");
    check("...and the window as fractions",
        style.get("dlo") === "0.0000" && style.get("dhi") === "1.0000",
        "fractions, because the count that means dense quadruples per level");

    // A tile is cached for a year and its ETag names the project and the
    // style, not the code that drew the pixels -- so without this a change to
    // the rasterizer is invisible until somebody empties their cache.
    const versioned = layerWith(
        { ...GRID, pixel_size: 0.2125, version: "20260919_gene_list_actions" }, {});
    check("the plugin's version rides the tile url too",
        new URLSearchParams(versioned.densityStyle()).get("v")
            === "20260919_gene_list_actions",
        "the only thing that reaches a cache entry nothing revalidates is a "
        + "different url");

    layer.state.colormap = TranscriptLayer.GENE_COLOURS;
    const perGene = new URLSearchParams(layer.densityStyle());
    check("one colour per gene sends no ramp at all",
        perGene.get("ramp") === null && perGene.get("colors") === "ff0000",
        "the server keeps drawing the composite rather than being handed a "
        + "ramp name it has to know to ignore");

    // THE LEGEND HAS TO AGREE WITH THE PICTURE. Both ends come out of the
    // same arithmetic the server stretches against -- 8x the average bin --
    // with the multiple sent in the manifest rather than written twice.
    layer.state.colormap = "viridis";
    layer.state.binMicrons = 20;
    const range = layer.densityRange();
    // 8x the average bin, and the average bin of THIS SELECTION -- gene A is
    // 100 of the panel's 1000 molecules. Without the share the window is the
    // whole panel's, three genes out of 480 reach two levels out of 255, and
    // the ramp draws its dark end over the entire slide.
    const expected = 8 * (1000 / (1000 * 1000)) * 94 * 94 * 0.1;
    check("the ramp's legend is the window the server uses",
        Math.abs(range.high - expected) < 1e-6,
        `${range.high.toFixed(3)} molecules per bin`);
    check("...scaled by what the selection is worth",
        layer.selectedShare() === 0.1,
        "gene A is a tenth of this panel");
    layer.state.selected = ["A", "B", "C"];
    check("...and the whole panel is the whole window",
        layer.selectedShare() === 1
        && Math.abs(layer.densityRange().high - expected * 10) < 1e-6);
    layer.state.selected = ["A"];
    layer.state.densityLow = 0.25;
    layer.state.densityHigh = 0.5;
    const narrowed = layer.densityRange();
    check("...and narrowing the threshold moves both ends",
        Math.abs(narrowed.low - expected * 0.25) < 1e-6
        && Math.abs(narrowed.high - expected * 0.5) < 1e-6);

}


// -- the ramps are the server's ---------------------------------------------
//
// The panel draws the swatch and the server draws the tile, so the anchors
// are written twice. Two copies of a palette drift; this is what stops them.
{
    const python = readFileSync(
        join(REPO, "plexora/server/utils/colormaps.py"), "utf8");
    const body = python.slice(python.indexOf("RAMPS = {"),
                              python.indexOf("DEFAULT_RAMP"));
    for (const [name, stops] of Object.entries(TranscriptLayer.RAMPS)) {
        const entry = new RegExp(`"${name}": \\(([^)]*)\\)`, "s").exec(body);
        const theirs = entry
            ? (entry[1].match(/#[0-9a-f]{6}/g) || []).join(",") : "";
        check(`${name} is the same ramp on both sides`,
            theirs === stops.join(","), theirs || "not found in colormaps.py");
    }
}


console.log(failures.length ? `\n${failures.length} FAILED` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
