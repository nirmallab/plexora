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
 * - the default contrast follows the clip range: the level's own hint over
 *   the whole stack, the planes kept (`/hint?z=`, at the level drawn) when
 *   the range is narrower, asked once per range;
 * - a texture being replaced (a window turning exact) is drawn from until the
 *   new one lands, in its own window, never evicted meanwhile; the new one
 *   dissolves in at a still camera, never across a camera move or a capture;
 * - MIP keeps its samples while moving; the jitter is on a fixed grid.
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
check("at the level the others are drawn at, so one picture has one resolution",
    new URLSearchParams(requests[2].split("?")[1]).get("level") === "1", requests[2]);

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

// -- the contrast follows the clip range -----------------------------------------
{
    const sent = [];
    const hintAsks = [];
    const dims4 = G.dims({ width: 32, height: 32 }, 4);
    let inspect = G.clean({}, dims4);
    box.dispatchEvent = (event) => { if (event.type === "plexora:volume-hint") sent.push(event.detail); return true; };
    const clipState = { depth: 4, info: { width: 32, height: 32 },
                        get: () => ({ t: 0, z: 1, section: {}, slab: { z0: 0, z1: 3 }, inspect }) };
    const fetchHint = (url) => {
        if (url.includes("/hint?")) {
            hintAsks.push(url);
            return Promise.resolve({ ok: true, json: async () => ({ hint: [5, 50], level: 1, z: [1, 2] }) });
        }
        const response = fetchImpl(url);
        return response.then((r) => ({ ...r, headers: { get: (name) => (name === "X-Plexora-Hint" ? "1,900" : r.headers.get(name)) } }));
    };
    const clipped = new VolumeRenderer({ viewState: clipState, datasource: "vol", fetchImpl: fetchHint });
    clipped.gl = fakeGl();
    clipped.maxTexture = 2048;
    await clipped.refresh();
    check("over the whole stack the level's own hint is sent",
          sent.length >= 1 && sent.every((d) => d.hint.join() === "1,900") && hintAsks.length === 0,
          JSON.stringify(sent));
    sent.length = 0;
    inspect = G.clean({ mode: "crop", axes: { z: true }, crop: { z: [1, 2] } }, dims4);
    clipped.refreshHints({ now: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const asked = hintAsks.map((url) => new URLSearchParams(url.split("?")[1]));
    check("a narrower clip range asks for its planes' contrast at the level drawn",
          asked.length >= 1 && asked.every((q) => q.get("z") === "1,2" && q.get("level") === "1" && q.get("w")),
          hintAsks.join(" "));
    check("and sends it", sent.length >= 1 && sent.every((d) => d.hint.join() === "5,50"), JSON.stringify(sent));
    const before = hintAsks.length;
    clipped.refreshHints({ now: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    check("the same range is not asked again", hintAsks.length === before);
    sent.length = 0;
    inspect = G.clean({ mode: "full" }, dims4);
    clipped.refreshHints({ now: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    check("back to the whole stack, the level's hint again", sent.length >= 1
          && sent.every((d) => d.hint.join() === "1,900") && hintAsks.length === before);
}

// -- the cut is uniforms (services/inspectGeometry.js) ------------------------------
{
    const near = (a, b) => Math.abs(a - b) < 1e-6;
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const info = { width: 2000, height: 1000,
                   spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
                              z: { value: 2, unit: "µm", source: "metadata" } } };
    const d = G.dims(info, 40);
    let state = { t: 0, z: 12, section: { x: 500, y: 250 }, slab: { z0: 0, z1: 39 },
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
    check("the contrast is levelled over the cropped planes", same(cutter.clipRange(), [4, 9]));
    set({ mode: "section", axes: { z: true }, context: 0 });
    check("a Z section drawn alone is levelled by its plane", same(cutter.clipRange(), [12, 12]));
    set({ mode: "section", axes: { z: true } });
    check("with the surroundings drawn too, by the whole stack", cutter.clipRange() === null);

    const calls = {};
    cutter.gl = new Proxy({}, { get: (target, name) => (typeof name !== "string" ? undefined
        : (/^[A-Z_0-9]+$/.test(name) ? name : (...args) => {
            if (name.startsWith("uniform")) calls[args[0]] = args.slice(1);
        })) });
    const uniform = (name) => name;
    set({ mode: "slab", axes: { x: true, z: true }, slab: { thickness: { x: 20, z: 4 }, composite: "mean" },
          context: 0.3 });
    cutter.bindCut(uniform, cutter.cutUniforms(), cutter.covered, [1, 0.5, 0.08]);
    check("a slab binds its planes, half thicknesses and how it combines",
          calls.u_cut[0] === 2 && calls.u_slab_op[0] === 2 && near(calls.u_context[0], 0.3)
          && same(calls.u_plane_on[0], [1, 0, 1]) && near(calls.u_slab_half[0][2], 2 / 40)
          && near(calls.u_slab_half[0][0], 10 / 2000), JSON.stringify(calls));
    check("and a voxel in box units, for a slab's sum", near(calls.u_vox[0][0], 1 / 2000) && near(calls.u_vox[0][2], 0.08 / 40));
    set({ mode: "cutaway", axes: { x: true, y: true, z: false }, face: true,
          cutaway: { x: { at: 800, keep: "hi" }, y: { at: 300, keep: "lo" } } });
    cutter.bindCut(uniform, cutter.cutUniforms(), cutter.covered, [1, 0.5, 0.08]);
    check("a cutaway over two axes binds its corner, faces solid",
          calls.u_hole_on[0] === 1 && near(calls.u_hole_hi[0][0], 0.4) && near(calls.u_hole_lo[0][1], 0.3)
          && same(calls.u_box_lo[0], [0, 0, 0]) && calls.u_cut[0] === 3 && calls.u_cut_alpha[0] === 1,
          JSON.stringify(calls));
    const before = cutter.cameraKey();
    set({ mode: "full" });
    check("a cut is a camera move: it never dissolves", cutter.cameraKey() !== before);
    box.PlexoraInspect = { viewState: vs, resolved: () => G.resolve(G.clean({ mode: "crop", axes: { z: true },
        crop: { z: [0, 9] } }, d), G.positions(state, d), d) };
    check("the cut controller's answer -- a preview -- wins over the state", near(cutter.cutUniforms().hi[2], 0.25));
    delete box.PlexoraInspect;
}

// -- the shader --------------------------------------------------------------------
{
    const text = VolumeRenderer.fragmentSource(2, false, false);
    check("the box is uniforms; the old z and xy clip are gone",
          text.includes("uniform vec3 u_box_lo;") && !text.includes("u_clip_z") && !text.includes("u_clip_xy"));
    for (const name of ["sampleAt", "inHole", "inSlab", "cutPlanes", "cutSlabs", "cutFace"]) {
        check(`the shader has ${name}`, text.includes(`${name}(`));
    }
    check("a section with no surroundings marches nothing", text.includes("if (i >= count || skipMarch) break;"));
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
    view.box = { x: 0, y: 0, width: 16, height: 16 };
    check("another area is never stood in for by the last one",
          view.entryFor(view.channels()[0]) === null);
    delete box.__plexora.volumeWindows;
    delete box.PlexoraViewPerf;
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
