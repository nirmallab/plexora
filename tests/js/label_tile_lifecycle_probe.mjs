/**
 * What a label tile holds, and when it lets go of it.
 *
 * A label tile carries the decoded id array (~4 MB at 1024 square) plus ONE
 * cached RGBA canvas per layer drawn from it (~4 MB each). Those canvases are
 * the reason several plugins can be stacked without refetching anything, and
 * they are also the largest thing on the client heap. Two rules keep that from
 * being a leak, and neither is visible anywhere else -- nothing throws, nothing
 * logs, and the symptom is a tab that grows for as long as somebody keeps
 * panning:
 *
 *   1. `tile-unloaded` frees them. It used to free only `_array`, so every tile
 *      OpenSeadragon evicted left its rendered canvas behind for the life of the
 *      session. That one is reinstated by the wrapper test as a mutation, to
 *      prove this probe can see it.
 *
 *   2. A layer that leaves the stack -- hidden, or switched to centroids --
 *      releases its canvases without waiting for the tile to be evicted, and a
 *      layer that rejoins gets them built again. That is what makes "loaded but
 *      switched off" cost a lookup table and nothing that grows with panning.
 *
 * The third rule is about work rather than memory: re-rendering ONE layer must
 * leave the others' canvases alone. It is what makes a gate edit over a
 * phenotype map cheaper than one combined layer would be, and getting it wrong
 * is invisible except as a slideshow.
 *
 * The methods are extracted from the real source. renderLabelTile itself is
 * stubbed and counted -- what it draws is cell_color_probe.mjs's job.
 *
 * Run directly:  node tests/js/label_tile_lifecycle_probe.mjs
 *   --source <path>   probe a different imageViewer.js (used to prove the probe
 *                     can fail, by mutating a copy)
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1
    ? path.join(here, "..", "..", "plexora", "client", "src", "js", "views", "imageViewer.js")
    : process.argv[sourceArg + 1];

const source = await readFile(SOURCE, "utf8");

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    if (start < 0) throw new Error(`${startMarker.trim()} not found in imageViewer.js`);
    const end = source.indexOf(endMarker, start);
    if (end < 0) throw new Error(`could not find the end of ${startMarker.trim()}`);
    return source.slice(start, end + endMarker.length);
}

const registry = slice("    get cellLayer() {",
    "    applyCellColor(name = null) {\n        this.rerenderSegmentationTiles(name);\n        this.viewer?.forceRedraw?.();\n    }");
const tiles = slice("    forEachLabelTile(visit) {",
    "            this.renderTileLayers(tile, draw, name);\n        });\n    }");
const constants = ["    static DEFAULT_CELL_LAYER_OPACITY = ", "    static MASK_MODES = ",
    "    static CORE_LAYER = "].map((marker) => slice(marker, ";")).join("\n");

const ImageViewer = new Function(`
    class ImageViewer {
${constants}
${registry}
${tiles}
    }
    return ImageViewer;
`)();

// The eviction handler, lifted out of the constructor it is registered in.
const unloadSource = slice('this.viewer.addHandler("tile-unloaded", (e) => {', "});");
const onTileUnloaded = new Function(`
    const captured = {};
    const self = { viewer: { addHandler(name, fn) { captured[name] = fn; } } };
    ${unloadSource.replace(/\bthis\./g, "self.")}
    return captured["tile-unloaded"];
`)();

/** One label tile as OpenSeadragon leaves it after decode. */
function labelTile(id) {
    return { id, _isLabel: true, _array: new Uint8Array(16), _labelWidth: 2, _labelHeight: 2 };
}

/** A viewer holding a world of label tiles, with renderLabelTile counted. */
function viewer(tileCount = 2) {
    const self = Object.create(ImageViewer.prototype);
    self._cellLayers = new Map();
    self._cellLayerOrder = [];
    self._activeCellLayer = null;
    self.cellDisplayMode = "outlines";
    self.segmentationFilterIds = null;
    self._coreLayerView = {
        name: ImageViewer.CORE_LAYER, provider: null, lut: null, mode: "outlines",
        userMode: null, supportedModes: null, opacity: 1, visible: true,
        filterIds: null, filterRequest: 0, styleCache: new Map(),
    };
    self.renders = [];
    self.renderLabelTile = (array, width, height, layer) => {
        self.renders.push(layer.name);
        return { canvas: { width, height }, layer: layer.name };
    };
    self.tiles = [];
    const matrix = { 0: { 0: {} } };
    for (let i = 0; i < tileCount; i += 1) {
        const tile = labelTile(i);
        self.tiles.push(tile);
        matrix[0][0][i] = tile;
    }
    self.viewer = {
        forceRedraw() {},
        world: {
            getItemCount: () => 1,
            getItemAt: () => ({ source: { tileFormat: 32 }, tilesMatrix: matrix }),
        },
    };
    return self;
}

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const keysOf = (tile) => [...(tile._layerContexts?.keys() || [])].join(",");

// -- one canvas per drawn layer, built at decode --------------------------

let v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellLayerMode("cell_explorer", "filled");
v.registerCellLayer("gating", {});
v.setCellLayerMode("gating", "outlines");

v.renders = [];
v.renderTileLayers(v.tiles[0]);
check("a decoded tile gets one canvas per layer in the stack",
    keysOf(v.tiles[0]) === "cell_explorer,gating", `got ${keysOf(v.tiles[0])}`);
check("and each was rendered exactly once",
    String(v.renders) === "cell_explorer,gating", `${v.renders}`);

check("a tile with no decoded array renders nothing",
    v.renderTileLayers({ _labelWidth: 2, _labelHeight: 2 }) === null);

// -- re-rendering one layer leaves the others alone -----------------------

v.renders = [];
v.renderTileLayers(v.tiles[0], v.maskDrawList(), "gating");
check("re-rendering one layer rebuilds only that layer's canvas",
    String(v.renders) === "gating",
    "a gate edit over a phenotype map must not re-derive the colours as well");
check("and the other layer's canvas is still the one it had",
    v.tiles[0]._layerContexts.get("cell_explorer").layer === "cell_explorer");

// -- a layer leaving the stack releases its canvases -----------------------

v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellLayerMode("cell_explorer", "filled");
v.registerCellLayer("gating", {});
v.setCellLayerMode("gating", "outlines");
v.tiles.forEach((tile) => v.renderTileLayers(tile));

v.setCellLayerVisible("gating", false);
check("hiding a layer takes its canvas off every loaded tile",
    v.tiles.every((tile) => keysOf(tile) === "cell_explorer"),
    `got ${v.tiles.map(keysOf)}`);
check("without waiting for the tiles to be evicted",
    v.tiles[0]._array !== undefined,
    "panning with a layer switched off must not grow the heap");

v.renders = [];
v.setCellLayerVisible("gating", true);
check("showing it again builds its canvases back",
    v.tiles.every((tile) => keysOf(tile) === "cell_explorer,gating"),
    `got ${v.tiles.map(keysOf)}`);
check("and only that layer was rendered",
    String(v.renders) === "gating,gating", `${v.renders}`);

v.renders = [];
v.setCellLayerMode("gating", "centroids");
check("a layer switched to points releases its mask canvases too",
    v.tiles.every((tile) => keysOf(tile) === "cell_explorer"),
    `got ${v.tiles.map(keysOf)}`);

// -- a tile that has not been decoded yet is left alone --------------------

v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellLayerMode("cell_explorer", "outlines");
v.rerenderSegmentationTiles();
check("rerender skips tiles that were never rendered through the label path",
    v.renders.length === 0 && v.tiles.every((tile) => tile._layerContexts === undefined),
    "they have no canvases to rebuild, and creating some here would render a "
    + "tile OpenSeadragon has not asked for");

// -- eviction frees everything --------------------------------------------

v = viewer(1);
v.registerCellLayer("cell_explorer", {});
v.setCellLayerMode("cell_explorer", "filled");
v.registerCellLayer("gating", {});
v.setCellLayerMode("gating", "outlines");
v.renderTileLayers(v.tiles[0]);

const evicted = v.tiles[0];
onTileUnloaded({ tile: evicted });
check("eviction frees the decoded array", evicted._array === undefined);
check("eviction frees every cached canvas on the tile",
    evicted._layerContexts === undefined,
    "the canvases are the larger half of a label tile, and leaving them is a "
    + "heap that grows for as long as the session lasts");

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
