/**
 * The line sidebar, driven against a stub DOM.
 *
 * Everything here is a decision the panel makes about a document, and every one
 * of them ships green and is wrong somewhere a user only meets later:
 *
 *   * a panel that claims a mixed selection puts one line's settings in front
 *     of somebody who selected five things, and applies them to whichever one
 *     it happened to pick;
 *
 *   * a panel that ignores stored `arrow` annotations leaves every arrow in
 *     every existing figure with no way to change its head -- the type is
 *     superseded, not gone;
 *
 *   * a head size committed on `input` rather than on `change` puts "1", "1.2"
 *     and "12" in the undo history for one number typed once;
 *
 *   * "Auto" that commits anything other than the stored ZERO is a second way
 *     to say a value the schema already has a value for, and the two disagree
 *     the moment one of them is read.
 *
 * Run directly:
 *   node tests/js/figure_line_panel_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
// figureCanvas.js is here for `isStrokeType`, which is the one definition of
// "this annotation is a stroke" and is what the panel filters a selection with.
const SCRIPTS = ["figureSchema.js", "figureRichText.js", "figureShapeGeometry.js",
                 "figureShapeDefs.js", "figureStrokeGeometry.js", "figureLineDefs.js",
                 "figureShapeDrawing.js", "figurePointEditor.js", "figureConfirm.js",
                 "figureColorField.js", "figureCanvas.js", "figureLinePanel.js"];

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
            const id = selector.replace("#", "").replace("[", "").replace("]", "");
            if (!this.innerHTML.includes(id)) return null;
            return { value: "", textContent: "", focus() {}, setSelectionRange() {} };
        },
    };
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, parseFloat, isNaN, RegExp,
    isFinite,
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

const applied = [];
ctx.__record = (id, changes) => applied.push({ id, changes });

runInContext(`
    globalThis.__build = function (annotations) {
        const document_ = { annotations: annotations };
        const panel = new FigureLinePanel({
            root: __root(),
            canvas: null,
            state: { document: document_ },
            onStyle: (id, changes) => __record(id, changes),
        });
        return panel;
    };
    globalThis.__line = function (id, type, style) {
        return {
            annotation_id: id, type: type, page_id: "pg_1",
            geometry: { x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 0, rotation: 0 },
            style: Object.assign({
                color: "#000000", fill: "", line_width_pt: 0.75, opacity: 1,
                line_style: "solid", start_head: "none", end_head: "none",
                head_size_pt: 0, edge: "standard",
            }, style || {}),
        };
    };
`, ctx);

const run = (source) => runInContext(`(() => { ${source} })()`, ctx);

// -- what it claims ---------------------------------------------------------

const claims = run(`
    const line = __line("ann_1", "line");
    const arrow = __line("ann_2", "arrow", { end_head: "open" });
    const text = { annotation_id: "ann_3", type: "text", style: {} };
    const panel = __build({ ann_1: line, ann_2: arrow, ann_3: text });
    const asks = (ids) => { panel.update(ids); return panel.wants; };
    return {
        line: asks(["ann_1"]),
        arrow: asks(["ann_2"]),
        text: asks(["ann_3"]),
        none: asks([]),
        // Single selection, like the shape panel: two lines with different
        // heads have no one answer to put in the row, and showing one of them
        // is showing the wrong one half the time.
        both: asks(["ann_1", "ann_2"]),
    };
`);
check("a line gets the panel", claims.line, true);
// The stored type is superseded, not gone. Every arrow already on a page has to
// be editable with the controls that would make one now.
check("so does a stored arrow", claims.arrow, true);
check("a caption does not", claims.text, false);
check("nor does an empty selection", claims.none, false);
check("nor does a mixed one", claims.both, false);

const dismissal = run(`
    const panel = __build({ ann_1: __line("ann_1", "line"),
                            ann_2: __line("ann_2", "line") });
    panel.update(["ann_1"]);
    panel.dismissed = true;
    const shut = panel.wants;
    panel.update(["ann_2"]);
    return { shut: shut, nextOne: panel.wants };
`);
check("shutting the panel shuts it", dismissal.shut, false);
// "Not for this one", not "never again" -- otherwise closing it once turns the
// panel off for the rest of the session with nothing that says so.
check("but only for the line it was shut on", dismissal.nextOne, true);

// -- what it draws ----------------------------------------------------------

const markup = run(`
    const panel = __build({ ann_1: __line("ann_1", "line",
        { line_style: "dashed", start_head: "bar", end_head: "diamond",
          head_size_pt: 14, edge: "fade_end", opacity: 0.4 }) });
    panel.update(["ann_1"]);
    return panel.root.innerHTML;
`);
// A panel that rendered nothing at all would pass every `includes` below by
// failing all of them in the same direction, so the length is checked first.
check("the panel rendered something", markup.length > 500, true);
for (const needle of ["fb_line_stroke", "fb_line_width", "fb_line_head",
                      "fb_line_edge", "fb_line_opacity"]) {
    check(`the panel has a ${needle} row`, markup.includes(needle), true);
}
check("every head style is offered at each end",
    (markup.match(/data-pick="start_head"/g) || []).length,
    runInContext("FigureStrokeGeometry.HEAD_STYLES.length", ctx));
check("and every dash",
    (markup.match(/data-pick="line_style"/g) || []).length,
    runInContext("FigureStrokeGeometry.LINE_STYLES.length", ctx));
check("every edge is in the select",
    (markup.match(/<option value=/g) || []).length,
    runInContext("FigureLinePanel.EDGES.length", ctx));
check("the stored values are the pressed ones",
    ['data-pick="start_head" data-value="bar"',
     'data-pick="end_head" data-value="diamond"'].every(
        (attributes) => markup.includes(attributes)), true);
check("the edge select is on the stored edge",
    markup.includes('value="fade_end" selected'), true);
check("opacity reads as a percentage", markup.includes("40%"), true);
// FontAwesome walks the document once at boot, so a span injected into a panel
// rendered afterwards never becomes anything. Generated icons are SVG.
check("the head cells are inline SVG", markup.includes("<svg"), true);

const auto = run(`
    const panel = __build({ ann_1: __line("ann_1", "line") });
    panel.update(["ann_1"]);
    return panel.root.innerHTML;
`);
// Zero is not "no head": it is "size the head from the pen", which is what
// every arrow drawn before this control existed stores by construction. So the
// field is EMPTY with a placeholder rather than reading "0", which would look
// like a head that had been switched off.
check("an unset head size offers Auto", auto.includes('placeholder="Auto"'), true);
check("and shows nothing rather than a zero",
    /id="fb_line_head"[^>]*value=""/.test(auto), true);

// -- what it commits --------------------------------------------------------

function commits(source) {
    applied.length = 0;
    run(source);
    return applied.map((entry) => entry.changes.style);
}

check("picking a head commits that one key",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line") });
        panel.update(["ann_1"]);
        panel.clicked({ target: { closest: (selector) =>
            selector === "[data-pick]"
                ? { dataset: { pick: "end_head", value: "filled" } } : null } });
    `), [{ end_head: "filled" }]);

check("choosing an edge commits that one key",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line") });
        panel.update(["ann_1"]);
        panel.changed({ type: "change",
            target: { dataset: { edge: "1" }, value: "taper_both" } });
    `), [{ edge: "taper_both" }]);

// On `change` -- the field being left, or Enter -- and never on `input`. A size
// is typed a digit at a time and every prefix of it is a valid number, so
// committing keystrokes leaves three entries in the undo history for one number.
check("a head size typed a digit at a time commits once",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line") });
        panel.update(["ann_1"]);
        const field = { dataset: { head: "1" }, value: "1" };
        panel.changed({ type: "input", target: field });
        field.value = "12";
        panel.changed({ type: "input", target: field });
        panel.changed({ type: "change", target: field });
    `), [{ head_size_pt: 12 }]);

check("blank means Auto, which is the stored zero",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line", { head_size_pt: 9 }) });
        panel.update(["ann_1"]);
        panel.changed({ type: "change", target: { dataset: { head: "1" }, value: "  " } });
    `), [{ head_size_pt: 0 }]);

check("and so does the word itself",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line", { head_size_pt: 9 }) });
        panel.update(["ann_1"]);
        panel.changed({ type: "change", target: { dataset: { head: "1" }, value: "Auto" } });
    `), [{ head_size_pt: 0 }]);

check("a head size past the schema's ceiling is clamped, not refused",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line") });
        panel.update(["ann_1"]);
        panel.changed({ type: "change", target: { dataset: { head: "1" }, value: "5000" } });
    `), [{ head_size_pt: runInContext("FigureStrokeGeometry.MAX_HEAD_SIZE_PT", ctx) }]);

// A range fires `input` per pixel of travel; one commit per pixel is a hundred
// entries in the undo history and a hundred queued writes for one drag.
check("dragging opacity commits only on release",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line") });
        panel.update(["ann_1"]);
        const field = { dataset: { opacity: "1" }, value: "60" };
        panel.changed({ type: "input", target: field });
        panel.changed({ type: "change", target: field });
    `), [{ opacity: 0.6 }]);

// Off remembers what it was so on can put it back -- which is the whole reason
// anyone reaches for the toggle rather than for undo. A stroke of none is a
// width of zero, which was already legal; there is no second key for it.
check("switching the stroke off and on again keeps the width",
    commits(`
        const panel = __build({ ann_1: __line("ann_1", "line", { line_width_pt: 3 }) });
        panel.update(["ann_1"]);
        panel.changed({ type: "change", target: { dataset: { toggle: "stroke" }, checked: false } });
        panel.annotation.style.line_width_pt = 0;
        panel.changed({ type: "change", target: { dataset: { toggle: "stroke" }, checked: true } });
    `), [{ line_width_pt: 0 }, { line_width_pt: 3 }]);

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
