/**
 * The classifier's review keys and the dataset chip's N, on one page.
 *
 * Reviewing uncertain cells (gatingClassifier.js) answers P positive, N
 * negative, S skip, Esc stop -- the panel's own buttons say so. The dataset
 * chip (views/datasetNav.js) walks to the next sample on a bare N, and stands
 * down only when the key was already taken (`event.defaultPrevented`). Both
 * listen on `document`. The chip mounts when /datasets answers, at page load;
 * the classifier's keys are added each time the panel is shown, which is
 * later whenever the tool is opened from the menu. So N during a review must
 * be the review's, whichever was added first: the page must not walk away
 * mid-review.
 *
 * Both real files run in one vm context over a document whose keydown
 * dispatch keeps the browser's order: capture listeners first, then bubbling
 * ones, each in the order they were added (the key is pressed on the page,
 * not on `document` itself).
 *
 * Run directly: `node tests/js/classifier_review_keys_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const NAV = join(REPO, "plexora", "client", "src", "js", "views", "datasetNav.js");
const CLASSIFIER = join(REPO, "plexora", "plugins", "gating", "static", "gatingClassifier.js");

function element(tag = "div") {
    return {
        tagName: tag.toUpperCase(),
        className: "",
        dataset: {},
        children: [],
        attributes: {},
        style: {},
        hidden: false,
        disabled: false,
        title: "",
        textContent: "",
        parentNode: null,
        appendChild(child) { this.children.push(child); return child; },
        removeChild(child) { this.children = this.children.filter((c) => c !== child); },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        addEventListener() {},
        removeEventListener() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
    };
}

/** One page: the dataset chip mounted, the classifier class loaded. */
async function page() {
    const listeners = [];
    const navigated = [];
    const wrapper = element("div");
    const document = {
        readyState: "complete",
        activeElement: null,
        body: element("body"),
        getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
        querySelector: () => null,
        createElement: element,
        addEventListener(type, fn, options) {
            const capture = options === true || Boolean(options && options.capture);
            if (listeners.some((l) => l.type === type && l.fn === fn && l.capture === capture)) return;
            listeners.push({ type, fn, capture });
        },
        removeEventListener(type, fn, options) {
            const capture = options === true || Boolean(options && options.capture);
            const at = listeners.findIndex((l) => l.type === type && l.fn === fn && l.capture === capture);
            if (at !== -1) listeners.splice(at, 1);
        },
    };
    const context = {
        console: { log() {}, error() {}, warn() {} },
        JSON, Date, Set, Map, Object, Array, Number, String, Boolean, Promise, Math,
        Uint8Array, Int8Array, Float32Array,
        encodeURIComponent,
        document,
        fetch: async () => ({
            ok: true,
            json: async () => ({ success: true, datasets: [{ id: "ds1", name: "Cohort",
                                                             projects: ["a", "b", "c"] }] }),
        }),
        plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
        setTimeout: () => 0,
        clearTimeout: () => {},
        requestAnimationFrame: (fn) => { fn(); return 1; },
        CSVGatingList: { events: { SELECTION_CHANGED: "selection" } },
    };
    context.window = context;
    context.addEventListener = () => {};
    context.removeEventListener = () => {};
    context.flaskVariables = { datasource: "b", datasources: ["a", "b", "c"] };
    context.PlexoraCarryOver = { stash: () => true };
    context.PlexoraToolLoader = { activeTool: () => "gating" };
    context.PlexoraRouter = { go: (href) => navigated.push(href) };
    createContext(context);
    // The chip mounts on load, as on the real page: readyState is complete.
    runInContext(readFileSync(NAV, "utf8"), context);
    runInContext(readFileSync(CLASSIFIER, "utf8")
        + "\nglobalThis.GateClassifierControl = GateClassifierControl;", context);
    // Let /datasets answer, so the chip's keydown is on the document.
    for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
    assert.equal(context.PlexoraDatasetNav.place().next, "c", "the chip has a next sample");

    /** A keydown pressed on the page, dispatched in the browser's order. */
    function press(key) {
        const event = {
            type: "keydown", key, defaultPrevented: false,
            metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
            target: document.body,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() {},
        };
        const keyed = listeners.filter((l) => l.type === "keydown");
        for (const l of keyed.filter((l) => l.capture)) l.fn(event);
        for (const l of keyed.filter((l) => !l.capture)) l.fn(event);
        return event;
    }

    /** The classifier panel, shown, as the gating panel shows it. */
    function classifier() {
        const controller = {
            ctx: { layers: { removeOverlay() {}, repaint() {} }, viewer: {} },
            api: {},
            acceptsKeys: () => true,
        };
        const control = new context.GateClassifierControl(controller);
        control.mode = "classifier";
        control.marker = "CD8";
        const answers = [];
        control.answerReview = (answer) => { answers.push(answer); };
        return { control, answers };
    }

    return { press, classifier, navigated, listeners };
}

const passed = [];
async function check(label, fn) {
    await fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

await check("N during a review labels the cell negative and does not walk to the next sample", async () => {
    const p = await page();
    // The tool opened from the menu after the page loaded: its keys are added
    // after the chip's.
    const { control, answers } = p.classifier();
    control.review = { marker: "CD8", cells: [{ cell_id: 7 }], at: 0, labelled: 0, skipped: 0 };
    control.show();
    p.press("n");
    assert.deepEqual(answers, ["negative"], "the review took N");
    assert.deepEqual(p.navigated, [], "N mid-review walked to " + p.navigated[0]);
});

await check("N during a review still holds after the panel was hidden and shown again", async () => {
    const p = await page();
    const { control, answers } = p.classifier();
    control.show();
    control.hide();
    control.show();
    control.review = { marker: "CD8", cells: [{ cell_id: 7 }], at: 0, labelled: 0, skipped: 0 };
    p.press("N");
    assert.deepEqual(answers, ["negative"]);
    assert.deepEqual(p.navigated, [], "N mid-review walked to " + p.navigated[0]);
});

await check("with no review going, N is the chip's and walks to the next sample", async () => {
    const p = await page();
    const { control, answers } = p.classifier();
    control.show();
    p.press("n");
    assert.deepEqual(answers, []);
    assert.equal(p.navigated.length, 1);
    assert.match(p.navigated[0], /\/c\?tool=gating$/);
});

console.log(`all checks passed (${passed.length})`);
