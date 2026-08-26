/**
 * The capture bridge, exercised from both sides.
 *
 * Figure Builder must not know that ROI or Cell Explorer exist, and neither of
 * them may learn about Figure Builder. All three talk through two DOM events,
 * and nothing in the Python suite can see whether they actually agree: the
 * events are dispatched and answered entirely in the browser.
 *
 * The rule this probe exists to hold is the one that is easiest to break by
 * being helpful: **a panel edit must never rewrite the project's own plugin
 * settings.** ROI's category visibility and Cell Explorer's palette are
 * PERSISTED preferences -- restoring them would edit the user's project because
 * they looked at a figure. So the bridges restore only what is transient and
 * REPORT the rest, and this checks that the reporting is honest rather than a
 * blanket "ok".
 *
 * Run directly:
 *   node tests/js/figure_bridge_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const problems = [];

const FILES = [
    join(REPO, "plexora/plugins/cell_explorer/static/cellExplorerColors.js"),
    join(REPO, "plexora/plugins/roi/static/roiFigureBridge.js"),
    join(REPO, "plexora/plugins/cell_explorer/static/cellExplorerFigureBridge.js"),
];

const listeners = new Map();

/** ROI as its controller really looks: categories with a persisted `visible`,
 *  and a renderer whose enabled flag is transient. */
function roiController() {
    return {
        renderer: {
            enabled: false,
            setEnabled(value) { this.enabled = Boolean(value); },
        },
        store: {
            categories: [
                { id: "c1", label: "Tumor", color: "#e04c4c", visible: true, sort_order: 0 },
                { id: "c2", label: "Stroma", color: "#4ca7e0", visible: false, sort_order: 1 },
            ],
            features: [{ id: "r1" }, { id: "r2" }, { id: "r3" }],
            sortedCategories() { return this.categories; },
        },
    };
}

function cellExplorerController() {
    const settings = {
        display: { mode: "filled", opacity: 0.7 },
        categorical: { phenotype: { colors: { "CD8 T": "#ff0000" }, hidden: ["Other"] } },
        continuous: {},
        overrides: {},
        selected: null,
    };
    return {
        selected: [],
        state: {
            column: "phenotype",
            settings: settings,
            descriptor: () => ({
                categories: [{ value: "CD8 T" }, { value: "Macrophage" }, { value: "Other" }],
                n_missing: 0,
            }),
            kindFor: () => "categorical",
            categorical: (column) => settings.categorical[column]
                || (settings.categorical[column] = { colors: {}, hidden: [] }),
            continuous: () => ({ palette: "viridis", custom: {}, range: { mode: "auto" }, hidden: false }),
            hiddenSet: (column) => new Set(settings.categorical[column]?.hidden || []),
            allLabels: () => ["CD8 T", "Macrophage", "Other"],
            domainFor: () => [0, 1],
        },
        select(column, options) { this.selected.push({ column, options }); },
    };
}

const roi = roiController();
const cell = cellExplorerController();

function browserGlobals() {
    const globals = {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Infinity, parseInt, isNaN,
        setTimeout: () => 1, clearTimeout: () => {},
    };
    globals.window = {
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        removeEventListener() {},
        __plexora: {
            plugins: new Map([
                ["roi", { definition: { version: "roi-v1" }, sidebarController: roi }],
                ["cell_explorer", { definition: { version: "ce-v1" }, sidebarController: cell }],
            ]),
        },
    };
    return globals;
}

const ctx = createContext(browserGlobals());
for (const file of FILES) {
    runInContext(readFileSync(file, "utf8"), ctx, { filename: file });
}

function dispatch(type, detail) {
    for (const handler of listeners.get(type) || []) handler({ type, detail });
}

function check(label, actual, expected) {
    const a = JSON.stringify(actual);
    const b = JSON.stringify(expected);
    if (a !== b) problems.push(`${label}: expected ${b}, got ${a}`);
}

// -- capture ---------------------------------------------------------------

const contributions = {};
dispatch("plexora:figure-capture-state", {
    contribute(name, payload) { contributions[name] = payload; },
});

check("both open plugins contributed", Object.keys(contributions).sort(),
    ["cell_explorer", "roi"]);
check("each contribution carries the plugin's version",
    [contributions.roi.version, contributions.cell_explorer.version],
    ["roi-v1", "ce-v1"]);

// A contribution is STATE and nothing else. Both bridges used to compute a
// legend here as well, and Figure Builder stored it -- but its export
// re-renders channels from the source and reproduces no overlay at all, so
// those rows keyed a picture the exported figure never contained. Asserted as
// an absence rather than deleted, because "the bridge quietly started sending
// legends again" is the way this comes back.
check("neither bridge sends a legend any more",
    [contributions.roi.legend, contributions.cell_explorer.legend],
    [undefined, undefined]);
check("a contribution is a version and a state",
    Object.keys(contributions.roi).sort(), ["state", "version"]);

check("ROI records each category's visibility",
    contributions.roi.state.categories, { c1: true, c2: false });
check("Cell Explorer records which column is showing",
    contributions.cell_explorer.state.column, "phenotype");

// -- restore ---------------------------------------------------------------

const report = {};
dispatch("plexora:figure-restore-state", {
    plugins: {
        roi: { version: "roi-v1", state: { overlay_visible: true,
                                           categories: { c1: true, c2: true } } },
        cell_explorer: { version: "ce-v1", state: { column: "grade", kind: "categorical",
                                                    categorical: { hidden: ["Other"] } } },
    },
    report(name, outcome) { report[name] = outcome; },
});

// Transient, and therefore restored.
check("ROI's overlay was turned on", roi.renderer.enabled, true);
// Persisted, and therefore NOT rewritten -- c2 is still hidden, as the project
// says, even though the panel was captured with it visible.
check("ROI's persisted category visibility is untouched",
    roi.store.categories.map((c) => c.visible), [true, false]);
check("and the difference is reported rather than hidden", report.roi, "partial");

// A column can be shown without becoming the project's remembered choice.
check("Cell Explorer switched column without persisting",
    cell.selected, [{ column: "grade", options: { persist: false } }]);
// The palette and hidden rows for that column are the project's, so the
// mismatch is reported.
check("and reports that the rest was not applied", report.cell_explorer, "partial");

// -- a plugin that is not open --------------------------------------------

runInContext("window.__plexora.plugins = new Map();", ctx);
const closedContributions = {};
dispatch("plexora:figure-capture-state", {
    contribute(name, payload) { closedContributions[name] = payload; },
});
check("a plugin that is not open contributes nothing",
    Object.keys(closedContributions), []);

const closedReport = {};
dispatch("plexora:figure-restore-state", {
    plugins: { roi: { state: {} }, cell_explorer: { state: { column: "x" } } },
    report(name, outcome) { closedReport[name] = outcome; },
});
// Silence, not a false "ok": Figure Builder fills in "skipped" for anything
// that did not answer, and a bridge claiming success while its tool is closed
// would tell the user their overlay was restored when nothing was drawn.
check("and answers nothing on restore", Object.keys(closedReport), []);

console.error(JSON.stringify({ problems, contributions, report }, null, 2));
process.exit(problems.length ? 1 : 0);
