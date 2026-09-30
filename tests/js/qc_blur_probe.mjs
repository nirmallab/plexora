/**
 * Blur QC in the QC panel: one line per DNA channel, each with its own
 * threshold slider and colour, and a + for the next DNA channel.
 *
 * What is worth pinning is where the lines could bleed into each other or
 * drift from the server: a slider that previews or stores another channel's
 * threshold, a value echoed back into the slider it came from, a drag that
 * stores every tick, a typed value rounded to the box's two decimals, a
 * colour that does not reach its own slider, a + that lists a channel
 * without measuring it, a remove that takes the wrong channel, and toggles (the heatmap, the regions, one channel's
 * eye) that are really one.
 *
 * The real class from qcBlur.js, in a vm with a hand-built document, slider,
 * swatch and API.
 *
 * Run directly: `node tests/js/qc_blur_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "plugins", "qc", "static", "qcBlur.js");

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

const elements = Object.fromEntries(["qc_tool_blur", "qc_blur_list", "qc_blur_add",
    "qc_blur_run", "qc_blur_view_heat", "qc_blur_eye", "qc_blur_edit", "qc_blur_summary",
    "qc_blur_status", "qc_blur_progress", "qc_blur_cancel"].map((id) => [id, element()]));

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

const pickers = [];
class ColorSwatchPicker {
    constructor(mount, options) {
        this.mount = mount;
        this.options = options;
        this.value = options.value;
        pickers.push(this);
    }
    setValue(hex) { this.value = hex; }
    destroy() {}
}

const selects = [];
class SearchableSelect {
    constructor(mount, options) {
        this.mount = mount;
        this.options = options;
        this.names = [...options.options];
        this.value = options.value;
        this.opened = 0;
        selects.push(this);
    }
    setOptions(names) { this.names = [...names]; }
    open() { this.opened += 1; }
    destroy() { this.destroyed = true; }
}

const menus = [];
const QcTree = { menu: (anchor, items, opts) => menus.push({ anchor, items, opts }),
                 closePopup() {} };

const CANDIDATES = ["DNA_1", "DNA_2", "DNA_3", "DNA_4"];
const COLORS = ["#f97316", "#ec4899", "#22e6e6", "#ffd60a"];
let shown = ["DNA_1", "DNA_2", "DNA_3"];

function result(channel, i) {
    return { channel, color: COLORS[i], stale: false,
             threshold: { value: 0.35, auto: 0.35, source: "auto" },
             evaluation: { blurred_pct: 1.5 * i, n_regions: i },
             summary: { fingerprint: `fp_${channel}`, status: "ok", channel,
                        auto_threshold: 0.35, global_blur: { possible: false } } };
}

function status() {
    return { available: true, brightfield: false, shown: [...shown], candidates: CANDIDATES,
             channels: [...CANDIDATES, "CD3"], results: shown.map(result), job: null };
}

const calls = [];
const api = {
    blur: async () => { calls.push(["status"]); return { ok: true, data: { blur_qc: status() } }; },
    blurMask: async (params) => { calls.push(["mask", params]);
        return { ok: true, data: { available: true, channel: params.channel,
                                   fingerprint: `fp_${params.channel}`, regions: [],
                                   threshold: params.threshold ?? 0.35, blurred_pct: 3.2,
                                   n_regions: 1 } }; },
    blurSet: async (body) => { calls.push(["set", body]);
        if (Array.isArray(body.channels)) shown = [...body.channels];
        return { ok: true, data: { channel: body.channel, shown,
                                   threshold: { value: body.threshold, auto: 0.35,
                                                source: "user" } } }; },
    blurRun: async (body) => { calls.push(["run", body]); return { ok: true, data: { job_id: "j1" } }; },
    blurMap: async (params) => { calls.push(["map", params]);
        return { ok: true, data: { available: false } }; },
    job: async () => ({ ok: true, data: { job: { status: "running", progress: {} } } }),
};

let invalidations = 0;
const sandbox = {
    console, Math, Number, JSON, Object, Array, Map, Set, Promise, String, Uint8Array, RegExp,
    PlexoraSlider, ColorSwatchPicker, SearchableSelect, QcTree,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    document: {
        getElementById: (id) => elements[id] || null,
        createElement: (tag) => element(tag),
    },
    window: {
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        setTimeout: (fn) => { const id = nextTimer++; timers.set(id, fn); return id; },
        clearTimeout: (id) => timers.delete(id),
    },
};
sandbox.window.document = sandbox.document;
createContext(sandbox);
runInContext(readFileSync(SOURCE, "utf8") + "\nthis.QcBlurQc = QcBlurQc;", sandbox);
const { QcBlurQc } = sandbox;

const ctx = {
    datasource: "demo",
    layers: { addOverlay: () => ({ invalidate: () => { invalidations++; }, remove() {} }) },
};
const host = { message() {}, reload() {} };
const blur = new QcBlurQc(ctx, api, host);
blur.setup();

async function flush() {
    for (let round = 0; round < 5; round++) {
        const pending = [...timers.entries()];
        timers.clear();
        for (const [, fn] of pending) await fn();
        await new Promise((resolve) => setImmediate(resolve));
    }
}

// Objects made inside the vm have its prototypes: compared as JSON.
const same = (actual, expected) => assert.equal(JSON.stringify(actual), JSON.stringify(expected));
const sliderOf = (channel) => blur.rows.get(channel).slider;
const rowNames = () => elements.qc_blur_list.children.map((row) => row.dataset.channel);

function check(name, fn) {
    return Promise.resolve(fn()).then(() => console.log(`ok  ${name}`));
}

blur.adopt(status());
await flush();

await check("the first three DNA channels are listed, each with its own slider and colour", () => {
    same(rowNames(), ["DNA_1", "DNA_2", "DNA_3"]);
    same(shown.map((c) => sliderOf(c).options.ariaLabel),
         ["DNA_1 blur threshold", "DNA_2 blur threshold", "DNA_3 blur threshold"]);
    same(shown.map((c) => sliderOf(c).accent), COLORS.slice(0, 3));
    same(shown.map((c) => blur.rows.get(c).picker.value), COLORS.slice(0, 3));
    assert.equal(elements.qc_blur_add.disabled, false);
    assert.equal(elements.qc_blur_add.title, "Add a channel");
    // Each line's select: its own channel, then the unlisted ones, DNA first.
    same(blur.rows.get("DNA_1").select.names, ["DNA_1", "DNA_4", "CD3"]);
});

await check("a row's slider previews its own channel's mask and never stores", async () => {
    calls.length = 0;
    const slider = sliderOf("DNA_2");
    slider.options.onInput(0.4);
    slider.options.onInput(0.41);
    slider.options.onInput(0.42);
    await flush();
    same(calls, [["mask", { channel: "DNA_2", threshold: 0.42 }]]);
    assert.ok(invalidations > 0);
});

await check("the release stores that channel's threshold once, typed in full", async () => {
    calls.length = 0;
    const slider = sliderOf("DNA_2");
    slider.options.onChange(0.4137);
    await flush();
    same(calls.filter((c) => c[0] === "set"), [["set", { channel: "DNA_2", threshold: 0.4137 }]]);
    assert.equal(blur.rows.get("DNA_2").value, 0.4137);
    assert.equal(blur.rows.get("DNA_1").value, 0.35);
    assert.equal(slider.options.display(0.4137), "0.41");
    assert.equal(slider.options.step, 0.01);
});

await check("a value from outside moves only its own slider, and none is echoed back", () => {
    for (const c of shown) sliderOf(c).sets.length = 0;
    blur.setThreshold(blur.rows.get("DNA_3"), 0.37);
    same(sliderOf("DNA_3").sets.at(-1), [0.37, { silent: true }]);
    assert.equal(sliderOf("DNA_1").sets.length + sliderOf("DNA_2").sets.length, 0);
    sliderOf("DNA_3").sets.length = 0;
    blur.setThreshold(blur.rows.get("DNA_3"), 0.52, { from: "slider" });
    assert.equal(sliderOf("DNA_3").sets.length, 0);
});

await check("a colour picked is that channel's and recolours its slider", async () => {
    calls.length = 0;
    blur.rows.get("DNA_1").picker.options.onChange("#123456");
    await flush();
    same(calls.filter((c) => c[0] === "set"), [["set", { channel: "DNA_1", color: "#123456" }]]);
    assert.equal(sliderOf("DNA_1").accent, "#123456");
    assert.equal(sliderOf("DNA_2").accent, COLORS[1]);
});

await check("+ adds an empty line whose pick lists the channel and, the others measured, measures it", async () => {
    calls.length = 0;
    elements.qc_blur_add.click();
    await flush();
    same(calls, []);
    const draft = blur.draft;
    assert.ok(draft && draft.select.opened === 1);
    assert.equal(elements.qc_blur_list.children.at(-1), draft.nodes.row);
    same(draft.select.names, ["DNA_4", "CD3"]);
    assert.equal(draft.select.options.emptyLabel, "Select channel…");
    // A second + opens the same empty line, never another.
    elements.qc_blur_add.click();
    assert.equal(blur.draft, draft);
    assert.equal(draft.select.opened, 2);
    draft.select.options.onChange("DNA_4");
    await flush();
    same(calls.filter((c) => c[0] !== "mask"), [
        ["set", { channels: ["DNA_1", "DNA_2", "DNA_3", "DNA_4"] }], ["status"],
        ["run", { channel: "DNA_4" }]]);
    assert.equal(blur.draft, null);
    same(rowNames(), ["DNA_1", "DNA_2", "DNA_3", "DNA_4"]);
});

await check("a line's select swaps its channel in place", async () => {
    calls.length = 0;
    blur.job = null;
    blur.rows.get("DNA_4").select.options.onChange("CD3");
    await flush();
    same(calls.filter((c) => c[0] === "set"), [["set", { channels: ["DNA_1", "DNA_2", "DNA_3", "CD3"] }]]);
    same(rowNames(), ["DNA_1", "DNA_2", "DNA_3", "CD3"]);
    shown = ["DNA_1", "DNA_2", "DNA_3", "DNA_4"];
    await blur.refresh();
});

await check("a line's remove takes only that channel off the list", async () => {
    calls.length = 0;
    blur.rows.get("DNA_2").nodes.remove.click();
    await flush();
    same(calls.filter((c) => c[0] === "set"), [["set", { channels: ["DNA_1", "DNA_3", "DNA_4"] }]]);
    same(rowNames(), ["DNA_1", "DNA_3", "DNA_4"]);
    assert.equal(calls.some((c) => c[0] === "run"), false);
});

await check("the heatmap, the regions and each channel's eye are toggled independently", () => {
    const before = { heat: blur.view.heat, mask: blur.view.mask };
    blur.setView("heat");
    assert.equal(blur.view.heat, !before.heat);
    assert.equal(blur.view.mask, before.mask);
    blur.setView("mask");
    assert.equal(blur.view.mask, !before.mask);
    assert.equal(blur.view.heat, !before.heat);
    blur.toggleChannel("DNA_3");
    assert.equal(blur.visible("DNA_3"), false);
    assert.equal(blur.visible("DNA_1"), true);
    assert.equal(blur.view.heat, !before.heat);
    assert.equal(blur.rows.get("DNA_3").nodes.row.classList.contains("is-hidden"), true);
    assert.equal(blur.rows.get("DNA_1").nodes.row.classList.contains("is-hidden"), false);
});
