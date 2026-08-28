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
 *   * the captions across a selection are MERGED into one row per distinct
 *     text, so "DNA_2" on four panels is one row and renaming it renames all
 *     four. A merged row must never be addressed by the text it is keyed on:
 *     the first keystroke of a rename would stop matching, and the second would
 *     apply to nothing;
 *
 *   * a scale bar's length is stored in microns whatever unit is on screen --
 *     except a PIXEL bar, which has no microns at all and is stored beside it.
 *     One field for both would make "500" mean 500 px one moment and 500 µm the
 *     next;
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
// figureActions.js is here because the panel's action buttons are drawn FROM
// the registry now, and a stub of it would agree with the panel by
// construction. That is the point of loading the real one: the bug this
// replaced was two copies of a predicate disagreeing.
const SCRIPTS = ["figureSchema.js", "figureColorField.js", "figureChoiceField.js",
                 "figureActions.js", "figureImagePanel.js"];

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
//: What a queried field reports as its value. The panel reads one control out
//: of the DOM rather than out of its own state -- the pixel-size box, which is
//: not a `data-field` because it is applied by a button and not on every
//: keystroke -- so a stub that always answered "" could not exercise Update.
let typed = "";

function rootStub() {
    return {
        innerHTML: "",
        contains: () => false,
        addEventListener() {},
        querySelector(selector) {
            const id = selector.replace(/[#[\]"']/g, "");
            if (!this.innerHTML.includes(id)) return null;
            return { value: typed, checked: false, textContent: "",
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
            panel_id: id, source_id: "src_1", render_revision: 1,
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
    globalThis.__build = function (panels, annotations, sources, status) {
        const document_ = {
            panels: panels, annotations: annotations || {},
            settings: { label_style: "A" },
            sources: sources || { src_1: { source_id: "src_1",
                kind: "plexora_project", datasource: "demo",
                display_name: "Slide 7",
                pixel_size: { value: 0.5, unit: "µm" } } },
        };
        const panel = new FigureImagePanel({
            root: __root(),
            // Null on purpose: the panel describes its selection to the
            // registry, and it has no canvas to describe it with.
            canvas: null,
            state: {
                document: document_,
                sourceStatus: status || {},
                panel: (id) => document_.panels[id] || null,
                source: (id) => document_.sources[id] || null,
            },
            handlers: {
                onPanelChange: (id, changes) => __record("panelChange", { id, changes }),
                onPanelsChange: (updates) => __record("panelsChange", updates),
                onSettingsChange: (settings) => __record("settings", settings),
                onSetPixelSize: (ids, value, panelIds) =>
                    __record("pixelSize", { ids, value, panelIds }),
                onQuickEdit: (id) => __record("quickEdit", id),
                onSplit: (mode) => __record("split", mode ?? null),
                onCopyRendering: (id) => __record("copy", id),
                onApplyRendering: (ids) => __record("apply", ids),
                hasRenderClipboard: () => globalThis.__armed === true,
            },
        });
        return panel;
    };
    //: A click on a delegated button, as the panel's own handler sees it.
    globalThis.__act = function (panel, name) {
        panel.clicked({ target: { closest: (selector) =>
            selector === "[data-act]" ? { dataset: { act: name } } : null } });
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
        // In the TRAY, not on the page: it has no scale bar and no place, and
        // every control here is about a panel that is placed.
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
// It was seven folds, then seven sections. Neither was the answer: a scale bar
// is five controls and one row, not eleven controls behind a disclosure. Nothing
// anybody opens this panel to change is hidden any more.
for (const needle of ["fb_image_mpp", "fb_image_bar_len", "fb_image_cb_ticks",
                      "fb_image_new_label", "quick_edit", "copy_rendering"]) {
    check(`a single selection shows ${needle} unfolded`, single.includes(needle), true);
}
// The panel's own A/B/C is the exception, and it earns it: it is set once when
// the figure is laid out and then left alone.
check("the panel label is still a fold",
    single.includes('data-fold="panel_label"'), true);
check("and its body is not built until it is opened",
    single.includes("fb_image_label"), false);
// One fold, and no others -- five disclosures with the panel's whole content
// behind them is the arrangement this replaced.
check("nothing else is behind a disclosure",
    (single.match(/data-fold=/g) || []).length, 1);

// Three things a panel could carry a word on are gone, and the captions are what
// survived: a caption carries its own corner, size and colour, which is what the
// other two could never do.
for (const gone of ["fb_image_title", "legend_channels", "split_channels_only"]) {
    check(`${gone} is gone`, single.includes(gone), false);
}

// The two ways back to the image sit on one row -- they are two halves of the
// same intention.
check("Quick Edit and the viewer share a row",
    single.includes("fb-side-actions-tight"), true);

// -- the order of the panel ---------------------------------------------------
//
// Everything DONE to the picture is at the top, in two rows: the two ways back
// to the image, then the split and the two rendering buttons. Copy and Apply
// used to be the last thing in the panel with the scale bar and the captions
// between them, which put the two halves of "make these eight panels match" as
// far apart as the column allows.
const order = (needle) => single.indexOf(needle);
check("the rendering buttons are above the scale bar",
    order("copy_rendering") < order("fb_image_mpp"), true);
check("and the split is on their row, not on its own",
    single.slice(order("split_with_composite"), order("copy_rendering"))
        .includes("fb-side-actions"), false);
// The panel's own A/B/C is the one thing here that is set once when the figure
// is laid out and then never touched, so it is last and behind a fold.
check("the panel's letter is the last thing in the panel",
    order('data-fold="panel_label"') > order("fb_image_new_label"), true);

// Four permanent lines of prose explaining two buttons, in a panel whose every
// other row is controls. The sentence is worth having and worth being asked for.
check("the rendering note is not printed unasked",
    single.includes("channel colors and contrast"), true);   // ...in the tooltip
check("but not as a paragraph",
    /<p class="fb-side-note">[\s\S]{0,40}Composite replaces/.test(single), false);
// All three verbs on that row, not just the two that share a clipboard.
// "Composite" is the one whose name says least about what it does.
check("and it says what all three buttons do",
    ["Composite replaces", "Copy and Apply"].every((n) => single.includes(n)), true);

const helped = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    __act(panel, "rendering_help");
    return panel.root.innerHTML;
`);
check("pressing the ? prints it",
    /<p class="fb-side-note">[\s\S]{0,40}Composite replaces/.test(helped), true);

const opened = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.openSections.add("panel_label");
    panel.render();
    return panel.root.innerHTML;
`);
check("opening the fold builds its body", opened.includes("fb_image_label"), true);
// Numbering is the whole figure's and lives under the page menu now. A
// document-wide setting inside one panel's own section looked like a property
// of that panel, and which panel was selected decided nothing about it.
check("numbering is not a panel property", opened.includes("label_style"), false);

const several = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b"),
                            pnl_c: __panelRecord("pnl_c") });
    panel.update(["pnl_a", "pnl_b", "pnl_c"]);
    return panel.root.innerHTML;
`);
// Quick Edit edits one view, the viewer opens one view, a split replaces one
// panel, and a letter is one panel's. None has a reading for three, and a
// button that quietly picked the first is the failure this avoids.
check("three panels lose the one-panel actions",
    ["quick_edit", 'data-fold="panel_label"'].some((n) => several.includes(n)), false);
// These do have a reading for three, and it is the reason the panel exists.
check("and keep everything that applies to all of them",
    ["fb_image_bar_len", "fb_image_cb_ticks", "fb_image_new_label", "apply_rendering"]
        .every((needle) => several.includes(needle)), true);
// The panel used to end the Add row with "Added to all 3 selected images", and
// the Channels preset with a paragraph explaining what it was about to do.
// Both were removed: the header already says "3 images", every other control in
// the panel applies to all of them without saying so, and a note that appears
// under one dropdown value moves the row below it every time the value changes.
check("and no longer narrate what the whole panel already does",
    several.includes("Added to all"), false);

const uncalibrated = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") }, {},
        { src_1: { source_id: "src_1", kind: "plexora_project", datasource: "demo",
                   pixel_size: null } });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
// A scale bar drawn from an assumed pixel size is wrong and looks exactly like
// one that is right. So the panel says there is none, offers the number, and
// falls back to the honest alternative -- a bar measured in image pixels, which
// is a true statement about the picture where silence was not an answer at all.
check("an image with no pixel size says so",
    uncalibrated.includes("No pixel size recorded"), true);
check("and the field stands empty rather than guessing",
    uncalibrated.includes('placeholder="NA"'), true);
// The panel used to tell the user to go and set the unit to px themselves. It
// does it for them: an uncalibrated bar IS a pixel bar, in all three of the
// places that decide how long it is, so the unit button reads px whatever the
// panel has on file.
check("and the unit reads px without being asked",
    /data-choice="scalebar_unit"[^>]*data-value="px"/.test(uncalibrated), true);
check("and the note says what supplying one would change",
    uncalibrated.includes("press Update"), true);

const mixed = run(`
    const a = __panelRecord("pnl_a");
    const b = Object.assign(__panelRecord("pnl_b"), { source_id: "src_2" });
    const panel = __build({ pnl_a: a, pnl_b: b }, {}, {
        src_1: { source_id: "src_1", kind: "plexora_project", datasource: "d1",
                 pixel_size: { value: 0.5, unit: "µm" } },
        src_2: { source_id: "src_2", kind: "plexora_project", datasource: "d2",
                 pixel_size: { value: 0.25, unit: "µm" } },
    });
    panel.update(["pnl_a", "pnl_b"]);
    return panel.root.innerHTML;
`);
// Two magnifications in one row is ordinary, and one number would describe
// neither. Typing over "Mixed" and pressing Update sets them both, which is the
// only thing anybody wants from that state.
check("a selection at two magnifications says so",
    mixed.includes('placeholder="Mixed"'), true);

const notEditable = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") }, {},
        { src_1: { source_id: "src_1", kind: "imported_asset", asset_id: "ast_1" } });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
check("an imported image cannot be quick-edited",
    /data-act="quick_edit"[^>]*disabled/.test(notEditable), true);
check("nor opened in the viewer",
    /data-act="edit"[^>]*disabled/.test(notEditable), true);

// The bug this section of the registry closed. The source is a real project
// with a real datasource, so every "can this be reopened" test that looks only
// at the source record says yes -- and Quick Edit fell through to "open it in
// the viewer instead", which navigates the user off their figure to a
// datasource whose image is not there. The panel asks the registry now, and the
// registry asks the status.
const missingSource = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") }, {}, null,
        { src_1: { status: "missing" } });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
check("a panel whose image has gone cannot be quick-edited either",
    /data-act="quick_edit"[^>]*disabled/.test(missingSource), true);
check("and says why", missingSource.includes("no longer references"), true);

// -- what it commits ---------------------------------------------------------

const scalebar = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b"),
                            pnl_c: __panelRecord("pnl_c") });
    panel.update(["pnl_a", "pnl_b", "pnl_c"]);
    __act(panel, "scalebar_visible");
`);
// ONE call carrying three panels, not three calls. Three commits would be
// three presses of Ctrl+Z with the row part-way through in between.
check("a scale bar across three panels is one change", scalebar.length, 1);
check("and it carries all three",
    scalebar[0].payload.map((entry) => entry.panel_id), ["pnl_a", "pnl_b", "pnl_c"]);
check("with the bar switched on for each",
    scalebar[0].payload.every((entry) => entry.changes.scalebar.visible === true), true);

// The control says what the STATE is -- an open eye on a bar that is showing --
// and the tooltip says what pressing it does. It was a button reading "Hide",
// which had to be the other way round and was the widest thing in a row of five
// controls to say it.
const toggledBack = acted(`
    const on = __panelRecord("pnl_a");
    on.scalebar = { ...on.scalebar, visible: true };
    const panel = __build({ pnl_a: on });
    panel.update(["pnl_a"]);
    globalThis.__word = panel.root.innerHTML.includes('fa-eye"');
    __act(panel, "scalebar_visible");
`);
check("a bar that is showing draws an open eye",
    run("return globalThis.__word;"), true);
check("and pressing it hides the bar",
    toggledBack[0].payload[0].changes.scalebar.visible, false);

// The caption beside the bar has its own eye, and it writes `label` -- NOT
// `visible`. It was a checkbox reading "Label", which said the same thing in a
// different vocabulary from the control directly above it and cost the row the
// width of the word. Two eyes in the same column is what says the caption
// belongs to the bar.
const caption = acted(`
    const on = __panelRecord("pnl_a");
    on.scalebar = { ...on.scalebar, label: true };
    const panel = __build({ pnl_a: on });
    panel.update(["pnl_a"]);
    __act(panel, "scalebar_label");
`);
check("the caption's eye turns the caption off",
    caption[0].payload[0].changes.scalebar.label, false);
check("and leaves the bar itself alone",
    caption[0].payload[0].changes.scalebar.visible,
    run("return __panelRecord('pnl_a').scalebar.visible;"));

// The unit is drawn INSIDE the length field, where "mm" and "pt" are printed on
// the fields either side of it. A fifth box on that row does not fit, and the
// suffix is a real FigureChoiceField button rather than a look-alike -- one
// popover, one open-state key, one list of the units.
const lengthRow = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;`);
check("the unit picker is drawn as a suffix",
    /class="[^"]*fb-choice-suffix[^"]*"[\s\S]{0,80}data-choice="scalebar_unit"/
        .test(lengthRow), true);
check("and it is inside the length field, not beside it",
    lengthRow.indexOf("fb-input-unit-wide") >= 0
    && lengthRow.indexOf("fb-choice-suffix")
        > lengthRow.indexOf("fb-input-unit-wide"), true);

// Numbering used to be a select in this panel, writing to the document through
// `onSettingsChange`. It is the FIGURE's -- every label on every page is drawn
// from it -- so it is under the page menu now, with the other document
// settings, and this panel neither renders it nor has a handler for it.
// The handler is still on the stub, deliberately: what is being checked is that
// this panel does not reach for it, not that nobody has one.
const numberingGone = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.changed({ target: { dataset: { field: "label_style" }, value: "a" } });
`);
check("the panel writes no document settings", numberingGone, []);

const typedLabel = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.changed({ target: { dataset: { field: "label" }, value: "A'" } });
`);
// Typing a label makes it the user's, so it stops renumbering when the page is
// rearranged. That is the whole difference between the two.
check("a typed label stops being automatic",
    typedLabel[0].payload.changes.label, { text: "A'", auto: false, visible: true });

const letterAcrossTwo = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    panel.changed({ target: { dataset: { field: "label" }, value: "A" } });
`);
// Two panels have no one letter, and writing it to the first is the silent
// failure a control that is absent cannot have.
check("a panel letter is not applied to a multi-selection", letterAcrossTwo, []);

const clicks = acted(`
    globalThis.__armed = true;
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    __act(panel, "apply_rendering");
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
    panel.choicePicked("scalebar_unit", "mm");
    panel.state.document.panels.pnl_a.scalebar.unit = "mm";
    panel.changed({ target: { dataset: { field: "scalebar_length" }, value: "0.5" } });
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

// A PIXEL bar has no microns at all. It is stored in its own field, so a figure
// that is calibrated later still has the physical length somebody typed, and a
// figure that is not still has the pixel one.
const pixels = acted(`
    const px = __panelRecord("pnl_a");
    px.scalebar = { ...px.scalebar, unit: "px", target_um: 250 };
    const panel = __build({ pnl_a: px });
    panel.update(["pnl_a"]);
    panel.changed({ target: { dataset: { field: "scalebar_length" }, value: "500" } });
`);
check("a length typed against a pixel bar lands in target_px",
    pixels[0].payload[0].changes.scalebar.target_px, 500);
check("and the physical length it also has is untouched",
    pixels[0].payload[0].changes.scalebar.target_um, 250);

const autoLength = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    return panel.lengthField(panel.panels);
`);
// 512 px at 0.5 µm/px is a 256 µm field; a quarter of it, snapped down, is 50.
// The field shows the number the bar is ACTUALLY going to be rather than the
// word "Auto", because a user comparing two panels needs to know which.
check("an automatic length shows the number it will be",
    autoLength, { value: "", placeholder: "50" });

const pixelAuto = run(`
    const px = __panelRecord("pnl_a");
    px.scalebar = { ...px.scalebar, unit: "px" };
    const panel = __build({ pnl_a: px }, {},
        { src_1: { source_id: "src_1", kind: "plexora_project", datasource: "demo",
                   pixel_size: null } });
    panel.update(["pnl_a"]);
    return panel.lengthField(panel.panels);
`);
// A quarter of 512 px, snapped down: 100. No calibration is needed for this
// one, which is the entire point of it.
check("and a pixel bar needs no calibration to have one",
    pixelAuto, { value: "", placeholder: "100" });

// -- where the furniture sits ------------------------------------------------

const anchored = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    panel.choicePicked("scalebar_position", "top_left");
    panel.choicePicked("colorbar_position", "middle_right");
`);
check("a corner picked for the scale bar reaches every selected panel",
    anchored[0].payload.map((entry) => [entry.panel_id, entry.changes.scalebar.position]),
    [["pnl_a", "top_left"], ["pnl_b", "top_left"]]);
check("and the colour bar has its own corner",
    anchored[1].payload[0].changes.colorbar.position, "middle_right");

// The keypad is behind a button now -- 78px and three rows tall, five times down
// one column, was most of the panel's height spent on a choice made once. What
// the panel emits is the button; the grid is the popover's.
check("the corner is a button, not an inline keypad",
    single.includes('data-choice="scalebar_position"')
        && !single.includes('data-anchors="scalebar"'), true);
check("and the popover it opens is the nine-cell grid",
    FigureChoiceFieldGrid(), true);
function FigureChoiceFieldGrid() {
    const markup = run(`
        return FigureChoiceField.markup({
            layout: "grid", value: "top_left",
            options: FigureChoiceField.anchorOptions() });
    `);
    return markup.includes('role="radiogroup"')
        && (markup.match(/role="radio"/g) || []).length === 9
        && markup.includes('aria-checked="true"')
        && !markup.includes("aria-pressed");
}

// -- the pixel size ----------------------------------------------------------

typed = "0.325";
const calibrated = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    __act(panel, "pixel_size");
`);
typed = "";
// It used to write only to sources that had NO calibration, which made the field
// useless for the case that matters most: a pixel size the file states, and
// states wrongly. Every bar in the figure is derived from it.
check("Update overwrites the calibration that is there",
    calibrated[0].payload.ids, ["src_1"]);
check("with the number in the box", calibrated[0].payload.value, 0.325);
// The panels ride along so that a bar currently measured in pixels can switch to
// microns in the SAME commit -- one thing the user did, one press of Ctrl+Z.
check("and the panels ride along so their bars can follow",
    calibrated[0].payload.panelIds, ["pnl_a", "pnl_b"]);

// -- the labels on the selection ---------------------------------------------

const added = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    panel.changed({ target: { dataset: { field: "new_label_text" }, value: " Tumor " } });
    __act(panel, "add_label");
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
    __act(panel, "add_label");
`);
check("an empty caption adds nothing", blank, []);

// -- the presets -------------------------------------------------------------
//
// A preset is per-PANEL by construction, which is the whole reason it is not
// just text put in the box for the user: "Channels" on a row of single-channel
// panels writes a different word on each, in its own colour, in one gesture.

const channelLabels = acted(`
    const two = __panelRecord("pnl_b");
    two.scene.channels = [
        { key: "ch_1", fullname_at_capture: "SOX10",
          color: { r: 0, g: 255, b: 0 }, window: [0, 1000], visible: true },
        { key: "ch_2", fullname_at_capture: "NGFR",
          color: { r: 255, g: 0, b: 0 }, window: [0, 1000], visible: true },
        { key: "ch_3", fullname_at_capture: "Off",
          color: { r: 255, g: 255, b: 255 }, window: [0, 1000], visible: false },
    ];
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: two });
    panel.update(["pnl_a", "pnl_b"]);
    panel.choicePicked("new_label_preset", "channels");
    __act(panel, "add_label");
`);
check("the channels preset is still one commit", channelLabels.length, 1);
check("and writes each panel's OWN channels",
    channelLabels[0].payload.map((entry) =>
        entry.changes.labels.map((label) => label.text)),
    [["DNA"], ["SOX10", "NGFR"]]);
// The channel's display colour, so a split row reads as its own legend without
// one being drawn. A legend was a caption per channel that could only ever be
// white and top-left; this is the same information, editable.
check("each in that channel's own colour",
    channelLabels[0].payload[1].changes.labels.map((label) => label.color),
    ["#00ff00", "#ff0000"]);
// A channel that is switched off is not in the picture, so naming it would be a
// caption for something the reader cannot see.
check("and a hidden channel is not named",
    channelLabels[0].payload[1].changes.labels.length, 2);

const nameLabels = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.choicePicked("new_label_preset", "image_name");
    __act(panel, "add_label");
`);
check("the image-name preset uses the source's display name",
    nameLabels[0].payload[0].changes.labels[0].text, "Slide 7");

// -- the field that is also the picker ---------------------------------------
//
// It was a box, a chevron beside it, and a list whose first row was "Text you
// type" -- three controls for one answer, one of which existed only to undo the
// other two. The box IS the picker now: clicking it offers what the image can
// supply, and typing in it is the un-choosing.

check("the presets are the two the image can supply, and no third",
    run("return FigureImagePanel.PRESETS.map((entry) => entry.value);"),
    ["image_name", "channels"]);

const labelRow = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
check("the text box is itself the preset picker",
    /<input[^>]*id="fb_image_new_label"[^>]*data-choice="new_label_preset"/
        .test(labelRow.replace(/\s+/g, " ")), true);
check("and nothing else on the panel offers the same list",
    (labelRow.match(/data-choice="new_label_preset"/g) || []).length, 1);
// A word set the width of the column it was the only member of, and it was the
// row's one verb. A symbol keeps the verb and gives the width back.
check("Add is a symbol",
    /data-act="add_label"[\s\S]{0,220}fa-plus/.test(labelRow), true);
check("and not a word as well", labelRow.includes(">Add</button>"), false);

const armedRow = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.choicePicked("new_label_preset", "channels");
    return panel.root.innerHTML;
`);
check("an armed preset shows its name where the typing would be",
    /id="fb_image_new_label"[\s\S]*?value="Channels"/.test(armedRow), true);
check("and the field says the words are the image's, not this box's",
    armedRow.includes("is-preset"), true);

const disarmed = run(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a") });
    panel.update(["pnl_a"]);
    panel.choicePicked("new_label_preset", "channels");
    const armed = panel.draft.preset;
    panel.changed({ target: { dataset: { field: "new_label_text" },
                              value: "Tumor" } });
    return [armed, panel.draft.preset, panel.draft.text];
`);
check("picking a preset arms it", disarmed[0], "channels");
check("and typing is what lets it go again",
    [disarmed[1], disarmed[2]], ["", "Tumor"]);

// A location button already IS a picture of the answer. The chevron beside the
// arrow read as a second arrow pointing somewhere else -- five of them down the
// panel is ten arrows for five answers. The unit keeps its one: "µm" closed is
// one symbol out of five and nothing else about it says so.
const wells = labelRow.match(/<button[^>]*fb-choice-well[\s\S]*?<\/button>/g) || [];
check("the panel draws location buttons", wells.length > 0, true);
check("and none of them draws a caret",
    wells.filter((html) => html.includes("fb-choice-grid")
                        && html.includes("fb-choice-caret")).length, 0);
check("while the unit picker keeps its own",
    wells.some((html) => html.includes("fb-choice-suffix")
                      && html.includes("fb-choice-caret")), true);

// -- the merged list ---------------------------------------------------------

ctx.__labelled = (id, texts) => {
    const record = ctx.__panelRecord(id);
    record.labels = texts.map((text, index) => ({
        label_id: `lbl_${id}_${index}`, text: text, position: "top_left",
        color: "#ffffff", size_pt: null, bold: false, italic: false,
    }));
    return record;
};

const merged = run(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA_2", "SOX10"]),
                            pnl_b: __labelled("pnl_b", ["DNA_2", "NGFR"]) });
    panel.update(["pnl_a", "pnl_b"]);
    return panel.labelRows().map((row) => [row.text, row.refs.length]);
`);
// Six panels' captions interleaved in one list, with nothing saying which row
// belongs to which image, is a list nobody could edit -- which is why this list
// used to be single-panel only. Merged, it is the list anybody wanted.
check("one row per distinct caption across the selection",
    merged, [["DNA_2", 2], ["SOX10", 1], ["NGFR", 1]]);

const renamed = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA_2", "SOX10"]),
                            pnl_b: __labelled("pnl_b", ["DNA_2", "NGFR"]) });
    panel.update(["pnl_a", "pnl_b"]);
    panel.changed({ target: { dataset: { field: "label_text", row: "0" },
                              value: "DNA" } });
`);
check("renaming a shared row is one commit", renamed.length, 1);
check("and touches both panels' copies of it",
    renamed[0].payload.map((entry) =>
        entry.changes.labels.map((label) => label.text)),
    [["DNA", "SOX10"], ["DNA", "NGFR"]]);

// Addressed by ROW INDEX and never by the text it is keyed on: the first
// keystroke of a rename changes that text, and the second would find nothing.
const renamedTwice = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA_2"]) });
    panel.update(["pnl_a"]);
    const type = (value) => panel.changed(
        { target: { dataset: { field: "label_text", row: "0" }, value: value } });
    type("DNA_");
    panel.state.document.panels.pnl_a.labels[0].text = "DNA_";
    type("DNA");
`);
check("a half-typed rename still addresses its own row",
    renamedTwice[1].payload[0].changes.labels[0].text, "DNA");

const rowEdits = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10"]),
                            pnl_b: __labelled("pnl_b", ["DNA"]) });
    panel.update(["pnl_a", "pnl_b"]);
    panel.choicePicked("label_position:0", "bottom_right");
    panel.colourPicked("label_color:1", "#00ff00");
    __act(panel, "label_delete:0");
`);
check("a corner set on a shared row reaches both panels",
    rowEdits[0].payload.map((entry) =>
        entry.changes.labels.filter((l) => l.text === "DNA")[0].position),
    ["bottom_right", "bottom_right"]);
// The second row is on one panel only, so only that panel is written.
check("a row on one panel only writes that panel",
    rowEdits[1].payload.map((entry) => entry.panel_id), ["pnl_a"]);
check("and deleting a shared row deletes it everywhere",
    rowEdits[2].payload.map((entry) =>
        entry.changes.labels.map((label) => label.text)), [["SOX10"], []]);

// -- reordering, which is a drag now -----------------------------------------
//
// It was one cycling button, then two arrows. Both had the same defect: a press
// renumbers the row out from under the pointer, so the control has to be
// re-aimed between presses and moving a row three places is three separate
// readings of the list. A drop says where the row is going once. Dropping DOWN
// lands after the row dropped on, dropping UP lands before it -- which is what
// the marker drawn on that edge promises.

const moved = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10", "NGFR"]) });
    panel.update(["pnl_a"]);
    panel.moveRowTo(0, 2);
`);
check("dropping a row on a lower one lands it after that one",
    moved[0].payload[0].changes.labels.map((label) => label.text),
    ["SOX10", "NGFR", "DNA"]);

const movedUp = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10", "NGFR"]) });
    panel.update(["pnl_a"]);
    panel.moveRowTo(2, 0);
`);
check("and on a higher one lands it before",
    movedUp[0].payload[0].changes.labels.map((label) => label.text),
    ["NGFR", "DNA", "SOX10"]);

// The neighbouring case is what the arrow keys on the handle do, and it has to
// keep meaning "swap with the one next door" or the keyboard route stops
// matching the pointer one.
const stepped = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10", "NGFR"]) });
    panel.update(["pnl_a"]);
    panel.moveRowTo(0, 1);
`);
check("a one-place move is still a swap",
    stepped[0].payload[0].changes.labels.map((label) => label.text),
    ["SOX10", "DNA", "NGFR"]);

const nowhere = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10"]) });
    panel.update(["pnl_a"]);
    panel.moveRowTo(1, 2);
    panel.moveRowTo(0, -1);
    panel.moveRowTo(1, 1);
`);
check("past either end, and onto itself, is not a move at all", nowhere.length, 0);

// A merged row stands for one caption in EACH panel, so the move happens once
// per panel against that panel's own list -- and all of it is one commit.
const bothPanels = acted(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10"]),
                            pnl_b: __labelled("pnl_b", ["DNA", "SOX10"]) });
    panel.update(["pnl_a", "pnl_b"]);
    panel.moveRowTo(0, 1);
`);
check("a shared row moves on every panel carrying it", bothPanels.length, 1);
check("each against its own list",
    bothPanels[0].payload.map((entry) =>
        entry.changes.labels.map((label) => label.text)),
    [["SOX10", "DNA"], ["SOX10", "DNA"]]);

const follows = run(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10", "NGFR"]) });
    panel.update(["pnl_a"]);
    panel.moveRowTo(0, 2);
    return panel.pendingFocus;
`);
check("and the keyboard is sent to where the row landed",
    follows, '[data-grip="2"]');

const panelSource = readFileSync(join(STATIC, "figureImagePanel.js"), "utf8");
const ends = run(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10"]) });
    panel.update(["pnl_a"]);
    return panel.root.innerHTML;
`);
check("a row is dragged by a handle", ends.includes('data-grip="0"'), true);
// SortableJS, which core already uses to restack its tool cards. The
// hand-rolled HTML5 version did not reorder anything: `dragstart` has to
// survive every ancestor's pointer handling, and this panel floats over a
// canvas that binds pointerdown for panning, marquee and tool arming.
check("and reordering runs on the library core already uses",
    /window\.Sortable/.test(panelSource) && /handle: "\.fb-grip"/.test(panelSource),
    true);
check("bound again on every render, since the list is rebuilt on every render",
    /this\.restore\(focus\);\s*\n\s*this\.bindSorting\(\);/.test(panelSource), true);
check("and never left behind", /this\.sorter\?\.destroy\(\)/.test(panelSource), true);
// Two columns the row gets back, and two controls that no longer have to be
// re-aimed between presses.
check("and the two arrows are gone",
    ["label_up", "label_down"].some((act) => ends.includes(act)), false);

const listUi = run(`
    const panel = __build({ pnl_a: __labelled("pnl_a", ["DNA", "SOX10"]),
                            pnl_b: __labelled("pnl_b", ["DNA"]) });
    panel.update(["pnl_a", "pnl_b"]);
    return panel.root.innerHTML;
`);
check("every row gets a delete", listUi.includes('data-act="label_delete:0"'), true);
check("and a row is one line now",
    (listUi.match(/fb-label-row/g) || []).length, 2);
// How many of the selection carry it -- drawn only when it is neither all of
// them nor one of them, which are the two cases a number would be noise for.
check("a row on some of the selection says how many",
    listUi.includes("fb-label-count"), true);

// -- the colour bar ----------------------------------------------------------

const colourBar = acted(`
    const panel = __build({ pnl_a: __panelRecord("pnl_a"), pnl_b: __panelRecord("pnl_b") });
    panel.update(["pnl_a", "pnl_b"]);
    __act(panel, "colorbar_visible");
    panel.changed({ target: { dataset: { field: "colorbar_ticks" }, value: "5" } });
`);
check("a colour bar switched on reaches every selected panel",
    colourBar[0].payload.every((entry) => entry.changes.colorbar.visible === true), true);
check("and so does its tick count",
    colourBar[1].payload.map((entry) => entry.changes.colorbar.ticks), [5, 5]);

// -- typing a decimal ---------------------------------------------------------
//
// Every numeric box here is `type="text" inputmode="decimal"`. A number input
// reports `selectionStart` as null, this panel rebuilds itself on every
// keystroke, and `focused()` therefore could not put the caret back -- so typing
// "1.25" into a millimetre field landed as "5".

check("no numeric field is a number input",
    single.includes('type="number"'), false);
check("and they all ask for the decimal keypad",
    (single.match(/inputmode="decimal"/g) || []).length >= 3, true);

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
