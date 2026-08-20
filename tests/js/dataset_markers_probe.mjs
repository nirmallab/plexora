/**
 * Which columns a plugin is told it may threshold.
 *
 * A CSV puts marker intensities and per-cell measurements in one header, which
 * is the entire reason the import step asks the user to split them. Nothing
 * about the numbers themselves draws that line: Area, Eccentricity and a
 * numeric slide label are as float-valued and as histogram-able as CD3 is.
 *
 * Gating derived its own list -- "every column with a histogram that is not
 * id/x/y" -- and so offered a threshold slider for every one of those. The
 * recorded split was sitting right there in the project record, unread.
 *
 * This drives the real getter out of datasetContext.js rather than restating
 * its rule, so a version that quietly went back to deriving fails here.
 *
 * Run directly: `node tests/js/dataset_markers_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora", "client", "src", "js", "services", "datasetContext.js");

const sandbox = { window: {} };
createContext(sandbox);
runInContext(readFileSync(SOURCE, "utf8"), sandbox);
const PlexoraDataset = sandbox.window.PlexoraDataset;
assert.ok(PlexoraDataset, "datasetContext.js did not define window.PlexoraDataset");

/** The getter runs inside the vm, so an array it builds belongs to that realm
 *  and is not deepStrictEqual to one built here. Copy before comparing. */
const markersOf = (dataset) => [...dataset.table.markers];

/** Per-column statistics as get_datasource_description() returns them. Every
 *  numeric column gets a histogram -- that is exactly why it cannot be the
 *  thing that decides what a marker is. */
function description(...columns) {
    return Object.fromEntries(columns.map((column) => [
        column, { min: 0, max: 10, histogram: [{ x: 1, y: 0.5 }] },
    ]));
}

const CSV_COLUMNS = ["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity", "CD3", "DAPI"];

function csvProject(columns) {
    return {
        name: "proj",
        imageData: [],
        dataset: {
            type: "csv",
            roles: { cell_id: "CellID", x: "X_centroid", y: "Y_centroid" },
            ...(columns ? { columns } : {}),
        },
    };
}

const passed = [];
function check(label, fn) {
    fn();
    passed.push(label);
    console.log("ok -", label);
}

check("the recorded split decides, not which columns happen to be numeric", () => {
    const dataset = PlexoraDataset.build(
        csvProject({
            markers: ["CD3", "DAPI"],
            metadata: ["CellID", "X_centroid", "Y_centroid", "Area", "Eccentricity"],
        }),
        {},
        description(...CSV_COLUMNS),
    );

    assert.deepEqual(markersOf(dataset), ["CD3", "DAPI"]);
    // The two that used to slip through: numeric, histogram-able, and not a
    // marker. Naming them keeps the failure message honest about the bug.
    for (const measurement of ["Area", "Eccentricity"]) {
        assert.ok(!markersOf(dataset).includes(measurement),
            `${measurement} is a per-cell measurement, not something to threshold`);
    }
});

check("a recorded marker the loaded table no longer holds is dropped", () => {
    // The split outlives the file it was recorded for -- swapping the data
    // file, or editing it outside Plexora, leaves answers naming columns that
    // are gone. A gate needs a range and a histogram to draw.
    const dataset = PlexoraDataset.build(
        csvProject({ markers: ["CD3", "CD8_removed"], metadata: [] }),
        {},
        description("CellID", "X_centroid", "Y_centroid", "CD3"),
    );

    assert.deepEqual(markersOf(dataset), ["CD3"]);
});

check("an unclassified project still gets a usable list", () => {
    // The fallback, and only the fallback: a project registered before the
    // classification screen ran has no answer to prefer, and an empty panel is
    // worse than a guess. Roles are still kept out of it.
    const dataset = PlexoraDataset.build(
        csvProject(null), {}, description(...CSV_COLUMNS, "id"));

    assert.deepEqual(markersOf(dataset),
        ["Area", "Eccentricity", "CD3", "DAPI"]);
});

check("a split with nothing describable in it falls back rather than emptying", () => {
    // Every recorded marker gone means the answer is unusable, not that this
    // project has no markers -- the panel would open with no rows at all and
    // nothing on screen to explain why.
    const dataset = PlexoraDataset.build(
        csvProject({ markers: ["gone_a", "gone_b"], metadata: [] }),
        {},
        description("CellID", "X_centroid", "Y_centroid", "CD3"),
    );

    assert.deepEqual(markersOf(dataset), ["CD3"]);
});

console.log(`\n${passed.length} checks passed`);
