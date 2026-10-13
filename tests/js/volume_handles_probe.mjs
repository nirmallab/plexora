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
 * area the 3-D view draws.
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
check("a cutaway over X and Y: one face per cut, across the corner taken out",
      laid.grips.map((g) => g.key).join() === "x:cut,y:cut"
      && JSON.stringify(laid.faces[0].span) === JSON.stringify([["y", [300, 1000]], ["z", [0, 40]]]),
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
