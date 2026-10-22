/**
 * The 3-D view's cut handles (views/volumeHandles.js), against the real
 * renderer's camera.
 *
 * Pinned: which faces and grips each mode draws; that a face's grip follows
 * the hand along its axis as the screen shows it -- perspective included, so
 * the same pointer travel moves a near face less far than a far one; that a
 * crop's body moves along two axes at once; that a press on a grip is taken
 * before the canvas (it can never turn the stack); that a drag previews and
 * commits once, Esc puts it back, and arrows nudge a focused grip in steps
 * that make one undo entry; and that a face cannot be dragged outside the
 * area the 3-D view draws. An area (a square opened in 3-D) has its own
 * grips, shown with no cut: its body slides it the way Shift+drag does (the
 * tissue follows the hand, one undo entry), a side resizes it without moving
 * the cut, Esc puts it back, and they give way to a crop's on the same spot.
 *
 * Run directly:  node tests/js/volume_handles_probe.mjs
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
const near = (a, b, eps) => Math.abs(a - b) <= eps;

function fakeElement(tag) {
    const attributes = {};
    const classes = new Set();
    const listeners = {};
    const node = {
        tagName: String(tag).toUpperCase(), children: [], parentElement: null,
        classList: {
            add: (...n) => n.forEach((c) => classes.add(c)), remove: (...n) => n.forEach((c) => classes.delete(c)),
            contains: (c) => classes.has(c),
            toggle: (c, on) => { if (on ?? !classes.has(c)) classes.add(c); else classes.delete(c); },
        },
        setAttribute(name, value) { attributes[name] = String(value); },
        removeAttribute(name) { delete attributes[name]; },
        getAttribute(name) { return attributes[name] ?? null; },
        appendChild(child) { child.parentElement = node; node.children.push(child); return child; },
        append(...nodes) { nodes.forEach((n) => node.appendChild(n)); },
        remove() {},
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
        removeEventListener(name, fn) { listeners[name] = (listeners[name] || []).filter((f) => f !== fn); },
        fire(name, event) { for (const fn of [...(listeners[name] || [])]) fn(event); },
        setPointerCapture() { node.captured = true; },
        releasePointerCapture() { node.captured = false; },
    };
    return node;
}

const windowListeners = {};
const documentListeners = {};
const wrapper = fakeElement("div");
const box = {
    console, Math, JSON, Number, Object, Array, Set, Map, Promise, Float32Array,
    performance: { now: () => Date.now() },
    setTimeout, clearTimeout,
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { for (const fn of [...(windowListeners[event.type] || [])]) fn(event); return true; },
    addEventListener: (name, fn) => { (windowListeners[name] ||= []).push(fn); },
    removeEventListener: () => {},
    document: {
        createElement: (tag) => fakeElement(tag),
        createElementNS: (ns, tag) => fakeElement(tag),
        getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
        addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
        removeEventListener: () => {},
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
for (const file of ["services/inspectGeometry.js", "services/inspectState.js", "views/volumeCamera.js",
                    "views/volumeRenderer.js", "views/volumeHandles.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}
const G = box.PlexoraInspectGeometry;
const INFO = { width: 2000, height: 1000, spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
                                                     z: { value: 2, unit: "µm", source: "metadata" } } };
const DIMS = G.dims(INFO, 40);

const asked = [];
let state = { mode: "volume", z: 12, t: 0, slab: { z0: 0, z1: 39 }, volume: {},
              inspect: G.clean({ mode: "crop", axes: { x: true, z: true },
                                 crop: { x: [400, 1600], z: [5, 30] } }, DIMS) };
const subscribers = new Set();
const vs = {
    depth: 40, frames: 1, info: INFO, enabled: () => true, isVolume: () => true,
    get: () => JSON.parse(JSON.stringify(state)),
    set(patch) {
        asked.push(patch);
        state = { ...state, ...patch,
                  inspect: G.clean(G.merge(state.inspect, patch.inspect || {}), DIMS) };
        for (const fn of subscribers) fn(state);
        return state;
    },
    subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn); },
};
const renderer = new box.PlexoraVolumeRenderer({ viewState: vs, datasource: "vol", fetchImpl: () => Promise.reject() });
renderer.canvas = { width: 1200, height: 800, clientWidth: 1200, clientHeight: 800, hidden: false };
renderer.covered = [0, 0, 2000, 1000];
renderer.shape = [40, 500, 1000];
renderer.camera = { q: box.PlexoraVolumeCamera.VIEWS.iso.slice(), dist: 2.2, panX: 0, panY: 0 };
const cutter = new box.PlexoraInspectState(vs);
box.PlexoraInspect = cutter;
const canvasPresses = [];
const handles = new box.PlexoraVolumeHandles.VolumeHandles({ viewState: vs, volume: { renderer }, cutter });
check("the handles mount over the canvas", handles.mount() && wrapper.children.includes(handles.root));

// -- what each mode draws -------------------------------------------------------
let laid = handles.layout(vs.get());
check("a crop along X and Z: both faces of each, and its body",
      laid.faces.length === 4 && laid.grips.map((g) => g.key).join() === "x:lo,x:hi,z:lo,z:hi,body",
      laid.grips.map((g) => g.key).join());
check("a crop's Z faces sit at its first plane and after its last", laid.grips[2].at === 5 && laid.grips[3].at === 31);
state.inspect = G.clean({ mode: "cutaway", axes: { x: true, y: true, z: false },
                          cutaway: { x: { at: 800, keep: "hi" }, y: { at: 300, keep: "lo" } } }, DIMS);
laid = handles.layout(vs.get());
check("a cutaway saved over X and Y: one face, along X, across the whole box",
      laid.grips.map((g) => g.key).join() === "x:cut" && laid.faces.length === 1
      && JSON.stringify(laid.faces[0].span) === JSON.stringify([["y", [0, 1000]], ["z", [0, 40]]]),
      JSON.stringify(laid.faces[0]));
state.inspect = G.clean({ mode: "crop", axes: { x: true, y: true, z: true } }, DIMS);
laid = handles.layout(vs.get());
check("a crop over X, Y and Z (the Crop's default): two faces an axis and the body",
      laid.faces.length === 6 && laid.grips.map((g) => g.key).join() === "x:lo,x:hi,y:lo,y:hi,z:lo,z:hi,body",
      laid.grips.map((g) => g.key).join());
check("no grip moves a plane any more", laid.grips.every((g) => g.kind !== "plane"));

// -- following the hand ---------------------------------------------------------
const camera = renderer.cameraMatrices();
const centre = handles.project([1000, 500, 20], camera, renderer.extent(), renderer.canvas);
check("the stack's middle is the screen's middle, unpanned",
      near(centre.x, 600, 0.5) && near(centre.y, 400, 0.5), JSON.stringify(centre));
const step = handles.screenStep("x", [1000, 500, 20]);
const moved = handles.travel([{ axis: "x", step }], step.x * 37, step.y * 37);
check("a pointer travel along X's screen direction is that many pixels of X", near(moved.x, 37, 1e-6), moved.x);
const across = handles.travel([{ axis: "x", step }], -step.y * 50, step.x * 50);
check("travel across the axis moves it not at all", near(across.x, 0, 1e-6), across.x);
const nearStep = handles.screenStep("x", [1000, 500, 39]);
const farStep = handles.screenStep("x", [1000, 500, 0]);
check("perspective: one pixel of X is longer on screen nearer the camera",
      Math.abs(Math.hypot(nearStep.x, nearStep.y) - Math.hypot(farStep.x, farStep.y)) > 1e-3,
      JSON.stringify([nearStep, farStep]));
const sx = handles.screenStep("x", [1000, 500, 20]);
const sz = handles.screenStep("z", [1000, 500, 20]);
const both = handles.travel([{ axis: "x", step: sx }, { axis: "z", step: sz }],
                            sx.x * 30 + sz.x * 4, sx.y * 30 + sz.y * 4);
check("a crop's body follows the hand along two axes at once", near(both.x, 30, 1e-6) && near(both.z, 4, 1e-6),
      JSON.stringify(both));

// -- dragging -------------------------------------------------------------------------
state.inspect = G.clean({ mode: "crop", axes: { x: true, z: true }, crop: { x: [400, 1600], z: [5, 30] } }, DIMS);
handles.paint();
const grip = handles.grips.get("x:hi");
check("each grip is a focusable slider", grip?.group.getAttribute("role") === "slider"
      && grip.group.getAttribute("tabindex") === "0");
let stopped = 0;
let prevented = 0;
const event = (x, y, extra = {}) => ({ button: 0, clientX: x, clientY: y, pointerId: 7,
                                       stopPropagation() { stopped += 1; }, preventDefault() { prevented += 1; }, ...extra });
asked.length = 0;
const cam = JSON.stringify(renderer.camera);
grip.group.fire("pointerdown", event(100, 100));
check("a press on a grip is taken before the canvas: stopped, captured",
      stopped === 1 && prevented === 1 && grip.group.captured && renderer.interacting === true);
const hiStep = handles.screenStep("x", handles.centre(grip.grip));
grip.group.fire("pointermove", event(100 + hiStep.x * 200, 100 + hiStep.y * 200));
check("dragging previews the face's move, nothing saved",
      asked.length === 0 && cutter.previewing && cutter.effective().inspect.crop.x[1] === 1800,
      JSON.stringify(cutter.effective().inspect.crop.x));
grip.group.fire("pointermove", event(100 + hiStep.x * 5000, 100 + hiStep.y * 5000));
check("never past the area the 3-D view draws", cutter.effective().inspect.crop.x[1] === 2000);
grip.group.fire("pointerup", event(0, 0));
check("let go: one change, the camera untouched",
      asked.length === 1 && state.inspect.crop.x[1] === 2000 && JSON.stringify(renderer.camera) === cam
      && renderer.interacting === false);
const lo = handles.grips.get("x:lo");
lo.group.fire("pointerdown", event(10, 10));
lo.group.fire("pointermove", event(10 + hiStep.x * 100, 10 + hiStep.y * 100));
documentListeners.keydown.forEach((fn) => fn({ key: "Escape", preventDefault() {}, stopPropagation() {} }));
check("Esc puts a drag back", !cutter.previewing && state.inspect.crop.x[0] === 400 && asked.length === 1);
const before = cutter.past.length;
const zhi = handles.grips.get("z:hi");
zhi.group.fire("keydown", { key: "ArrowUp", shiftKey: false, preventDefault() {}, stopPropagation() {} });
zhi.group.fire("keydown", { key: "ArrowUp", shiftKey: true, preventDefault() {}, stopPropagation() {} });
check("arrows nudge a focused grip a plane (Shift ten), as one undo entry",
      state.inspect.crop.z[1] === 39 && cutter.past.length === before + 1, JSON.stringify(state.inspect.crop.z));
const body = handles.grips.get("body");
body.group.fire("pointerdown", event(0, 0));
const ax = handles.drag.axes.map((a) => a.axis).sort().join();
body.group.fire("pointerup", event(0, 0));
check("the body moves along the two axes the screen shows best", ax === "x,z", ax);

// -- the area's grips -----------------------------------------------------------------
const AREA = { x: 500, y: 250, width: 1000, height: 500 };
state = { ...state, volume: { area: { ...AREA } }, inspect: G.clean({ mode: "full" }, DIMS) };
renderer.setArea(state.volume.area, { refresh: false });
renderer.onSlide = (area, options) => cutter.slideArea(area, options);
laid = handles.layout(vs.get());
check("an area drawn: its four sides and a body, nothing of the cut",
      laid.grips.map((g) => g.key).join() === "area:x:lo,area:x:hi,area:y:lo,area:y:hi,area-body"
      && laid.faces.length === 4 && laid.faces.every((f) => f.area), laid.grips.map((g) => g.key).join());
check("its sides are the area's", laid.grips.slice(0, 4).map((g) => g.at).join() === "500,1500,250,750",
      laid.grips.map((g) => g.at).join());
handles.paint();
check("shown with no cut: there is an area to move", handles.root.getAttribute("hidden") === null);
const areaBody = handles.grips.get("area-body");
check("the area's grips are its own look, named for what they do",
      areaBody.group.getAttribute("class").includes("is-area")
      && areaBody.group.getAttribute("aria-label") === "Slide the area across the tissue"
      && handles.faces.get("area:x:lo").getAttribute("class") === "handle-face is-area");
asked.length = 0;
stopped = 0;
let past = cutter.past.length;
const camBefore = JSON.stringify(renderer.camera);
areaBody.group.fire("pointerdown", event(300, 300));
check("a press on the area's body is taken before the canvas",
      stopped === 1 && renderer.interacting === true && Boolean(handles.drag?.session));
const slideX = renderer.slideSteps().find((s) => s.axis === "x").step;
areaBody.group.fire("pointermove", event(300 + slideX.x * 100, 300 + slideX.y * 100));
areaBody.group.fire("pointerup", event(0, 0));
check("the body slides the area as Shift+drag does: the tissue follows the hand",
      near(state.volume.area.x, 400, 1) && near(state.volume.area.y, 250, 1)
      && state.volume.area.width === 1000, JSON.stringify(state.volume.area));
check("one undo entry, the camera untouched, the hand let go",
      cutter.past.length === past + 1 && JSON.stringify(renderer.camera) === camBefore
      && renderer.interacting === false && handles.drag === null);
renderer.setArea(state.volume.area, { refresh: false });
const areaWas = JSON.stringify(state.volume.area);
areaBody.group.fire("pointerdown", event(300, 300));
areaBody.group.fire("pointermove", event(300 + slideX.x * 150, 300 + slideX.y * 150));
documentListeners.keydown.forEach((fn) => fn({ key: "Escape", preventDefault() {}, stopPropagation() {} }));
check("Esc puts a slide back", JSON.stringify(state.volume.area) === areaWas && handles.drag === null,
      JSON.stringify(state.volume.area));
state.inspect = G.clean({ mode: "crop", axes: { x: true, y: false, z: false }, crop: { x: [600, 900] } }, DIMS);
renderer.setArea(state.volume.area, { refresh: false });
handles.paint();
const sideHi = handles.grips.get("area:x:hi");
past = cutter.past.length;
sideHi.group.fire("pointerdown", event(50, 50));
const sideStep = handles.screenStep("x", handles.centre(sideHi.grip));
sideHi.group.fire("pointermove", event(50 + sideStep.x * 200, 50 + sideStep.y * 200));
sideHi.group.fire("pointerup", event(0, 0));
check("a side's grip resizes the area along its axis",
      near(state.volume.area.x, 400, 1) && near(state.volume.area.width, 1200, 1), JSON.stringify(state.volume.area));
check("...the cut left where it was in the image, one undo entry",
      JSON.stringify(state.inspect.crop.x) === "[600,900]" && cutter.past.length === past + 1,
      JSON.stringify(state.inspect.crop.x));
check("a side never makes the area narrower than a few pixels",
      handles.resized(sideHi.grip, { x: 400, y: 0, width: 100, height: 100 }, { x: -5000 }).width === 16);
renderer.setArea(state.volume.area, { refresh: false });
const was = state.volume.area.x;
areaBody.group.fire("keydown", { key: "ArrowRight", shiftKey: false, preventDefault() {}, stopPropagation() {} });
check("an arrow slides a focused area body", state.volume.area.x === was + 10, JSON.stringify(state.volume.area));
const a = state.volume.area;
state.inspect = G.clean({ mode: "crop", axes: { x: true, y: true, z: false },
                          crop: { x: [a.x, a.x + a.width], y: [a.y, a.y + a.height] } }, DIMS);
renderer.setArea(a, { refresh: false });
handles.paint();
const hiddenOf = (key) => handles.grips.get(key)?.group.getAttribute("display") === "none";
check("a crop on the area's sides: the area's grips give way to the crop's",
      hiddenOf("area:x:hi") && hiddenOf("area-body") && !hiddenOf("x:hi") && !hiddenOf("body"));
state = { ...state, volume: { area: null }, inspect: G.clean({ mode: "full" }, DIMS) };
renderer.setArea(null, { refresh: false });

// -- visibility ---------------------------------------------------------------------
box.PlexoraViewerPrefs = { handles: () => ({ visible: false, opacity: 0.55 }) };
handles.paint();
check("hidden when the prefs say so", handles.root.getAttribute("hidden") !== null);
box.PlexoraViewerPrefs = { handles: () => ({ visible: true, opacity: 0.4 }) };
state.inspect = G.clean({ mode: "full" }, DIMS);
handles.paint();
check("nothing to drag over the whole stack", handles.root.getAttribute("hidden") !== null);
state.inspect = G.clean({ mode: "cutaway", axes: { x: false, z: true } }, DIMS);
handles.paint();
check("shown for a cut, outlines at the prefs' strength",
      handles.root.getAttribute("hidden") === null && handles.faceLayer.getAttribute("opacity") === "0.4");

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
