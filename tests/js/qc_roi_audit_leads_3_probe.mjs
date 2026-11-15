/**
 * Client leads from the Nov 2026 audit (docs/internal/AUDIT_2026-11-14.md §7),
 * QC and ROI panels, each pinned by the symptom it had:
 *
 * - pg-qc-static-1: a channel QC put on screen (an artifact's, the
 *   registration check's) was auto-levelled a second later, after QC had
 *   released persistence, and that late fit saved the arrangement into the
 *   project's channel list;
 * - pg-qc-static-2: a region drawn while the previous one was still saving
 *   was dropped (`act` refuses while busy) with nothing said;
 * - pg-qc-static-3: Registration bound Image Channels' colour event and never
 *   unbound it, so each tool close and reopen kept the old controller alive;
 * - pg-roi-4: Map to cells and Save-to-file wrote the server's regions while
 *   the panel held edits not yet saved, or refused;
 * - pg-roi-6: `store.load()` took the server's reply as it came, so an edit
 *   made (or saved) during its GET was wiped (or its revision regressed), and
 *   'From cells' reloaded over queued edits.
 *
 * The real classes (their prototypes where a constructor needs a viewer), in
 * a vm with hand-built panels and servers.
 *
 * Run directly: `node tests/js/qc_roi_audit_leads_3_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const QC = join(REPO, "plexora", "plugins", "qc", "static");
const ROI = join(REPO, "plexora", "plugins", "roi", "static");

/** Short waits run; anything longer (a save's debounce, a retry) is held. */
const held = [];
function timeout(fn, ms) {
    if ((ms || 0) <= 60) return setTimeout(fn, ms);
    held.push(fn);
    return { held: true };
}
function clear(handle) {
    if (handle && !handle.held) clearTimeout(handle);
}

const window = {
    setTimeout: timeout, clearTimeout: clear, addEventListener() {}, removeEventListener() {},
    PlexoraStatus: { track: (_label, promise) => promise, begin: () => ({ done() {}, fail() {} }) },
    PlexoraTelemetry: null,
};
const document = {
    addEventListener() {}, removeEventListener() {}, getElementById: () => null,
    querySelector: () => null, querySelectorAll: () => [],
    createElement: () => ({ classList: { add() {}, remove() {} }, append() {}, style: {} }),
};
window.window = window;
window.document = document;
const context = createContext({ window, document, console, setTimeout: timeout,
                                clearTimeout: clear, navigator: {}, Promise });
for (const [dir, file] of [[QC, "qcArtifacts.js"], [QC, "qcRegistration.js"],
                           [QC, "qcSidebarController.js"], [ROI, "roiState.js"],
                           [ROI, "roiSidebarController.js"]]) {
    runInContext(readFileSync(join(dir, file), "utf8"), context, { filename: file });
}
runInContext("window.RoiStore = RoiStore; window.RoiSidebarController = RoiSidebarController;",
             context);

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
let failed = 0;
async function check(name, fn) {
    try {
        await fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failed += 1;
        console.log(`FAIL ${name}: ${error.message}`);
    }
}

// -- pg-qc-static-1 ---------------------------------------------------------------------

/** Image Channels as far as auto-levelling and saving go: setSlotMarker
 *  schedules a level for a slot nobody levelled (autoLevelChannelIfNeeded),
 *  and the level, when it lands, saves unless the slot's autoSilent is up
 *  (applyAutoRange) -- after QC has resumed persistence. */
function channelPanel(slots) {
    const panel = {
        channelSlots: slots, saves: 0, suspended: 0, levels: [],
        setSlotMarker(i, name, options = {}) {
            const slot = this.channelSlots[i];
            if (slot.name !== name) {
                slot.name = name;
                slot.autoLeveled = false;
                slot.userRangeChanged = false;
            }
            if (options.enable) slot.enabled = true;
            if (options.reveal || options.enable) slot.visible = true;
            if (slot.enabled && !slot.autoLeveled && !slot.autoLeveling && !slot.userRangeChanged) {
                this.levels.push(slot);
            }
        },
        setSlotEnabled(i, on) { this.channelSlots[i].enabled = on; },
        setSlotColor(i, hex) { this.channelSlots[i].colorHex = hex; },
        suspendPersistence() { this.suspended += 1; },
        resumePersistence() { this.suspended -= 1; },
        /** The fits land, late. */
        land() {
            for (const slot of this.levels.splice(0)) {
                if (!slot.autoSilent && !this.suspended) this.saves += 1;
                slot.autoSilent = false;
                slot.autoLeveled = true;
            }
        },
    };
    return panel;
}

await check("pg-qc-static-1: a channel QC places is levelled without a save", async () => {
    const panel = channelPanel([{ name: "DNA", enabled: true, visible: true, autoLeveled: true },
                                { name: null, enabled: false, visible: false }]);
    const artifacts = Object.create(window.QcArtifactsQc.prototype);
    artifacts.place(panel, 1, "CD45");
    assert.equal(panel.channelSlots[1].name, "CD45");
    assert.equal(panel.suspended, 0);
    panel.land();
    assert.equal(panel.saves, 0, "the late auto-level saved QC's channel into the project");
});

await check("pg-qc-static-1: a slot that will not level keeps no silent flag", async () => {
    const panel = channelPanel([{ name: "CD45", enabled: true, visible: true, autoLeveled: true }]);
    const artifacts = Object.create(window.QcArtifactsQc.prototype);
    artifacts.place(panel, 0, "CD45");
    assert.ok(!panel.channelSlots[0].autoSilent,
              "a flag left up would swallow the save of the user's next auto-level");
});

await check("pg-qc-static-1: the registration check's channels are levelled without a save",
            async () => {
    const panel = channelPanel([{ name: "DNA_1", enabled: true, visible: true, autoLeveled: true },
                                { name: "CD3", enabled: true, visible: true, autoLeveled: true }]);
    window.__plexora = { viewerSidebar: panel };
    const reg = Object.create(window.QcRegistration.prototype);
    reg.state = { active: true, reference: "DNA_1", comparison: "DNA_3" };
    reg._applied = [null, null];
    reg._appliedNames = [null, null];
    reg.stopFlicker = () => {};
    reg.syncFlicker = () => {};
    reg.wantedColor = () => ({});
    reg.applyViewer();
    assert.equal(panel.channelSlots[1].name, "DNA_3");
    panel.land();
    assert.equal(panel.saves, 0, "the check's comparison channel was saved into the project");
    // Off: the user's channel back, its levelling no save either.
    reg.state = { active: false };
    reg.applyViewer();
    assert.equal(panel.channelSlots[1].name, "CD3");
    panel.land();
    assert.equal(panel.saves, 0);
    delete window.__plexora;
});

// -- pg-qc-static-2 ---------------------------------------------------------------------

await check("pg-qc-static-2: a region drawn while another saves is saved after it", async () => {
    const c = Object.create(window.QcSidebarController.prototype);
    const posted = [];
    const answers = [];
    c.busy = false;
    c.drawTarget = { target: { class: "tissue_fold" }, label: "Fold" };
    c.regionData = { regions: [] };
    c.el = () => null;
    c.message = () => {};
    c.currentView = () => ({ channels: [] });
    c.show = () => false;
    c.reload = async () => {};
    c.api = { drawRegion(body) {
        posted.push(body.points);
        return new Promise((resolve) => answers.push(resolve));
    } };
    const first = c.saveStroke([[0, 0], [1, 0], [1, 1]]);
    await tick();
    const second = c.saveStroke([[5, 5], [6, 5], [6, 6]]);
    await tick();
    assert.equal(posted.length, 1);
    answers.shift()({ ok: true, data: { roi: { id: "r1" } } });
    await first;
    for (let i = 0; i < 20 && posted.length < 2; i++) await tick(60);
    assert.equal(posted.length, 2, "the second region was dropped");
    assert.deepEqual(posted[1], [[5, 5], [6, 5], [6, 6]]);
    answers.shift()({ ok: true, data: { roi: { id: "r2" } } });
    await second;
});

// -- pg-qc-static-3 ---------------------------------------------------------------------

await check("pg-qc-static-3: closing the registration check unbinds its colour listener",
            async () => {
    const bound = new Set();
    window.__plexora = { viewerSidebar: { eventHandler: {
        bind(name, fn) { const entry = { name, fn }; bound.add(entry);
                         return () => bound.delete(entry); },
    } } };
    const reg = Object.create(window.QcRegistration.prototype);
    reg._alive = true;
    reg.pickers = new Map();
    reg.overlay = null;
    reg.disarmKeys = () => {};
    reg.stopFlicker = () => {};
    reg.bindChannelColors();
    assert.equal(bound.size, 1);
    reg.destroy();
    assert.equal(bound.size, 0, "the old controller stays reachable from the event handler");
    delete window.__plexora;
});

// -- ROI --------------------------------------------------------------------------------

const TRIANGLE = { type: "Polygon", coordinates: [[[0, 0], [10, 0], [10, 10], [0, 0]]] };
const CREATE = { op: "roi.create", feature: { id: "roi-1", category_id: "c", geometry: TRIANGLE } };

class FakeApi {
    constructor(log) {
        this.log = log;
        this.revision = 5;
        this.mode = "ok";
        this.nextState = null;   // a promise to hand back from the next getState
    }

    state(revision) {
        return { ok: true, status: 200, data: {
            success: true, schema_version: 1, revision, image: "default",
            categories: [{ id: "c", label: "C", color: "#ffffff" }], features: [],
            coordinate_space: { width: 100, height: 100 }, image_size: [100, 100],
            stored_image_size: [100, 100], dimension_mismatch: false } };
    }

    getState() {
        this.log.push("getState");
        if (this.nextState) {
            const pending = this.nextState;
            this.nextState = null;
            return pending;
        }
        return Promise.resolve(this.state(this.revision));
    }

    async postOperations(base, ops) {
        this.log.push("post");
        if (this.mode === "offline") throw new TypeError("Failed to fetch");
        this.revision = base + 1;
        return { ok: true, status: 200, data: { success: true, revision: this.revision } };
    }
}

async function loadedStore(log) {
    const api = new FakeApi(log);
    const store = new window.RoiStore({}, api);
    await store.load();
    log.length = 0;
    return { api, store };
}

function roiController(store, api, log) {
    const c = Object.create(window.RoiSidebarController.prototype);
    const button = { hidden: false, disabled: false };
    c.store = store;
    c.notices = [];
    c.notify = (text) => c.notices.push(text);
    c.el = () => button;
    c.renderer = { invalidate() {}, schedule() {} };
    c.ctx = { dataLayer: { getCurrentSelection: () => new Map([[7, true]]) },
              view: { frame: () => 0 } };
    c.mapPrefix = () => "rois";
    c.confirmMapping = async () => null;
    c.afterSaveToSource = () => {};
    c.api = Object.assign(api, {
        mapToCells: async () => { log.push("mapToCells"); return { ok: true, data: { preview: true, success: true } }; },
        saveToAnndata: async () => { log.push("save"); return { ok: true, data: { success: true } }; },
        fromCells: async () => { log.push("fromCells");
                                 return { ok: true, data: { success: true, roi: { id: "roi-9" } } }; },
    });
    return c;
}

await check("pg-roi-4: map to cells waits for queued edits to land", async () => {
    const log = [];
    const { api, store } = await loadedStore(log);
    store.enqueue([CREATE]);
    const c = roiController(store, api, log);
    await c.mapToCells();
    assert.deepEqual(log.slice(0, 2), ["post", "mapToCells"],
                     `the server's regions were mapped before this panel's edit reached it: ${log}`);
});

await check("pg-roi-4: map to cells refuses while edits are refused", async () => {
    const log = [];
    const { api, store } = await loadedStore(log);
    store.refused.push(CREATE);
    store.status = "failed";
    const c = roiController(store, api, log);
    await c.mapToCells();
    assert.ok(!log.includes("mapToCells"), "mapped while the panel shows an edit the server refused");
    assert.ok(c.notices.length, "nothing said");
});

await check("pg-roi-4: save to file refuses while edits are unsaved", async () => {
    const log = [];
    const { api, store } = await loadedStore(log);
    api.mode = "offline";
    store.enqueue([CREATE]);
    const c = roiController(store, api, log);
    c._destination = { kind: "anndata", existing: [], remembered: "rois" };
    c._destinationOpen = true;
    c.destinationName = () => "rois";
    await c.saveToSource();
    assert.ok(!log.includes("save"), "wrote the server's regions without the queued edit");
    assert.equal(store.queue.length, 1);
    assert.ok(c.notices.length, "nothing said");
});

await check("pg-roi-6: a reload whose read was overtaken by an edit keeps the edit", async () => {
    const log = [];
    const { api, store } = await loadedStore(log);
    let answer;
    api.nextState = new Promise((resolve) => { answer = resolve; });
    const loading = store.load();
    store.enqueue([CREATE]);
    answer(api.state(5));
    const loaded = await loading;
    assert.equal(store.queue.length, 1, "the edit made during the read was wiped");
    assert.notEqual(loaded, true);
});

await check("pg-roi-6: a reload whose read was overtaken by a save keeps the newer revision",
            async () => {
    const log = [];
    const { api, store } = await loadedStore(log);
    let answer;
    api.nextState = new Promise((resolve) => { answer = resolve; });
    const loading = store.load();
    store.enqueue([CREATE]);
    await store.flush();
    assert.equal(store.revision, 6);
    answer(api.state(5));
    await loading;
    assert.equal(store.revision, 6, "the revision went back to the read's");
});

await check("pg-roi-6: from cells saves queued edits first and never wipes them", async () => {
    const log = [];
    const { api, store } = await loadedStore(log);
    store.enqueue([CREATE]);
    const c = roiController(store, api, log);
    await c.regionFromCells();
    assert.deepEqual(log.slice(0, 3), ["post", "fromCells", "getState"], String(log));
    // The server unreachable: nothing made, the edit still queued.
    const second = await loadedStore([]);
    const log2 = [];
    second.api.log = log2;
    second.api.mode = "offline";
    second.store.enqueue([CREATE]);
    const c2 = roiController(second.store, second.api, log2);
    await c2.regionFromCells();
    assert.ok(!log2.includes("fromCells"));
    assert.equal(second.store.queue.length, 1, "the queued edit was wiped");
});

process.exit(failed ? 1 : 0);
