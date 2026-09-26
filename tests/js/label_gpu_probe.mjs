/**
 * The GPU cell layer, without a GPU: what can be proven in node.
 *
 *   A. The viewer's side (imageViewer.js, sliced from the real source): moving
 *      between the CPU and GPU renderers, what a label tile holds in each, how
 *      a gate or colour change marks the GPU path's tiles for redraw, and the
 *      browser-side gate falling back to the provider when it cannot run.
 *   B. labelGpu.js against a recording fake WebGL2 context: the state it must
 *      hand back to the channel program (program, viewport, unpack flip,
 *      texture unit), NEAREST on every texture it creates (an integer texture
 *      without it is incomplete and reads 0 silently), a_uv pinned to 0 before
 *      link, the id-indexed tables wrapping into rows, re-upload only on change,
 *      and the 2^24 cap turning the GPU path off.
 *   C. evaluateGateMask against a hand-computed reference of the server's rules
 *      (data_model.apply_range_mask): float32 bounds, exclusive, NaN, AND,
 *      unknown keys skipped, duplicate ids counted once.
 *   D. labelTile.alphaTables equals the CPU loop's rounding, and the CPU loop
 *      honours a gate mask.
 *
 * What it cannot prove is that the shader draws the same pixels as
 * renderLabelTile: tests/test_label_gpu_parity.py does that in a browser.
 *
 * Run directly:  node tests/js/label_gpu_probe.mjs
 */

import { readFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const VIEWS = path.join(here, "..", "..", "plexora", "client", "src", "js", "views");
const source = await readFile(path.join(VIEWS, "imageViewer.js"), "utf8");

// The shipped files, run in this realm (the sliced methods resolve these
// globals at call time).
for (const file of ["layerStack.js", "glInit.js", "labelTile.js", "labelGpu.js"]) {
    (0, eval)(readFileSync(path.join(VIEWS, file), "utf8"));
}
const { PlexoraLabelGpu, PlexoraLabelTile } = globalThis;

function slice(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    if (start < 0) throw new Error(`${startMarker.trim()} not found in imageViewer.js`);
    const end = source.indexOf(endMarker, start);
    if (end < 0) throw new Error(`could not find the end of ${startMarker.trim()}`);
    return source.slice(start, end + endMarker.length);
}

const registry = slice("    get cellLayer() {",
    "    applyCellColor(name = null) {\n        this.rerenderSegmentationTiles(name);\n        this.viewer?.forceRedraw?.();\n    }");
const tiles = slice("    async updateSegmentationFilter(",
    "            console.warn(\"Gate evaluated on the server instead:\", e.message || e);\n            return null;\n        }\n    }");
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
const unloadSource = slice('this.viewer.addHandler("tile-unloaded", (e) => {', "});");
const onTileUnloaded = new Function(`
    const captured = {};
    const self = { viewer: { addHandler(name, fn) { captured[name] = fn; } } };
    ${unloadSource.replace(/\bthis\./g, "self.")}
    return captured["tile-unloaded"];
`)();

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

// ---------------------------------------------------------------------------
// A. the viewer
// ---------------------------------------------------------------------------

/** A 4x4 label tile: cell 1 on the left half, cell 2 on the right. */
function labelTileOf(id) {
    const array = new Uint8Array(4 * 4 * 4);
    for (let p = 0; p < 16; p += 1) array[p * 4] = (p % 4) < 2 ? 1 : 2;
    return { id, _isLabel: true, _array: array, _labelWidth: 4, _labelHeight: 4 };
}

function fakeLabelGpu() {
    return {
        active: true, software: false, dropped: [], cleared: 0,
        isSoftware() { return this.software; },
        dropLayer(name) { this.dropped.push(name); },
        clear() { this.cleared += 1; },
    };
}

function viewer({ tileCount = 2, gpu = true } = {}) {
    const self = Object.create(ImageViewer.prototype);
    self._cellStack = new PlexoraLayerStack.SubLayerStack({
        makeRecord: (name) => ({
            name, provider: null, lut: null, mode: "none", userMode: null, supportedModes: null,
            opacity: ImageViewer.DEFAULT_CELL_LAYER_OPACITY, visible: true,
            filterIds: null, filterRequest: 0, gateMask: null, gateCount: null,
            renderVersion: 0, lutVersion: 0, styleCache: new Map(),
        }),
    });
    self.cellDisplayMode = "outlines";
    self.segmentationFilterIds = null;
    self.segmentationFilterRequest = 0;
    self._coreLayerView = {
        name: ImageViewer.CORE_LAYER, provider: null, lut: null, mode: "outlines",
        userMode: null, supportedModes: null, opacity: 1, visible: true,
        filterIds: null, filterRequest: 0, gateMask: null, gateCount: null,
        renderVersion: 0, lutVersion: 0, styleCache: new Map(),
    };
    self.config = { segmentationMode: "filled" };
    self.renders = [];
    self.renderLabelTile = (array, width, height, layer) => {
        self.renders.push(layer.name);
        return { canvas: { width, height }, layer: layer.name };
    };
    self._labelRenderer = "cpu";
    self._labelRendererPref = null;
    self.labelGpu = gpu ? fakeLabelGpu() : null;
    self.redraws = 0;
    self.tiles = [];
    const matrix = { 0: { 0: {} } };
    for (let i = 0; i < tileCount; i += 1) {
        const tile = labelTileOf(i);
        self.tiles.push(tile);
        matrix[0][0][i] = tile;
    }
    self.viewer = {
        forceRedraw() { self.redraws += 1; },
        world: {
            getItemCount: () => 1,
            getItemAt: () => ({ source: { tileFormat: 32 }, tilesMatrix: matrix }),
        },
    };
    self.viewerManagerVMain = { sel_outlines: true };
    self.segmentationReady = true;
    self.ensureSegmentationReady = async () => {};
    self.setLoading = () => {};
    return self;
}

const keysOf = (tile) => [...(tile._layerContexts?.keys() || [])].join(",");

let v = viewer();
v.registerCellLayer("cell_explorer", {});
v.setCellLayerMode("cell_explorer", "filled");
v.registerCellLayer("gating", {});
v.setCellLayerMode("gating", "outlines");
v.tiles.forEach((tile) => v.renderTileLayers(tile));
check("on the CPU a decoded tile holds one canvas per layer (unchanged)",
    v.tiles.every((t) => keysOf(t) === "cell_explorer,gating"), v.tiles.map(keysOf).join(" "));

check("the GPU is chosen when it is up and nothing asks otherwise",
    v.applyLabelRenderer() === "gpu" && v.labelRenderer === "gpu");
check("moving to the GPU drops every layer canvas",
    v.tiles.every((t) => t._layerContexts === undefined));
check("and computes each tile's fill weight once",
    v.tiles.every((t) => typeof t._fillWeight === "number"), v.tiles.map((t) => t._fillWeight).join(","));
const fill0 = PlexoraLabelTile.fillWeightOf(v.tiles[0]._array, 4, 4);
check("the fill weight is labelTile's own smallCellWeight", v.tiles[0]._fillWeight === fill0,
    `${v.tiles[0]._fillWeight} vs ${fill0}`);

const fresh = labelTileOf(9);
v.renders = [];
check("on the GPU a newly decoded tile gets no canvases",
    v.renderTileLayers(fresh) === null && fresh._layerContexts === undefined && v.renders.length === 0);
check("only its fill weight", typeof fresh._fillWeight === "number");

let before = { g: v.getCellLayer("gating").renderVersion, c: v.getCellLayer("cell_explorer").renderVersion };
v.renders = [];
v.rerenderSegmentationTiles("gating");
check("a gate re-render on the GPU marks only that layer for redraw",
    v.getCellLayer("gating").renderVersion === before.g + 1
    && v.getCellLayer("cell_explorer").renderVersion === before.c, "");
check("and renders no pixels in JavaScript", v.renders.length === 0 && v.tiles.every((t) => !t._layerContexts));

before = { g: v.getCellLayer("gating").renderVersion, c: v.getCellLayer("cell_explorer").renderVersion,
           core: v._coreLayerView.renderVersion };
v.rerenderSegmentationTiles(null);
check("a full re-render marks every layer and core's",
    v.getCellLayer("gating").renderVersion === before.g + 1
    && v.getCellLayer("cell_explorer").renderVersion === before.c + 1
    && v._coreLayerView.renderVersion === before.core + 1);

before = { lut: v.getCellLayer("cell_explorer").lutVersion, r: v.getCellLayer("cell_explorer").renderVersion };
v.setCellColorLUT("cell_explorer", { colors: new Uint8Array(12), maxId: 2 });
check("a new colour table moves the layer's lutVersion and renderVersion",
    v.getCellLayer("cell_explorer").lutVersion === before.lut + 1
    && v.getCellLayer("cell_explorer").renderVersion === before.r + 1);

before = v.getCellLayer("gating").renderVersion;
v.setCellLayerVisible("gating", false);
v.setCellLayerVisible("gating", true);
check("showing a layer again marks it for redraw", v.getCellLayer("gating").renderVersion === before + 1);

v.unregisterCellLayer("cell_explorer");
check("unregistering a layer frees its GPU tables", v.labelGpu.dropped.includes("cell_explorer"));

v.renders = [];
check("setLabelRenderer('cpu') moves back", v.setLabelRenderer("cpu") === "cpu" && v.labelRenderer === "cpu");
check("rebuilding every tile's canvases", v.tiles.every((t) => keysOf(t) === "gating"),
    v.tiles.map(keysOf).join(" "));
check("and releasing the GPU's textures", v.labelGpu.cleared === 1);
check("setLabelRenderer(null) returns to the default", v.setLabelRenderer(null) === "gpu");

v.labelGpu.software = true;
v._labelRenderer = "gpu";
check("a software renderer takes the measured default",
    v.desiredLabelRenderer() === PlexoraLabelGpu.SOFTWARE_DEFAULT);
v._labelRendererPref = "cpu";
check("an explicit preference wins over the default", v.desiredLabelRenderer() === "cpu");
v._labelRendererPref = null;
v.labelGpu.active = false;
check("a GPU path that turned itself off means the CPU", v.desiredLabelRenderer() === "cpu"
    && v.labelRenderer === "cpu");
check("and the probes' viewers without a labelGpu stay on the CPU", viewer({ gpu: false }).desiredLabelRenderer() === "cpu");

const unloaded = labelTileOf(3);
unloaded._fillWeight = 0.3;
unloaded._layerContexts = new Map([["gating", {}]]);
onTileUnloaded({ tile: unloaded });
check("eviction also forgets the fill weight", unloaded._fillWeight === undefined
    && unloaded._layerContexts === undefined && unloaded._array === undefined);

// -- the browser-side gate, and its fallback ---------------------------------

async function gateRun({ localRangeGate = true, column = null, getColumnFails = false } = {}) {
    const g = viewer();
    g.registerCellLayer("gating", {});
    g.setCellLayerMode("gating", "outlines");
    g.applyLabelRenderer();
    const asked = [];
    g.getCellLayer("gating").provider = {
        localRangeGate,
        async getSelectedIds(gates) { asked.push(gates); return new Set([2]); },
    };
    g.ids = new Uint32Array([1, 2, 3]);
    const fetched = [];
    g.numericData = {
        hasCellTable: () => true,
        async getColumn(key) {
            fetched.push(key);
            if (getColumnFails) throw new Error("500");
            return column || new Float32Array([0.5, 2.5, 5]);
        },
    };
    await g.updateSegmentationFilter({ CD45: [1, 4] }, false, "gating");
    return { g, layer: g.getCellLayer("gating"), asked, fetched };
}

let run = await gateRun();
check("a range gate is evaluated in the browser",
    run.layer.gateMask instanceof Uint8Array && run.asked.length === 0, `asked ${run.asked.length}`);
check("the mask passes exactly the cells in range",
    Array.from(run.layer.gateMask).join("") === "0010" && run.layer.gateCount === 1,
    Array.from(run.layer.gateMask || []).join(""));
check("and the id list is not also kept", run.layer.filterIds === null);
check("the gate change marks the layer for redraw", run.layer.renderVersion >= 1);

run = await gateRun({ column: new Float32Array([1, 2]) });
check("a column that does not line up with the ids falls back to the provider",
    run.asked.length === 1 && run.layer.filterIds instanceof Set && run.layer.gateMask === null);
run = await gateRun({ getColumnFails: true });
check("so does a column the server will not give", run.asked.length === 1 && run.layer.gateMask === null);
run = await gateRun({ localRangeGate: false });
check("a provider that does not declare a range gate is always asked",
    run.asked.length === 1 && run.fetched.length === 0);

// ---------------------------------------------------------------------------
// B. labelGpu.js against a recording WebGL2
// ---------------------------------------------------------------------------

function fakeGl({ maxTexture = 16 } = {}) {
    const calls = [];
    let serial = 0;
    let bound = null;
    const textureParams = new Map();
    const canvas = { width: 8, height: 8, listeners: {}, addEventListener(n, f) { this.listeners[n] = f; } };
    const special = {
        canvas,
        throwOnDraw: false,
        createTexture: () => ({ texture: ++serial }),
        createProgram: () => ({ program: ++serial }),
        createShader: () => ({ shader: ++serial }),
        createBuffer: () => ({ buffer: ++serial }),
        getShaderParameter: () => true,
        getProgramParameter: () => true,
        getUniformLocation: (p, name) => name,
        getExtension: () => null,
        isContextLost: () => false,
        getParameter: (name) => (name === "MAX_TEXTURE_SIZE" ? maxTexture
            : name === "RENDERER" ? "FakeGL" : null),
    };
    const gl = new Proxy({}, {
        get(_, prop) {
            if (typeof prop !== "string") return undefined;
            if (prop === "calls") return calls;
            if (prop === "textureParams") return textureParams;
            if (prop in special) {
                const value = special[prop];
                if (typeof value !== "function") return value;
                return (...args) => { const out = value(...args); calls.push([prop, ...args, out]); return out; };
            }
            if (/^[A-Z0-9_]+$/.test(prop)) return prop;
            return (...args) => {
                calls.push([prop, ...args]);
                if (prop === "bindTexture") bound = args[1];
                if (prop === "texParameteri" && bound) {
                    const m = textureParams.get(bound) || {};
                    m[args[1]] = args[2];
                    textureParams.set(bound, m);
                }
                if (prop === "drawArrays" && gl.throwOnDraw) { gl.throwOnDraw = false; throw new Error("boom"); }
                return undefined;
            };
        },
        set(_, prop, value) { special[prop] = value; return true; },
    });
    return gl;
}

globalThis.fetch = async (url) => ({ ok: true, text: async () => `// ${url}` });
globalThis.setTimeout = globalThis.setTimeout || ((f) => f());
const gl = fakeGl();
const renderer = {
    gl, width: 8, height: 8, buffer: "quadBuffer", program: "channelProgram",
    updateShape(w, h) { this.width = w; this.height = h; gl.calls.push(["updateShape", w, h]); },
};
let changes = 0;
const gpu = PlexoraLabelGpu.createLabelGpu({
    renderer, vShaderUrl: "vert.glsl", fShaderUrl: "label.frag.glsl",
    labelTile: PlexoraLabelTile, onChange: () => { changes += 1; },
});
await gpu.ready;
await new Promise((r) => setTimeout(r, 5));
check("the program builds and the GPU path comes up", gpu.active && gpu.reason === null, String(gpu.reason));
check("and says so, so the viewer can move across", changes === 1);

const names = gl.calls.map((c) => c[0]);
const bindAttr = gl.calls.findIndex((c) => c[0] === "bindAttribLocation" && c[2] === 0 && c[3] === "a_uv");
check("a_uv is pinned to attribute 0 before the program links",
    bindAttr >= 0 && bindAttr < names.indexOf("linkProgram"));

const tile = labelTileOf(0);
tile._fillWeight = 0.5;
const drawn = [];
const rendered = { drawImage: (...args) => drawn.push(args) };
const layer = { name: "gating", mode: "outlines", lut: null, gateMask: new Uint8Array(40).fill(1), lutVersion: 0 };
gl.calls.length = 0;
check("a tile draws", gpu.drawTile(tile, layer, rendered, 4, 4, "filled") === true);
const after = gl.calls.map((c) => c[0]);
const drawAt = after.lastIndexOf("drawArrays");
const tailCalls = gl.calls.slice(drawAt + 1);
const restoredProgram = tailCalls.find((c) => c[0] === "useProgram");
const restoredViewport = tailCalls.find((c) => c[0] === "viewport");
const restoredFlip = tailCalls.find((c) => c[0] === "pixelStorei" && c[1] === "UNPACK_FLIP_Y_WEBGL");
const restoredUnit = tailCalls.find((c) => c[0] === "activeTexture");
check("afterwards the channel program is current again", restoredProgram?.[1] === "channelProgram");
check("with the channel viewport", restoredViewport && restoredViewport.slice(1).join(",") === "0,0,8,8");
check("unpack flip back on, as selectTexture expects", restoredFlip?.[2] === 1);
check("and texture unit 0 active", restoredUnit?.[1] === "TEXTURE0");
check("the draw is 1:1 into the canvas's bottom-left tile-sized corner",
    gl.calls.some((c) => c[0] === "viewport" && c.slice(1).join(",") === "0,0,4,4"));
check("and blitted from the matching canvas rows",
    drawn.length === 1 && drawn[0].slice(1).join(",") === "0,4,4,4,0,0,4,4", drawn[0]?.slice(1).join(","));
const alphaUpload = gl.calls.find((c) => c[0] === "texSubImage2D" && c[5] === 256 && c[6] === 2);
const expected = PlexoraLabelTile.alphaTables(0.5);
check("the tile's alpha tables are labelTile's, uploaded for its fill weight",
    alphaUpload && Array.from(alphaUpload[9]).every((x, i) => x === expected[i]));

let incomplete = [];
for (const [texture, params] of gl.textureParams) {
    if (params.TEXTURE_MIN_FILTER !== "NEAREST" || params.TEXTURE_MAG_FILTER !== "NEAREST") incomplete.push(texture.texture);
}
const created = gl.calls.length;  // (all textures created so far were bound and parameterised)
check("every texture it made is NEAREST (integer textures are otherwise incomplete)",
    incomplete.length === 0 && gl.textureParams.size >= 4, `incomplete: ${incomplete}`);

const tableUpload = gl.calls.filter((c) => c[0] === "texImage2D" && c[3] === "R8UI" && c[4] === 16);
check("an id table wider than MAX_TEXTURE_SIZE wraps into rows",
    tableUpload.length === 1 && tableUpload[0][5] === 3, JSON.stringify(tableUpload.map((c) => c.slice(3, 6))));
const subs = gl.calls.filter((c) => c[0] === "texSubImage2D" && c[3] !== 256 && c[5] !== 256);
check("filled as whole rows plus the remainder, without a padded copy",
    subs.length === 2 && subs[0][5] === 16 && subs[0][6] === 2 && subs[1][4] === 2 && subs[1][5] === 8,
    JSON.stringify(subs.map((c) => c.slice(3, 7))));

let stats = { ...gpu.stats };
gl.calls.length = 0;
gpu.drawTile(tile, layer, rendered, 4, 4, "filled");
check("drawing again with the same gate and tile uploads nothing",
    gpu.stats.tableUploads === stats.tableUploads && gpu.stats.tileUploads === stats.tileUploads,
    JSON.stringify(gpu.stats));
layer.gateMask = new Uint8Array(40);
gpu.drawTile(tile, layer, rendered, 4, 4, "filled");
check("a new gate is one table upload", gpu.stats.tableUploads === stats.tableUploads + 1);
layer.lut = { colors: new Uint8Array(4 * 3).fill(200), maxId: 2 };
gpu.drawTile(tile, layer, rendered, 4, 4, "filled");
check("a colour table is uploaded when it arrives", gpu.stats.tableUploads === stats.tableUploads + 2);
gpu.drawTile(tile, layer, rendered, 4, 4, "filled");
check("and not again while it is the same", gpu.stats.tableUploads === stats.tableUploads + 2);
layer.lutVersion = 1;
gpu.drawTile(tile, layer, rendered, 4, 4, "filled");
check("but again when its version moves (edited in place)", gpu.stats.tableUploads === stats.tableUploads + 3);

gl.calls.length = 0;
gl.throwOnDraw = true;
const failed = gpu.drawTile(tile, layer, rendered, 4, 4, "filled");
const lastProgram = gl.calls.filter((c) => c[0] === "useProgram").pop();
check("a draw that throws reports false", failed === false);
check("and still hands the context back to the channel program", lastProgram?.[1] === "channelProgram");

const dense = PlexoraLabelGpu.denseFromMap(new Map([[3, [1, 2, 3, 4]], [1, [9, 9, 9, 0]]]));
check("a sparse colour map becomes the dense shape",
    dense.maxId === 3 && Array.from(dense.colors).join(",") === "0,0,0,0,9,9,9,0,0,0,0,0,1,2,3,4");

const big = { name: "huge", mode: "outlines", lut: null, gateMask: new Uint8Array(PlexoraLabelGpu.MAX_IDS + 1) };
changes = 0;
check("a table reaching 2^24 ids does not draw", gpu.drawTile(tile, big, rendered, 4, 4, "filled") === false);
await new Promise((r) => setTimeout(r, 5));
check("and turns the GPU path off, telling the viewer", gpu.active === false && changes === 1, String(gpu.reason));

// ---------------------------------------------------------------------------
// C. the gate's rules
// ---------------------------------------------------------------------------

const f32 = (xs) => new Float32Array(xs);
function reference(ids, columns, gates) {
    // Written out the way numpy evaluates it: float32 column, bound cast to
    // float32, strict comparisons, AND across known keys.
    const pass = new Set();
    for (let i = 0; i < ids.length; i += 1) {
        let ok = true;
        for (const [key, [lo, hi]] of Object.entries(gates)) {
            if (!columns[key]) continue;
            const v = columns[key][i];
            if (!(v > Math.fround(lo) && v < Math.fround(hi))) ok = false;
        }
        if (ok) pass.add(ids[i]);
    }
    return pass;
}
const ids = new Uint32Array([5, 1, 2, 3, 4, 2]);
const columns = {
    A: f32([0.1, 1, 2, NaN, 3.0000001, 2.5]),
    B: f32([9, 9, 0, 9, 9, 9]),
};
const cases = [
    ["exclusive bounds", { A: [1, 3] }],
    ["a bound that is not a float32", { A: [0.1, 2.5] }],
    ["two keys AND", { A: [0, 10], B: [1, 10] }],
    ["an unknown key is skipped", { A: [0, 10], Z: [100, 200] }],
    ["only unknown keys pass everything", { Z: [100, 200] }],
    ["string bounds", { A: ["0.5", "2.75"] }],
];
for (const [name, gates] of cases) {
    const out = PlexoraLabelGpu.evaluateGateMask(ids, columns, gates);
    const want = reference(ids, columns, Object.fromEntries(Object.entries(gates).map(([k, [a, b]]) => [k, [Number(a), Number(b)]])));
    const got = new Set([...out.mask.keys()].filter((i) => out.mask[i]));
    check(`gate: ${name}`, [...want].sort().join() === [...got].sort().join() && out.count === want.size,
        `want ${[...want].sort()} got ${[...got].sort()} count ${out.count}`);
}
const nan = PlexoraLabelGpu.evaluateGateMask(new Uint32Array([7]), { A: f32([NaN]) }, { A: [-Infinity, Infinity] });
check("gate: NaN never passes", nan.mask[7] === 0 && nan.count === 0);
const dup = PlexoraLabelGpu.evaluateGateMask(new Uint32Array([2, 2]), { A: f32([1, 1]) }, { A: [0, 2] });
check("gate: a repeated id is counted once, as a Set of the server's list is", dup.count === 1);
check("gate: the mask ends at the largest id", dup.mask.length === 3 && nan.mask.length === 8);
// Math.fround is round-to-nearest-even, the same as numpy's float64 -> float32.
check("gate: float32 rounding of a bound matches numpy's for a halfway value",
    Math.fround(16777217) === 16777216 && Math.fround(16777219) === 16777220);

// ---------------------------------------------------------------------------
// D. the CPU reference: alpha tables, and the gate mask in the loop
// ---------------------------------------------------------------------------

for (const fill of [0, 0.25, 0.5, 0.7, 1]) {
    const t = PlexoraLabelTile.alphaTables(fill);
    let ok = true;
    for (let a = 0; a < 256; a += 1) {
        const tint = fill ? Math.round(a * 0.45 * fill) : 0;
        const edge = fill ? Math.max(tint, Math.round(a * (1 - fill))) : a;
        if (t[a] !== tint || t[256 + a] !== edge) ok = false;
    }
    check(`alpha tables match the CPU loop at fill ${fill}`, ok);
}

globalThis.document = {
    createElement: () => {
        const canvas = { width: 0, height: 0 };
        canvas.getContext = () => ({
            canvas,
            createImageData: (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }),
            putImageData(img) { canvas.img = img; },
        });
        return canvas;
    },
};
const gated = PlexoraLabelTile.renderLabelTile(labelTileOf(0)._array, 4, 4,
    { name: "g", mode: "filled", lut: null, filterIds: null, gateMask: new Uint8Array([0, 0, 1]) }, "filled");
const px = gated.canvas.img.data;
check("the CPU loop draws a cell the gate mask passes", px[4 * 2 + 3] === 220);
check("and skips one it does not", px[3] === 0);
const open = PlexoraLabelTile.renderLabelTile(labelTileOf(0)._array, 4, 4,
    { name: "g", mode: "filled", lut: null, filterIds: null }, "filled");
check("with no gate mask it draws every cell (unchanged)", open.canvas.img.data[3] === 220);

console.log(failures.length ? `\n${failures.length} FAILED` : "\nall passed");
process.exit(failures.length ? 1 : 0);
