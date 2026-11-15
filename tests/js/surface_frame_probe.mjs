/**
 * A surface is of one frame: services/surfaceLayers.js on a time series.
 *
 * What is pinned: a surface built at frame t (its spec's `surface.t`) is drawn
 * in 3-D, and its cuts painted on the plane and the section panes, at frame t
 * only; moving to another frame hides it, coming back shows it again. An
 * imported mesh (no `t`) is of no frame and is drawn on every one.
 *
 * Run directly:  node tests/js/surface_frame_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/surfaceLayers.js");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "ok" : "FAIL"} - ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let frame = 0;
const listeners = new Map();
const fetched = [];
const styles = new Map();
const layers = [
    { id: "iso-t2", kind: "surface", visible: true, opacity: 1,
      spec: { status: "ready", surface: { type: "isosurface", t: 2 } } },
    { id: "imported", kind: "surface", visible: true, opacity: 1,
      spec: { status: "ready", surface: { type: "imported" } } },
];
const renderer = {
    meshes: new Map(layers.map((layer) => [layer.id, {}])),
    has: (id) => renderer.meshes.has(id),
    setStyle: (id, style) => styles.set(id, style),
    load() { return {}; },
    unload() {},
};
const win = {
    console, setTimeout, clearTimeout, URLSearchParams, JSON, Map, Set, Promise, Number, String,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    addEventListener(type, fn) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatchEvent(event) { [...(listeners.get(event.type) || [])].forEach((fn) => fn(event)); return true; },
    fetch: async (url) => {
        fetched.push(String(url));
        return { ok: true, json: async () => ({ xy: { z: 5, objects: {} } }) };
    },
    plexoraUrl: (path) => `/${path}`,
    flaskVariables: { datasource: "series" },
    __plexora: {
        viewState: { enabled: () => true, get: () => ({ t: frame, z: 5, mode: "plane" }) },
        volumeView: { surfaces: renderer },
    },
};
win.window = win;
const context = createContext(win);
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "surfaceLayers.js" });
const Surfaces = win.PlexoraSurfaceLayers;
const stack = { layers: () => layers, get: (id) => layers.find((l) => l.id === id) || null,
                subscribe: () => () => {} };
Surfaces.init({ datasource: "series", layerStack: stack });

const cutsFor = (id) => fetched.filter((u) => u.includes(`/surfaces/${id}/sections`)).length;
const goTo = async (t) => {
    frame = t;
    win.dispatchEvent(new win.CustomEvent("plexora:view-changed", { detail: { t } }));
    await sleep(200);
};

await sleep(200);
check("at frame 0 a surface built at frame 2 is not drawn in 3-D",
      styles.get("iso-t2")?.visible === false, JSON.stringify(styles.get("iso-t2")));
check("...and its cut on the plane is not read", cutsFor("iso-t2") === 0, JSON.stringify(fetched));
check("an imported mesh (no frame) is drawn on every frame",
      styles.get("imported")?.visible === true && cutsFor("imported") === 1);
await goTo(2);
check("at frame 2 the surface is drawn", styles.get("iso-t2")?.visible === true);
check("...and its cut read", cutsFor("iso-t2") === 1, JSON.stringify(fetched));
await goTo(3);
check("moving on to frame 3 hides it again", styles.get("iso-t2")?.visible === false);

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
