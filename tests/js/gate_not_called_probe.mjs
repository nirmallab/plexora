/**
 * QC's "not called" in the browser: a cell QC says cannot be called for a gated
 * marker is neither positive nor negative, and the gate draws it grey.
 *
 * Every way this goes wrong is quiet. A mask that still says 1 for a
 * not-called cell draws it positive; a cache that outlives a QC change keeps
 * yesterday's grey; a write nobody announces leaves the gate stale until the
 * next drag; a note that miscounts reads as a QC result. None of that throws.
 *
 *   A. labelGpu.js evaluateGateMask with its `notCalled` argument: absent (the
 *      gate as it was), a key the gates do not name, a multi-key gate.
 *   B. numericData.js getNotCalled: the request, the cache, a failure.
 *   C. `plexora:qc-changed`: raised by QC's writes (qcApi.js) and by an ROI
 *      save that touched a QC region (roiState.js) once it lands -- not by a
 *      read, a refused write, or an ordinary ROI.
 *   D. the gating panel's note: ", N not called" only when there are some.
 *
 * Run directly:  node tests/js/gate_not_called_probe.mjs
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (file) => readFileSync(join(REPO, file), "utf8");

const checks = [];
const failures = [];

function check(name, actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

const BASICS = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, WeakSet, Promise, Error,
    Uint8Array, Uint32Array, Float32Array, URLSearchParams, console,
};

// ---------------------------------------------------------------------------
// A. evaluateGateMask(ids, columns, gates, notCalled)
// ---------------------------------------------------------------------------

const gpuContext = createContext({ ...BASICS });
runInContext(read("plexora/client/src/js/views/labelGpu.js"), gpuContext);
const { evaluateGateMask } = gpuContext.PlexoraLabelGpu;

const f32 = (xs) => Float32Array.from(xs);
const u8 = (xs) => Uint8Array.from(xs);
const maskOf = (r) => Array.from(r.mask).join("");
const summary = (r) => ({ mask: maskOf(r), count: r.count, notCalledCount: r.notCalledCount, maxId: r.maxId });

const ids = Uint32Array.from([1, 2, 3, 4, 5]);
const columns = { A: f32([5, 5, 0, 5, NaN]), B: f32([9, 0, 9, 9, 9]) };

check("without notCalled the mask and count are the gate's alone",
    summary(evaluateGateMask(ids, columns, { A: [1, 10] })),
    { mask: "011010", count: 3, notCalledCount: 0, maxId: 5 });
check("...and the same with null or an empty object",
    [summary(evaluateGateMask(ids, columns, { A: [1, 10] }, null)),
     summary(evaluateGateMask(ids, columns, { A: [1, 10] }, {}))].map((s) => s.mask + s.count),
    ["0110103", "0110103"]);
check("a not-called row is 2, whether the gate passed it or not",
    summary(evaluateGateMask(ids, columns, { A: [1, 10] }, { A: u8([1, 0, 1, 0, 0]) })),
    { mask: "021210", count: 2, notCalledCount: 2, maxId: 5 });
check("not-called bytes for a key the gates do not name are ignored",
    summary(evaluateGateMask(ids, columns, { A: [1, 10] }, { B: u8([1, 1, 1, 1, 1]) })),
    { mask: "011010", count: 3, notCalledCount: 0, maxId: 5 });
check("multi-key: the gate is the AND, and not called for EITHER key is 2",
    summary(evaluateGateMask(ids, columns, { A: [1, 10], B: [1, 10] },
        { A: u8([0, 0, 0, 1, 0]), B: u8([0, 1, 0, 0, 0]) })),
    { mask: "012020", count: 1, notCalledCount: 2, maxId: 5 });
check("multi-key: a key without not-called bytes leaves its cells to the other",
    summary(evaluateGateMask(ids, columns, { A: [1, 10], B: [1, 10] }, { B: u8([0, 0, 0, 1, 1]) })),
    { mask: "010022", count: 1, notCalledCount: 2, maxId: 5 });
check("the one-key and many-key loops agree on the same cells",
    maskOf(evaluateGateMask(ids, columns, { A: [1, 10] }, { A: u8([1, 0, 0, 1, 0]) })),
    maskOf(evaluateGateMask(ids, columns, { A: [1, 10], B: [-1, 100] }, { A: u8([1, 0, 0, 1, 0]) })));
check("a gated key the table lacks is skipped by the gate, not by QC (as the server asks QC)",
    summary(evaluateGateMask(ids, columns, { A: [1, 10], Z: [0, 1] }, { Z: u8([0, 0, 0, 0, 1]) })),
    { mask: "011012", count: 3, notCalledCount: 1, maxId: 5 });
check("a repeated id: counted once as positive or as not called, and not called wins",
    summary(evaluateGateMask(Uint32Array.from([2, 2, 3, 3]), { A: f32([5, 5, 5, 0]) }, { A: [1, 10] },
        { A: u8([0, 0, 0, 1]) })),
    { mask: "0012", count: 1, notCalledCount: 1, maxId: 3 });

// ---------------------------------------------------------------------------
// B. numericData.getNotCalled, and C. the announcement, in one page
// ---------------------------------------------------------------------------

const requests = [];
const events = [];
const fetchAnswers = { notCalled: () => ({ ok: true, status: 200, bytes: [0, 1, 0] }),
                       qc: () => ({ ok: true, status: 200, body: { ok: true } }),
                       roi: () => ({ ok: true, status: 200, body: { success: true, revision: 2 } }) };
const timers = [];

const page = createContext({
    ...BASICS,
    datasource: "probe_ds",
    plexoraUrl: (path) => `/base/${path}`,
    PlexoraDataset: { resolveSchema: () => null },
    CustomEvent: class CustomEvent {
        constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
    },
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    async fetch(url, init = {}) {
        url = String(url);
        requests.push({ url, method: init.method || "GET" });
        if (url.includes("get_not_called")) {
            const answer = fetchAnswers.notCalled();
            if (answer.throws) throw new Error("offline");
            return { ok: answer.ok, status: answer.status,
                     arrayBuffer: async () => Uint8Array.from(answer.bytes || []).buffer };
        }
        const answer = url.includes("plugins/roi/") ? fetchAnswers.roi() : fetchAnswers.qc();
        return { ok: answer.ok, status: answer.status, json: async () => answer.body };
    },
});
runInContext("globalThis.window = globalThis;", page);
page.dispatchEvent = (event) => { events.push({ type: event.type, detail: event.detail }); return true; };
runInContext(`${read("plexora/client/src/js/services/numericData.js")}
${read("plexora/plugins/qc/static/qcApi.js")}
${read("plexora/plugins/roi/static/roiState.js")}
;globalThis.__NumericData = NumericData; globalThis.__RoiStore = RoiStore;`, page);

const numeric = new page.__NumericData({}, {});
const notCalledRequests = () => requests.filter((r) => r.url.includes("get_not_called"));

const first = await numeric.getNotCalled("CD3");
check("getNotCalled asks /get_not_called for the marker on this datasource, through plexoraUrl",
    notCalledRequests().map((r) => r.url), ["/base/get_not_called?datasource=probe_ds&marker=CD3"]);
check("...and answers one byte per row", [first instanceof page.Uint8Array, Array.from(first)],
    [true, [0, 1, 0]]);
const again = await numeric.getNotCalled("CD3");
check("a second ask is the cache's, not another request", [again === first, notCalledRequests().length],
    [true, 1]);
requests.length = 0;
const both = await Promise.all([numeric.getNotCalled("CD8"), numeric.getNotCalled("CD8")]);
check("two asks at once share one request", [notCalledRequests().length, both[0] === both[1]], [1, true]);

requests.length = 0;
fetchAnswers.notCalled = () => ({ ok: false, status: 500 });
const failed = await numeric.getNotCalled("PanCK");
fetchAnswers.notCalled = () => ({ throws: true });
const offline = await numeric.getNotCalled("PanCK");
check("a failed request resolves to null -- the gate still draws -- and is not cached",
    [failed, offline, notCalledRequests().length], [null, null, 2]);
fetchAnswers.notCalled = () => ({ ok: true, status: 200, bytes: [1, 1, 1] });

requests.length = 0;
numeric.invalidateNotCalled();
await numeric.getNotCalled("CD3");
check("invalidateNotCalled forgets every marker", notCalledRequests().length, 1);
numeric.invalidateColumns();
await numeric.getNotCalled("CD3");
check("and so does invalidateColumns: the bytes are per row", notCalledRequests().length, 2);

requests.length = 0;
numeric.invalidateNotCalled();
const depth = page.__NumericData.NOT_CALLED_CACHE_SIZE;
for (let i = 0; i <= depth; i += 1) await numeric.getNotCalled(`M${i}`);
await numeric.getNotCalled(`M${depth}`);
await numeric.getNotCalled("M1");
await numeric.getNotCalled("M0");
check("kept NOT_CALLED_CACHE_SIZE markers deep, the least recently used out first",
    notCalledRequests().map((r) => new URL(r.url, "http://x").searchParams.get("marker")).slice(depth + 1),
    ["M0"]);

// -- C. plexora:qc-changed --------------------------------------------------

events.length = 0;
page.PlexoraQcChanged.announce("probe_ds");
check("PlexoraQcChanged.announce raises plexora:qc-changed with the datasource", events,
    [{ type: "plexora:qc-changed", detail: { datasource: "probe_ds" } }]);

const api = new page.QcApi({ url: (path) => `/base/${path}`, datasource: "probe_ds" });
const writes = {
    deleteRegion: ["r1"], deleteRegions: [["r1", "r2"]], setStrictness: ["strict"],
    refresh: [null], refineRegion: [{ all: true }], approve: ["r1", "exclude"],
    drawRegion: [{ class: "fold", points: [] }], reshapeRegion: ["r1", {}, "sam"],
    dismissFinding: [{ finding: "marker", reason: "r", marker: "CD3" }],
    blurWriteRegions: [{}], artifactsWriteRegions: [{}],
};
const announcedBy = [];
for (const [method, args] of Object.entries(writes)) {
    events.length = 0;
    await api[method](...args);
    if (events.length === 1 && events[0].type === "plexora:qc-changed") announcedBy.push(method);
}
check("every QC write that can move its calls announces once it lands", announcedBy, Object.keys(writes));
events.length = 0;
await api.state();
await api.regions();
await api.setColor({ class: "fold" }, "#ff0000");
await api.addCategory({ class: "fold" });
await api.renameRegion("r1", "a name");
check("a read, a colour, a new empty category or a name does not", events.length, 0);
fetchAnswers.qc = () => ({ ok: false, status: 409, body: { ok: false, error: { message: "a session is open" } } });
events.length = 0;
await api.approve("r1", "exclude");
check("a write the server refused does not", events.length, 0);
fetchAnswers.qc = () => ({ ok: true, status: 200, body: { ok: true } });

/** A saved ROI document: one QC region and one ordinary ROI. */
function roiStore() {
    const store = new page.__RoiStore({ datasource: "probe_ds" }, {
        async postOperations(base, ops) {
            const answer = fetchAnswers.roi();
            return { ok: answer.ok, status: answer.status, data: answer.body };
        },
    });
    store.categories = [{ id: "qc_tissue_acquisition", label: "QC: tissue" }, { id: "tumour", label: "Tumour" }];
    store.features = [
        { id: "q1", category_id: "qc_tissue_acquisition", geometry: { type: "Polygon", coordinates: [] } },
        { id: "t1", category_id: "tumour", geometry: { type: "Polygon", coordinates: [] } },
    ];
    return store;
}
const move = (id) => ({ label: "Move", redo: [{ op: "roi.update_geometry", id, geometry: { moved: true } }],
                        undo: [{ op: "roi.update_geometry", id, geometry: {} }] });

events.length = 0;
let store = roiStore();
store.commit(move("q1"));
const beforeSave = events.length;
await store.flush();
check("an ROI edit to a QC region announces once it is saved, not before",
    [beforeSave, events.map((e) => e.type)], [0, ["plexora:qc-changed"]]);
events.length = 0;
store = roiStore();
store.commit({ label: "Delete", redo: [{ op: "roi.delete", id: "q1" }], undo: [] });
await store.flush();
check("deleting one announces too: its category is read before it goes", events.length, 1);
events.length = 0;
store = roiStore();
store.commit(move("t1"));
store.commit({ label: "Rename", redo: [{ op: "roi.update_properties", id: "t1", changes: { name: "x" } }],
               undo: [] });
await store.flush();
check("an edit to an ordinary ROI does not", events.length, 0);
events.length = 0;
store = roiStore();
store.commit({ label: "Move to QC", redo: [{ op: "roi.update_properties", id: "t1",
                                              changes: { category_id: "qc_tissue_acquisition" } }],
               undo: [] });
await store.flush();
check("moving an ordinary ROI into a QC category does", events.length, 1);
events.length = 0;
fetchAnswers.roi = () => ({ ok: false, status: 409, body: { revision: 3 } });
store = roiStore();
store.commit(move("q1"));
await store.flush();
check("a save that did not land announces nothing", events.length, 0);

// ---------------------------------------------------------------------------
// D. the gating panel's note
// ---------------------------------------------------------------------------

const note = { hidden: true, textContent: "", title: "", dataset: {} };
const gating = createContext({
    ...BASICS,
    document: { getElementById: (id) => (id === "gate_qc_note" ? note : null) },
    GatingApi: class GatingApi {},
});
runInContext("globalThis.window = globalThis;", gating);
runInContext(`${read("plexora/plugins/gating/static/gatingSidebarController.js")}
;globalThis.__Controller = GatingSidebarController;`, gating);
const Controller = gating.__Controller;

function panel({ fitted = false, notCalled = null } = {}) {
    const self = Object.create(Controller.prototype);
    self.gateMarker = "CD3";
    self.gatingList = {
        pluginName: "gating",
        hasGatingGMM: fitted ? { CD3: { qc_exclusion: { applied: true, n_cells: 100, n_left_out: 5,
                                                         mode: "exclude", warning: false } } } : {},
    };
    self.ctx = { viewer: { getCellLayer: (name) => (name === "gating" ? { gateNotCalled: notCalled } : null) } };
    return self;
}

panel({ fitted: true, notCalled: 0 }).paintQcNote();
check("with a fit and none not called the note is QC's words, unchanged",
    [note.hidden, note.textContent], [false, "QC-passed cells only · 5 left out by QC"]);
panel({ fitted: true, notCalled: 12 }).paintQcNote();
check("with cells not called it ends \", N not called\"",
    note.textContent, "QC-passed cells only · 5 left out by QC, 12 not called");
check("...and the tooltip says why they are grey",
    [note.title.startsWith("Auto fitted on 95 QC-passed cells"), note.title.includes("drawn grey")], [true, true]);
panel({ notCalled: 12 }).paintQcNote();
check("without a fit, the not-called count alone", [note.hidden, note.textContent], [false, "12 not called"]);
panel({ notCalled: null }).paintQcNote();
check("with neither, the note is hidden, as it was", [note.hidden, note.textContent], [true, ""]);

const painted = [];
const controller = new Controller({ sidebar: null, moduleInstance: { pluginName: "gating" }, dataLayer: {},
                                    eventHandler: {}, config: {} });
controller.paintQcNote = () => painted.push("gating");
controller._onCellGate({ detail: { layer: "gating" } });
controller._onCellGate({ detail: { layer: "cell_explorer" } });
check("the viewer's word that this layer's gate moved repaints it; another layer's does not",
    painted, ["gating"]);

for (const name of checks) {
    if (!failures.some((f) => f.check === name)) console.log(`ok  ${name}`);
}
for (const failure of failures) {
    console.log(`FAIL ${failure.check}\n     expected ${failure.expected}\n     actual   ${failure.actual}`);
}
console.log(`\n${checks.length - failures.length}/${checks.length} checks held`);
process.exit(failures.length ? 1 : 0);
