/**
 * What does QC's hover card say, and when?
 *
 * qcHover.js turns a QC region or a cell's QC record into a small card beside
 * the pointer. Every way it can be wrong is quiet: a card that says the wrong
 * thing reads as a QC result, a card re-rendered every frame flickers, a cell
 * question asked on every move floods the server, an answer to an old question
 * describes a cell the pointer has left, and a card that outlives a press or a
 * drag sits over the tissue being worked on. None of that throws.
 *
 * Run directly:  node tests/js/qc_hover_probe.mjs
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/plugins/qc/static/qcHover.js");

// -- a DOM just big enough for the card --------------------------------------------

class Node {
    constructor(tag) {
        this.tagName = tag;
        this.children = [];
        this.className = "";
        this._text = "";
        this.hidden = false;
        this.dataset = {};
        this.attributes = {};
        this.style = { setProperty(name, value) { this[name] = value; } };
        this.removed = false;
    }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join("|"); }
    append(...nodes) { this.children.push(...nodes); }
    appendChild(node) { this.children.push(node); return node; }
    replaceChildren() { this.children = []; }
    setAttribute(name, value) { this.attributes[name] = value; }
    remove() { this.removed = true; }
    getBoundingClientRect() { return { width: 200, height: 120 }; }
    find(className) {
        if (String(this.className).split(" ").includes(className)) return this;
        for (const child of this.children) {
            const hit = child.find(className);
            if (hit) return hit;
        }
        return null;
    }
}

const frames = [];
const timers = new Map();
let nextTimer = 1;
const portaled = [];
const warnings = [];

const context = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Infinity, Promise,
    Uint8Array, atob,
    console: { log: console.log, error: console.error, warn: (...a) => warnings.push(a) },
    document: { createElement: (tag) => new Node(tag), body: new Node("body") },
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    cancelAnimationFrame: () => { frames.length = 0; },
    window: {
        innerWidth: 1200, innerHeight: 900,
        setTimeout: (fn) => { const id = nextTimer++; timers.set(id, fn); return id; },
        clearTimeout: (id) => { timers.delete(id); },
        PopoverPortal: { attach: (el) => portaled.push(el), detach: () => {} },
        OpenSeadragon: {
            MouseTracker: class MouseTracker {
                constructor(options) { this.options = options; this.destroyed = false; }
                destroy() { this.destroyed = true; }
            },
        },
    },
};
const ctx = createContext(context);
runInContext(`${readFileSync(SOURCE, "utf8")}\n;globalThis.__Card = QcHoverCard; globalThis.__Probe = QcHoverProbe;`, ctx);
const Card = ctx.__Card;
const Probe = ctx.__Probe;

const checks = [];
const failures = [];

function check(name, actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

const capital = (s) => { s = String(s || ""); return s ? s[0].toUpperCase() + s.slice(1) : s; };
const helpers = {
    capital,
    originOf: (r) => (r.created_by === "user" ? { manual: true, words: "manual" }
        : String(r.created_by || "").startsWith("blur") ? { manual: false, words: "Derived from Blur QC" }
        : { manual: false, words: "AI" }),
    tracedWords: () => "",
    regionName: (r) => r.name || capital(r.category_words),
    classWords: (id) => String(id).replace(/_/g, " "),
    categoryWords: (k) => String(k).replace(/_/g, " "),
    categoryColor: () => "#888888",
    defaultClass: (k) => ({ tissue_acquisition: "tissue_artifact", blur_focus: "out_of_focus" }[k] || null),
};

// -- the words on a region's card ----------------------------------------------------

{
    const manual = Card.regionModel({
        roi_id: "r1", name: "Fold by the edge", class: "tissue_artifact", words: "tissue artifact",
        category: "tissue_acquisition", category_words: "tissue / acquisition", color: "#f00",
        action: "exclude", created_by: "user", tool: { name: "user", origin: "user" },
        n_cells: 1234,
    }, helpers);
    check("a hand-drawn region says what it is and that it was drawn by hand",
        [manual.title, manual.chip.words, manual.status.words, manual.lead],
        ["Fold by the edge", "Tissue / acquisition", "Excludes cells", "Drawn by hand"]);
    check("...with no subtype row for its category's own class, and its cells",
        manual.rows, [["Cells", "1,234 flagged"]]);
    check("...and a click opens it in the panel", manual.footer, "Click to open in the panel");

    const check_ = {
        roi_id: "r2", class: "out_of_focus", words: "out of focus", category: "blur_focus",
        category_words: "blur / focus", action: "warn", created_by: "blur_qc",
        tool: { name: "blur", origin: "check" }, score: 0.4213, score_kind: "blur_score",
        threshold: 0.31, threshold_source: "agent_refined", offset_steps: 1,
        channels: ["DNA_2"], evidence_channels: ["DNA_2"],
        ai: { verdict: "artifact", confidence: "sure", notes: null },
    };
    const scored = Card.regionModel(check_, helpers);
    check("an image check's region explains itself by its score over the bar",
        scored.lead, "Blur score 0.421 over the bar 0.31; the agent judged it an artifact");
    check("...names where the bar came from, the channels and the agent",
        scored.rows, [["Bar", "agent-refined, +1 step"], ["Channels", "DNA_2"], ["Agent", "artifact · sure"]]);
    check("...and a warning region says it only warns", scored.status.words, "Warns only");

    const noted = Card.regionModel({ ...check_, ai: { ...check_.ai, notes: "Soft field at the top edge." } }, helpers);
    check("the agent's own notes lead, the numbers said under them",
        [noted.lead, noted.note], ["Soft field at the top edge.",
                                   "Blur score 0.421 over the bar 0.31; the agent judged it an artifact"]);
    check("...and the score is then kept as a row", noted.rows[0], ["Score", "0.421 · bar 0.31"]);

    const found = Card.regionModel({
        roi_id: "r3", class: "tissue_fold", words: "tissue fold", category: "tissue_acquisition",
        category_words: "tissue / acquisition", action: "exclude", created_by: "agent",
        tool: { name: "diffuse_bright", origin: "detector" }, score: 3.2, score_kind: "detector_score",
        channels: [], ai: { verdict: "artifact", confidence: "fairly_sure", severity: "moderate" },
    }, helpers);
    check("a detector's region names the detector and the agent's judgment",
        found.lead, "Found by the diffuse bright detector; the agent judged it an artifact");
    check("...is titled by its subtype, with its score, channels and the agent's words",
        [found.title, found.rows], ["Tissue fold", [["Score", "3.2"], ["Channels", "all"],
                                                    ["Agent", "artifact · fairly sure · moderate"]]]);
    const renamed = Card.regionModel({ ...check_, name: "Top edge haze" }, helpers);
    const generated = Card.regionModel({ ...check_, name: "QC warn: out of focus · DNA_2" }, helpers);
    check("a name the user gave is kept as the title; QC's own generated name is not",
        [renamed.title, generated.title], ["Top edge haze", "Out of focus"]);
}

// -- the words on a cell's card ------------------------------------------------------

const FLAGGED = {
    cell_id: 16566, calls: true, pass: false, action: "exclude", primary_reason: "counterstain_low",
    reasons: [
        { reason: "counterstain_low", words: "Low counterstain", category: "staining_signal",
          category_words: "staining / signal", color: "#60a5fa", status: "fail",
          channels: ["DNA_1"], source_label: "DNA_1", measure: "m_counterstain_log",
          value: 4.1234, side: "low", cutoff: 4.6, space: "log", offset_steps: null,
          threshold_source: "auto", verdict: "accept", notes: "Faint nuclei in debris.",
          via_regions: [] },
        { reason: "region:tissue_fold", words: "In tissue fold", category: "tissue_acquisition",
          category_words: "tissue / acquisition", color: "#ef4444", status: "warn",
          via_regions: [{ roi_id: "r9", name: "Fold 2", class_words: "tissue fold",
                          fraction: 0.86, method: "mask" }] },
    ],
    markers: [{ marker: "CD3", reason: "extreme_value", words: "Extreme value", color: "#fb7185",
                status: "unreliable", value: 9.5, cutoff: 7, space: "log1p", via_regions: [] }],
    regions: [{ roi_id: "r9", name: "Fold 2", class_words: "tissue fold", action: "warn",
                fraction: 0.86, method: "mask", color: "#ef4444" },
              { roi_id: "r10", name: "Bubble", class_words: "air bubble", action: "exclude",
                fraction: 1, method: "mask", color: "#22d3ee" }],
    segqc: { status: "under_segmented", under_score: 0.71, over_score: 0.1, flag: 0.6, partner_id: 77 },
    unreliable_markers: ["CD3"],
};

{
    check("a cell QC has nothing to say about gets no card",
        Card.cellModel({ cell_id: 4, calls: true, pass: true, action: "pass", reasons: [],
                         markers: [], regions: [], segqc: { status: "pass" } }, helpers), null);
    const model = Card.cellModel(FLAGGED, helpers);
    check("a flagged cell's card leads with its primary reason, the value and the bar",
        [model.title, model.status.words, model.chip.words, model.lead],
        ["Cell 16566", "Excluded", "Staining / signal", "Low counterstain: DNA_1 4.12 below the bar 4.6 (log)"]);
    check("...the module's notes under it", model.note, "Faint nuclei in debris.");
    check("...its channels and the agent's verdict on that side",
        model.rows, [["Channels", "DNA_1"], ["Agent", "accept on this side"]]);
    check("...then its other reasons, markers, regions and Segmentation QC, in that order",
        model.sections.map((s) => s.heading), ["Also", "Markers", "In regions", "Segmentation QC"]);
    check("...a region reason says which region and how much of the cell is in it",
        model.sections[0].items[0], { text: "In Fold 2 · 86% inside", detail: "warned", color: "#ef4444" });
    check("...a marker flag its value over its bar",
        model.sections[1].items[0], { text: "CD3 · extreme value", detail: "9.5 > 7 (unreliable)", color: "#fb7185" });
    check("...a region already named is not listed again",
        model.sections[2].items.map((i) => i.text), ["Bubble"]);
    check("...and Segmentation QC's call with its scores and partner",
        model.sections[3].items[0], { text: "Merged cells · with cell 77", detail: "under 0.71 / over 0.1 · bar 0.6" });

    const generated = Card.cellModel({ ...FLAGGED, reasons: [{ ...FLAGGED.reasons[1], via_regions: [
        { roi_id: "r9", name: "QC warn: tissue fold · Nucleus, AF1, CD45, Ki67 +36",
          class_words: "tissue fold", fraction: 1, method: "mask" }] }] }, helpers);
    check("a region QC named itself is called by its subtype on a cell's card",
        generated.lead, "In Tissue fold");

    const many = { ...FLAGGED, markers: [], regions: [], segqc: null,
                   reasons: [FLAGGED.reasons[0], ...Array.from({ length: 6 }, (_, i) => ({
                       reason: `r${i}`, words: `Reason ${i}`, status: "warn", color: "#fff" }))] };
    const capped = Card.cellModel(many, helpers).sections[0].items;
    check("several other reasons are capped, the rest pointed to the panel",
        [capped.length, capped[4].text], [5, "+2 more in the panel"]);

    const hidden = Card.visibleRecord(FLAGGED, (level, entry) => level !== "marker"
        && entry.reason !== "region:tissue_fold");
    check("a reason or marker whose group is hidden is left off",
        [hidden.reasons.map((r) => r.reason), hidden.markers.length], [["counterstain_low"], 0]);

    const grouped = Card.cellModelFromGroups(5, [
        { level: "cell", status: "warn", label: "High counterstain", color: "#818cf8", category_words: "staining / signal" },
        { level: "cell", status: "fail", label: "In tissue fold", color: "#ef4444", category_words: "tissue / acquisition" },
        { level: "marker", status: "unreliable", label: "CD3 · extreme value", color: "#fb7185" },
    ], helpers);
    check("without calls to read, the panel's groups say what the cell is coloured for",
        [grouped.status.words, grouped.lead, grouped.sections.map((s) => s.heading)],
        ["Excluded", "In tissue fold", ["Also", "Markers"]]);
    check("...and a cell in no group gets no card", Card.cellModelFromGroups(5, [], helpers), null);
}

// -- where the card sits -------------------------------------------------------------

{
    const card = new Card();
    card.ensure();
    const bounds = { left: 100, top: 50, right: 900, bottom: 650 };
    card.place({ x: 300, y: 200 }, bounds);
    check("the card sits below and right of the pointer", [card.card.style.left, card.card.style.top],
          ["318px", "218px"]);
    card.place({ x: 850, y: 600 }, bounds);
    check("...flips left and up at the image's far edges", [card.card.style.left, card.card.style.top],
          ["632px", "462px"]);
    card.place({ x: 120, y: 60 }, { left: 100, top: 50, right: 260, bottom: 140 });
    check("...and never leaves the image where nothing fits", [card.card.style.left, card.card.style.top],
          ["108px", "58px"]);
    check("the card is portaled and never takes the pointer's role", [portaled.length >= 1,
          card.card.attributes.role], [true, "tooltip"]);
}

// -- the pointer ---------------------------------------------------------------------

/** OSD's Point, as far as the viewer's pixel-to-image conversion needs it: it
 *  calls `minus`, so a plain {x, y} handed on in its place throws there. */
class Point {
    constructor(x, y) { this.x = x; this.y = y; }
    clone() { return new Point(this.x, this.y); }
    minus(other) { return new Point(this.x - other.x, this.y - other.y); }
}

function toImage(p) {
    if (typeof p.minus !== "function") throw new TypeError("e.minus is not a function");
    return [p.x, p.y];
}

const REGION = {
    roi_id: "r1", name: "Fold", class: "tissue_artifact", category: "tissue_acquisition",
    category_words: "tissue / acquisition", action: "exclude", created_by: "user",
    tool: { name: "user", origin: "user" },
};

/** A filled square of a cell's pixels, packed as the server sends it. */
function square(x, y, size) {
    const bits = new Uint8Array(Math.ceil(size * size / 8)).fill(0xff);
    return { box: [x, y, size, size], bits: Buffer.from(bits).toString("base64") };
}

function makeProbe(options = {}) {
    const handlers = new Map();
    const viewer = {
        canvas: { getBoundingClientRect: () => ({ left: 20, top: 10, right: 1020, bottom: 810 }) },
        addHandler: (name, fn) => handlers.set(name, fn),
        removeHandler: (name) => handlers.delete(name),
    };
    const asked = [];
    const selected = [];
    const state = { cellLayer: options.cellLayer ?? false, suppressed: false };
    const api = {
        answers: [],
        cellAt(x, y, radius) {
            asked.push({ x, y, radius });
            const next = this.answers.shift();
            return next instanceof Promise ? next : Promise.resolve(next);
        },
    };
    const hits = [];
    const probe = new Probe({ viewer: { viewer }, layers: { onViewportChange: () => () => {} } }, {
        overlay: { hitTest: (x, y) => { hits.push([x, y]); return x < 100 ? { region: REGION } : null; } },
        api,
        toImage,
        imagePerScreen: () => 1,
        isSuppressed: () => state.suppressed,
        isCellLayerOn: () => state.cellLayer,
        helpers,
        cellGroupsFor: () => [],
        onSelect: (region) => selected.push(region.roi_id),
        onSelectCell: (record, shape) => selected.push(`cell ${record.cell_id} @${shape ? shape.x : "-"}`),
    });
    probe.arm();
    return { probe, handlers, asked, selected, state, api, hits };
}

function moveTo(probe, x, y) {
    probe.tracker.options.moveHandler({ position: new Point(x, y) });
    frames.splice(0, frames.length).forEach((fn) => fn());
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

{
    const { probe, asked, hits } = makeProbe();
    let renders = 0;
    const render = probe.card.render.bind(probe.card);
    probe.card.render = (model) => { renders += 1; render(model); };
    moveTo(probe, 50, 50);
    moveTo(probe, 52, 51);
    moveTo(probe, 54, 52);
    check("moving about inside one region renders its card once", [renders, probe.card.visible], [1, true]);
    check("...with the pointer's client position as the anchor",
        [probe.card.card.style.left, probe.card.card.style.top], ["92px", "80px"]);
    check("...hit-testing once per frame", hits.length, 3);
    probe.tracker.options.moveHandler({ position: new Point(60, 60) });
    probe.tracker.options.moveHandler({ position: new Point(61, 60) });
    frames.splice(0, frames.length).forEach((fn) => fn());
    check("...however many moves arrive within the frame", hits.length, 4);
    check("with the cell layer off the server is never asked about a cell", asked.length, 0);
    moveTo(probe, 300, 50);
    check("leaving the region's outline hides the card", probe.card.visible, false);
    moveTo(probe, 50, 50);
    probe.tracker.options.leaveHandler();
    check("the pointer leaving the image hides it", probe.card.visible, false);
}

{
    const { probe, handlers, asked, api, state } = makeProbe({ cellLayer: true });
    api.answers.push({ ok: true, data: { cell: FLAGGED, shape: square(40, 40, 20) } });
    moveTo(probe, 50, 50);
    check("with the cell layer on, the cell under the pointer is asked for at once",
        asked, [{ x: 50, y: 50, radius: 5 }]);
    await settle();
    check("the cell's card wins over the region's, and says a click opens the region",
        [probe.card.model.title, probe.card.model.footer],
        ["Cell 16566", "Click to open the region in the panel"]);
    moveTo(probe, 58, 44);
    moveTo(probe, 41, 59);
    check("moving anywhere over a cell already seen asks nothing more", asked.length, 1);

    // One question in flight; the newest point waits, older ones are not asked.
    let release;
    api.answers.push(new Promise((resolve) => { release = resolve; }));
    api.answers.push({ ok: true, data: { cell: { ...FLAGGED, cell_id: 2 }, shape: square(80, 80, 10) } });
    moveTo(probe, 70, 70);
    moveTo(probe, 75, 75);
    moveTo(probe, 85, 85);
    check("while one question is in flight no other is sent", asked.length, 2);
    check("...and the card shown stays until the answer lands", probe.card.model.title, "Cell 16566");
    release({ ok: true, data: { cell: { ...FLAGGED, cell_id: 1 }, shape: square(68, 68, 4) } });
    await settle();
    await settle();
    check("then only the newest point is asked about, and its cell shown",
        [asked.length, asked[2], probe.card.model.title], [3, { x: 85, y: 85, radius: 5 }, "Cell 2"]);
    moveTo(probe, 69, 69);
    check("a cell answered on the way is in hand too", [asked.length, probe.card.model.title],
        [3, "Cell 1"]);

    api.answers.push({ ok: true, data: { cell: { cell_id: 3, calls: true, pass: true, action: "pass",
                                                 reasons: [], markers: [], regions: [], segqc: null },
                                         shape: square(20, 20, 10) } });
    moveTo(probe, 25, 25);
    await settle();
    check("a clean cell inside a region leaves the region's card", probe.card.model.title, "Fold");

    api.answers.push({ ok: true, data: { cell: null } });
    moveTo(probe, 300, 300);
    await settle();
    moveTo(probe, 300.5, 300.5);
    check("glass is asked about once, not on every small move", [asked.length, probe.card.visible],
        [5, false]);

    moveTo(probe, 50, 50);
    handlers.get("canvas-press")();
    check("a press hides the card", probe.card.visible, false);
    moveTo(probe, 50, 50);
    handlers.get("canvas-drag")();
    check("...and so does a drag", probe.card.visible, false);
    moveTo(probe, 50, 50);
    handlers.get("canvas-scroll")();
    check("...and a wheel", probe.card.visible, false);

    state.suppressed = true;
    const before = asked.length;
    moveTo(probe, 150, 150);
    check("no card and no question while a stroke is drawn or the ROI tool is up",
        [probe.card.visible, asked.length], [false, before]);
    state.suppressed = false;

    for (let i = 0; i < 3; i += 1) {
        api.answers.push({ ok: false, status: 500, data: {} });
        moveTo(probe, 400 + i * 10, 10);
        await settle();
    }
    const paused = asked.length;
    moveTo(probe, 480, 20);
    check("three failures in a row pause cell questions, with one warning",
        [asked.length - paused, warnings.length], [0, 1]);
}

{
    const { probe, handlers, selected, asked, api } = makeProbe({ cellLayer: true });
    api.answers.push({ ok: true, data: { cell: FLAGGED, shape: square(140, 40, 20) } });
    moveTo(probe, 150, 50);
    await settle();
    check("off every region a cell's card says a click shows its call", probe.card.model.footer,
        "Click to show the channels behind this call");
    handlers.get("canvas-click")({ quick: true, position: new Point(145, 45) });
    check("a click on a flagged cell shows that cell's call in the panel", selected, ["cell 16566 @140"]);
    // Found as the nearest cell: the pointer is off the cell's own pixels.
    api.answers.push({ ok: true, data: { cell: { ...FLAGGED, cell_id: 9 }, shape: square(200, 200, 5) } });
    moveTo(probe, 196, 196);
    await settle();
    handlers.get("canvas-press")();
    handlers.get("canvas-click")({ quick: true, position: new Point(196, 196) });
    check("a click where the card showed the nearest cell shows that cell", selected.slice(1),
        ["cell 9 @200"]);
    selected.splice(1);
    api.answers.push({ ok: true, data: { cell: { ...FLAGGED, cell_id: 8 }, shape: square(20, 20, 20) } });
    moveTo(probe, 30, 30);
    await settle();
    handlers.get("canvas-press")();
    const regionClick = { quick: true, position: new Point(30, 30) };
    handlers.get("canvas-click")(regionClick);
    check("a click inside a region opens the region, even on a flagged cell", selected.slice(1), ["r1"]);
    check("...and stops OSD's own click-to-zoom from moving it off centre",
        regionClick.preventDefaultAction, true);
    handlers.get("canvas-click")({ quick: false, position: new Point(30, 30) });
    const nothing = { quick: true, position: new Point(300, 30) };
    handlers.get("canvas-click")(nothing);
    check("...not a drag's release, nor a click on nothing, which keeps OSD's own behaviour",
        [selected.length, nothing.preventDefaultAction], [2, undefined]);
    moveTo(probe, 50, 50);
    probe.disarm();
    check("disarming cancels everything and lets go of the viewer",
        [probe.card.visible, probe.tracker, handlers.size, asked.length], [false, null, 0, 4]);
    probe.destroy();
    check("destroying removes the card", probe.card.card, null);
}

for (const name of checks) {
    if (!failures.some((f) => f.check === name)) console.log(`ok  ${name}`);
}
for (const failure of failures) {
    console.log(`FAIL ${failure.check}\n     expected ${failure.expected}\n     actual   ${failure.actual}`);
}
console.log(`\n${checks.length - failures.length}/${checks.length} checks held`);
process.exit(failures.length ? 1 : 0);
