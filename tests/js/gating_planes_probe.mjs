/**
 * Thresholding a z-stack plane by plane: the gating panel's plane control
 * (plugins/gating/static/gatingPlanes.js).
 *
 * A gate is always applied to each cell's whole-cell value; the slab only
 * changes what the image shows. What is pinned here:
 *
 * - "N positive" on a plane counts the gate's calls exactly as the viewer drew
 *   them (the layer's own gate table), else the cells' values under the gate;
 * - the plane counted is the one whose outlines are drawn (a following
 *   slab's own plane, a fixed slab's middle; nothing in 3-D);
 * - choosing Slab asks the viewer for a following, clipped projection of the
 *   right planes and combination; Plane takes it back to one plane;
 * - a thickness in microns becomes planes centre to centre, from the z step
 *   alone, and without a z step the thickness is in planes;
 * - the slab comes back as the user left it;
 * - the plane's readings are kept per plane and marker, and read ahead;
 * - the plane's shading is on the plot's own axis, smoothed, NaN left out;
 * - the readout says what the gate uses and what the image shows.
 *
 * The real methods, against a hand-built controller, context and view state.
 * Run directly: `node tests/js/gating_planes_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "gating", "static", "gatingPlanes.js");

const requests = [];
const stored = new Map();
function element() {
    return {
        textContent: "", title: "", hidden: false, children: [],
        replaceChildren(...nodes) { this.children = nodes; this.textContent = nodes.map((n) => n.textContent).join(""); },
        append(...nodes) { this.children.push(...nodes); this.textContent = this.children.map((n) => n.textContent).join(""); },
        appendChild(node) { this.append(node); },
        setAttribute() {},
    };
}
const sandbox = {
    console,
    document: { getElementById: () => null, activeElement: null, createElement: () => element() },
    URLSearchParams,
    Uint8Array, Uint32Array, Float32Array, Map, Promise, JSON, Math, Number, String,
    setTimeout: () => 0,
    clearTimeout: () => {},
    localStorage: { getItem: (k) => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v) },
    fetch: async (url) => {
        requests.push(String(url));
        const n = 3;
        const buffer = new ArrayBuffer(8 * n);
        new Uint32Array(buffer, 0, n).set([4, 7, 9]);
        new Float32Array(buffer, 4 * n, n).set([2, 6, 8]);
        return { ok: true, headers: { get: () => JSON.stringify({ n, source: "index" }) },
                 arrayBuffer: async () => buffer };
    },
};
sandbox.window = sandbox;
createContext(sandbox);
runInContext(readFileSync(SOURCE, "utf8"), sandbox);
const { GatePlaneControl, smoothBins } = sandbox;

/** A view state that records what it was asked to do. */
function fakeViewState({ state, zStep = { value: 1, unit: "µm", source: "metadata" } } = {}) {
    return {
        sets: [], state,
        enabled: () => true,
        zStep: () => zStep,
        set(patch) { this.sets.push(patch); return patch; },
    };
}

function control({ gate = [3, 7], mask = null, state = null, zStep, frame = 0 } = {}) {
    const viewState = fakeViewState({ state: state || { mode: "plane", z: 5, slab: {} }, zStep });
    sandbox.__plexora = { viewState };
    const controller = {
        gateMarker: "CD8",
        dataLayer: { getFullChannelName: (name) => name },
        gatingList: { gating_channels: { CD8: gate } },
        layerName: () => "gating",
        redrawPlaneOverlay: () => {},
    };
    controller.ctx = {
        datasource: "stack",
        url: (path) => `/${path}`,
        viewer: { getCellLayer: () => (mask ? { gateMask: mask } : null) },
        view: { enabled: true, depth: 20, get state() { return viewState.state; },
                frame: () => frame, labelsAre3d: () => true },
    };
    const self = new GatePlaneControl(controller);
    self.lastSlab = null;
    return { self, viewState };
}

function entry({ ids = [4, 7, 9], values = [2, 6, 8], header = { source: "index" } } = {}) {
    return { ids: Uint32Array.from(ids), values: Float32Array.from(values), header };
}

const passed = [];
async function check(label, fn) {
    requests.length = 0;
    await fn();
    passed.push(label);
    console.log(`ok  ${label}`);
}

await check("positives are the gate's calls as the viewer drew them", () => {
    const mask = new Uint8Array(12);
    mask[9] = 1;                        // the layer says cell 9 only
    const { self } = control({ mask });
    self.plane = { z: 5, t: 0, column: "CD8" };
    self.entry = entry();               // values 2, 6, 8 would say 7 too
    assert.equal(self.counts().positive, 1);
    assert.equal(self.counts().cells, 3);
});

await check("without the viewer's table, the values under low < v <= high", () => {
    const { self } = control({ gate: [2, 8] });
    self.plane = { z: 5, t: 0, column: "CD8" };
    self.entry = entry();               // 2 is out (not above low), 6 and 8 in
    assert.equal(self.counts().positive, 2);
});

await check("the plane counted is the one whose outlines are drawn", () => {
    const plane = control({ state: { mode: "plane", z: 11, slab: {} }, frame: 2 }).self;
    assert.deepEqual({ ...plane.planeOnScreen() }, { z: 11, t: 2 });
    const following = control({ state: { mode: "slab", z: 1, slab: { z0: 0, z1: 3, follow: true } } }).self;
    assert.equal(following.planeOnScreen().z, 1, "a following slab: its own plane, even cut at an end");
    const fixed = control({ state: { mode: "slab", z: 0, slab: { z0: 4, z1: 9, follow: false } } }).self;
    assert.equal(fixed.planeOnScreen().z, 6, "a fixed slab: its middle");
    const volume = control({ state: { mode: "volume", z: 3, slab: {} } }).self;
    assert.equal(volume.planeOnScreen(), null);
});

await check("Slab asks the viewer for a following, clipped projection; Plane takes it back", () => {
    const { self, viewState } = control();
    self.show("slab", { thickness_um: 5, agg: "median" });
    assert.deepEqual(JSON.parse(JSON.stringify(viewState.sets.at(-1))),
                     { mode: "slab", slab: { thickness: 5, op: "med", follow: true, clip: true } });
    self.show("slab", { thickness_um: 2, agg: "max" });
    assert.equal(viewState.sets.at(-1).slab.thickness, 3);
    assert.equal(viewState.sets.at(-1).slab.op, "max");
    self.show("plane");
    assert.deepEqual(JSON.parse(JSON.stringify(viewState.sets.at(-1))), { mode: "plane" });
});

await check("microns become planes centre to centre, from the z step alone", () => {
    const cases = [[5, 1, 2], [5, 2, 1], [5, 0.5, 5], [5, 3, 0], [6, 3, 1], [2.5, 0.25, 5],
                   [0.4, 0.4, 0]];
    for (const [thickness, step, half] of cases) {
        const { self } = control({ zStep: { value: step, unit: "µm", source: "metadata" } });
        assert.equal(self.halfFor({ thickness_um: thickness }), half, `${thickness} um at ${step} um`);
        // Every plane kept is within half the thickness, and the next one out is not.
        assert.ok(half * step <= thickness / 2 + 1e-9 && thickness / 2 < (half + 1) * step);
    }
});

await check("without a known z step the thickness is in planes", () => {
    const { self, viewState } = control({ zStep: { value: 1, unit: "µm", source: "default" } });
    assert.equal(self.zStepUm(), null, "a guessed step is not a step");
    assert.equal(self.halfFor({ thickness_um: 5 }), null);
    assert.deepEqual({ ...self.slabChoice() }, { planes: 5, agg: "mean" });
    self.show("slab", { planes: 4, agg: "mean" });
    assert.equal(viewState.sets.at(-1).slab.thickness, 5, "an even count stays centred");
});

await check("the slab comes back as the user left it", () => {
    const first = control();
    first.self.show("slab", { thickness_um: 7.5, agg: "max" });
    const again = control();
    again.self.lastSlab = again.self.loadLastSlab();
    assert.deepEqual({ ...again.self.slabChoice() }, { thickness_um: 7.5, agg: "max" });
    const fresh = control();
    assert.deepEqual({ ...fresh.self.slabChoice() }, { thickness_um: 5, agg: "mean" });
    // A following projection set elsewhere (the Depth card) is said in its own planes.
    const elsewhere = control({ state: { mode: "slab", z: 8,
                                         slab: { z0: 4, z1: 12, thickness: 9, op: "max", follow: true } } });
    assert.deepEqual({ ...elsewhere.self.slabChoice() }, { planes: 9, agg: "max", foreign: false });
});

await check("the plane's cells are read once per plane and marker", async () => {
    const { self } = control();
    await self.read(5, 0, "CD8");
    await self.read(5, 0, "CD8");
    assert.equal(requests.length, 1);
    assert.ok(requests[0].startsWith("/volume/stack/plane_cells?"));
    assert.ok(requests[0].includes("z=5") && requests[0].includes("column=CD8"));
    await self.read(5, 0, "CD20");
    assert.equal(requests.length, 2, "another marker is another reading");
    const hit = await self.read(5, 0, "CD8");
    assert.deepEqual([...hit.ids], [4, 7, 9]);
    assert.deepEqual([...hit.values], [2, 6, 8]);
});

await check("the planes ahead of the hand are read once it is still", async () => {
    const { self } = control();
    self.plane = { z: 5, t: 0, column: "CD8" };
    self.direction = -1;
    self.prefetch();
    await Promise.all([...self.inflight.values()]);
    const planes = requests.map((url) => new URLSearchParams(url.split("?")[1]).get("z"));
    assert.deepEqual(planes, ["4", "3", "6"]);
});

await check("the plane's shading: on the plot's own axis, smoothed, NaN left out", () => {
    const { self } = control();
    self.plane = { z: 5, t: 0, column: "CD8" };
    self.entry = entry({ ids: [1, 2, 3, 4, 5], values: [0, 0, 10, Number.NaN, 4.99] });
    let drawn = null;
    const path = { datum(points) { drawn = points; return path; }, attr() { return path; } };
    const group = { insert: () => path };
    const area = () => { const a = { x: () => a, y0: () => a, y1: () => a, curve: () => a }; return a; };
    sandbox.d3 = { area, curveMonotoneX: null };
    const xScale = (v) => v;
    xScale.domain = () => [0, 10];
    self.drawOverlay(group, { xScale, innerHeight: 100, line: true });
    assert.equal(drawn.length, 50);
    const expected = smoothBins((() => {
        const bins = new Array(50).fill(0);
        bins[0] = 2; bins[49] = 1; bins[24] = 1;
        return bins;
    })());
    const peak = Math.max(...expected);
    // Plain copies: arrays made inside the sandbox are another realm's.
    assert.deepEqual(JSON.parse(JSON.stringify(drawn.map((p) => p.y))),
                     JSON.parse(JSON.stringify(expected.map((v) => v / peak))));
    assert.equal(drawn[0].y, 1, "the two zeros are the peak");
    assert.ok(drawn[49].y > 0 && drawn[48].y > 0, "the top value lands in the last bin, not past it");
});

await check("smoothing keeps a lone bin's shape and does not dim the ends", () => {
    const counts = new Array(9).fill(0);
    counts[4] = 16;
    assert.deepEqual(Array.from(smoothBins(counts).slice(2, 7)), [1, 4, 6, 4, 1]);
    const edge = new Array(5).fill(0);
    edge[0] = 11;
    assert.equal(smoothBins(edge)[0], 66 / 11, "the end bin is divided by the weights it has");
});

await check("the readout says what the gate uses and what the image shows", () => {
    const { self } = control({ state: { mode: "slab", z: 12,
                                        slab: { z0: 10, z1: 14, op: "mean", follow: true } } });
    self.root = { hidden: false };
    self.readout = element();
    self.plane = { z: 12, t: 0, column: "CD8" };
    self.entry = entry();
    self.paintReadout();
    assert.equal(self.readout.textContent, "z 12 · 1 of 3 positive");
    assert.match(self.readout.title, /Gates use each cell's whole-cell value, the same on every plane/);
    assert.match(self.readout.title, /The image shows the mean of planes 10–14/);
    self.entry = entry({ header: { source: "bbox" } });
    self.paintReadout();
    assert.match(self.readout.title, /measure the cells again for an exact count/);
});

console.log(`all checks passed (${passed.length})`);
