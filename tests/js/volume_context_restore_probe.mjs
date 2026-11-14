/**
 * The 3-D view draws again after its WebGL context is lost and restored
 * (views/volumeRenderer.js attach / mount / lost).
 *
 * A GPU driver reset or the browser reclaiming the context fires
 * `webglcontextlost` on the 3-D canvas; the renderer hands the viewer back to
 * planes and asks for the context back (preventDefault). When the browser
 * restores it, every object made on the old context is gone and its state is
 * back to the defaults (UNPACK_ALIGNMENT 4). Opening 3-D again must then draw
 * on the live context: a quad buffer of the live context, levels uploaded with
 * an unpack alignment of 1 into textures of the live context, and
 * `plexora:volume-mounted` raised again so the surfaces and the handles hook
 * the view again.
 *
 * Here the real classic script runs in node against a WebGL2 context that
 * models loss and restore: objects carry the generation of the context they
 * were made on, a restore starts a new generation with the default state, and
 * binding an object of an older generation, or uploading an R8 level whose
 * rows are not 4-byte aligned under UNPACK_ALIGNMENT 4, is recorded as
 * INVALID_OPERATION, as WebGL2 does.
 *
 * Run directly:  node tests/js/volume_context_restore_probe.mjs
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

// -- a WebGL2 context that can be lost and restored ----------------------------

const glStates = new WeakMap();
const uploads = [];
const glErrors = [];

function makeGl(canvas) {
    const state = { lost: false, gen: 0, alignment: 4, prevented: false, bound: {} };
    const make = (kind) => (state.lost ? null : { kind, gen: state.gen });
    const stale = (object) => object && object.gen !== state.gen;
    const ext = {
        loseContext() {
            state.lost = true;
            state.prevented = false;
            canvas.fire("webglcontextlost", { preventDefault() { state.prevented = true; } });
        },
        restoreContext() {
            if (!state.lost || !state.prevented) return;
            state.lost = false;
            state.gen += 1;
            state.alignment = 4;
            state.bound = {};
            canvas.fire("webglcontextrestored", {});
        },
    };
    const methods = {
        isContextLost: () => state.lost,
        getExtension: (name) => (name === "WEBGL_lose_context" ? ext : null),
        getParameter: (name) => (name === "MAX_3D_TEXTURE_SIZE" ? 2048 : 0),
        createBuffer: () => make("buffer"),
        createTexture: () => make("texture"),
        createShader: () => make("shader"),
        createProgram: () => make("program"),
        createVertexArray: () => make("vao"),
        getShaderParameter: () => true,
        getProgramParameter: () => true,
        pixelStorei(name, value) {
            if (state.lost) return;
            if (name === "UNPACK_ALIGNMENT") state.alignment = value;
        },
        bindBuffer(target, buffer) {
            if (state.lost) return;
            if (stale(buffer)) glErrors.push(`bindBuffer: INVALID_OPERATION (a buffer of context generation ${buffer.gen}, live is ${state.gen})`);
            state.bound[target] = buffer;
        },
        bindTexture(target, texture) {
            if (state.lost) return;
            if (stale(texture)) glErrors.push(`bindTexture: INVALID_OPERATION (a texture of context generation ${texture.gen}, live is ${state.gen})`);
            state.bound[target] = texture;
        },
        texImage3D(target, level, internal, width, height, depth) {
            if (state.lost) return;
            const texture = state.bound[target];
            uploads.push({ gl: proxy, gen: state.gen, alignment: state.alignment, internal, width,
                           textureGen: texture ? texture.gen : null });
            if (internal === "R8" && state.alignment === 4 && width % 4) {
                glErrors.push(`texImage3D: INVALID_OPERATION (R8 width ${width} under UNPACK_ALIGNMENT 4)`);
            }
        },
    };
    const proxy = new Proxy({}, {
        get(target, name) {
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            if (name in methods) return methods[name];
            return () => undefined;
        },
    });
    glStates.set(proxy, state);
    return proxy;
}

function element(tag) {
    const listeners = {};
    const el = {
        tagName: String(tag).toUpperCase(), hidden: false, className: "", style: {}, children: [],
        width: 300, height: 150, clientWidth: 400, clientHeight: 300, tabIndex: -1,
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, removeAttribute() {}, matches: () => false, contains: () => false,
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        removeEventListener(type, fn) {
            listeners[type] = (listeners[type] || []).filter((each) => each !== fn);
        },
        appendChild(child) { el.children.push(child); return child; },
        remove() {},
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 300 }),
        fire(type, event) { for (const fn of [...(listeners[type] || [])]) fn({ type, ...event }); },
        listeners,
    };
    if (el.tagName === "CANVAS") {
        let gl = null;
        el.getContext = (kind) => {
            if (kind === "webgl2") return (gl ||= makeGl(el));
            return null;
        };
    }
    return el;
}

// -- the page ---------------------------------------------------------------------

const slots = [
    { index: 0, name: "DNA", enabled: true, range: [10, 200], color: "#ff0000" },
    { index: 1, name: "CD8", enabled: true, range: [0, 255], color: "#00ff00" },
];
const sidebar = {
    channelSlots: slots,
    dataLayer: { getFullChannelName: (name) => `full:${name}` },
    isHdMode: () => false,
};
const SHAPE = [4, 10, 10];  // an R8 level 10 voxels wide: rows not 4-byte aligned
function fetchImpl(url) {
    const buffer = new ArrayBuffer(SHAPE[0] * SHAPE[1] * SHAPE[2]);
    const headers = { "X-Plexora-Shape": SHAPE.join(","), "X-Plexora-Format": "u8",
                      "X-Plexora-Level": "1", "X-Plexora-Box": "0,0,40,40" };
    return Promise.resolve({ ok: true, status: 200, arrayBuffer: async () => buffer,
                             headers: { get: (name) => headers[name] ?? null } });
}

const wrapper = element("div");
const events = [];
const box = {
    console, setTimeout, clearTimeout, performance, URLSearchParams, URL,
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { events.push(event.type); return true; },
    addEventListener: () => {}, removeEventListener: () => {},
    fetch: fetchImpl,
    document: {
        createElement: (tag) => element(tag),
        getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
        querySelector: () => null,
        activeElement: null,
    },
    __plexora: { viewerSidebar: sidebar },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
runInContext(readFileSync(join(JS, "services/inspectGeometry.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/volumeCamera.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/volumeRenderer.js"), "utf8"), context);
const VolumeRenderer = box.PlexoraVolumeRenderer;

let current = { mode: "plane", t: 0, slab: { z0: 0, z1: 3 }, volume: {} };
const subscribers = [];
const viewState = {
    depth: 4,
    info: { width: 40, height: 40 },
    enabled: () => true,
    get: () => current,
    isVolume: (state = current) => state?.mode === "volume",
    set(patch) {
        current = { ...current, ...patch };
        for (const fn of [...subscribers]) fn(current);
    },
    subscribe(fn) { subscribers.push(fn); return () => {}; },
};
const settle = async () => {
    for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};
const mounted = () => events.filter((type) => type === "plexora:volume-mounted").length;

const controller = VolumeRenderer.attach(viewState, { datasource: "vol" });
check("the 3-D controller attaches", Boolean(controller));

// 1. 3-D on: the first canvas, its context, its levels.
viewState.set({ mode: "volume" });
await settle();
const first = controller.renderer;
check("3-D opens and mounts once", Boolean(first?.gl) && mounted() === 1, `mounted ${mounted()}`);
check("the first levels upload cleanly", uploads.length === 2 && glErrors.length === 0,
      JSON.stringify({ uploads: uploads.length, glErrors }));

// 2. The driver takes the context; the view goes back to planes.
const firstGl = first.gl;
glStates.get(firstGl) && firstGl.getExtension("WEBGL_lose_context").loseContext();
check("a lost context hands the viewer back to planes", current.mode === "plane", current.mode);

// 3. The browser gives the context back (the renderer asked, with preventDefault).
firstGl.getExtension("WEBGL_lose_context").restoreContext();
check("the renderer asked for the context back", glStates.get(firstGl).gen === 1);

// 4. The user opens 3-D again.
const uploadMark = uploads.length;
const errorMark = glErrors.length;
viewState.set({ mode: "volume" });
await settle();
const again = controller.renderer;
const liveGen = again?.gl ? glStates.get(again.gl)?.gen : undefined;

check("plexora:volume-mounted is raised again, so surfaces and handles hook the live view",
      mounted() === 2, `raised ${mounted()} time(s)`);
check("the quad the ray caster binds is a buffer of the live context",
      Boolean(again?.quad) && again.quad.gen === liveGen,
      `quad of generation ${again?.quad?.gen}, live context is generation ${liveGen}`);
const after = uploads.slice(uploadMark).filter((each) => each.gl === again?.gl);
check("the levels are uploaded again after the restore", after.length >= 2,
      `${after.length} upload(s) on the live context`);
check("every level is uploaded with UNPACK_ALIGNMENT 1, into a texture of the live context",
      after.length > 0 && after.every((each) => each.alignment === 1 && each.textureGen === liveGen),
      JSON.stringify(after.map(({ alignment, textureGen, width }) => ({ alignment, textureGen, width }))));
check("no INVALID_OPERATION after the restore", glErrors.length === errorMark,
      glErrors.slice(errorMark).join("; "));

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks failed:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
