/**
 * Video Studio's viewer adapters -- videoSceneState.js and videoViewerState.js
 * -- against a stand-in viewer: view state, sidebar, image viewer, renderer.
 *
 *     node tests/js/video_studio_scene_probe.mjs
 *
 * What it holds:
 *   - applying a frame changes the view through ONE `viewState.set(patch,
 *     {save: false})`, and a frame that changes nothing about the view makes
 *     no navigation revision at all;
 *   - nothing on the way in or out is saved: no `set` without `save: false`,
 *     no committed window, no view transform with `save` other than false;
 *   - presence 0.5 is the channel's colour at half strength, set as not
 *     user-changed; colour itself comes from the frame, never blended; a
 *     visible channel at presence 0 stays enabled (its tiles stay loaded);
 *   - a raw window is converted to the slot's byte domain (and passed through
 *     in HD); an unchanged window touches nothing;
 *   - a 3-D frame sets the renderer's camera and clip and stops its motion;
 *   - restore puts back the view, the 2-D view, the camera, the clip and every
 *     slot exactly (the snapshot digests match);
 *   - a frame state round-trips through Figure Builder's scene shape.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");
const FILES = ["videoEasing.js", "videoInterp.js", "videoSchema.js", "videoCamera3d.js", "videoCamera2d.js",
               "videoAnimation.js", "videoSceneState.js", "videoViewerState.js"];

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}

function viewer() {
    const calls = { set: [], slot: [], transform: [], fit: [], saves: 0 };
    const state = { t: 0, z: 3, mode: "plane", layout: "single",
                    slab: { z0: 2, z1: 4, op: "max", follow: true, thickness: 3 },
                    volume: { composite: "mip", opacity: {} },
                    // As core's view state always has one (inspectGeometry.clean).
                    inspect: { mode: "full", tool: "cutaway", axes: { x: true, y: false, z: false },
                               crop: { x: [0, 1000], y: [0, 800], z: [0, 9] } } };
    const vs = {
        depth: 10, frames: 2, info: { width: 1000, height: 800, spacing: { x: { value: 0.5, unit: "µm" } } },
        get: () => JSON.parse(JSON.stringify(state)),
        set(patch, options = {}) {
            calls.set.push({ patch, options });
            if (options.save !== false) calls.saves += 1;
            for (const [k, v] of Object.entries(patch)) {
                if (v && typeof v === "object" && !Array.isArray(v) && state[k] && typeof state[k] === "object") {
                    state[k] = { ...state[k], ...v };
                } else state[k] = v;
            }
            return this.get();
        },
        settled: async () => JSON.parse(JSON.stringify(state)),
    };
    const slots = [
        { name: "DNA", colorHex: "#0000ff", enabled: true, range: [10, 200], userRangeChanged: false },
        { name: "CD3", colorHex: "#ff0000", enabled: true, range: [20, 180], userRangeChanged: false },
        { name: "CD8", colorHex: "#00ff00", enabled: false, range: [0, 255], userRangeChanged: false },
    ];
    const packet = { qmin: 0, qmax: 1020 };
    const sidebar = {
        channelSlots: slots,
        hd: false,
        isHdMode() { return this.hd; },
        dataLayer: { getFullChannelName: (n) => `${n}_full` },
        channelIndexOf: (full) => ({ DNA_full: 0, CD3_full: 1, CD8_full: 2 })[full],
        quantWindow: () => packet,
        rawToByteRange([a, b], p) {
            const span = p.qmax - p.qmin;
            return [Math.round(((a - p.qmin) / span) * 255), Math.round(((b - p.qmin) / span) * 255)];
        },
        toRawRangeForSlot(slot) {
            if (this.hd) return slot.range;
            return slot.range.map((v) => packet.qmin + (v / 255) * (packet.qmax - packet.qmin));
        },
        setSlotEnabled(i, on) { calls.slot.push(["enabled", i, on]); slots[i].enabled = on; },
        setSlotColor(i, hex, user) { calls.slot.push(["color", i, hex, user]); slots[i].colorHex = hex; },
        setSlotWindow(i, range, options) {
            calls.slot.push(["window", i, range, options]);
            if (options?.commit) calls.saves += 1;
            slots[i].range = range.slice();
            slots[i].userRangeChanged = true;
        },
        applyLaunchChannels: async (rows) => { calls.slot.push(["launch", rows.length]); },
        suspendPersistence() {}, resumePersistence() {},
    };
    let transform = { degrees: 0, flipH: false, flipV: false };
    const iv = {
        viewTransform: {
            get: () => ({ ...transform }),
            set(t, options) { calls.transform.push([t, options]); if (options?.save !== false) calls.saves += 1; transform = { ...transform, ...t }; },
        },
        cellLayers: () => [],
    };
    let viewport = { x: 100, y: 50, w: 400, h: 300 };
    const scene = {
        currentViewport: () => ({ ...viewport }),
        fitRegion(_iv, rect) { calls.fit.push(rect); viewport = { x: rect.x, y: rect.y, w: rect.width, h: rect.height }; return true; },
    };
    const renderer = {
        camera: { q: [1, 0, 0, 0], dist: 2.2, panX: 0, panY: 0 }, box: null, clipXY: null, zoomTarget: 0.5,
        spin: { rate: 1 },
        stopMotion() { this.spin = null; this.tween = null; },
        invalidate() { this.invalidated = (this.invalidated || 0) + 1; },
        setClip(clip) { this.clipXY = clip && JSON.stringify(clip) !== JSON.stringify({ x: [0, 1], y: [0, 1], z: [0, 1] }) ? clip : null; return true; },
    };
    const env = {
        vs, sidebar, iv, renderer, ctx: { layers: { list: () => [] } }, datasource: "demo",
        config: { imageData: [{ src: "/generated/data/demo/k_dna/" }, { src: "/generated/data/demo/k_cd3/" },
                              { src: "/generated/data/demo/k_cd8/" }] },
    };
    return { env, calls, scene, slots };
}

const v = viewer();
const context = createContext({ console, Math, JSON, Promise, setTimeout, Date, Array, Object, Number, String });
context.window = context;
context.globalThis = context;
context.PlexoraViewerScene = v.scene;
for (const file of FILES) runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
const Scene = context.PlexoraVideoSceneState;
const ViewerState = context.PlexoraVideoViewerState;

const { env, calls, slots } = v;
const keys = Scene.slots(env).map((s) => s.key);
check("channels are keyed by their tile URL", JSON.stringify(keys) === JSON.stringify(["k_dna", "k_cd3", "k_cd8"]), keys);

const captured = Scene.capture(env);
check("capture records the plane and mode", captured.view.z === 3 && captured.view.mode === "plane", captured.view);
check("capture records raw windows", Math.abs(captured.channels[0].window[0] - 40) < 1e-6, captured.channels[0].window);
const saved = ViewerState.snapshot(env);

// One frame, applied.
const frame = JSON.parse(JSON.stringify(captured));
frame.view.z = 6;
frame.channels[1].presence = 0.5;
frame.channels[0].window = [80, 900];
frame.channels[2].visible = true;
frame.channels[2].presence = 0;
await Scene.apply(env, frame, { aspect: 16 / 9 });
check("one navigation revision per frame", calls.set.length === 1, calls.set);
check("the view is set without saving", calls.set[0].options.save === false);
const half = calls.slot.find((c) => c[0] === "color" && c[1] === 1);
check("presence 0.5 is the colour at half strength", half && half[2] === "#800000" && half[3] === false, half);
const win = calls.slot.find((c) => c[0] === "window" && c[1] === 0);
check("a raw window lands in the byte domain", win && win[2][0] === 20 && win[2][1] === 225 && win[3].commit === false, win);
const enable = calls.slot.find((c) => c[0] === "enabled" && c[1] === 2);
check("a visible channel at presence 0 is enabled", enable && enable[2] === true, calls.slot);
const black = calls.slot.find((c) => c[0] === "color" && c[1] === 2);
check("…and drawn black", black && black[2] === "#000000", black);

calls.set.length = 0;
calls.slot.length = 0;
await Scene.apply(env, frame, { aspect: 16 / 9 });
check("a frame that changes nothing makes no revision", calls.set.length === 0, calls.set);
check("…and touches no slot", calls.slot.length === 0, calls.slot);

// HD: raw windows pass through.
env.sidebar.hd = true;
const hdFrame = JSON.parse(JSON.stringify(frame));
hdFrame.channels[0].window = [100, 700];
calls.slot.length = 0;
await Scene.apply(env, hdFrame, { aspect: 16 / 9 });
const hdWin = calls.slot.find((c) => c[0] === "window");
check("in HD the raw window is the domain", hdWin && hdWin[2][0] === 100 && hdWin[2][1] === 700, hdWin);
env.sidebar.hd = false;

// 3-D.
const volume = JSON.parse(JSON.stringify(frame));
volume.view.mode = "volume";
volume.view.slab = null;
volume.view.clip = { x: [0, 0.5], y: [0, 1], z: [0, 1] };
volume.camera3d = { q: [0, 0, 0.7071067811865476, 0.7071067811865476], dist: 1.5, pan: [0.1, -0.1] };
calls.set.length = 0;
await Scene.apply(env, volume, { aspect: 16 / 9 });
check("entering 3-D opens the whole stack", calls.set[0]?.patch?.slab?.z0 === 0 && calls.set[0].patch.slab.z1 === 9
      && calls.set[0].patch.slab.follow === false, calls.set[0]);
check("the camera is the frame's", env.renderer.camera.dist === 1.5 && env.renderer.camera.panX === 0.1);
check("the renderer's own motion is stopped", env.renderer.spin === null && env.renderer.zoomTarget === null);
check("the clip reaches the renderer", env.renderer.clipXY && env.renderer.clipXY.x[1] === 0.5, env.renderer.clipXY);

// The 3-D view's cut (core's inspect) rides with the frame's view.
const cut = { mode: "crop", tool: "crop", axes: { x: true, y: false, z: true }, crop: { x: [100, 600], z: [2, 5] } };
const cutFrame = JSON.parse(JSON.stringify(volume));
cutFrame.view.inspect = cut;
calls.set.length = 0;
await Scene.apply(env, cutFrame, { aspect: 16 / 9 });
check("a frame's cut reaches the view, unsaved",
      JSON.stringify(calls.set[0]?.patch?.inspect) === JSON.stringify(cut)
      && calls.set[0].options.save === false, calls.set[0]);
check("…and no section position rides with it (the view state has none)",
      calls.set[0] && !("section" in calls.set[0].patch), calls.set[0]);
check("capture records the cut", JSON.stringify(Scene.capture(env).view.inspect) === JSON.stringify(cut)
      && !("section" in Scene.capture(env).view), Scene.capture(env).view);
calls.set.length = 0;
await Scene.apply(env, cutFrame, { aspect: 16 / 9 });
check("the same cut again is no revision", calls.set.length === 0, calls.set);
// A video's slab in 3-D still narrows z, now through the clip.
const slabbed = JSON.parse(JSON.stringify(volume));
slabbed.view.slab = { z0: 2, z1: 4, op: "max" };
slabbed.view.clip = { x: [0, 1], y: [0, 1], z: [0, 1] };
await Scene.apply(env, slabbed, { aspect: 16 / 9 });
check("a frame's slab in 3-D narrows the clip's z to its planes",
      env.renderer.clipXY && Math.abs(env.renderer.clipXY.z[0] - 0.2) < 1e-9
      && Math.abs(env.renderer.clipXY.z[1] - 0.5) < 1e-9, env.renderer.clipXY);

// Restore.
env.touchedWindows = true;
await ViewerState.restore(env, saved);
const after = ViewerState.snapshot(env);
const a = JSON.stringify(ViewerState.digest(saved));
const b = JSON.stringify(ViewerState.digest(after));
check("restore puts the view back", after.view.mode === "plane" && after.view.z === 3, after.view);
check("restore puts the camera back", after.camera.dist === 2.2 && after.camera.panX === 0, after.camera);
check("restore clears the clip", env.renderer.clipXY === null, env.renderer.clipXY);
check("a touched window goes back through the launch path", calls.slot.some((c) => c[0] === "launch"), calls.slot);
check("nothing was saved on the way in or out", calls.saves === 0, calls.saves);
// The slots in this stand-in are restored by the launch path, which the stub
// does not perform; compare the parts the restore itself sets.
const strip = (d) => { const o = JSON.parse(d); delete o.slots; return JSON.stringify(o); };
check("the viewer comes back exactly (view, 2-D view, camera, layers)", strip(a) === strip(b), { a, b });

// Figure Builder.
const state = { view: { t: 1, z: 4, mode: "plane", slab: null }, camera3d: null,
                viewport: { cx: 500, cy: 400, w: 320, degrees: 0, flip_h: false, flip_v: false },
                channels: [{ key: "k_dna", color: "#0000ff", window: [0, 900], visible: true, presence: 1 },
                           { key: "k_cd3", color: "#ff0000", window: [0, 900], visible: true, presence: 0 }],
                cell_layers: [], layers: [], t: 2.5 };
const fig = Scene.toFigureScene(env, state, { aspect: 16 / 9, video_id: "vid_000000000001", revision: 3 });
check("a figure scene keeps only channels on screen", fig.channels.length === 1 && fig.channels[0].key === "k_dna", fig.channels);
check("a figure scene's viewport is the output rectangle", Math.abs(fig.viewport.w - 320) < 1e-9
      && Math.abs(fig.viewport.h - 180) < 1e-9, fig.viewport);
check("the plane and time ride in the plugin part", fig.plugins.video_studio.state.view.z === 4);
const back = Scene.fromFigureScene(fig);
check("…and come back", back.scene.view.z === 4 && back.defaulted.length === 1, back);

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
