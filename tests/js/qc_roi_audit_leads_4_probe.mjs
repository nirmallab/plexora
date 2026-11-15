/**
 * Client leads from the Nov 2026 audit (docs/internal/AUDIT_2026-11-14.md §7),
 * QC and ROI panels, batch 4, each pinned by the symptom it had:
 *
 * - pg-qc-static-4: the hover probe's revalidate() (a reload, another plane)
 *   let a cell_at answer already in flight land, so a stale or wrong-plane
 *   cell was cached and shown;
 * - pg-qc-static-6: a job poll (Blur, Artifacts, Segmentation, Stack QC) or
 *   the session's follow, mid-request when the QC tool was torn down, carried
 *   on and wrote into the reopened panel;
 * - pg-qc-static-7: a reload while a heavy payload (the detector's objects,
 *   Segmentation QC's cells, a blur map or mask) was still on its way asked
 *   for it again;
 * - pg-roi-11: the ROI tree filtered every feature once per category on
 *   every store change, and the renderer re-scanned every vertex of every
 *   region for its bounds on every frame.
 *
 * The real classes, built from their prototypes, in a vm with hand-built
 * servers whose answers this probe releases when it wants.
 *
 * Run directly: `node tests/js/qc_roi_audit_leads_4_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const QC = join(REPO, "plexora", "plugins", "qc", "static");
const ROI = join(REPO, "plexora", "plugins", "roi", "static");

/** Every timer is held; the probe runs them itself. */
let timers = [];
const window = {
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout() {}, addEventListener() {}, removeEventListener() {},
    requestAnimationFrame() { return 1; },
};
const document = {
    addEventListener() {}, removeEventListener() {}, getElementById: () => null,
    querySelector: () => null, querySelectorAll: () => [],
};
window.window = window;
window.document = document;
const context = createContext({ window, document, console, setTimeout: window.setTimeout,
                                clearTimeout() {}, requestAnimationFrame: () => 1,
                                navigator: {}, Promise, Path2D: class {} });
for (const [dir, file] of [[QC, "qcHover.js"], [QC, "qcBlur.js"], [QC, "qcArtifacts.js"],
                           [QC, "qcSegmentation.js"], [QC, "qcStack.js"],
                           [QC, "qcSidebarController.js"], [ROI, "roiGeometry.js"],
                           [ROI, "roiRenderer.js"], [ROI, "roiTree.js"]]) {
    runInContext(readFileSync(join(dir, file), "utf8"), context, { filename: file });
}
runInContext("window.QcStack = QcStack; window.QcSidebarController = QcSidebarController;"
             + "window.RoiGeometry = RoiGeometry; window.RoiRenderer = RoiRenderer;"
             + "window.RoiTree = RoiTree; var QcTree = { closePopup() {} };", context);

const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
function deferred() {
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    return { promise, resolve };
}

let failed = 0;
async function check(name, fn) {
    timers = [];
    try {
        await fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failed += 1;
        console.log(`FAIL ${name}: ${error.message}`);
    }
}

// -- pg-qc-static-4 ---------------------------------------------------------------------

await check("pg-qc-static-4: revalidate drops a cell_at answer in flight", async () => {
    const pending = deferred();
    const probe = Object.create(window.QcHoverProbe.prototype);
    Object.assign(probe, {
        tracker: {}, token: 0, inflight: false, cells: [], last: null, cell: null,
        cellKey: null, cellModel: null, region: null, regionModel: null, failures: 0,
        pausedUntil: 0, _frame: 0, position: null, artifactKey: null,
        wanted: { x: 10, y: 10, scale: 1 }, ctx: {}, card: { visible: false },
        deps: { api: { cellAt: () => pending.promise }, helpers: {} },
        render() {}, modelOf: (record) => ({ record }),
    });
    probe.ask();
    probe.revalidate();   // another plane, or the cells reloaded
    pending.resolve({ ok: true, data: { cell: { cell_id: 7, calls: true }, shape: null } });
    await flush();
    assert.equal(probe.cell, null, "the old plane's cell was shown");
    assert.equal(probe.last, null, "the old plane's cell was kept");
});

// -- pg-qc-static-6 ---------------------------------------------------------------------

/** A tool whose job poll is mid-request when the QC tool closes. */
async function pollOutlivesDestroy(make) {
    const pending = deferred();
    const tool = make();
    let renders = 0, refreshes = 0, jobs = 0;
    tool.api = { job: () => { jobs += 1; return pending.promise; } };
    tool.render = () => { renders += 1; };
    tool.refresh = async () => { refreshes += 1; };
    tool.host = { message() {} };
    tool.follow("job1");
    timers.splice(0).forEach((fn) => fn());   // the first tick: its request goes out
    assert.equal(jobs, 1);
    renders = 0;
    tool.destroy();
    pending.resolve({ ok: true, data: { job: { status: "running", progress: {} } } });
    await flush();
    assert.equal(timers.length, 0, "the poll was scheduled again after the tool closed");
    assert.equal(renders, 0, "the closed tool rendered");
}

await check("pg-qc-static-6: blur QC's poll stops when the tool closes", async () => {
    await pollOutlivesDestroy(() => Object.assign(Object.create(window.QcBlurQc.prototype),
        { rows: new Map(), draft: null, overlay: null, listed: () => [] }));
});

await check("pg-qc-static-6: the artifact detector's poll stops when the tool closes",
    async () => {
        await pollOutlivesDestroy(() => Object.assign(
            Object.create(window.QcArtifactsQc.prototype),
            { cats: new Map(), lines: new Map(), draft: null, overlay: null, _anchor: null }));
    });

await check("pg-qc-static-6: segmentation QC's poll stops when the tool closes", async () => {
    await pollOutlivesDestroy(() => Object.assign(
        Object.create(window.QcSegmentationQc.prototype),
        { sliders: {}, pickers: {}, overlay: null }));
});

await check("pg-qc-static-6: stack QC's poll stops when the tool closes", async () => {
    await pollOutlivesDestroy(() => Object.assign(Object.create(window.QcStack.prototype),
        { enabled: true }));
});

await check("pg-qc-static-6: the session follow stops when the tool closes", async () => {
    const pending = deferred();
    const stub = { destroy() {}, stop() {} };
    const c = Object.assign(Object.create(window.QcSidebarController.prototype), {
        session: { state: "running", jobId: "job1" }, _followingBulk: false,
        registration: stub, segmentation: stub, blur: stub, artifacts: stub, stack: null,
        freehand: stub, magic: stub, drawBar: stub, hover: stub, overlay: stub,
        cellLayer: stub, _colorTimers: new Map(), _onMagicKey: null,
    });
    let jobs = 0, statuses = 0;
    c.api = { job: () => { jobs += 1; return pending.promise; } };
    c.renderStatus = () => { statuses += 1; };
    const following = c.followBulk();
    await flush();
    c.destroy();
    pending.resolve({ ok: true, data: { job: { status: "running", progress: { step: 1 } } } });
    await flush();
    timers.splice(0).forEach((fn) => fn());
    await flush();
    void following;
    assert.equal(statuses, 0, "the closed panel's status line was written");
    assert.equal(jobs, 1, "the job was asked about again after the panel closed");
});

// -- pg-qc-static-7 ---------------------------------------------------------------------

await check("pg-qc-static-7: the detector's objects are fetched once while in flight",
    async () => {
        const pending = deferred();
        let asked = 0;
        const tool = Object.assign(Object.create(window.QcArtifactsQc.prototype), {
            cats: new Map(), objects: [], objectsFingerprint: null, _objectsSeq: 0,
            paths: new Map(), job: null, selectedId: null,
            api: { artifactsObjects: () => { asked += 1; return pending.promise; } },
            render() {}, invalidate() {},
        });
        const status = { categories: [], results: { fingerprint: "fp1" } };
        tool.adopt(status);
        tool.adopt(status);
        assert.equal(asked, 1, "a second reload asked for the same objects again");
        pending.resolve({ ok: true, data: { available: true, fingerprint: "fp1",
                                            objects: [] } });
        await flush();
        tool.adopt(status);
        assert.equal(asked, 1);
        tool.adopt({ categories: [], results: { fingerprint: "fp2" } });
        assert.equal(asked, 2, "a new result's objects were not fetched");
    });

await check("pg-qc-static-7: segmentation QC's cells are fetched once while in flight",
    async () => {
        const pending = deferred();
        let asked = 0;
        const tool = Object.assign(Object.create(window.QcSegmentationQc.prototype), {
            cells: null, _cellsSeq: 0, flags: {}, job: null, view: {},
            host: { setCellGroups() {} }, ctx: {},
            api: { segmentationCells: () => { asked += 1; return pending.promise; } },
            render() {}, scheduleDensity() {},
        });
        const status = { summary: { fingerprint: "fp1" } };
        tool.adopt(status);
        tool.adopt(status);
        assert.equal(asked, 1, "a second reload asked for the same cells again");
        pending.resolve({ ok: true, data: { available: true, fingerprint: "fp1", groups: [] } });
        await flush();
        tool.adopt(status);
        assert.equal(asked, 1);
    });

await check("pg-qc-static-7: a blur map and mask are fetched once while in flight",
    async () => {
        const maps = deferred(), masks = deferred();
        let mapAsked = 0, maskAsked = 0;
        const tool = Object.assign(Object.create(window.QcBlurQc.prototype), {
            rows: new Map(), job: null, view: { heat: true }, overlay: null,
            api: { blurMap: () => { mapAsked += 1; return maps.promise; },
                   blurMask: () => { maskAsked += 1; return masks.promise; } },
            render() {},
        });
        const status = { results: [{ channel: "DNA", color: "#fff",
                                     threshold: { value: 0.5 },
                                     summary: { status: "ok", fingerprint: "fp1" } }] };
        tool.adopt(status);
        tool.adopt(status);
        assert.equal(mapAsked, 1, "a second reload asked for the same map again");
        assert.equal(maskAsked, 1, "a second reload asked for the same mask again");
    });

// -- pg-roi-11 --------------------------------------------------------------------------

await check("pg-roi-11: the tree walks the features once, not once per category", async () => {
    const categories = Array.from({ length: 6 }, (_, i) => ({ id: `c${i}` }));
    const features = Array.from({ length: 30 }, (_, i) => ({ id: `f${i}`,
                                                             category_id: `c${i % 6}` }));
    let filters = 0;
    features.filter = function (...args) {
        filters += 1;
        return Array.prototype.filter.apply(this, args);
    };
    const placed = new Map();
    const tree = Object.assign(Object.create(window.RoiTree.prototype), {
        list: { classList: { toggle() {} } }, _bound: true, categoryRows: new Map(),
        featureRows: new Map(), collapsed: new Set(), known: new Set(),
        _lastSelection: null, _editing: null,
        store: { editable: true, selectionId: null, activeCategory: null, features,
                 sortedCategories: () => categories, feature: () => null },
        buildCategoryRow: () => ({ el: { classList: { toggle() {} }, setAttribute() {} },
                                   children: {} }),
        updateCategoryRow(row, category, count) { placed.set(category.id, count); },
        buildFeatureRow: (f) => ({ el: { id: f.id } }),
        updateFeatureRow() {},
    });
    const place = window.RoiTree.place;
    window.RoiTree.place = () => {};
    try {
        tree.render();
    } finally {
        window.RoiTree.place = place;
    }
    assert.equal(placed.get("c0"), 5);
    assert.equal(tree.featureRows.size, 30);
    assert.ok(filters <= 1, `the features were filtered ${filters} times`);
});

await check("pg-roi-11: a region's bounds are measured once, not every frame", async () => {
    // RoiGeometry is frozen: the shape counts its own reads instead.
    let measured = 0;
    const shape = (coords) => ({ type: "Polygon",
                                 get coordinates() { measured += 1; return coords; } });
    const renderer = Object.create(window.RoiRenderer.prototype);
    const view = { minX: 0, minY: 0, maxX: 100, maxY: 100 };
    const feature = { id: "a",
                      geometry: shape([[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]) };
    assert.ok(renderer.intersects(feature, view));
    const once = measured;
    assert.ok(once >= 1);
    for (let frame = 0; frame < 5; frame++) assert.ok(renderer.intersects(feature, view));
    assert.equal(measured, once, "the vertices were scanned again on a later frame");
    // A new shape (every edit makes one) is measured again.
    feature.geometry = shape([[[200, 200], [210, 200], [210, 210], [200, 200]]]);
    assert.ok(!renderer.intersects(feature, view));
    assert.ok(measured > once);
});

process.exit(failed ? 1 : 0);
