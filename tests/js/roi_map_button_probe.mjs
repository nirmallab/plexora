/**
 * When does "Map to cells" appear, and what does it send?
 *
 * The button writes two columns onto the user's own rows, so both halves of its
 * visibility rule matter and both fail silently:
 *
 *   shown with no table  -- the click can only ever produce an error, and the
 *                           user has no way to tell what they were meant to do
 *                           first. The project has no cells; there is nothing
 *                           to map onto.
 *   shown with no ROIs   -- the same shape of bug the save button already
 *                           guards against, with a worse result: it would write
 *                           two empty columns over every cell in the file.
 *
 * The third check is the prefix. The columns are derived from the destination
 * name, and `destinationName()` -- the one the save button uses -- falls back to
 * `default_name`, which is `plexora_rois` for a SpatialData project. Reusing it
 * here would silently produce `plexora_rois_category`. `mapPrefix()` exists to
 * fall back to nothing instead and let the server supply `rois` for every
 * format, so the two ends cannot drift.
 *
 * Run directly:  node tests/js/roi_map_button_probe.mjs
 *   --source <path>   probe a different roiSidebarController.js
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1
    ? join(REPO, "plexora/plugins/roi/static/roiSidebarController.js")
    : process.argv[sourceArg + 1];

const checks = [];
const failures = [];

function check(what, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    checks.push(what);
    if (!ok) failures.push({ what, actual, expected });
}

/**
 * The smallest DOM the controller's render path touches.
 *
 * Deliberately not a real one: every element added here is an element this
 * probe stops noticing the absence of, and the panel's markup is the template's
 * business rather than this file's.
 */
function fakeElement(id) {
    return {
        id,
        hidden: false,
        disabled: false,
        value: "",
        textContent: "",
        _attrs: {},
        setAttribute(name, value) { this._attrs[name] = value; },
        addEventListener() {},
    };
}

const IDS = [
    "roi_save_to_source", "roi_export_download", "roi_map_to_cells",
    "roi_map_info", "roi_destination", "roi_destination_name",
    "roi_destination_hint", "roi_save_to_source_label",
];

function loadController() {
    const context = createContext({
        window: {}, document: { getElementById: () => null },
        console, setTimeout, clearTimeout, JSON, Math, Object, Array, String,
        Number, Boolean, Promise, crypto: { randomUUID: () => "x" },
    });
    const code = readFileSync(SOURCE, "utf8")
        + ";globalThis.__cls = RoiSidebarController;";
    runInContext(code, context);
    return context.__cls;
}

const RoiSidebarController = loadController();

/**
 * A controller with its render inputs stubbed, standing far enough up that
 * renderSourceButton() is the real one.
 */
function panel({ hasTable, kind = null, features = 0, remembered = "", typed = "" }) {
    const elements = new Map(IDS.map((id) => [id, fakeElement(id)]));
    const controller = Object.create(RoiSidebarController.prototype);
    controller.el = (id) => elements.get(id) || null;
    controller.store = { features: new Array(features).fill({}) };
    controller._destinationOpen = false;
    controller._destination = {
        kind, default_name: kind === "spatialdata" ? "plexora_rois" : "rois",
        remembered, existing: [], hasTable, replaceOnce: false,
    };
    elements.get("roi_destination_name").value = typed;
    controller.renderSourceButton();
    return { controller, elements };
}

// -- the gate ------------------------------------------------------------

{
    const { elements } = panel({ hasTable: false, features: 2 });
    check("no cell data means no Map to cells button",
        elements.get("roi_map_to_cells").hidden, true);
    check("...and no ? explaining a button that is not there",
        elements.get("roi_map_info").hidden, true);
}

{
    const { elements } = panel({ hasTable: true, features: 0 });
    check("nothing drawn means no Map to cells button",
        elements.get("roi_map_to_cells").hidden, true);
    check("...and the ? goes with it",
        elements.get("roi_map_info").hidden, true);
}

{
    const { controller, elements } = panel({ hasTable: true, features: 2 });
    check("cells plus regions is what the button is for",
        elements.get("roi_map_to_cells").hidden, false);
    check("...and the ? that explains it",
        elements.get("roi_map_info").hidden, false);
    check("...and it knows which columns it will write",
        controller.mapColumnNames(), ["rois_category", "rois_name"]);
}

{
    // A CSV project: no native destination, so no save button -- and cells all
    // the same, so the mapping button stands on its own.
    const { elements } = panel({ hasTable: true, features: 2, kind: null });
    check("a CSV project gets the GeoJSON download",
        elements.get("roi_export_download").hidden, false);
    check("...and no native save button",
        elements.get("roi_save_to_source").hidden, true);
    check("...and Map to cells regardless",
        elements.get("roi_map_to_cells").hidden, false);
}

// -- the prefix ----------------------------------------------------------

{
    const { controller } = panel({ hasTable: true, features: 1, kind: "spatialdata" });
    check("a SpatialData project does not inherit plexora_rois as a column prefix",
        controller.mapPrefix(), "");
}

{
    const { controller } = panel({
        hasTable: true, features: 1, kind: "spatialdata" });
    check("...so the names shown are what the server will actually write",
        controller.mapColumnNames(), ["rois_category", "rois_name"]);
    check("...and the prefix is still blank", controller.mapPrefix(), "");
}

{
    const { controller } = panel({
        hasTable: true, features: 1, kind: "anndata", typed: "pass2" });
    check("what the user typed is what the columns are named from",
        controller.mapPrefix(), "pass2");
    check("...and the names follow it",
        controller.mapColumnNames(), ["pass2_category", "pass2_name"]);
}

{
    const { controller } = panel({
        hasTable: true, features: 1, kind: "anndata", remembered: "reviewer_b" });
    check("where this project last saved is the default prefix",
        controller.mapPrefix(), "reviewer_b");
}

const report = {
    source: SOURCE.replace(REPO + "/", ""),
    checked: checks.length,
    failures,
};

console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
