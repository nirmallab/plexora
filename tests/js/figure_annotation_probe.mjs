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
const SCRIPTS = ["figureSchema.js", "figureRichText.js", "figureCanvas.js"];

const problems = [];
const commits = [];

function elementStub() {
    const element = {
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        innerHTML: "",
        addEventListener() {},
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

// -- arrowheads agree with the exporter ------------------------------------
//
// The numbers below are compared against `export._arrow_head` itself, by the
// pytest that runs this probe. Emitted rather than asserted here because only
// one side of the comparison lives in JavaScript.

const arrow = run(`
    canvas.scale = 96 / 25.4;
    return {
        // The size rule: max(3, lw * 4) POINTS, converted to screen pixels.
        sizeAtDefaultWidth: canvas.arrowHeadPx(0.75),
        sizeAtFatWidth: canvas.arrowHeadPx(2),
        points: FigureCanvas.arrowHeadPoints(10, 20, 60, 20, 3),
        diagonal: FigureCanvas.arrowHeadPoints(0, 0, 30, 40, 5),
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

console.error(JSON.stringify({ problems, commits: commits.length, arrow }, null, 2));
process.exit(problems.length ? 1 : 0);
