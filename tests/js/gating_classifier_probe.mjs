/**
 * A gating session's layer in the Single Cell Classifier panel.
 *
 * Automatic gating trains the classifier at each gate it writes and keeps the
 * calls as a layer beside the threshold (the server's `session_run`). What is
 * pinned here is the panel's half: the two disagreement codes are drawn
 * through the cell layer's colour table and lose to a labelled cell; the
 * marker list's dot says filled or hollow; the block names the session, the
 * gate and the counts and offers Adopt only while the threshold decides; the
 * switch asks the server for the classifier's own calls only where the run
 * kept them; the hover card names a disagreement and the gate's call; a stale
 * layer says why; and no server change moves the card into this mode.
 *
 * The real class, run in a vm context against a hand-built DOM and API --
 * the arrangement of gating_nudge_probe.mjs.
 *
 * Run directly: `node tests/js/gating_classifier_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "gating", "static", "gatingClassifier.js");

/** Just enough of an element for the panel's painting. */
class FakeElement {
    constructor(id = "", { dataset = {} } = {}) {
        this.id = id;
        this.hidden = false;
        this.disabled = false;
        this.checked = false;
        this.title = "";
        this.className = "";
        this.dataset = { ...dataset };
        this.children = [];
        this.attributes = {};
        this._text = "";
        this._classes = new Set();
        this.classList = {
            toggle: (name, on) => {
                const want = on === undefined ? !this._classes.has(name) : Boolean(on);
                if (want) this._classes.add(name); else this._classes.delete(name);
            },
            add: (name) => this._classes.add(name),
            remove: (name) => this._classes.delete(name),
            contains: (name) => this._classes.has(name),
        };
    }

    get textContent() {
        return this._text + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
    }

    set textContent(value) {
        this._text = String(value);
        this.children = [];
    }

    append(...nodes) { this.children.push(...nodes); }
    appendChild(node) { this.children.push(node); return node; }
    replaceChildren(...nodes) { this._text = ""; this.children = [...nodes]; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener() {}
    removeEventListener() {}
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

    querySelectorAll(selector) {
        const out = [];
        const walk = (node) => {
            for (const child of node.children) {
                if (typeof child === "string") continue;
                const hit = selector === "[data-code]" ? "code" in child.dataset
                    : selector.startsWith(".") ? child.className.split(" ").includes(selector.slice(1))
                        : false;
                if (hit) out.push(child);
                walk(child);
            }
        };
        walk(this);
        return out;
    }
}

const elements = new Map();
function element(id, options) {
    const node = new FakeElement(id, options);
    elements.set(id, node);
    return node;
}

function freshDom() {
    elements.clear();
    for (const id of ["clf_summary", "clf_status", "clf_session", "clf_session_head",
                      "clf_session_actions", "clf_adopt_button", "clf_discard_button",
                      "clf_raw_line", "clf_raw_toggle", "clf_run_button", "clf_revert_button",
                      "clf_train_button", "clf_review_line", "clf_review_bar",
                      "clf_positive_count", "clf_negative_count"]) {
        element(id);
    }
    // The counts, as the template nests them.
    const session = elements.get("clf_session");
    const counts = new FakeElement();
    counts.className = "clf-session-counts";
    const removedItem = new FakeElement("", { dataset: { code: "5" } });
    const addedItem = new FakeElement("", { dataset: { code: "4" } });
    removedItem.append(element("clf_session_removed"));
    addedItem.append(element("clf_session_added"));
    counts.append(removedItem, addedItem);
    session.append(elements.get("clf_session_head"), counts);
    return { counts, removedItem, addedItem };
}

const timers = [];
const sandbox = {
    console,
    Uint8Array, Int8Array, Float32Array, Map, Set, Promise, Number, String, Math, Object,
    document: {
        getElementById: (id) => elements.get(id) || null,
        createElement: () => new FakeElement(),
        addEventListener: () => {},
        removeEventListener: () => {},
        body: new FakeElement("body"),
    },
};
sandbox.window = sandbox;
sandbox.setTimeout = (fn) => { timers.push(fn); return timers.length; };
sandbox.clearTimeout = () => {};
sandbox.requestAnimationFrame = (fn) => { fn(); return 1; };
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.CSVGatingList = { events: { SELECTION_CHANGED: "selection" } };
createContext(sandbox);
runInContext(readFileSync(SOURCE, "utf8")
    + "\nglobalThis.GateClassifierControl = GateClassifierControl;"
    + "\nglobalThis.GateClassifierHover = GateClassifierHover;"
    + "\nglobalThis.CLASSIFIER_COLORS = CLASSIFIER_COLORS;", sandbox);

const IDS = [1, 2, 3, 4, 5, 6];

/** A control on a hand-built controller; `api` overrides the fake server. */
function control({ api = {}, mode = "classifier" } = {}) {
    const luts = [];
    const asked = [];
    const controller = {
        ctx: {
            layers: {
                setColorLUT: (name, lut) => luts.push([name, lut]),
                repaint: () => {},
                addOverlay: () => {},
                removeOverlay: () => {},
            },
            viewer: { numericData: { loadCells: async () => ({ ids: IDS }) } },
            telemetry: { feature: () => {} },
        },
        api: {
            classifierState: async () => { asked.push(["state"]); return {}; },
            classifierCalls: async (marker, view) => {
                asked.push(["calls", marker, view]);
                return { state: "ready", run: "r1", which: "session_run", view, codes: new Uint8Array(6),
                         positive: 0, eligible: 6, uncertain: 0, notClassified: 0, added: 0, removed: 0 };
            },
            classifierScores: async () => ({ run: "r1", scores: new Float32Array(6),
                                             agreement: new Uint8Array(6), gate: null }),
            ...api,
        },
        gateMarkerSelect: null,
        getGateMarkerNames: () => [],
        sidebar: { formatValue: (v) => Number.parseFloat(v || 0).toFixed(2) },
    };
    const self = new sandbox.GateClassifierControl(controller);
    self.mode = mode;
    self.marker = "CD8";
    self.summary = elements.get("clf_summary");
    self.statusLine = elements.get("clf_status");
    self.session = elements.get("clf_session");
    self.sessionHead = elements.get("clf_session_head");
    self.sessionActions = elements.get("clf_session_actions");
    self.adoptButton = elements.get("clf_adopt_button");
    self.discardButton = elements.get("clf_discard_button");
    self.rawLine = elements.get("clf_raw_line");
    self.rawToggle = elements.get("clf_raw_toggle");
    self.revertButton = elements.get("clf_revert_button");
    return { self, luts, asked };
}

const LAYER = {
    run_id: "r1", session_id: "gs_1a2b", origin: "session", gate_used: [6, null],
    call_rule: "gate_unless_confident", c_clean: 0.3, c_add_used: null, stale: null,
    snr: { grade: "ok" },
};

function pendingState(extra = {}) {
    return { marker: "CD8", source: "threshold", shown: "session_run", run: null,
             session_run: { ...LAYER }, stale: null, examples: {}, ...extra };
}

function readyCalls(extra = {}) {
    return { state: "ready", run: "r1", which: "session_run", view: "integrated",
             codes: new Uint8Array([0, 1, 2, 3, 4, 5]), positive: 2, eligible: 5, uncertain: 1,
             notClassified: 1, added: 0, removed: 41, ...extra };
}

const passed = [];
async function check(label, fn) {
    freshDom();
    timers.length = 0;
    await fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

const rgba = (lut, id) => Array.from(lut.colors.slice(id * 4, id * 4 + 4));

await check("codes 4 and 5 are drawn sky blue and magenta through the cell layer", async () => {
    const { self, luts } = control();
    self.state = pendingState();
    await self.drawCalls(readyCalls());
    const [name, lut] = luts.at(-1);
    assert.equal(name, "cells");
    assert.deepEqual(rgba(lut, 5), [86, 180, 233, 235]);
    assert.deepEqual(rgba(lut, 6), [255, 70, 230, 235]);
    assert.deepEqual(rgba(lut, 4), Array.from(sandbox.CLASSIFIER_COLORS[3]));
    assert.deepEqual(rgba(lut, 1), [0, 0, 0, 0], "a negative is drawn as nothing");
});

await check("a labelled cell keeps its example colour over a disagreement", async () => {
    const { self, luts } = control();
    self.state = pendingState({ examples: { negative: [{ cell_id: 6 }], positive: [{ cell_id: 5 }] } });
    await self.drawCalls(readyCalls());
    const lut = luts.at(-1)[1];
    assert.deepEqual(rgba(lut, 6), [248, 113, 113, 255]);
    assert.deepEqual(rgba(lut, 5), [52, 211, 153, 255]);
});

await check("the dot is filled for a decided marker and hollow for a session's layer", async () => {
    const { self } = control();
    self.classified = new Set(["CD3"]);
    self.runs = {
        CD3: { active: "classifier", run: "a", session_run: "a", session_id: "gs_1" },
        CD8: { active: "threshold", run: null, session_run: "b", session_id: "gs_2" },
        CD4: { active: "threshold", run: "c", session_run: null, session_id: null },
    };
    assert.equal(self.describeIndicator("CD3"), "Decided by the Single Cell Classifier");
    assert.equal(self.describeSessionLayer("CD3"), null, "a decided marker's dot stays filled");
    assert.equal(self.describeSessionLayer("CD8"),
                 "Classified by gating session gs_2 (threshold decides)");
    assert.equal(self.describeIndicator("CD8"), null);
    assert.equal(self.describeSessionLayer("CD4"), null, "a panel run alone is no session's");
});

await check("a session's layer names the session, the gate and the counts, and offers Adopt", async () => {
    const { self } = control();
    const { removedItem, addedItem } = freshDom();
    Object.assign(self, { session: elements.get("clf_session"),
                          sessionHead: elements.get("clf_session_head") });
    self.state = pendingState();
    self.calls = readyCalls();
    self.paintSession();
    assert.equal(self.session.hidden, false);
    assert.equal(self.sessionHead.textContent,
                 "Classified by session gs_1a2b at gate 6.00. The threshold decides.");
    assert.equal(elements.get("clf_session_removed").textContent, "41");
    assert.equal(elements.get("clf_session_added").textContent, "0");
    assert.ok(addedItem.classList.contains("is-zero") && !removedItem.classList.contains("is-zero"));
    assert.equal(self.sessionActions.hidden, false);
    assert.equal(self.adoptButton.disabled, false);
    assert.equal(self.rawLine.hidden, false);
    assert.match(self.sessionHead.title, /unless the classifier is confident/);
});

await check("an adopted layer keeps its head and counts and loses Adopt", async () => {
    const { self } = control();
    freshDom();
    Object.assign(self, { session: elements.get("clf_session"),
                          sessionHead: elements.get("clf_session_head") });
    self.state = pendingState({ source: "classifier", shown: "run", run: { ...LAYER } });
    self.calls = readyCalls({ which: "run" });
    self.paintSession();
    assert.equal(self.session.hidden, false);
    assert.equal(self.sessionHead.textContent,
                 "Decided by the classifier: adopted from session gs_1a2b at gate 6.00.");
    assert.equal(self.sessionActions.hidden, true);
    assert.equal(elements.get("clf_session_removed").textContent, "41");
});

await check("calls that no longer match the table cannot be adopted", async () => {
    const { self } = control();
    self.state = pendingState();
    self.calls = readyCalls({ state: "stale" });
    self.paintSession();
    assert.equal(self.adoptButton.disabled, true);
    assert.equal(self.session.querySelector(".clf-session-counts").hidden, true);
});

await check("a panel run with no layer and no gate shows no block", async () => {
    const { self } = control();
    self.state = { marker: "CD8", source: "classifier", shown: "run", examples: {},
                   run: { run_id: "p1", call_rule: "score_0.5", gate_used: null }, session_run: null };
    self.calls = readyCalls({ run: "p1", which: "run" });
    self.paintSession();
    assert.equal(self.session.hidden, true);
});

await check("the switch asks for the classifier's own calls only where the run kept them", async () => {
    const { self, asked } = control({
        api: { classifierState: async () => ({ marker: pendingState(), runs: {}, classified: [] }) },
    });
    await self.refresh();
    assert.deepEqual(asked.filter((a) => a[0] === "calls").at(-1), ["calls", "CD8", "integrated"]);
    self.setView("raw");
    await new Promise((resolve) => setImmediate(resolve));
    await self.refresh();
    assert.deepEqual(asked.filter((a) => a[0] === "calls").at(-1), ["calls", "CD8", "raw"]);
    // A panel run keeps no raw calls: the switch is moot and not asked for.
    const panel = control({
        api: { classifierState: async () => ({
            marker: { marker: "CD8", source: "classifier", shown: "run", examples: {},
                      run: { run_id: "p1", call_rule: "score_0.5" }, session_run: null } }) },
    });
    panel.self.view = "raw";
    await panel.self.refresh();
    assert.deepEqual(panel.asked.filter((a) => a[0] === "calls").at(-1), ["calls", "CD8", "integrated"]);
});

await check("the hover names a disagreement and the gate's call", async () => {
    const { self } = control();
    self.state = pendingState();
    self.calls = readyCalls();
    self.scores = { run: "r1", scoreName: "score", scores: new Float32Array([0.1, 0.9, NaN, 0.42, 0.97, 0.12]),
                    agreement: new Uint8Array([90, 90, 0, 70, 95, 88]),
                    gate: new Int8Array([0, 1, -1, 1, 0, 1]) };
    const hover = new sandbox.GateClassifierHover(self);
    const shown = [];
    hover.showCard = (title, tone, rows) => shown.push({ title, tone, rows });
    hover.hideCard = () => shown.push(null);
    await hover.render({ cell_id: 6 });
    assert.equal(shown.at(-1).title, "CD8 negative · above the threshold");
    assert.equal(shown.at(-1).tone, "removed");
    assert.deepEqual(Array.from(shown.at(-1).rows.find((r) => r[0] === "Gate")), ["Gate", "positive"]);
    await hover.render({ cell_id: 5 });
    assert.equal(shown.at(-1).title, "CD8 positive · below the threshold");
    assert.equal(shown.at(-1).tone, "added");
    // Uncertain, agreeing with its gate: the gate's call, whatever the score.
    await hover.render({ cell_id: 4 });
    assert.equal(shown.at(-1).title, "CD8 positive · uncertain");
    await hover.render({ cell_id: 1 });
    assert.equal(shown.at(-1), null, "a negative says nothing");
});

await check("a stale layer says why, and no examples hint sits under it", async () => {
    const { self } = control();
    self.state = pendingState({ session_run: { ...LAYER, stale: "the gate moved since this run" } });
    self.paintStatus(0, 0, 3);
    assert.equal(self.statusLine.hidden, false);
    assert.equal(self.statusLine.textContent,
                 "Stale: the gate moved since this run. These calls were made at gate 6.00.");
    assert.equal(self.statusLine.dataset.tone, "warning");
    self.state = pendingState();
    self.paintStatus(0, 0, 3);
    assert.equal(self.statusLine.hidden, true);
});

await check("no server change moves the card into this mode", async () => {
    const { self, asked } = control({ mode: "threshold" });
    self.onServerChange({ detail: { plugin: "gating", kind: "classifier.changed",
                                    payload: { marker: "CD8" } } });
    self.onServerChange({ detail: { plugin: "gating", kind: "gating.session",
                                    payload: { event: "unit_closed" } } });
    self.onServerChange({ detail: { plugin: "gating", kind: "gating.session",
                                    payload: { event: "issued" } } });
    assert.equal(timers.length, 1, "a closed marker catches up once; an issued packet does not");
    timers.forEach((fn) => fn());
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(self.mode, "threshold");
    assert.equal(asked.filter((a) => a[0] === "state").length, 2, "only the dots catch up");
    assert.equal(asked.filter((a) => a[0] === "calls").length, 0);
});

console.log(`all checks passed (${passed.length})`);
