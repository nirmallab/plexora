/**
 * Quick Edit's session, its fetching, and what it puts on the canvas.
 *
 * Four claims, all of which ship green and are wrong somewhere a user only
 * meets later:
 *
 *   * the mini view draws the pixels it holds through the CURRENT framing. If
 *     it draws them where they were when they arrived, a pan moves nothing
 *     until the refetch lands and the whole thing feels dead -- which is what
 *     it did, and it looked like a slow server rather than like arithmetic.
 *
 *   * a superseded batch of pixels must not land. A pan fires refetches faster
 *     than they return, and without a sequence number the last response to
 *     arrive wins rather than the last one asked for -- so a pan ENDS on
 *     whichever framing the network happened to finish last.
 *
 *   * switching panels saves the one being left. Selection-follow that dropped
 *     the session would make clicking the next panel the cheapest way to lose
 *     ten minutes of channel work.
 *
 *   * the panel on the figure shows the unsaved picture only while the session
 *     is open. An override left behind is a figure that looks committed and is
 *     not; one cleared too early is a panel that blinks back to the old raster
 *     while the upload is still in flight.
 *
 * Run directly:
 *   node tests/js/figure_quickedit_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
// The real schema, because `aspectViewport` is the one definition of "which
// region does a panel of this shape show" and a stub of it would agree with
// Quick Edit by construction rather than by being the same code. The
// compositor for the same reason: it is the one copy of the arithmetic the
// mini view draws with.
const SCRIPTS = ["figureSchema.js", "figurePanelCompositor.js", "figureQuickEdit.js"];

const problems = [];

function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

// -- the world Quick Edit runs in -------------------------------------------

function contextStub() {
    return {
        setTransform() {}, clearRect() {}, fillRect() {}, strokeRect() {},
        drawImage() {}, putImageData() {},
        fillStyle: "", strokeStyle: "", lineWidth: 1,
    };
}

/** A canvas with the handful of methods this code actually calls. */
function canvasStub(width, height) {
    return {
        width: width || 1, height: height || 1, style: {},
        parentElement: { getBoundingClientRect: () => ({ width: 400, height: 300 }) },
        handlers: {},
        addEventListener(type, fn) { this.handlers[type] = fn; },
        setPointerCapture() {},
        getContext() { return contextStub(); },
        toDataURL() { return "data:image/webp;base64,LIVE"; },
        toBlob(resolve) { resolve({ type: "image/webp" }); },
    };
}

function elementStub() {
    return {
        hidden: true, innerHTML: "", textContent: "",
        addEventListener() {},
        querySelector() { return null; },
    };
}

const elements = {};
for (const id of ["fb_quickedit", "fb_quickedit_done", "fb_quickedit_cancel",
                  "fb_quickedit_close", "fb_quickedit_main", "fb_quickedit_title",
                  "fbqe_channel_slot_list"]) {
    elements[id] = elementStub();
}
elements.fb_quickedit_canvas = canvasStub();

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, parseFloat, parseInt, isNaN,
    RegExp, isFinite, Uint8ClampedArray, Uint16Array, AbortController,
    setTimeout, clearTimeout,
    document: {
        readyState: "complete", activeElement: null,
        getElementById: (id) => elements[id] || null,
        createElement: () => canvasStub(),
        addEventListener() {}, removeEventListener() {},
    },
    window: {
        devicePixelRatio: 2,
        addEventListener() {}, removeEventListener() {},
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (handle) => clearTimeout(handle),
    },
});
ctx.globalThis = ctx;
ctx.ImageData = class {
    constructor(width, height) {
        this.width = width;
        this.height = height;
        this.data = new Uint8ClampedArray(width * height * 4);
    }
};

// The log every stub writes to, so ORDER across the api, the document state
// and the canvas is one sequence rather than three that have to be reconciled.
const log = [];
ctx.__log = (entry) => log.push(entry);

ctx.ChannelList = { events: { CHANNELS_CHANGE: "cc", COLOR_TRANSFER_CHANGE: "ctc", BRUSH_MOVE: "bm" } };
ctx.FigureConfirm = { tell: () => __log("confirm") };

const buses = [];
ctx.SimpleEventHandler = class {
    constructor(element) {
        this.element = element;
        buses.push(element);
    }
    bind() {}
    trigger() {}
};

const sidebars = [];
ctx.ViewerSidebar = class {
    constructor(config, columns, dataLayer, eventHandler, channelList, options) {
        this.eventHandler = eventHandler;
        this.options = options;
        this.channelSlots = columns.map((name, index) => ({
            index: index, name: name, enabled: index === 0,
            color: { r: 255, g: 255, b: 255 }, range: [100, 900],
        }));
        sidebars.push(this);
    }
    async init() {}
    setSlotMarker() {}
    setSlotColor() {}
    setSlotEnabled(index, on) { this.channelSlots[index].enabled = on; }
    setSlotRange(index, range) { this.channelSlots[index].range = range; }
    rgbToHex() { return "#ffffff"; }
    quantWindow() { return { qmin: 0, qmax: 65535 }; }
    rawToByteRange(range) { return range; }
    toRawRangeForSlot(slot) { return slot.range; }
};

for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

// -- the figure under it -----------------------------------------------------

//: Resolvers for readPixels calls, when `manualPixels` is on. A test that hands
//: back the answers itself is the only way to make one batch overtake another.
let pending = [];
let manualPixels = false;
let commitResolver = null;
let manualCommit = false;

function panelRecord(id) {
    return {
        panel_id: id, source_id: "src_1", title: id, render_revision: 3,
        placement: { x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 40, z: 1, page_id: "pg_1" },
        scene: {
            viewport: { x: 0, y: 0, w: 512, h: 512 },
            channels: [{ key: "DNA", fullname_at_capture: "DNA",
                         color: { r: 0, g: 0, b: 255 }, window: [120, 880], visible: true }],
        },
    };
}

const panels = { p1: panelRecord("p1"), p2: panelRecord("p2") };
const canvasSpy = {
    previewOverrides: new Map(),
    surfaceEl: { querySelector: () => null },
    render: () => log.push("canvas.render"),
};

function build() {
    log.length = 0;
    pending = [];
    buses.length = 0;
    sidebars.length = 0;
    canvasSpy.previewOverrides.clear();
    ctx.__deps = {
        workspace: { canvas: canvasSpy },
        figureId: "fig_1",
        api: {
            pixelInfo: async (figureId, sourceId, key) => {
                log.push("pixelInfo:" + key);
                return { ok: true, data: { stats: { min: 0, max: 1000, p01: 10, p999: 900 } } };
            },
            readPixels: (figureId, sourceId, params) => {
                log.push("readPixels:" + params.channel);
                const answer = {
                    ok: true, width: params.out_w, height: params.out_h,
                    data: new Uint16Array(params.out_w * params.out_h),
                };
                if (!manualPixels) return Promise.resolve(answer);
                return new Promise((resolve) => pending.push({ params, resolve, answer }));
            },
            putPreview: async () => { log.push("putPreview"); return { ok: true }; },
        },
        state: {
            sourceStatus: {},
            panel: (id) => panels[id] || null,
            source: () => ({
                kind: "plexora_project", datasource: "demo", display_name: "Demo",
                image: { width: 1024, height: 1024 },
                channels: [{ key: "DNA", fullname_at_capture: "DNA" },
                           { key: "CD8", fullname_at_capture: "CD8" }],
            }),
            commit: (ops) => {
                log.push("commit:" + ops[0].panel_id);
                if (!manualCommit) return Promise.resolve(true);
                return new Promise((resolve) => { commitResolver = () => resolve(true); });
            },
        },
        onOpenInViewer: () => log.push("openInViewer"),
    };
    return runInContext(`
        (() => {
            const quickEdit = new FigureQuickEdit(__deps);
            quickEdit.setup();
            return quickEdit;
        })()
    `, ctx);
}

const settled = () => new Promise((resolve) => setTimeout(resolve, 0));

// -- projecting held pixels through the current view -------------------------
//
// The whole of the pan fix. Pure arithmetic, so it is checked as arithmetic.

const project = (box, region) =>
    runInContext("FigureQuickEdit.projectBox", ctx)(box, region);

const box = { x: 100, y: 50, w: 200, h: 100 };
check("pixels sitting exactly where the view starts draw at the origin",
    project(box, { x: 100, y: 50, perPixel: 2 }),
    { x: 0, y: 0, w: 100, h: 50 });
// A pan is the view moving over pixels that stay where they are. 50 image
// pixels right at 2 image pixels per CSS pixel is 25 CSS pixels left.
check("panning the view moves the pixels the other way, to scale",
    project(box, { x: 150, y: 50, perPixel: 2 }).x, -25);
check("zooming in draws the same pixels bigger",
    project(box, { x: 100, y: 50, perPixel: 1 }),
    { x: 0, y: 0, w: 200, h: 100 });

// -- the session -------------------------------------------------------------

const opened = await (async () => {
    const quickEdit = build();
    await quickEdit.open("p1");
    await settled();
    return { quickEdit: quickEdit, log: [...log] };
})();

check("opening asks for the panel's channel pixels",
    opened.log.some((entry) => entry.startsWith("readPixels:")), true);
// Pinned because it is the only reason the sliders are 16-bit here: there is no
// viewer on this page for `isHdMode` to ask, so the default answer is "no" and
// every window would go through a lossy byte round trip.
check("the channel widget is pinned to HD",
    sidebars[0].options.hdMode, true);
// SimpleEventHandler has no unbind. A bus on document.body outlives its session
// and repaints into a dead one.
check("the session's event bus is its own element",
    buses[0] !== ctx.document && buses[0] !== null, true);

const reopened = await (async () => {
    const quickEdit = build();
    await quickEdit.open("p1");
    await quickEdit.open("p1");
    await settled();
    return { buses: buses.length, distinct: buses[0] !== buses[1] };
})();
check("reopening builds a second bus", reopened.buses, 2);
check("and not the same one twice", reopened.distinct, true);

// -- following the selection -------------------------------------------------

const follow = await (async () => {
    const quickEdit = build();
    await quickEdit.open("p1");
    await settled();
    log.length = 0;
    await quickEdit.update(["p2"]);
    await settled();
    return { log: [...log], panelId: quickEdit.session.panelId };
})();
check("the session moves to the panel that was selected", follow.panelId, "p2");
// Before anything is fetched for the new panel. The work already done on the
// old one is work, and moving forward must not be how it is lost.
check("and the one being left is saved first",
    follow.log.indexOf("commit:p1") < follow.log.findIndex(
        (entry) => entry.startsWith("pixelInfo:")), true);

const ignored = await (async () => {
    const quickEdit = build();
    await quickEdit.open("p1");
    await settled();
    log.length = 0;
    await quickEdit.update(["p1", "p2"]);
    await quickEdit.update([]);
    await quickEdit.update(["ann_1"]);
    await settled();
    return { log: [...log], panelId: quickEdit.session.panelId };
})();
// None of these is an instruction about which panel to quick-edit, and acting
// on them would make the slide-over flicker every time a box was drawn.
check("a multi-selection, an empty one and a non-panel leave the session alone",
    ignored, { log: [], panelId: "p1" });

// -- superseded fetches ------------------------------------------------------

const overtaken = await (async () => {
    const quickEdit = build();
    manualPixels = true;
    // Not awaited: with the answers held back, opening does not finish until
    // this test hands them over, which is the point.
    const opening = quickEdit.open("p1");
    await settled();
    const first = pending.splice(0);

    // The user pans, which asks for a different region.
    quickEdit.session.view.cx += 300;
    const panning = quickEdit.refresh();
    await settled();
    const second = pending.splice(0);

    // The second answer arrives first, then the first one turns up late.
    for (const call of second) call.resolve(call.answer);
    await settled();
    for (const call of first) call.resolve(call.answer);
    await Promise.all([opening, panning]);
    await settled();
    manualPixels = false;

    const plane = quickEdit.planes.get("DNA");
    return {
        asked: second.length > 0,
        heldX: plane ? Math.round(plane.box.x) : null,
        wantedX: Math.round(second[0].params.x),
    };
})();
check("a second framing was actually asked for", overtaken.asked, true);
// The last framing ASKED FOR wins, not the last one to come back. Without the
// sequence number a pan ends wherever the network happened to finish.
check("a late answer for a framing the user has left is dropped",
    overtaken.heldX, overtaken.wantedX);

// -- what the panel on the figure shows --------------------------------------

const live = await (async () => {
    const quickEdit = build();
    await quickEdit.open("p1");
    await settled();
    quickEdit.syncLivePreview();
    const during = canvasSpy.previewOverrides.get("p1");
    return { during: during };
})();
check("a change is shown on the figure straight away, unsaved",
    live.during, "data:image/webp;base64,LIVE");

const done = await (async () => {
    const quickEdit = build();
    await quickEdit.open("p1");
    await settled();
    manualCommit = true;
    const finishing = quickEdit.commit();
    await settled();
    const midCommit = canvasSpy.previewOverrides.has("p1");
    commitResolver();
    await finishing;
    manualCommit = false;
    return { midCommit: midCommit, after: canvasSpy.previewOverrides.has("p1"),
             log: [...log] };
})();
// The commit re-renders the canvas at a revision the server has no picture for
// yet. Without the override standing in front of it the panel blinks to the old
// raster, or to nothing, for as long as the upload takes.
check("the new picture is in front of the panel before the commit lands",
    done.midCommit, true);
check("and stays until the upload has actually happened",
    done.log.indexOf("putPreview") < done.log.length, true);
check("then the panel goes back to what is saved for it",
    done.after, false);

const cancelled = await (async () => {
    const quickEdit = build();
    await quickEdit.open("p1");
    await settled();
    quickEdit.syncLivePreview();
    log.length = 0;
    quickEdit.close();
    return { log: [...log], override: canvasSpy.previewOverrides.has("p1"),
             session: quickEdit.session };
})();
check("cancelling writes nothing",
    cancelled.log.some((entry) => entry.startsWith("commit")), false);
check("and puts the panel back", cancelled.override, false);
check("and redraws it", cancelled.log.includes("canvas.render"), true);
check("and ends the session", cancelled.session, null);

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
