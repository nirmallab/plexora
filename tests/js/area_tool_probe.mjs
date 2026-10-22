/**
 * The Area tool and V (views/areaTool.js, views/depthStrip.js), in node.
 *
 * Pinned: the square is the prefs' side ON SCREEN over the zoom, so zooming
 * in takes less tissue, and a zoom under a still pointer moves and resizes it;
 * a readout says how much tissue (microns over a stated spacing, else
 * pixels); at an edge it keeps its size, moved inside; on an image smaller
 * than it, the image. Armed, the pointer is followed and drawn on the plane
 * (never in 3-D); a click places it in ONE change of the view state and opens
 * 3-D, OSD's own click-to-zoom held back; unarmed, a click is the image's.
 * V places the square under the pointer when armed, else goes to 3-D and
 * back; it yields to the ROI tool, to typing and to an open dialog. Esc
 * disarms.
 *
 * Run directly:  node tests/js/area_tool_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const documentListeners = {};
const events = [];
let typingNow = false;
let dialogOpen = false;
const box = {
    console, Math, JSON, Number, String, Array, Object, Boolean, Set, Map, Promise,
    setTimeout, clearTimeout,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { events.push(event.type); return true; },
    addEventListener: () => {},
    removeEventListener: () => {},
    PlexoraShortcuts: { isTyping: () => typingNow },
    document: {
        addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
        removeEventListener: () => {},
        querySelector: (selector) => (selector === "dialog[open]" && dialogOpen ? {} : null),
        createElement: () => ({}),
        getElementById: () => null,
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
for (const file of ["services/inspectGeometry.js", "views/areaTool.js", "views/depthStrip.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}
const G = box.PlexoraInspectGeometry;

// -- the viewer: a canvas, an item two image pixels a screen pixel, OSD's tracker --
const trackers = [];
box.OpenSeadragon = { MouseTracker: class { constructor(options) { this.options = options; trackers.push(this); }
                                             destroy() {} } };
//: Image pixels a screen pixel: 2, so an image pixel is half a screen pixel.
let zoom = 2;
box.PlexoraViewTransform = { screenToImage: (viewer, item, p) => ({ x: p.x * zoom, y: p.y * zoom }) };
box.PlexoraViewerScene = { scaleOf: () => 1 / zoom };
const viewportFns = [];
const handlers = {};
const viewer = {
    canvas: { style: {} },
    world: { getItemAt: () => ({}) },
    addHandler: (name, fn) => { (handlers[name] ||= []).push(fn); },
    removeHandler: () => {},
};
const overlays = [];
let invalidated = 0;
const imageViewer = {
    viewer,
    addOverlay: (spec) => { overlays.push(spec); return { invalidate: () => { invalidated += 1; } }; },
    removeOverlay: () => {},
    onViewportChange: (fn) => { viewportFns.push(fn); return () => {}; },
};

// -- the view state ------------------------------------------------------------
const INFO = { width: 4000, height: 3000,
               spacing: { x: { value: 0.5, unit: "µm", source: "metadata" } } };
let state = { mode: "plane", slab: { z0: 0, z1: 39, follow: true }, volume: { area: null }, inspect: {} };
const sets = [];
const subscribers = new Set();
const vs = {
    depth: 40, info: INFO, color: false,
    enabled: () => true,
    get: () => JSON.parse(JSON.stringify(state)),
    set(patch) {
        sets.push(patch);
        state = { ...state, ...patch, volume: { ...state.volume, ...(patch.volume || {}) } };
        for (const fn of subscribers) fn(state);
        return state;
    },
    // As services/viewState.js: without a square, the whole stack.
    enterVolume(volume) { return this.set({ mode: "volume", volume: volume || { area: null } }); },
    toggleVolume() { return this.set({ mode: state.mode === "volume" ? "plane" : "volume", volume: { area: null } }); },
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
};
let prefs = { screen: 300 };
box.PlexoraViewerPrefs = { area: () => ({ ...prefs }) };

const tool = box.PlexoraAreaTool.attach({ viewState: vs, imageViewer });
check("the tool draws on the image's overlay, follows the pointer and the zoom", overlays.length === 1
      && overlays[0].kind === "shapes" && trackers.length === 1 && (handlers["canvas-click"] || []).length === 1
      && viewportFns.length === 1);
check("300 pixels on screen at half a screen pixel an image pixel is a 600 pixel square", tool.sizePx() === 600);
check("its readout is the tissue it takes: 600 pixels at 0.5 µm is 300 µm", tool.sizeLabel() === "300 µm",
      tool.sizeLabel());
zoom = 0.5;
check("zoomed in four times, the same square on screen takes a sixteenth of the tissue",
      tool.sizePx() === 150 && tool.sizeLabel() === "75.0 µm", `${tool.sizePx()} ${tool.sizeLabel()}`);
zoom = 2;
const unstated = new box.PlexoraAreaTool.AreaTool({ viewState: { ...vs, info: { width: 4000, height: 3000 } },
                                                     prefs: box.PlexoraViewerPrefs, imageViewer });
check("without a stated spacing, the readout is in pixels", unstated.sizePx() === 600
      && unstated.sizeLabel() === "600 px", unstated.sizeLabel());
check("at an edge the square keeps its size, moved inside",
      same(tool.squareAt(3990, 10), { x: 3400, y: 0, width: 600, height: 600 }), JSON.stringify(tool.squareAt(3990, 10)));
const small = new box.PlexoraAreaTool.AreaTool({ viewState: { ...vs, info: { width: 200, height: 100 } },
                                                 prefs: { area: () => ({ screen: 4000 }) }, imageViewer });
check("on an image smaller than the square, the image", same(small.squareAt(50, 50),
                                                              { x: 0, y: 0, width: 200, height: 100 }));

// -- unarmed: the image's own gestures --------------------------------------------
const click = (x, y) => {
    const event = { quick: true, position: { x, y }, preventDefaultAction: false };
    handlers["canvas-click"].forEach((fn) => fn(event));
    return event;
};
const before = sets.length;
const plain = click(100, 100);
check("unarmed, a click is the image's: no change, OSD's zoom left alone", sets.length === before
      && plain.preventDefaultAction === false);

// -- armed -------------------------------------------------------------------------
box.PlexoraAreaTool.toggle();
check("armed: said, and the cursor a crosshair", box.PlexoraAreaTool.armed() && events.includes("plexora:area-tool")
      && viewer.canvas.style.cursor === "crosshair");
trackers[0].options.moveHandler({ position: { x: 1000, y: 700 } });
check("the pointer is followed in image pixels", same(box.PlexoraAreaTool.hover(), { x: 2000, y: 1400 }));
const drawn = [];
const texts = [];
const context2d = {
    save() {}, restore() {}, setLineDash() {}, lineDashOffset: 0, lineWidth: 0, strokeStyle: "",
    strokeRect: (...args) => drawn.push(args),
    strokeText() {}, fillText: (text) => texts.push(text),
};
overlays[0].draw({ context: context2d, px: 2 });
check("the square is drawn dashed where it would go, its readout under it", drawn.length === 2
      && same(drawn[0], [1700, 1100, 600, 600]) && same(texts, ["300 µm"]), JSON.stringify([drawn, texts]));
zoom = 1;
viewportFns[0]();
check("a zoom under a still pointer: the square where the pointer is now, its size for the new zoom",
      same(box.PlexoraAreaTool.hover(), { x: 1000, y: 700 }) && tool.sizePx() === 300,
      JSON.stringify(box.PlexoraAreaTool.hover()));
zoom = 2;
viewportFns[0]();
sets.length = 0;
const placing = click(1000, 700);
check("a click places it: ONE change, 3-D on that square, OSD's zoom held back",
      sets.length === 1 && sets[0].mode === "volume" && same(sets[0].volume.area, { x: 1700, y: 1100, width: 600, height: 600 })
      && placing.preventDefaultAction === true, JSON.stringify(sets));
drawn.length = 0;
overlays[0].draw({ context: context2d, px: 2 });
check("in 3-D nothing is drawn on the plane", drawn.length === 0);
check("in 3-D a click is not the tool's", (() => { const n = sets.length; click(10, 10); return sets.length === n; })());

// -- V ------------------------------------------------------------------------------
const strip = { viewState: vs, volumeSupported: true, stepKey: null };
const Strip = box.PlexoraDepthStrip;
const key = (extra = {}) => {
    const event = { code: "KeyV", key: "v", defaultPrevented: false, prevented: false,
                    preventDefault() { this.prevented = true; }, ...extra };
    Strip.prototype.onKey.call(strip, event);
    return event;
};
strip.toggleVolume = Strip.prototype.toggleVolume;
sets.length = 0;
key();
check("V in 3-D goes back to the plane", state.mode === "plane" && sets.length === 1);
trackers[0].options.moveHandler({ position: { x: 200, y: 200 } });
sets.length = 0;
const placed = key();
check("V armed, the pointer on the image: the square placed there, in 3-D", state.mode === "volume"
      && same(state.volume.area, { x: 100, y: 100, width: 600, height: 600 }) && placed.prevented, JSON.stringify(state.volume));
key();
check("V again: back to the plane, the square let go", state.mode === "plane" && state.volume.area === null);
box.PlexoraAreaTool.disarm();
key();
check("V unarmed: 3-D on the whole stack", state.mode === "volume" && state.volume.area === null && sets.length === 3);
key();
sets.length = 0;
box.PlexoraToolLoader = { activeTool: () => "roi" };
const yielded = key();
check("V is the ROI tool's while it is open", sets.length === 0 && !yielded.prevented);
delete box.PlexoraToolLoader;
typingNow = true;
key();
check("not while typing", sets.length === 0);
typingNow = false;
dialogOpen = true;
key();
check("nor with a dialog open", sets.length === 0);
dialogOpen = false;
key({ shiftKey: true });
key({ altKey: true });
key({ metaKey: true });
check("Shift+V, Alt+V and Cmd+V are not V", sets.length === 0);
const flat = { viewState: { ...vs, depth: 1 }, volumeSupported: true, toggleVolume: Strip.prototype.toggleVolume };
Strip.prototype.onKey.call(flat, { code: "KeyV", preventDefault() {} });
check("nor on a single plane", sets.length === 0);

// -- Esc --------------------------------------------------------------------------------
box.PlexoraAreaTool.arm();
documentListeners.keydown.forEach((fn) => fn({ key: "Escape", preventDefault() {} }));
check("Esc on the plane disarms it", !box.PlexoraAreaTool.armed() && viewer.canvas.style.cursor === "");

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
