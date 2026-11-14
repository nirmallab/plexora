/**
 * The 2-D tile renderer comes back after its WebGL context is lost.
 *
 * Every channel tile is coloured on one offscreen WebGL2 context (GLRenderer,
 * built by glInit.js's createGLRenderer). A GPU reset, a driver update or a
 * GPU-process crash loses that context. The browser gives it back only to a
 * page that called preventDefault() on `webglcontextlost`; the page must then
 * rebuild its program, its textures and its buffers on `webglcontextrestored`
 * and redraw. Without that every channel tile is drawn from a dead context --
 * a blank canvas -- until the page is reloaded.
 *
 * Here the real GLRenderer (the ES module the vendor bundle ships) and the real
 * glInit.js run against a fake WebGL2 context whose canvas is an event target,
 * the same way gl_init_probe.mjs drives them. The probe brings GL up through
 * the `open` handler, loses the context, restores it, and checks what the
 * renderer did.
 *
 * Run directly:  node tests/js/gl_tile_context_restore_probe.mjs
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

// -- a WebGL2 context that counts, on a canvas that dispatches events ----------------

const counts = { linkProgram: 0, createTexture: 0, createBuffer: 0 };
let lost = false;
let generation = 0;       // bumped on a loss: objects of an older generation are dead
function fakeGl() {
    const listeners = {};
    const canvas = {
        width: 0,
        height: 0,
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        removeEventListener(type, fn) {
            listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
        },
        dispatch(type) {
            let prevented = false;
            const event = { type, preventDefault() { prevented = true; } };
            for (const fn of [...(listeners[type] || [])]) fn(event);
            return { prevented, heard: (listeners[type] || []).length };
        },
    };
    const gl = new Proxy({ canvas }, {
        get(target, name) {
            if (name in target) return target[name];
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;          // constants
            return (...args) => {
                if (name === "isContextLost") return lost;
                if (lost) return name.startsWith("create") ? null : undefined;
                if (name in counts) counts[name] += 1;
                if (name === "getShaderParameter" || name === "getProgramParameter") return true;
                if (name.startsWith("create")) return { kind: name, generation };
                if (name === "getUniformLocation") return { uniform: args[1], generation };
                if (name === "getAttribLocation") return 0;
                return undefined;
            };
        },
    });
    canvas.getContext = () => gl;
    return gl;
}

globalThis.document = { createElement: () => ({ getContext: () => fakeGl() }) };
const moduleSource = readFileSync(join(JS, "services/glRenderer.js"), "utf8");
const { GLRenderer } = await import(
    `data:text/javascript;base64,${Buffer.from(moduleSource).toString("base64")}`);

const box = { console, setTimeout, GLRenderer, plexoraUrl: (p) => `/${p}` };
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(JS, "views/glInit.js"), "utf8"), createContext(box));

const renderer = box.PlexoraGL.createGLRenderer({
    indexOfTexture: () => 0, selectTexture: () => {}, resolveGLReady: () => {},
});
renderer.getter = (where) => Promise.resolve(`// ${where}`);

let updates = 0;
const items = [{}, {}];
const viewer = {
    addHandler: () => {},
    world: {
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        update: () => { updates += 1; },
    },
};
const open = box.PlexoraGL.createGLInit({
    viewer, renderer, config: { tileWidth: 256, tileHeight: 256 },
    handleTileLoaded: () => {}, tileDrawingCustom: () => {}, tileDrawingDefault: () => {},
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
open();
await settle();

// Two channel tiles go up to the GPU, as tileColorize's loadArray would put them.
renderer._tileTextureCache.acquire("tile-a|u8", 1024);
renderer._tileTextureCache.acquire("tile-b|u8", 1024);
check("before the loss: the program is built once and two tiles are resident",
    counts.linkProgram === 1 && renderer._tileTextureCache.entries.size === 2,
    `${counts.linkProgram} links, ${renderer._tileTextureCache.entries.size} tiles`);

// -- the GPU resets ---------------------------------------------------------------------

lost = true;
generation += 1;
const onLoss = renderer.gl.canvas.dispatch("webglcontextlost");
check("the loss is claimed (preventDefault) so the browser will give the context back",
    onLoss.prevented, `${onLoss.heard} webglcontextlost listener(s), none called preventDefault`);

// -- the browser gives it back ----------------------------------------------------------

lost = false;
const updatesBefore = updates;
const onRestore = renderer.gl.canvas.dispatch("webglcontextrestored");
await settle();

check("something listens for webglcontextrestored on the tile renderer's canvas",
    onRestore.heard > 0, "no listener");
check("the tile program is compiled and linked again on the restored context",
    counts.linkProgram === 2 && renderer.program?.generation === generation,
    `${counts.linkProgram} links; the kept program is from generation ${renderer.program?.generation}, the context is on ${generation}`);
check("no tile texture from the lost context is left in the cache to be bound",
    [...renderer._tileTextureCache.entries.values()].every((e) => e.texture?.generation === generation),
    `${renderer._tileTextureCache.entries.size} cached texture(s) still from the dead context`);
check("the renderer's own texture and quad buffer are made again",
    renderer.texture?.generation === generation && renderer.buffer?.generation === generation,
    `texture generation ${renderer.texture?.generation}, buffer generation ${renderer.buffer?.generation}`);
check("the viewer is asked to redraw after the restore",
    updates > updatesBefore, `${updates - updatesBefore} world.update() calls`);

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
