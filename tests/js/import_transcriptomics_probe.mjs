/**
 * The import screens name a 10x run only on a licence that imports one.
 *
 * Without `transcriptomics` the server does not detect a Xenium, Visium or
 * Visium HD run at all (import_proposal._licensed), so the screens that say
 * what can be picked do not offer one either: no card, no example, no chip.
 * Absent -- not greyed, not explained. With the grant they are all there.
 *
 *   - importHelp.js: the Formats, Overview and Examples tabs.
 *   - browsePicker.js: the "sample" examples under File | Folder.
 *
 * Run directly:  node tests/js/import_transcriptomics_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(REPO, path), "utf8");

function check(label, fn) {
    fn();
    console.log("  ok  " + label);
}

// -- a DOM just big enough for a dialog of cards ------------------------------

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        id: "",
        type: "",
        title: "",
        textContent: "",
        hidden: false,
        tabIndex: 0,
        autofocus: false,
        children: [],
        parentNode: null,
        dataset: {},
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
            toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
        appendChild(child) {
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        remove() {
            const parent = element.parentNode;
            if (parent) parent.children = parent.children.filter((c) => c !== element);
            element.parentNode = null;
        },
        addEventListener() {},
        closest() { return null; },
        focus() {},
        showModal() { element.open = true; },
        close() { element.open = false; },
        //: `#id` only, which is all importHelp.js asks for.
        querySelector(selector) {
            const wanted = selector.startsWith("#") ? selector.slice(1) : null;
            const walk = (node) => {
                for (const child of node.children) {
                    if (wanted && child.id === wanted) return child;
                    const found = walk(child);
                    if (found) return found;
                }
                return null;
            };
            return walk(element);
        },
    };
    return element;
}

function textOf(node) {
    return [node.textContent, ...node.children.map(textOf)].filter(Boolean).join(" ");
}

function sandbox(license) {
    const body = makeElement("body");
    const ctx = {
        console, Object, Array, JSON, String, Set, Map, Boolean, Promise,
        document: { body, createElement: makeElement },
        plexoraUrl: (path) => `/base/${path}`,
    };
    ctx.window = ctx;
    ctx.window.flaskVariables = { license };
    createContext(ctx);
    runInContext(read("plexora/client/src/js/services/paidFeature.js"), ctx);
    runInContext(read("plexora/client/src/js/services/browsePicker.js"), ctx);
    runInContext(read("plexora/client/src/js/views/importHelp.js"), ctx);
    return { ctx, body };
}

const FREE = { plan: "free", state: "free", paid: false, entitlements: [] };
const PAID = { plan: "paid", state: "licensed", paid: true,
               entitlements: ["ai", "volume", "transcriptomics"] };
const AI_ONLY = { plan: "paid", state: "licensed", paid: true, entitlements: ["ai"] };

/** Every tab of the help, as text, the way a person would read them all. */
function helpText(license) {
    const { ctx, body } = sandbox(license);
    const dialog = ctx.PlexoraImportHelp.open({ tab: "formats" });
    assert.equal(body.children.length, 1);
    const text = textOf(dialog);
    ctx.PlexoraImportHelp.close();
    return text;
}

function sampleExamples(license) {
    const { ctx } = sandbox(license);
    return textOf(runInContext('buildSplitControl("sample", () => {})', ctx));
}

const TENX = /Xenium|Visium|transcripts\.parquet|feature_name/;

check("on Free the help describes no Xenium, Visium or transcripts format", () => {
    const text = helpText(FREE);
    assert.doesNotMatch(text, TENX, text);
    assert.doesNotMatch(text, /Transcripts/);
    assert.doesNotMatch(text, /licen|Paid/i);
    // ...and still describes everything else.
    for (const name of ["SpatialData store", "OME-Zarr image", "Multiplex image",
                        "Segmentation mask", "Cell table", "Boundaries",
                        "No image at all"]) {
        assert.ok(text.includes(name), name);
    }
});

check("a Paid licence without transcriptomics is Free here", () => {
    assert.doesNotMatch(helpText(AI_ONLY), TENX);
});

check("with transcriptomics every 10x format is described", () => {
    const text = helpText(PAID);
    for (const name of ["Xenium run", "Visium (Space Ranger)",
                        "Visium HD (Space Ranger 3/4)", "Transcripts",
                        "a Xenium run, Visium", "transcripts.parquet"]) {
        assert.ok(text.includes(name), name);
    }
});

check("on Free the sample examples name no 10x run", () => {
    const text = sampleExamples(FREE);
    assert.doesNotMatch(text, /Xenium|Visium|transcripts/, text);
    assert.match(text, /SpatialData/);
    assert.match(text, /image · mask · table/);
});

check("with transcriptomics they do", () => {
    const text = sampleExamples(PAID);
    assert.match(text, /Xenium · SpatialData · Visium · \.zarr/);
    assert.match(text, /image · mask · table · transcripts/);
});

check("the other examples are the same on every plan", () => {
    for (const name of ["image", "mask", "data"]) {
        const free = sandbox(FREE);
        const paid = sandbox(PAID);
        const of = (box) => textOf(runInContext(`buildSplitControl("${name}", () => {})`, box.ctx));
        assert.equal(of(free), of(paid), name);
    }
});

console.log("import_transcriptomics_probe: all checks passed");
