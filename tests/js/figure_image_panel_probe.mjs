/**
 * The image sidebar, driven against a stub DOM.
 *
 * It replaces six popovers on the floating bar, and the reason it is a panel is
 * the reason the cases below matter: a figure is a ROW of crops of one slide,
 * so several panels selected at once is the normal state, not the exception.
 *
 *   * a control that claims a multi-selection and then acts on whichever panel
 *     was first is worse than one that is absent: the user sets a scale bar on
 *     six panels and gets one, and the other five look set until export;
 *
 *   * six panels changed in six commits is six presses of Ctrl+Z with the
 *     figure sitting part-way through in between;
 *
 *   * a legend across panels that colour the same marker differently is a
 *     figure that misleads a reader, so the clash has to be SHOWN rather than
 *     resolved silently -- and "keep them separate" has to stay the first
 *     answer, because the other one repaints scientific images;
 *
 *   * numbering is the whole FIGURE's. Writing it to the selected panel would
 *     give one page two numbering schemes, and which one applied would depend
 *     on what happened to be selected when it was changed.
 *
 * Run directly:
 *   node tests/js/figure_image_panel_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureSchema.js", "figureColorField.js", "figureImagePanel.js"];

const problems = [];

function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

/** Just enough of an element for a panel that only ever sets innerHTML and
 *  queries it back. The markup is kept as a STRING and matched against, which
 *  is the honest thing to assert without a DOM parser: what the panel emits is
 *  markup, and a real DOM here would be a second implementation of one. */
function rootStub() {
    return {
        innerHTML: "",
        contains: () => false,
        addEventListener() {},
        querySelector(selector) {
            const id = selector.replace(/[#[\]"']/g, "");
            if (!this.innerHTML.includes(id)) return null;
            return { value: "", checked: false, textContent: "",
                     focus() {}, setSelectionRange() {} };
        },
    };
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, parseFloat, parseInt, isNaN,
    RegExp, isFinite,
    document: {
        readyState: "complete", activeElement: null,
        getElementById: () => null, createElement: () => rootStub(),
        addEventListener() {}, removeEventListener() {},
    },
    window: { addEventListener() {}, removeEventListener() {} },
});
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}
ctx.__root = rootStub;

const calls = [];
ctx.__record = (what, payload) => calls.push({ what, payload });

runInContext(`
    globalThis.__panelRecord = function (id, extra) {
        return Object.assign({
            panel_id: id, source_id: "src_1", title: "", render_revision: 1,
            placement: { page_id: "pg_1", x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 30, z: 0 },
            label: { text: "", auto: true, visible: true },
            // Through the shared defaults rather than spelled out, so a field
            // added to the format reaches the fixture the same way it reaches a
            // captured panel -- and a panel built without one is exactly the
            // bug this goes through the helper to avoid.
            ...FigureSchema.defaultFurniture(),
            scene: {
                viewport: { x: 0, y: 0, w: 512, h: 512 },
                channels: [{ key: "ch_0", fullname_at_capture: "DNA",
                             color: { r: 0, g: 0, b: 255 }, window: [0, 1000],
                             visible: true }],
            },
        }, extra || {});
    };
    globalThis.__build = function (panels, annotations, sources) {
        const document_ = {
            panels: panels, annotations: annotations || {},
            settings: { label_style: "A" },
            sources: sources || { src_1: { source_id: "src_1",
                kind: "plexora_project", datasource: "demo",
                pixel_size: { value: 0.5, unit: "µm" } } },
        };
        const panel = new FigureImagePanel({
            root: __root(),
            canvas: null,
            state: {
                document: document_,
                panel: (id) => document_.panels[id] || null,
                source: (id) => document_.sources[id] || null,
            },
            handlers: {
                onPanelChange: (id, changes) => __record("panelChange", { id, changes }),
                onPanelsChange: (updates) => __record("panelsChange", updates),
                onSettingsChange: (settings) => __record("settings", settings),
                onShareLegendColours: (ids) => __record("share", ids),
                onSetPixelSize: (ids, value) => __record("pixelSize", { ids, value }),
                onQuickEdit: (id) => __record("quickEdit", id),
                onSplit: (mode) => __record("split", mode),
                onCopyRendering: (id) => __record("copy", id),
                onApplyRendering: (ids) => __record("apply", ids),
                hasRenderClipboard: () => globalThis.__armed === true,
            },
        });
        return panel;
    };
`, ctx);

const run = (source) => runInContext(`(() => { ${source} })()`, ctx);

function acted(source) {
    calls.length = 0;
    run(source);
    return calls.slice();
}

// -- what it claims ----------------------------------------------------------

const claims = run(`
    const panel = __build(
        { pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b"),
          pnl_tray: Object.assign(__panelRecord("pnl_tray"), { placement: null }) },
        { ann_1: { annotation_id: "ann_1", type: "text" } });
    const asks = (ids) => { panel.update(ids); return panel.wants; };
    return {
        one: asks(["pnl_a"]),
        several: asks(["pnl_a", "pnl_b"]),
        annotation: asks(["ann_1"]),
        none: asks([]),
        // In the TRAY, not on the page: it has no scale bar, no legend and no
        // place, and every control here is about a panel that is placed.
        unplaced: asks(["pnl_tray"]),
    };
`);
check("one panel gets the panel", claims.one, true);
// Unlike the text, shape and line panels, which take a single selection only:
// a scale bar across a row is the case this exists for.
check("and so do several", claims.several, true);
check("an annotation does not", claims.annotation, false);
check("nor does an empty selection", claims.none, false);
check("nor does a panel that is only in the tray", claims.unplaced, false);

const dismissal = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a"]);
    panel.dismissed = true;
    const shut = panel.wants;
    panel.update(["pnl_b"]);
    return { shut: shut, nextOne: panel.wants };
`);
check("shutting the panel shuts it", dismissal.shut, false);
// "Not for this one", not "never again" -- otherwise closing it once turns the
// panel off for the rest of the session with nothing that says so.
check("but only for the selection it was shut on", dismissal.nextOne, true);

// -- what it draws -----------------------------------------------------------

const single = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
check("the panel rendered something", single.length > 800, true);
for (const needle of ["fb_image_title", "fb_image_label", "fb_image_bar_len",
                      "legend_channels", "copy_rendering", "quick_edit"]) {
    check(`a single selection has ${needle}`, single.includes(needle), true);
}

const several = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b"),
                            pnl_c: __panelRecord("pnl_c") });
    panel.update(["pnl_a", "pnl_b", "pnl_c"]);
    return panel.root.innerHTML;
`);
// Quick Edit edits one view, the viewer opens one view, a split replaces one
// panel, and a title is one panel's. None has a reading for three, and a
// button that quietly picked the first is the failure this avoids.
check("three panels lose the one-panel actions",
    ["quick_edit", "fb_image_title"].some((needle) => several.includes(needle)), false);
// These do have a reading for three, and it is the reason the panel exists.
check("and keep the ones that apply to all of them",
    ["fb_image_bar_len", "legend_channels", "apply_rendering"]
        .every((needle) => several.includes(needle)), true);
check("and say how many they will act on", several.includes("on all 3"), true);

const conflicted = run(`
    const blue = __panelRecord("pnl_a");
    const green = __panelRecord("pnl_b");
    green.scene.channels = [{ key: "ch_0", fullname_at_capture: "DNA",
                              color: { r: 0, g: 255, b: 0 }, window: [0, 1000],
                              visible: true }];
    const panel = __build({ pnl_a: blue, pnl_b: green });
    panel.update(["pnl_a", "pnl_b"]);
    return panel.root.innerHTML;
`);
// Shown, and with "keep them separate" first. The other button recolours
// scientific images, so it is never the default and never automatic.
check("a colour clash is reported rather than resolved",
    conflicted.includes("different colors for"), true);
check("with keeping them separate offered first",
    conflicted.indexOf("legend_keep") < conflicted.indexOf("legend_share"), true);

const uncalibrated = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") }, {},
        { src_1: { source_id: "src_1", kind: "plexora_project", datasource: "demo",
                   pixel_size: null } });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
// A scale bar drawn from an assumed pixel size is wrong and looks exactly like
// one that is right. So the panel says there is none, and offers the number.
check("an image with no pixel size says so", uncalibrated.includes("no pixel size"), true);
check("and offers somewhere to type one", uncalibrated.includes("fb_image_mpp"), true);

const notEditable = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") }, {},
        { src_1: { source_id: "src_1", kind: "imported_asset", asset_id: "ast_1" } });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
check("an imported image cannot be quick-edited",
    /data-act="quick_edit"[^>]*disabled/.test(notEditable), true);

// -- what it commits ---------------------------------------------------------

const scalebar = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b"),
                            pnl_c: __panelRecord("pnl_c") });
    panel.update(["pnl_a", "pnl_b", "pnl_c"]);
    panel.changed({ target: { dataset: { field: "scalebar" }, checked: true } });
`);
// ONE call carrying three panels, not three calls. Three commits would be
// three presses of Ctrl+Z with the row part-way through in between.
check("a scale bar across three panels is one change", scalebar.length, 1);
check("and it carries all three",
    scalebar[0].payload.map((entry) => entry.panel_id), ["pnl_a", "pnl_b", "pnl_c"]);
check("with the bar switched on for each",
    scalebar[0].payload.every((entry) => entry.changes.scalebar.visible === true), true);

const numbering = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.changed({ target: { dataset: { field: "label_style" }, value: "a" } });
`);
// The FIGURE's, not the panel's: every label on the page is drawn from it.
check("numbering goes to the document",
    numbering, [{ what: "settings", payload: { label_style: "a" } }]);

const typedLabel = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.changed({ target: { dataset: { field: "label" }, value: "A'" } });
`);
// Typing a label makes it the user's, so it stops renumbering when the page is
// rearranged. That is the whole difference between the two.
check("a typed label stops being automatic",
    typedLabel[0].payload.changes.label, { text: "A'", auto: false, visible: true });

const titleAcrossThree = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    panel.changed({ target: { dataset: { field: "title" }, value: "Tumour" } });
`);
// Two panels have no one title, and writing it to the first is the silent
// failure a control that is absent cannot have.
check("a title is not applied to a multi-selection", titleAcrossThree, []);

const clicks = acted(`
    globalThis.__armed = true;
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    panel.clicked({ target: { closest: (selector) =>
        selector === "[data-act]" ? { dataset: { act: "apply_rendering" } } : null } });
`);
check("Apply rendering runs on every selected panel",
    clicks, [{ what: "apply", payload: ["pnl_a", "pnl_b"] }]);

// -- the scale bar's length, and the unit it is written in -------------------
//
// The length is stored in MICRONS whatever unit is on screen, which is what
// makes a figure whose panels are labelled in different units still comparable
// -- and what stops switching the unit resizing the bar.

const lengths = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    const set = (field, value) => panel.changed(
        { target: { dataset: { field: field }, value: value } });
    set("scalebar_unit", "mm");
    panel.state.document.panels.pnl_a.scalebar.unit = "mm";
    set("scalebar_length", "0.5");
`);
check("choosing a unit does not change the length",
    lengths[0].payload[0].changes.scalebar.target_um, null);
check("and a length typed in millimetres is stored in microns",
    lengths[1].payload[0].changes.scalebar.target_um, 500);

const auto = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.changed({ target: { dataset: { field: "scalebar_length" }, value: "" } });
`);
// Empty is "a round number that fits", which is a different answer per panel --
// stored as null rather than as whatever fits this one.
check("an empty length goes back to automatic",
    auto[0].payload[0].changes.scalebar.target_um, null);

// -- the nine-anchor keypad --------------------------------------------------

const anchored = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    const press = (group, anchor) => panel.clicked({ target: { closest: (selector) =>
        selector === "[data-anchor]"
            ? { dataset: { anchor: anchor },
                closest: () => ({ dataset: { anchors: group } }) }
            : null } });
    press("scalebar", "top_left");
    press("colorbar", "middle_right");
`);
check("a corner picked for the scale bar reaches every selected panel",
    anchored[0].payload.map((entry) => [entry.panel_id, entry.changes.scalebar.position]),
    [["pnl_a", "top_left"], ["pnl_b", "top_left"]]);
check("and the colour bar has its own corner",
    anchored[1].payload[0].changes.colorbar.position, "middle_right");

// -- captions ----------------------------------------------------------------

const added = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    panel.changed({ target: { dataset: { field: "new_label_text" }, value: " Tumor " } });
    panel.clicked({ target: { closest: (selector) =>
        selector === "[data-act]" ? { dataset: { act: "add_label" } } : null } });
    globalThis.__draftAfter = panel.draft.text;
`);
check("adding a caption puts one on every selected panel", added.length, 1);
check("each panel gets its own", added[0].payload.length, 2);
const captions = added[0].payload.map((entry) => entry.changes.labels[0]);
check("with the text trimmed", captions.map((entry) => entry.text), ["Tumor", "Tumor"]);
// One id per panel: the same word on six images is six captions, each editable
// where it sits rather than one shared row that six panels disagree about.
check("and a distinct id each", captions[0].label_id !== captions[1].label_id, true);
check("the text box is emptied so the next one can be typed",
    run("return globalThis.__draftAfter;"), "");

const blank = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.changed({ target: { dataset: { field: "new_label_text" }, value: "   " } });
    panel.clicked({ target: { closest: (selector) =>
        selector === "[data-act]" ? { dataset: { act: "add_label" } } : null } });
`);
check("an empty caption adds nothing", blank, []);

ctx.__labelled = () => {
    const record = ctx.__panelRecord("pnl_a");
    record.labels = [
        { label_id: "lbl_1", text: "one", position: "top_left",
          color: "#ffffff", size_pt: null, bold: false, italic: false },
        { label_id: "lbl_2", text: "two", position: "top_left",
          color: "#ffffff", size_pt: null, bold: false, italic: false },
    ];
    return record;
};

const edited = acted(`
    const panel = __build({ pnl_a: __labelled() });
    panel.update(["pnl_a"]);
    const act = (name) => panel.clicked({ target: { closest: (selector) =>
        selector === "[data-act]" ? { dataset: { act: name } } : null } });
    panel.changed({ target: { dataset: { field: "label_text", labelId: "lbl_2" },
                              value: "second" } });
    act("label_up:lbl_2");
    act("label_delete:lbl_1");
`);
check("editing one caption rewrites only that one",
    edited[0].payload.changes.labels.map((entry) => entry.text), ["one", "second"]);
// The order decides which of two captions in the same corner is on top, and
// nothing else -- so moving one is a swap, not a re-layout.
check("moving one swaps it with its neighbour",
    edited[1].payload.changes.labels.map((entry) => entry.label_id), ["lbl_2", "lbl_1"]);
check("deleting one leaves the rest",
    edited[2].payload.changes.labels.map((entry) => entry.label_id), ["lbl_2"]);

const labelUi = run(`
    const panel = __build({ pnl_a: __labelled() });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
check("each caption gets a delete", labelUi.includes('data-act="label_delete:lbl_1"'), true);
// The first has nothing above it and the last nothing below, and a button that
// looks pressable and does nothing is worse than one that is plainly off.
check("the first cannot move up",
    /data-act="label_up:lbl_1"[^>]*disabled/.test(labelUi), true);
check("the last cannot move down",
    /data-act="label_down:lbl_2"[^>]*disabled/.test(labelUi), true);

const labelsAcrossTwo = run(`
    const panel = __build({ pnl_a: __labelled(), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    return panel.root.innerHTML;
`);
// Two panels' captions interleaved in one list, with nothing saying which row
// belongs to which image, is a list nobody could edit.
check("several panels can be added to but not edited row by row",
    labelsAcrossTwo.includes('data-act="add_label"')
        && !labelsAcrossTwo.includes("label_delete"), true);

// -- the colour bar ----------------------------------------------------------

const colourBar = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    panel.changed({ target: { dataset: { field: "colorbar" }, checked: true } });
    panel.changed({ target: { dataset: { field: "colorbar_ticks" }, value: "5" } });
`);
check("a colour bar switched on reaches every selected panel",
    colourBar[0].payload.every((entry) => entry.changes.colorbar.visible === true), true);
check("and so does its tick count",
    colourBar[1].payload.map((entry) => entry.changes.colorbar.ticks), [5, 5]);

// -- the legend has no overlays any more -------------------------------------

check("the legend offers channels and nothing else",
    single.includes("legend_channels") && !single.includes("legend_plugins"), true);

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
