/**
 * What a viewer opened with `plexora.view(..., overlay=, channels=)` shows.
 *
 * The two decisions, isolated from the machinery that acts on them:
 *
 *   ViewerSidebar.launchChannels   which requested channels this image has
 *   CellExplorerState.chooseColumn which column the panel opens on
 *
 * Both are driven through `Object.create(...prototype)` rather than a real
 * constructor, the way channel_rename_probe.mjs does: what is under test is a
 * choice over a few fields, and building either object for real needs sliders,
 * markup and a server.
 *
 * The ephemerality of all this -- that nothing here is written back to the
 * project -- is asserted on the Python side, where the persist calls are
 * (tests/test_launch_state.py).
 *
 * Run directly:  node tests/js/launch_state_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function load(file, exported, extraGlobals = {}) {
    const warnings = [];
    const context = createContext({
        console: { ...console, warn: (...args) => warnings.push(args.join(" ")) },
        Object, Array, String, Boolean, Number, Math, JSON, Set, Map, Promise,
        window: { addEventListener() {} },
        document: { getElementById: () => null },
        ...extraGlobals,
    });
    runInContext(
        `${readFileSync(join(REPO, file), "utf8")}\n;globalThis.__exported = ${exported};`,
        context, { filename: file });
    return { exported: context.__exported, context, warnings };
}

// -- which channels a launch turns on ----------------------------------------

const SIDEBAR = "plexora/client/src/js/views/viewerSidebar.js";

function sidebarWith(launch, columns, options = {}) {
    const { exported, context, warnings } = load(SIDEBAR, "ViewerSidebar");
    context.window.flaskVariables = launch === null ? undefined : { launch };
    const sidebar = Object.create(exported.prototype);
    Object.assign(sidebar, { columns, persist: options.persist !== false });
    return { sidebar, warnings };
}

{
    const { sidebar } = sidebarWith(
        { channels: [{ name: "DAPI" }, { name: "CD3", color: "#3366ff" }] },
        ["DAPI", "CD3", "CD8"]);
    const picked = sidebar.launchChannels();

    check("the requested channels come through in the order they were given",
        same(picked.map((entry) => entry.name), ["DAPI", "CD3"]),
        JSON.stringify(picked));
    check("...carrying whatever options each was given",
        picked[1].color === "#3366ff");
}

{
    const { sidebar } = sidebarWith(null, ["DAPI"]);
    check("an ordinary page load asks for nothing",
        same(sidebar.launchChannels(), []),
        "every existing viewer takes this branch");
}

{
    const { sidebar } = sidebarWith({}, ["DAPI"]);
    check("...and so does a launch that named no channels",
        same(sidebar.launchChannels(), []));
}

{
    const { sidebar, warnings } = sidebarWith(
        { channels: [{ name: "DAPI" }, { name: "Typo" }] }, ["DAPI", "CD3"]);
    const picked = sidebar.launchChannels();

    check("a channel this image does not have is dropped",
        same(picked.map((entry) => entry.name), ["DAPI"]));
    check("...and said out loud, because the two causes are a typo and a stale cell",
        warnings.some((line) => line.includes("no channel named")),
        JSON.stringify(warnings));
}

{
    const { sidebar } = sidebarWith(
        { channels: [{ name: "DAPI" }] }, ["DAPI"], { persist: false });
    check("a scoped sidebar takes no launch state at all",
        same(sidebar.launchChannels(), []),
        "Figure Builder's Quick Edit shows a panel's channels, not the page's");
}

// -- which column the overlay opens on ---------------------------------------

const STATE = "plexora/plugins/cell_explorer/static/cellExplorerState.js";

function stateWith(descriptors, saved) {
    const { exported } = load(STATE, "CellExplorerState");
    const state = Object.create(exported.prototype);
    Object.assign(state, { descriptors, settings: { selected: saved || null } });
    return state;
}

const CATALOGUE = [
    { name: "barcode", kind: "categorical", identifier_like: true },
    { name: "leiden", kind: "categorical" },
    { name: "phenotype", kind: "categorical" },
];

{
    const state = stateWith(CATALOGUE, "phenotype");
    check("what the launch asked for beats what was showing last time",
        state.chooseColumn(null, "leiden") === "leiden",
        "it is the more recent instruction and the more specific one");
}

{
    const state = stateWith(CATALOGUE, "phenotype");
    check("a request for a column this table lost falls through to the saved one",
        state.chooseColumn(null, "gone") === "phenotype",
        "a stale notebook cell must not open the panel on nothing");
}

{
    const state = stateWith(CATALOGUE, "phenotype");
    check("with no request, the saved selection still wins",
        state.chooseColumn(null, "") === "phenotype",
        "every existing viewer takes this branch");
}

{
    const state = stateWith(CATALOGUE, null);
    check("...and with neither, the project's own annotation column does",
        state.chooseColumn("phenotype", "") === "phenotype");
}

// -- a paste runs the same path, and says which slots stay off ---------------

async function applied(entries, options) {
    const { exported } = load(SIDEBAR, "ViewerSidebar", {
        plexoraMapWithLimit: async (items, _limit, fn) => Promise.all(items.map(fn)),
        plexoraChannelConcurrency: () => 2,
    });
    const sidebar = Object.create(exported.prototype);
    const marked = [];
    Object.assign(sidebar, {
        columns: ["DAPI", "CD3", "CD8"],
        channelSlots: [],
        channelSlotSliders: new Map(), colorPickers: new Map(), markerSelects: new Map(),
        maxChannelSlots: 8, initialChannelSlots: 2,
        el: () => ({ innerHTML: "", appendChild() {} }),
        getDefaultColor: () => ({ rgb: [1, 1, 1], hex: "#ffffff" }),
        getImageRange: () => [0, 255],
        createChannelSlot: () => ({}),
        channelList: {
            ensureChannelStats: async () => {}, hasChannelGMM: {}, getAndDrawChannelGMM: async () => {},
        },
        setSlotMarker(index, name, opts) {
            marked.push({ index, name, opts, autoSilent: Boolean(this.channelSlots[index].autoSilent) });
            if (opts.enable) this.channelSlots[index].enabled = true;
        },
        setSlotColor() {}, setSlotRange() {}, applySlotExpansion() {}, updateSelectedCount() {},
        isHdMode: () => true,
    });
    await (options === undefined
        ? sidebar.applyLaunchChannels(entries)
        : sidebar.applyLaunchChannels(entries, options));
    return { marked, sidebar };
}

{
    const { marked } = await applied([{ name: "DAPI" }, { name: "CD3", enabled: false, range: [1, 9] }]);
    check("a launch row turns its channel on, and a pasted off slot stays off",
        marked[0].opts.enable === true && marked[1].opts.enable === false,
        JSON.stringify(marked.map((m) => m.opts.enable)));
    check("...and a launch's auto-level is still kept off the project",
        marked[0].autoSilent === true && marked[1].autoSilent === false);
    const paste = await applied([{ name: "DAPI" }], { silent: false });
    check("a paste's auto-level is the user's edit, and is saved",
        paste.marked[0].autoSilent === false);
}

{
    const { exported } = load(SIDEBAR, "ViewerSidebar");
    const sidebar = Object.create(exported.prototype);
    Object.assign(sidebar, {
        columns: ["DAPI", "CD3", "CD8"],
        hdModeOverride: false,
        channelSlots: [
            { name: "CD3", colorHex: "#00ff00", enabled: true, visible: true, range: [10, 20] },
            { name: "DAPI", colorHex: "#0000ff", enabled: true, visible: true, range: [0, 255] },
            { name: "CD8", colorHex: "#ff0000", enabled: false, visible: true, range: [0, 255] },
            { name: "", colorHex: "#ffffff", enabled: false, visible: true, range: [0, 255] },
        ],
        quantWindow: (name) => (name === "CD3" ? { qmin: 0, qmax: 255 * 4 } : null),
    });
    const snap = sidebar.snapshotSlots();
    check("a copy records each slot's channel, position, colour and state",
        same(snap.map((s) => [s.name, s.index, s.colorHex, s.enabled]),
            [["CD3", 1, "#00ff00", true], ["DAPI", 0, "#0000ff", true], ["CD8", 2, "#ff0000", false]]));
    check("...a window only where it can be said in raw units",
        same(snap[0].range, [40, 80]) && snap[1].range === null && snap[2].range === null,
        JSON.stringify(snap.map((s) => s.range)));
}

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
