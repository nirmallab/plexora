/**
 * A segmentation mask is drawable without a feature table.
 *
 * This is an ordinary project shape, not a broken one: attach an image, then
 * attach a mask from the edit page, and that is exactly what you have. The mask
 * carries its own cell ids -- renderLabelTile reads them out of the label
 * pyramid -- so nothing about drawing outlines or filled cells needs a row per
 * cell. Cell ids are what PLUGINS need (Thresholding gates on them, Cell
 * Explorer colours by them), and those tools are already gated on a table
 * existing.
 *
 * NumericData.fetchCells destructured `this.schema` unguarded, so the first
 * click on Outlines threw `Cannot destructure property 'cellId' of 'this.schema'
 * as it is null` before the pyramid was ever requested. The mask never drew and
 * the failure surfaced as a TypeError in the console -- ViewerControls caught
 * it, saw a mask it could not load, and fell back.
 *
 * Two shapes reach here and both used to break, differently. A project with no
 * data block at all has a null schema (the destructure). A project WITH a table
 * whose x/y/cell-id roles nobody has answered has a schema full of nulls, which
 * sailed past the destructure and asked the server for columns named "null".
 *
 * Run directly:  node tests/js/mask_without_table_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const NUMERIC = join(REPO, "plexora/client/src/js/services/numericData.js");
const DATASET = join(REPO, "plexora/client/src/js/services/datasetContext.js");

/**
 * The real NumericData over the real resolveSchema, with the server end
 * recorded rather than stubbed away -- "did this round trip at all" is half of
 * what is being asserted.
 */
function build(config) {
    const requests = [];
    const win = {};
    const context = createContext({
        console, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Promise, Error, Uint32Array, Float32Array,
        window: win,
        datasource: "demo",
    });
    runInContext(readFileSync(DATASET, "utf8"), context, { filename: "datasetContext.js" });
    runInContext("globalThis.PlexoraDataset = window.PlexoraDataset;", context);
    runInContext(readFileSync(NUMERIC, "utf8"), context, { filename: "numericData.js" });
    runInContext("globalThis.__NumericData = NumericData;", context);

    const dataLayer = {
        async getAllCells(keys, useInt) {
            requests.push(keys);
            // 3 fields x 2 cells, which is what a real project would answer.
            return new Uint32Array([1, 10, 20, 2, 30, 40]).buffer;
        },
    };
    return { numeric: new context.__NumericData(config, dataLayer), requests };
}

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

// -- image + mask, and nothing else ---------------------------------------

{
    const { numeric, requests } = build({ segmentation: "/mask.zarr" });
    const { ids, centers } = await numeric.loadCells();
    check("a project with no table loads no cells instead of throwing",
        ids.length === 0 && centers.length === 0,
        "the mask carries its own ids; a row per cell is what PLUGINS need");
    check("...and does not ask the server for a table that is not there",
        requests.length === 0, `requests: ${JSON.stringify(requests)}`);
}

// -- a table whose roles nobody has answered ------------------------------

{
    const { numeric, requests } = build({
        segmentation: "/mask.zarr", dataset: { type: "csv", roles: {} } });
    const { ids, centers } = await numeric.loadCells();
    check("a table with no roles answered is treated the same way",
        ids.length === 0 && centers.length === 0);
    check("...rather than asking the server for columns named null",
        requests.length === 0, `requests: ${JSON.stringify(requests)}`);
}

{
    // Half-answered is still unanswerable: the fetch interleaves all three.
    const { numeric, requests } = build({
        segmentation: "/mask.zarr",
        dataset: { type: "csv", roles: { cell_id: "CellID", x: "X_centroid" } } });
    const { ids } = await numeric.loadCells();
    check("and so is a table missing only one of the three",
        ids.length === 0 && requests.length === 0,
        "the three are fetched interleaved -- two of them is not two thirds of an answer");
}

// -- the ordinary project still works -------------------------------------

{
    const { numeric, requests } = build({
        segmentation: "/mask.zarr",
        dataset: { type: "csv",
            roles: { cell_id: "CellID", x: "X_centroid", y: "Y_centroid" } } });
    const { ids, centers } = await numeric.loadCells();
    check("a project that HAS coordinates still fetches and deinterleaves them",
        ids.length === 2 && ids[0] === 1 && ids[1] === 2
        && centers.length === 4 && centers[0] === 10 && centers[3] === 40,
        `ids ${ids}, centers ${centers}`);
    check("...asking for the columns the project recorded, in role order",
        String(requests[0]) === "CellID,X_centroid,Y_centroid",
        `requests: ${JSON.stringify(requests)}`);
}

// -- the answer is cached either way --------------------------------------

{
    const { numeric, requests } = build({
        segmentation: "/mask.zarr",
        dataset: { type: "csv",
            roles: { cell_id: "CellID", x: "X_centroid", y: "Y_centroid" } } });
    await numeric.loadCells();
    await numeric.loadCells();
    check("loading twice round-trips once",
        requests.length === 1,
        "ensureSegmentationReady re-checks ids.length, which stays 0 for an empty table");
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
