/**
 * Shapes: the geometry the browser runs, and the tables both languages read.
 *
 * A shape is nodes in a normalised box. The browser draws from those nodes, the
 * server validates them, `compose` turns them into millimetres and both
 * exporters walk the result -- so the same arithmetic exists in JavaScript and
 * in Python, and a disagreement shows the user one shape and prints another.
 * Nothing else in the suite would notice: the canvas renders from the browser's
 * answer and the PDF from Python's, and neither is ever compared to the other.
 *
 * So this probe owns ONE case table, emits its own answers beside the inputs,
 * and `test_figure_builder_shapes.py` pushes the identical inputs through
 * `schema.normalize_shape` and `server/shapegeom.py`. The table lives here
 * rather than being written out twice and drifting, which is the failure it
 * exists to catch.
 *
 * Each of the self-checks below would ship green and be wrong somewhere a user
 * only sees later:
 *
 *   * a preset whose ink does not fill its box -- invisible until the shape is
 *     rotated, because the box centre is what all three renderers turn about,
 *     so the shape moves rather than merely sitting off-centre;
 *
 *   * `renormalize` that forgets the rotation -- a node dragged on a rotated
 *     shape makes the whole shape jump, and only a rotated one;
 *
 *   * `splitSegment` that drops a point on the chord instead of subdividing --
 *     adding a node visibly changes a curve at the exact moment the user is
 *     watching it;
 *
 *   * a normaliser that throws on garbage -- `normalize_document` reads a raise
 *     as "drop this annotation", so the shape disappears on the next reload.
 *
 * Run directly:
 *   node tests/js/figure_shape_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureShapeGeometry.js", "figureShapeDefs.js"];

const problems = [];

function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

function near(what, got, want, tolerance) {
    if (!(Math.abs(got - want) <= (tolerance === undefined ? 1e-9 : tolerance))) {
        problems.push({ what, got: String(got), want: String(want) });
    }
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    isFinite, RegExp, Date, Error, parseFloat, isNaN, Infinity, NaN,
});
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}
const run = (source) => runInContext(source, ctx);
const G = run("FigureShapeGeometry");
const D = run("FigureShapeDefs");

// -- constants the Python side asserts equal to its own --------------------

const constants = run(`({
    KAPPA: FigureShapeGeometry.KAPPA,
    MAX_NODES: FigureShapeGeometry.MAX_NODES,
    COORD_SLACK: FigureShapeGeometry.COORD_SLACK,
    MAX_FLATTEN_DEPTH: FigureShapeGeometry.MAX_FLATTEN_DEPTH,
    PRESET_IDS: FigureShapeGeometry.PRESET_IDS,
})`);

// -- the case table both languages normalise -------------------------------

const node = (x, y, type, into, out) =>
    ({ x, y, type: type || "corner", in: into || null, out: out || null });

const NORMALIZE_CASES = [
    { name: "nothing at all becomes the unit rectangle", shape: null },
    { name: "a string where an object belongs", shape: "rectangle" },
    { name: "an object with no nodes key", shape: { preset: "pentagon" } },
    { name: "nodes that are not a list", shape: { preset: "rect", nodes: "M0 0" } },
    { name: "a plain rectangle survives untouched",
      shape: { preset: "rect", closed: true,
               nodes: [node(0, 0), node(1, 0), node(1, 1), node(0, 1)] } },
    { name: "two nodes cannot enclose anything, so a closed path falls back",
      shape: { preset: "rect", closed: true, nodes: [node(0, 0), node(1, 1)] } },
    { name: "two nodes ARE an open path",
      shape: { preset: "custom", closed: false, nodes: [node(0, 0), node(1, 1)] } },
    { name: "one node is not a path even open",
      shape: { preset: "custom", closed: false, nodes: [node(0, 0)] } },
    { name: "an unknown preset becomes custom, geometry kept",
      shape: { preset: "dodecahedron", closed: true,
               nodes: [node(0, 0), node(1, 0), node(0.5, 1)] } },
    { name: "a preset arriving with padding and control characters",
      shape: { preset: "  hexagon ", closed: true,
               nodes: [node(0, 0), node(1, 0), node(0.5, 1)] } },
    { name: "closed defaults to true when the key is absent",
      shape: { preset: "rect", nodes: [node(0, 0), node(1, 0), node(0.5, 1)] } },
    { name: "closed: 0 is open",
      shape: { preset: "custom", closed: 0, nodes: [node(0, 0), node(1, 1)] } },
    { name: "non-object node entries are skipped, not fatal",
      shape: { preset: "custom", closed: true,
               nodes: [node(0, 0), "nope", null, 7, node(1, 0), node(0.5, 1)] } },
    { name: "unreadable coordinates become zero rather than NaN",
      shape: { preset: "custom", closed: true,
               nodes: [{ x: "0.5", y: null }, { x: true, y: 0.5 },
                       { x: 0.5, y: 1 }] } },
    { name: "coordinates far outside the box are clamped, not rescaled",
      shape: { preset: "custom", closed: true,
               nodes: [node(-99, 0), node(1e30, 0), node(0.5, 42)] } },
    { name: "a handle missing one coordinate is no handle at all",
      shape: { preset: "custom", closed: true,
               nodes: [node(0, 0, "smooth", { x: 0.1 }, { x: 0.2, y: 0.2 }),
                       node(1, 0, "smooth", "nope", null),
                       node(0.5, 1, "corner", null, { x: 9, y: 9 })] } },
    { name: "node type is smooth only when it says so exactly",
      shape: { preset: "custom", closed: true,
               nodes: [node(0, 0, "SMOOTH"), node(1, 0, "smooth"),
                       node(0.5, 1, 7)] } },
    { name: "600 nodes are sliced to the ceiling",
      shape: { preset: "custom", closed: true,
               nodes: Array.from({ length: 600 }, (unused, index) =>
                   node(index / 600, (index % 7) / 7)) } },
];

const normalizeCases = NORMALIZE_CASES.map((one) => ({
    name: one.name,
    input: one.shape,
    output: G.normalize(one.shape),
}));

// Normalising twice must change nothing, or the stored form is not canonical
// and every equality check downstream -- including the no-op guard before a
// commit -- is unreliable.
for (const one of normalizeCases) {
    check(`normalise is idempotent: ${one.name}`, G.normalize(one.output), one.output);
}

// -- the preset tables, which Python re-validates and re-measures -----------

const presets = D.GRID.map((id) => {
    const preset = D.byId(id);
    return {
        id,
        label: preset.label,
        aspect: preset.aspect,
        closed: preset.closed,
        nodes: preset.nodes,
        bounds: G.inkBounds(preset.nodes, preset.closed),
        d: G.pathD(preset.nodes, preset.closed),
    };
});

check("the grid offers every preset id except custom",
      D.GRID.slice().sort(),
      G.PRESET_IDS.filter((id) => id !== "custom").sort());

for (const preset of presets) {
    // The load-bearing one. `w_mm`/`h_mm` are the box the renderers rotate
    // about, so ink that does not fill its box moves a rotated shape.
    near(`${preset.id}: ink starts at the box's left edge`, preset.bounds.x, 0);
    near(`${preset.id}: ink starts at the box's top edge`, preset.bounds.y, 0);
    near(`${preset.id}: ink fills the box's width`, preset.bounds.w, 1);
    near(`${preset.id}: ink fills the box's height`, preset.bounds.h, 1);
    check(`${preset.id}: survives normalisation unchanged`,
          G.normalize({ preset: preset.id, closed: preset.closed, nodes: preset.nodes }),
          { preset: preset.id, closed: preset.closed, nodes: preset.nodes });
    if (!(preset.aspect > 0.05 && preset.aspect < 20)) {
        problems.push({ what: `${preset.id}: implausible aspect`,
                        got: String(preset.aspect), want: "0.05..20" });
    }
    const icon = D.icon(preset.id);
    if (!icon.includes(preset.d) || !icon.includes("non-scaling-stroke")) {
        problems.push({ what: `${preset.id}: the icon does not draw the preset's own path`,
                        got: icon.slice(0, 120), want: preset.d.slice(0, 120) });
    }
}

// The closing edge is drawn explicitly and THEN closed, which mirrors what
// `segmentsMm` emits: the exporters walk edges, and one of them having an
// implicit final edge that the other draws is the kind of difference that only
// shows up as a missing line in a PDF.
check("a rectangle's path is the four straight edges and a close",
      G.pathD(D.byId("rect").nodes, true), "M 0 0 L 1 0 L 1 1 L 0 1 L 0 0 Z");

for (const tool of D.CUSTOM_TOOLS) {
    if (!D.customIcon(tool.id).includes("<path")) {
        problems.push({ what: `${tool.id}: drawing tool has no icon`, got: "", want: "<path" });
    }
}

// -- placement and flattening ----------------------------------------------

const BOXES = [
    { name: "a square box at the origin",
      geometry: { x_mm: 0, y_mm: 0, w_mm: 10, h_mm: 10, rotation: 0 } },
    { name: "an offset, non-square box",
      geometry: { x_mm: 37.5, y_mm: 12.25, w_mm: 60, h_mm: 18, rotation: 0 } },
];

const segments = [];
for (const id of ["rect", "ellipse", "pentagon", "capsule"]) {
    const preset = D.byId(id);
    for (const box of BOXES) {
        const shape = { preset: id, closed: preset.closed, nodes: preset.nodes };
        const emitted = G.segmentsMm(shape, box.geometry);
        segments.push({
            name: `${id} in ${box.name}`,
            shape, geometry: box.geometry, segments: emitted,
            flattened: G.flatten(emitted, 0.05),
        });
    }
}

// An open path emits no close and one fewer edge than a closed one.
const openShape = { preset: "custom", closed: false,
                    nodes: [node(0, 0), node(0.5, 1), node(1, 0)] };
segments.push({
    name: "an open three-node path",
    shape: openShape, geometry: BOXES[0].geometry,
    segments: G.segmentsMm(openShape, BOXES[0].geometry),
    flattened: G.flatten(G.segmentsMm(openShape, BOXES[0].geometry), 0.05),
});
check("an open path does not close itself",
      G.segmentsMm(openShape, BOXES[0].geometry).some((s) => s[0] === "close"), false);

// A flattened ellipse is a circle when its box is square: every point on it is
// the same distance from the centre, within the tolerance that was asked for.
const circle = G.flatten(G.segmentsMm(
    { preset: "ellipse", closed: true, nodes: D.byId("ellipse").nodes },
    { x_mm: 0, y_mm: 0, w_mm: 10, h_mm: 10, rotation: 0 }), 0.02);
let worst = 0;
for (const [x, y] of circle) worst = Math.max(worst, Math.abs(Math.hypot(x - 5, y - 5) - 5));
near("a flattened ellipse stays on its own circle", worst, 0, 0.03);
if (circle.length < 20 || circle.length > 400) {
    problems.push({ what: "a flattened ellipse has a workable number of points",
                    got: String(circle.length), want: "20..400" });
}

// -- renormalisation, rotated and not --------------------------------------

/** Where a point in the box's local mm frame actually sits on the page. */
const world = (point, geometry) => {
    const turned = G.turn(point.x - geometry.w_mm / 2, point.y - geometry.h_mm / 2,
                          geometry.rotation || 0);
    return { x: geometry.x_mm + geometry.w_mm / 2 + turned.x,
             y: geometry.y_mm + geometry.h_mm / 2 + turned.y };
};

const RENORMALIZE_CASES = [
    { name: "a node dragged outside an upright box",
      geometry: { x_mm: 100, y_mm: 50, w_mm: 40, h_mm: 20, rotation: 0 },
      closed: true,
      local: [{ x: 0, y: 0 }, { x: 60, y: -10 }, { x: 40, y: 20 }, { x: 0, y: 20 }] },
    { name: "the same drag on a box rotated 30 degrees",
      geometry: { x_mm: 100, y_mm: 50, w_mm: 40, h_mm: 20, rotation: 30 },
      closed: true,
      local: [{ x: 0, y: 0 }, { x: 60, y: -10 }, { x: 40, y: 20 }, { x: 0, y: 20 }] },
    { name: "a shape dragged wholly off its own box, rotated -117 degrees",
      geometry: { x_mm: 12, y_mm: 8, w_mm: 25, h_mm: 25, rotation: -117 },
      closed: true,
      local: [{ x: 30, y: 30 }, { x: 55, y: 30 }, { x: 42, y: 60 }] },
    { name: "a flat open path, which has no height at all",
      geometry: { x_mm: 5, y_mm: 5, w_mm: 30, h_mm: 10, rotation: 0 },
      closed: false,
      local: [{ x: 0, y: 5 }, { x: 15, y: 5 }, { x: 30, y: 5 }] },
];

const renormalizeCases = RENORMALIZE_CASES.map((one) => {
    const nodes = one.local.map((p) => node(p.x, p.y));
    const out = G.renormalize(nodes, one.closed, one.geometry);
    // The whole point: the shape does not move. Every anchor's page position
    // before and after must be the same, whatever the rotation.
    for (let index = 0; index < nodes.length; index += 1) {
        const before = world(nodes[index], one.geometry);
        const after = world({ x: out.nodes[index].x * out.geometry.w_mm,
                              y: out.nodes[index].y * out.geometry.h_mm }, out.geometry);
        near(`${one.name}: node ${index} keeps its page x`, after.x, before.x, 1e-9);
        near(`${one.name}: node ${index} keeps its page y`, after.y, before.y, 1e-9);
    }
    return { name: one.name, geometry: one.geometry, closed: one.closed,
             nodes, output: out };
});

check("renormalising a box that already fits changes nothing",
      G.renormalize([node(0, 0), node(10, 0), node(10, 5), node(0, 5)], true,
                    { x_mm: 3, y_mm: 4, w_mm: 10, h_mm: 5, rotation: 0 }).geometry,
      { x_mm: 3, y_mm: 4, w_mm: 10, h_mm: 5, rotation: 0 });

// -- splitting, projecting, smoothing, simplifying -------------------------

const curveStart = node(0, 0, "smooth", null, { x: 0.4, y: -0.4 });
const curveEnd = node(1, 0, "smooth", { x: 0.6, y: 0.4 }, null);
const split = G.splitSegment(curveStart, curveEnd, 0.5);
const midpoint = G.pointAt(curveStart, curveEnd, 0.5);
near("splitting a curve puts the node ON the curve (x)", split.node.x, midpoint.x);
near("splitting a curve puts the node ON the curve (y)", split.node.y, midpoint.y);

// The two halves must reproduce the original curve, not merely start and end
// in the same places -- that is the difference between subdividing and
// dropping a point on the chord.
const halfA = [{ ...curveStart, out: split.startOut }, split.node];
const halfB = [{ ...split.node, in: split.node.in, out: split.node.out },
               { ...curveEnd, in: split.endIn }];
let drift = 0;
for (let step = 0; step <= 10; step += 1) {
    const t = step / 10;
    const whole = G.pointAt(curveStart, curveEnd, t / 2);
    const part = G.pointAt(halfA[0], halfA[1], t);
    drift = Math.max(drift, Math.hypot(whole.x - part.x, whole.y - part.y));
    const wholeLate = G.pointAt(curveStart, curveEnd, 0.5 + t / 2);
    const partLate = G.pointAt(halfB[0], halfB[1], t);
    drift = Math.max(drift, Math.hypot(wholeLate.x - partLate.x, wholeLate.y - partLate.y));
}
near("the two halves of a split curve ARE the original curve", drift, 0, 1e-12);

check("splitting a straight edge inserts a corner and touches no handles",
      G.splitSegment(node(0, 0), node(1, 1), 0.25),
      { node: { x: 0.25, y: 0.25, type: "corner", in: null, out: null },
        startOut: null, endIn: null });

const projected = G.nearestOnSegment(node(0, 0), node(10, 0), { x: 4, y: 3 });
near("a point projects onto a straight edge exactly", projected.t, 0.4);
near("and reports how far away it was", projected.distance, 3);
near("a point past the end of an edge clamps to the end",
     G.nearestOnSegment(node(0, 0), node(10, 0), { x: 40, y: 0 }).t, 1);

const smooth = G.catmullRom([{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }, { x: 0, y: 1 }], true);
check("a closed smooth path gives every node two handles",
      smooth.every((n) => n.type === "smooth" && n.in && n.out), true);
const openSmooth = G.catmullRom([{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 1 }], false);
check("an open smooth path has no handle before its first node", openSmooth[0].in, null);
check("nor after its last", openSmooth[2].out, null);

const noisy = [];
for (let index = 0; index <= 100; index += 1) {
    noisy.push({ x: index / 100, y: (index % 2 === 0 ? 0.0004 : -0.0004) });
}
const simplified = G.rdp(noisy, 0.01);
check("a hundred samples along a jittery straight line reduce to its ends",
      simplified.length, 2);
check("simplification keeps the first point", simplified[0], noisy[0]);
check("and the last", simplified[1], noisy[noisy.length - 1]);
const corner = G.rdp([{ x: 0, y: 0 }, { x: 0.5, y: 0.001 }, { x: 1, y: 0 },
                      { x: 1, y: 1 }, { x: 0.999, y: 0.5 }], 0.01);
check("but never a corner that carries the shape", corner.length, 4);

// Shift, in both the drawing tools and the point editor. Projected onto the
// chosen axis rather than having its smaller component zeroed: zeroing turns a
// 40mm diagonal drag into a 40mm horizontal one, so the thing being dragged
// travels further than the pointer did.
const straight = G.constrainDelta(40, 3);
near("a shallow drag snaps flat", straight.y, 0);
near("and keeps only what it travelled along that axis", straight.x, 40);
const diagonal = G.constrainDelta(30, 26);
near("a near-diagonal drag snaps to 45 (x)", diagonal.x, diagonal.y, 1e-9);
near("and is the projection, not the length", diagonal.x, (30 + 26) / 2, 1e-9);
check("a drag that has not moved stays put", G.constrainDelta(0, 0), { x: 0, y: 0 });
near("a vertical drag snaps vertical", G.constrainDelta(-2, -25).x, 0);

console.error(JSON.stringify({
    problems, constants, normalizeCases, presets, segments, renormalizeCases,
}));
process.exitCode = problems.length ? 1 : 0;
