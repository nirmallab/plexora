/**
 * Quick Edit while a session is still opening.
 *
 * `open()` makes the session at once and then waits: for the source's
 * description, for each shown channel's range (`pixel_info`), for the 3-D
 * renderer. Until that is done there is no channel widget and nothing has been
 * drawn. Three claims about that window:
 *
 *   * Done (or following the selection to another panel, which saves the one
 *     being left) must not save a half-opened session. Saving it wrote the
 *     panel with no channels and a black preview -- and for a 3-D panel that
 *     preview is the picture the export is made from.
 *
 *   * an open that has been left (closed, or replaced by another open) must not
 *     carry on into the session that replaced it: no second channel widget, no
 *     writes onto the next session.
 *
 *   * nor throw when it finds no session at all.
 *
 * Run directly:
 *   node tests/js/figure_quickedit_loading_commit_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["../../../client/src/js/services/viewerScene.js", "figureSchema.js",
                 "figurePanelCompositor.js", "figureActions.js", "figureQuickEdit.js"];

const problems = [];

function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

// -- the world Quick Edit runs in (as figure_quickedit_probe.mjs builds it) ---

function contextStub() {
    return {
        setTransform() {}, clearRect() {}, fillRect() {}, strokeRect() {},
        drawImage() {}, putImageData() {},
        fillStyle: "", strokeStyle: "", lineWidth: 1,
    };
}

function canvasStub(width, height) {
    return {
        width: width || 1, height: height || 1, style: {},
        parentElement: { getBoundingClientRect: () => ({ width: 400, height: 300 }),
                         appendChild() {} },
        handlers: {},
        addEventListener(type, fn) { this.handlers[type] = fn; },
        remove() {},
        setPointerCapture() {},
        getContext() { return contextStub(); },
        toDataURL() { return "data:image/webp;base64,LIVE"; },
        toBlob(resolve) { resolve({ type: "image/webp" }); },
    };
}

function elementStub() {
    return {
        hidden: true, innerHTML: "", textContent: "", value: "", max: "",
        handlers: {}, attributes: {},
        addEventListener(type, fn) { this.handlers[type] = fn; },
        querySelector() { return null; },
        setAttribute(name, value) { this.attributes[name] = value; },
        removeAttribute(name) { delete this.attributes[name]; },
        classList: { toggle: () => {} },
    };
}

const elements = {};
for (const id of ["fb_quickedit", "fb_quickedit_done", "fb_quickedit_cancel",
                  "fb_quickedit_close", "fb_quickedit_main", "fb_quickedit_title",
                  "fbqe_channel_slot_list", "fb_quickedit_z", "fb_quickedit_z_range",
                  "fb_quickedit_z_label", "fb_quickedit_note", "fb_quickedit_channels"]) {
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
ctx.Image = class {
    constructor() { this.complete = false; this.naturalWidth = 0; }
};
ctx.ImageData = class {
    constructor(width, height) {
        this.width = width;
        this.height = height;
        this.data = new Uint8ClampedArray(width * height * 4);
    }
};

const log = [];
ctx.ChannelList = { events: { CHANNELS_CHANGE: "cc", COLOR_TRANSFER_CHANGE: "ctc", BRUSH_MOVE: "bm" } };
ctx.FigureConfirm = { tell: () => log.push("confirm") };
ctx.SimpleEventHandler = class {
    constructor(element) { this.element = element; }
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

const renderers = [];
ctx.window.PlexoraVolumeWindows = class {
    constructor(datasource) { this.datasource = datasource; }
};
ctx.window.PlexoraVolumeRenderer = class {
    static supported() { return true; }
    constructor(options) { this.options = options; renderers.push(this); }
    mount() { return true; }
    restoreCamera() { return true; }
    setArea() { return true; }
    show() {}
    async refresh() {}
    invalidate() {}
    cameraState() { return { q: [0, 0, 0, 1], dist: 2.2, pan: [0, 0] }; }
    snapshot(size) { return canvasStub(size ? size.width : 300, size ? size.height : 300); }
    destroy() {}
};

for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

// -- the figure under it -----------------------------------------------------

function panelRecord(id, volume) {
    return {
        panel_id: id, source_id: "src_1", title: id, render_revision: 3,
        placement: { x_mm: 0, y_mm: 0, w_mm: 40, h_mm: 40, z: 1, page_id: "pg_1" },
        scene: {
            viewport: { x: 0, y: 0, w: 512, h: 512 },
            channels: [{ key: "DNA", fullname_at_capture: "DNA",
                         color: { r: 0, g: 0, b: 255 }, window: [120, 880], visible: true }],
            ...(volume ? { plugins: { volume: { version: 1, state: volume } } } : {}),
        },
    };
}

const panels = {
    p1: panelRecord("p1"),
    p2: panelRecord("p2"),
    three: panelRecord("three", { mode: "volume", z: 7, t: 0, composite: "mip",
                                  camera: { q: [0, 0, 0, 1], dist: 2.2, pan: [0, 0] },
                                  box: { x: 10, y: 20, width: 300, height: 300 },
                                  inspect: null, interpolation: "linear" }),
};

//: Each pixel_info call, held until the test answers it.
let held = [];
const commits = [];
const previews = [];
const canvasSpy = {
    previewOverrides: new Map(),
    surfaceEl: { querySelector: () => null },
    render: () => {},
};

function build({ loaded = "" } = {}) {
    log.length = 0;
    held = [];
    sidebars.length = 0;
    commits.length = 0;
    previews.length = 0;
    renderers.length = 0;
    canvasSpy.previewOverrides.clear();
    ctx.__deps = {
        workspace: { canvas: canvasSpy },
        figureId: "fig_1",
        loadedDatasource: loaded,
        api: {
            describeSource: async () => ({ ok: true, data: { source: {
                volume: { depth: 40, frames: 1, width: 1024, height: 1024 },
                channels: [{ key: "DNA", fullname_at_capture: "DNA" },
                           { key: "CD8", fullname_at_capture: "CD8" }] } } }),
            previewUrl: (figureId, panelId, revision) => `/preview/${panelId}/${revision}`,
            // The slow part of opening: on the website it queues on the page's
            // one Python thread, against a node it crosses the network.
            pixelInfo: (figureId, sourceId, key) => new Promise((resolve) => {
                held.push(() => resolve({ ok: true, data: { stats:
                    { min: 0, max: 1000, p01: 10, p999: 900 } } }));
            }),
            readPixels: (figureId, sourceId, params) => Promise.resolve({
                ok: true, width: params.out_w, height: params.out_h,
                data: new Uint16Array(params.out_w * params.out_h) }),
            putPreview: async (figureId, panelId, revision, blob, size) => {
                previews.push({ panelId, size });
                return { ok: true };
            },
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
                commits.push(JSON.parse(JSON.stringify(ops)));
                return Promise.resolve(true);
            },
        },
        onOpenInViewer: (panelId) => log.push("openInViewer:" + panelId),
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
const written = () => commits.map((ops) => ({
    panel: ops[0].panel_id, channels: ops[0].changes.scene.channels }));
const uploaded = () => previews.map((entry) => entry.panelId);

// -- Done while the channels are still loading ---------------------------------

const doneEarly = await (async () => {
    const quickEdit = build();
    quickEdit.open("p1");          // the user double-clicked; still opening
    await settled();
    await quickEdit.commit();      // and pressed Done
    await settled();
    return { written: written(), uploaded: uploaded() };
})();
check("Done on a session still opening writes no panel (no channels, black preview)",
    doneEarly.written, []);
check("and uploads no preview cut from a view that has drawn nothing",
    doneEarly.uploaded, []);

// -- Open in Main Viewer while loading -------------------------------------------

const mainEarly = await (async () => {
    const quickEdit = build();
    quickEdit.open("p1");
    await settled();
    await quickEdit.openInViewer();
    await settled();
    return { written: written(), uploaded: uploaded() };
})();
check("Open in Main Viewer on a session still opening saves nothing over the panel",
    mainEarly, { written: [], uploaded: [] });

// -- clicking another panel while loading ----------------------------------------

const followEarly = await (async () => {
    const quickEdit = build();
    quickEdit.open("p1");
    await settled();
    const switching = quickEdit.update(["p2"]);   // the user clicked p2 on the canvas
    await settled();
    for (const answer of held.splice(0)) answer();
    await switching;
    await settled();
    return { written: written().filter((entry) => entry.panel === "p1"),
             uploaded: uploaded().filter((panelId) => panelId === "p1") };
})();
check("clicking another panel while the first is opening does not save the first",
    followEarly, { written: [], uploaded: [] });

// -- a 3-D panel: its preview IS the exported picture ---------------------------

const threeEarly = await (async () => {
    const quickEdit = build({ loaded: "demo" });
    quickEdit.open("three");
    await settled();
    await settled();
    const rendererMounted = renderers.length;
    await quickEdit.commit();
    await settled();
    return { rendererMounted, written: written(), uploaded: uploaded() };
})();
check("Done on a 3-D panel before its renderer is up stores no picture over its master",
    { written: threeEarly.written, uploaded: threeEarly.uploaded },
    { written: [], uploaded: [] });

// -- an abandoned open does not carry on into the next session ------------------

const superseded = await (async () => {
    const quickEdit = build();
    const first = quickEdit.open("p1");
    await settled();
    const p1Answers = held.splice(0);
    // A double-click on p2 opens it (no save on the way: open() just tears down).
    const second = quickEdit.open("p2");
    await settled();
    for (const answer of held.splice(0)) answer();
    await second;
    await settled();
    const p2Sidebar = quickEdit.sidebar;
    // Now p1's late channel ranges turn up.
    for (const answer of p1Answers) answer();
    let threw = null;
    await first.catch((error) => { threw = String(error); });
    await settled();
    return {
        panelId: quickEdit.session && quickEdit.session.panelId,
        sameSidebar: quickEdit.sidebar === p2Sidebar,
        sidebars: sidebars.length,
        threw,
    };
})();
check("a superseded open leaves the new session's channel widget in place",
    { panelId: superseded.panelId, sameSidebar: superseded.sameSidebar,
      sidebars: superseded.sidebars },
    { panelId: "p2", sameSidebar: true, sidebars: 1 });

const cancelled = await (async () => {
    const quickEdit = build();
    const opening = quickEdit.open("p1");
    await settled();
    quickEdit.close();              // Cancel, while it is still opening
    for (const answer of held.splice(0)) answer();
    let threw = null;
    await opening.catch((error) => { threw = String(error); });
    await settled();
    return { threw, sidebars: sidebars.length, session: quickEdit.session };
})();
check("an open cancelled mid-way finishes quietly, without a widget or a session",
    cancelled, { threw: null, sidebars: 0, session: null });

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
