/**
 * The sheet, from the keyboard.
 *
 * Before this the page surface was a div of anonymous divs: no role, no name,
 * no tab stop anywhere on it. A keyboard user could not SELECT an object, and
 * every control this workspace has -- the floating bar, the contextual
 * sidebars, the right-click menu, the nudge, the z-order chords -- acts on a
 * selection. So the whole page was unreachable rather than merely awkward, and
 * that is a WCAG 2.1.1 failure of the plainest kind.
 *
 * What is pinned here is the smallest contract that fixes it, and the reason
 * each half of it matters:
 *
 *   * one tab stop, not one per object. A figure with thirty panels must not be
 *     thirty presses of Tab between the canvas and the status bar; a listbox is
 *     walked with the arrows and entered once;
 *
 *   * the roving cursor is not the selection. Walking a row to find the panel
 *     you want is not the same as taking it, and a cursor that selected as it
 *     moved would make Shift the only way to look at anything;
 *
 *   * the arrows keep their INCUMBENT meaning. With something selected they
 *     nudge, which is a half-millimetre move users have relied on since long
 *     before any of this. They only walk the page when nothing is selected --
 *     which is exactly when nudging has nothing to nudge. This is the case that
 *     would be a real regression if it broke, so it is the case with two
 *     assertions on it;
 *
 *   * every object says what it IS. "Panel B: CD8 in tumour", not "div" -- a
 *     figure is a page of pictures and the only thing telling two of them apart
 *     is what the user put in them.
 *
 * Run directly:
 *   node tests/js/figure_canvas_a11y_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureSchema.js", "figureRichText.js", "figureShapeGeometry.js",
                 "figureShapeDefs.js", "figureStrokeGeometry.js", "figureLineDefs.js",
                 "figureShapeDrawing.js", "figurePointEditor.js",
                 "figureCanvas.js"];

const problems = [];

function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

/**
 * A surface that keeps the attributes set on it.
 *
 * `innerHTML` is a string here, as it is in every probe in this tree -- what
 * the canvas emits is markup, and a real DOM would be a second implementation
 * of one. What this stub adds is the CHILDREN: `describeObjects` runs over the
 * elements it has just drawn, so the surface has to hand some back. They are
 * built from the ids in the markup, which is the one thing the string is
 * honest about.
 */
function surfaceStub() {
    const kids = [];
    const surface = {
        style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
        addEventListener() {}, focus() {}, tabIndex: -1,
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        contains: (node) => kids.includes(node),
        get innerHTML() { return this._html || ""; },
        set innerHTML(html) {
            this._html = html;
            kids.length = 0;
            const pattern = /data-(panel|annotation)-id="([^"]+)"/g;
            let match = pattern.exec(html);
            while (match) {
                kids.push(objectStub(match[1], match[2]));
                match = pattern.exec(html);
            }
        },
        querySelector: (selector) => {
            const id = /"([^"]+)"/.exec(selector);
            return kids.find((kid) => kid.id === (id && id[1])) || null;
        },
        querySelectorAll: () => kids.slice(),
    };
    return surface;
}

function objectStub(kind, id) {
    const attributes = {};
    return {
        id: id,
        kind: kind,
        tabIndex: -1,
        attributes: attributes,
        dataset: kind === "panel" ? { panelId: id } : { annotationId: id },
        style: {}, classList: { add() {}, remove() {}, toggle() {} },
        setAttribute: (name, value) => { attributes[name] = String(value); },
        getAttribute: (name) => (name in attributes ? attributes[name] : null),
        focus() {},
    };
}

function elementStub() {
    return {
        style: {}, dataset: {}, innerHTML: "",
        classList: { add() {}, remove() {}, toggle() {} },
        addEventListener() {}, focus() {}, contains: () => false,
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        querySelector: () => null, querySelectorAll: () => [],
    };
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, parseFloat, parseInt, isNaN,
    RegExp, isFinite,
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    document: {
        readyState: "complete", activeElement: null,
        getElementById: () => null, createElement: () => elementStub(),
        addEventListener() {}, removeEventListener() {},
    },
    window: {
        addEventListener() {}, removeEventListener() {},
        devicePixelRatio: 1, innerWidth: 1400, innerHeight: 900,
        getComputedStyle: () => ({}),
        crypto: { randomUUID: () => "0123456789abcdef0123456789abcdef" },
    },
});
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}
ctx.__surface = surfaceStub;
ctx.__element = elementStub;

runInContext(`
    globalThis.__buildCanvas = function () {
        const document_ = {
            schema_version: 1, revision: 1, figure_id: "fig_a11y", title: "T",
            settings: { label_style: "A", body_size_pt: 8 },
            sources: { src_1: { source_id: "src_1", kind: "plexora_project",
                                datasource: "demo",
                                pixel_size: { value: 0.5, unit: "um" } } },
            pages: [{ page_id: "pg_1", name: "Page 1", preset: "a4",
                      orientation: "portrait", size_mm: { w: 210, h: 297 },
                      margins_mm: { top: 10, right: 10, bottom: 10, left: 10 },
                      background: "#ffffff" }],
            panels: {
                pnl_a: __panel("pnl_a", 10, 10, "Tumour core"),
                pnl_b: __panel("pnl_b", 60, 10, ""),
            },
            annotations: {
                ann_1: {
                    annotation_id: "ann_1", page_id: "pg_1", type: "text", z: 1,
                    geometry: { x_mm: 10, y_mm: 60, w_mm: 40, h_mm: 8, rotation: 0 },
                    style: { font_family: "Helvetica", font_size_pt: 8,
                             color: "#000000", align: "left", valign: "top",
                             line_height: 1.2, autofit: true },
                    rich: { lines: [{ runs: [{ text: "Scale bars, 100 um" }] }] },
                },
            },
            groups: {},
        };
        const state = {
            document: document_,
            sourceStatus: {},
            panel: (id) => document_.panels[id] || null,
            source: (id) => document_.sources[id] || null,
            commit: () => Promise.resolve(true),
        };
        const canvas = new FigureCanvas({
            state: state,
            api: { previewUrl: () => "preview" },
            figureId: "fig_a11y",
            pageEl: __element(),
            surfaceEl: __surface(),
            guideEl: __element(),
            onSelectionChange: () => {},
        });
        canvas.pageId = "pg_1";
        canvas.render();
        return canvas;
    };
    globalThis.__panel = function (id, x, y, title) {
        return {
            panel_id: id, source_id: "src_1", title: title, render_revision: 1,
            placement: { page_id: "pg_1", x_mm: x, y_mm: y, w_mm: 40, h_mm: 30, z: 0 },
            label: { text: "", auto: true, visible: true },
            ...FigureSchema.defaultFurniture(),
            scene: { viewport: { x: 0, y: 0, w: 512, h: 512 }, channels: [] },
        };
    };
    globalThis.__press = function (canvas, key, flags) {
        const held = flags || {};
        let prevented = false;
        canvas.surfaceKeyDown({
            key: key,
            shiftKey: Boolean(held.shift),
            ctrlKey: Boolean(held.ctrl),
            metaKey: false,
            preventDefault: () => { prevented = true; },
        });
        return prevented;
    };
    globalThis.__objects = function (canvas) {
        return canvas.surfaceEl.querySelectorAll().map((kid) => ({
            id: kid.id,
            role: kid.getAttribute("role"),
            selected: kid.getAttribute("aria-selected"),
            label: kid.getAttribute("aria-label"),
            tabIndex: kid.tabIndex,
        }));
    };
`, ctx);

const run = (source) => runInContext(`(() => { ${source} })()`, ctx);

// -- what the sheet says it holds --------------------------------------------

const described = run(`
    const canvas = __buildCanvas();
    return __objects(canvas);
`);
check("every object on the sheet is an option", described.length, 3);
check("...with a role", described.every((entry) => entry.role === "option"), true);
check("...and a name", described.every((entry) => entry.label && entry.label.length), true);
// The user's own words, because nothing else tells two crops of one slide
// apart. A title where there is one, the kind where there is not.
check("a titled panel is named by its title", described[0].label, "Panel: Tumour core");
check("an untitled one still says what it is", described[1].label, "Panel");
check("and a caption is named by what it says",
    described[2].label, "Text: Scale bars, 100 um");

// ONE tab stop. Thirty panels must not be thirty presses of Tab between the
// canvas and the status bar.
check("exactly one object is in the tab order",
    described.filter((entry) => entry.tabIndex === 0).length, 1);
check("and it is the first", described[0].tabIndex, 0);

// -- walking it --------------------------------------------------------------

const walked = run(`
    const canvas = __buildCanvas();
    const seen = [canvas.focusId];
    __press(canvas, "ArrowRight");
    seen.push(canvas.focusId);
    __press(canvas, "ArrowRight");
    seen.push(canvas.focusId);
    // Round, rather than stopping: a list of three walked forwards four times
    // is back where it started, which is what a listbox does.
    __press(canvas, "ArrowRight");
    seen.push(canvas.focusId);
    __press(canvas, "ArrowLeft");
    seen.push(canvas.focusId);
    return { seen: seen, selection: Array.from(canvas.selection) };
`);
check("the arrows walk the objects in order",
    walked.seen, ["pnl_a", "pnl_b", "ann_1", "pnl_a", "ann_1"]);
// The cursor is not the selection. Moving to look at something is not taking
// it, and a cursor that selected as it went would make Shift the only way to
// look at anything.
check("and select nothing on the way", walked.selection, []);

const took = run(`
    const canvas = __buildCanvas();
    __press(canvas, "ArrowRight");
    __press(canvas, "Enter");
    const one = Array.from(canvas.selection);
    return { one: one, tab: __objects(canvas).map((e) => e.tabIndex),
             flags: __objects(canvas).map((e) => e.selected) };
`);
check("Enter takes what the cursor is on", took.one, ["pnl_b"]);
// The selection is REPORTED, not merely held: the same attribute a screen
// reader reads is the one the canvas draws its outline from.
check("and the sheet says which option is selected", took.flags,
    ["false", "true", "false"]);
check("the cursor stays where it was", took.tab, [-1, 0, -1]);

const added = run(`
    const canvas = __buildCanvas();
    __press(canvas, "Enter");
    // Ctrl, because a BARE arrow nudges once something is selected -- see the
    // next block. Without a way to move the cursor while a selection stands
    // there would be no way to build one from the keyboard at all.
    const moved = __press(canvas, "ArrowRight", { ctrl: true });
    const between = Array.from(canvas.selection);
    __press(canvas, "Enter", { shift: true });
    return { moved: moved, between: between,
             both: Array.from(canvas.selection), cursor: canvas.focusId };
`);
check("the modifier moves the cursor past a standing selection", added.moved, true);
check("...without disturbing it", added.between, ["pnl_a"]);
check("...and Shift+Enter adds what it lands on", added.both, ["pnl_a", "pnl_b"]);
check("...leaving the cursor where it was put", added.cursor, "pnl_b");

// -- the incumbent meaning of the arrows -------------------------------------
//
// This is the regression that would matter. A nudge is a half-millimetre move
// of what is selected, bound to the arrows since long before any of this, and
// it is the only way to place an object precisely at a low zoom.

const nudging = run(`
    const canvas = __buildCanvas();
    __press(canvas, "Enter");
    const before = canvas.focusId;
    const handled = __press(canvas, "ArrowRight");
    return { before: before, after: canvas.focusId, handled: handled,
             selection: Array.from(canvas.selection) };
`);
check("with something selected the arrows are left alone", nudging.handled, false);
check("...so the cursor does not move either", nudging.after, nudging.before);
check("...and the selection stands", nudging.selection, ["pnl_a"]);

const released = run(`
    const canvas = __buildCanvas();
    __press(canvas, "Enter");
    __press(canvas, "Escape");
    const cleared = Array.from(canvas.selection);
    const handled = __press(canvas, "ArrowRight");
    return { cleared: cleared, handled: handled, focus: canvas.focusId };
`);
check("Escape lets go", released.cleared, []);
check("and the arrows go back to walking", released.handled, true);
check("from where the cursor was", released.focus, "pnl_b");

// -- a sheet with nothing on it ----------------------------------------------

const empty = run(`
    const canvas = __buildCanvas();
    canvas.state.document.panels = {};
    canvas.state.document.annotations = {};
    canvas.render();
    return { focus: canvas.focusId, handled: __press(canvas, "ArrowRight"),
             objects: __objects(canvas).length };
`);
check("an empty page has nothing to walk", empty.objects, 0);
check("...and no cursor", empty.focus, null);
check("...and does not swallow the key", empty.handled, false);

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
