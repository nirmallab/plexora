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
 *   what lets the browser keep them;
 * - the 3-D view asks for no contrast of its own, whole or cut: a channel's
 *   one contrast (views/viewerSidebar.js) is every view's;
 * - the reference grid: 1-2-5 steps, microns only from a stated spacing (pixels
 *   and planes otherwise), its numbers where the camera puts them, the
 *   preference repaired, and the uniforms that turn it on and off;
 * - a texture being replaced (a window turning exact) is drawn from until the
 *   new one lands, in its own window, never evicted meanwhile; the new one
 *   dissolves in at a still camera, never across a camera move or a capture;
 * - the area (`setArea`): another area fetches; while it loads the last
 *   texture stands in, read where the new area overlaps it, so a slide never
 *   draws a blank frame; an area the same size keeps the level; Shift+drag
 *   slides it (at most one move per SLIDE_MS, the camera untouched), Alt or
 *   the middle button pans;
 * - MIP keeps its samples while moving; the jitter is on a fixed grid;
 * - a detail is drawn at the base's brightness: one gain per channel from
 *   its columns' maxima against the base's over the same footprint, or what
 *   the same two levels measured when it is too small to say;
 * - close up -- nearer than a lattice step -- the part seen is still found,
 *   as tight as the view; the camera stops in front of the box, by zoom or
 *   by a far-zoomed plane opened in 3-D.
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
runInContext(readFileSync(join(JS, "services/inspectGeometry.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/volumeCamera.js"), "utf8"), context);
runInContext(readFileSync(join(JS, "views/volumeRenderer.js"), "utf8"), context);
const VolumeRenderer = box.PlexoraVolumeRenderer;
const G = box.PlexoraInspectGeometry;

const bytesOf = (shape) => shape[0] * shape[1] * shape[2];
function fetchImpl(url) {
    requests.push(url);
    const query = new URLSearchParams(url.split("?")[1]);
    const shape = query.get("box") ? [4, 8, 8] : [4, 16, 16];
    const half = query.get("format") === "f16";
    const buffer = new ArrayBuffer(bytesOf(shape) * (half ? 2 : 1));
    // The box the server covers: what was asked, rounded out (serve.py).
    const asked = query.get("box") ? query.get("box").split(",").map(Number) : null;
    const covered = asked ? [Math.floor(asked[0]), Math.floor(asked[1]), Math.ceil(asked[0] + asked[2]),
                             Math.ceil(asked[1] + asked[3])].join(",") : "0,0,32,32";
    const headers = { "X-Plexora-Shape": shape.join(","), "X-Plexora-Format": half ? "f16" : "u8",
                      "X-Plexora-Level": "1", "X-Plexora-Box": covered };
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
check("at the level the others are drawn at, so one picture has one resolution",
    new URLSearchParams(requests[2].split("?")[1]).get("level") === "1", requests[2]);

hd = true;
await settle();
check("HD is another format: fetched as half floats",
    requests.length === 6 && requests.slice(3).every((url) => url.includes("format=f16")));
hd = false;
await settle();
check("and back to fast tiles is the textures already held", requests.length === 6);

check("setArea says whether the area changed", renderer.setArea({ x: 0, y: 0, width: 16, height: 16 },
                                                                 { refresh: false }) === true
      && renderer.setArea({ x: 0, y: 0, width: 16, height: 16 }, { refresh: false }) === false);
await settle();
check("another area is fetched", requests.length === 9 && requests.slice(6).every((url) => url.includes("box=")));
check("the area covered is the server's rounding", JSON.stringify(
    (renderer.setArea({ x: 10.4, y: 0, width: 16.2, height: 16 }, { refresh: false }), renderer.areaCovered()))
    === JSON.stringify([10, 0, 27, 16]));
renderer.setArea(null, { refresh: false });
await settle();
check("and the whole plane again is not", requests.length === 9);

// The budget: room for the three whole-plane u8 textures and nothing else.
renderer.gpuBudget = 3 * bytesOf([4, 16, 16]);
renderer.draw = VolumeRenderer.prototype.draw;
renderer.evict(new Set(["full:DNA|0|u8||0,4095", "full:CD8|0|u8||0,4095", "full:CD3|0|u8||0,4095"]));
const keys = [...renderer.textures.keys()];
check("past the GPU budget the textures not being drawn go, the drawn ones stay",
    keys.length === 3 && keys.every((key) => key.endsWith("|0|u8||0,4095")) && renderer.textureBytes <= renderer.gpuBudget,
    JSON.stringify(keys));
check("and their GPU memory is given back", counts.deleteTexture === 6);

// -- a finer detail where the camera looks ---------------------------------------

{
    const levelOf = { value: 0 };
    const detailRequests = [];
    const fetchDetail = (url) => {
        const query = new URLSearchParams(url.split("?")[1]);
        if (!query.get("z")) return fetchImpl(url);
        detailRequests.push(query);
        const shape = [4, 8, 8];
        const headers = { "X-Plexora-Shape": shape.join(","), "X-Plexora-Format": "u8",
                          "X-Plexora-Level": String(levelOf.value), "X-Plexora-Box": "8,8,24,24",
                          "X-Plexora-Z": "1,2" };
        return Promise.resolve({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(256),
                                 headers: { get: (name) => headers[name] ?? null } });
    };
    hd = false;
    slots.forEach((slot, i) => { slot.enabled = i === 0; });
    const view = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl: fetchDetail });
    view.gl = fakeGl();
    view.maxTexture = 2048;
    view.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600, hidden: false };
    await view.refresh();
    const share = (r) => r.hi.reduce((p, v, i) => p * (v - r.lo[i]), 1);
    view.camera.q = box.PlexoraVolumeCamera.VIEWS.xy.slice();   // looking down the z axis
    view.camera.dist = 3;
    const far = view.visibleRegion();
    view.camera.dist = 0.5;
    const near = view.visibleRegion();
    check("seen from afar the whole stack is in view; zoomed in, a part of it",
        share(far) > 0.95 && share(near) < 0.35, `${share(far).toFixed(2)} ${share(near).toFixed(2)}`);
    levelOf.value = 0;               // the whole level is level 1: this is finer
    await view.considerDetail();
    check("zoomed in at rest, the part seen is fetched finer and drawn from",
        detailRequests.length === 1 && view.detail?.lo && view.detail.hi[0] > view.detail.lo[0]);
    await view.considerDetail();
    check("the same view again asks nothing", detailRequests.length === 1);
    // A second channel switched on, the camera still: the same region again.
    slots[1].enabled = true;
    await view.refresh();
    await view.considerDetail();
    check("a channel switched on while zoomed in fetches only its own detail",
        detailRequests.length === 2, String(detailRequests.length));
    check("...and keeps every channel's, the new one's included",
        view.detail && view.channels().every((c) => view.textures.has(view.detailKey(c, view.detail))));
    await view.considerDetail();
    check("...so the next rest asks nothing again", detailRequests.length === 2, String(detailRequests.length));
    slots[1].enabled = false;
    await view.refresh();
    view.camera.dist = 0.3;           // another part: nothing of it held
    view.detail = null;
    view._noFiner = null;
    levelOf.value = 1;               // no finer than what is drawn
    await view.considerDetail();
    check("a part with nothing finer to give is not drawn from a second texture",
        view.detail === null && view._noFiner !== null,
        JSON.stringify({ detail: Boolean(view.detail), noFiner: view._noFiner, asked: detailRequests.length,
                         region: view.visibleRegion() }));
    const asked = detailRequests.length;
    await view.considerDetail();
    check("and is not asked for again while the view stays", detailRequests.length === asked);
}

// -- brightness across levels: a finer piece at the base's brightness -------------
{
    const B = VolumeRenderer.brightness;
    check("half floats read as their values, a negative one as 0",
          B.halfValue(0x3c00) === 1 && B.halfValue(0x3800) === 0.5 && B.halfValue(0xbc00) === 0
          && B.halfValue(0x7c00) === 0);
    // Two planes of 2 x 4: the column maxima, every second row and column.
    const voxels = new Uint8Array([1, 9, 3, 4, 5, 6, 7, 8, 2, 1, 8, 1, 9, 1, 1, 1]);
    const peaks = B.foldPeaks(B.peaksFor(2, 4, 2, false), voxels, [2, 2, 4], { stride: 2 });
    const every = B.foldPeaks(B.peaksFor(2, 4, 1, false), voxels, [2, 2, 4]);
    check("a fold keeps each sampled column's largest value",
          Array.from(peaks).join() === "2,8" && Array.from(every).join() === "2,9,8,4,9,6,7,8",
          `${Array.from(peaks).join()} / ${Array.from(every).join()}`);
    const halves = B.foldPeaks(B.peaksFor(1, 1, 1, true), new Uint16Array([0xbc00, 0x3800]), [2, 1, 1], { half: true });
    check("a negative half never wins the fold", halves[0] === 0x3800);
    const big = B.brightnessSummary(new Uint8Array(64 * 512 * 512), [64, 512, 512], false);
    check("a base's summary keeps every plane and at most 2^20 samples",
          big.shape[0] === 64 && big.data.length <= 2 ** 20 && big.stride === 4, JSON.stringify(big.shape));
    check("a gain is clamped, and none from a dark piece",
          B.brightnessGain({ sum: 1, n: 1 }, { sum: 100, n: 1 }) === B.GAIN_RANGE[0]
          && B.brightnessGain({ sum: 0, n: 4 }, { sum: 1, n: 4 }) === null);

    // A base of 2 planes, 8 x 8 voxels over 8 x 8 pixels: 64 everywhere but
    // 192 in its top-left 2 x 2.
    const plane = (z) => Array.from({ length: 64 }, (_, i) => (z === 0 && i % 8 < 2 && i < 16 ? 192 : 64));
    const baseVoxels = new Uint8Array([...plane(0), ...plane(1)]);
    const base = { shape: [2, 8, 8], covered: [0, 0, 8, 8], level: 1, format: "u8",
                   summary: B.brightnessSummary(baseVoxels, [2, 8, 8], false) };
    const view = new VolumeRenderer({ viewState: { depth: 2, info: { width: 8, height: 8 }, get: () => ({ t: 0 }) },
                                      datasource: "vol", fetchImpl });
    const finer = (side, value) => B.peakSum(B.foldPeaks(B.peaksFor(side, side, 1, false),
                                                         new Uint8Array(2 * side * side).fill(value), [2, side, side]), false);
    const whole = view.detailGain({ name: "DNA" }, base, { covered: [0, 0, 8, 8], z: [0, 1], level: 0 }, finer(16, 128));
    check("a detail is brought to the base's brightness over its own footprint", Math.abs(whole - 72 / 128) < 1e-6,
          String(whole));
    const small = view.detailGain({ name: "DNA" }, base, { covered: [0, 0, 2, 2], z: [0, 1], level: 0 }, finer(4, 128));
    check("one too small to match on its own takes what the same levels measured, not its own few samples",
          Math.abs(small - 5376 / 8704) < 1e-6, String(small));
    check("a detail with no base to match is drawn as it is",
          view.detailGain({ name: "DNA" }, null, { covered: [0, 0, 8, 8], level: 0 }, finer(16, 128)) === 1);

    const detail = VolumeRenderer.fragmentSource(2, true, false);
    check("the shader scales a detail's samples by its channel's gain, the base's not",
          detail.includes("uniform float u_detail_gain[NCH];")
          && detail.includes("texture(u_detail1, q).r * u_detail_gain[1] : baseRead(u_volume1, p)")
          && !VolumeRenderer.fragmentSource(2, false, false).includes("u_detail_gain"));
}

// -- close up: the part seen, and the camera kept out of the box ------------------
{
    // A thin stack (2 planes of 512 x 512): closer than a lattice step, no
    // lattice point is on screen.
    const thin = new VolumeRenderer({ viewState: { depth: 2, info: { width: 512, height: 512 }, get: () => ({ t: 0 }) },
                                      datasource: "vol", fetchImpl });
    thin.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600, hidden: false };
    thin.camera.q = box.PlexoraVolumeCamera.VIEWS.xy.slice();
    Object.assign(thin.camera, { dist: 0.02, panX: 0.03, panY: 0 });
    const region = thin.visibleRegion();
    const share = region ? region.hi.reduce((p, v, i) => p * (v - region.lo[i]), 1) : null;
    check("zoomed in closer than a lattice step, the part seen is still found, as tight as the view",
          region && share < 0.001 && region.lo[0] < 0.53 && region.hi[0] > 0.53 && region.lo[1] < 0.5
          && region.hi[1] > 0.5, JSON.stringify({ region, share }));

    // The probe's stack: 4 planes of 32 x 32, its box 0.125 deep.
    const view = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl });
    view.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600, hidden: false };
    view.camera.q = box.PlexoraVolumeCamera.VIEWS.xy.slice();
    view.camera.dist = 1;
    for (let i = 0; i < 40; i += 1) view.zoomBy(0.5);
    const closest = view.zoomTarget ?? view.camera.dist;
    check("zoomed all the way in, the camera stops in front of the box, not inside it",
          closest > 0.0625 + 0.01 && closest < 0.0625 + 0.02, String(closest));
    // Turned, the eye stops just outside the box -- not as far back as the
    // box's farthest corner (half a side away here).
    Object.assign(view.camera, { q: box.PlexoraVolumeCamera.VIEWS.iso.slice(), dist: 1, panX: 0, panY: 0 });
    view.zoomTarget = null;
    for (let i = 0; i < 40; i += 1) view.zoomBy(0.5);
    view.camera.dist = view.zoomTarget ?? view.camera.dist;
    const eye = view.cameraMatrices().eye;
    const gap = Math.hypot(...eye.map((v, a) => Math.max(0, Math.abs(v) - [0.5, 0.5, 0.0625][a])));
    check("turned, the camera comes as close as just outside the box",
          gap > 0.01 && gap < 0.03 && view.camera.dist < 0.3, JSON.stringify({ eye, gap, dist: view.camera.dist }));
    check("a far-zoomed plane opens at the closest outside the box",
          view.frameRegion({ box: [10, 10, 12, 12], perPixel: 400 }) && view.camera.dist > 0.0625 + 0.01,
          String(view.camera.dist));
}

// -- the 3-D view asks for no contrast of its own ----------------------------------
{
    const sent = [];
    const hintAsks = [];
    const dims4 = G.dims({ width: 32, height: 32 }, 4);
    let inspect = G.clean({}, dims4);
    box.dispatchEvent = (event) => { if (event.type === "plexora:volume-hint") sent.push(event.detail); return true; };
    const clipState = { depth: 4, info: { width: 32, height: 32 },
                        get: () => ({ t: 0, z: 1, slab: { z0: 0, z1: 3 }, inspect }) };
    const counting = (url) => {
        if (url.includes("/hint?")) hintAsks.push(url);
        return fetchImpl(url);
    };
    const clipped = new VolumeRenderer({ viewState: clipState, datasource: "vol", fetchImpl: counting });
    clipped.gl = fakeGl();
    clipped.maxTexture = 2048;
    await clipped.refresh();
    inspect = G.clean({ mode: "crop", axes: { x: false, z: true }, crop: { z: [1, 2] } }, dims4);
    clipped.invalidate();
    await new Promise((resolve) => setTimeout(resolve, 0));
    check("neither the whole stack nor a cut asks for contrast, or sends any",
          hintAsks.length === 0 && sent.length === 0 && typeof clipped.refreshHints === "undefined",
          JSON.stringify({ hintAsks, sent }));
}

// -- the cut is uniforms (services/inspectGeometry.js) ------------------------------
{
    const near = (a, b) => Math.abs(a - b) < 1e-6;
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const info = { width: 2000, height: 1000,
                   spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
                              z: { value: 2, unit: "µm", source: "metadata" } } };
    const d = G.dims(info, 40);
    let state = { t: 0, z: 12, slab: { z0: 0, z1: 39 },
                  inspect: G.clean({}, d), volume: {} };
    const vs = { depth: 40, info, get: () => state };
    const cutter = new VolumeRenderer({ viewState: vs, datasource: "vol", fetchImpl });
    cutter.covered = [0, 0, 2000, 1000];
    const set = (raw) => { state = { ...state, inspect: G.clean(raw, d) }; };
    let u = cutter.cutUniforms();
    check("the whole stack is the whole box", same(u.lo, [0, 0, 0]) && same(u.hi, [1, 1, 1]) && u.cut === 0 && !u.hole);
    set({ mode: "crop", axes: { x: true, z: true }, crop: { x: [200, 1200], z: [4, 9] } });
    u = cutter.cutUniforms();
    check("a crop is the box", near(u.lo[0], 0.1) && near(u.hi[0], 0.6) && near(u.lo[2], 0.1) && near(u.hi[2], 0.25),
          JSON.stringify(u));
    cutter.covered = [500, 0, 1500, 1000];
    u = cutter.cutUniforms();
    check("x relative to the area the textures cover", near(u.lo[0], 0) && near(u.hi[0], 0.7), JSON.stringify(u));
    cutter.covered = [0, 0, 2000, 1000];
    cutter.setClip({ x: [0.3, 1], y: [0, 1] });
    u = cutter.cutUniforms();
    check("Video Studio's clip narrows the cut and never widens it", near(u.lo[0], 0.3) && near(u.hi[0], 0.6));
    cutter.setClip(null);
    check("the surfaces' z range follows the cut", near(cutter.clipRangeZ()[0], 0.1) && near(cutter.clipRangeZ()[1], 0.25));
    cutter.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600 };
    set({ mode: "crop", axes: { x: false, z: true }, crop: { z: [10, 19] } });
    const kept = cutter.visibleRegion();
    check("a crop's part seen is inside it", kept && kept.lo[2] >= 0.25 - 1e-9 && kept.hi[2] <= 0.5 + 1e-9,
          JSON.stringify(kept));
    delete cutter.canvas;

    const calls = {};
    cutter.gl = new Proxy({}, { get: (target, name) => (typeof name !== "string" ? undefined
        : (/^[A-Z_0-9]+$/.test(name) ? name : (...args) => {
            if (name.startsWith("uniform")) calls[args[0]] = args.slice(1);
        })) });
    const uniform = (name) => name;
    set({ mode: "crop", axes: { x: true, z: true }, crop: { x: [200, 1200], z: [4, 9] } });
    cutter.bindCut(uniform, cutter.cutUniforms());
    check("a crop binds its box and nothing beyond it",
          near(calls.u_box_lo[0][0], 0.1) && near(calls.u_box_hi[0][2], 0.25) && calls.u_cut[0] === 0
          && !("u_hole_on" in calls), JSON.stringify(calls));
    check("no section or slab uniform is bound any more",
          !Object.keys(calls).some((name) => /u_plane|u_slab|u_context|u_vox|u_cut_alpha|u_cut_color/.test(name)),
          Object.keys(calls).join());
    set({ mode: "cutaway", axes: { x: true, y: true, z: false }, face: true,
          cutaway: { x: { at: 800, keep: "hi" }, y: { at: 300, keep: "lo" } } });
    cutter.bindCut(uniform, cutter.cutUniforms());
    check("a cutaway saved over two axes is cut along its first, faces solid, no corner",
          near(calls.u_box_lo[0][0], 0.4) && near(calls.u_box_lo[0][1], 0) && near(calls.u_box_hi[0][1], 1)
          && calls.u_cut[0] === G.CUT.faces && !("u_hole_on" in calls), JSON.stringify(calls));
    state = { ...state, volume: { area: { x: 500, y: 0, width: 1000, height: 1000 } } };
    cutter.box = state.volume.area;
    set({ mode: "crop", axes: { x: true, z: false }, crop: { x: [200, 1200] } });
    u = cutter.cutUniforms();
    check("with an area, the frame is the area and the crop is kept inside it",
          same(cutter.frameCovered(), [500, 0, 1500, 1000]) && near(u.lo[0], 0) && near(u.hi[0], 0.7),
          JSON.stringify(u));
    cutter.box = null;
    state = { ...state, volume: {} };
    const before = cutter.cameraKey();
    set({ mode: "full" });
    check("a cut is a camera move: it never dissolves", cutter.cameraKey() !== before);
    box.PlexoraInspect = { viewState: vs, resolved: () => G.resolve(G.clean({ mode: "crop",
        axes: { x: false, z: true }, crop: { z: [0, 9] } }, d), d) };
    check("the cut controller's answer -- a preview -- wins over the state", near(cutter.cutUniforms().hi[2], 0.25));
    delete box.PlexoraInspect;
}

// -- the shader --------------------------------------------------------------------
{
    const text = VolumeRenderer.fragmentSource(2, false, false);
    check("the box is uniforms; the old z and xy clip are gone",
          text.includes("uniform vec3 u_box_lo;") && !text.includes("u_clip_z") && !text.includes("u_clip_xy"));
    for (const name of ["sampleAt", "cutFace", "baseRead"]) {
        check(`the shader has ${name}`, text.includes(`${name}(`));
    }
    check("no cut-away corner is left anywhere: one axis a Cut",
          !["inHole", "u_hole"].some((name) => text.includes(name) || VolumeRenderer.fragmentSource(2, true, false).includes(name)
              || readFileSync(join(JS, "views/volumeRenderer.js"), "utf8").includes(name)
              || readFileSync(join(JS, "views/surfaceRenderer.js"), "utf8").includes(name)));
    check("a base texture is read through the frame's transform, nothing outside it",
          /float baseRead\(sampler3D tex, vec3 p\)[\s\S]*u_tex_lo \+ p \* u_tex_span/.test(text)
          && text.includes("baseRead(u_volume0, p)"));
    for (const name of ["inSlab", "cutPlanes", "cutSlabs", "skipMarch", "u_plane", "u_slab", "u_context",
                        "u_vox", "u_cut_alpha", "u_cut_color"]) {
        check(`the shader has no ${name} left`, !text.includes(name)
              && !VolumeRenderer.fragmentSource(3, true, false).includes(name)
              && !readFileSync(join(JS, "views/volumeRenderer.js"), "utf8").includes(name));
    }
    const detail = VolumeRenderer.fragmentSource(2, true, false);
    check("a detail's sample point reads the detail where it holds it",
          /void sampleAt[\s\S]*inDetail[\s\S]*u_detail1/.test(detail));
    check("one sampleAt in each variant", (text.match(/void sampleAt\(/g) || []).length === 1
          && (detail.match(/void sampleAt\(/g) || []).length === 1);
}

// -- a stack no level of which fits a 3-D texture ----------------------------------

{
    const said = "no level of this stack fits a 3-D texture (900 MB, 4096 px a side at its coarsest)";
    const refuse = () => Promise.resolve({ ok: false, status: 400, json: async () => ({ error: said }),
                                           headers: { get: () => null } });
    const notes = [];
    const wrapper = { appendChild: (node) => notes.push(node) };
    const realCreate = box.document.createElement;
    box.document.createElement = (tag) => (tag === "p"
        ? { tag, hidden: true, textContent: "", setAttribute() {}, remove() {} } : realCreate(tag));
    const refused = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl: refuse, wrapper });
    refused.gl = fakeGl();
    refused.maxTexture = 2048;
    await refused.refresh();
    check("a refused level is not an error: the view says why there is nothing to draw",
          refused.status === "unavailable" && notes.length === 1 && notes[0].textContent === said
          && notes[0].hidden === false, JSON.stringify({ status: refused.status, notes }));
    refused.show(false);
    check("and the sentence leaves with the 3-D view", notes[0].hidden === true);
    box.document.createElement = realCreate;
}

// -- a channel switched on for the first time is drawn at once --------------------
{
    // The plane asks for a channel's window the moment it is switched on
    // (viewerManager.addChannelItems), and the window's first answer is
    // announced to nobody (volumeWindows.adopt). A level fetched before it
    // is known was kept under one window and looked for under the next: the
    // channel stayed dark until it was switched off and on again.
    runInContext(readFileSync(join(JS, "services/volumeWindows.js"), "utf8"), context);
    const fetchWindow = (url) => {
        const names = new URLSearchParams(url.split("?")[1]).getAll("channel");
        const windows = Object.fromEntries(names.map((name) => [name, { qmin: 7, qmax: 900, exact: true }]));
        return new Promise((resolve) => setTimeout(
            () => resolve({ ok: true, json: async () => ({ windows }) }), 5));
    };
    const windows = new box.PlexoraVolumeWindows("vol", { fetchImpl: fetchWindow });
    box.__plexora.volumeWindows = windows;
    hd = false;
    slots.forEach((slot, i) => { slot.enabled = i === 0; });
    requests.length = 0;
    const view = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl });
    view.gl = fakeGl();
    view.maxTexture = 2048;
    await windows.ensure(["full:DNA"]);          // the plane had it before 3-D
    await view.refresh();
    // Switched on in 3-D: the plane's ask and the 3-D view's refresh start
    // in the same CHANNELS_CHANGE.
    slots[1].enabled = true;
    const planeAsk = windows.ensure(["full:CD8"]);
    await view.refresh();
    await planeAsk;
    const drawn = view.channels().filter((channel) => view.textureOf(channel)).map((c) => c.name);
    check("a channel switched on for the first time is drawn once its level arrives",
          drawn.length === 2, `drawn: ${drawn.join(", ")}; keys: ${[...view.textures.keys()].join(" ")}`);
    check("and its level is asked for with the window the plane's tiles use",
          requests.length === 2 && requests.every((url) => url.includes("w=7%2C900")), requests.join(" "));
    delete box.__plexora.volumeWindows;
}

// -- a channel comes in rather than lands -----------------------------------------
{
    // A GL that compiles anything and keeps the last value of each uniform.
    const set = {};
    const recordingGl = new Proxy({}, {
        get(target, name) {
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            if (name === "getUniformLocation") return (program, uniform) => uniform;
            if (name.startsWith("uniform")) return (location, ...values) => { set[location] = values; };
            if (name === "getShaderParameter" || name === "getProgramParameter") return () => true;
            if (name === "createTexture" || name === "createProgram") return () => ({});
            return () => undefined;
        },
    });
    hd = false;
    slots.forEach((slot, i) => { slot.enabled = i === 0; });
    const view = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl });
    view.gl = recordingGl;
    view.maxTexture = 2048;
    view.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600, hidden: false };
    await view.refresh();
    view.draw(1000);
    view.draw(1000 + 300);                   // DNA's own fade has run its course
    const opacity = () => [...set.u_opacity[0]];
    check("a channel already drawn is drawn in full", opacity().length === 1 && opacity()[0] === 0.6
          && !view.fading(), JSON.stringify(opacity()));
    slots[1].enabled = true;
    await view.refresh();
    view.draw(2000);
    const first = opacity();
    view.draw(2000 + 120);
    const middle = opacity();
    check("a channel switched on starts from nothing; the one already there is untouched",
          first[0] === 0.6 && first[1] === 0 && view.fading(), JSON.stringify(first));
    check("and comes in over the fade, eased out (past half way at half time)",
          middle[1] > 0.3 && middle[1] < 0.6 && set.u_color[0][4] > 0.5 && set.u_color[0][4] < 1,
          JSON.stringify({ middle, colour: [...set.u_color[0]] }));
    view.draw(2000 + 240);
    check("at the end of the fade it is in full, and nothing more is drawn for it",
          opacity()[1] === 0.6 && !view.fading(), JSON.stringify(opacity()));
    slots[2].enabled = true;
    await view.refresh();
    view.draw(3000);
    check("while it comes in, the view asks for every frame", view.fading());
    view.snapshot();
    check("a capture draws every channel in full, never partway in",
          opacity().every((value) => value === 0.6) && !view.fading(), JSON.stringify(opacity()));
    slots[2].enabled = false;
    view.draw(4000);
    slots[2].enabled = true;
    view.prefs = { smoothMotion: () => false };
    view.draw(4001);
    check("with motion off a channel lands at once",
          opacity().every((value) => value === 0.6) && !view.fading(), JSON.stringify(opacity()));
    view.prefs = null;
    view.show(false);
    view.show(true);
    view.draw(5000);
    check("back in 3-D, what is drawn comes in again",
          opacity().every((value) => value === 0) && view.fading(), JSON.stringify(opacity()));
}

// -- the reference grid ----------------------------------------------------------
{
    const G = VolumeRenderer.grid;
    check("an automatic step is a 1-2-5 value with about six lines across",
          G.niceStep(1000) === 200 && G.niceStep(300) === 50 && G.niceStep(120) === 20
          && G.niceStep(0.9) === 0.2 && G.niceStep(6) === 1,
          [1000, 300, 120, 0.9, 6].map((v) => G.niceStep(v)).join());
    check("finer and coarser are the neighbouring 1-2-5 values",
          G.niceNeighbour(50, -1) === 20 && G.niceNeighbour(50, 1) === 100
          && G.niceNeighbour(1, -1) === 0.5 && G.niceNeighbour(200, 1) === 500);
    check("a tick is printed without float dust", G.formatTick(0.1 + 0.2) === "0.3"
          && G.formatTick(150) === "150" && G.formatTick(-1e-12) === "0");

    // 1000 x 500 pixels of 0.5 µm by 20 planes of 2 µm: 500 x 250 x 40 µm.
    const covered = [0, 0, 1000, 500];
    const extent = [1, 0.5, 0.08];
    const microns = { x: { value: 0.5, unit: "µm", source: "metadata" },
                      z: { value: 2, unit: "µm", source: "manual" } };
    const axes = G.axes({ spacing: microns, covered, planes: 20, extent });
    check("stated spacing is counted in microns on every axis, one step for all",
          axes.units.join() === "µm,µm,µm" && axes.auto === 100
          && axes.step.join() === "100,100,100", JSON.stringify(axes));
    check("the box's centre is the area's centre, z from the first plane's centre",
          axes.centre.join() === "250,125,19" && axes.per.join() === "500,500,500",
          JSON.stringify(axes));
    const nm = G.axes({ spacing: { x: { value: 500, unit: "nm", source: "metadata" },
                                   z: { value: 0.002, unit: "mm", source: "metadata" } },
                        covered, planes: 20, extent });
    check("nanometres and millimetres are turned into microns",
          nm.units.join() === "µm,µm,µm" && nm.centre.join() === "250,125,19");
    const assumed = G.axes({ spacing: { x: { value: 1, unit: "µm", source: "assumed" },
                                        z: { value: 2, unit: "µm", source: "metadata" } },
                             covered, planes: 20, extent });
    check("an assumed pixel size is never printed as microns: pixels and planes",
          assumed.units.join() === "px,px,planes" && assumed.step[0] === 200
          && assumed.step[2] === 5 && assumed.centre[2] === 9.5, JSON.stringify(assumed));
    const noZ = G.axes({ spacing: { x: microns.x, z: { value: null, source: "missing" } },
                         covered, planes: 20, extent });
    check("a stated pixel size with no z step: microns across, planes in depth",
          noZ.units.join() === "µm,µm,planes" && noZ.step[0] === 100 && noZ.step[2] === 5);
    const chosen = G.axes({ spacing: microns, covered, planes: 20, extent, step: 20 });
    const crowded = G.axes({ spacing: microns, covered, planes: 20, extent, step: 0.01 });
    check("a chosen step is kept, but never more than forty lines across",
          chosen.step[0] === 20 && crowded.step[0] === 20, `${chosen.step} ${crowded.step}`);

    // Projection: the camera's own matrices, looking down onto XY.
    const C = box.PlexoraVolumeCamera;
    const view = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl });
    view.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600 };
    view.camera = { q: C.VIEWS.xy.slice(), dist: 2, panX: 0, panY: 0 };
    const { forward, eye } = view.cameraMatrices();
    const centre = G.project(forward, [0, 0, 0], 800, 600);
    const f = 1 / Math.tan(0.35);
    const right = G.project(forward, [0.5, 0, 0], 800, 600);
    const down = G.project(forward, [0, 0.25, 0], 800, 600);
    const expectX = 400 + (0.5 * f / (800 / 600) / 2) * 400;
    const expectY = 300 + (0.25 * f / 2) * 300;
    check("a point in the box lands where the camera puts it (XY: x right, y down)",
          Math.abs(centre.x - 400) < 1e-6 && Math.abs(centre.y - 300) < 1e-6
          && Math.abs(right.x - expectX) < 1e-3 && Math.abs(right.y - 300) < 1e-3
          && Math.abs(down.y - expectY) < 1e-3 && Math.abs(down.x - 400) < 1e-3,
          JSON.stringify({ centre, right, down, expectX, expectY }));
    check("and a point behind the camera is not projected",
          G.project(forward, [0, 0, -5], 800, 600) === null);

    view.camera = { q: C.VIEWS.iso.slice(), dist: 2.2, panX: 0, panY: 0 };
    const iso = view.cameraMatrices();
    // A deeper stack (20 planes of 10 µm), so z has an edge long enough.
    const deep = [1, 0.5, 0.4];
    const cube = G.axes({ spacing: { x: microns.x, z: { value: 10, unit: "µm", source: "metadata" } },
                          covered, planes: 20, extent: deep });
    const lo = deep.map((e) => -e / 2);
    const hi = deep.map((e) => e / 2);
    const labels = G.labels({ forward: iso.forward, eye: iso.eye, lo, hi, axes: cube, width: 800, height: 600 });
    const titles = labels.filter((label) => label.title).map((label) => label.text);
    const ticks = labels.filter((label) => !label.title);
    check("from three-quarters every axis is numbered, and named with its unit",
          titles.join() === "X (µm),Y (µm),Z (µm)" && ticks.length >= 6, JSON.stringify(labels));
    check("every number is a line's value, on the screen",
          ticks.every((label) => Number(label.text) % 100 === 0 && label.x >= 0 && label.x <= 800
                       && label.y >= 0 && label.y <= 600), JSON.stringify(ticks));
    const along = G.labels({ forward, eye, lo, hi, axes: cube, width: 800, height: 600 });
    check("looking straight down z, z has no edge to be numbered along",
          !along.some((label) => label.text.startsWith("Z")), JSON.stringify(along.filter((l) => l.title)));

    check("the preference is repaired: nonsense reads as the default",
          JSON.stringify(G.prefs({ on: "yes", faces: "ceiling", step: -3, opacity: 9, labels: 0 }))
          === JSON.stringify({ on: false, faces: "walls", step: "auto", opacity: 1, labels: true })
          && G.prefs({ on: true, faces: "floor", step: 25, opacity: 0.3 }).step === 25
          && JSON.stringify(G.prefs(null)) === JSON.stringify(G.DEFAULTS));

    const source = VolumeRenderer.fragmentSource(2);
    check("the grid is drawn by the ray caster, on the face each ray leaves by",
          source.includes("vec4 gridBehind(") && source.includes("uniform float u_grid;")
          && source.includes("grid.rgb") && /fwidth\(f\)/.test(source));

    // The uniforms, with the grid off and on.
    const set = {};
    const recordingGl = new Proxy({}, {
        get(target, name) {
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            if (name === "getUniformLocation") return (program, uniform) => uniform;
            if (name.startsWith("uniform")) return (location, ...values) => { set[location] = values; };
            if (name === "getShaderParameter" || name === "getProgramParameter") return () => true;
            if (name === "createTexture" || name === "createProgram") return () => ({});
            return () => undefined;
        },
    });
    slots.forEach((slot, i) => { slot.enabled = i === 0; });
    const drawn = new VolumeRenderer({ viewState: { ...viewState, info: { width: 32, height: 32,
                                                                        spacing: microns } },
                                       datasource: "vol", fetchImpl });
    drawn.gl = recordingGl;
    drawn.maxTexture = 2048;
    drawn.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600, hidden: false };
    await drawn.refresh();
    drawn.draw(1000);
    check("with no grid preference the grid is off: an opacity of nothing",
          set.u_grid?.[0] === 0 && !("u_grid_scale" in set), JSON.stringify(set.u_grid));
    let grid = { on: true, faces: "floor", step: "auto", opacity: 0.3, labels: true };
    drawn.prefs = { smoothMotion: () => false, grid: () => grid };
    drawn.draw(2000);
    // 32 x 32 px of 0.5 µm by 4 planes of 2 µm: 16 x 16 x 8 µm, the longest
    // side one box unit, and a 2 µm step -- eight lines per box unit.
    const scale = [...set.u_grid_scale[0]];
    check("on, the shader is told the opacity, the faces and lines per box unit",
          Math.abs(set.u_grid[0] - 0.3) < 1e-6 && set.u_grid_faces[0] === 1
          && scale.every((v) => Math.abs(v - 16 / 2) < 1e-4), JSON.stringify({ scale, g: set.u_grid }));
    grid = { ...grid, on: false };
    drawn.draw(3000);
    check("and off again, nothing but the opacity", set.u_grid[0] === 0);
}

// -- a texture being replaced is not a channel gone ---------------------------------
{
    // The provisional window turning exact (services/volumeWindows.js) gives a
    // channel a new texture key. The old texture used to be filtered out of
    // the draw until the new one landed -- the channel went black, then faded
    // in from nothing. Now it keeps drawing, in its own window.
    const set = {};
    const bound = [];
    const recordingGl = new Proxy({}, {
        get(target, name) {
            if (typeof name !== "string") return undefined;
            if (/^[A-Z_0-9]+$/.test(name)) return name;
            if (name === "getUniformLocation") return (program, uniform) => uniform;
            if (name.startsWith("uniform")) return (location, ...values) => { set[location] = values; };
            if (name === "getShaderParameter" || name === "getProgramParameter") return () => true;
            if (name === "createTexture" || name === "createProgram") return () => ({ id: Math.random() });
            if (name === "bindTexture") return (target, texture) => bound.push(texture);
            return () => undefined;
        },
    });
    const marks = [];
    box.PlexoraViewPerf = { mark: (...args) => marks.push(args), provide() {} };
    const windows = new box.PlexoraVolumeWindows("vol", { fetchImpl: () => Promise.resolve({ ok: false }) });
    windows.windows.set("full:DNA", { qmin: 0, qmax: 1000, exact: false });
    box.__plexora.volumeWindows = windows;
    hd = false;
    slots.forEach((slot, i) => { slot.enabled = i === 0; });
    let release = null;
    const fetchHeld = (url) => (url.includes("w=0%2C1100")
        ? new Promise((resolve) => { release = () => resolve(fetchImpl(url)); })
        : fetchImpl(url));
    const view = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl: fetchHeld });
    view.gl = recordingGl;
    view.maxTexture = 2048;
    view.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600, hidden: false };
    const ghost = {
        captures: 0, releases: 0, cancels: 0, held: null,
        capture(cam) { this.captures += 1; this.held = cam; return true; },
        release() { this.releases += 1; },
        cancel() { this.cancels += 1; this.held = null; },
        sync(cam) { if (this.held && cam !== this.held) this.cancel(); },
    };
    view.ghost = ghost;
    await view.refresh();
    view.draw(1000);
    view.draw(1300);
    const oldKey = view._shownKey.get("full:DNA");
    const raw = [(10 / 255) * 4095, (200 / 255) * 4095];
    windows.adopt("full:DNA", { qmin: 0, qmax: 1100, exact: true });
    const loading = view.refresh();
    await new Promise((resolve) => setTimeout(resolve, 0));
    bound.length = 0;
    view.draw(2000);
    const near = (a, b) => Math.abs(a - b) < 1e-4;
    check("while its new texture loads, a channel is drawn from the one it had, in full",
          set.u_opacity?.[0]?.[0] === 0.6 && !view.fading() && view._shownKey.get("full:DNA") === oldKey,
          JSON.stringify({ opacity: set.u_opacity?.[0], key: view._shownKey.get("full:DNA") }));
    check("...read in the window that texture was made with, so its contrast holds",
          near(set.u_window[0][0], raw[0] / 1000) && near(set.u_window[0][1], Math.min(1, raw[1] / 1000)),
          JSON.stringify([...set.u_window[0]]));
    check("...and the miss is marked once", marks.filter((m) => m[1] === "texmiss").length === 1);
    view.gpuBudget = 1;
    view.evict(view.keepKeys());
    check("past the GPU budget the texture being drawn is never evicted while its replacement loads",
          view.textures.has(oldKey));
    view.gpuBudget = 1024 * 2 ** 20;
    check("finer views wait for the channel's own texture", (() => {
        let asked = false;
        const saved = view.considerDetail;
        view.considerDetail = () => { asked = true; return Promise.resolve(); };
        view.status = "ready";
        view.refine();
        view.considerDetail = saved;
        return !asked;
    })());
    const capturesBefore = ghost.captures;
    release();
    await loading;
    view.draw(2100);
    check("the new texture, once landed, is drawn in its own window",
          view._shownKey.get("full:DNA") !== oldKey && near(set.u_window[0][0], raw[0] / 1100),
          JSON.stringify([...set.u_window[0]]));
    check("...and dissolves in over the old: one capture, one release, at the same camera",
          ghost.captures === capturesBefore + 1 && ghost.releases === capturesBefore + 1);
    view.draw(2200);
    check("the same picture drawn again holds nothing", ghost.captures === capturesBefore + 1);
    view.camera.dist += 0.1;
    view.draw(2300);
    check("a camera that moves ends the fade and holds nothing",
          ghost.cancels >= 1 && ghost.captures === capturesBefore + 1);
    view.snapshot();
    check("a capture never holds one", ghost.captures === capturesBefore + 1);

    // MIP draws as many samples moving as at rest; additive halves them.
    view.composite = "mip";
    view.interacting = true;
    view.draw(3000);
    const moving = set.u_steps[0];
    view.interacting = false;
    view.draw(3100);
    check("MIP is drawn with the same samples moving as at rest", moving === set.u_steps[0] && moving > 0,
          `${moving} vs ${set.u_steps[0]}`);
    view.composite = "additive";
    view.interacting = true;
    view.draw(3200);
    const halved = set.u_steps[0];
    view.interacting = false;
    view.draw(3300);
    check("additive halves them while moving", halved === Math.round(set.u_steps[0] / 2),
          `${halved} vs ${set.u_steps[0]}`);
    check("the sampling jitter is on the full-quality grid, whatever the resolution",
          set.u_jitter_res && set.u_jitter_res[0][0] === 800 && set.u_jitter_res[0][1] === 600,
          JSON.stringify(set.u_jitter_res));
    // The area slid: the last texture stands in while the new area's loads,
    // read where the new area overlaps it; the frame is the new area's.
    view.composite = "mip";
    view.draw(3400);
    const standIn = view.entryFor(view.channels()[0]);
    let landArea = null;
    view.fetch = (url) => new Promise((resolve) => { landArea = () => resolve(fetchImpl(url)); });
    const ghostsBefore = ghost.captures;
    view.setArea({ x: 8, y: 8, width: 16, height: 16 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    view.draw(4000);
    const nearAll = (a, b) => a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < 1e-6);
    check("a new area is drawn at once from the last texture: never a blank frame",
          view.entryFor(view.channels()[0]) === standIn && Boolean(standIn) && set.u_opacity?.[0]?.[0] > 0,
          JSON.stringify(set.u_opacity));
    check("...framed as the new area, read where it lies in the old texture",
          nearAll(view.frameCovered(standIn), [8, 8, 24, 24]) && nearAll([...set.u_tex_lo[0]], [0.25, 0.25, 0])
          && nearAll([...set.u_tex_span[0]], [0.5, 0.5, 1]) && nearAll(view.extent(), [1, 1, 0.25]),
          JSON.stringify({ lo: set.u_tex_lo, span: set.u_tex_span, extent: view.extent() }));
    check("...with no dissolve on the way in (the area is a camera move)", ghost.captures === ghostsBefore);
    landArea();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    view.draw(4100);
    check("its own texture, landed, is drawn exactly (no transform) and dissolves in once",
          view.entryFor(view.channels()[0]) !== standIn && nearAll([...set.u_tex_lo[0]], [0, 0, 0])
          && nearAll([...set.u_tex_span[0]], [1, 1, 1]) && ghost.captures === ghostsBefore + 1,
          JSON.stringify({ lo: set.u_tex_lo, captures: ghost.captures - ghostsBefore }));
    view.fetch = fetchImpl;
    const asked = requests.length;
    view.setArea({ x: 12, y: 8, width: 16, height: 16 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const slid = requests.slice(asked).find((url) => url.includes("box="));
    check("an area the same size, elsewhere, is fetched at the level already drawn",
          Boolean(slid) && new URLSearchParams(slid.split("?")[1]).get("level") === "1", slid);
    delete box.__plexora.volumeWindows;
    delete box.PlexoraViewPerf;
}


// -- Shift+drag slides the area; Alt or the middle button pans ------------------------
{
    const listeners = {};
    const canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600, hidden: false,
                     addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
                     setPointerCapture() {},
                     getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) };
    const fire = (name, event) => (listeners[name] || []).forEach((fn) => fn({
        type: name, button: 0, pointerId: 1, preventDefault() {}, ...event }));
    const vs = { depth: 4, info: { width: 1000, height: 1000 }, get: () => ({ t: 0 }) };
    const view = new VolumeRenderer({ viewState: vs, datasource: "vol", fetchImpl });
    view.canvas = canvas;
    view.box = { x: 400, y: 400, width: 100, height: 100 };
    const slides = [];
    view.onSlide = (area, { final }) => slides.push({ area, final });
    view.bindPointer(canvas);
    const q = view.camera.q.slice();
    const pan = [view.camera.panX, view.camera.panY];
    fire("pointerdown", { shiftKey: true, clientX: 400, clientY: 300 });
    fire("pointermove", { shiftKey: true, clientX: 420, clientY: 300 });
    fire("pointermove", { shiftKey: true, clientX: 440, clientY: 310 });
    check("Shift+drag slides the area: at most one move per SLIDE_MS", slides.length === 1
          && slides[0].final === false && VolumeRenderer.SLIDE_MS === 40, JSON.stringify(slides));
    fire("pointerup", { shiftKey: true, clientX: 440, clientY: 310 });
    const last = slides.at(-1);
    check("...the release is the final move, the size kept, inside the image",
          slides.length === 2 && last.final === true && last.area.width === 100 && last.area.height === 100
          && (last.area.x !== 400 || last.area.y !== 400) && last.area.x >= 0 && last.area.x <= 900,
          JSON.stringify(slides));
    check("...and never turns or pans the camera", JSON.stringify(view.camera.q) === JSON.stringify(q)
          && view.camera.panX === pan[0] && view.camera.panY === pan[1] && !view.spin);
    slides.length = 0;
    fire("pointerdown", { shiftKey: true, clientX: 400, clientY: 300 });
    fire("pointermove", { shiftKey: true, clientX: 5000, clientY: 300 });
    fire("pointerup", { shiftKey: true, clientX: 5000, clientY: 300 });
    const far = slides.at(-1).area;
    check("a slide stops at the image's edge, never shrinking", far.width === 100
          && far.x >= 0 && far.x + far.width <= 1000 && far.y >= 0 && far.y + far.height <= 1000, JSON.stringify(far));
    slides.length = 0;
    fire("pointerdown", { altKey: true, clientX: 400, clientY: 300 });
    fire("pointermove", { altKey: true, clientX: 450, clientY: 300 });
    fire("pointerup", { altKey: true, clientX: 450, clientY: 300 });
    check("Alt+drag pans the camera, the area untouched", view.camera.panX !== pan[0] && slides.length === 0
          && JSON.stringify(view.camera.q) === JSON.stringify(q));
    const panned = view.camera.panX;
    fire("pointerdown", { button: 1, clientX: 400, clientY: 300 });
    fire("pointermove", { button: 1, clientX: 450, clientY: 300 });
    fire("pointerup", { button: 1, clientX: 450, clientY: 300 });
    check("the middle button pans too", view.camera.panX !== panned && slides.length === 0);
    view.box = null;
    const before = view.camera.panX;
    fire("pointerdown", { shiftKey: true, clientX: 400, clientY: 300 });
    fire("pointermove", { shiftKey: true, clientX: 450, clientY: 300 });
    fire("pointerup", { shiftKey: true, clientX: 450, clientY: 300 });
    check("with no area to slide, Shift+drag pans as it did", view.camera.panX !== before && slides.length === 0);
    // The same drag, for the area's grips (views/volumeHandles.js).
    check("no area, no area drag", view.dragArea() === null);
    view.box = { x: 400, y: 400, width: 100, height: 100 };
    const heard = [];
    const grow = view.dragArea({ to: (start, dx) => ({ ...start, width: start.width + dx }),
                                 send: (area, { final }) => heard.push({ area, final }) });
    grow.move(30, 0);
    grow.move(60, 0);
    grow.end();
    check("an area drag takes its own shape and its own ear, the last move final",
          heard.length === 2 && heard[0].area.width === 130 && heard[1].area.width === 160
          && heard[1].final === true && slides.length === 0, JSON.stringify(heard));
    heard.length = 0;
    const undone = view.dragArea({ to: (start, dx) => ({ ...start, x: start.x + dx }),
                                   send: (area, { final }) => heard.push({ area, final }) });
    undone.move(50, 0);
    undone.cancel();
    check("cancelled, the area goes back where it started",
          heard.length === 2 && heard[1].area.x === 400 && heard[1].final === true, JSON.stringify(heard));
}

// -- the pivot and the framing (a square placed on the plane) -----------------------
{
    const C = box.PlexoraVolumeCamera;
    const info = { width: 2000, height: 1000,
                   spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
                              z: { value: 2, unit: "µm", source: "metadata" } } };
    const d = G.dims(info, 40);
    let state = { t: 0, z: 12, slab: { z0: 0, z1: 39 }, inspect: G.clean({}, d), volume: {} };
    const vs = { depth: 40, info, get: () => state };
    const view = new VolumeRenderer({ viewState: vs, datasource: "vol", fetchImpl });
    view.canvas = { width: 800, height: 600, clientWidth: 800, clientHeight: 600 };
    view.prefs = { smoothMotion: () => false };
    check("with no crop there is no pivot: the stack turns about its centre", view.pivot() === null);
    state = { ...state, inspect: G.clean({ mode: "crop", axes: { x: true, y: true },
                                           crop: { x: [1400, 1600], y: [600, 800] } }, d) };
    const middle = () => view.projectImage([1500, 700, 20]);
    const before = middle();
    view.turnAboutPivot(C.turnFree(view.camera.q, 120, -60));
    const after = middle();
    check("a crop turns in place: its middle stays where it was on screen",
          Math.hypot(after.x - before.x, after.y - before.y) < 0.5, JSON.stringify([before, after]));
    const turned = view.camera.q.slice();
    check("the frame asked for is taken", view.frameRegion({ box: [1400, 600, 1600, 800], perPixel: 1.5 }));
    const framed = middle();
    check("framed: the square's middle in the middle of the view, turned the way the stack was",
          Math.abs(framed.x - 400) < 0.5 && Math.abs(framed.y - 300) < 0.5
          && C.quat.angleBetween(view.camera.q, turned) < 1e-6, JSON.stringify(framed));
    view.preset("xy");
    const left = view.projectImage([1400, 700, 20]);
    const right = view.projectImage([1600, 700, 20]);
    check("looking straight down, at the plane's magnification: 200 pixels at 1.5 is 300 on screen",
          Math.abs(right.x - left.x - 300) < 3, `${(right.x - left.x).toFixed(1)}`);
    view.preset("xz");
    const side = middle();
    check("a named view centres the crop", Math.abs(side.x - 400) < 0.5 && Math.abs(side.y - 300) < 0.5,
          JSON.stringify(side));
    check("the pivot is on screen where the stack turns about", Math.abs(view.centreOnScreen().x - 400) < 0.5
          && Math.abs(view.centreOnScreen().y - 300) < 0.5, JSON.stringify(view.centreOnScreen()));
    check("no frame without the plane's scale", !view.frameRegion({ box: [0, 0, 10, 10], perPixel: 0 }));

    // A crop far off the stack's centre, turned: `dist` (to the centre) is not
    // the distance to what is looked at, and zoom must be about the latter.
    state = { ...state, inspect: G.clean({ mode: "crop", axes: { x: true, y: true },
                                           crop: { x: [1800, 1900], y: [850, 950] } }, d) };
    view.preset("iso");
    const corner = () => view.projectImage([1850, 900, 20]);
    const away = view.pivotDistance();
    view.zoomBy(0.5);
    const zoomed = corner();
    check("zooming halves the distance to the crop, not to the stack's centre, the crop held in the middle",
          Math.abs(view.pivotDistance() - away / 2) < 1e-9 && Math.abs(zoomed.x - 400) < 0.5
          && Math.abs(zoomed.y - 300) < 0.5, `${away} -> ${view.pivotDistance()}`);
    for (let i = 0; i < 40; i += 1) view.zoomBy(0.5);
    check("zoomed all the way in, the camera stops short of the crop, never past it",
          view.pivotDistance() >= 0.03 - 1e-9 && corner() !== null, String(view.pivotDistance()));
    const kept = view.cameraState();
    view.camera.dist = 1;
    check("a camera kept that close comes back as it was, below zero or not",
          view.restoreCamera(kept) && view.camera.dist === kept.dist, `${kept.dist} -> ${view.camera.dist}`);
}

// -- a sidebar and windows of its own ----------------------------------------------
{
    // Figure Builder's Quick Edit draws a 3-D panel beside its own scoped
    // channel widget, on a page with no viewer sidebar at all: the renderer
    // is handed both rather than reading the viewer's globals.
    runInContext(readFileSync(join(JS, "services/volumeWindows.js"), "utf8"), context);
    const held = box.__plexora;
    box.__plexora = {};
    const own = {
        channelSlots: [{ index: 0, name: "A", enabled: true, range: [100, 900], color: "#ff0000" },
                       { index: 1, name: "B", enabled: true, range: [0, 4000], color: "#00ff00" }],
        dataLayer: { getFullChannelName: (name) => `now:${name}` },
        isHdMode: () => true,
        quantWindow: () => ({ qmin: 0, qmax: 4095 }),
    };
    const asked = [];
    const windows = new box.PlexoraVolumeWindows("vol", { fetchImpl: (url) => {
        asked.push(url);
        const names = new URLSearchParams(url.split("?")[1]).getAll("channel");
        return Promise.resolve({ ok: true, json: async () => ({ windows: Object.fromEntries(
            names.map((name) => [name, { qmin: 3, qmax: 5000, exact: true }])) }) });
    } });
    requests.length = 0;
    const view = new VolumeRenderer({ viewState, datasource: "vol", fetchImpl, sidebar: own, windows });
    view.gl = fakeGl();
    view.maxTexture = 2048;
    await view.refresh();
    check("handed a sidebar, the renderer draws its channels with no viewer sidebar on the page",
          requests.length === 2 && requests.some((url) => url.includes("channel=now%3AA"))
          && requests.some((url) => url.includes("channel=now%3AB")), requests.join(" "));
    check("in its HD format", view.format() === "f16" && requests.every((url) => url.includes("format=f16")));
    check("with the windows it was handed",
          asked.length === 1 && requests.every((url) => url.includes("w=3%2C5000")),
          `${asked.join(" ")} | ${requests.join(" ")}`);
    box.__plexora = held;
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
