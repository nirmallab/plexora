/**
 * Back (services/viewHistory.js, views/backButton.js), in node, over the real
 * view state and cut controller.
 *
 * Pinned: an entry is spatial only (mode, plane, range, area, cut, camera or
 * 2-D view), so a channel changed in the child view survives Back; Back puts
 * the mode, the area and the cut back in ONE change and one undo entry; the
 * camera turns back only into 3-D, the 2-D view glides only into 2-D; the
 * trail ends when the view goes back to 2-D any other way; B is Back's only
 * while there is somewhere to go back to, and says so with preventDefault so
 * the dataset chip's B (views/datasetNav.js) stands aside; never with a
 * modifier, while typing or with a dialog up.
 *
 * Run directly:  node tests/js/view_history_probe.mjs
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

const documentListeners = [];
let typingNow = false;
let dialogOpen = false;
const box = {
    console, Math, JSON, Number, String, Array, Object, Boolean, Set, Map, Promise, URLSearchParams,
    setTimeout, clearTimeout,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: () => true,
    addEventListener: () => {},
    removeEventListener: () => {},
    location: { search: "" },
    PlexoraShortcuts: { isTyping: () => typingNow },
    document: {
        addEventListener: (name, fn, capture) => documentListeners.push({ name, fn, capture }),
        removeEventListener: () => {},
        querySelector: (selector) => (selector === "dialog[open]" && dialogOpen ? {} : null),
        getElementById: () => null,
        createElement: (tag) => {
            const node = { tag, children: [], attrs: {}, hidden: false, listeners: {},
                           setAttribute(k, v) { this.attrs[k] = String(v); },
                           append(...kids) { this.children.push(...kids); },
                           appendChild(kid) { this.children.push(kid); },
                           addEventListener(name, fn) { this.listeners[name] = fn; },
                           remove() {} };
            return node;
        },
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
for (const file of ["services/inspectGeometry.js", "services/viewState.js", "services/inspectState.js",
                    "services/viewHistory.js", "views/backButton.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}

const VOLUME = { volumetric: true, depth: 40, frames: 1, width: 2000, height: 1500, default_view: "z20" };
const fetchImpl = async (url, options) => {
    if (options?.method === "PUT") return { ok: true, json: async () => ({}) };
    if (url.includes("volume/vol/info")) return { ok: true, json: async () => VOLUME };
    return { ok: true, json: async () => ({}) };
};
const vs = new box.PlexoraViewState("vol", { fetchImpl });
await vs.load({ search: "" });
const cutter = new box.PlexoraInspectState(vs);
box.PlexoraInspect = cutter;

// The renderer and the image viewer, as far as Back uses them.
let camera = { q: [0, 0, 0, 1], dist: 2, pan: [0, 0] };
const turns = [];
const volume = { renderer: { cameraState: () => JSON.parse(JSON.stringify(camera)),
                             turnTo: (target) => turns.push(target) } };
let osdView = { center: { x: 0.5, y: 0.4 }, zoom: 1 };
const glides = [];
box.PlexoraViewerScene = { viewOf: () => JSON.parse(JSON.stringify(osdView)),
                           glideTo: (iv, target, options) => { glides.push({ target, options }); return Promise.resolve(true); } };
box.PlexoraViewerPrefs = { smoothMotion: () => true };
const history = box.PlexoraViewHistory.attach({ viewState: vs, cutter, volume, imageViewer: {}, config: {} });
const pill = box.PlexoraBackButton.attach({ history });

check("nothing to go back to at first: the pill hidden", !history.canGoBack() && pill.root.hidden === true);

// -- a square from a plane, then Back --------------------------------------------------
vs.set({ z: 12 });
osdView = { center: { x: 0.25, y: 0.3 }, zoom: 4 };
box.PlexoraViewHistory.push();
vs.enterVolume({ area: { x: 100, y: 100, width: 400, height: 400 } });
check("pushed before the square opened: one view back, the pill shown",
      history.depth === 1 && pill.root.hidden === false);
const entry = history.stack[0];
check("the entry is where the view was: the plane, its 2-D view, no camera",
      entry.mode === "plane" && entry.z === 12 && entry.area === null && entry.camera === null
      && same(entry.view, { center: { x: 0.25, y: 0.3 }, zoom: 4 }));
check("and nothing of the rendering", !("channels" in entry) && !("composite" in entry) && !("show" in entry));
vs.set({ volume: { composite: "additive" } });
const undoBefore = cutter.past.length;
check("Back returns to the plane, the square gone", history.back() && vs.get().mode === "plane"
      && vs.get().volume.area === null && vs.get().z === 12, JSON.stringify(vs.get().volume));
check("what was changed in the child view stays", vs.get().volume.composite === "additive");
check("one undo entry of the cut", cutter.past.length === undoBefore + 1);
check("the 2-D view glides back, the camera left alone",
      glides.length === 1 && same(glides[0].target, entry.view) && glides[0].options.ms > 0 && turns.length === 0);
check("nothing left: the pill hidden again", !history.canGoBack() && pill.root.hidden === true);

// -- two levels: whole stack in 3-D -> a crop -> Crop to area -> Back -> Back -----------------
vs.toggleVolume();
cutter.set({ inspect: { mode: "crop", axes: { x: true, y: true, z: false }, crop: { x: [200, 600], y: [100, 500] } } });
camera = { q: [0.1, 0.2, 0.3, 0.9], dist: 1.5, pan: [0.1, -0.1] };
cutter.cropToArea();
check("Crop to area pushes the 3-D view it came from, its camera with it",
      history.depth === 1 && history.stack[0].mode === "volume" && history.stack[0].area === null
      && same(history.stack[0].camera, camera) && history.stack[0].view === null);
check("...and draws that area", same(vs.get().volume.area, { x: 200, y: 100, width: 400, height: 400 }));
camera = { q: [0, 0, 0, 1], dist: 1, pan: [0, 0] };
glides.length = 0;
history.back();
const back3d = vs.get();
check("Back: the whole stack again, with the crop it had", back3d.mode === "volume" && back3d.volume.area === null
      && back3d.inspect.mode === "crop" && same(back3d.inspect.crop.x, [200, 600]), JSON.stringify(back3d.inspect));
check("the camera turns back (a tween, not a jump), no 2-D glide",
      turns.length === 1 && same(turns[0], { q: [0.1, 0.2, 0.3, 0.9], dist: 1.5, panX: 0.1, panY: -0.1 })
      && glides.length === 0, JSON.stringify(turns));

// -- the trail ends when 2-D comes back another way ---------------------------------------
cutter.cropToArea();
check("a view to go back to", history.canGoBack());
vs.set({ mode: "plane", volume: { area: null } });
check("the mode buttons back to 2-D end the trail", !history.canGoBack() && pill.root.hidden === true);

// -- B ------------------------------------------------------------------------------------
const keydown = documentListeners.find((l) => l.name === "keydown");
check("B listens in the capture phase, before the dataset chip", keydown && keydown.capture === true);
const press = (extra = {}) => {
    const event = { key: "b", repeat: false, defaultPrevented: false, prevented: false,
                    preventDefault() { this.prevented = true; this.defaultPrevented = true; }, ...extra };
    keydown.fn(event);
    return event;
};
check("with no history B is left to the dataset chip", press().prevented === false);
vs.set({ mode: "plane" });
box.PlexoraViewHistory.push();
vs.enterVolume({ area: { x: 0, y: 0, width: 300, height: 300 } });
for (const [label, extra] of [["Shift+B", { shiftKey: true }], ["Ctrl+B", { ctrlKey: true }],
                              ["Cmd+B", { metaKey: true }], ["Alt+B", { altKey: true }],
                              ["a held B", { repeat: true }]]) {
    check(`${label} is not Back`, press(extra).prevented === false && history.canGoBack());
}
typingNow = true;
check("not while typing", press().prevented === false && history.canGoBack());
typingNow = false;
dialogOpen = true;
check("nor with a dialog up", press().prevented === false && history.canGoBack());
dialogOpen = false;
const taken = press({ key: "B" });
check("B goes back, and says so (the chip sees defaultPrevented)",
      taken.prevented && !history.canGoBack() && vs.get().mode === "plane");

// -- the dataset chip stands aside --------------------------------------------------------
const nav = readFileSync(join(JS, "views/datasetNav.js"), "utf8");
check("the dataset chip's B stands aside for a key already taken",
      /function onKeyDown\(event\) \{[\s\S]{0,200}if \(event\.defaultPrevented\) return;/.test(nav));

// -- reduced motion -----------------------------------------------------------------------
box.PlexoraViewerPrefs = { smoothMotion: () => false };
box.PlexoraViewHistory.push();
vs.enterVolume({ area: { x: 0, y: 0, width: 300, height: 300 } });
glides.length = 0;
history.back();
check("under reduced motion the 2-D view jumps", glides.length === 1 && glides[0].options.ms === 0);

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
