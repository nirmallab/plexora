/**
 * The cell-layer registry: who may colour cells, and what stacking them means.
 *
 * This replaced an exclusive claim -- one plugin at a time, and a change of
 * owner threw the previous plugin's colours away. Several plugins can now draw
 * at once, and almost every rule that mattered under the old model has an
 * equivalent here that is easy to get subtly wrong. None of it is visible to any
 * other kind of test: nothing throws, nothing logs, and the failure mode is a
 * viewer showing one tool's colours under another tool's legend.
 *
 * What is pinned:
 *
 *   1. A plugin with no registered layer cannot set colours. A response that
 *      lands after the tool was removed must not repaint the screen.
 *   2. A layer's colours belong to it. Registering a second plugin, selecting
 *      it, hiding the first one -- none of those may touch the first one's
 *      table, because rebuilding it is the expensive part.
 *   3. Re-registering under the SAME name keeps everything. That is what makes
 *      switching a tool away and back instant rather than a reload.
 *   4. Hiding a layer DROPS its per-tile canvases and KEEPS its lookup table.
 *      That split is the whole argument for not capping how many plugins may be
 *      loaded, so it is asserted rather than assumed.
 *   5. Order is composite order, and changing it is a redraw, never a
 *      re-render. Dragging a card has to be smooth at any cell count.
 *   6. With nothing registered, every read falls back to core's own layer --
 *      the plain white cell layer a viewer with no plugin has always drawn.
 *
 * The methods are extracted from the real source rather than reimplemented; the
 * rest of ImageViewer is not needed, so the side effects these methods have (a
 * tile re-render and a viewer redraw) are stubbed and counted.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(
    path.join(here, "..", "..", "plexora", "client", "src", "js", "views", "imageViewer.js"),
    "utf8",
);

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    if (start < 0) throw new Error(`${startMarker.trim()} not found in imageViewer.js`);
    const end = source.indexOf(endMarker, start);
    if (end < 0) throw new Error(`could not find the end of ${startMarker.trim()}`);
    return source.slice(start, end + endMarker.length);
}

// One contiguous run: the two getters through applyCellColor.
const methods = slice("    get cellLayer() {",
    "    applyCellColor(name = null) {\n        this.rerenderSegmentationTiles(name);\n        this.viewer?.forceRedraw?.();\n    }");
const defaultOpacity = slice("    static DEFAULT_CELL_LAYER_OPACITY = ", ";");
const maskModes = slice("    static MASK_MODES = ", ";");
const coreLayer = slice("    static CORE_LAYER = ", ";");

const ImageViewer = new Function(`
    class ImageViewer {
        ${defaultOpacity}
        ${maskModes}
        ${coreLayer}
${methods}
    }
    return ImageViewer;
`)();

/** A viewer stood up in the state main.js leaves it in, with the two side
 *  effects these methods have replaced by counters. */
function viewer() {
    const self = Object.create(ImageViewer.prototype);
    self._cellLayers = new Map();
    self._cellLayerOrder = [];
    self._activeCellLayer = null;
    self.cellDisplayMode = "outlines";
    self.segmentationFilterIds = null;
    self._coreLayerView = {
        name: ImageViewer.CORE_LAYER,
        provider: null,
        lut: null,
        mode: "outlines",
        userMode: null,
        supportedModes: null,
        opacity: 1,
        visible: true,
        filterIds: null,
        filterRequest: 0,
        styleCache: new Map(),
    };
    self.rerenders = [];
    self.redraws = 0;
    self.dropped = [];
    self.rerenderSegmentationTiles = (name = null) => { self.rerenders.push(name); };
    self.dropLayerContexts = (name) => { self.dropped.push(name); };
    self.viewer = { forceRedraw: () => { self.redraws += 1; } };
    return self;
}

const LUT = { colors: new Uint8Array([0, 0, 0, 0, 255, 0, 0, 255]), maxId: 1 };
const OTHER = { colors: new Uint8Array([0, 0, 0, 0, 0, 255, 0, 255]), maxId: 1 };

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const names = (list) => list.map((layer) => layer.name).join(",");

// -- nothing registered: core's own layer --------------------------------

let v = viewer();
check("a plugin with no layer cannot set colours",
    v.setCellColorLUT("cell_explorer", LUT) === false,
    "a response from a removed tool must not repaint");

check("with nothing registered, the mask draws core's own layer",
    names(v.maskDrawList()) === ImageViewer.CORE_LAYER,
    `got ${names(v.maskDrawList())}`);
check("and it composites at full strength",
    v.layerAlpha(v.coreLayerView()) === 1,
    "Thresholding's white outlines must look exactly as they always did");
check("core's layer follows core's mode and core's gate",
    (() => {
        v.cellDisplayMode = "filled";
        v.segmentationFilterIds = new Set([1]);
        const core = v.coreLayerView();
        return core.mode === "filled" && core.filterIds.has(1);
    })());
check("and there is no active layer to read a provider off",
    v.cellLayer === null && v.cellLayerOwner === null);

// -- registering ----------------------------------------------------------

v = viewer();
const explorer = v.registerCellLayer("cell_explorer", { pluginName: "cell_explorer" });
check("registering makes the layer active", v.cellLayerOwner === "cell_explorer");
check("and its provider is what core reads",
    v.cellLayer?.pluginName === "cell_explorer");
check("a new layer draws nothing until something asks",
    explorer.mode === "none" && v.maskDrawList().length === 0,
    "nothing goes over the image because a tool was opened");

check("the owner can set colours",
    v.setCellColorLUT("cell_explorer", LUT) === true && explorer.lut === LUT);
check("setting colours re-renders only that layer, and redraws",
    v.rerenders.length === 1 && v.rerenders[0] === "cell_explorer" && v.redraws === 1,
    `${JSON.stringify(v.rerenders)}`);

check("a name that was never registered is refused",
    v.setCellColorLUT("gating", OTHER) === false && explorer.lut === LUT,
    "a late response from a removed tool must not repaint");

// -- two layers keep their own everything ---------------------------------

v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
v.setCellLayerMode("cell_explorer", "filled");
const gating = v.registerCellLayer("gating", {});

check("registering a second layer does not disturb the first one's colours",
    v.getCellLayer("cell_explorer").lut === LUT,
    "the exclusive model dropped them here, and rebuilding one is a whole pass "
    + "over a column");
check("but the second one becomes the active layer",
    v.cellLayerOwner === "gating");
check("a second layer starts with no colours of its own", gating.lut === null);

v.setCellColorLUT("gating", OTHER);
check("each layer holds its own table",
    v.getCellLayer("cell_explorer").lut === LUT && v.getCellLayer("gating").lut === OTHER);

v.setCellLayerMode("gating", "outlines");
check("and its own mode",
    v.getCellLayer("cell_explorer").mode === "filled"
    && v.getCellLayer("gating").mode === "outlines");

v.getCellLayer("gating").filterIds = new Set([7]);
check("and its own gate",
    v.getCellLayer("cell_explorer").filterIds === null,
    "one tool's selection must not subtract cells from another tool's colours");

// -- order is z-order -----------------------------------------------------

check("a new layer goes on top of the stack",
    names(v.maskDrawList()) === "cell_explorer,gating",
    `got ${names(v.maskDrawList())} -- bottom first`);

let beforeRerenders = v.rerenders.length;
let beforeRedraws = v.redraws;
check("restacking reports that it changed something",
    v.setCellLayerOrder(["gating", "cell_explorer"]) === true);
check("and puts the layers in the order given, bottom first",
    names(v.maskDrawList()) === "gating,cell_explorer");
check("restacking is a redraw and never a re-render",
    v.rerenders.length === beforeRerenders && v.redraws > beforeRedraws,
    "or dragging a card would re-derive every visible tile's boundaries");
check("restacking to the same order does nothing",
    v.setCellLayerOrder(["gating", "cell_explorer"]) === false);
check("a name that is not registered is ignored",
    v.setCellLayerOrder(["roi", "cell_explorer", "gating"]) === true
    && names(v.maskDrawList()) === "cell_explorer,gating");
check("a layer the caller did not mention keeps a place in the stack",
    (() => {
        v.setCellLayerOrder(["gating"]);
        return names(v.maskDrawList()) === "cell_explorer,gating";
    })(),
    "a partial order must never drop a layer off the picture");

// -- visible / active are different questions -----------------------------

v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
v.setCellLayerMode("cell_explorer", "filled");
v.registerCellLayer("gating", {});
v.setCellLayerMode("gating", "outlines");
v.setActiveCellLayer("cell_explorer");

check("selecting a different layer changes nothing about what is drawn",
    names(v.maskDrawList()) === "cell_explorer,gating",
    "every layer that was visible stays visible -- that is the point of layers");
check("but the shared controls now point at it",
    v.cellLayerOwner === "cell_explorer");
check("setActiveCellLayer reports who was displaced",
    v.setActiveCellLayer("gating") === "cell_explorer");
check("and selecting the one already selected is a no-op",
    v.setActiveCellLayer("gating") === null);

v.dropped = [];
v.rerenders = [];
check("hiding a layer takes it out of the stack",
    v.setCellLayerVisible("cell_explorer", false) === true
    && names(v.maskDrawList()) === "gating");
check("hiding drops that layer's per-tile canvases",
    String(v.dropped) === "cell_explorer", `dropped ${v.dropped}`);
check("and KEEPS its lookup table",
    v.getCellLayer("cell_explorer").lut === LUT,
    "four bytes a cell, and the expensive thing to recompute -- which is why a "
    + "loaded-but-hidden plugin needs no cache limit");
check("hiding re-renders nothing",
    v.rerenders.length === 0, `${JSON.stringify(v.rerenders)}`);
check("hiding the active layer does not deselect it",
    v.cellLayerOwner === "gating" && v.getCellLayer("cell_explorer") !== null);

v.rerenders = [];
check("showing it again rebuilds only that layer",
    v.setCellLayerVisible("cell_explorer", true) === true
    && String(v.rerenders) === "cell_explorer",
    `${JSON.stringify(v.rerenders)}`);
check("and it is back in the stack, where it was",
    names(v.maskDrawList()) === "cell_explorer,gating");
check("setting the visibility it already has does nothing",
    v.setCellLayerVisible("cell_explorer", true) === false);

// -- re-registering keeps everything --------------------------------------

v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
v.setCellLayerMode("cell_explorer", "filled");
v.setLayerOpacity("cell_explorer", 0.25);
let steady = v.rerenders.length;
v.registerCellLayer("cell_explorer", { second: true });

check("re-registering keeps the colours, mode and opacity",
    v.getCellLayer("cell_explorer").lut === LUT
    && v.getCellLayer("cell_explorer").mode === "filled"
    && v.getCellLayer("cell_explorer").opacity === 0.25,
    "switching a tool away and back must not cost a refetch");
check("re-registering does not repaint", v.rerenders.length === steady);
check("re-registering does not stack a second copy",
    v.cellLayers().length === 1);
check("but it does adopt the new provider",
    v.cellLayer.second === true);

// -- unregistering --------------------------------------------------------

v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
v.registerCellLayer("gating", {});
v.setCellLayerMode("gating", "outlines");

check("a layer that was never registered cannot be unregistered",
    v.unregisterCellLayer("roi") === false);

check("unregistering takes it out entirely",
    v.unregisterCellLayer("gating") === true
    && v.getCellLayer("gating") === null && v.cellLayers().length === 1);
check("and hands the selection to the topmost survivor",
    v.cellLayerOwner === "cell_explorer",
    "rather than stranding the shared controls while a layer is still on screen");
check("a removed plugin's late response is refused",
    v.setCellColorLUT("gating", OTHER) === false);

v.unregisterCellLayer("cell_explorer");
check("removing the last one leaves nothing selected",
    v.cellLayerOwner === null && v.cellLayer === null);
check("and core's own layer takes the picture back",
    names(v.maskDrawList()) === ImageViewer.CORE_LAYER);

// -- opacity --------------------------------------------------------------

v = viewer();
const layer = v.registerCellLayer("cell_explorer", {});
check("a layer starts at the default opacity",
    layer.opacity === ImageViewer.DEFAULT_CELL_LAYER_OPACITY);
check("a layer with no colours composites at full strength anyway",
    v.layerAlpha(layer) === 1,
    "the opacity control belongs to whoever supplies the colours");
v.setCellColorLUT("cell_explorer", LUT);
check("colours bring the opacity into effect",
    v.layerAlpha(layer) === ImageViewer.DEFAULT_CELL_LAYER_OPACITY);

beforeRerenders = v.rerenders.length;
check("opacity changes are a redraw, never a re-render",
    v.setLayerOpacity("cell_explorer", 0.35) === true
    && v.layerAlpha(layer) === 0.35
    && v.rerenders.length === beforeRerenders);
check("setting the same opacity twice does nothing",
    v.setLayerOpacity("cell_explorer", 0.35) === false);
check("opacity is clamped to a real alpha",
    v.setLayerOpacity("cell_explorer", 4) && v.layerAlpha(layer) === 1);
check("opacity for a layer that does not exist is refused",
    v.setLayerOpacity("gating", 0.5) === false);

// -- mode -----------------------------------------------------------------

v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
v.rerenders = [];
v.setCellLayerMode("cell_explorer", "outlines");
check("joining the mask stack re-renders",
    v.rerenders.length === 1, `${JSON.stringify(v.rerenders)}`);

v.rerenders = [];
v.setCellLayerMode("cell_explorer", "filled");
check("outlines to filled re-renders, because it changes what the tiles hold",
    v.rerenders.length === 1);

v.rerenders = [];
v.setCellLayerMode("cell_explorer", "centroids");
check("leaving the mask stack re-renders, to release the canvases",
    v.rerenders.length === 1 && v.rerenders[0] === null,
    "everything is rebuilt here, because the stack itself changed");
check("and the layer draws as points now",
    names(v.centroidDrawList()) === "cell_explorer"
    && v.maskDrawList().length === 0);

v.rerenders = [];
v.setCellLayerMode("cell_explorer", "centroids");
check("setting the same mode twice does nothing",
    v.rerenders.length === 0);

v.rerenders = [];
v.setCellLayerMode("cell_explorer", "none");
check("moving between two non-mask modes is a redraw only",
    v.rerenders.length === 0 && v.centroidDrawList().length === 0);

check("a hidden layer draws no points either",
    (() => {
        v.setCellLayerMode("cell_explorer", "centroids");
        v.setCellLayerVisible("cell_explorer", false);
        return v.centroidDrawList().length === 0;
    })());

// -- colour lookup --------------------------------------------------------

v = viewer();
const only = v.registerCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
check("a mapped cell resolves to a fill",
    v.cellColorStyle(1, only) === "rgb(255,0,0)", `got ${v.cellColorStyle(1, only)}`);
check("an alpha-0 cell resolves to nothing", v.cellColorStyle(0, only) === null);
check("a cell past the end of the table resolves to nothing",
    v.cellColorStyle(99, only) === null, "a mask object with no row in the table");
check("a layer with no colours resolves to nothing",
    v.cellColorStyle(1, v.coreLayerView()) === null);
check("fill strings are memoized per layer, not rebuilt per point",
    v.cellColorStyle(1, only) === v.cellColorStyle(1, only) && only.styleCache.size === 1);

v.setCellColorLUT("cell_explorer", { map: new Map([[7, [1, 2, 3, 255]], [8, [4, 5, 6, 0]]]) });
check("the sparse form resolves the same way",
    v.cellColorStyle(7, only) === "rgb(1,2,3)", `got ${v.cellColorStyle(7, only)}`);
check("the sparse form hides an alpha-0 entry", v.cellColorStyle(8, only) === null);
check("the sparse form has nothing for an unmapped cell", v.cellColorStyle(9, only) === null);
check("swapping the LUT drops the memoized fills", only.styleCache.size === 1,
    "or a recoloured category would keep drawing its old colour");

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
