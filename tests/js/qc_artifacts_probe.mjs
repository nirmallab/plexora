/**
 * The Artifact Detector in the QC panel: one line per category with its own
 * threshold slider, the channels shown under it, and a click on an object
 * that puts its channel on screen.
 *
 * What is worth pinning is where the controls could bleed into each other or
 * drift from the server: a slider that asks the server on every tick (it is
 * a filter of objects in hand) or stores another category's threshold, a
 * value echoed back into the slider it came from, a channel line that moves
 * a threshold, eyes that are really one, a hit test that misses an object
 * under another or finds a hidden one, a click that takes a slot from
 * Registration or the nuclear channel, and a menu that selects before the
 * user chose.
 *
 * The real class from qcArtifacts.js, in a vm with a hand-built document,
 * slider, swatch, select, Path2D and API.
 *
 * Run directly: `node tests/js/qc_artifacts_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "qc", "static", "qcArtifacts.js");

const timers = new Map();
let nextTimer = 1;

function classList() {
    const set = new Set();
    return {
        add: (...names) => names.forEach((c) => set.add(c)), remove: (c) => set.delete(c),
        contains: (c) => set.has(c),
        toggle: (c, on) => { const want = on === undefined ? !set.has(c) : Boolean(on);
                             if (want) set.add(c); else set.delete(c); return want; },
    };
}

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(), children: [], parent: null, attributes: {}, dataset: {},
        style: { props: {}, setProperty(k, v) { this.props[k] = v; } },
        hidden: false, disabled: false, textContent: "", title: "", className: "",
        classList: classList(), listeners: {}, innerHTML: "",
        setAttribute(key, value) { node.attributes[key] = String(value); },
        getAttribute(key) { return node.attributes[key] ?? null; },
        addEventListener(name, fn) { (node.listeners[name] ||= []).push(fn); },
        click() { for (const fn of node.listeners.click || []) {
            fn({ currentTarget: node, stopPropagation() {} }); } },
        appendChild(child) { return node.insertBefore(child, null); },
        append(...kids) { kids.forEach((k) => node.appendChild(k)); },
        insertBefore(child, before) {
            child.parent?.children.splice(child.parent.children.indexOf(child), 1);
            const at = before ? node.children.indexOf(before) : -1;
            if (at < 0) node.children.push(child); else node.children.splice(at, 0, child);
            child.parent = node;
            return child;
        },
        get firstChild() { return node.children[0] || null; },
        get nextSibling() {
            const kids = node.parent ? node.parent.children : [];
            return kids[kids.indexOf(node) + 1] || null;
        },
        remove() { node.parent?.children.splice(node.parent.children.indexOf(node), 1);
                   node.parent = null; },
    };
    return node;
}

const ids = ["qc_tool_art", "qc_art_cats", "qc_art_channels", "qc_art_all", "qc_art_all_count",
    "qc_art_all_eye", "qc_art_add", "qc_art_run", "qc_art_eye", "qc_art_edit", "qc_art_summary",
    "qc_art_status", "qc_art_progress", "qc_art_fill", "qc_art_phase", "qc_art_cancel"];
const elements = Object.fromEntries(ids.map((id) => [id, element()]));
elements.qc_art_channels.appendChild(elements.qc_art_all);

const sliders = [];
class PlexoraSlider {
    constructor(mount, options) {
        this.mount = mount;
        this.options = options;
        this.value = options.value;
        this.sets = [];
        this.accent = null;
        this.disabled = false;
        sliders.push(this);
    }
    get() { return this.value; }
    set(value, options = {}) { this.value = value; this.sets.push([value, options]); }
    setAccent(css) { this.accent = css; }
    setDisabled(flag) { this.disabled = Boolean(flag); }
    destroy() {}
}

class ColorSwatchPicker {
    constructor(mount, options) { this.mount = mount; this.options = options; this.value = options.value; }
    setValue(hex) { this.value = hex; }
    destroy() {}
}

class SearchableSelect {
    constructor(mount, options) {
        this.mount = mount;
        this.options = options;
        this.names = [...options.options];
        this.opened = 0;
    }
    setOptions(names) { this.names = [...names]; }
    open() { this.opened += 1; }
    destroy() { this.destroyed = true; }
}

const menus = [];
const QcTree = { menu: (anchor, items, opts) => menus.push({ anchor, items, opts }),
                 closePopup() {} };

/** Rings, even-odd inside and a stroke as wide as `lineWidth`. */
class Path2D {
    constructor() { this.rings = []; }
    moveTo(x, y) { this.rings.push([[x, y]]); }
    lineTo(x, y) { this.rings.at(-1).push([x, y]); }
    closePath() {}
}
const scratch = {
    lineWidth: 1,
    isPointInPath(path, x, y) {
        let inside = false;
        for (const ring of path.rings) {
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
                const [xi, yi] = ring[i];
                const [xj, yj] = ring[j];
                if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
            }
        }
        return inside;
    },
    isPointInStroke(path, x, y) {
        const half = this.lineWidth / 2;
        for (const ring of path.rings) {
            for (let i = 0; i < ring.length; i++) {
                const [ax, ay] = ring[i];
                const [bx, by] = ring[(i + 1) % ring.length];
                const len = Math.hypot(bx - ax, by - ay) || 1;
                const t = Math.max(0, Math.min(1, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / (len * len)));
                if (Math.hypot(x - (ax + t * (bx - ax)), y - (ay + t * (by - ay))) <= half) return true;
            }
        }
        return false;
    },
};
const QcRegionOverlay = { scratch: () => scratch };

const square = (x0, y0, x1, y1) => ({ type: "Polygon",
    coordinates: [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]] });
const OBJECTS = [
    { id: "art_fold_0001", category: "fold", score: 0.9, channels: ["DNA_1", "CD3"],
      source_channel: "DNA_1", geometry: square(0, 0, 100, 100), bbox: [0, 0, 100, 100],
      area_um2: 10000, refined: true, metrics: {} },
    { id: "art_debris_0001", category: "debris", score: 0.5, channels: ["CD3"],
      source_channel: "CD3", geometry: square(50, 50, 150, 150), bbox: [50, 50, 150, 150],
      area_um2: 10000, refined: true, metrics: { shape: "compact" } },
    { id: "art_saturation_0001", category: "saturation", score: 0.7, channels: ["DNA_2"],
      source_channel: "DNA_2", geometry: square(300, 300, 350, 350), bbox: [300, 300, 350, 350],
      area_um2: 2500, refined: true, metrics: {} },
];
const COLORS = { fold: "#ef4444", tear: "#0ea5e9", debris: "#a855f7", saturation: "#f97316" };
const CHANNELS = ["DNA_1", "CD3", "CD8", "DNA_2"];
let shown = [];
let results = { fingerprint: "fp1", status: "ok", stale: false, warnings: [] };
let job = null;
const stored = {};

function status() {
    return {
        available: true, channels: CHANNELS, nuclear: "DNA_1", shown: [...shown],
        categories: Object.keys(COLORS).map((key) => ({
            key, class: key, color: COLORS[key], words: key, enabled: true, n_total: 1,
            threshold: key in stored ? { value: stored[key], auto: 0.5, source: "user" }
                : { value: 0.5, auto: 0.5, source: "auto" } })),
        results, job,
    };
}

const calls = [];
const api = {
    artifacts: async () => { calls.push(["status"]); return { ok: true, data: { artifacts_qc: status() } }; },
    artifactsObjects: async () => { calls.push(["objects"]);
        return { ok: true, data: { available: true, fingerprint: results.fingerprint,
                                   objects: OBJECTS } }; },
    artifactsSet: async (body) => { calls.push(["set", body]);
        if (Array.isArray(body.channels)) shown = [...body.channels];
        if (body.channels === "default") shown = [];
        if (body.category && typeof body.threshold === "number") stored[body.category] = body.threshold;
        return { ok: true, data: { category: body.category, shown,
                                   threshold: { value: body.threshold, auto: 0.5, source: "user" } } }; },
    artifactsRun: async (body) => { calls.push(["run", body]); return { ok: true, data: { job_id: "j1" } }; },
    artifactsWriteRegions: async (body) => { calls.push(["write", body]);
        return { ok: true, data: { written: ["r1", "r2"], kept: [] } }; },
    job: async (id) => { calls.push(["job", id]);
        return { ok: true, data: { job: jobAnswer } }; },
    jobCancel: async () => ({ ok: true, data: {} }),
};
let jobAnswer = { status: "running", progress: { done: 3, total: 12, message: "Reading channels (3/12)" } };

// The image channels: slot 0 DNA_1 (nuclear), slot 1 CD8 on, slot 2 empty.
const slots = [
    { index: 0, name: "DNA_1", enabled: true, visible: true },
    { index: 1, name: "CD8", enabled: true, visible: true },
    { index: 2, name: "CD3", enabled: false, visible: true },
    { index: 3, name: "", enabled: false, visible: false },
];
const sidebar = {
    channelSlots: slots, marks: [], persistence: [],
    setSlotMarker(i, name, options) { this.marks.push([i, name, options]);
        Object.assign(slots[i], { name, enabled: Boolean(options.enable) || slots[i].enabled,
                                  visible: true }); },
    createAdditionalSlot() { this.created = (this.created || 0) + 1; return null; },
    suspendPersistence() { this.persistence.push("off"); },
    resumePersistence() { this.persistence.push("on"); },
};

let invalidations = 0;
const messages = [];
let confirmed = [];
const sandbox = {
    console, Math, Number, JSON, Object, Array, Map, Set, Promise, String, RegExp,
    PlexoraSlider, ColorSwatchPicker, SearchableSelect, QcTree, Path2D, QcRegionOverlay,
    document: {
        getElementById: (id) => elements[id] || null,
        createElement: (tag) => element(tag),
        body: element("body"),
    },
    window: {
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        setTimeout: (fn) => { const id = nextTimer++; timers.set(id, fn); return id; },
        clearTimeout: (id) => timers.delete(id),
        __plexora: { viewerSidebar: sidebar },
        PlexoraConfirm: { ask: async (spec) => { confirmed.push(spec); return true; } },
    },
};
sandbox.window.document = sandbox.document;
createContext(sandbox);
runInContext(readFileSync(SOURCE, "utf8") + "\nthis.QcArtifactsQc = QcArtifactsQc;", sandbox);
const { QcArtifactsQc } = sandbox;

const ctx = {
    datasource: "demo",
    layers: { addOverlay: () => ({ invalidate: () => { invalidations++; }, remove() {} }) },
};
let reloads = 0;
const host = { message: (m) => messages.push(m), reload: () => { reloads++; },
               registration: { active: false }, nuclearChannel: () => "DNA_1" };
const art = new QcArtifactsQc(ctx, api, host);
art.setup();

async function flush() {
    for (let round = 0; round < 6; round++) {
        const pending = [...timers.entries()];
        timers.clear();
        for (const [, fn] of pending) await fn();
        await new Promise((resolve) => setImmediate(resolve));
    }
}

const same = (actual, expected) => assert.equal(JSON.stringify(actual), JSON.stringify(expected));
const entry = (key) => art.cats.get(key);
const sliderOf = (key) => entry(key).slider;
// The real slider holds the value its thumb is at before it says so.
const drag = (key, value) => { sliderOf(key).value = value; sliderOf(key).options.onInput(value); };
const release = (key, value) => { sliderOf(key).value = value; sliderOf(key).options.onChange(value); };
const visibleIds = () => art.shownObjects().map((o) => o.id);
const catNames = () => elements.qc_art_cats.children.map((row) => row.dataset.key);
const lineNames = () => elements.qc_art_channels.children.slice(1)
    .filter((row) => row.dataset.channel).map((row) => row.dataset.channel);

function check(name, fn) {
    return Promise.resolve(fn()).then(() => console.log(`ok  ${name}`));
}

art.adopt(status());
await flush();

await check("four category rows come from the status, each with its colour, threshold and count", () => {
    same(catNames(), ["fold", "tear", "debris", "saturation"]);
    same(["fold", "debris"].map((k) => sliderOf(k).accent), [COLORS.fold, COLORS.debris]);
    same(["fold", "tear", "debris", "saturation"].map((k) => entry(k).nodes.count.textContent),
         ["1", "0", "1", "1"]);
    assert.equal(calls.filter((c) => c[0] === "objects").length, 1);
    art.adopt(status());
    assert.equal(calls.filter((c) => c[0] === "objects").length, 1);
    assert.equal(elements.qc_art_summary.textContent, "3");
});

await check("a slider filters the objects at once and asks the server nothing while dragged", async () => {
    calls.length = 0;
    const before = invalidations;
    drag("debris", 0.6);
    assert.equal(art.retained(OBJECTS[1]), false);
    same(visibleIds(), ["art_fold_0001", "art_saturation_0001"]);
    drag("debris", 0.4);
    same(visibleIds(), ["art_fold_0001", "art_debris_0001", "art_saturation_0001"]);
    await flush();
    same(calls, []);
    assert.ok(invalidations > before);
});

await check("the release stores that category's threshold once, typed in full", async () => {
    calls.length = 0;
    release("debris", 0.4137);
    await flush();
    same(calls, [["set", { category: "debris", threshold: 0.4137 }]]);
    assert.equal(entry("debris").value, 0.4137);
    assert.equal(entry("fold").value, 0.5);
    assert.equal(sliderOf("debris").options.display(0.4137), "0.41");
});

await check("a value from outside moves only its own slider, and none is echoed back", () => {
    for (const s of sliders) s.sets.length = 0;
    art.setThreshold(entry("tear"), 0.37);
    same(sliderOf("tear").sets.at(-1), [0.37, { silent: true }]);
    assert.equal(sliderOf("fold").sets.length + sliderOf("debris").sets.length, 0);
    sliderOf("tear").sets.length = 0;
    sliderOf("tear").value = 0.52;
    art.setThreshold(entry("tear"), 0.52, { from: "slider" });
    assert.equal(sliderOf("tear").sets.length, 0);
});

await check("a colour picked is that category's and recolours its slider", async () => {
    calls.length = 0;
    entry("fold").picker.options.onChange("#123456");
    await flush();
    same(calls, [["set", { category: "fold", color: "#123456" }]]);
    assert.equal(sliderOf("fold").accent, "#123456");
    assert.equal(sliderOf("debris").accent, COLORS.debris);
});

await check("with no channel listed, All channels' eye shows and hides everything", () => {
    elements.qc_art_all_eye.click();
    same(visibleIds(), []);
    assert.equal(elements.qc_art_all_eye.title, "Show all");
    assert.equal(elements.qc_art_all.classList.contains("is-hidden"), true);
    // Counts are what the thresholds keep, whatever the eyes say.
    assert.equal(elements.qc_art_summary.textContent, "3");
    elements.qc_art_all_eye.click();
    assert.equal(visibleIds().length, 3);
    assert.equal(elements.qc_art_all_eye.title, "Hide all");
});

await check("+ adds an empty line whose pick lists the channel and moves no threshold", async () => {
    calls.length = 0;
    elements.qc_art_add.click();
    await flush();
    same(calls, []);
    const draft = art.draft;
    assert.ok(draft && draft.select.opened === 1);
    same(draft.select.names, CHANNELS);
    draft.select.options.onChange("CD3");
    await flush();
    same(calls, [["set", { channels: ["CD3"] }], ["status"]]);
    assert.equal(art.draft, null);
    same(lineNames(), ["CD3"]);
    same(Object.keys(COLORS).map((k) => entry(k).value), [0.5, 0.5, 0.4137, 0.5]);
});

await check("with channels listed, an object shows when a channel it is seen in is listed and on", async () => {
    same(visibleIds(), ["art_fold_0001", "art_debris_0001"]);
    await art.setChannels(["CD3", "DNA_2"]);
    same(lineNames(), ["CD3", "DNA_2"]);
    same(visibleIds(), ["art_fold_0001", "art_debris_0001", "art_saturation_0001"]);
    art.lines.get("CD3").eye.click();
    same(visibleIds(), ["art_saturation_0001"]);
    // All: some line off -> every line on; all on -> every line off.
    elements.qc_art_all_eye.click();
    same(visibleIds().length, 3);
    elements.qc_art_all_eye.click();
    same(visibleIds(), []);
    elements.qc_art_all_eye.click();
    assert.equal(art.lines.get("DNA_2").count.textContent, "1");
});

await check("a line's remove takes one channel off; the last one removed is every channel again", async () => {
    calls.length = 0;
    art.lines.get("CD3").remove.click();
    await flush();
    same(calls.filter((c) => c[0] === "set"), [["set", { channels: ["DNA_2"] }]]);
    same(lineNames(), ["DNA_2"]);
    art.lines.get("DNA_2").remove.click();
    await flush();
    same(calls.filter((c) => c[0] === "set").at(-1), ["set", { channels: "default" }]);
    same(lineNames(), []);
    assert.equal(visibleIds().length, 3);
});

await check("a category's eye hides its objects and disables its slider", () => {
    entry("saturation").nodes.eye.click();
    same(visibleIds(), ["art_fold_0001", "art_debris_0001"]);
    assert.equal(sliderOf("saturation").disabled, true);
    assert.equal(sliderOf("fold").disabled, false);
    entry("saturation").nodes.eye.click();
    assert.equal(sliderOf("saturation").disabled, false);
});

await check("the hit test returns every shown object under the point, topmost first, edges included", () => {
    same(art.hitTest(75, 75, {}).map((h) => h.id), ["art_debris_0001", "art_fold_0001"]);
    same(art.hitTest(20, 20, {}).map((h) => h.id), ["art_fold_0001"]);
    assert.equal(art.hitTest(200, 20, {}), null);
    const edge = art.hitTest(101, 20, { tolerance: 2 });
    same(edge.map((h) => [h.id, h.edge]), [["art_fold_0001", true]]);
    art.setThreshold(entry("debris"), 0.6);
    same(art.hitTest(75, 75, {}).map((h) => h.id), ["art_fold_0001"]);
    art.setThreshold(entry("debris"), 0.4137);
});

await check("a click on one object selects it and switches on a slot already holding its channel", () => {
    art.onClick(art.hitTest(120, 120, {}), { x: 120, y: 120, scale: 1 });
    assert.equal(art.selectedId, "art_debris_0001");
    same(sidebar.marks, [[2, "CD3", { enable: true, keepColor: true, reveal: true }]]);
    same(sidebar.persistence, ["off", "on"]);
    // A channel already on screen changes no slot.
    sidebar.marks.length = 0;
    art.onClick(art.hitTest(20, 20, {}), { x: 20, y: 20, scale: 1 });
    assert.equal(art.selectedId, "art_fold_0001");
    same(sidebar.marks, []);
});

await check("otherwise one slot is kept for the section and reused, never Registration's pair nor the nuclear slot", () => {
    host.registration.active = true;
    slots[2].name = "CD3";
    slots[2].enabled = true;
    art.onClick(art.hitTest(320, 320, {}), { x: 320, y: 320, scale: 1 });
    same(sidebar.marks.at(-1), [3, "DNA_2", { enable: true, keepColor: true, reveal: true }]);
    same(JSON.parse(JSON.stringify(art.artifactSlot)), { index: 3, name: "DNA_2" });
    OBJECTS[2].source_channel = "CD8";
    OBJECTS[2].channels = ["CD8"];
    slots[1].name = "CD20";
    art.onClick(art.hitTest(320, 320, {}), { x: 320, y: 320, scale: 1 });
    same(sidebar.marks.at(-1), [3, "CD8", { enable: true, keepColor: true, reveal: true }]);
    assert.equal(sidebar.marks.some(([i]) => i === 0 || i === 1), false);
    assert.equal(sidebar.created || 0, 0);
    host.registration.active = false;
});

await check("several objects under a click open a menu to choose; nothing is selected until one is chosen", () => {
    art.selectedId = null;
    menus.length = 0;
    art.onClick(art.hitTest(75, 75, {}), { x: 75, y: 75, scale: 1, anchor: { x: 400, y: 300 } });
    assert.equal(art.selectedId, null);
    assert.equal(menus.length, 1);
    same(menus[0].items.map((i) => i.label), ["Debris · CD3 · 0.50", "Fold · DNA_1 · 0.90"]);
    same(menus[0].opts.heading, "Artifacts here");
    assert.equal(menus[0].anchor.style.left, "400px");
});

await check("choosing one selects it; a second click at the same spot steps to the next", () => {
    menus[0].items[1].onSelect();
    assert.equal(art.selectedId, "art_fold_0001");
    // Selected comes first under the pointer now; the same spot steps on.
    const hits = art.hitTest(75, 75, {});
    same(hits.map((h) => h.id), ["art_fold_0001", "art_debris_0001"]);
    art.lastClick = { x: 75, y: 75, ids: hits.map((h) => h.id), index: 0 };
    art.onClick(hits, { x: 76, y: 75, scale: 1 });
    assert.equal(art.selectedId, "art_debris_0001");
    assert.equal(menus.length, 1);
});

await check("the progress bar shows the job's share and phase, and Play is held", async () => {
    calls.length = 0;
    elements.qc_art_run.click();
    await flush();
    same(calls.slice(0, 2), [["run", {}], ["job", "j1"]]);
    assert.equal(elements.qc_art_progress.hidden, false);
    assert.equal(elements.qc_art_fill.style.transform, "scaleX(0.250)");
    assert.equal(elements.qc_art_phase.textContent, "25% · Reading channels (3/12)");
    assert.equal(elements.qc_art_run.getAttribute("aria-disabled"), "true");
    assert.equal(elements.qc_tool_art.dataset.state, "running");
});

await check("a finished run is refreshed and its objects fetched once per result", async () => {
    calls.length = 0;
    jobAnswer = { status: "done", progress: { done: 12, total: 12 } };
    results = { ...results, fingerprint: "fp2" };
    await flush();
    assert.equal(art.job, null);
    assert.equal(elements.qc_art_progress.hidden, true);
    same(calls.filter((c) => c[0] !== "job"), [["status"], ["objects"]]);
    assert.equal(art.objectsFingerprint, "fp2");
});

await check("a running job in the status is followed", async () => {
    calls.length = 0;
    job = { job_id: "j9", status: "running", progress: { done: 1, total: 4 } };
    jobAnswer = { status: "running", progress: { done: 2, total: 4, message: "Refining objects (1/2)" } };
    art.adopt(status());
    await flush();
    assert.equal(art.job.job_id, "j9");
    assert.ok(calls.some((c) => c[0] === "job" && c[1] === "j9"));
    job = null;
    jobAnswer = { status: "failed", error: { message: "no tissue" } };
    await flush();
    assert.equal(art.job, null);
});

await check("stale and failed runs say so in the note", () => {
    assert.equal(elements.qc_art_status.textContent, "no tissue");
    assert.equal(elements.qc_art_status.classList.contains("is-error"), true);
    art.error = null;
    results = { ...results, stale: true };
    art.adopt(status());
    assert.equal(elements.qc_art_status.textContent, "Stale: the image changed since the last run");
    assert.match(elements.qc_art_run.title, /image changed/);
});

await check("the settings menu writes the kept regions to ROI QC after a confirm", async () => {
    menus.length = 0;
    calls.length = 0;
    const anchor = element();
    art.openMenu(anchor);
    const items = menus[0].items;
    same(items.map((i) => i.label), ["Fill regions", "Automatic thresholds", "Run again",
                                     "All channels", "Add artifact regions to ROI QC…"]);
    await items[4].onSelect();
    await flush();
    assert.equal(confirmed.length, 1);
    same(calls.filter((c) => c[0] === "write"), [["write", {}]]);
    assert.equal(messages.at(-1), "2 artifact regions added to ROI QC");
    assert.equal(reloads, 1);
});

await check("summary() reports what get_state needs", () => {
    const s = JSON.parse(JSON.stringify(art.summary()));
    assert.equal(s.fingerprint, "fp2");
    assert.equal(s.stale, true);
    same(s.shown, []);
    same(s.categories.map((c) => [c.key, c.threshold, c.n_retained]),
         [["fold", 0.5, 1], ["tear", 0.5, 0], ["debris", 0.4137, 1], ["saturation", 0.5, 1]]);
    assert.equal(s.selected, "art_debris_0001");
    assert.equal(s.muted, false);
});
