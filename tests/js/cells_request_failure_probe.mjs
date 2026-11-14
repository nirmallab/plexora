/**
 * One failed /get_all_cells is not kept for the rest of the page.
 *
 * NumericData.loadCells caches the one cells request (`cellsPromise`) for the
 * page: picking, the legacy centroids, the gating classifier and Cell
 * Explorer's ROI mapping all read it. A request that FAILED must not be what
 * is cached -- a tunnel or node blip, a server restart, or the website's
 * Python answering 503 is over a moment later, and the next caller should ask
 * again, the way getColumn and getNotCalled already do.
 *
 * Three shapes of failure, against the real DataLayer.getAllCells and the real
 * NumericData:
 *   - fetch itself rejects (network error): getAllCells swallowed it and
 *     returned undefined, `new Uint32Array(undefined)` is empty, and an EMPTY
 *     cell table was cached as if the project had no cells;
 *   - an error status with a body whose length is not a multiple of 4: the
 *     body went into `new Uint32Array(...)`, which threw, and the REJECTED
 *     promise was cached, so every later caller threw too;
 *   - an error status with a body whose length is a multiple of 4 (the
 *     website's 503 JSON): its ASCII bytes were read as cell ids and centres.
 *
 * Run directly:  node tests/js/cells_request_failure_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const DATA_LAYER = join(REPO, "plexora/client/src/js/services/dataLayer.js");
const NUMERIC = join(REPO, "plexora/client/src/js/services/numericData.js");
const DATASET = join(REPO, "plexora/client/src/js/services/datasetContext.js");

const CONFIG = {
    segmentation: "/mask.zarr",
    dataset: { type: "csv",
        roles: { cell_id: "CellID", x: "X_centroid", y: "Y_centroid" } },
};

// 3 fields x 2 cells: what a healthy server answers.
const GOOD = new Uint32Array([1, 10, 20, 2, 30, 40]).buffer;

function bytes(text) {
    const encoded = new TextEncoder().encode(text);
    return encoded.buffer.slice(encoded.byteOffset, encoded.byteOffset + encoded.byteLength);
}

function ok(buffer) {
    return { ok: true, status: 200, arrayBuffer: async () => buffer };
}

function failed(status, text) {
    return { ok: false, status, arrayBuffer: async () => bytes(text) };
}

/**
 * The real DataLayer and NumericData, with `fetch` answering from `answers`
 * in turn (a function answer is called, so it can throw like a network error).
 */
function build(answers) {
    const requests = [];
    const win = {};
    const context = createContext({
        console: { log() {}, warn() {}, error() {} },
        Object, Array, Number, String, Boolean, JSON, Set, Map, Promise, Error,
        RangeError, TypeError, Uint32Array, Float32Array, Uint8Array,
        URLSearchParams, Headers, TextEncoder,
        window: win,
        datasource: "demo",
        plexoraUrl: (path) => `/${path}`,
        fetch: async (url) => {
            requests.push(String(url));
            const answer = answers.shift();
            if (typeof answer === "function") return answer();
            return answer;
        },
    });
    runInContext(readFileSync(DATASET, "utf8"), context, { filename: "datasetContext.js" });
    runInContext("globalThis.PlexoraDataset = window.PlexoraDataset;", context);
    runInContext(readFileSync(DATA_LAYER, "utf8"), context, { filename: "dataLayer.js" });
    runInContext("globalThis.__DataLayer = DataLayer;", context);
    runInContext(readFileSync(NUMERIC, "utf8"), context, { filename: "numericData.js" });
    runInContext("globalThis.__NumericData = NumericData;", context);
    const dataLayer = new context.__DataLayer(CONFIG, []);
    return { numeric: new context.__NumericData(CONFIG, dataLayer), requests };
}

async function settle(promise) {
    try {
        return { value: await promise };
    } catch (error) {
        return { error };
    }
}

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

function describe(outcome) {
    if (outcome.error) return `rejected: ${outcome.error?.name}: ${outcome.error?.message}`;
    const { ids, centers } = outcome.value || {};
    return `resolved ids=[${ids ? Array.from(ids).slice(0, 6) : ids}] (${ids?.length}), `
        + `centers ${centers?.length}`;
}

// -- the network fails once ---------------------------------------------------

{
    const { numeric, requests } = build([
        () => { throw new TypeError("Failed to fetch"); },
        ok(GOOD),
    ]);
    const first = await settle(numeric.loadCells());
    check("a network error is not reported as a project with no cells",
        Boolean(first.error) || !(first.value?.ids?.length === 0),
        describe(first));
    const second = await settle(numeric.loadCells());
    check("after a network error the next loadCells asks the server again",
        requests.length === 2, `requests: ${requests.length}; second ${describe(second)}`);
    check("...and gets the cells once the server answers",
        second.value?.ids?.length === 2 && second.value.ids[0] === 1,
        describe(second));
}

// -- an error status whose body is not a whole number of uint32s ------------

{
    const body = "<!doctype html><title>500 Internal Server Error</title>"
        + "<h1>Internal Server Error</h1><p>The server encountered an internal error.</p>";
    const odd = body.length % 4 === 0 ? `${body} ` : body;
    const { numeric, requests } = build([failed(500, odd), ok(GOOD)]);
    const first = await settle(numeric.loadCells());
    check("an HTTP 500 makes loadCells fail rather than resolve",
        Boolean(first.error), describe(first));
    const second = await settle(numeric.loadCells());
    check("after an HTTP 500 the next loadCells asks the server again",
        requests.length === 2, `requests: ${requests.length}; second ${describe(second)}`);
    check("...and gets the cells instead of the cached failure",
        second.value?.ids?.length === 2 && second.value.ids[0] === 1,
        describe(second));
}

// -- an error status whose body happens to be a whole number of uint32s ------

{
    let body = JSON.stringify({ success: false, error: "Plexora's Python is not loaded" });
    while (body.length % 12) body += " ";
    const { numeric, requests } = build([failed(503, body), ok(GOOD)]);
    const first = await settle(numeric.loadCells());
    check("a 503 body is never read as cell ids",
        Boolean(first.error) || !first.value?.ids?.length,
        describe(first));
    const second = await settle(numeric.loadCells());
    check("after a 503 the next loadCells asks the server again",
        requests.length === 2 && second.value?.ids?.length === 2,
        `requests: ${requests.length}; second ${describe(second)}`);
}

// -- the healthy path is untouched ------------------------------------------

{
    const { numeric, requests } = build([ok(GOOD), ok(GOOD)]);
    const first = await numeric.loadCells();
    const second = await numeric.loadCells();
    check("a successful load is still cached: two loads, one request",
        requests.length === 1 && first.ids.length === 2 && second === first,
        `requests: ${requests.length}`);
}

if (failures.length) {
    console.error(`cells request failure: ${failures.length} failed\n  ${failures.join("\n  ")}`);
    process.exit(1);
}
console.log("cells request failure: ok");
