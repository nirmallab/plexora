/**
 * The page arithmetic, which decides what a figure actually looks like.
 *
 * All of it is pure and none of it runs anywhere else in the suite. Each of the
 * mistakes below would ship with a green suite and produce a figure that is
 * subtly, expensively wrong:
 *
 * * a corner resize that does not keep the aspect ratio silently squashes the
 *   tissue in a panel, which is a scientific error dressed as a layout one;
 *
 * * "distribute" implemented as equal CENTRES rather than equal GAPS looks
 *   right only when every panel is the same size, which for a figure of mixed
 *   crops is almost never;
 *
 * * a snap threshold in millimetres rather than screen pixels is unusably
 *   sticky zoomed in and does nothing at all zoomed out;
 *
 * * labels assigned in capture order rather than reading order give a 3x2 grid
 *   labelled by when each field was found, which is not what any reader expects.
 *
 * Run directly:
 *   node tests/js/figure_layout_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
// figureRichText.js carries the typographic tables and the run model that
// FigureCanvas reaches for while drawing text. A real page loads every file
// in PLUGIN.scripts, so leaving it out here is a fixture that is missing a
// dependency rather than a dependency that is optional.
const SCRIPTS = ["figureSchema.js", "figureRichText.js", "figureCanvas.js"];

const problems = [];
const commits = [];

function elementStub() {
    return {
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {} },
        innerHTML: "",
        addEventListener() {},
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        querySelector: () => null,
        querySelectorAll: () => [],
    };
}

function browserGlobals() {
    const globals = {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Infinity, parseFloat, isNaN,
        setTimeout: () => 1, clearTimeout: () => {},
        requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
        document: {
            readyState: "complete",
            activeElement: null,
            getElementById: () => null,
            createElement: () => elementStub(),
            addEventListener() {}, removeEventListener() {},
        },
    };
    globals.window = {
        crypto: { randomUUID: () => "0123456789abcdef0123456789abcdef" },
        addEventListener() {}, removeEventListener() {},
        devicePixelRatio: 1,
    };
    globals.crypto = globals.window.crypto;
    return globals;
}

const ctx = createContext(browserGlobals());
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

/** A4, four panels, one of them a different size -- which is what makes the
 *  distribute and same-size cases mean anything. */
function documentFixture() {
    const panel = (id, x, y, w, h, z) => ({
        panel_id: id,
        source_id: "src_1",
        scene: { viewport: { x: 0, y: 0, w: 1000, h: 800 } },
        placement: { page_id: "pg_1", x_mm: x, y_mm: y, w_mm: w, h_mm: h, z: z },
        label: { text: "", auto: true, visible: true },
        title: "",
        scalebar: { visible: false, target_um: null },
        legend: { channels: false, plugins: false },
        link_group: null,
        render_revision: 1,
    });
    return {
        schema_version: 1, revision: 3, figure_id: "fig_test", title: "Test",
        sources: { src_1: { source_id: "src_1", kind: "plexora_project",
                            datasource: "demo", pixel_size: { value: 0.5, unit: "µm" } } },
        pages: [{
            page_id: "pg_1", name: "Page 1", preset: "a4", orientation: "portrait",
            size_mm: { w: 210, h: 297 },
            margins_mm: { top: 10, right: 10, bottom: 10, left: 10 },
            background: "#ffffff",
        }],
        // Deliberately out of reading order in the map, so the label ordering
        // below is actually asserting something.
        panels: {
            pnl_d: panel("pnl_d", 70, 60, 40, 30, 3),
            pnl_a: panel("pnl_a", 20, 20, 40, 30, 0),
            pnl_c: panel("pnl_c", 20, 60, 40, 30, 2),
            pnl_b: panel("pnl_b", 70, 20, 60, 30, 1),
            pnl_tray: { ...panel("pnl_tray", 0, 0, 0, 0, 0), placement: null },
        },
        annotations: {}, link_groups: {},
        settings: { dpi_default: 300, label_style: "A",
                    style: { gutter_mm: 3, font_size_pt: 8, label_size_pt: 10,
                             title_size_pt: 9, line_width_pt: 0.75,
                             font_family: "Helvetica", text_color: "#000000",
                             panel_background: "#000000" } },
    };
}

ctx.__makeCanvas = () => null;
ctx.__fixture = documentFixture;
ctx.__record = (operations) => { commits.push(operations); };
ctx.__commitCount = () => commits.length;
ctx.__lastCommit = () => commits[commits.length - 1];
ctx.__element = elementStub;

runInContext(`
    globalThis.__buildCanvas = function () {
        const document_ = __fixture();
        const state = {
            document: document_,
            sourceStatus: {},
            panel: (id) => document_.panels[id] || null,
            source: (id) => document_.sources[id] || null,
            commit: (operations, mutate) => {
                __record(operations);
                if (mutate) mutate(document_);
                return Promise.resolve(true);
            },
        };
        const canvas = new FigureCanvas({
            state: state,
            api: { previewUrl: () => "preview" },
            figureId: "fig_test",
            pageEl: __element(),
            surfaceEl: __element(),
            guideEl: __element(),
        });
        canvas.pageId = "pg_1";
        return canvas;
    };
`, ctx);

function check(label, actual, expected) {
    const a = JSON.stringify(actual);
    const b = JSON.stringify(expected);
    if (a !== b) problems.push(`${label}: expected ${b}, got ${a}`);
}

function close(label, actual, expected, tolerance = 1e-6) {
    if (Math.abs(actual - expected) > tolerance) {
        problems.push(`${label}: expected ${expected}, got ${actual}`);
    }
}

function run(expression) {
    return runInContext(`(() => { const canvas = __buildCanvas(); ${expression} })()`, ctx);
}

// -- millimetres are the unit ---------------------------------------------

const units = run(`
    return { pxAt100: canvas.toPx(210), roundTrip: canvas.toMm(canvas.toPx(37.5)) };
`);
// 210 mm at 96 dpi is 793.7 CSS pixels -- an A4 page at 100%.
close("A4 at 100%", units.pxAt100, 210 * (96 / 25.4), 1e-9);
close("mm survive a round trip", units.roundTrip, 37.5, 1e-9);

// -- resizing --------------------------------------------------------------

const resize = run(`
    const start = { page_id: "pg_1", x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30, z: 0 };
    return {
        cornerLocked: canvas.resizedBox(start, "se", 20, 0, false),
        cornerFree: canvas.resizedBox(start, "se", 20, 0, true),
        edgeEast: canvas.resizedBox(start, "e", 10, 99, false),
        edgeSouth: canvas.resizedBox(start, "s", 99, 10, false),
        northWest: canvas.resizedBox(start, "nw", 10, 0, true),
        tooSmall: canvas.resizedBox(start, "se", -100, -100, true),
        tooSmallNorth: canvas.resizedBox(start, "nw", 100, 100, true),
    };
`);
// A corner keeps the shape of the region the panel shows. 40x30 grown by 20 mm
// wide stays 4:3, so the height follows to 45.
check("a corner resize keeps the aspect ratio", resize.cornerLocked,
    { page_id: "pg_1", x_mm: 20, y_mm: 20, w_mm: 60, h_mm: 45, z: 0 });
check("Shift frees the aspect ratio", resize.cornerFree,
    { page_id: "pg_1", x_mm: 20, y_mm: 20, w_mm: 60, h_mm: 30, z: 0 });
// An edge handle is single-axis by definition and ignores the other one.
check("an east handle changes only the width", resize.edgeEast,
    { page_id: "pg_1", x_mm: 20, y_mm: 20, w_mm: 50, h_mm: 30, z: 0 });
check("a south handle changes only the height", resize.edgeSouth,
    { page_id: "pg_1", x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 40, z: 0 });
// Dragging the north-west corner moves the origin as well as the size.
check("a north-west handle moves the origin", resize.northWest,
    { page_id: "pg_1", x_mm: 30, y_mm: 20, w_mm: 30, h_mm: 30, z: 0 });
// Below the floor the panel stops shrinking rather than inverting -- an
// inverted box has handles the user cannot grab to undo it with.
check("a panel cannot be shrunk past the floor", resize.tooSmall,
    { page_id: "pg_1", x_mm: 20, y_mm: 20, w_mm: 5, h_mm: 5, z: 0 });
check("shrinking from the north-west keeps the far edge still", resize.tooSmallNorth,
    { page_id: "pg_1", x_mm: 55, y_mm: 45, w_mm: 5, h_mm: 5, z: 0 });

// -- snapping --------------------------------------------------------------

const snap = run(`
    canvas.scale = 96 / 25.4;
    canvas.selection = new Set(["pnl_a"]);
    canvas.gesture = { items: canvas.gestureItems(), origin: { x: 0, y: 0 } };
    return {
        // pnl_a starts at x=20 (the left margin is 10, pnl_c's left edge is 20).
        // Nudged to 10.4, its left edge is within a pixel of the margin at 10.
        toMargin: canvas.snapMove(-9.6, 0),
        // Far from anything: left alone.
        free: canvas.snapMove(-30, 0),
        // pnl_b's left edge is at 70; moving pnl_a to 69.6 snaps it there.
        toNeighbour: canvas.snapMove(49.6, 0),
    };
`);
close("a near miss snaps onto the page margin", snap.toMargin.dx, -10);
close("a distant move is not snapped", snap.free.dx, -30);
close("a near miss snaps onto another panel's edge", snap.toNeighbour.dx, 50);

const snapZoomedOut = run(`
    // A tenth of the scale: the same 6-pixel threshold is now ten times as many
    // millimetres, so a move that was too far to snap at 100% now snaps.
    canvas.scale = (96 / 25.4) / 10;
    canvas.selection = new Set(["pnl_a"]);
    canvas.gesture = { items: canvas.gestureItems(), origin: { x: 0, y: 0 } };
    return canvas.snapMove(-8, 0);
`);
close("the snap threshold is in screen pixels, not millimetres", snapZoomedOut.dx, -10);

// -- distributing and packing ---------------------------------------------

const distributed = run(`
    // Three boxes of DIFFERENT widths, which is the case equal-centres gets
    // wrong: 20..60, 70..130, 150..190.
    const boxes = [
        { x_mm: 20, w_mm: 40 }, { x_mm: 70, w_mm: 60 }, { x_mm: 150, w_mm: 40 },
    ];
    canvas.distribute(boxes, "x_mm", "w_mm", 20, 190);
    return boxes.map((b) => [b.x_mm, b.x_mm + b.w_mm]);
`);
// Span 170, panels 140, so two gaps of 15 each.
check("distribute produces equal gaps, not equal centres", distributed,
    [[20, 60], [75, 135], [150, 190]]);

const packed = run(`
    const boxes = [
        { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30 },
        { x_mm: 90, y_mm: 55, w_mm: 60, h_mm: 30 },
        { x_mm: 20, y_mm: 90, w_mm: 40, h_mm: 20 },
        { x_mm: 90, y_mm: 90, w_mm: 40, h_mm: 30 },
    ];
    canvas.pack(boxes, "grid");
    return boxes;
`);
// A 2x2 grid from the selection's top-left, every cell the largest panel's size
// (60x30) plus the document's 3 mm gutter.
check("a grid packs from the top-left with the document gutter", packed, [
    { x_mm: 20, y_mm: 20, w_mm: 60, h_mm: 30 },
    { x_mm: 83, y_mm: 20, w_mm: 60, h_mm: 30 },
    { x_mm: 20, y_mm: 53, w_mm: 60, h_mm: 30 },
    { x_mm: 83, y_mm: 53, w_mm: 60, h_mm: 30 },
]);

// -- align and match reach ANNOTATIONS, not only panels --------------------
//
// `arrange` read `selectedPanels()` and nothing else, so selecting two captions
// and pressing Align left ran the whole arithmetic over an empty list and
// returned -- a popover with six live rows in it, none of which did anything to
// the objects it was opened over. Nothing in the suite went near it, because
// every test of the layout arithmetic called `distribute` and `pack` directly
// with a list of boxes already in hand.

const withText = (extra) => `
    canvas.state.document.annotations = {
        ann_a: { annotation_id: "ann_a", type: "text", page_id: "pg_1", z: 0,
                 geometry: { x_mm: 30, y_mm: 100, w_mm: 40, h_mm: 6, rotation: 0 } },
        ann_b: { annotation_id: "ann_b", type: "text", page_id: "pg_1", z: 1,
                 geometry: { x_mm: 55, y_mm: 120, w_mm: 20, h_mm: 6, rotation: 0 } },
        ann_line: { annotation_id: "ann_line", type: "line", page_id: "pg_1", z: 2,
                    geometry: { x_mm: 10, y_mm: 200, w_mm: 30, h_mm: -10, rotation: 0 } },
    };
    ${extra}`;

const aligned = run(withText(`
    canvas.selection = new Set(["ann_a", "ann_b"]);
    canvas.arrange("left");
    const at = canvas.state.document.annotations;
    return [at.ann_a.geometry.x_mm, at.ann_b.geometry.x_mm];
`));
check("two captions align to each other", aligned, [30, 30]);

const matched = run(withText(`
    canvas.selection = new Set(["ann_a", "ann_b"]);
    canvas.arrange("same_width");
    const at = canvas.state.document.annotations;
    return [at.ann_a.geometry.w_mm, at.ann_b.geometry.w_mm];
`));
check("and match each other's width", matched, [40, 40]);

// The rotation is the annotation's fifth key and the panel's is `z`; a box that
// came back without them would have silently un-rotated the caption or reset
// its place in the stack.
const kept = run(withText(`
    canvas.state.document.annotations.ann_a.geometry.rotation = 15;
    canvas.selection = new Set(["ann_a", "ann_b"]);
    canvas.arrange("top");
    const at = canvas.state.document.annotations;
    return { rotation: at.ann_a.geometry.rotation, z: canvas.state.document.panels.pnl_b.placement.z };
`));
check("aligning a caption keeps its rotation", kept, { rotation: 15, z: 1 });

// A line's w_mm/h_mm are the two components of a vector, so "same width" on one
// would reverse its direction rather than resize it. It is left out entirely,
// which is also what FigureSelection.arrangeable counts.
const skipped = run(withText(`
    canvas.selection = new Set(["ann_a", "ann_line"]);
    return canvas.arrangeItems().map((item) => item.id);
`));
check("a line is not something to line up", skipped, ["ann_a"]);

// One commit, not two: aligning a caption to a panel is one thing the user did,
// and two commits would be two presses of Ctrl+Z with the figure sitting
// half-aligned in between.
const mixedCommit = run(withText(`
    canvas.selection = new Set(["pnl_a", "ann_a"]);
    const before = __commitCount();
    canvas.arrange("left");
    const at = canvas.state.document.annotations;
    // Defaulted to an empty list because the way this fails is by not
    // committing at all, and a probe that throws reports nothing -- including
    // the three checks above it.
    return { commits: __commitCount() - before,
             ops: (__lastCommit() || []).map((op) => op.op),
             caption: at.ann_a.geometry.x_mm,
             panel: canvas.state.document.panels.pnl_a.placement.x_mm };
`));
check("a panel and a caption align in one undo step", mixedCommit, {
    commits: 1, ops: ["move_panels", "update_annotation"],
    caption: 20, panel: 20,
});

// -- labels follow reading order ------------------------------------------

const labels = run(`
    const panels = FigureSchema.panelsOnPage(canvas.state.document, "pg_1");
    return panels.map((panel, index) => [panel.panel_id, FigureSchema.labelFor(index, "A")]);
`);
// Rows before columns: the top row left-to-right, then the bottom row. Capture
// order and z-order are both different from this, which is the point.
check("labels run in reading order", labels,
    [["pnl_a", "A"], ["pnl_b", "B"], ["pnl_c", "C"], ["pnl_d", "D"]]);

// The geometry that produced those labels, emitted so the PYTHON side can be
// held to the same answer over the same fixture. Two sort orders is two
// figures: the canvas would say A B C D and the exported PDF something else,
// and the author would only find out from the file.
const orderingFixture = run(`
    return FigureSchema.panelsOnPage(canvas.state.document, "pg_1").map((panel) => ({
        panel_id: panel.panel_id,
        x_mm: panel.placement.x_mm,
        y_mm: panel.placement.y_mm,
        w_mm: panel.placement.w_mm,
        h_mm: panel.placement.h_mm,
        z: panel.placement.z,
    }));
`);

// -- reopening a panel frames the shape it is NOW --------------------------
//
// A panel is captured at one proportion and dragged into another; a square
// field cropped into a wide strip is the commonest thing anybody does to a
// figure. Both routes into editing -- Quick Edit's mini viewer and the main
// viewer's outline -- call this one function, so they cannot disagree about
// what a panel is looking at.

const framing = run(`
    const square = { x: 1000, y: 1000, w: 400, h: 400 };
    return {
        wide: FigureSchema.aspectViewport(square, 2, { width: 4000, height: 3000 }),
        tall: FigureSchema.aspectViewport(square, 0.5, { width: 4000, height: 3000 }),
        unchanged: FigureSchema.aspectViewport(square, 1, { width: 4000, height: 3000 }),
        // Off the top edge: slid back in rather than left hanging over it.
        clamped: FigureSchema.aspectViewport(
            { x: 10, y: 10, w: 400, h: 400 }, 0.5, { width: 4000, height: 3000 }),
        // Taller than the whole image: centred and overhanging both ends,
        // because shrinking it would change the field rather than move it and
        // sliding it would put all the overhang at one end.
        oversize: FigureSchema.aspectViewport(
            { x: 0, y: 0, w: 4000, h: 4000 }, 0.5, { width: 4000, height: 3000 }),
        noImage: FigureSchema.aspectViewport(square, 2, null),
    };
`);
// The WIDTH is what survives: the user framed the field by what is across it,
// so the left and right edges stay put and the height follows the panel.
check("a wide panel keeps the width and loses height", framing.wide,
    { x: 1000, y: 1100, w: 400, h: 200 });
check("a tall panel keeps the width and gains height", framing.tall,
    { x: 1000, y: 800, w: 400, h: 800 });
check("a panel whose shape has not changed is untouched", framing.unchanged,
    { x: 1000, y: 1000, w: 400, h: 400 });
check("a frame that would hang off the top is slid back in", framing.clamped,
    { x: 10, y: 0, w: 400, h: 800 });
check("a frame bigger than the image stays centred on the field",
    framing.oversize, { x: 0, y: -2000, w: 4000, h: 8000 });
check("with no image to clamp against, the shape is still honoured",
    framing.noImage, { x: 1000, y: 1100, w: 400, h: 200 });

// -- deleting a placed panel returns it to the tray ------------------------

commits.length = 0;
const afterDelete = run(`
    canvas.selection = new Set(["pnl_a"]);
    canvas.removeSelection();
    return {
        placement: canvas.state.document.panels.pnl_a.placement,
        stillThere: Boolean(canvas.state.document.panels.pnl_a),
    };
`);
check("Delete unplaces a panel rather than destroying it",
    afterDelete, { placement: null, stillThere: true });
check("and does it as one move operation",
    commits[0], [{ op: "move_panels", moves: [{ panel_id: "pnl_a", placement: null }] }]);

// -- one gesture, one operation -------------------------------------------

commits.length = 0;
run(`
    canvas.selection = new Set(["pnl_a", "pnl_b", "pnl_c"]);
    canvas.nudge(1, 0);
`);
check("nudging three panels is one operation", commits.length, 1);
check("and it is a single move_panels carrying all three",
    commits[0][0].moves.map((m) => m.panel_id), ["pnl_a", "pnl_b", "pnl_c"]);

console.error(JSON.stringify({
    problems,
    commits: commits.length,
    ordering: { panels: orderingFixture, labels: labels.map(([, label]) => label) },
}, null, 2));
process.exit(problems.length ? 1 : 0);
