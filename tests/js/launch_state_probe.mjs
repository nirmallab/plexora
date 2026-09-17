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

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
