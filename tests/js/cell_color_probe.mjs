/**
 * Runs imageViewer.js's renderLabelTile() with a per-cell colour lookup table.
 *
 * Two things are being pinned here, and the first matters more than the second.
 *
 * **Nothing changes when no plugin colours cells.** The cell layer predates any
 * of this: Thresholding draws white outlines through the same function, and a
 * plain viewer with no tool open draws them too. So the null-LUT output is
 * asserted BYTE FOR BYTE against the pixels the old code produced -- not "looks
 * about right", not a count of drawn pixels, the actual RGBA bytes. A colour
 * path that quietly shifts white outlines to 254 or drops their alpha from 220
 * would pass every other check in this file.
 *
 * **A LUT colours cells without touching geometry.** Same boundary pixels, new
 * colours; alpha 0 removes a cell entirely, which is how both "hidden category"
 * and "no value for this cell" are expressed.
 *
 * The function is extracted from the real source rather than reimplemented -- a
 * copy would happily pass while the shipped code was wrong.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(
    path.join(here, "..", "..", "plexora", "client", "src", "js", "views", "imageViewer.js"),
    "utf8",
);

const start = source.indexOf("const renderLabelTile = (tileArray, width, height, layer) => {");
if (start < 0) throw new Error("renderLabelTile not found in imageViewer.js");
const end = source.indexOf("\n        };", start);
if (end < 0) throw new Error("could not find the end of renderLabelTile");
const body = source.slice(start, end + "\n        };".length);

function fakeDocument() {
    return {
        createElement() {
            return {
                width: 0,
                height: 0,
                getContext() {
                    return {
                        createImageData(w, h) {
                            return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) };
                        },
                        putImageData(imageData) {
                            this.imageData = imageData;
                        },
                    };
                },
            };
        },
    };
}

/**
 * The renderer, bound to one layer.
 *
 * Everything the pass depends on except the pyramid's own storage mode lives on
 * the LAYER record now, not on the viewer -- which is what lets several layers
 * be rendered from one decoded tile without seeing each other's colours. The one
 * thing still read off the viewer is `config.segmentationMode`, because that is
 * a fact about the file rather than about any layer.
 */
function makeRenderer({ segmentationMode, filterIds = null, lut = null, mode = "outlines" }) {
    const self = { config: { segmentationMode } };
    const factory = new Function(
        "self", "document",
        `${body.replace("const renderLabelTile =", "const fn =").replace(/\bthis\./g, "self.")}
         return fn;`,
    );
    const fn = factory(self, fakeDocument());
    const layer = { name: "probe", lut, filterIds, mode };
    return (tileArray, width, height) => fn(tileArray, width, height, layer);
}

/** Filled label tile of abutting `cell`-sized squares, packed the way the tile
 *  route packs uint32 cell IDs into RGBA bytes. */
function filledTile(width, height, cell) {
    const array = new Uint8Array(width * height * 4);
    for (let y = 0; y < height; y += 1) {
        for (let x = 0; x < width; x += 1) {
            const id = 1 + Math.floor(y / cell) * Math.ceil(width / cell) + Math.floor(x / cell);
            const i = (y * width + x) * 4;
            array[i] = id & 255;
            array[i + 1] = (id >> 8) & 255;
            array[i + 2] = (id >> 16) & 255;
            array[i + 3] = (id >> 24) & 255;
        }
    }
    return array;
}

const pixels = (context) => context.imageData.data;

const drawn = (context, width, height) => {
    const set = new Set();
    const data = pixels(context);
    for (let p = 0; p < width * height; p += 1) {
        if (data[p * 4 + 3] !== 0) set.add(p);
    }
    return set;
};

const colorAt = (context, width, x, y) => {
    const data = pixels(context);
    const i = (y * width + x) * 4;
    return [data[i], data[i + 1], data[i + 2], data[i + 3]];
};

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const W = 64, H = 64, CELL = 16;
const tile = filledTile(W, H, CELL);
//: 4x4 abutting cells at this tile size, so ids 1..16 are all present.
const CELL_COUNT = (W / CELL) * (H / CELL);

// --------------------------------------------------------------------------
// The gating guard: no LUT means the exact pixels the old code wrote.
// --------------------------------------------------------------------------

/** What renderLabelTile produced before a colour path existed: opaque-ish white
 *  on every pixel it decided to draw. Written independently of the shipped
 *  code's colour variables so a regression in those cannot hide in here. */
function whiteReference(context, width, height) {
    const data = pixels(context);
    const expected = new Uint8ClampedArray(width * height * 4);
    for (let p = 0; p < width * height; p += 1) {
        const i = p * 4;
        if (data[i + 3] === 0) continue;      // not drawn -- stays zeroed
        expected[i] = 255;
        expected[i + 1] = 255;
        expected[i + 2] = 255;
        expected[i + 3] = 220;
    }
    return expected;
}

const sameBytes = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

for (const segmentationMode of ["filled", "outlines"]) {
    const context = makeRenderer({ segmentationMode })(tile, W, H);
    const data = pixels(context);
    check(`null LUT is byte-identical white on a "${segmentationMode}" pyramid`,
        sameBytes(data, whiteReference(context, W, H)),
        `${drawn(context, W, H).size} pixels drawn`);
}

check("null LUT never draws a pixel the old code left alone",
    drawn(makeRenderer({ segmentationMode: "filled" })(tile, W, H), W, H).size
    === drawn(makeRenderer({ segmentationMode: "filled", mode: "centroids" })(tile, W, H), W, H).size,
    "centroids mode must not change what the label layer draws");

// --------------------------------------------------------------------------
// Dense LUT
// --------------------------------------------------------------------------

/** Dense RGBA table over ids 0..maxId. `colorFor(id)` returns [r,g,b,a]. */
function denseLUT(maxId, colorFor) {
    const colors = new Uint8Array(4 * (maxId + 1));
    for (let id = 0; id <= maxId; id += 1) {
        const [r, g, b, a] = colorFor(id);
        colors[id * 4] = r;
        colors[id * 4 + 1] = g;
        colors[id * 4 + 2] = b;
        colors[id * 4 + 3] = a;
    }
    return { colors, maxId };
}

//: Cell 1 red, cell 2 green, everything else blue. Alpha deliberately not 220,
//: so a renderer ignoring the LUT's alpha shows up.
const paletteLUT = denseLUT(CELL_COUNT, (id) => {
    if (id === 0) return [0, 0, 0, 0];
    if (id === 1) return [255, 0, 0, 200];
    if (id === 2) return [0, 255, 0, 200];
    return [0, 0, 255, 200];
});

const outlinedWhite = makeRenderer({ segmentationMode: "filled" })(tile, W, H);
const outlinedColor = makeRenderer({ segmentationMode: "filled", lut: paletteLUT })(tile, W, H);

const sameSet = (a, b) => a.size === b.size && [...a].every((v) => b.has(v));

//: Cell 1's right edge, against cell 2. Deliberately not (0,0): a tile's own
//: border counts as "same" so that seams do not draw a grid, so the top-left
//: corner is interior and nothing is drawn there.
const CELL_1_EDGE = [CELL - 1, 5];

check("colouring draws exactly the same boundary pixels as white did",
    sameSet(drawn(outlinedWhite, W, H), drawn(outlinedColor, W, H)),
    "geometry must not depend on colour");

check("cell 1's outline takes its colour from the LUT",
    String(colorAt(outlinedColor, W, ...CELL_1_EDGE)) === String([255, 0, 0, 200]),
    `got ${colorAt(outlinedColor, W, ...CELL_1_EDGE)}`);

check("cell 2's outline takes a different colour",
    String(colorAt(outlinedColor, W, CELL, 0)) === String([0, 255, 0, 200]),
    `got ${colorAt(outlinedColor, W, CELL, 0)}`);

check("a stored-outlines pyramid colours every labelled pixel",
    String(colorAt(makeRenderer({ segmentationMode: "outlines", lut: paletteLUT })(tile, W, H),
        W, 8, 8)) === String([255, 0, 0, 200]),
    "cell interiors ARE the outline for these");

// --------------------------------------------------------------------------
// Alpha 0 -- hidden category, and cell with no value
// --------------------------------------------------------------------------

const hiddenFirst = denseLUT(CELL_COUNT, (id) =>
    (id === 1 ? [255, 0, 0, 0] : [0, 0, 255, 200]));
const withHidden = makeRenderer({ segmentationMode: "filled", lut: hiddenFirst })(tile, W, H);

check("an alpha-0 cell is not drawn at all",
    colorAt(withHidden, W, ...CELL_1_EDGE)[3] === 0,
    `got alpha ${colorAt(withHidden, W, ...CELL_1_EDGE)[3]}`);

check("hiding one cell leaves its neighbours untouched",
    String(colorAt(withHidden, W, CELL, 0)) === String([0, 0, 255, 200]),
    `got ${colorAt(withHidden, W, CELL, 0)}`);

check("a hidden cell's neighbour keeps the edge they share",
    // The boundary comes from the raw ids, exactly as it does for gating: a
    // cell's edge against a hidden neighbour is still its edge.
    drawn(withHidden, W, H).has(10 * W + CELL),
    "left edge of cell 2, against hidden cell 1");

//: A LUT that stops short of the ids in the tile -- a segmentation object with
//: no row in the table. Transparent, not black, and not a crash.
const shortLUT = denseLUT(2, (id) => (id === 0 ? [0, 0, 0, 0] : [255, 0, 0, 200]));
const withShort = makeRenderer({ segmentationMode: "filled", lut: shortLUT })(tile, W, H);
let beyondDrawn = 0;
for (const p of drawn(withShort, W, H)) {
    const x = p % W;
    const y = (p - x) / W;
    const id = 1 + Math.floor(y / CELL) * (W / CELL) + Math.floor(x / CELL);
    if (id > 2) beyondDrawn += 1;
}
check("a cell the LUT does not describe stays transparent",
    beyondDrawn === 0 && drawn(withShort, W, H).size > 0,
    `${beyondDrawn} pixels drawn for unmapped cells`);

// --------------------------------------------------------------------------
// Sparse LUT
// --------------------------------------------------------------------------

const sparseLUT = { map: new Map([[1, [255, 0, 0, 200]], [2, [0, 255, 0, 0]]]) };
const withSparse = makeRenderer({ segmentationMode: "filled", lut: sparseLUT })(tile, W, H);

check("the sparse form colours a mapped cell",
    String(colorAt(withSparse, W, ...CELL_1_EDGE)) === String([255, 0, 0, 200]),
    `got ${colorAt(withSparse, W, ...CELL_1_EDGE)}`);
check("the sparse form hides an alpha-0 entry",
    colorAt(withSparse, W, CELL, 0)[3] === 0);
check("the sparse form skips cells it has no entry for",
    drawn(withSparse, W, H).size
    === drawn(makeRenderer({
        segmentationMode: "filled",
        lut: denseLUT(1, (id) => (id === 1 ? [255, 0, 0, 200] : [0, 0, 0, 0])),
    })(tile, W, H), W, H).size,
    "only cell 1 should draw either way");

// --------------------------------------------------------------------------
// Filled mode
// --------------------------------------------------------------------------

const filled = makeRenderer({
    segmentationMode: "filled", lut: paletteLUT, mode: "filled",
})(tile, W, H);

check("filled mode paints every labelled pixel",
    drawn(filled, W, H).size === W * H, `${drawn(filled, W, H).size}/${W * H}`);

check("filled mode paints interiors in the cell's colour",
    String(colorAt(filled, W, 8, 8)) === String([255, 0, 0, 200]),
    `centre of cell 1: ${colorAt(filled, W, 8, 8)}`);

check("filled mode strictly outdraws outline mode",
    drawn(filled, W, H).size > drawn(outlinedColor, W, H).size,
    `${drawn(filled, W, H).size} vs ${drawn(outlinedColor, W, H).size}`);

const filledHidden = makeRenderer({
    segmentationMode: "filled", lut: hiddenFirst, mode: "filled",
})(tile, W, H);
check("filled mode still hides an alpha-0 cell entirely",
    colorAt(filledHidden, W, 8, 8)[3] === 0
    && drawn(filledHidden, W, H).size === W * H - CELL * CELL,
    `${drawn(filledHidden, W, H).size} of ${W * H} drawn`);

// The fallback that keeps the control honest: a pyramid whose labels were
// pre-reduced to boundaries has no interior pixels, so "filled" is a request
// there is nothing to satisfy. It must render as it always did rather than
// producing something different-but-wrong.
const filledOnStored = makeRenderer({
    segmentationMode: "outlines", lut: paletteLUT, mode: "filled",
})(tile, W, H);
const outlinesOnStored = makeRenderer({
    segmentationMode: "outlines", lut: paletteLUT,
})(tile, W, H);
check("filled on a stored-outlines pyramid falls back to what it can draw",
    sameBytes(pixels(filledOnStored), pixels(outlinesOnStored)),
    "identical output, since every labelled pixel is already a boundary");

// --------------------------------------------------------------------------
// Gating and colouring are separate channels
// --------------------------------------------------------------------------

const gatedAndColored = makeRenderer({
    segmentationMode: "filled", lut: paletteLUT, filterIds: new Set([1, 2]),
})(tile, W, H);
let outsideGate = 0;
for (const p of drawn(gatedAndColored, W, H)) {
    const x = p % W;
    const y = (p - x) / W;
    const id = 1 + Math.floor(y / CELL) * (W / CELL) + Math.floor(x / CELL);
    if (id > 2) outsideGate += 1;
}
check("a gate still restricts drawing while a LUT supplies colours",
    outsideGate === 0 && drawn(gatedAndColored, W, H).size > 0,
    `${outsideGate} pixels outside the gate`);
check("gated cells keep their LUT colours",
    String(colorAt(gatedAndColored, W, ...CELL_1_EDGE)) === String([255, 0, 0, 200]),
    `got ${colorAt(gatedAndColored, W, ...CELL_1_EDGE)}`);

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
