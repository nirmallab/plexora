/**
 * The mismatch notes under the Thresholding distribution plot.
 *
 * Core answers whether this project's table, mask and image describe the same
 * sample (models/consistency.py). The panel adds the one finding core cannot
 * have -- that the marker on screen is not an image channel -- and draws the
 * lot, or takes the block away when there is nothing to say.
 *
 * Three behaviours worth pinning, and all three are logic rather than markup:
 * an agreeing project leaves no empty block behind; the marker note is
 * suppressed on a project whose table and image simply use different names for
 * everything; and the block is rebuilt rather than appended to, so opening and
 * closing the panel does not stack three copies of the same sentence.
 *
 * The real methods, called against hand-built state with `.call()` -- building
 * a whole GatingSidebarController would need a sidebar, a gating list, an API
 * and an event bus, none of which these three methods touch.
 *
 * Run directly: `node tests/js/gating_consistency_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "gating", "static",
                    "gatingSidebarController.js");

// --------------------------------------------------------------- the DOM

function makeNode(tag) {
    const node = {
        tagName: tag.toUpperCase(),
        className: "",
        dataset: {},
        hidden: false,
        attributes: {},
        children: [],
        setAttribute(name, value) { this.attributes[name] = value; },
        append(...items) { this.children.push(...items); },
        set innerHTML(value) {
            if (value === "") this.children.length = 0;
            this._html = value;
        },
        get innerHTML() { return this._html; },
        get text() {
            return this.children
                .map((child) => (typeof child === "string" ? child : child.text || ""))
                .join("");
        },
    };
    return node;
}

const target = makeNode("div");
const sandbox = {
    window: {},
    console,
    document: {
        getElementById: (id) => (id === "gate_consistency" ? target : null),
        createElement: (tag) => makeNode(tag),
        createTextNode: (text) => String(text),
    },
};
createContext(sandbox);
// `class X {}` at a script's top level is a lexical binding, not a property of
// the global object, so the vm would otherwise hand back nothing.
runInContext(
    readFileSync(SOURCE, "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;",
    sandbox);
const Controller = sandbox.GatingSidebarController;
assert.ok(Controller, "gatingSidebarController.js defined no class");

// ------------------------------------------------------------- the state

/** Just enough `this` for the three methods under test. `markers` are the
 *  gate-able columns and `channels` the image's own channel names -- two
 *  vocabularies that are frequently different sets of strings. */
function panel({ marker = null, markers = [], channels = [], consistency = [] } = {}) {
    return {
        gateMarker: marker,
        consistency,
        markersAreChannels: null,
        getGateMarkerNames: () => [...markers],
        dataLayer: { getFullChannelName: (name) => name },
        ctx: { dataset: { image: { has: (name) => channels.includes(name) } } },
        markerFinding: Controller.prototype.markerFinding,
        markersShareTheImageVocabulary: Controller.prototype.markersShareTheImageVocabulary,
        paintConsistency: Controller.prototype.paintConsistency,
    };
}

const codes = () => target.children.map((note) => note.dataset.code);

const passed = [];
function check(label, fn) {
    fn();
    passed.push(label);
    console.log("PASS", label);
}

// ------------------------------------------------------------- the checks

check("a project that agrees leaves no empty block behind", () => {
    const state = panel({ marker: "CD3", markers: ["CD3"], channels: ["CD3"] });
    state.paintConsistency();

    assert.equal(target.children.length, 0);
    assert.equal(target.hidden, true, "an empty block still occupies its margin");
});

check("core's findings are drawn, worst first, as core ordered them", () => {
    const state = panel({
        marker: "CD3", markers: ["CD3"], channels: ["CD3"],
        consistency: [{ code: "mask_size", message: "The mask is a different size." },
                      { code: "ids_below_one", message: "Cell ids start at 0." }],
    });
    state.paintConsistency();

    assert.deepEqual(codes(), ["mask_size", "ids_below_one"]);
    assert.equal(target.hidden, false);
    assert.match(target.children[0].text, /different size/);
});

check("a marker with no image channel is said so, after core's findings", () => {
    const state = panel({
        marker: "FOXP3", markers: ["CD3", "FOXP3"], channels: ["CD3"],
        consistency: [{ code: "mask_size", message: "The mask is a different size." }],
    });
    state.paintConsistency();

    assert.deepEqual(codes(), ["mask_size", "marker_has_no_channel"]);
    assert.match(target.children[1].text, /FOXP3/);
});

check("...and not on a project whose two vocabularies never overlap", () => {
    // A panel measured on one instrument and imaged on another: no marker is a
    // channel, so "no image channel is named X" is true of every one of them
    // and is a description of the project rather than a warning about it.
    const state = panel({
        marker: "FOXP3", markers: ["CD3", "FOXP3"], channels: ["DAPI", "AF488"],
    });
    state.paintConsistency();

    assert.deepEqual(codes(), []);
});

check("the overlap is decided once and not per marker", () => {
    let asked = 0;
    const state = panel({ marker: "CD3", markers: ["CD3", "FOXP3"], channels: ["CD3"] });
    state.ctx.dataset.image.has = (name) => { asked += 1; return name === "CD3"; };

    state.markersShareTheImageVocabulary();
    const afterFirst = asked;
    state.markersShareTheImageVocabulary();

    assert.equal(asked, afterFirst, "the marker list was walked twice");
});

check("repainting replaces the notes rather than stacking them", () => {
    // Every reopen of the panel and every marker change repaints; three copies
    // of one sentence is what an append would have produced by the third.
    const state = panel({
        marker: "CD3", markers: ["CD3"], channels: ["CD3"],
        consistency: [{ code: "mask_size", message: "The mask is a different size." }],
    });
    state.paintConsistency();
    state.paintConsistency();
    state.paintConsistency();

    assert.deepEqual(codes(), ["mask_size"]);
});

check("a finding with no code still draws its sentence", () => {
    // The panel renders what core sends; a server that grew a finding this
    // client has never heard of must not silently drop it.
    const state = panel({
        marker: "CD3", markers: ["CD3"], channels: ["CD3"],
        consistency: [{ message: "Something else is wrong." }],
    });
    state.paintConsistency();

    assert.equal(target.children.length, 1);
    assert.match(target.children[0].text, /Something else/);
});

console.log(`all checks passed (${passed.length})`);
