/**
 * The 3-D view keeps its textures (views/volumeRenderer.js).
 *
 * Switching a channel off in 3-D used to delete its 3-D texture, and switching
 * it back on fetched the whole level again and uploaded it again -- a second
 * or more for a stack of any size. Here the renderer runs against a WebGL2
 * context that counts uploads and deletions and a level route that counts
 * requests. Pinned:
 *
 * - a channel switched off and on again is drawn from the texture it had: no
 *   request, no upload;
 * - a texture is kept by what its bytes are -- channel, frame, format, area --
 *   so HD (another format) or another area fetches, and going back does not;
 * - past the GPU budget the least recently drawn go first, never one being
 *   drawn;
 * - a request names the window its bytes are quantized with (`w=`), which is
 *   what lets the browser keep them.
 *
 * Run directly:  node tests/js/volume_renderer_probe.mjs
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

const counts = { texImage3D: 0, deleteTexture: 0, createTexture: 0 };
function fakeGl() {
    return new Proxy({}, {
        get(target, name) {
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            return (...args) => {
                if (name in counts) counts[name] += 1;
                if (name === "createTexture") return { id: counts.createTexture };
                if (name === "isContextLost") return false;
                return undefined;
            };
        },
    });
}

const slots = [
    { index: 0, name: "DNA", enabled: true, range: [10, 200], color: "#ff0000" },
    { index: 1, name: "CD8", enabled: true, range: [0, 255], color: "#00ff00" },
    { index: 2, name: "CD3", enabled: false, range: [0, 255], color: "#0000ff" },
];
let hd = false;
const sidebar = {
    channelSlots: slots,
    dataLayer: { getFullChannelName: (name) => `full:${name}` },
    isHdMode: () => hd,
    quantWindow: () => ({ qmin: 0, qmax: 4095 }),
};
const requests = [];
const box = {
    console, setTimeout, clearTimeout, performance, URLSearchParams,
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: () => true,
    document: { createElement: () => ({ getContext: () => null }), getElementById: () => null },
    __plexora: { viewerSidebar: sidebar },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
runInContext(readFileSync(join(JS, "views/volumeCamera.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/volumeRenderer.js"), "utf8"), context);
const VolumeRenderer = box.PlexoraVolumeRenderer;

const bytesOf = (shape) => shape[0] * shape[1] * shape[2];
function fetchImpl(url) {
    requests.push(url);
    const query = new URLSearchParams(url.split("?")[1]);
    const shape = query.get("box") ? [4, 8, 8] : [4, 16, 16];
    const half = query.get("format") === "f16";
    const buffer = new ArrayBuffer(bytesOf(shape) * (half ? 2 : 1));
    const headers = { "X-Plexora-Shape": shape.join(","), "X-Plexora-Format": half ? "f16" : "u8",
                      "X-Plexora-Level": "1", "X-Plexora-Box": "0,0,32,32" };
    return Promise.resolve({ ok: true, status: 200, arrayBuffer: async () => buffer,
                             headers: { get: (name) => headers[name] ?? null } });
}

const viewState = { depth: 4, info: { width: 32, height: 32 }, get: () => ({ t: 0, slab: { z0: 0, z1: 3 } }) };
const renderer = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl });
renderer.gl = fakeGl();
renderer.maxTexture = 2048;
const settle = async () => { await renderer.refresh(); };

await settle();
check("the visible channels are fetched and uploaded once", requests.length === 2 && counts.texImage3D === 2);
check("each request names the window its bytes are quantized with",
    requests.every((url) => url.includes("w=0%2C4095") || url.includes("w=0,4095")), requests[0]);

slots[1].enabled = false;
await settle();
slots[1].enabled = true;
await settle();
check("a channel switched off and on again is drawn from the texture it had",
    requests.length === 2 && counts.texImage3D === 2 && counts.deleteTexture === 0);

slots[2].enabled = true;
await settle();
check("a channel switched on for the first time is fetched, the others are not",
    requests.length === 3 && requests[2].includes("channel=full%3ACD3"));

hd = true;
await settle();
check("HD is another format: fetched as half floats",
    requests.length === 6 && requests.slice(3).every((url) => url.includes("format=f16")));
hd = false;
await settle();
check("and back to fast tiles is the textures already held", requests.length === 6);

renderer.box = { x: 0, y: 0, width: 16, height: 16 };
await settle();
check("another area is fetched", requests.length === 9 && requests.slice(6).every((url) => url.includes("box=")));
renderer.box = null;
await settle();
check("and the whole plane again is not", requests.length === 9);

// The budget: room for the three whole-plane u8 textures and nothing else.
renderer.gpuBudget = 3 * bytesOf([4, 16, 16]);
renderer.draw = VolumeRenderer.prototype.draw;
renderer.evict(new Set(["full:DNA|0|u8|", "full:CD8|0|u8|", "full:CD3|0|u8|"]));
const keys = [...renderer.textures.keys()];
check("past the GPU budget the textures not being drawn go, the drawn ones stay",
    keys.length === 3 && keys.every((key) => key.endsWith("|0|u8|")) && renderer.textureBytes <= renderer.gpuBudget,
    JSON.stringify(keys));
check("and their GPU memory is given back", counts.deleteTexture === 6);

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
