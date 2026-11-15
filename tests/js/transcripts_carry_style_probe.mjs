/**
 * Walking to a sibling sample carries the transcript display controls (size,
 * opacity, point style, quality floor) -- and they must reach the RENDERER and
 * the Layers card, not only `layer.state`. They used to be written into state
 * and left there: the sliders said 3 px / 40 %, the molecules were drawn at
 * the old size and opacity until something else happened to restyle them.
 *
 * Loads the real TranscriptLayer and controller; stubs only the renderer, the
 * stack and the panel painting.
 *
 * Run directly: node tests/js/transcripts_carry_style_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/transcripts/static");

const context = createContext({
    console, Object, Array, Number, String, Map, Set, Promise, JSON, Math,
    Infinity, Uint8Array, Float32Array,
    setTimeout: () => 1, clearTimeout: () => {},
    document: { getElementById: () => null },
    // transcriptPoints.js needs WebGL to load; the layer only reads ICONS.
    TranscriptPointRenderer: { ICONS: ["circle", "square"], MAX_GROWTH: 1.6 },
});
context.window = context;
for (const file of ["transcriptsApi.js", "transcriptLayer.js", "transcriptsSidebarController.js"]) {
    const path = join(STATIC, file);
    runInContext(readFileSync(path, "utf8"), context, { filename: path });
}
const Layer = runInContext("TranscriptLayer", context);
const Controller = runInContext("TranscriptsSidebarController", context);

const pushed = {};
const stackOpacity = [];
const ctx = {
    layers: { setOpacity: (id, value) => stackOpacity.push([id, value]),
              viewport: () => null },
};
const layer = new Layer(ctx, "tx", {});
layer.manifest = { status: "ready", genes: ["A", "B"], gene_counts: [1, 1] };
layer.renderer = {
    set: (values) => Object.assign(pushed, values),
    setGeneTable() {}, invalidate() {}, setVisible() {},
};
layer.fullZoom = () => 1;
layer.emphasisFill = () => 1;
layer.binPixelsAt = () => 1;

const controller = Object.create(Controller.prototype);
controller.ctx = ctx;
controller.layer = layer;
controller._started = Promise.resolve();
controller.fill = () => {};
controller.paintTree = () => {};
controller.paintControls = () => {};

await controller.applyCarryState({
    selected: ["A"], colors: { A: "#ff0000" }, icons: {}, hidden: [], groups: [],
    size: 7, opacity: 0.4, pointStyle: "icons", minQ: 33,
});

const failures = [];
if (pushed.pointSize !== 7) failures.push(`renderer pointSize ${pushed.pointSize}, wanted 7`);
if (pushed.opacity !== 0.4) failures.push(`renderer opacity ${pushed.opacity}, wanted 0.4`);
if (pushed.styleMode !== 1) failures.push(`renderer styleMode ${pushed.styleMode}, wanted 1 (icons)`);
if (pushed.minQ !== 33) failures.push(`renderer minQ ${pushed.minQ}, wanted 33`);
if (!stackOpacity.some(([id, v]) => id === "tx" && v === 0.4)) {
    failures.push("the Layers card's opacity was not set to 0.4");
}
if (failures.length) {
    console.log(failures.join("\n"));
    process.exit(1);
}
console.log("ok");
