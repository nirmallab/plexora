/**
 * Names that arrive from outside reach the page as text, never as markup.
 *
 * A channel name comes from an image's own OME-XML or from an uploaded CSV; an
 * annotation's colour and id come from a figure document that can be imported
 * from a file. Any of them reaching `innerHTML` or `outerHTML` unescaped is
 * script in the viewer -- and in the desktop shell that page also holds the
 * native bridge. Each check below feeds one hostile value through a real
 * sink, run from the shipped source, and asserts where it lands:
 *
 *   * the viewer's channel legend builds nodes, so a name is a text node and
 *     no markup is written at all;
 *   * the figure canvas escapes a colour that would otherwise close the
 *     `style` attribute it sits in, for every kind of annotation;
 *   * a selector built from an id keeps that id inside its quoted string, so
 *     it cannot become a second selector -- the canvas's focus and lookup, and
 *     the point editor's `redraw`, which rewrites the matched element's
 *     `outerHTML`.
 *
 * Run directly:  node tests/js/xss_sinks_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");

const PAYLOAD = "<img src=x onerror=alert(1)>";
//: Closes a double-quoted attribute, then opens a tag.
const BREAKOUT = `#000000" onmouseover="alert(1)"><img src=x onerror=alert(1)>`;
//: Closes a quoted attribute selector and starts a second selector.
const HOSTILE_ID = `x"],img[src=x`;

const results = [];
function check(name, fn) {
    fn();
    results.push(name);
    console.log(`ok  ${name}`);
}

// -- a DOM that keeps what is written into it ---------------------------------

/** Every markup string any element was given, so "nothing was parsed" is
 *  something a check can assert. */
const markupWrites = [];

function element(tag) {
    let text = "";
    const node = {
        tagName: String(tag).toUpperCase(),
        className: "",
        style: {},
        children: [],
        append(...nodes) { node.children.push(...nodes); },
        appendChild(child) { node.children.push(child); return child; },
        replaceChildren(...nodes) { node.children = nodes; text = ""; },
        set innerHTML(html) { markupWrites.push(String(html)); node.children = []; text = ""; },
        get innerHTML() { return ""; },
        set textContent(value) { text = String(value); node.children = []; },
        get textContent() { return text + node.children.map((c) => c.textContent).join(""); },
    };
    return node;
}

// -- 1. the viewer's channel legend ------------------------------------------

/** `getActiveLegendChannels` and `updateLegend`, sliced from the real file
 *  and compiled alone: the rest of ImageViewer is not needed to draw a
 *  legend, and a copy of the method would test the copy. */
function legendMethods() {
    const source = readFileSync(join(VIEWS, "imageViewer.js"), "utf8");
    const start = source.indexOf("    getActiveLegendChannels() {");
    const endMarker = 'legend.style.display = active.length ? "flex" : "none";\n    }';
    const end = source.indexOf(endMarker, start);
    assert.ok(start >= 0 && end > start, "updateLegend was not found in imageViewer.js");
    return source.slice(start, end + endMarker.length);
}

check("a channel name reaches the legend as text, and no markup is written", () => {
    const legend = element("div");
    const context = createContext({
        String, Array,
        document: {
            getElementById: (id) => (id === "viewer_channel_legend" ? legend : null),
            createElement: element,
        },
    });
    context.window = context;
    context.window.__plexora = { viewerSidebar: { channelSlots: [
        { enabled: true, name: PAYLOAD, colorHex: BREAKOUT },
        { enabled: true, name: "CD8", colorHex: "#00ff00" },
        { enabled: false, name: "off", colorHex: "#ffffff" },
    ] } };
    const Viewer = runInContext(`(class { ${legendMethods()} })`, context);
    markupWrites.length = 0;
    new Viewer().updateLegend();

    assert.deepEqual(markupWrites, [], "the legend wrote markup");
    assert.equal(legend.children.length, 2);
    const [row] = legend.children;
    assert.equal(row.className, "legend-row");
    const [swatch, name] = row.children;
    assert.equal(name.className, "legend-name");
    assert.equal(name.textContent, PAYLOAD);
    assert.equal(name.children.length, 0, "the name grew child elements");
    // A style PROPERTY, where a browser ignores a value it cannot parse.
    assert.equal(swatch.style.background, BREAKOUT);
    assert.equal(legend.style.display, "flex");
});

// -- 2. the figure canvas -----------------------------------------------------

const SCRIPTS = ["../../../client/src/js/services/viewerScene.js", "figureSchema.js",
                 "figureRichText.js", "figureShapeGeometry.js", "figureShapeDefs.js",
                 "figureStrokeGeometry.js", "figureLineDefs.js", "figureShapeDrawing.js",
                 "figurePointEditor.js", "figureCanvas.js"];

/** A surface that records every selector it is asked for. */
function surface(found) {
    const asked = [];
    return {
        asked,
        style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
        addEventListener() {}, focus() {}, contains: () => false,
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
        innerHTML: "",
        querySelector(selector) { asked.push(selector); return found || null; },
        querySelectorAll() { return []; },
    };
}

function plainElement() {
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
        getElementById: () => null, createElement: () => plainElement(),
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

function hostileAnnotation(type, extra) {
    return {
        annotation_id: HOSTILE_ID, page_id: "pg_1", type, z: 1,
        geometry: { x_mm: 10, y_mm: 10, w_mm: 40, h_mm: 20, rotation: 0 },
        style: {
            color: BREAKOUT, fill: BREAKOUT, opacity: 1, line_width_pt: 1,
            font_family: "Helvetica", font_size_pt: 8, align: "left", valign: "top",
            line_height: 1.2, autofit: false,
            start_head: "none", end_head: "filled", head_size_pt: 0,
            line_style: "solid", edge: "none",
        },
        ...extra,
    };
}

ctx.__annotations = {
    text: hostileAnnotation("text", { rich: { lines: [{ runs: [{ text: PAYLOAD }] }] } }),
    rect: hostileAnnotation("rect"),
    shape: hostileAnnotation("shape", { shape: { preset: "custom", closed: true, nodes: [
        { x: 0, y: 0, type: "corner" }, { x: 1, y: 0, type: "corner" },
        { x: 1, y: 1, type: "corner" }] } }),
    line: hostileAnnotation("arrow"),
};
ctx.__surface = surface;
ctx.__element = plainElement;

runInContext(`
    globalThis.__canvas = function (surfaceEl) {
        const document_ = {
            schema_version: 1, revision: 1, figure_id: "fig_xss", title: "T",
            settings: { label_style: "A", body_size_pt: 8,
                        style: { gutter_mm: 3, font_size_pt: 8, label_size_pt: 10,
                                 title_size_pt: 9, line_width_pt: 0.75,
                                 font_family: "Helvetica", text_color: "#000000",
                                 panel_background: "#000000" } },
            sources: {},
            pages: [{ page_id: "pg_1", name: "Page 1", preset: "a4",
                      orientation: "portrait", size_mm: { w: 210, h: 297 },
                      margins_mm: { top: 10, right: 10, bottom: 10, left: 10 },
                      background: "#ffffff" }],
            panels: {},
            annotations: { [__annotations.shape.annotation_id]: __annotations.shape },
            groups: {},
        };
        const canvas = new FigureCanvas({
            state: {
                document: document_, sourceStatus: {},
                panel: (id) => document_.panels[id] || null,
                source: (id) => document_.sources[id] || null,
                commit: () => Promise.resolve(true),
            },
            api: { previewUrl: () => "preview" },
            figureId: "fig_xss",
            pageEl: __element(),
            surfaceEl: surfaceEl || __surface(),
            guideEl: __element(),
            onSelectionChange: () => {},
        });
        canvas.pageId = "pg_1";
        return canvas;
    };
`, ctx);

const run = (source) => runInContext(`(() => { ${source} })()`, ctx);

/** The tags a markup string opens. A hostile value that escaped its
 *  attribute shows up here as an <img>; one that stayed text does not. */
const tagsIn = (markup) => Array.from(markup.matchAll(/<([a-zA-Z][\w:-]*)/g), (m) => m[1].toLowerCase());

/** Each `name="value"` pair, decoded the way a parser would see it. An
 *  attribute a value broke out to make (`onmouseover`) appears as a name. */
const attributeNames = (markup) => Array.from(
    markup.matchAll(/\s([a-zA-Z_:][\w:.-]*)\s*=\s*(?:"[^"]*"|'[^']*')/g), (m) => m[1].toLowerCase());

check("a colour cannot close the attribute it is written into, on any annotation", () => {
    for (const kind of ["text", "rect", "shape", "line"]) {
        const markup = run(`return __canvas().annotationMarkup(__annotations.${kind});`);
        assert.ok(!tagsIn(markup).includes("img"), `${kind}: an <img> was opened\n${markup}`);
        assert.ok(!attributeNames(markup).some((name) => name.startsWith("on")),
            `${kind}: an event handler attribute appeared\n${markup}`);
        assert.ok(markup.includes("&lt;img src=x onerror=alert(1)&gt;"),
            `${kind}: the value is not in the markup as text\n${markup}`);
    }
});

/** Every `[data-…-id="…"]` in a selector, with its CSS string unescaped. A
 *  selector the id broke out of leaves text these do not account for. */
function attributeSelectors(selector, prefix = "", suffix = "") {
    const pattern = /\[data-(?:panel|annotation)-id="((?:\\.|[^"\\])*)"\]/g;
    const values = [];
    const rest = selector.replace(pattern, (whole, value) => {
        values.push(value.replace(/\\(.)/g, "$1"));
        return "@";
    });
    return { values, rest: rest.split(",").map((part) => part.trim()
        .replace(prefix, "").replace(suffix, "")) };
}

check("a hostile id stays inside its selector when the canvas looks an object up", () => {
    const found = run(`
        const surfaceEl = __surface();
        const canvas = __canvas(surfaceEl);
        canvas.focusObject(${JSON.stringify(HOSTILE_ID)});
        canvas.elementFor({ kind: "panel", id: ${JSON.stringify(HOSTILE_ID)} });
        canvas.elementFor({ kind: "annotation", id: ${JSON.stringify(HOSTILE_ID)} });
        return surfaceEl.asked.slice();
    `);
    assert.equal(found.length, 3);
    const focus = attributeSelectors(found[0]);
    assert.deepEqual(focus.values, [HOSTILE_ID, HOSTILE_ID], found[0]);
    assert.deepEqual(focus.rest, ["@", "@"], found[0]);
    const panel = attributeSelectors(found[1], ".fb-panel");
    assert.deepEqual([panel.values, panel.rest], [[HOSTILE_ID], ["@"]], found[1]);
    const annotation = attributeSelectors(found[2], ".fb-annotation");
    assert.deepEqual([annotation.values, annotation.rest], [[HOSTILE_ID], ["@"]], found[2]);
});

check("the point editor's redraw finds its element by an escaped id and writes escaped markup", () => {
    const outcome = run(`
        let written = null;
        const target = { set outerHTML(html) { written = String(html); } };
        const surfaceEl = __surface(target);
        const canvas = __canvas(surfaceEl);
        const editor = new FigurePointEditor({ canvas });
        editor.annotationId = __annotations.shape.annotation_id;
        editor.closed = true;
        editor.local = [{ x: 0, y: 0, type: "corner" }, { x: 40, y: 0, type: "corner" },
                        { x: 40, y: 20, type: "corner" }];
        editor.redraw();
        return { asked: surfaceEl.asked.slice(), written };
    `);
    assert.equal(outcome.asked.length, 1);
    const looked = attributeSelectors(outcome.asked[0]);
    assert.deepEqual([looked.values, looked.rest], [[HOSTILE_ID], ["@"]], outcome.asked[0]);
    assert.ok(outcome.written, "redraw wrote nothing");
    assert.ok(!tagsIn(outcome.written).includes("img"), outcome.written);
    assert.ok(!attributeNames(outcome.written).some((name) => name.startsWith("on")), outcome.written);
});

check("the selector escape is the browser's CSS.escape wherever the page has one", () => {
    const viaBrowser = run(`
        globalThis.CSS = { escape: (value) => "css(" + value + ")" };
        try { return FigureSchema.cssEscape("pnl_a"); } finally { delete globalThis.CSS; }
    `);
    assert.equal(viaBrowser, "css(pnl_a)");
    // Without one: quotes and backslashes escaped, nothing else touched.
    assert.equal(run(`return FigureSchema.cssEscape('a"b\\\\c');`), 'a\\"b\\\\c');
    assert.equal(run(`return FigureSchema.cssEscape("pnl_a.1:2");`), "pnl_a.1:2");
});

console.log(`${results.length} checks passed`);
