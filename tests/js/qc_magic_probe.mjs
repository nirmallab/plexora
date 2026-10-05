/**
 * Magic select in the QC panel: qcDraw.js's QcMagic and the controller's
 * side of it (qcSidebarController.js).
 *
 * What is worth pinning is where the feature meets a person or the server:
 *
 *   - the words a region's provenance is shown in -- the technical values
 *     (`sam`, `sam_agent`) must never reach the screen;
 *   - the view a region drawn now remembers: the whole view when the server
 *     takes one (`views_format` >= 1), else the legacy channel list, which an
 *     older server would reject in any other shape;
 *   - a click on a region that remembers its view getting that view back,
 *     animated, instead of a box fit -- and the menu saying so;
 *   - E only when the QC panel owns the keyboard, and E with nothing in hand
 *     reopening the last category in magic mode;
 *   - QcMagic arming the one-time setup, outlining through the controller's
 *     commit, and yielding while the ROI tool is on screen;
 *   - QcMagic's floating bar and modes: in Add a drag pans, only Box draws
 *     a box, Remove carves; carving the selected region starts from its
 *     outline (`maskGeometry`), a plain refine click carries none.
 *
 * The real QcTree, QcFreehand, QcMagic and QcSidebarController (prototypes,
 * no constructor: it builds the whole panel) in a vm with a hand-built
 * document, as in qc_picker_probe.mjs. The real viewSnapshot.js and
 * segmentService.js supply the snapshot and the planner.
 *
 * Run directly: `node tests/js/qc_magic_probe.mjs`
 * Prints `ok  <check>` per check held; exit 1 if any did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora", "plugins", "qc", "static");
const SERVICES = join(REPO, "plexora", "client", "src", "js", "services");

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(), children: [], attributes: {}, dataset: {},
        style: { props: {}, setProperty(k, v) { this.props[k] = v; } },
        hidden: false, textContent: "", className: "", listeners: {},
        classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
        setAttribute(key, value) { node.attributes[key] = String(value); },
        getAttribute(key) { return node.attributes[key] ?? null; },
        addEventListener(name, fn) { (node.listeners[name] ||= []).push(fn); },
        appendChild(child) { node.children.push(child); return child; },
        append(...kids) { kids.forEach((k) => node.appendChild(k)); },
        querySelector: () => null,
        getBoundingClientRect() { return { left: 0, right: 10, top: 0, bottom: 10 }; },
    };
    return node;
}

let dialogOpen = false;
const document = {
    activeElement: null,
    createElement: (tag) => element(tag),
    body: element("body"),
    addEventListener() {}, removeEventListener() {},
    querySelector: (selector) => (selector === "dialog[open]" && dialogOpen ? element("dialog") : null),
    querySelectorAll: () => [],
    getElementById: () => null,
};

const sceneCalls = [];
const window = {
    innerWidth: 1200, innerHeight: 900, setTimeout: () => 0, clearTimeout() {},
    addEventListener() {}, removeEventListener() {},
    location: { search: "" },
    PlexoraViewerScene: {
        currentViewport: () => ({ x: 100, y: 200, w: 400, h: 300 }),
        scaleOf: () => 0.5,
        restoreViewport: (...args) => { sceneCalls.push(args); return true; },
    },
    __plexora: {
        seaDragonViewer: { viewer: {} },
        viewerSidebar: {
            channelSlots: [{ name: "DNA", enabled: true, colorHex: "#0000ff" },
                           { name: "CD3", enabled: true, colorHex: "#ff0000" }],
            isHdMode: () => false,
            quantWindow: () => ({}),
            toRawRangeForSlot: () => [10, 900],
        },
    },
};
window.window = window;
const context = createContext({ window, document, console, setTimeout: () => 0,
                                clearTimeout() {}, navigator: {}, URLSearchParams, Date,
                                Promise });
for (const [dir, file] of [[SERVICES, "viewSnapshot.js"], [SERVICES, "segmentService.js"],
                           [STATIC, "qcTree.js"], [STATIC, "qcDraw.js"],
                           [STATIC, "qcSidebarController.js"]]) {
    runInContext(readFileSync(join(dir, file), "utf8"), context, { filename: file });
}
const Controller = window.QcSidebarController;
const RealSnapshot = window.PlexoraViewSnapshot;
const RealSegment = window.PlexoraSegment;

/** A controller without its constructor (which builds the whole panel). */
function controller() {
    const c = Object.create(Controller.prototype);
    c.ctx = { datasource: "sample-A" };
    c.vocabulary = { views_format: 1 };
    c.hidden = new Set();
    c.roiMuted = false;
    c.selectedRegion = null;
    c.regionData = { regions: [] };
    c.said = [];
    c.message = (text) => c.said.push(text);
    c.calls = [];
    c.fit = (box) => c.calls.push(["fit", box]);
    c.ensureDrawn = () => {};
    c.showChannels = () => {};
    c.redraw = () => {};
    c.saveHidden = () => {};
    return c;
}

/** Equal values across the vm's realm (its objects have other prototypes). */
function same(actual, expected) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

const settled = () => new Promise((resolve) => setImmediate(resolve));

const failures = [];
async function check(name, fn) {
    try {
        await fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failures.push(name);
        console.log(`FAIL  ${name}\n      ${String(error && error.message || error).split("\n").join("\n      ")}`);
    }
}

// -- words ----------------------------------------------------------------------

await check("methodWords says how an outline was made, never the technical value", () => {
    assert.equal(Controller.methodWords({ method: "sam" }), "magic select");
    assert.equal(Controller.methodWords({ method: "sam_agent" }), "magic select (automatic)");
    assert.equal(Controller.methodWords({ method: "freehand" }), "drawn by hand");
    assert.equal(Controller.methodWords({ method: "traced" }), "traced to the artifact's pixels");
    assert.equal(Controller.methodWords({ method: "nonsense" }), null);
    assert.equal(Controller.methodWords({}), null);
    assert.equal(Controller.methodWords(null), null);
});

await check("originOf: a user's magic-select region is 'magic select', a hand-drawn one 'manual'", () => {
    same(Controller.originOf({ created_by: "user", method: "sam" }),
         { manual: true, words: "magic select" });
    same(Controller.originOf({ created_by: "user", method: "freehand" }),
         { manual: true, words: "manual" });
    same(Controller.originOf({ created_by: "agent", method: "sam_agent" }),
         { manual: false, words: "AI, with magic select" });
    same(Controller.originOf({ created_by: "blur" }),
         { manual: false, words: "Derived from Blur QC" });
});

// -- the view a region remembers ---------------------------------------------------

await check("currentView is the whole view when the server takes one", () => {
    const view = controller().currentView();
    assert.equal(Array.isArray(view), false);
    assert.equal(view.sample, "sample-A");
    same(view.viewport, { x: 100, y: 200, width: 400, height: 300 });
    same(view.channels.map((ch) => ch.name), ["DNA", "CD3"]);
    same(view.channels[0], { name: "DNA", visible: true, color: "#0000ff", range: [10, 900] });
    assert.equal(view.hd_mode, false);
    assert.equal("version" in view, false);
});

await check("...else the legacy channel list", () => {
    const old = controller();
    old.vocabulary = {};
    const view = old.currentView();
    assert.equal(Array.isArray(view), true);
    same(view, [{ name: "DNA", color: "#0000ff", range: [10, 900] },
                { name: "CD3", color: "#ff0000", range: [10, 900] }]);
    window.PlexoraViewSnapshot = undefined;
    try {
        assert.equal(Array.isArray(controller().currentView()), true);
    } finally {
        window.PlexoraViewSnapshot = RealSnapshot;
    }
});

// -- clicking a region -------------------------------------------------------------

function recordingSnapshots() {
    const record = { viewport: [], hd: [] };
    window.PlexoraViewSnapshot = {
        ...RealSnapshot,
        restoreViewport: (view, options) => { record.viewport.push([view, options]); return true; },
        restoreHdMode: (view) => { record.hd.push(view); return false; },
    };
    return record;
}

const VIEW = { viewport: { x: 1, y: 2, width: 300, height: 200 }, hd_mode: true,
               channels: [{ name: "DNA", visible: true }] };
const REGION = { roi_id: "qcroi_1", category: "blur_focus", bbox: [0, 0, 10, 10],
                 created_by: "user", method: "sam", channels: [] };

await check("a region that remembers its view gets it back, animated, not a box fit", () => {
    const record = recordingSnapshots();
    try {
        const c = controller();
        c.focusRegion({ ...REGION, view: VIEW });
        assert.equal(record.viewport.length, 1);
        same(record.viewport[0][1], { immediately: false });
        assert.equal(record.viewport[0][0].viewport.width, 300);
        assert.equal(record.hd.length, 1);
        same(c.calls, []);
        assert.equal(c.selectedRegion, "qcroi_1");
    } finally {
        window.PlexoraViewSnapshot = RealSnapshot;
    }
});

await check("...and the real restore reaches the scene with {immediately: false}", () => {
    sceneCalls.length = 0;
    const c = controller();
    c.focusRegion({ ...REGION, view: { ...VIEW, hd_mode: undefined } });
    assert.equal(sceneCalls.length, 1);
    same(sceneCalls[0][3], { immediately: false });
    same(c.calls, []);
});

await check("a region without one is framed by its box", () => {
    const record = recordingSnapshots();
    try {
        const c = controller();
        c.focusRegion(REGION);
        same(c.calls, [["fit", [0, 0, 10, 10]]]);
        assert.equal(record.viewport.length, 0);
        c.calls.length = 0;
        c.focusRegion({ ...REGION, view: { channels: [] } });
        same(c.calls, [["fit", [0, 0, 10, 10]]]);
    } finally {
        window.PlexoraViewSnapshot = RealSnapshot;
    }
});

await check("the region menu offers 'Show it as it was drawn' only when it remembers a view", () => {
    const c = controller();
    const withView = c.menuFor({ kind: "region", key: "r:qcroi_1", ref: { ...REGION, view: VIEW } });
    assert.equal(withView[0].label, "Show it as it was drawn");
    const without = c.menuFor({ kind: "region", key: "r:qcroi_1", ref: REGION });
    assert.equal(without[0].label, "Zoom to region");
});

// -- E ------------------------------------------------------------------------------

function keyed(c) {
    c.toggled = 0;
    c.toggleMagic = () => { c.toggled += 1; };
    return (extra = {}) => {
        const event = { key: "e", repeat: false, ctrlKey: false, metaKey: false, altKey: false,
                        prevented: false, preventDefault() { event.prevented = true; }, ...extra };
        c.magicKey(event);
        return event;
    };
}

await check("E toggles magic select when the QC panel owns the keyboard", () => {
    const c = controller();
    const press = keyed(c);
    window.PlexoraToolLoader = { activeTool: () => "qc" };
    try {
        assert.equal(press().prevented, true);
        press({ key: "E" });
        assert.equal(c.toggled, 2);
    } finally {
        window.PlexoraToolLoader = undefined;
    }
});

await check("...and is left alone while typing, with a modifier, a dialog, or another tool up", () => {
    const c = controller();
    const press = keyed(c);
    window.PlexoraToolLoader = { activeTool: () => "qc" };
    try {
        document.activeElement = { tagName: "INPUT" };
        press();
        document.activeElement = { tagName: "DIV", isContentEditable: true };
        press();
        document.activeElement = null;
        press({ ctrlKey: true });
        press({ metaKey: true });
        press({ altKey: true });
        press({ repeat: true });
        press({ key: "r" });
        dialogOpen = true;
        press();
        dialogOpen = false;
        window.PlexoraToolLoader = { activeTool: () => "roi" };
        assert.equal(press().prevented, false);
        assert.equal(c.toggled, 0);
    } finally {
        document.activeElement = null;
        dialogOpen = false;
        window.PlexoraToolLoader = undefined;
    }
});

await check("toggleMagic: off when on, on in the category in hand, else the last one reopened", () => {
    const c = controller();
    const modes = [];
    c.setDrawMode = (mode) => modes.push(mode);
    const drawn = [];
    c.startDrawing = (target, options) => { drawn.push([target, options]); return Promise.resolve(true); };
    c.magic = { active: true };
    c.toggleMagic();
    c.magic = { active: false };
    c.drawTarget = { color: "#fff" };
    c.toggleMagic();
    same(modes, ["draw", "magic"]);
    c.drawTarget = null;
    c.lastTarget = { category: "blur_focus" };
    c.toggleMagic();
    same(drawn, [[{ category: "blur_focus" }, { mode: "magic" }]]);
});

await check("toggleMagic with nothing in hand opens the picker at the header wand, in magic mode", () => {
    const c = controller();
    const opened = [];
    c.openDrawMenu = (at, options) => opened.push([at, options]);
    c.magic = { active: false };
    c.drawTarget = null;
    c.lastTarget = null;
    const wand = { id: "qc_magic" };
    c.toggleMagic(wand);
    assert.equal(opened.length, 1);
    assert.equal(opened[0][0], wand);
    same(opened[0][1], { mode: "magic" });
});

function drawingController(state) {
    const c = controller();
    c.act = (_label, fn) => fn();
    c.api = { addCategory: async () => ({ key: "blur_focus", label: "QC: Blur / focus issue",
                                          color: "#f97316" }) };
    c.categoryWords = () => "blur";
    c.hover = { gesture() {} };
    c.modes = [];
    c.setDrawMode = (mode) => c.modes.push(mode);
    c.renderDrawbar = () => {};
    c.freehand = { start: () => true, pan() {}, stop() {} };
    c.magic = { stop() {}, active: false };
    window.PlexoraSegment = { ...RealSegment, status: async () => state };
    return c;
}

await check("startDrawing puts magic select in hand by default; the pen only when asked", async () => {
    try {
        const c = drawingController({ state: "ready" });
        assert.equal(await c.startDrawing({ category: "blur_focus" }), true);
        same(c.modes, ["magic"]);
        assert.ok(/line over/.test(c.said.at(-1)), c.said.at(-1));
        const pen = drawingController({ state: "ready" });
        await pen.startDrawing({ category: "blur_focus" }, { mode: "draw" });
        same(pen.modes, []);
        assert.ok(/press and drag/.test(pen.said.at(-1)), pen.said.at(-1));
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("...and the pen where the server cannot run the model", async () => {
    try {
        const c = drawingController({ state: "not_installed_runtime" });
        await c.startDrawing({ category: "blur_focus" });
        same(c.modes, []);
        assert.ok(/press and drag/.test(c.said.at(-1)), c.said.at(-1));
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("setDrawMode switches the pen and the wand, and does nothing with no category", () => {
    const c = controller();
    const log = [];
    c.freehand = { pan: () => log.push("pen.pan"), start: (color) => log.push(`pen.start ${color}`) };
    c.magic = { start: (color) => log.push(`magic.start ${color}`), stop: () => log.push("magic.stop") };
    c.renderDrawbar = () => log.push("render");
    c.drawTarget = null;
    c.setDrawMode("magic");
    same(log, []);
    c.drawTarget = { color: "#abc" };
    c.setDrawMode("magic");
    c.setDrawMode("draw");
    c.setDrawMode("pan");
    same(log, ["pen.pan", "magic.start #abc", "render",
               "magic.stop", "pen.start #abc", "render",
               "magic.stop", "pen.pan", "render"]);
});

// -- storing an outline ------------------------------------------------------------

await check("magicSelected hands the planner the selected region's box and outline", () => {
    const c = controller();
    const geometry = { type: "Polygon", coordinates: [[[10, 20], [110, 20], [110, 70], [10, 20]]] };
    c.regionData = { regions: [{ roi_id: "qcroi_1", category: "blur_focus",
                                 bbox: [10, 20, 110, 70], locked: true, geometry }] };
    assert.equal(c.magicSelected(), null);
    c.selectedRegion = "qcroi_1";
    same(c.magicSelected(), { id: "qcroi_1", locked: true, geometry,
                              bbox: { x: 10, y: 20, width: 100, height: 50 } });
    c.hidden.add("c:blur_focus");
    assert.equal(c.magicSelected(), null);
});

await check("saveGeometry stores a new outline as magic select's, with the view it was made in", async () => {
    const c = controller();
    const sent = [];
    c.api = {
        drawRegion: async (body) => { sent.push(["draw", body]); return { ok: true,
            data: { roi: { id: "qcroi_9", name: "Blur / focus issue 1" } } }; },
        reshapeRegion: async (...args) => { sent.push(["reshape", ...args]);
            return { ok: true, data: {} }; },
    };
    c.reload = async () => {};
    c.overlay = {};
    c.drawTarget = { target: { category: "blur_focus" }, label: "Blur", words: "Blur", color: "#f00" };
    const geometry = { type: "Polygon", coordinates: [[[0, 0], [5, 0], [5, 5], [0, 0]]] };
    assert.equal(await c.saveGeometry({ roiId: null, geometry }), "qcroi_9");
    const [kind, body] = sent[0];
    assert.equal(kind, "draw");
    assert.equal(body.category, "blur_focus");
    assert.equal(body.method, "sam");
    same(body.geometry, geometry);
    assert.equal(Array.isArray(body.views), false);
    assert.equal(body.views.sample, "sample-A");
    assert.equal(c.selectedRegion, "qcroi_9");
    assert.equal(await c.saveGeometry({ roiId: "qcroi_9", geometry }), "qcroi_9");
    same(sent[1], ["reshape", "qcroi_9", geometry, "sam"]);
});

// -- QcMagic ---------------------------------------------------------------------

/** PlexoraMagicBar, recording: what was shown, for whom, and what it was told. */
const bar = {
    log: [],
    current: null,
    show(options) {
        bar.current = options;
        bar.log.push(["show", options.mode]);
        return { setMode: (m) => bar.log.push(["mode", m]),
                 setBusy: (b) => bar.log.push(["busy", b]),
                 hide: () => { bar.log.push(["hide"]); bar.current = null; },
                 isShown: () => bar.current === options };
    },
    hide() { bar.log.push(["hide"]); bar.current = null; },
};
window.PlexoraMagicBar = bar;

function magicHarness(answer) {
    const viewer = { handlers: [], addHandler(name) { this.handlers.push(name); },
                     removeHandler() {}, canvas: { style: {} },
                     world: { getItemAt: () => ({ source: { getImagePixel: (_, p) => [p.x, p.y] } }) },
                     viewport: { getZoom: () => 1 } };
    const ctx = { viewer: { viewer }, config: { width: 1000, height: 1000 }, datasource: "s1" };
    const overlay = { draft: null, prompts: null, schedule() {} };
    const pen = new window.QcFreehand(ctx, overlay);
    const magic = new window.QcMagic(ctx, overlay, pen);
    const segment = { ...RealSegment, ready: 0, calls: [],
                      ensureReady() { segment.ready += 1; return Promise.resolve(true); },
                      point(request) { segment.calls.push(request); return Promise.resolve(answer(request)); } };
    window.PlexoraSegment = segment;
    bar.log = [];
    bar.current = null;
    const commits = [];
    const said = [];
    magic.canCreate = () => true;
    magic.onCommit = async (change) => { commits.push(change); return change.roiId || "qcroi_new"; };
    magic.onMessage = (text) => said.push(text);
    return { magic, segment, commits, said, overlay, viewer };
}

const square = (request) => {
    const b = request.box;
    const p = request.points.at(-1) || { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    return { ok: true, geometry: { type: "Polygon", coordinates: [[[p.x - 20, p.y - 20],
        [p.x + 20, p.y - 20], [p.x + 20, p.y + 20], [p.x - 20, p.y + 20], [p.x - 20, p.y - 20]]] },
             bbox: { x: p.x - 20, y: p.y - 20, width: 40, height: 40 }, flags: {} };
};
const qcClick = (x, y, shift = false) => ({ position: { x, y }, quick: true,
                                            preventDefaultAction: false,
                                            originalEvent: { shiftKey: shift } });

await check("QcMagic: arming starts the setup and takes clicks from the viewer", () => {
    const { magic, segment, viewer } = magicHarness(square);
    try {
        assert.equal(magic.start("#f00"), true);
        assert.equal(segment.ready, 1);
        assert.ok(viewer.handlers.includes("canvas-click"));
        assert.equal(viewer.canvas.style.cursor, "crosshair");
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: a click commits a new outline, the next one near it grows the same region", async () => {
    const { magic, segment, commits, overlay } = magicHarness(square);
    try {
        magic.start();
        magic.setMode("add");   // Box is the default; this check clicks
        const event = qcClick(500, 500);
        magic.click(event);
        assert.equal(event.preventDefaultAction, true);
        await settled();
        assert.equal(commits.length, 1);
        assert.equal(commits[0].roiId, null);
        magic.click(qcClick(520, 500));
        await settled();
        assert.equal(segment.calls.length, 2);
        assert.equal(commits[1].roiId, "qcroi_new");
        same(overlay.prompts.map((p) => p.label), [1, 1]);
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: with no category it says what to pick, and asks nothing", async () => {
    const { magic, segment, said } = magicHarness(square);
    try {
        magic.canCreate = () => false;
        magic.start();
        magic.setMode("add");   // Box is the default; this check clicks
        magic.click(qcClick(500, 500));
        await settled();
        assert.equal(segment.calls.length, 0);
        same(said, ["Pick what you are marking first (the + above)."]);
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: it yields while the ROI tool is on screen", async () => {
    const { magic, segment } = magicHarness(square);
    window.PlexoraToolLoader = { isToolVisible: (name) => name === "roi" };
    try {
        magic.start();
        magic.setMode("add");   // Box is the default; this check clicks
        const event = qcClick(500, 500);
        magic.click(event);
        await settled();
        assert.equal(segment.calls.length, 0);
        assert.equal(event.preventDefaultAction, false);
    } finally {
        window.PlexoraToolLoader = undefined;
        window.PlexoraSegment = RealSegment;
    }
});

const SELECTED_OUTLINE = { type: "Polygon", coordinates: [[[400, 400], [600, 400], [600, 600],
                                                          [400, 600], [400, 400]]] };
const selectedRegion = () => ({ id: "qcroi_1", locked: false, geometry: SELECTED_OUTLINE,
                                bbox: { x: 400, y: 400, width: 200, height: 200 } });
const at = (x, y) => ({ position: { x, y }, preventDefaultAction: false });

await check("QcMagic: the bar shows in Box on start and goes on stop", () => {
    const { magic } = magicHarness(square);
    try {
        magic.mode = "scribble";
        magic.start();
        assert.equal(magic.mode, "box");
        same(bar.log[0], ["show", "box"]);
        assert.equal(bar.current.owner, magic);
        bar.current.onMode("remove");
        magic.start();
        assert.equal(magic.mode, "remove", "starting again keeps the mode in hand");
        magic.stop();
        assert.equal(bar.current, null);
        assert.equal(magic.active, false);
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: the bar's close is the controller's onClose, else a stop", () => {
    const { magic } = magicHarness(square);
    try {
        let closed = 0;
        magic.onClose = () => { closed += 1; };
        magic.start();
        magic.setMode("add");   // Box is the default; this check clicks
        bar.current.onClose();
        assert.equal(closed, 1);
        assert.equal(magic.active, true);
        magic.onClose = null;
        bar.current.onClose();
        assert.equal(magic.active, false);
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: in Add a drag pans -- no box, the viewer keeps the drag", async () => {
    const { magic, segment, overlay } = magicHarness(square);
    try {
        magic.start();
        magic.setMode("add");
        magic.press(at(300, 300));
        assert.equal(magic.dragOrigin, null);
        const drag = at(400, 400);
        magic.dragging(drag);
        assert.equal(drag.preventDefaultAction, false);
        assert.equal(overlay.draft, null);
        const end = at(400, 400);
        magic.dragEnd(end);
        assert.equal(end.preventDefaultAction, false);
        await settled();
        assert.equal(segment.calls.length, 0);
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: in Box a drag draws a box and prompts with it; a click does nothing", async () => {
    const { magic, segment, commits } = magicHarness(square);
    try {
        magic.start();
        magic.setMode("box");
        magic.click(qcClick(500, 500));
        await settled();
        assert.equal(segment.calls.length, 0);
        magic.press(at(300, 300));
        const drag = at(400, 380);
        magic.dragging(drag);
        assert.equal(drag.preventDefaultAction, true);
        magic.dragEnd(at(400, 380));
        await settled();
        assert.equal(segment.calls.length, 1);
        same(segment.calls[0].box, { x: 300, y: 300, width: 100, height: 80 });
        assert.equal(commits.length, 1);
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: in Scribble a drag is a line, sent as points along it, drawn as it goes", async () => {
    const { magic, segment, commits, overlay } = magicHarness(square);
    try {
        magic.start();
        magic.setMode("scribble");
        magic.press({ ...at(300, 500), originalEvent: { shiftKey: false } });
        const drag = at(400, 500);
        magic.dragging(drag);
        assert.equal(drag.preventDefaultAction, true);
        assert.equal(overlay.draft.scribble, "add");
        for (let x = 410; x <= 650; x += 10) magic.dragging(at(x, 500));
        magic.dragEnd(at(650, 500));
        assert.equal(overlay.draft, null);
        await settled();
        assert.equal(segment.calls.length, 1);
        const points = segment.calls[0].points;
        assert.equal(points.length, 8);
        assert.ok(points.every((p) => p.label === 1), JSON.stringify(points));
        assert.equal(commits.length, 1);
        magic.click(qcClick(700, 700));   // a tap with the brush: one more point
        await settled();
        assert.equal(segment.calls.length, 2);
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: Remove inside the selected region carves it, seeded with its outline", async () => {
    const { magic, segment, commits } = magicHarness(square);
    try {
        magic.selected = selectedRegion;
        magic.start();
        magic.setMode("remove");
        magic.click(qcClick(500, 500));
        await settled();
        assert.equal(segment.calls.length, 1);
        same(segment.calls[0].points, [{ x: 500, y: 500, label: 0 }]);
        same(segment.calls[0].maskGeometry, SELECTED_OUTLINE);
        assert.equal(commits[0].roiId, "qcroi_1");
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

await check("QcMagic: a plain click inside it refines it, with no seed", async () => {
    const { magic, segment, commits } = magicHarness(square);
    try {
        magic.selected = selectedRegion;
        magic.start();
        magic.setMode("add");   // Box is the default; this check clicks
        magic.click(qcClick(500, 500));
        await settled();
        same(segment.calls[0].points, [{ x: 500, y: 500, label: 1 }]);
        assert.equal(segment.calls[0].maskGeometry ?? null, null);
        assert.ok(segment.calls[0].box);
        assert.equal(commits[0].roiId, "qcroi_1");
    } finally {
        window.PlexoraSegment = RealSegment;
    }
});

process.exit(failures.length ? 1 : 0);
