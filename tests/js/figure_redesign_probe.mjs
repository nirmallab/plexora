/**
 * The three-column editor's arithmetic (2026-11): layout presets, the one rule
 * for what a panel of another shape shows, and the canvas commands the Layers
 * list and the inspector drive.
 *
 * What would ship green and be wrong without this:
 *
 * * a layout whose cells are not the same shape, or whose gaps are not the gap
 *   the user typed, or which runs past the bottom margin;
 * * a "Left + 2" whose stack is not as tall as its large cell;
 * * `FigureSchema.shownViewport` and `compose.visible_viewport` disagreeing --
 *   the canvas would measure a scale bar across one field and the PDF across
 *   another (the pytest side re-runs every case here through Python);
 * * hiding or locking that the marquee, Select All or an arrange still reach;
 * * a layout that is not one undo step, or a re-layout that forgets its order.
 *
 * Run directly:  node tests/js/figure_redesign_probe.mjs
 * Prints JSON on stdout: {problems: [...], parity: [...]}.
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
// viewerScene.js first: FigureSchema/FigureScene delegate their viewport
// helpers to core's PlexoraViewerScene (loaded by base.html on every page).
const SCRIPTS = ["../../../client/src/js/services/viewerScene.js", "figureSchema.js", "figureRichText.js", "figureShapeGeometry.js",
                 "figureShapeDefs.js", "figureStrokeGeometry.js", "figureLineDefs.js",
                 "figureShapeDrawing.js", "figurePointEditor.js",
                 "figureCanvas.js", "figureLayouts.js"];

const problems = [];
const commits = [];

function elementStub() {
    return {
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {} },
        innerHTML: "",
        addEventListener() {},
        // `contains` and `focus`, because the canvas describes the objects it
        // has just drawn and puts the keyboard back on the one that had it --
        // see FigureCanvas.describeObjects. A stub with no `contains` is an
        // element the render cannot ask whether the keyboard is inside it.
        contains: () => false, focus() {},
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
        scalebar: { visible: false, target_um: null, target_px: null },
        labels: [],
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
    if (!(Math.abs(actual - expected) <= tolerance)) {
        problems.push(`${label}: expected ${expected}, got ${actual}`);
    }
}

function run(expression) {
    return runInContext(`(() => { const canvas = __buildCanvas(); ${expression} })()`, ctx);
}

// -- presets ------------------------------------------------------------------

const presets = runInContext(`FigureLayouts.PRESETS.map((p) => ({
    id: p.id, n: p.n,
    full: FigureLayouts.cells(p, { x: 10, y: 10, w: 190, maxH: 277 }, 3, 4, 4 / 3),
    tight: FigureLayouts.cells(p, { x: 10, y: 10, w: 190, maxH: 60 }, 3, 4, 4 / 3),
}))`, ctx);
for (const preset of presets) {
    check(`${preset.id} has one cell per panel`, preset.full.length, preset.n);
    for (const [name, cells, maxH] of [["full", preset.full, 277], ["tight", preset.tight, 60]]) {
        const bottom = Math.max(...cells.map((c) => c.y_mm + c.h_mm));
        if (bottom > 10 + maxH + 1e-6) problems.push(`${preset.id}/${name} runs past maxH: ${bottom}`);
        const left = Math.min(...cells.map((c) => c.x_mm));
        const right = Math.max(...cells.map((c) => c.x_mm + c.w_mm));
        if (left < 10 - 1e-6 || right > 200 + 1e-6) problems.push(`${preset.id}/${name} leaves the box`);
        close(`${preset.id}/${name} is centred`, (left + right) / 2, 105, 1e-6);
    }
    // Every small cell has the cells' aspect; the large cell of top/left too.
    for (const c of preset.full) close(`${preset.id} cell aspect`, c.w_mm / c.h_mm, 4 / 3, 1e-9);
}
const grid = presets.find((p) => p.id === "2x2").full;
close("2x2 gap across", grid[1].x_mm - (grid[0].x_mm + grid[0].w_mm), 3);
close("2x2 gap down", grid[2].y_mm - (grid[0].y_mm + grid[0].h_mm), 4);
check("2x2 is in reading order", grid.map((c) => [Math.round(c.x_mm), Math.round(c.y_mm)]),
      [[Math.round(grid[0].x_mm), 10], [Math.round(grid[1].x_mm), 10],
       [Math.round(grid[0].x_mm), Math.round(grid[2].y_mm)], [Math.round(grid[1].x_mm), Math.round(grid[2].y_mm)]]);
close("2x2 fills the width", grid[1].x_mm + grid[1].w_mm - grid[0].x_mm, 190);
for (const id of ["left2", "left3"]) {
    const cells = presets.find((p) => p.id === id).full;
    const stack = cells.slice(1);
    close(`${id}: the stack is as tall as the large cell`,
          stack[stack.length - 1].y_mm + stack[stack.length - 1].h_mm - stack[0].y_mm, cells[0].h_mm, 1e-9);
    close(`${id}: the stack's gaps are the gap down`, stack[1].y_mm - (stack[0].y_mm + stack[0].h_mm), 4, 1e-9);
    close(`${id}: fills the width`, stack[0].x_mm + stack[0].w_mm - cells[0].x_mm, 190, 1e-9);
}
const top3 = presets.find((p) => p.id === "top3").full;
close("top3: the top cell spans the row below",
      top3[3].x_mm + top3[3].w_mm - top3[1].x_mm, top3[0].w_mm, 1e-9);

check("fits", runInContext(`[
    FigureLayouts.fits(FigureLayouts.byId("2x2"), 4), FigureLayouts.fits(FigureLayouts.byId("2x2"), 3),
    FigureLayouts.fits(FigureLayouts.byId("2x2"), 2), FigureLayouts.fits(FigureLayouts.byId("4x2"), 7),
    FigureLayouts.fits(FigureLayouts.byId("top3"), 4), FigureLayouts.fits(FigureLayouts.byId("top3"), 3),
    FigureLayouts.fits(FigureLayouts.byId("2x2"), 1)]`, ctx),
    [true, true, false, true, true, false, false]);

const placed = runInContext(`({
    wide: FigureLayouts.place({ x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 30 }, 2, "fit"),
    tall: FigureLayouts.place({ x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 30 }, 0.5, "fit"),
    fill: FigureLayouts.place({ x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 30 }, 0.5, "fill"),
})`, ctx);
check("fit: a wide image keeps its width and is centred down", placed.wide, { x_mm: 0, y_mm: 5, w_mm: 40, h_mm: 20 });
check("fit: a tall image keeps its height and is centred across", placed.tall, { x_mm: 12.5, y_mm: 0, w_mm: 15, h_mm: 30 });
check("fill: the cell itself", placed.fill, { x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 30 });

// -- what a panel of another shape shows -----------------------------------------

const parityCases = [
    [{ x: 0, y: 0, w: 1000, h: 800 }, { w_mm: 50, h_mm: 40 }],
    [{ x: 0, y: 0, w: 1000, h: 800 }, { w_mm: 40, h_mm: 40 }],
    [{ x: 100, y: 50, w: 1000, h: 800 }, { w_mm: 80, h_mm: 20 }],
    [{ x: 0, y: 0, w: 1000, h: 1000 }, { w_mm: 30, h_mm: 60 }],
    [{ x: 0, y: 0, w: 1000, h: 800, orientation: { degrees: 90, frame_w: 800, frame_h: 1000 } },
     { w_mm: 40, h_mm: 40 }],
    [{ x: 0, y: 0, w: 1000, h: 800, orientation: { degrees: 0 } }, { w_mm: 40, h_mm: 10 }],
];
const parity = runInContext(`${JSON.stringify(parityCases)}.map(([viewport, place]) =>
    ({ viewport, place, shown: FigureSchema.shownViewport(viewport, place) }))`, ctx);
check("square panel from a 5:4 field shows the centre square",
      parity[1].shown, { x: 100, y: 0, w: 800, h: 800 });
check("matching shape shows the whole field", parity[0].shown, parityCases[0][0]);

const quality = run(`
    const panel = canvas.state.panel("pnl_a");
    panel.placement.h_mm = 32;   // the field's own 5:4
    const before = FigureCanvas.panelQuality(panel, canvas.state.document);
    panel.placement.h_mm = 15;   // flatter: the field is cropped top and bottom
    const cropped = FigureCanvas.panelQuality(panel, canvas.state.document);
    return { before, cropped };
`);
// 1000 px across 40 mm = 635 ppi, at 300 DPI: not low.
check("ppi", quality.before.ppi, Math.round(1000 / (40 / 25.4)));
check("not low", quality.before.low, false);
check("cropped flag", [quality.before.cropped, quality.cropped.cropped], [false, true]);
// 40 x 15 mm from a 1000 x 800 field: full width, 1000 / (40/15) = 375 high.
check("cropped source", [quality.cropped.source_w, quality.cropped.source_h], [1000, 375]);

// -- hidden and locked ------------------------------------------------------------

const hidden = run(`
    canvas.state.document.panels.pnl_b.hidden = true;
    canvas.state.document.panels.pnl_c.locked = true;
    canvas.selectAllOnPage();
    const all = Array.from(canvas.selection).sort();
    canvas.select(["pnl_a", "pnl_b", "pnl_c", "pnl_d"], false, true);
    const editable = canvas.arrangeItems().map((item) => item.id).sort();
    canvas.render();
    const drawn = (canvas.surfaceEl.innerHTML.match(/data-panel-id="([^"]+)"/g) || []).sort();
    return { all, editable, drawn };
`);
check("Select All skips hidden and locked", hidden.all, ["pnl_a", "pnl_d"]);
check("arrange skips hidden and locked", hidden.editable, ["pnl_a", "pnl_d"]);
check("a hidden panel is not drawn", hidden.drawn,
      ['data-panel-id="pnl_a"', 'data-panel-id="pnl_c"', 'data-panel-id="pnl_d"']);

const fields = run(`
    canvas.setLayerFields(["pnl_a"], { hidden: true, name: "Overview" });
    return { op: __lastCommit(), panel: canvas.state.panel("pnl_a") };
`);
check("hide and rename are one update_panel", fields.op,
      [{ op: "update_panel", panel_id: "pnl_a", changes: { hidden: true, name: "Overview" } }]);
check("the draft follows", [fields.panel.hidden, fields.panel.name], [true, "Overview"]);

// -- layouts ----------------------------------------------------------------------

const layout = run(`
    const before = __commitCount();
    const groupId = canvas.applyLayout(["pnl_a", "pnl_b", "pnl_c", "pnl_d"],
        { preset: "2x2", gap_x_mm: 5, gap_y_mm: 5, fit: "fit" }, { label: true });
    const commits = __commitCount() - before;
    const ops = __lastCommit().map((op) => op.op);
    const copy = (value) => JSON.parse(JSON.stringify(value));
    const group = copy(canvas.state.document.groups[groupId]);
    const boxes = copy(group.member_ids.map((id) => canvas.state.panel(id).placement));
    canvas.relayout(groupId, { gap_x_mm: 10 });
    const after = copy(group.member_ids.map((id) => canvas.state.panel(id).placement));
    const reversed = group.member_ids.slice().reverse();
    canvas.relayout(groupId, {}, { order: reversed });
    const reordered = canvas.state.document.groups[groupId].member_ids;
    return { commits, ops, members: group.member_ids, layout: group.layout,
             boxes, after, reversed, reordered,
             lastOps: __lastCommit().map((op) => op.op) };
`);
check("a layout is one commit", layout.commits, 1);
check("a new layout groups its panels", layout.ops, ["move_panels", "group_items"]);
check("members are in reading order", layout.members, ["pnl_a", "pnl_b", "pnl_c", "pnl_d"]);
check("the layout is recorded", layout.layout, { preset: "2x2", gap_x_mm: 5, gap_y_mm: 5, fit: "fit" });
close("the gap across is the gap", layout.boxes[1].x_mm - (layout.boxes[0].x_mm + layout.boxes[0].w_mm), 5);
close("a re-layout uses the new gap", layout.after[1].x_mm - (layout.after[0].x_mm + layout.after[0].w_mm), 10);
check("a reorder keeps the membership", layout.reordered, layout.reversed);
check("a re-layout updates the group it has", layout.lastOps, ["move_panels", "update_group"]);

const removed = run(`
    const groupId = canvas.applyLayout(["pnl_a", "pnl_b", "pnl_c", "pnl_d"],
        { preset: "2x2", gap_x_mm: 3, gap_y_mm: 3, fit: "fit" });
    canvas.select(["pnl_a"], false, true);
    canvas.removeSelection();
    const after = Object.values(canvas.state.document.groups);
    canvas.select(["pnl_b", "pnl_c"], false, true);
    canvas.removeSelection();
    return { ops: __lastCommit().map((op) => op.op), first: after,
             left: Object.keys(canvas.state.document.groups).length };
`);
// Three panels still fill a 2 x 2 (a grid may leave its last row short).
check("a 2x2 that loses one panel stays a layout of three",
      removed.first.map((g) => [g.member_ids.length, g.layout && g.layout.preset]), [[3, "2x2"]]);
check("a group that falls below two is dissolved", removed.left, 0);
check("leaving the page and the regroup are one commit", removed.ops,
      ["move_panels", "ungroup_items"]);

const restack = run(`
    canvas.restack(["pnl_a", "pnl_b", "pnl_c", "pnl_d"]);
    return ["pnl_a", "pnl_b", "pnl_c", "pnl_d"].map((id) => canvas.state.panel(id).placement.z);
`);
check("the list's top is the front", restack, [3, 2, 1, 0]);

const reflow = run(`
    canvas.fitContentToPage();
    const panels = Object.values(canvas.state.document.panels).filter((p) => p.placement);
    const box = FigureCanvas.unionBox(panels.map((p) => p.placement));
    return box;
`);
close("fit content: left margin", reflow.x_mm, 10, 1e-6);
close("fit content: fills the width or the height",
      Math.max(reflow.w_mm / 190, reflow.h_mm / 277), 1, 1e-6);

// -- one stack for panels and annotations (2026-11-15) -------------------------

const stack = run(`
    const doc = canvas.state.document;
    doc.annotations.ann_box = { annotation_id: "ann_box", type: "shape", page_id: "pg_1", z: 0,
        geometry: { x_mm: 25, y_mm: 25, w_mm: 20, h_mm: 10, rotation: 0 },
        style: { color: "#ff0000", fill: "", line_width_pt: 1 },
        shape: { preset: "rect", closed: true, nodes: [] } };
    const ids = () => FigureSchema.stackOnPage(doc, "pg_1").map((e) => e.id);
    const before = ids();
    const splitIndex = FigureSchema.stackIndex(doc, "annotation", 0);
    canvas.select(["pnl_a"], false, true);
    canvas.reorderZ("front");
    const ops = __lastCommit().map((op) => op.op);
    return { before, splitIndex, after: ids(), stack: doc.settings.stack, ops,
             sharedIndex: FigureSchema.stackIndex(doc, "annotation", doc.annotations.ann_box.z),
             nextPanel: FigureSchema.nextZ(doc, "pg_1", "panel"),
             nextAnnotation: FigureSchema.nextZ(doc, "pg_1", "annotation") };
`);
check("an old figure draws every annotation above every panel", stack.before,
      ["pnl_a", "pnl_b", "pnl_c", "pnl_d", "ann_box"]);
check("an old figure's annotation is lifted by 1000", stack.splitIndex, 1000);
check("bringing a panel to front puts it above the shape", stack.after,
      ["pnl_b", "pnl_c", "pnl_d", "ann_box", "pnl_a"]);
check("the figure moves to the shared stack", stack.stack, "shared");
check("conversion and reorder are one commit", stack.ops.includes("set_meta") && stack.ops.includes("move_panels"), true);
check("on the shared stack an annotation draws at its own z", stack.sharedIndex, 3);
check("a new object of either kind goes on top", [stack.nextPanel, stack.nextAnnotation], [5, 5]);

const listed = run(`
    const doc = canvas.state.document;
    doc.settings.stack = "shared";
    doc.annotations.ann_box = { annotation_id: "ann_box", type: "shape", page_id: "pg_1", z: 9,
        geometry: { x_mm: 25, y_mm: 25, w_mm: 20, h_mm: 10, rotation: 0 },
        style: { color: "#ff0000", fill: "", line_width_pt: 1 }, shape: { preset: "rect", closed: true, nodes: [] } };
    canvas.restack(["pnl_b", "ann_box", "pnl_a", "pnl_c", "pnl_d"]);
    return FigureSchema.stackOnPage(doc, "pg_1").map((e) => e.id).reverse();
`);
check("the Layers order is the page's order across both kinds", listed,
      ["pnl_b", "ann_box", "pnl_a", "pnl_c", "pnl_d"]);

// -- letters run along rows, not exact tops ------------------------------------

const rowsCase = [
    // A fit layout centred B (a wider picture) in its cell, so its top is lower.
    { panel_id: "pnl_r1", x_mm: 10, y_mm: 10, w_mm: 50, h_mm: 40, z: 0 },
    { panel_id: "pnl_r2", x_mm: 65, y_mm: 18, w_mm: 50, h_mm: 24, z: 1 },
    { panel_id: "pnl_r3", x_mm: 120, y_mm: 10, w_mm: 50, h_mm: 40, z: 2 },
    { panel_id: "pnl_r4", x_mm: 10, y_mm: 55, w_mm: 50, h_mm: 40, z: 3 },
];
const rows = runInContext(`(() => {
    const doc = { panels: {} };
    for (const entry of ${JSON.stringify(rowsCase)}.slice().reverse()) {
        doc.panels[entry.panel_id] = { panel_id: entry.panel_id,
            placement: { page_id: "pg_1", x_mm: entry.x_mm, y_mm: entry.y_mm, w_mm: entry.w_mm, h_mm: entry.h_mm, z: entry.z } };
    }
    return FigureSchema.panelsOnPage(doc, "pg_1").map((p) => p.panel_id);
})()`, ctx);
check("a centred picture keeps its place in the row", rows, ["pnl_r1", "pnl_r2", "pnl_r3", "pnl_r4"]);

// -- layouts: locked panels, typed letters ------------------------------------

const lockedLayout = run(`
    canvas.state.document.panels.pnl_b.locked = true;
    let notice = "";
    canvas.onNotice = (message) => { notice = message; };
    const before = __commitCount();
    const result = canvas.applyLayout(["pnl_a", "pnl_b", "pnl_c", "pnl_d"],
        { preset: "2x2", gap_x_mm: 3, gap_y_mm: 3, fit: "fit" });
    return { result, committed: __commitCount() - before, notice: Boolean(notice) };
`);
check("a layout holding a locked panel is refused, and says so", lockedLayout,
      { result: null, committed: 0, notice: true });

const typedLetter = run(`
    const doc = canvas.state.document;
    doc.panels.pnl_a.label = { ...doc.panels.pnl_a.label, auto: false, text: "Ctrl", visible: true };
    doc.panels.pnl_b.label = { ...doc.panels.pnl_b.label, visible: false };
    canvas.applyLayout(["pnl_a", "pnl_b", "pnl_c", "pnl_d"],
        { preset: "2x2", gap_x_mm: 3, gap_y_mm: 3, fit: "fit" }, { label: true });
    return [doc.panels.pnl_a.label.text, doc.panels.pnl_a.label.auto, doc.panels.pnl_b.label.visible];
`);
check("a layout keeps a typed letter and only shows hidden ones", typedLetter, ["Ctrl", false, true]);

const flipped = run(`
    const doc = canvas.state.document;
    doc.panels.pnl_a.placement.rotation = 30;
    canvas.select(["pnl_a"], false, true);
    canvas.flip(true);
    return doc.panels.pnl_a.placement.rotation;
`);
check("a mirrored panel tilts the other way", flipped, 330);

console.log(JSON.stringify({ problems, parity, rows: { panels: rowsCase, order: rows } }));
process.exit(problems.length ? 1 : 0);
