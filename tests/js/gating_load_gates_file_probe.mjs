/**
 * Load Gates: a gates CSV read into the Thresholding panel is the panel's
 * gates afterwards -- on screen, in the cells drawn, and saved.
 *
 * Every way in -- the panel's Load Gates button (the hidden file input's
 * onchange), a file dropped on the list, and a file dropped on the desktop
 * window and confirmed through the drop router (the `upload_gates` executor)
 * -- ends in CSVGatingList#applyGates('file'); the desktop drop goes through
 * uploadGatesFrom, which is what this probe calls. The panel itself is the
 * GatingSidebarController: one marker on screen, its slider, and the
 * debounced save (scheduleSaveGating -> persistGatingList -> saveGatingList).
 *
 * The real CSVGatingList and GatingSidebarController (constructed with their
 * real constructors), the real SimpleEventHandler and the real gating
 * definition's bindEvents (which wires RESET_GATINGLIST to resetGatingList),
 * run in a vm context. What init() builds for the off-screen legacy list --
 * a row per marker whose click runs toggleChannelPanel, and a slider per
 * marker -- is built here by hand, the way init() builds it. The server is a
 * stub that answers the uploaded file's rows and records every save.
 *
 * Run directly: `node tests/js/gating_load_gates_file_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora", "plugins", "gating", "static");
const EVENTS = join(REPO, "plexora", "client", "src", "js", "services", "simpleEventHandler.js");

// -- a page just big enough for the two classes ------------------------------

const timers = [];
const registered = new Map();

/** The few lodash calls csvGatingList.js makes. */
const lodash = {
    each(collection, fn) {
        if (Array.isArray(collection)) collection.forEach((value, index) => fn(value, index));
        else Object.keys(collection || {}).forEach((key) => fn(collection[key], key));
    },
    size: (value) => (Array.isArray(value) ? value.length : Object.keys(value || {}).length),
    get: (object, key) => (object ? object[key] : undefined),
};

/** d3, for what these two classes do with it: toggle a class, set a style. */
function d3Chain(target) {
    const chain = {
        attr: () => chain,
        property: () => chain,
        data: () => chain,
        append: () => d3Chain(null),
        select: () => d3Chain(null),
        selectAll: () => d3Chain(null),
        remove: () => chain,
        style(name, value) {
            if (target && target.style && value !== undefined) target.style[name] = value;
            return chain;
        },
        classed(name, on) {
            if (target && target.classList) target.classList[on ? "add" : "remove"](name);
            return chain;
        },
    };
    return chain;
}

/** Elements by id the legacy list's rows answer to (`#csv_gating-slider_<id>`). */
const byId = new Map();

function element(classes = [], tagName = "DIV") {
    const set = new Set(classes);
    const node = {
        tagName,
        parent: null,
        children: [],
        listeners: [],
        style: {},
        text: "",
        get textContent() { return this.text; },
        classList: {
            add: (name) => set.add(name),
            remove: (name) => set.delete(name),
            contains: (name) => set.has(name),
        },
        appendChild(child) { child.parent = node; node.children.push(child); return child; },
        matches(selector) {
            if (selector.startsWith(".")) return set.has(selector.slice(1));
            return node.tagName.toLowerCase() === selector.toLowerCase();
        },
        closest(selector) {
            for (let at = node; at; at = at.parent) if (at.matches(selector)) return at;
            return null;
        },
        querySelector(selector) {
            for (const child of node.children) {
                if (child.matches(selector)) return child;
                const found = child.querySelector(selector);
                if (found) return found;
            }
            return null;
        },
        addEventListener(type, fn) { if (type === "click") node.listeners.push(fn); },
        /** A click bubbles from here up to the row, as a DOM click does. */
        click() {
            const event = { target: node, stopPropagation() {} };
            for (let at = node; at; at = at.parent) at.listeners.forEach((fn) => fn(event));
        },
    };
    return node;
}

const sandbox = {
    console,
    EventTarget,
    CustomEvent,
    Event,
    _: lodash,
    d3: { select: (target) => d3Chain(typeof target === "object" ? target : null) },
    document: {
        getElementById: () => null,
        querySelector: (selector) => byId.get(selector.replace(/^#/, "")) || null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        removeEventListener: () => {},
    },
    FormData: class { append() {} },
    // The plugin's server client, built by both constructors: every method is
    // the shared fake server below, put on each instance after construction.
    GatingApi: class { constructor(ctx) { this.ctx = ctx; } },
    Plexora: { registerPlugin: (definition) => registered.set(definition.name, definition) },
    PlexoraDesktop: { fileFromPath: async (path) => ({ name: path.split("/").pop(), path }) },
    addEventListener: () => {},
    removeEventListener: () => {},
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (handle) => { if (timers[handle - 1]) timers[handle - 1].fn = () => {}; },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
createContext(sandbox);
runInContext(readFileSync(EVENTS, "utf8") + "\nglobalThis.SimpleEventHandler = SimpleEventHandler;",
             sandbox, { filename: EVENTS });
runInContext(
    readFileSync(join(STATIC, "csvGatingList.js"), "utf8")
    + "\n" + readFileSync(join(STATIC, "gatingSidebarController.js"), "utf8")
    + "\nglobalThis.GatingSidebarController = GatingSidebarController;"
    + "\nglobalThis.CSVGatingList = CSVGatingList;",
    sandbox);
const { CSVGatingList, GatingSidebarController, SimpleEventHandler } = sandbox;
const definition = registered.get("gating");
assert.ok(definition?.bindEvents, "csvGatingList.js registers the gating plugin with bindEvents");

// -- the sample: three markers, B on screen and gated by hand -----------------

const MARKERS = ["A", "B", "C"];
const DESCRIPTION = { A: { min: 0, max: 100 }, B: { min: 0, max: 100 }, C: { min: 0, max: 100 } };

/** A gates CSV exported from another sample, as the upload route answers it:
 *  A and B gated, C left ungated (an empty `thresholded` cell). */
const UPLOADED = [
    { channel: "A", gate_start: 20, gate_end: 100, thresholded: true, gated_by: "User" },
    { channel: "B", gate_start: 55, gate_end: 100, thresholded: true, gated_by: "User" },
    { channel: "C", gate_start: 0, gate_end: 100, thresholded: null, gated_by: null },
];

const dataLayer = {
    getFullChannelName: (name) => name,
    getShortChannelName: (name) => name,
    gateDecimals: () => 1,
    gateValueDecimals: () => 1,
};

/** Run every timer that is due (the 400 ms save among them) and let the
 *  promises they start settle, until nothing is left. */
async function drain() {
    for (let round = 0; round < 20; round += 1) {
        await new Promise((resolve) => setImmediate(resolve));
        const due = timers.splice(0, timers.length);
        if (!due.length) return;
        due.forEach((timer) => timer.fn());
    }
}

/** The panel after `init`, with B picked and gated by hand at [40, 100], that
 *  gate saved, and nothing pending. */
async function panel() {
    byId.clear();
    timers.length = 0;
    const saves = [];
    let stored = [];
    const server = {
        submitGatingUpload: async () => ({ success: true }),
        getUploadedGatingCsvValues: async () => UPLOADED.map((row) => ({ ...row })),
        getSavedGatingList: async () => stored.map((row) => ({ ...row })),
        saveGatingList: async (channels, selections) => {
            saves.push({ channels: JSON.parse(JSON.stringify(channels)),
                         selections: JSON.parse(JSON.stringify(selections)) });
            stored = Object.entries(channels).map(([channel, [gate_start, gate_end]]) => ({
                channel, gate_start, gate_end, gate_active: channel in selections }));
            return { success: true };
        },
        getGateProvenance: async () => ({}),
        getGatingGMM: async () => ({}),
    };
    const eventHandler = new SimpleEventHandler(new EventTarget());
    const dataset = { image: { index: {}, has: () => false },
                      table: { markers: [...MARKERS], sourceKind: "csv" },
                      schema: { cellId: "id" } };
    const list = new CSVGatingList({
        config: { maxSelections: 4 }, columns: [...MARKERS], eventHandler, dataLayer,
        dataset, datasource: "sample_2", channelList: null, url: (path) => path,
    });
    Object.assign(list.api, server);

    // What init() leaves behind for the off-screen legacy list.
    list.databaseDescription = DESCRIPTION;
    list.columns = [...MARKERS];
    MARKERS.forEach((column, index) => {
        const channelID = `channel_${index}`;
        list.gatingIDs[column] = channelID;
        const row = element(["list-group-item", "container", "gating-list-content"]);
        const name = row.appendChild(element(["gating-name", "list-button"], "SPAN"));
        name.text = column;
        const svgCol = row.appendChild(element(["gating-col", "col-svg-wrapper"]));
        const sliderCol = row.appendChild(element(["csv_gating-slider"]));
        byId.set(`csv_gating-slider_${channelID}`, sliderCol);
        row.addEventListener("click", (event) => list.toggleChannelPanel(event, svgCol));
        list.gating_channels[column] = [DESCRIPTION[column].min, DESCRIPTION[column].max];
        let value = [...list.gating_channels[column]];
        list.sliders.set(column, {
            value(next) { if (next !== undefined) value = next; return value; },
            silentValue(next) { value = next; },
        });
        // A fit already drawn, so selecting a row asks the server for nothing.
        list.hasGatingGMM[column] = { gate: 50 };
    });

    definition.bindEvents({
        eventHandler, moduleInstance: list,
        seaDragonViewer: { forceRepaint() {} },
        updateSeaDragonSelection() {}, updateCentroidsForGate() {}, runSegmentationGate() {},
        onCleanup() {}, coreEvents: { RESET_LISTS: "RESET_LISTS" },
    });

    const controller = new GatingSidebarController({
        sidebar: { databaseDescription: DESCRIPTION, isRestoring: () => false,
                   setSlotMarker() {}, slotShowing: () => false, formatValue: String },
        moduleInstance: list, dataLayer, eventHandler, config: { maxSelections: 4 },
        dataset, onCleanup() {},
    });
    Object.assign(controller.api, server);
    // The slider on screen: every value the panel puts on it.
    const shown = [];
    controller.gateSlider = { set: (values) => shown.push([...values]), setBounds() {} };
    // The distribution plot is an SVG measured off the page; not drawn here.
    controller.drawGateDistribution = () => {};

    // B picked, then dragged to 40 and let go -- the panel's own path.
    controller.gateMarker = "B";
    controller.setGateRange([40, 100], CSVGatingList.events.SELECTION_CHANGED);
    await drain();
    assert.equal(saves.length, 1, "the hand-set gate on B was saved before the load");
    assert.deepEqual(JSON.parse(JSON.stringify(list.selections)), { B: [40, 100] });
    saves.length = 0;
    shown.length = 0;
    return { list, controller, saves, shown };
}

/** Load the other sample's gates the way a confirmed desktop drop does. */
async function loadGates(list) {
    await list.uploadGatesFrom("C:/data/sample_1_gated_channel_ranges.csv");
    await drain();
}

const passed = [];
const failed = [];
async function check(label, fn) {
    try {
        await fn();
    } catch (error) {
        failed.push(label);
        console.log(`FAIL ${label}\n    ${String(error.message).split("\n").join("\n    ")}`);
        return;
    }
    passed.push(label);
    console.log(`PASS ${label}`);
}

await check("after Load Gates the cells are gated by the marker on screen alone", async () => {
    const { list } = await panel();
    await loadGates(list);
    assert.deepEqual(Object.keys(list.selections), ["B"],
        `the drawn gate holds ${JSON.stringify(list.selections)}`);
    assert.deepEqual([...list.selections.B], [55, 100]);
});

await check("after Load Gates the slider shows the loaded gate of the marker on screen", async () => {
    const { list, shown } = await panel();
    await loadGates(list);
    assert.ok(shown.length > 0, "the panel's slider was never told about the loaded gates");
    assert.deepEqual(shown[shown.length - 1], [55, 100]);
});

await check("Load Gates saves the loaded gates with the sample", async () => {
    const { list, saves } = await panel();
    await loadGates(list);
    assert.ok(saves.length > 0, "nothing was saved after the gates were loaded");
    const last = saves[saves.length - 1];
    assert.deepEqual(last.channels.A, [20, 100]);
    assert.deepEqual(last.channels.B, [55, 100]);
});

if (failed.length) {
    console.log(`${failed.length} checks failed`);
    process.exitCode = 1;
} else {
    console.log(`all checks passed (${passed.length})`);
}
