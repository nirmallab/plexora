/**
 * Split Composite: the move this plugin is worth building for.
 *
 * Making a split-channel row by hand is: find the field again, turn off every
 * channel but one, screenshot, repeat, then line five images up and hope they
 * are the same crop. Two things have to be true for this to be better than
 * that, and neither can be checked anywhere else in the suite:
 *
 * * every derived panel carries the SAME viewport and the SAME window as the
 *   composite. A split that re-found the crop, or re-levelled each channel,
 *   produces a row nobody can compare -- which is the exact failure the manual
 *   method has;
 *
 * * the whole thing is ONE undo step. A five-channel split that undoes as five
 *   deletions is a feature people use once.
 *
 * Run directly:
 *   node tests/js/figure_split_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureSchema.js", "figureCanvas.js"];

const problems = [];
const commits = [];

function elementStub() {
    return {
        style: {}, dataset: {}, innerHTML: "",
        classList: { add() {}, remove() {}, toggle() {} },
        addEventListener() {},
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        querySelector: () => null, querySelectorAll: () => [],
    };
}

function browserGlobals() {
    const globals = {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Infinity, parseFloat, isNaN,
        setTimeout: () => 1, clearTimeout: () => {},
        document: {
            readyState: "complete", activeElement: null,
            getElementById: () => null, createElement: () => elementStub(),
            addEventListener() {}, removeEventListener() {},
        },
    };
    globals.window = {
        crypto: { randomUUID: () => "0123456789abcdef0123456789abcdef" },
        addEventListener() {}, removeEventListener() {},
    };
    globals.crypto = globals.window.crypto;
    return globals;
}

const ctx = createContext(browserGlobals());
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

// Ids are random by design, so the probe hands out predictable ones instead --
// otherwise nothing below could name the panels it is asserting about.
let counter = 0;
runInContext("FigureSchema.newId = (prefix) => prefix + '_' + (globalThis.__next());", ctx);
ctx.__next = () => String(++counter).padStart(3, "0");

ctx.__record = (operations) => commits.push(operations);
ctx.__element = elementStub;

runInContext(`
    globalThis.__buildCanvas = function () {
        const document_ = {
            schema_version: 1, revision: 1, figure_id: "fig_test", title: "T",
            sources: { src_1: { source_id: "src_1", kind: "plexora_project",
                                datasource: "demo",
                                pixel_size: { value: 0.5, unit: "um" } } },
            pages: [{ page_id: "pg_1", name: "Page 1", preset: "a4",
                      orientation: "portrait", size_mm: { w: 210, h: 297 },
                      margins_mm: { top: 10, right: 10, bottom: 10, left: 10 },
                      background: "#ffffff" }],
            panels: {
                pnl_c: {
                    panel_id: "pnl_c", source_id: "src_1",
                    scene: {
                        snapshot_version: 1, source_id: "src_1",
                        viewport: { x: 1234.5, y: 678.9, w: 4096, h: 3072 },
                        channels: [
                            { key: "demo_0", fullname_at_capture: "DAPI",
                              color: { r: 0, g: 0, b: 255 }, window: [100, 9000], visible: true },
                            { key: "demo_1", fullname_at_capture: "CD8a",
                              color: { r: 255, g: 0, b: 0 }, window: [300, 12000], visible: true },
                            { key: "demo_2", fullname_at_capture: "CD3",
                              color: { r: 0, g: 255, b: 0 }, window: [50, 4000], visible: true },
                        ],
                        core_overlays: { cell_layers: [], hd_tiles: false, scalebar_visible: true },
                        plugins: {}, captured_at: "2026-01-01T00:00:00Z",
                    },
                    placement: { page_id: "pg_1", x_mm: 20, y_mm: 20,
                                 w_mm: 40, h_mm: 30, z: 0 },
                    label: { text: "", auto: true, visible: true },
                    title: "Composite",
                    scalebar: { visible: true, target_um: null },
                    legend: { channels: true, plugins: false },
                    link_group: null, render_revision: 3, derived_from: null,
                },
            },
            annotations: {}, link_groups: {},
            settings: { dpi_default: 300, label_style: "A",
                        style: { gutter_mm: 3, font_size_pt: 8, label_size_pt: 10,
                                 title_size_pt: 9, line_width_pt: 0.75,
                                 font_family: "Helvetica", text_color: "#000000",
                                 panel_background: "#000000" } },
        };
        const state = {
            document: document_, sourceStatus: {},
            panel: (id) => document_.panels[id] || null,
            source: (id) => document_.sources[id] || null,
            commit: (operations, mutate) => {
                __record(operations);
                if (mutate) mutate(document_);
                return Promise.resolve(true);
            },
        };
        const canvas = new FigureCanvas({
            state, api: { previewUrl: () => "p", assetUrl: () => "a" },
            figureId: "fig_test",
            pageEl: __element(), surfaceEl: __element(), guideEl: __element(),
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

// -- composite + channels -------------------------------------------------

commits.length = 0;
const split = runInContext(`(() => {
    const canvas = __buildCanvas();
    const groupId = canvas.splitComposite("pnl_c", "with_composite");
    return { groupId, document: canvas.state.document };
})()`, ctx);

check("a split is one commit", commits.length, 1);

const operations = commits[0];
check("the batch is add x3, one move, one link",
    operations.map((op) => op.op),
    ["add_panel", "add_panel", "add_panel", "move_panels", "link_panels"]);

const derived = operations.filter((op) => op.op === "add_panel").map((op) => op.panel);
check("one derived panel per channel", derived.length, 3);

// The crop is COPIED, not re-found. This is the whole difference between this
// and doing it by hand.
for (const panel of derived) {
    check(`${panel.title} keeps the composite's viewport`, panel.scene.viewport,
        { x: 1234.5, y: 678.9, w: 4096, h: 3072 });
    check(`${panel.title} shows exactly one channel`, panel.scene.channels.length, 1);
}
check("each derived panel carries a different channel",
    derived.map((panel) => panel.scene.channels[0].key), ["demo_0", "demo_1", "demo_2"]);
// The windows are the composite's too: re-levelling each channel would make the
// row incomparable, which is exactly the failure the manual method has.
check("the display windows are the composite's",
    derived.map((panel) => panel.scene.channels[0].window),
    [[100, 9000], [300, 12000], [50, 4000]]);
check("panels are titled from their channel",
    derived.map((panel) => panel.title), ["DAPI", "CD8a", "CD3"]);
// Lineage, for the provenance page and for a future regeneration.
check("each records what it came from",
    derived.map((panel) => panel.derived_from.operation),
    ["split_channel", "split_channel", "split_channel"]);
check("and names the panel it came from",
    derived.map((panel) => panel.derived_from.panel_id), ["pnl_c", "pnl_c", "pnl_c"]);

// The composite stays, first, and the row runs left to right with the
// document's gutter.
const moves = operations.find((op) => op.op === "move_panels").moves;
check("the composite leads the row", moves[0].panel_id, "pnl_c");
check("laid out with the document's gutter",
    moves.map((move) => move.placement.x_mm), [20, 63, 106, 149]);
check("all on the same row", moves.map((move) => move.placement.y_mm), [20, 20, 20, 20]);
check("all the same size", moves.map((move) => [move.placement.w_mm, move.placement.h_mm]),
    [[40, 30], [40, 30], [40, 30], [40, 30]]);

const link = operations.find((op) => op.op === "link_panels").group;
check("the row is linked by crop and size", link.sync, ["viewport", "size"]);
check("and every panel is in it", link.panel_ids.length, 4);

// -- channels only ---------------------------------------------------------

commits.length = 0;
runInContext(`(() => {
    const canvas = __buildCanvas();
    canvas.splitComposite("pnl_c", "channels_only");
})()`, ctx);
const kinds = commits[0].map((op) => op.op);
check("dropping the composite is part of the same batch", kinds,
    ["add_panel", "add_panel", "add_panel", "move_panels", "remove_panels", "link_panels"]);
check("and it is the composite that goes",
    commits[0].find((op) => op.op === "remove_panels").panel_ids, ["pnl_c"]);

// -- a panel with nothing to split ----------------------------------------

commits.length = 0;
const refused = runInContext(`(() => {
    const canvas = __buildCanvas();
    canvas.state.document.panels.pnl_c.scene.channels =
        [canvas.state.document.panels.pnl_c.scene.channels[0]];
    return canvas.splitComposite("pnl_c", "with_composite");
})()`, ctx);
check("a single-channel panel cannot be split", refused, null);
check("and nothing is committed", commits.length, 0);

// -- linked resize ---------------------------------------------------------

commits.length = 0;
runInContext(`(() => {
    const canvas = __buildCanvas();
    canvas.splitComposite("pnl_c", "with_composite");
    __record.calls = 0;
    const row = Object.keys(canvas.state.document.link_groups)[0];
    const ids = canvas.state.document.link_groups[row].panel_ids;
    const target = canvas.state.panel(ids[1]);
    globalThis.__linked = canvas._linkedSizeMoves(target, { w_mm: 55, h_mm: 41 });
})()`, ctx);
const linked = ctx.__linked;
check("resizing one linked panel resizes the others", linked.length, 3);
check("to the new size", linked.map((move) => [move.placement.w_mm, move.placement.h_mm]),
    [[55, 41], [55, 41], [55, 41]]);
// Positions are untouched: sharing a POSITION would mean the row could never
// be a row, because dragging one panel would drag them all onto each other.
check("and leaves their positions alone",
    linked.map((move) => move.placement.x_mm), [20, 106, 149]);

console.error(JSON.stringify({ problems, commits: commits.length }, null, 2));
process.exit(problems.length ? 1 : 0);
