/**
 * The 2-D tile renderer recovers from what goes wrong once, and never keeps
 * stale pixels.
 *
 * Three failures, each silent on screen:
 *
 *   - A PROVISIONAL TILE'S TEXTURE IS NOT THE FINAL TILE'S. A section tile
 *     the server drew up from a coarser level (X-Plexora-Served-Steps, not
 *     cached) is replaced at the SAME address once the area is prepared in
 *     3-D. The GPU texture cache (glInit.js) was keyed by the address, so the
 *     final bytes found the provisional texture "resident" and were never
 *     uploaded: the blurry section stayed.
 *   - A FAILED SHADER FETCH IS NOT A PROGRAM. glRenderer's getter answered a
 *     failed fetch with the URL itself, which was compiled as GLSL, linked
 *     into a broken program and kept for the page -- ready()'s "a failure is
 *     not kept" did not hold, and every tile drew nothing until a reload.
 *   - A FAILED REPAINT DOES NOT STOP EVERY LATER ONE. ImageViewer.forceRepaint
 *     set ready=false, awaited loadBuffers (a /get_all_float32 fetch), and
 *     put ready back only on success: one failed fetch dropped every later
 *     repaint request without a word.
 *
 * The real GLRenderer (ES module) and the real glInit.js run against a fake
 * WebGL2 context, as gl_tile_context_restore_probe.mjs drives them;
 * forceRepaint is sliced from the shipped imageViewer.js.
 *
 * Run directly:  node tests/js/gl_tile_render_recovery_probe.mjs
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

// -- a WebGL2 context that records what it is given ----------------------------------

const calls = [];
const gl_state = { linkOk: true };
function fakeGl() {
    const canvas = { width: 0, height: 0, addEventListener() {}, removeEventListener() {} };
    const gl = new Proxy({ canvas }, {
        get(target, name) {
            if (name in target) return target[name];
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            return (...args) => {
                calls.push([name, ...args]);
                if (name === "isContextLost") return false;
                if (name === "getProgramParameter") return gl_state.linkOk;
                if (name === "getShaderParameter") return true;
                if (name.startsWith("create")) return { kind: name };
                if (name === "getAttribLocation") return 0;
                return undefined;
            };
        },
    });
    canvas.getContext = () => gl;
    return gl;
}
const count = (name) => calls.filter((c) => c[0] === name).length;

// -- a server for the two shader files, answered to fetch and to XHR alike ------------

const server = { answers: {} };
function answer(url) {
    const path = String(url).replace(/^.*?(client\/src\/shaders\/)/, "$1");
    const status = server.answers[path] ?? 200;
    return { status, body: status === 200 ? `// GLSL of ${path}` : "<html>Not Found</html>" };
}
globalThis.XMLHttpRequest = class {
    open(method, url) { this.url = url; }
    setRequestHeader() {}
    send() {
        const { status, body } = answer(this.url);
        this.status = status;
        this.response = body;
        setTimeout(() => this.onload && this.onload(), 0);
    }
};
const fetchFake = (url) => {
    const { status, body } = answer(url);
    return Promise.resolve({ ok: status === 200, status, text: async () => body });
};

globalThis.document = { createElement: () => ({ getContext: () => fakeGl() }) };
const moduleSource = readFileSync(join(JS, "services/glRenderer.js"), "utf8");
const { GLRenderer } = await import(
    `data:text/javascript;base64,${Buffer.from(moduleSource).toString("base64")}`);

const box = { console: { ...console, log() {}, warn() {}, error() {} }, setTimeout, GLRenderer,
              fetch: fetchFake, XMLHttpRequest: globalThis.XMLHttpRequest, plexoraUrl: (p) => `/${p}` };
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(JS, "views/glInit.js"), "utf8"), createContext(box));

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

function makeRenderer() {
    return box.PlexoraGL.createGLRenderer({
        indexOfTexture: () => 0, selectTexture: () => {}, resolveGLReady: () => {},
    });
}

/** A failed build must never be kept, and a sound one must be built from source. */
async function outcome(renderer) {
    try {
        await renderer.ready();
        return "built";
    } catch (error) {
        return "refused";
    }
}

// -- a provisional section tile, then the final one at the same address ---------------

{
    const renderer = makeRenderer();
    renderer.gl_arguments = {};
    const source = { format: 8, getTileKey: (level, x, y) => `16-0-xzy40-${level}-${x}-${y}` };
    const draw = (coarse, fill) => {
        const bytes = new Uint8Array(16).fill(fill);
        const tile = { level: 2, x: 0, y: 0, _array: bytes, _format: "u8" };
        if (coarse) tile._plexoraCoarse = true;
        renderer.loadArray({ tiledImage: { source }, tile }, 4, 4);
    };
    const uploads = () => calls.filter((c) => c[0] === "texImage2D").length;
    const start = uploads();
    draw(true, 10);
    const afterCoarse = uploads();
    draw(false, 200);
    const afterFinal = uploads();
    draw(false, 200);
    const afterAgain = uploads();
    check("the final bytes of a provisional section tile are uploaded, not bound from the coarse texture",
        afterCoarse === start + 1 && afterFinal === afterCoarse + 1,
        `uploads: coarse ${afterCoarse - start}, final ${afterFinal - afterCoarse} (0: the coarse texture was bound in its place)`);
    check("...and the final tile, drawn again, is a cache hit",
        afterAgain === afterFinal, `${afterAgain - afterFinal} re-upload(s)`);
}

// -- a shader fetch that fails, then succeeds -------------------------------------------

{
    calls.length = 0;
    const renderer = makeRenderer();
    server.answers["client/src/shaders/frag.glsl"] = 404;
    const first = await outcome(renderer);
    const compiledUrl = calls.some((c) => c[0] === "shaderSource" && /^\/?client\/src\/shaders\//.test(String(c[2])));
    check("a shader that fails to fetch is not compiled: its URL is not GLSL",
        !compiledUrl, "the address of the shader was handed to shaderSource as its source");
    check("...and ready() refuses rather than keeping a broken program",
        first === "refused", `ready() resolved (${first}): the broken program is now the page's`);
    delete server.answers["client/src/shaders/frag.glsl"];
    const second = await outcome(renderer);
    const sources = calls.filter((c) => c[0] === "shaderSource").map((c) => String(c[2]));
    check("the next ready() fetches again and builds from the shaders' source",
        second === "built" && sources.slice(-2).every((s) => s.startsWith("// GLSL of")),
        `${second}; last sources ${JSON.stringify(sources.slice(-2))}`);
}

// -- a program that does not link ---------------------------------------------------------

{
    const renderer = makeRenderer();
    gl_state.linkOk = false;
    const first = await outcome(renderer);
    gl_state.linkOk = true;
    const second = await outcome(renderer);
    check("a program that does not link is refused, and the next ready() builds it again",
        first === "refused" && second === "built", `${first}, then ${second}`);
}

// -- forceRepaint after a failed loadBuffers ------------------------------------------------

{
    const source = readFileSync(join(JS, "views/imageViewer.js"), "utf8");
    const start = source.indexOf("    async forceRepaint() {");
    const end = source.indexOf("    /**\n     * @function scheduleRepaint", start);
    const method = source.slice(start, end);
    const Viewer = new Function(`return class { ${method} };`)();
    const viewer = new Viewer();
    let redraws = 0;
    let fail = true;
    let loads = 0;
    Object.assign(viewer, {
        ready: true,
        idCount: 5,
        markerCacheKey: "CD3,CD8",
        viewerManagers: [{ viewer: { forceRedraw: () => { redraws += 1; } } }],
        async loadBuffers() {
            loads += 1;
            if (fail) throw new Error("HTTP 503");
        },
    });
    const origError = console.error;
    console.error = () => {};
    let threw = false;
    try {
        await viewer.forceRepaint();
    } catch (error) {
        threw = true;
    }
    fail = false;
    await viewer.forceRepaint().catch(() => {});
    console.error = origError;
    check("a repaint after one whose loadBuffers failed still runs",
        loads === 2 && viewer.ready === true,
        `loadBuffers ran ${loads} time(s); ready is ${viewer.ready}: every later repaint is dropped`);
    check("...and the failed one still redraws what is there, without an unhandled rejection",
        !threw && redraws === 2, `threw: ${threw}, redraws: ${redraws}`);
    check("...and forgets the markers it could not load, so the next one fetches them again",
        viewer.markerCacheKey === null || viewer.markerCacheKey === undefined,
        `markerCacheKey still "${viewer.markerCacheKey}": the next loadBuffers thinks they are on the GPU`);
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
