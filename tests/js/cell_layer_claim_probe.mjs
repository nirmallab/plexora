/**
 * Who is allowed to colour cells, and what happens when that moves.
 *
 * The cell layer is exclusive -- one plugin at a time -- and it now carries a
 * colour lookup table as well as a selection provider. That makes the handoff
 * the interesting part, and it is entirely invisible to every other kind of
 * test: nothing here throws, nothing logs, and the failure mode is a viewer
 * showing one tool's colours under another tool's legend.
 *
 * Three rules are pinned:
 *
 *   1. A plugin that does not hold the layer cannot set colours. A response
 *      that lands after the user switched tools must not repaint the screen.
 *   2. A change of owner drops the colours. They mean nothing without the
 *      legend that explains them, and that legend has just been hidden.
 *   3. Re-claiming under the SAME name keeps everything. That is what makes
 *      switching a tool away and back instant rather than a reload.
 *
 * The methods are extracted from the real source rather than reimplemented; the
 * rest of ImageViewer is not needed, so the two things these methods reach for
 * (a tile re-render and a viewer redraw) are stubbed and counted.
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

// One contiguous run: the two ownership getters through applyCellColor.
const methods = slice("    get cellLayer() {",
    "    applyCellColor() {\n        this.rerenderSegmentationTiles();\n        this.viewer?.forceRedraw?.();\n    }");
const defaultOpacity = slice("    static DEFAULT_CELL_LAYER_OPACITY = ", ";");

const ImageViewer = new Function(`
    class ImageViewer {
        ${defaultOpacity}
${methods}
    }
    return ImageViewer;
`)();

/** A viewer stood up in the state main.js leaves it in, with the two side
 *  effects these methods have replaced by counters. */
function viewer() {
    const self = Object.create(ImageViewer.prototype);
    self._cellLayerOwner = null;
    self.selectionProvider = null;
    self.cellColorLUT = null;
    self._cellStyleCache = new Map();
    self.cellDisplayMode = "outlines";
    self.cellLayerOpacity = ImageViewer.DEFAULT_CELL_LAYER_OPACITY;
    self.rerenders = 0;
    self.redraws = 0;
    self.rerenderSegmentationTiles = () => { self.rerenders += 1; };
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

// -- only the owner may colour -------------------------------------------

let v = viewer();
check("a plugin that holds nothing cannot set colours",
    v.setCellColorLUT("cell_explorer", LUT) === false && v.cellColorLUT === null);

v.claimCellLayer("cell_explorer", { pluginName: "cell_explorer" });
check("the owner can set colours",
    v.setCellColorLUT("cell_explorer", LUT) === true && v.cellColorLUT === LUT);

check("a non-owner is refused while someone else holds the layer",
    v.setCellColorLUT("gating", OTHER) === false && v.cellColorLUT === LUT,
    "a late response from a switched-away tool must not repaint");

check("setting colours redraws without refetching anything",
    v.rerenders > 0 && v.redraws > 0, `${v.rerenders} re-renders, ${v.redraws} redraws`);

// -- ownership change clears --------------------------------------------

v = viewer();
v.claimCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
const before = v.rerenders;
const displaced = v.claimCellLayer("gating", {});

check("claiming reports who was displaced", displaced === "cell_explorer", `got ${displaced}`);
check("a change of owner drops the previous colours", v.cellColorLUT === null);
check("dropping them repaints, rather than leaving stale pixels",
    v.rerenders > before, `${before} -> ${v.rerenders}`);
check("the new owner's provider is in place", v.cellLayerOwner === "gating");

// -- re-claiming under the same name keeps everything --------------------

v = viewer();
v.claimCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
const steady = v.rerenders;
v.claimCellLayer("cell_explorer", { second: true });

check("re-claiming the layer keeps the colours", v.cellColorLUT === LUT,
    "switching a tool away and back must not cost a refetch");
check("re-claiming does not repaint", v.rerenders === steady, `${steady} -> ${v.rerenders}`);

// -- release -------------------------------------------------------------

v = viewer();
v.claimCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
v.setCellLayerOpacity(0.2);

check("a plugin cannot release a layer it does not hold",
    v.releaseCellLayer("gating") === false && v.cellColorLUT === LUT);

v.cellDisplayMode = "filled";
check("releasing clears the colours", v.releaseCellLayer("cell_explorer") === true
    && v.cellColorLUT === null && v.cellLayerOwner === null);
check("releasing restores the default opacity",
    v.cellLayerOpacity === ImageViewer.DEFAULT_CELL_LAYER_OPACITY,
    `got ${v.cellLayerOpacity}`);
check("releasing leaves the display mode where the user set it",
    v.cellDisplayMode === "filled",
    "the Cells control is core's; a plugin shutting down does not move it");

// -- opacity is inert until something colours cells ----------------------

v = viewer();
check("a viewer with no colours composites at full strength",
    v.cellLayerAlpha() === 1,
    "Thresholding's white outlines must look exactly as they always did");
v.claimCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
check("colours bring the opacity control into effect",
    v.cellLayerAlpha() === ImageViewer.DEFAULT_CELL_LAYER_OPACITY);
v.setCellLayerOpacity(0.35);
check("opacity changes are a redraw, never a re-render",
    v.cellLayerAlpha() === 0.35, `got ${v.cellLayerAlpha()}`);

const beforeOpacity = v.rerenders;
v.setCellLayerOpacity(0.5);
check("dragging the slider does not re-render tiles",
    v.rerenders === beforeOpacity, `${beforeOpacity} -> ${v.rerenders}`);
check("opacity is clamped to a real alpha",
    v.setCellLayerOpacity(4) && v.cellLayerAlpha() === 1);

// -- display mode --------------------------------------------------------

v = viewer();
v.claimCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
let modeRerenders = v.rerenders;
v.setCellDisplayMode("centroids");
check("centroids does not re-render the label tiles",
    v.rerenders === modeRerenders,
    "which layer is showing is not what those tiles hold");
modeRerenders = v.rerenders;
v.setCellDisplayMode("filled");
check("filled re-renders, because it changes what the tiles hold",
    v.rerenders > modeRerenders);
modeRerenders = v.rerenders;
v.setCellDisplayMode("filled");
check("setting the same mode twice does nothing", v.rerenders === modeRerenders);

// -- colour lookup -------------------------------------------------------

v = viewer();
v.claimCellLayer("cell_explorer", {});
v.setCellColorLUT("cell_explorer", LUT);
check("a mapped cell resolves to a fill", v.cellColorStyle(1) === "rgb(255,0,0)",
    `got ${v.cellColorStyle(1)}`);
check("an alpha-0 cell resolves to nothing", v.cellColorStyle(0) === null);
check("a cell past the end of the table resolves to nothing",
    v.cellColorStyle(99) === null, "a mask object with no row in the table");
check("fill strings are memoized, not rebuilt per point",
    v.cellColorStyle(1) === v.cellColorStyle(1) && v._cellStyleCache.size === 1);

v.setCellColorLUT("cell_explorer", { map: new Map([[7, [1, 2, 3, 255]], [8, [4, 5, 6, 0]]]) });
check("the sparse form resolves the same way", v.cellColorStyle(7) === "rgb(1,2,3)",
    `got ${v.cellColorStyle(7)}`);
check("the sparse form hides an alpha-0 entry", v.cellColorStyle(8) === null);
check("the sparse form has nothing for an unmapped cell", v.cellColorStyle(9) === null);
check("swapping the LUT drops the memoized fills", v._cellStyleCache.size === 1,
    "or a recoloured category would keep drawing its old colour");

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
