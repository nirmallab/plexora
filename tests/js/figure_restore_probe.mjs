/**
 * Does a captured panel actually come back?
 *
 * This is the claim the whole plugin is built on -- a panel is a reproducible
 * viewer scene, not a screenshot -- and it is the one nothing else in the suite
 * can check: restoring runs entirely in the browser, against a viewer and a
 * sidebar the Python tests never construct.
 *
 * The fixture is therefore a sidebar that really applies what it is given, so
 * that capturing AFTER a restore reads back what the restore wrote. A stub that
 * merely recorded the call would let a restore that applies nothing pass.
 *
 * Three failures it exists to catch, all of them silent:
 *
 * * a round trip that does not land where it started -- the panel reopens as
 *   something close to, but not, the view that was captured;
 * * a restore that lets the project's own saved channel list be written --
 *   looking at a figure panel would permanently overwrite the settings the user
 *   had, with nothing on screen to say so;
 * * a channel that no longer exists being quietly replaced by a neighbour,
 *   which produces a panel that looks right and is wrong.
 *
 * Run directly:
 *   node tests/js/figure_restore_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureSchema.js", "figureSceneSnapshot.js"];

const IMAGE_WIDTH = 4000;
const IMAGE_HEIGHT = 3000;

const problems = [];
const events = [];
const fitBoundsCalls = [];
const saveAttempts = [];

/** A sidebar that behaves like the real one in the two ways that matter: it
 *  really applies the rows it is handed, and it really refuses to persist while
 *  persistence is suspended. */
function sidebarStub() {
    return {
        channelSlots: [
            { name: "DNA", enabled: true, color: { r: 0, g: 0, b: 255 }, range: [0, 65535] },
            { name: "CD8", enabled: false, color: { r: 255, g: 255, b: 255 }, range: [0, 65535] },
            { name: "CD3", enabled: false, color: { r: 255, g: 255, b: 255 }, range: [0, 65535] },
        ],
        _suspended: 0,
        suspendPersistence() { this._suspended += 1; },
        resumePersistence() { this._suspended -= 1; },
        // The guard as viewerSidebar.js implements it. A row recorded here with
        // suspended === 0 is a save that would really have gone out.
        scheduleSaveChannels() {
            saveAttempts.push({ suspended: this._suspended });
        },
        toRawRangeForSlot(slot) { return slot.range; },
        async applySavedChannels(rows) {
            for (const slot of this.channelSlots) {
                slot.enabled = false;
            }
            rows.forEach((row) => {
                const slot = this.channelSlots.find((s) => s.name === row.channel);
                if (!slot) return;
                slot.enabled = Boolean(row.channel_active);
                slot.color = { r: row.r, g: row.g, b: row.b };
                slot.range = [row.start, row.end];
                // The real setters schedule an autosave; that is exactly what
                // the suspension has to be covering.
                this.scheduleSaveChannels();
            });
        },
    };
}

function browserGlobals(sidebar) {
    const item = {
        source: {
            getImagePixel: (_item, position) => [position.x * 5, position.y * 5],
        },
        imageToViewportRectangle: (rect) => ({
            kind: "viewport-rect",
            x: rect.x / IMAGE_WIDTH, y: rect.y / IMAGE_HEIGHT,
            width: rect.width / IMAGE_WIDTH, height: rect.height / IMAGE_HEIGHT,
        }),
        viewportToImageRectangle: (rect) => ({
            x: rect.x * IMAGE_WIDTH, y: rect.y * IMAGE_HEIGHT,
            width: rect.width * IMAGE_WIDTH, height: rect.height * IMAGE_HEIGHT,
        }),
    };
    const osdViewer = {
        world: { getItemAt: () => item },
        viewport: {
            getBounds: () => ({ x: 0.1, y: 0.2, width: 0.3, height: 0.25 }),
            fitBounds: (bounds, immediately) => fitBoundsCalls.push({ bounds, immediately }),
        },
    };
    const layers = {
        cell_explorer: { name: "cell_explorer", mode: "filled", opacity: 0.4, visible: false },
    };
    const globals = {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Infinity,
        setTimeout: () => 1, clearTimeout: () => {},
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
        },
        Event: class Event { constructor(type) { this.type = type; } },
        OpenSeadragon: {
            Rect: class Rect {
                constructor(x, y, width, height) { Object.assign(this, { x, y, width, height }); }
            },
        },
        document: {
            readyState: "complete",
            _hd: { checked: false, dispatchEvent(event) { events.push("hd:" + event.type); } },
            getElementById(id) { return id === "viewer_controls_hd" ? this._hd : null; },
            createElement: () => ({ style: {} }),
            addEventListener() {}, removeEventListener() {},
        },
    };
    globals.imageViewer = {
        viewer: osdViewer,
        cellLayers: () => Object.values(layers),
        cellLayer: (name) => layers[name] || null,
        setCellLayerMode: (name, mode) => { layers[name].mode = mode; },
        setLayerOpacity: (name, value) => { layers[name].opacity = value; },
        setCellLayerVisible: (name, value) => { layers[name].visible = value; },
        setScalebarVisible(value) { this.show_scalebar = value; },
        show_scalebar: false,
    };
    globals.window = {
        crypto: { randomUUID: () => "0123456789abcdef0123456789abcdef" },
        addEventListener() {}, removeEventListener() {},
        dispatchEvent: (event) => {
            events.push(event.type);
            if (event.type === "plexora:figure-capture-state") {
                event.detail.contribute("roi", { version: "1", state: { on: true }, legend: [] });
            }
            if (event.type === "plexora:figure-restore-state") {
                // cell_explorer deliberately does NOT answer, which is what a
                // plugin that is installed but not open looks like.
                event.detail.report("roi", "ok");
            }
            return true;
        },
        __plexora: {
            viewerSidebar: sidebar,
            viewerControls: { selectMode: (mode) => events.push("mode:" + mode) },
        },
    };
    globals.crypto = globals.window.crypto;
    return globals;
}

const sidebar = sidebarStub();
const ctx = createContext(browserGlobals(sidebar));
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

ctx.__ctx = {
    config: {
        width: IMAGE_WIDTH, height: IMAGE_HEIGHT, extraZoomLevels: 0,
        imageData: [
            { name: "DNA", fullname: "DNA_full", src: "/generated/data/demo/demo_0/" },
            { name: "CD8", fullname: "CD8_full", src: "/generated/data/demo/demo_1/" },
            { name: "CD3", fullname: "CD3_full", src: "/generated/data/demo/demo_2/" },
        ],
    },
    dataLayer: { getFullChannelName: (short) => short + "_full" },
    viewer: ctx.imageViewer,
};

function check(label, actual, expected) {
    const a = JSON.stringify(actual);
    const b = JSON.stringify(expected);
    if (a !== b) problems.push(`${label}: expected ${b}, got ${a}`);
}

const report = await runInContext(`(async () => {
    // A scene captured with two channels, one of them not currently on, and an
    // overlay that is currently off -- so a restore that did nothing would be
    // visibly distinguishable from one that worked.
    const captured = {
        snapshot_version: 1,
        source_id: "src_1",
        viewport: { x: 1000, y: 750, w: 2000, h: 1500 },
        channels: [
            { key: "demo_1", fullname_at_capture: "CD8_full",
              color: { r: 255, g: 0, b: 0 }, window: [1028, 51400], visible: true },
            { key: "demo_2", fullname_at_capture: "CD3_full",
              color: { r: 0, g: 255, b: 0 }, window: [200, 40000], visible: true },
        ],
        core_overlays: {
            cell_layers: [{ name: "cell_explorer", mode: "outlines", opacity: 0.85,
                            visible: true, z: 0 }],
            hd_tiles: true,
            scalebar_visible: true,
        },
        plugins: {
            roi: { version: "1", state: { on: true }, legend: [] },
            cell_explorer: { version: "1", state: { column: "phenotype" }, legend: [] },
        },
        captured_at: "2026-01-01T00:00:00.000Z",
    };

    const restoreReport = await FigureScene.restore(__ctx, captured);
    // Capture again, at the SAME region, and compare.
    const recaptured = FigureScene.capture(__ctx, "src_1", captured.viewport);
    return { captured, recaptured, restoreReport };
})()`, ctx);

const { captured, recaptured, restoreReport } = report;

// -- the round trip --------------------------------------------------------

check("the viewport survives a round trip", recaptured.viewport, captured.viewport);
check("the channels survive a round trip", recaptured.channels, captured.channels);
check("the core overlays survive a round trip",
    recaptured.core_overlays, captured.core_overlays);
// The timestamp is the one field that must differ -- it records when the
// snapshot was taken, not what it holds.
if (recaptured.captured_at === captured.captured_at) {
    problems.push("captured_at did not move: the recapture was not a new snapshot");
}
{
    const before = { ...captured };
    const after = { ...recaptured };
    delete before.captured_at;
    delete after.captured_at;
    // plugins differ because only one plugin is open in this fixture, which is
    // its own assertion below.
    delete before.plugins;
    delete after.plugins;
    check("everything but the timestamp round trips", after, before);
}

// -- the project's own settings are not written ---------------------------

if (!saveAttempts.length) {
    problems.push("the fixture never attempted a save, so the guard is untested");
}
const leaked = saveAttempts.filter((attempt) => attempt.suspended <= 0);
check("no save was attempted while restoring", leaked, []);
check("persistence was handed back afterwards", sidebar._suspended, 0);

// -- the viewport was really moved ----------------------------------------

check("the viewport was fitted to the captured region exactly once",
    fitBoundsCalls.length, 1);
check("and to the right rectangle, immediately", fitBoundsCalls[0], {
    bounds: { kind: "viewport-rect", x: 0.25, y: 0.25, width: 0.5, height: 0.5 },
    immediately: true,
});
check("the restore reported the viewport", restoreReport.viewport, true);

// -- what could not be restored is reported, never guessed ----------------

const missing = await runInContext(`(async () => {
    return FigureScene.restoreRows(__ctx, { channels: [
        { key: "demo_0", fullname_at_capture: "DNA_full",
          color: { r: 1, g: 2, b: 3 }, window: [0, 1] },
        { key: "demo_gone", fullname_at_capture: "CD11c_full",
          color: { r: 1, g: 2, b: 3 }, window: [0, 1] },
    ] });
})()`, ctx);
check("a channel that no longer exists is reported", missing.missing, ["CD11c_full"]);
// One row, not two: nothing was substituted for the missing channel.
check("and nothing is substituted for it", missing.rows.map((r) => r.channel), ["DNA"]);

// -- plugins ---------------------------------------------------------------

check("a plugin that answered is recorded as restored", restoreReport.plugins.roi, "ok");
check("a plugin that is not open is recorded as skipped",
    restoreReport.plugins.cell_explorer, "skipped");
if (!events.includes("plexora:figure-restore-state")) {
    problems.push("the restore bridge event was never dispatched");
}

console.error(JSON.stringify(
    { problems, events, saveAttempts, restoreReport }, null, 2));
process.exit(problems.length ? 1 : 0);
