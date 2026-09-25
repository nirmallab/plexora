/**
 * Runs labelTile.js's renderLabelTile() against synthetic label tiles.
 *
 * The label layer is NOT drawn by the WebGL shader: handleTileLoaded() renders
 * every tile through renderLabelTile() once per drawn layer into
 * tile._layerContexts, and the tile-drawing handler blits those canvases, so
 * frag.glsl's u32_rgba_map branch is unreachable for tileFormat 32. That makes
 * this function the only place cell
 * boundaries can be derived for a datasource storing filled labels
 * (segmentationMode = "filled"), and the only place worth testing them.
 *
 * The real views/labelTile.js is loaded and run, not reimplemented -- a copy
 * would happily pass while the shipped code was wrong.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const SOURCE = path.join(here, "..", "..", "plexora", "client", "src", "js", "views", "labelTile.js");

// Minimal stand-ins for the two DOM objects the function touches.
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

// The shipped module, run as the page runs it -- a classic script that hangs its
// exports off the global. Loading the real file rather than slicing a function
// body out of it is what keeps this probe honest: a copy would happily pass while
// the shipped code was wrong.
const sandbox = { document: fakeDocument(), Uint8ClampedArray, Uint32Array, Uint8Array, console };
sandbox.globalThis = sandbox;
createContext(sandbox);
runInContext(readFileSync(SOURCE, "utf8"), sandbox, { filename: SOURCE });
const { renderLabelTile } = sandbox.PlexoraLabelTile;

/** The renderer, bound to one layer -- the record the real one is handed per
 *  pass, carrying that layer's gate, colours and mode. */
function makeRenderer({ segmentationMode, filterIds = null }) {
    const layer = { name: "probe", lut: null, filterIds, mode: "outlines" };
    return (tileArray, width, height) =>
        renderLabelTile(tileArray, width, height, layer, segmentationMode);
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

const drawn = (context, width, height) => {
    const set = new Set();
    const { data } = context.imageData;
    for (let p = 0; p < width * height; p += 1) {
        if (data[p * 4 + 3] !== 0) set.add(p);
    }
    return set;
};

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const W = 64, H = 64, CELL = 16;
const tile = filledTile(W, H, CELL);

const outlined = drawn(makeRenderer({ segmentationMode: "filled" })(tile, W, H), W, H);
const asStored = drawn(makeRenderer({ segmentationMode: "outlines" })(tile, W, H), W, H);

check("outline mode leaves the tile untouched (every labelled pixel drawn)",
    asStored.size === W * H, `${asStored.size}/${W * H}`);
check("filled mode draws strictly fewer pixels",
    outlined.size > 0 && outlined.size < asStored.size, `${outlined.size} vs ${asStored.size}`);

/** Independent reference: written as plain nested x/y loops so it shares none
 *  of the shipped code's flat-index arithmetic, which is the part most likely
 *  to be subtly wrong. Eight-neighbour, matching the offline writer's "exact"
 *  method. Out-of-tile neighbours count as "same", matching the
 *  no-grid-along-seams rule. */
function referenceOutline(array, width, height, allowed = null) {
    const idAt = (x, y) => {
        const i = (y * width + x) * 4;
        return array[i] + array[i + 1] * 256 + array[i + 2] * 65536 + array[i + 3] * 16777216;
    };
    const expected = new Set();
    for (let y = 0; y < height; y += 1) {
        for (let x = 0; x < width; x += 1) {
            const id = idAt(x, y);
            if (!id || (allowed && !allowed.has(id))) continue;
            const neighbours = [];
            for (let dy = -1; dy <= 1; dy += 1) {
                for (let dx = -1; dx <= 1; dx += 1) {
                    if (dx === 0 && dy === 0) continue;
                    const nx = x + dx;
                    const ny = y + dy;
                    if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
                    neighbours.push(idAt(nx, ny));
                }
            }
            if (neighbours.some((other) => other !== id)) expected.add(y * width + x);
        }
    }
    return expected;
}

const sameSet = (a, b) => a.size === b.size && [...a].every((v) => b.has(v));

check("matches an independently computed boundary, pixel for pixel",
    sameSet(outlined, referenceOutline(tile, W, H)),
    `${outlined.size} drawn vs ${referenceOutline(tile, W, H).size} expected`);

// Interiors must be gone.
let interiorDrawn = 0;
for (let y = 2; y < CELL - 2; y += 1) {
    for (let x = 2; x < CELL - 2; x += 1) {
        if (outlined.has(y * W + x)) interiorDrawn += 1;
    }
}
check("cell interiors are cleared", interiorDrawn === 0, `${interiorDrawn} interior pixels drawn`);

// The tile's own border must not become an outline, or every seam shows a grid.
// Here the top-left cell's edge genuinely lies along it, so probe a row that is
// mid-cell: column 8 of the top edge is cell interior continuing off-tile.
check("a tile edge mid-cell is not drawn as a boundary",
    !outlined.has(0 * W + 8) && !outlined.has((H - 1) * W + 8),
    "top/bottom edge at x=8");

// Boundaries between two *different* cells must survive.
check("the seam between two cells is drawn",
    outlined.has(10 * W + (CELL - 1)) && outlined.has(10 * W + CELL),
    "both sides of the vertical seam");

// Gating filters which cells draw, but boundaries still come from raw ids.
const gated = drawn(
    makeRenderer({ segmentationMode: "filled", filterIds: new Set([1]) })(tile, W, H), W, H);
check("gating restricts drawing to selected cells",
    sameSet(gated, referenceOutline(tile, W, H, new Set([1]))), `${gated.size} drawn`);
check("a gated cell keeps the edge it shares with a filtered-out neighbour",
    gated.has(5 * W + (CELL - 1)), "right edge of cell 1");

// Cells meeting only corner-to-corner. Build a tile that is entirely cell 1
// except for a quadrant of cell 2, so the pixel at (31,31) has cell 1 on all
// four sides and cell 2 only diagonally. Four-neighbour called that an interior
// pixel and left the two cells sharing an unbroken block of white; the offline
// writer's "exact" method marks it, and so must this.
const diagonal = new Uint8Array(W * H * 4);
for (let y = 0; y < H; y += 1) {
    for (let x = 0; x < W; x += 1) {
        const id = x >= 32 && y >= 32 ? 2 : 1;
        const i = (y * W + x) * 4;
        diagonal[i] = id;
    }
}
const diagOut = drawn(makeRenderer({ segmentationMode: "filled" })(diagonal, W, H), W, H);
check("a cell touching another only at a corner is still separated",
    diagOut.has(31 * W + 31), "pixel (31,31), whose only differing neighbour is diagonal");
check("the corner case matches the reference too",
    sameSet(diagOut, referenceOutline(diagonal, W, H)), `${diagOut.size} drawn`);

// Background must never draw, in either mode.
const sparse = new Uint8Array(W * H * 4);
for (let y = 20; y < 30; y += 1) {
    for (let x = 20; x < 30; x += 1) sparse[(y * W + x) * 4] = 7;
}
const sparseRef = referenceOutline(sparse, W, H);
const sparseOut = drawn(makeRenderer({ segmentationMode: "filled" })(sparse, W, H), W, H);
check("background stays empty and an isolated cell is ringed",
    !sparseOut.has(0) && sameSet(sparseOut, sparseRef) && sparseOut.size > 0,
    `${sparseOut.size} drawn around one 10x10 cell`);

// Cells a few pixels across. Outlining them draws nothing but outline -- the
// whole-slide carpet of a Visium HD run -- so the tile fills each cell with a
// faint tint of its colour instead, and fades between the two in the middle.
const alphas = (context, width, height) => {
    const { data } = context.imageData;
    const out = [];
    for (let p = 0; p < width * height; p += 1) out.push(data[p * 4 + 3]);
    return out;
};
const outlineAlpha = Math.max(...alphas(makeRenderer({ segmentationMode: "filled" })(tile, W, H), W, H));
check("cells big enough to outline are pure outlines, at full alpha",
    outlineAlpha === 220 && interiorDrawn === 0, `max alpha ${outlineAlpha}`);

const tiny = alphas(makeRenderer({ segmentationMode: "filled" })(filledTile(W, H, 4), W, H), W, H);
check("cells four pixels across are filled, every pixel",
    tiny.every((a) => a > 0), `${tiny.filter((a) => a > 0).length}/${W * H}`);
check("the fill is a tint, one value, well under the outline's alpha",
    new Set(tiny).size === 1 && tiny[0] < outlineAlpha / 2, `alpha ${tiny[0]}`);

const middling = alphas(makeRenderer({ segmentationMode: "filled" })(filledTile(W, H, 6), W, H), W, H);
const middleValues = [...new Set(middling)].sort((a, b) => a - b);
check("cells six pixels across fade between the two: a fainter outline over a fill",
    middleValues.length === 2 && middleValues[0] > 0 && middleValues[1] < outlineAlpha
        && middleValues[0] < middleValues[1],
    `alphas ${middleValues.join(", ")}`);

const hiddenTiny = alphas(makeRenderer({
    segmentationMode: "filled", filterIds: new Set([1]),
})(filledTile(W, H, 4), W, H), W, H);
check("a filtered-out cell stays empty when the others are filled",
    hiddenTiny.filter((a) => a > 0).length === 16, `${hiddenTiny.filter((a) => a > 0).length} drawn`);

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
