/**
 * An edit to a surface while its mesh is still downloading:
 * services/surfaceLayers.js reload().
 *
 * A surface's mesh is fetched once, and a fetch already out is not started
 * again. An edit (refine: smooth, undo, ...) makes a new version on the server
 * and asks for the mesh again. If the first download was still in flight it
 * was asked for before the edit, so what it brings is the PRE-edit mesh: it
 * must not be drawn, and the new version must be fetched after it.
 *
 * Run directly:  node tests/js/surface_reload_probe.mjs
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

const listeners = new Map();
//: Every mesh download, in order: {version, release()}.
const downloads = [];
let version = 1;
const layers = [
    { id: "iso", kind: "surface", visible: true, opacity: 1,
      spec: { status: "ready", surface: { type: "isosurface" } } },
];
//: What the renderer was handed, in order: the version of each mesh loaded.
const drawn = [];
const renderer = {
    meshes: new Map(),
    has: (id) => renderer.meshes.has(id),
    setStyle() {},
    load(id, buffer) { renderer.meshes.set(id, buffer); drawn.push(buffer.version); return {}; },
    unload(id) { renderer.meshes.delete(id); },
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
        url = String(url);
        if (url.endsWith("/mesh")) {
            // The server answers with the version current when it was ASKED.
            const asked = version;
            return new Promise((resolve) => {
                downloads.push({ version: asked, release: () => resolve({
                    ok: true, status: 200, headers: { get: () => `"v${asked}"` },
                    arrayBuffer: async () => ({ version: asked }),
                }) });
            });
        }
        if (url.includes("/refine")) {
            version += 1;
            return { ok: true, status: 200, clone() { return this; }, json: async () => ({ ok: true }) };
        }
        return { ok: true, status: 200, json: async () => ({ xy: { z: 0, objects: {} } }) };
    },
    plexoraUrl: (path) => `/${path}`,
    flaskVariables: { datasource: "vol" },
    __plexora: {
        viewState: { enabled: () => false, get: () => ({ t: 0, z: 0, mode: "volume" }) },
        volumeView: { surfaces: renderer },
    },
};
win.window = win;
const context = createContext(win);
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "surfaceLayers.js" });
const Surfaces = win.PlexoraSurfaceLayers;
const stack = { layers: () => layers, get: (id) => layers.find((l) => l.id === id) || null,
                subscribe: () => () => {} };
Surfaces.init({ datasource: "vol", layerStack: stack });
await sleep(20);

check("opening the surface starts one mesh download", downloads.length === 1 && downloads[0].version === 1,
      JSON.stringify(downloads.map((d) => d.version)));
// The user edits it while that download is still out.
await Surfaces.refine("iso", { op: "smooth" });
await sleep(20);
downloads[0].release();
await sleep(20);
check("the pre-edit mesh, landing after the edit, is not drawn", !drawn.includes(1), JSON.stringify(drawn));
check("...and the edited version is fetched after it",
      downloads.length === 2 && downloads[1].version === 2, JSON.stringify(downloads.map((d) => d.version)));
downloads[1]?.release();
await sleep(20);
check("...and drawn", drawn.at(-1) === 2 && Surfaces.loading() === false, JSON.stringify(drawn));

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
