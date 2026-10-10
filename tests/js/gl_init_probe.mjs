/**
 * The tile shaders are fetched and compiled once per page, however often the
 * viewer re-raises `open`.
 *
 * viewerManager raises `open` for every item it adds -- the cover and the
 * paint of every channel, on every plane step -- and each one used to run
 * GLRenderer.init(): two XHRs for the shader files sent with no-cache headers,
 * a compile and a link, every time. Here the real GLRenderer (the ES module the
 * vendor bundle ships) runs against a WebGL context that counts what it is
 * asked to do, behind glInit.js's real `open` handler.
 *
 * Run directly:  node tests/js/gl_init_probe.mjs
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

// -- a WebGL2 context that counts ---------------------------------------------------

const counts = { compileShader: 0, linkProgram: 0, createProgram: 0, canvasResizes: 0 };
function fakeGl() {
    const canvas = {};
    let width = 0;
    let height = 0;
    Object.defineProperty(canvas, "width", { get: () => width, set: (v) => { width = v; counts.canvasResizes += 1; } });
    Object.defineProperty(canvas, "height", { get: () => height, set: (v) => { height = v; counts.canvasResizes += 1; } });
    const gl = new Proxy({ canvas }, {
        get(target, name) {
            if (name in target) return target[name];
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;          // constants
            return (...args) => {
                if (name in counts) counts[name] += 1;
                if (name === "getShaderParameter" || name === "getProgramParameter") return true;
                if (name.startsWith("create")) return { kind: name };
                if (name === "getUniformLocation") return { uniform: args[1] };
                if (name === "getAttribLocation") return 0;
                return undefined;
            };
        },
    });
    canvas.getContext = () => gl;
    return gl;
}

globalThis.document = { createElement: () => ({ getContext: () => fakeGl() }) };
// The file is an ES module the bundler imports; loaded as one here too.
const moduleSource = readFileSync(join(JS, "services/glRenderer.js"), "utf8");
const { GLRenderer } = await import(
    `data:text/javascript;base64,${Buffer.from(moduleSource).toString("base64")}`);

// glInit.js is a classic script that reads GLRenderer as a global.
const box = { console, setTimeout, GLRenderer, plexoraUrl: (p) => `/${p}` };
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(JS, "views/glInit.js"), "utf8"), createContext(box));

let resolved = 0;
const renderer = box.PlexoraGL.createGLRenderer({
    indexOfTexture: () => 0, selectTexture: () => {}, resolveGLReady: () => { resolved += 1; },
});
let fetched = 0;
renderer.getter = (where) => { fetched += 1; return Promise.resolve(`// ${where}`); };

const handlers = {};
let updates = 0;
const items = [{}, {}, {}];
const viewer = {
    addHandler: (name, fn) => { (handlers[name] ||= []).push(fn); },
    world: {
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        update: () => { updates += 1; },
    },
};
const open = box.PlexoraGL.createGLInit({
    viewer, renderer, config: { tileWidth: 1024, tileHeight: 1024 },
    handleTileLoaded: () => {}, tileDrawingCustom: () => {}, tileDrawingDefault: () => {},
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
for (let i = 0; i < 6; i += 1) open();
await settle();

check("six opens fetch the two shaders once", fetched === 2, `${fetched} fetches`);
check("and compile and link them once", counts.compileShader === 2 && counts.linkProgram === 1,
    `${counts.compileShader} compiles, ${counts.linkProgram} links`);
check("the tile handlers are put on the viewer once",
    (handlers["tile-loaded"] || []).length === 1 && (handlers["tile-drawing"] || []).length === 1);
check("every open still redraws the world", updates === 6, `${updates} updates`);
check("every item is marked to draw again", items.every((item) => item._needsDraw === true));
check("the canvas is sized once, not on every open", counts.canvasResizes === 2,
    `${counts.canvasResizes} assignments to width/height`);
check("the program is kept for the cell layer to hand back to", renderer.program?.kind === "createProgram");

// a later open, long after: nothing is built again
open();
await settle();
check("a later open builds nothing", fetched === 2 && counts.compileShader === 2 && updates === 7);

// a failed build is not kept: the next open tries again
{
    const failing = box.PlexoraGL.createGLRenderer({
        indexOfTexture: () => 0, selectTexture: () => {}, resolveGLReady: () => {} });
    let tries = 0;
    failing.getter = () => { tries += 1; return tries <= 2 ? Promise.reject(new Error("offline")) : Promise.resolve("//"); };
    let rejected = false;
    await failing.ready().catch(() => { rejected = true; });
    await failing.ready();
    check("a build that failed is tried again on the next call", rejected && tries === 4);
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
