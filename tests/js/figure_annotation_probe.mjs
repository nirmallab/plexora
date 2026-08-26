/**
 * Drawing on the page: the geometry of shapes, lines and arrows.
 *
 * The whole annotation back end -- the schema, the seventeen operations, the
 * PDF renderer -- shipped before anything could create one, so this is the
 * first code that turns a drag into an annotation and none of it runs anywhere
 * else in the suite. Each mistake below ships green and produces a figure that
 * is wrong in a way the author only sees in the PDF:
 *
 * * a line whose w/h is normalised the way a rectangle's is points every arrow
 *   down and to the right, whichever way it was drawn;
 *
 * * an arrowhead sized or angled differently from `export._arrow_head` gives a
 *   canvas that disagrees with the deliverable, and the canvas is what the
 *   author looked at while deciding it was finished;
 *
 * * a click that places nothing because the pointer did not travel far enough
 *   is a tool people press twice and then stop using;
 *
 * * an automatic placement that stacks two panels at one coordinate hides one
 *   of them completely, and it is found only by dragging the other away.
 *
 * Run directly:
 *   node tests/js/figure_annotation_probe.mjs
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
const SCRIPTS = ["figureSchema.js", "figureRichText.js", "figureShapeGeometry.js",
                 "figureShapeDefs.js", "figureStrokeGeometry.js", "figureLineDefs.js",
                 "figureShapeDrawing.js", "figurePointEditor.js",
                 "figureConfirm.js",
                 "figureCanvas.js"];

const problems = [];
const commits = [];

function elementStub() {
    const element = {
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
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
    return element;
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
            // FigureConfirm.modalOpen asks the document whether a <dialog> is
            // up, and FigureCanvas.keyDown asks IT before every shortcut.
            querySelector: () => null,
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

function documentFixture() {
    return {
        schema_version: 1, revision: 1, figure_id: "fig_test", title: "Test",
        sources: { src_1: { source_id: "src_1", kind: "plexora_project", datasource: "demo" } },
        pages: [{
            page_id: "pg_1", name: "Page 1", preset: "a4", orientation: "portrait",
            size_mm: { w: 210, h: 297 },
            margins_mm: { top: 10, right: 10, bottom: 10, left: 10 },
            background: "#ffffff",
        }],
        panels: {}, annotations: {}, link_groups: {}, groups: {},
        settings: { dpi_default: 300, label_style: "A",
                    style: { gutter_mm: 3, font_size_pt: 8, label_size_pt: 10,
                             title_size_pt: 9, line_width_pt: 0.75,
                             font_family: "Helvetica", text_color: "#111111",
                             panel_background: "#000000" } },
    };
}

ctx.__fixture = documentFixture;
ctx.__record = (operations) => { commits.push(operations); };
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

// -- what a drag describes -------------------------------------------------

const boxes = run(`
    const drag = (tool, ox, oy, cx, cy, shift) => canvas.drawBox(
        Boolean(shift),
        { origin: { x: ox, y: oy }, current: { x: cx, y: cy } },
        tool);
    return {
        rect: drag("rect", 20, 20, 60, 50),
        rectBackwards: drag("rect", 60, 50, 20, 20),
        rectShift: drag("rect", 20, 20, 60, 50, true),
        line: drag("line", 20, 20, 60, 50),
        lineBackwards: drag("line", 60, 50, 20, 20),
        lineShift: drag("line", 20, 20, 60, 25, true),
    };
`);

check("a rectangle dragged down-right is its bounding box", boxes.rect,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30 });
// The case a naive implementation gets wrong: dragging up and left must give
// the same box, not a negative one -- a div cannot have a negative width.
check("a rectangle dragged up-left is the same box", boxes.rectBackwards,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30 });
check("Shift squares a rectangle", boxes.rectShift,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 40 });

// A line is a start point and an OFFSET. Normalising it would point every
// arrow down and to the right whichever way the user drew it, and the head
// would end up at the wrong end.
check("a line keeps the direction it was drawn in", boxes.line,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30 });
check("a line drawn backwards is negative, not flipped", boxes.lineBackwards,
    { x_mm: 60, y_mm: 50, w_mm: -40, h_mm: -30 });
check("Shift makes a line axis-aligned", boxes.lineShift,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 0 });

// -- a click still places something ----------------------------------------

// Through `pointerUp`, not straight into `finishDraw`. The difference is the
// whole point: `pointerUp` CLEARS `this.gesture` and releases the tool before
// it hands the gesture on, so a `finishDraw` reached with both still set is a
// state the app never reaches. Calling it directly hid two live bugs -- a throw
// on `this.gesture` being null, and every arrow pointing down-and-right because
// `this.tool` had already gone.
const __draw = `
    const draw = (tool, ox, oy, cx, cy) => {
        canvas.tool = tool;
        canvas.gesture = {
            kind: "draw", origin: { x: ox, y: oy }, current: { x: cx, y: cy },
            moved: ox !== cx || oy !== cy, handle: null, items: [],
        };
        canvas.pointerUp();
        return Object.values(canvas.state.document.annotations).pop();
    };
`;

commits.length = 0;
const clicked = run(`
    ${__draw}
    const added = draw("rect", 30, 40, 30, 40);
    return { type: added.type, geometry: added.geometry, page: added.page_id };
`);
check("a click with no drag still places a shape", clicked.type, "rect");
check("at the default size, where it was clicked",
    [clicked.geometry.x_mm, clicked.geometry.y_mm,
     clicked.geometry.w_mm, clicked.geometry.h_mm], [30, 40, 30, 18]);
check("on the page being looked at", clicked.page, "pg_1");
check("as exactly one add_annotation", commits.map((batch) => batch.map((op) => op.op)),
    [["add_annotation"]]);

// The tool is one-shot: a mode that persisted would turn the next click on a
// panel into another rectangle on top of it.
const toolAfter = run(`
    ${__draw}
    draw("arrow", 10, 10, 40, 10);
    return canvas.tool;
`);
check("a drawing tool disarms itself once it has placed something", toolAfter, null);

// The direction rule again, but through the real hand-off rather than through
// `drawBox` alone -- `finishDraw` releases the tool before it asks for the
// geometry, so an arrow drawn up-and-left is where reading `this.tool` back
// would silently give the bounding box instead of the offset.
const backwards = run(`
    ${__draw}
    return draw("arrow", 60, 50, 20, 20).geometry;
`);
check("an arrow drawn up-left survives the whole pointer-up path",
    [backwards.x_mm, backwards.y_mm, backwards.w_mm, backwards.h_mm],
    [60, 50, -40, -30]);
// -- the lines card arms a variant, not a type -----------------------------
//
// Five cells, one object. What the card picks is a handful of style keys laid
// over the ordinary drawing defaults -- so the tool that used to be called
// Arrow now inserts a `line` carrying a head, and "turn this arrow into a plain
// line" is a property edit rather than a delete and a redraw.

check("a bare line tool is a plain solid line",
    run("return FigureCanvas.lineTool('line');"), { line_style: "solid" });
// The old rail menu armed these two names and every stored annotation is typed
// with them, so both have to keep meaning what they meant.
check("a bare arrow tool still carries the head it always did",
    run("return FigureCanvas.lineTool('arrow');"),
    { line_style: "solid", end_head: "open" });
check("a card variant is its own overlay",
    run("return FigureCanvas.lineTool('line:double');"),
    { line_style: "solid", start_head: "open", end_head: "open" });
check("a variant nobody defined arms nothing",
    run("return FigureCanvas.lineTool('line:squiggle');"), null);
check("and neither does a shape tool",
    run("return FigureCanvas.lineTool('shape:rect');"), null);

commits.length = 0;
const dashed = run(`
    ${__draw}
    const added = draw("line:dashed", 10, 10, 50, 10);
    return { type: added.type, style: added.style };
`);
check("a variant commits as a line, never as a variant type", dashed.type, "line");
check("carrying the keys the cell stood for", dashed.style.line_style, "dashed");
check("and the drawing defaults underneath", dashed.style.head_size_pt, 0);
check("as exactly one add_annotation", commits.map((batch) => batch.map((op) => op.op)),
    [["add_annotation"]]);

// `arrow` is superseded the way `rect` and `ellipse` were by `shape`: readable
// forever, never created again. A build that kept making them would keep the
// two-renderer split alive in every new figure.
const fromArrowTool = run(`
    ${__draw}
    const added = draw("arrow", 10, 10, 50, 10);
    return { type: added.type, end: added.style.end_head };
`);
check("the arrow tool makes a line", fromArrowTool.type, "line");
check("with the head that made it an arrow", fromArrowTool.end, "open");

// -- Shift is 45 degrees, drawing and dragging alike -----------------------
//
// Projected onto the chosen axis rather than having the smaller component
// zeroed: zeroing turns a 40mm diagonal drag into a 40mm horizontal one, so the
// far end travels further than the pointer did.

const snapped = run(`
    const drag = (ox, oy, cx, cy) => canvas.drawBox(
        true, { origin: { x: ox, y: oy }, current: { x: cx, y: cy } }, "line:arrow");
    return { diagonal: drag(20, 20, 60, 54), flat: drag(20, 20, 60, 25) };
`);
close("a near-diagonal drag snaps to 45",
    snapped.diagonal.w_mm, snapped.diagonal.h_mm, 1e-9);
close("and is the projection, not the longer component",
    snapped.diagonal.w_mm, (40 + 34) / 2, 1e-9);
check("a shallow drag still snaps flat", snapped.flat,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 0 });

// Endpoints snap the LINE, not the pointer delta. Snapping the delta would make
// a shallow drag flat while leaving the line at whatever angle the other end
// happened to give it -- which is not what the user is aiming at.
const snappedEnds = run(`
    const start = { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30, rotation: 0 };
    return {
        p2: canvas.resizedBox(start, "p2", 0, 10, true),
        p1: canvas.resizedBox(start, "p1", -10, 0, true),
        p2Free: canvas.resizedBox(start, "p2", 0, 10, false),
    };
`);
close("Shift on the far end squares the line up (w)", snappedEnds.p2.w_mm, 40, 1e-9);
close("Shift on the far end squares the line up (h)", snappedEnds.p2.h_mm, 40, 1e-9);
// The near end moves and the FAR one stays put: (60, 50) either way.
close("Shift on the near end still lands on 45 (x)", snappedEnds.p1.x_mm, 20, 1e-9);
close("Shift on the near end still lands on 45 (y)", snappedEnds.p1.y_mm, 10, 1e-9);
close("and anchors the far end (x)",
    snappedEnds.p1.x_mm + snappedEnds.p1.w_mm, 60, 1e-9);
close("and anchors the far end (y)",
    snappedEnds.p1.y_mm + snappedEnds.p1.h_mm, 50, 1e-9);
check("and without Shift nothing is snapped at all",
    [snappedEnds.p2Free.w_mm, snappedEnds.p2Free.h_mm], [40, 40]);



// -- a line's two ends -----------------------------------------------------

const ends = run(`
    const start = { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30, rotation: 0 };
    return {
        head: canvas.resizedBox(start, "p1", 5, -5, false),
        tail: canvas.resizedBox(start, "p2", 5, -5, false),
        // The floor that applies to panels must NOT apply here: a horizontal
        // line has zero height, and clamping it to 5 mm would make one
        // impossible to draw.
        flat: canvas.resizedBox(start, "p2", 0, -30, false),
    };
`);
check("dragging the start moves it and keeps the far end still", ends.head,
    { x_mm: 25, y_mm: 15, w_mm: 35, h_mm: 35, rotation: 0 });
check("dragging the end moves only the offset", ends.tail,
    { x_mm: 20, y_mm: 20, w_mm: 45, h_mm: 25, rotation: 0 });
check("a line may be perfectly flat", ends.flat,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 0, rotation: 0 });
// -- what a stroke actually renders as -------------------------------------
//
// The only place `strokeMarkup` is exercised. Each of these ships green and is
// wrong in a way nobody looks for:
//
//   * a gradient id shared between annotations -- SVG defs are document-global
//     and a user-space gradient carries its own direction, so the second faded
//     line on a page fades along the FIRST one's axis;
//   * a hit line sized from the CSS literal rather than from the ink, so a fat
//     line is unclickable along the edges the user can see;
//   * a taper drawn as a stroke, which is a constant-width line that passes
//     every test looking only for ink.

const svg = run(`
    const draw = (style) => canvas.strokeMarkup({
        annotation_id: "ann_x", type: "line", z: 1,
        geometry: { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 0, rotation: 0 },
        style: Object.assign({
            color: "#123456", fill: "", line_width_pt: 2, opacity: 1,
            line_style: "solid", start_head: "none", end_head: "none",
            head_size_pt: 0, edge: "standard",
        }, style),
    });
    return {
        plain: draw({}),
        dashed: draw({ line_style: "dashed" }),
        dotted: draw({ line_style: "dotted" }),
        taper: draw({ edge: "taper_end" }),
        fade: draw({ edge: "fade_both" }),
        double: draw({ start_head: "open", end_head: "filled" }),
        fat: draw({ line_width_pt: 12 }),
    };
`);

check("a plain line is one stroked line plus its hit line",
    (svg.plain.match(/<line /g) || []).length, 2);
check("and carries no dash array at all", svg.plain.includes("stroke-dasharray"), false);
check("a dashed line does", svg.dashed.includes("stroke-dasharray"), true);
// A zero-length dash under a round cap is what a dot IS, in SVG and in a PDF
// alike -- there is no other way to get round dots out of a dash array.
check("a dotted line's pattern starts at zero",
    /stroke-dasharray="0 /.test(svg.dotted), true);
check("a taper is filled ink, not a pen",
    [svg.taper.includes("<polygon"), svg.taper.includes('fill="#123456"')],
    [true, true]);
// Per annotation, never per canvas.
check("a fade defines its own gradient", svg.fade.includes('id="fb-fade-ann_x"'), true);
check("and paints the shaft with it", svg.fade.includes('stroke="url(#fb-fade-ann_x)"'), true);
check("fading both ends takes three stops",
    (svg.fade.match(/<stop /g) || []).length, 3);
check("a plain line defines no gradient", svg.plain.includes("<defs>"), false);
// Two open barbs, one filled triangle, the shaft and the hit line.
check("both ends are drawn", (svg.double.match(/<line /g) || []).length, 4);
check("and a solid head is a polygon",
    (svg.double.match(/<polygon /g) || []).length, 1);
// Sized from the ink and set INLINE, which is what beats the 12px literal in
// figure_builder.css -- a fat line whose hit area was that literal would be
// unclickable along the edges the user can see. At 96 dpi a 12pt pen is 16
// screen pixels, so the target is 24.
const hit = /class="fb-stroke-hit"[^>]*stroke-width="([\d.]+)"/.exec(svg.fat);
check("the hit line is sized from the ink", Boolean(hit), true);
if (hit) {
    check("and is wider than the line it has to catch", Number(hit[1]), 24);
}
// A hairline still gets a target somebody can hit, which is what the floor is
// for: 1px of ink is not 1px of clickable.
const thinHit = /class="fb-stroke-hit"[^>]*stroke-width="([\d.]+)"/.exec(svg.plain);
check("and a thin one gets the floor instead", Number(thinHit[1]), 12);



// -- arrowheads agree with the exporter ------------------------------------
//
// The numbers below are compared against `server/strokegeom.py` itself, by the
// pytest that runs this probe. Emitted rather than asserted here because only
// one side of the comparison lives in JavaScript.
//
// What is actually at stake is the CONVERSION. The head table is shared and
// pinned in `figure_stroke_probe.mjs`; what only exists here is the canvas
// turning a size in points into screen pixels at the current zoom. A canvas
// that sized a head in millimetres would look right at one zoom level and wrong
// at every other, and nothing on screen would say which was the correct one.

const arrow = run(`
    canvas.scale = 96 / 25.4;
    const perPt = canvas.scale / FigureCanvas.PT_PER_MM;
    const barbs = (x1, y1, x2, y2, size) => FigureStrokeGeometry
        .placeHead([x2, y2], [x1, y1], FigureStrokeGeometry.headGeometry("open", size, 0))
        .lines.map((line) => line[1]);
    return {
        // The size rule: max(3, lw * 4) POINTS, converted to screen pixels.
        sizeAtDefaultWidth: FigureStrokeGeometry.headSize(0, 0.75) * perPt,
        sizeAtFatWidth: FigureStrokeGeometry.headSize(0, 2) * perPt,
        points: barbs(10, 20, 60, 20, 3),
        diagonal: barbs(0, 0, 30, 40, 5),
    };
`);
// 0.75 * 4 = 3, which is also the floor, so both ends of the rule are covered.
close("a thin arrow uses the 3-point floor",
    arrow.sizeAtDefaultWidth, 3 * (96 / 25.4) / 2.8346, 1e-9);
close("a fat arrow scales with the line width",
    arrow.sizeAtFatWidth, 8 * (96 / 25.4) / 2.8346, 1e-9);
check("an arrow has exactly two barbs", arrow.points.length, 2);

// -- automatic placement never stacks --------------------------------------

const placed = run(`
    const page = canvas.page;
    const sizes = [
        { w_mm: 60, h_mm: 45 }, { w_mm: 60, h_mm: 45 },
        { w_mm: 60, h_mm: 45 }, { w_mm: 60, h_mm: 45 },
    ];
    return FigureCanvas.freePlacements(sizes, page, [], 3, null);
`);
check("four panels flow left to right and wrap", placed, [
    { x_mm: 10, y_mm: 10, w_mm: 60, h_mm: 45 },
    { x_mm: 73, y_mm: 10, w_mm: 60, h_mm: 45 },
    { x_mm: 136, y_mm: 10, w_mm: 60, h_mm: 45 },
    { x_mm: 10, y_mm: 58, w_mm: 60, h_mm: 45 },
]);

const dodged = run(`
    const page = canvas.page;
    // Something is already sitting where the first one would go.
    const occupied = [{ x_mm: 10, y_mm: 10, w_mm: 60, h_mm: 45 }];
    return FigureCanvas.freePlacements([{ w_mm: 60, h_mm: 45 }], page, occupied, 3, null);
`);
check("a new panel steps past what is already there", dodged,
    [{ x_mm: 73, y_mm: 10, w_mm: 60, h_mm: 45 }]);

const crowded = run(`
    const page = canvas.page;
    // A page with no room at all: every candidate position is taken.
    const occupied = [{ x_mm: 0, y_mm: 0, w_mm: 210, h_mm: 297 }];
    const out = FigureCanvas.freePlacements(
        [{ w_mm: 60, h_mm: 45 }, { w_mm: 60, h_mm: 45 }], page, occupied, 3, null);
    return { first: out[0], second: out[1], same: JSON.stringify(out[0]) === JSON.stringify(out[1]) };
`);
// A full page cascades rather than stacking: two panels at one coordinate look
// like one panel, and the second is found only by dragging the first away.
check("a full page cascades rather than stacking", crowded.same, false);


// -- shapes ----------------------------------------------------------------

// The picker arms `shape:<id>`, and the id decides whether that is a preset to
// drag out or a drawing mode with no geometry until something has been drawn.
// The two helpers partition one namespace, so a preset that is also a mode --
// or neither -- would be a tool the canvas silently ignores.
const shapeNaming = run(`
    return {
        preset: (FigureCanvas.shapePreset("shape:pentagon") || {}).id || null,
        presetMode: FigureCanvas.shapeMode("shape:pentagon"),
        mode: FigureCanvas.shapeMode("shape:freehand"),
        modePreset: (FigureCanvas.shapePreset("shape:freehand") || {}).id || null,
        text: FigureCanvas.shapePreset("text"),
        nothing: FigureCanvas.shapeMode(null),
    };
`);
check("a known id names a preset", shapeNaming.preset, "pentagon");
check("and is therefore not a drawing mode", shapeNaming.presetMode, null);
check("an unknown id names a drawing mode", shapeNaming.mode, "freehand");
check("and is therefore not a preset", shapeNaming.modePreset, null);
check("no other tool is a shape", [shapeNaming.text, shapeNaming.nothing], [null, null]);

const shapeBoxes = run(`
    const drag = (tool, ox, oy, cx, cy, shift) => canvas.drawBox(
        Boolean(shift),
        { origin: { x: ox, y: oy }, current: { x: cx, y: cy } },
        tool);
    return {
        plain: drag("shape:pentagon", 20, 20, 60, 50),
        squared: drag("shape:pentagon", 20, 20, 60, 50, true),
        bar: drag("shape:bar", 20, 20, 60, 50, true),
    };
`);
check("a shape dragged out is its bounding box", shapeBoxes.plain,
    { x_mm: 20, y_mm: 20, w_mm: 40, h_mm: 30 });
// Shift constrains to the shape's OWN proportions. A pentagon is very nearly
// square, so it looks like the old squaring rule...
close("Shift on a near-square preset is near-square",
    shapeBoxes.squared.w_mm / shapeBoxes.squared.h_mm, 1.0515, 1e-3);
// ...and a bar is where forcing 1:1 would have given back something the picker
// never offered.
close("Shift on a bar keeps 8:1", shapeBoxes.bar.w_mm / shapeBoxes.bar.h_mm, 8, 1e-9);

commits.length = 0;
const shapePlaced = run(`
    ${__draw}
    const added = draw("shape:pentagon", 30, 40, 30, 40);
    return { type: added.type, preset: added.shape.preset, closed: added.shape.closed,
             nodes: added.shape.nodes.length, geometry: added.geometry,
             opacity: added.style.opacity };
`);
check("clicking a preset places a shape", [shapePlaced.type, shapePlaced.preset], ["shape", "pentagon"]);
check("with the preset's own nodes", [shapePlaced.nodes, shapePlaced.closed], [5, true]);
// 30mm wide and as tall as a pentagon is, rather than the 18mm every other
// bare click gets: a click on the bar has to place a bar.
close("at the preset's own proportions", shapePlaced.geometry.w_mm / shapePlaced.geometry.h_mm,
    1.0515, 1e-3);
check("fully opaque to start with", shapePlaced.opacity, 1);
check("as exactly one add_annotation", commits.map((batch) => batch.map((op) => op.op)),
    [["add_annotation"]]);

// The definition table is shared by every shape of that kind AND by the
// picker's icon. Editing points mutates a shape's node list in place, so a
// shapePlaced shape that pointed back into the table would rewrite the preset.
const shapeIsolated = run(`
    ${__draw}
    const first = draw("shape:pentagon", 10, 10, 40, 40);
    first.shape.nodes[0].x = 0.123;
    const second = draw("shape:pentagon", 60, 10, 90, 40);
    return { moved: first.shape.nodes[0].x, untouched: second.shape.nodes[0].x,
             table: FigureShapeDefs.byId("pentagon").nodes[0].x };
`);
check("editing one shape's nodes leaves the next one alone",
    [shapeIsolated.untouched === shapeIsolated.moved, shapeIsolated.table === shapeIsolated.moved],
    [false, false]);

const shapeMarkupCases = run(`
    canvas.scale = 3;
    const shape = FigureShapeDefs.byId("star5");
    return {
        filled: canvas.shapeMarkup({
            annotation_id: "ann_1", type: "shape", z: 1,
            geometry: { x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 20, rotation: 0 },
            shape: { preset: "star5", closed: true, nodes: shape.nodes },
            style: { color: "#112233", fill: "#ff0000", line_width_pt: 2, opacity: 1 },
        }),
        hollow: canvas.shapeMarkup({
            annotation_id: "ann_2", type: "shape", z: 1,
            geometry: { x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 20, rotation: 0 },
            shape: { preset: "star5", closed: true, nodes: shape.nodes },
            style: { color: "#112233", fill: "", line_width_pt: 2, opacity: 1 },
        }),
        open: canvas.shapeMarkup({
            annotation_id: "ann_3", type: "shape", z: 1,
            geometry: { x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 20, rotation: 0 },
            shape: { preset: "custom", closed: false,
                     nodes: [{ x: 0, y: 0, type: "corner", in: null, out: null },
                             { x: 1, y: 1, type: "corner", in: null, out: null }] },
            style: { color: "#112233", fill: "#ff0000", line_width_pt: 2, opacity: 1 },
        }),
    };
`);
// The box is the viewBox, so a resize scales the path instead of re-emitting
// it -- and the stroke has to opt out of that scaling or a stretched shape has
// a fatter outline down one side than the other.
check("a shape is drawn in its own 0-1 box",
    shapeMarkupCases.filled.includes('viewBox="0 0 1 1"')
    && shapeMarkupCases.filled.includes('preserveAspectRatio="none"'), true);
check("with a stroke that does not stretch with it",
    (shapeMarkupCases.filled.match(/non-scaling-stroke/g) || []).length, 2);
// An outline drawn around a panel must not be a place where clicking the panel
// stops working, so an unfilled shape answers on its ink alone.
check("a filled shape takes clicks anywhere inside it",
    shapeMarkupCases.filled.includes('pointer-events="all"'), true);
check("an unfilled one only on its outline",
    shapeMarkupCases.hollow.includes('pointer-events="stroke"'), true);
// Where the missing edge runs is a guess, and each renderer guesses
// differently -- so an open path is never filled, whatever colour it holds.
check("an open path is not filled even when it has a fill",
    shapeMarkupCases.open.includes('fill="none"'), true);


// -- drawing a custom shape ------------------------------------------------

// Polygon, curve, freehand and open path are MODES, not gestures: there is no
// gesture object for the whole of a polygon, so nothing `pointerUp` or
// `commitGesture` does can be relied on to finish one. Each of these would
// ship green and be wrong in a way only a user finds:
//
//   * a polygon that never closes -- clicking the first point is the way most
//     people expect to finish, and the alternative is an object nobody can
//     stop drawing;
//   * a freehand stroke stored at pointer resolution -- three hundred nodes to
//     drag past to reach the one you meant;
//   * a cancelled draw that still commits -- an object appears from a gesture
//     the user explicitly abandoned.
const __drawing = `
    canvas.scale = 1;
    let clock = 0;
    const press = (x, y) => {
        clock += 1000;
        return { button: 0, clientX: x, clientY: y, timeStamp: clock,
                 shiftKey: false, preventDefault() {},
                 target: { closest: () => null } };
    };
    const key = (name) => ({ key: name, preventDefault() {} });
    const arm = (mode) => canvas.setTool("shape:" + mode);
    const last = () => Object.values(canvas.state.document.annotations).pop() || null;
`;

commits.length = 0;
const polygon = run(`
    ${__drawing}
    arm("polygon");
    for (const [x, y] of [[10, 10], [50, 10], [50, 40], [10, 40]]) {
        canvas.pointerDown(press(x, y));
    }
    canvas.pointerDown(press(11, 11));   // back on the first point: close
    const made = last();
    return { type: made.type, preset: made.shape.preset, closed: made.shape.closed,
             nodes: made.shape.nodes.length,
             types: made.shape.nodes.map((node) => node.type),
             geometry: made.geometry, tool: canvas.tool,
             drawing: canvas.shapeDrawing.active };
`);
check("a polygon closes when the first point is clicked again",
    [polygon.type, polygon.preset, polygon.closed], ["shape", "custom", true]);
check("with one corner node per click", [polygon.nodes, polygon.types[0]], [4, "corner"]);
// The box is the ink's tight bounds, which is what the renderers rotate about.
check("in a box that is exactly the shape",
    [polygon.geometry.x_mm, polygon.geometry.y_mm,
     polygon.geometry.w_mm, polygon.geometry.h_mm], [10, 10, 40, 30]);
check("and the mode ends with the object", [polygon.tool, polygon.drawing], [null, false]);
check("as exactly one add_annotation",
    commits.map((batch) => batch.map((op) => op.op)), [["add_annotation"]]);

const finishing = run(`
    ${__drawing}
    arm("path");
    for (const [x, y] of [[10, 10], [30, 40], [50, 10]]) canvas.pointerDown(press(x, y));
    canvas.keyDown(key("Enter"));
    const open = last();

    arm("polygon");
    for (const [x, y] of [[60, 60], [90, 60], [90, 90]]) canvas.pointerDown(press(x, y));
    canvas.keyDown(key("Enter"));
    const closed = last();
    return { openClosed: open.shape.closed, openNodes: open.shape.nodes.length,
             closedClosed: closed.shape.closed, closedNodes: closed.shape.nodes.length };
`);
// An open path is the one mode that does NOT close on Enter -- it is what the
// tool is for, and closing it would make it the polygon tool with extra steps.
check("Enter finishes an open path, open",
    [finishing.openClosed, finishing.openNodes], [false, 3]);
check("and finishes a polygon, closed",
    [finishing.closedClosed, finishing.closedNodes], [true, 3]);

const curved = run(`
    ${__drawing}
    arm("curve");
    for (const [x, y] of [[10, 10], [50, 10], [50, 40], [10, 40]]) {
        canvas.pointerDown(press(x, y));
    }
    canvas.keyDown(key("Enter"));
    const made = last();
    return { types: made.shape.nodes.map((node) => node.type),
             handled: made.shape.nodes.every((node) => node.in && node.out) };
`);
check("a curved shape gives every node two levers",
    [curved.types.every((type) => type === "smooth"), curved.handled], [true, true]);

const freehand = run(`
    ${__drawing}
    arm("freehand");
    canvas.pointerDown(press(20, 20));
    // A straight run with a jitter far below the sampling step, then a corner.
    let samples = 1;
    for (let index = 1; index <= 120; index += 1) {
        canvas.pointerMove(press(20 + index, 20 + (index % 2 ? 0.2 : -0.2)));
        samples += 1;
    }
    for (let index = 1; index <= 60; index += 1) canvas.pointerMove(press(140, 20 + index));
    const raw = canvas.shapeDrawing.state.points.length;
    canvas.pointerUp(press(140, 80));
    const made = last();
    return { samples, raw, nodes: made.shape.nodes.length, closed: made.shape.closed };
`);
// Two things at once: the stroke is not stored at pointer resolution, and it is
// not flattened into a straight line either -- the corner survives.
check("a freehand stroke is simplified to something editable",
    freehand.nodes >= 3 && freehand.nodes <= 12, true);
check("from many more samples than that", freehand.raw > freehand.nodes * 3, true);
check("and an open stroke stays open", freehand.closed, false);

commits.length = 0;
const abandoned = run(`
    ${__drawing}
    arm("polygon");
    for (const [x, y] of [[10, 10], [50, 10], [50, 40]]) canvas.pointerDown(press(x, y));
    canvas.keyDown(key("Escape"));
    const after = { count: Object.keys(canvas.state.document.annotations).length,
                    tool: canvas.tool, drawing: canvas.shapeDrawing.active };

    // And a gesture that never became a shape: two clicks cannot enclose
    // anything, so Enter cancels it rather than committing a degenerate object.
    arm("polygon");
    canvas.pointerDown(press(10, 10));
    canvas.pointerDown(press(50, 10));
    canvas.keyDown(key("Enter"));
    after.stillEmpty = Object.keys(canvas.state.document.annotations).length;
    return after;
`);
check("Escape mid-draw commits nothing and disarms",
    [abandoned.count, abandoned.tool, abandoned.drawing], [0, null, false]);
check("and a two-click polygon is silently nothing at all", abandoned.stillEmpty, 0);
check("neither of which wrote to the document", commits.length, 0);

// -- Edit Points -----------------------------------------------------------

// The rotation case is the one that matters. Nodes are stored normalised
// against the box, so a node drag has to move the box and renormalise -- and on
// a ROTATED shape the box's world offset is the local centre delta TURNED by
// the angle. Getting that wrong makes the whole shape jump the instant a node
// is touched, and only on rotated ones, which is why this uses 30 degrees.
const __editing = `
    canvas.scale = 1;
    let clock = 0;
    const press = (x, y) => {
        clock += 1000;
        return { button: 0, clientX: x, clientY: y, timeStamp: clock,
                 shiftKey: false, preventDefault() {},
                 target: { closest: () => null } };
    };
    const worldOf = (made, index) => {
        const g = made.geometry;
        const node = made.shape.nodes[index];
        const turned = FigureShapeGeometry.turn(
            node.x * g.w_mm - g.w_mm / 2, node.y * g.h_mm - g.h_mm / 2, g.rotation || 0);
        return { x: g.x_mm + g.w_mm / 2 + turned.x, y: g.y_mm + g.h_mm / 2 + turned.y };
    };
    const place = (rotation) => {
        canvas.tool = "shape:rect";
        canvas.gesture = { kind: "draw", origin: { x: 100, y: 50 },
                           current: { x: 140, y: 70 }, moved: true, handle: null, items: [] };
        canvas.pointerUp();
        const made = Object.values(canvas.state.document.annotations).pop();
        made.geometry.rotation = rotation;
        return made;
    };
`;

commits.length = 0;
const converted = run(`
    ${__editing}
    const made = place(0);
    canvas.pointEditor.enter(made.annotation_id);
    return { preset: made.shape.preset, active: canvas.pointEditor.active,
             nodes: canvas.pointEditor.local.length };
`);
// One undoable operation, and no dialog: there is no state between "a pentagon"
// and "the nodes of a pentagon" for anyone to be in.
check("entering converts a preset to a custom path", converted.preset, "custom");
check("and opens the editor on its nodes",
    [converted.active, converted.nodes], [true, 4]);
check("as one add and one update",
    commits.map((batch) => batch.map((op) => op.op)),
    [["add_annotation"], ["update_annotation"]]);

for (const angle of [0, 30, -117]) {
    commits.length = 0;
    const dragged = run(`
        ${__editing}
        const made = place(${angle});
        const id = made.annotation_id;
        canvas.pointEditor.enter(id);
        const before = [0, 1, 2, 3].map((index) => worldOf(
            canvas.state.document.annotations[id], index));

        canvas.pointEditor.selected = new Set([1]);
        canvas.pointEditor.beginDrag(press(0, 0), 1, null);
        canvas.pointerMove(press(17, -9));
        canvas.pointerUp(press(17, -9));

        const after = [0, 1, 2, 3].map((index) => worldOf(
            canvas.state.document.annotations[id], index));
        return { before, after,
                 preset: canvas.state.document.annotations[id].shape.preset };
    `);
    // The dragged node follows the pointer exactly, in PAGE coordinates -- not
    // in the box's tilted frame -- and nothing else moves at all.
    close(`at ${angle} degrees the dragged node follows the pointer (x)`,
        dragged.after[1].x - dragged.before[1].x, 17, 1e-9);
    close(`at ${angle} degrees the dragged node follows the pointer (y)`,
        dragged.after[1].y - dragged.before[1].y, -9, 1e-9);
    for (const index of [0, 2, 3]) {
        close(`at ${angle} degrees node ${index} does not move (x)`,
            dragged.after[index].x, dragged.before[index].x, 1e-9);
        close(`at ${angle} degrees node ${index} does not move (y)`,
            dragged.after[index].y, dragged.before[index].y, 1e-9);
    }
    // Three writes in all: the add, the preset-to-custom conversion on entry,
    // and ONE for the whole drag -- not one per pointer move.
    check(`at ${angle} degrees a node drag ends in exactly one commit`,
        [commits.length, commits[commits.length - 1].map((op) => op.op)],
        [3, ["update_annotation"]]);
    check(`at ${angle} degrees it writes geometry and shape together`,
        Object.keys(commits[commits.length - 1][0].changes).sort(), ["geometry", "shape"]);
}

const tools = run(`
    ${__editing}
    const made = place(0);
    const id = made.annotation_id;
    const editor = canvas.pointEditor;
    editor.enter(id);
    const out = {};

    // Four nodes closed: deleting one leaves three, which is still a shape.
    editor.selected = new Set([0]);
    out.canDeleteOne = editor.canDelete;
    editor.deleteSelected();
    out.afterOne = editor.local.length;
    // Three left: deleting another would leave two, which encloses nothing.
    editor.selected = new Set([0]);
    out.canDeleteAgain = editor.canDelete;
    editor.deleteSelected();
    out.afterRefusal = editor.local.length;

    // Opening the path lowers the floor to two, so now it can go.
    editor.toggleClosed();
    out.opened = editor.closed;
    editor.selected = new Set([0]);
    out.canDeleteOpen = editor.canDelete;
    editor.deleteSelected();
    out.afterOpen = editor.local.length;

    editor.selected = new Set([0]);
    editor.setType("smooth");
    out.type = editor.local[0].type;
    out.grewLevers = Boolean(editor.local[0].out);

    editor.insertOn(0, { x: 0, y: 0 });
    out.afterInsert = editor.local.length;
    out.stored = canvas.state.document.annotations[id].shape.nodes.length;

    editor.exit();
    out.active = editor.active;
    return out;
`);
check("a closed shape may lose a node while three remain",
    [tools.canDeleteOne, tools.afterOne], [true, 3]);
// Silently, with no dialog: a geometric constraint the user can see for
// themselves is not news, and a modal on every fourth Delete is.
check("but not the one that would leave two",
    [tools.canDeleteAgain, tools.afterRefusal], [false, 3]);
check("opening the path lowers the floor to two",
    [tools.opened, tools.canDeleteOpen, tools.afterOpen], [false, true, 2]);
check("converting a bare corner to smooth grows it levers",
    [tools.type, tools.grewLevers], ["smooth", true]);
check("clicking a segment inserts a node", [tools.afterInsert, tools.stored], [3, 3]);
check("and Done leaves the mode", tools.active, false);

// Undo rewrites the shape with nothing to tell the editor, so the working copy
// has to come back off the document rather than be trusted. Left stale, the
// markers would sit where the path no longer is -- and the next drag would
// commit the undone geometry straight back.
const resynced = run(`
    ${__editing}
    const made = place(0);
    const editor = canvas.pointEditor;
    editor.enter(made.annotation_id);
    const before = editor.local.map((node) => [node.x, node.y]);

    // What an undo looks like from here: the document changes underneath.
    made.shape = { preset: "custom", closed: true,
                   nodes: [{ x: 0, y: 0, type: "corner", in: null, out: null },
                           { x: 1, y: 0, type: "corner", in: null, out: null },
                           { x: 0.5, y: 1, type: "corner", in: null, out: null }] };
    canvas.annotationMarkup(made);
    return { before, after: editor.local.map((node) => [node.x, node.y]) };
`);
check("an undo underneath the editor is picked up, not painted over",
    [resynced.before.length, resynced.after.length], [4, 3]);
check("and the working copy holds the document's nodes",
    resynced.after, [[0, 0], [40, 0], [20, 20]]);

// Shift constrains in SCREEN space, not in the box's own tilted frame: on a
// shape rotated 30 degrees the axes it would otherwise snap to are nowhere on
// the screen, so "straight" would come out crooked.
const constrained = run(`
    ${__editing}
    const made = place(30);
    const id = made.annotation_id;
    canvas.pointEditor.enter(id);
    const before = worldOf(canvas.state.document.annotations[id], 1);

    canvas.pointEditor.selected = new Set([1]);
    canvas.pointEditor.beginDrag(press(0, 0), 1, null);
    const shifted = press(30, 4);
    shifted.shiftKey = true;
    canvas.pointerMove(shifted);
    canvas.pointerUp(shifted);
    const after = worldOf(canvas.state.document.annotations[id], 1);
    return { dx: after.x - before.x, dy: after.y - before.y };
`);
close("Shift holds a node drag flat on the PAGE, whatever the rotation",
    constrained.dy, 0, 1e-9);
close("and moves it as far along that axis as the pointer went",
    constrained.dx, 30, 1e-9);

console.error(JSON.stringify({ problems, commits: commits.length, arrow }, null, 2));
process.exit(problems.length ? 1 : 0);
