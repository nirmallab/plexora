/**
 * Video Studio's pure modules -- easing, interpolation, arc length, the 3-D and
 * 2-D cameras, the animation evaluator -- run in node:vm exactly as the page
 * loads them (classic scripts on one global).
 *
 *     node tests/js/video_studio_math_probe.mjs [fixtures.json]
 *
 * What it holds:
 *   - every easing starts at 0 and ends at 1; step holds; cinematic covers 1;
 *   - unwrapped quaternions never flip (consecutive dot >= 0), and a squad path
 *     through four views keeps dot(q_k, q_k+1) > 0 on every sampled frame;
 *   - arc-length reparameterisation gives constant speed (CV < 1 %);
 *   - a 360° orbit sampled k in [0, N) is seamless: frame N (never rendered)
 *     equals frame 0, frame N-1 does not, and every step turns the same angle;
 *   - an orbit about a pivot keeps the pivot's projected screen position and
 *     depth fixed through the renderer's own matrices, with anisotropic spacing;
 *   - extent() matches the renderer's formula, area box and missing-z fallback;
 *   - a log zoom from 1000 to 100 passes 316.23 at its midpoint, and a 2-D
 *     zoom into a sub-region keeps the shared point fixed on screen;
 *   - with a fixtures file (written by the pytest wrapper from the Python
 *     presets), the evaluator's acquired t/z/slab/mode on every frame equal
 *     server/schema.py's acquired_plan, and the frame counts agree.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");
const FILES = ["videoEasing.js", "videoInterp.js", "videoArcLength.js", "videoSchema.js",
               "videoCamera3d.js", "videoCamera2d.js", "videoAnimation.js", "videoHistory.js"];

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}
const near = (a, b, eps = 1e-6) => Math.abs(a - b) <= eps;

const context = createContext({ console, crypto: globalThis.crypto, Math, JSON, Float64Array,
                                Float32Array, Uint8Array, Array, Object, Number });
context.window = context;
context.globalThis = context;
for (const file of FILES) runInContext(readFileSync(join(STATIC, file), "utf8"), context,
                                       { filename: file });
const { PlexoraVideoEasing: Easing, PlexoraVideoInterp: Interp, PlexoraVideoArcLength: Arc,
        PlexoraVideoSchema: Schema, PlexoraVideoCamera3d: Cam3, PlexoraVideoCamera2d: Cam2,
        PlexoraVideoAnimation: Anim, PlexoraVideoHistory: History } = context;

// -- easing -------------------------------------------------------------------
for (const name of Easing.NAMES) {
    const f = Easing.resolve(name);
    if (name === "step") continue;
    check(`easing ${name} starts at 0`, near(f(0), 0), f(0));
    check(`easing ${name} ends at 1`, near(f(1), 1), f(1));
}
check("step holds until the end", Easing.ease("step", 0.999) === 0 && Easing.ease("step", 1) === 1);
const bez = Easing.resolve([0.42, 0, 0.58, 1]);
check("bezier is monotone", [0.1, 0.3, 0.5, 0.7, 0.9].every((u, i, a) => i === 0
    || bez(u) > bez(a[i - 1])));
check("cinematic custom covers 1", near(Easing.ease({ accel: 0.4, decel: 0.1 }, 1), 1));

// -- quaternions -----------------------------------------------------------------
const { VIEWS } = { VIEWS: { xy: [1, 0, 0, 0], xz: [Math.SQRT1_2, 0, 0, Math.SQRT1_2],
                             yz: [Math.SQRT1_2, 0, Math.SQRT1_2, 0] } };
const views = [VIEWS.xy, VIEWS.xz.map((v) => -v), VIEWS.yz, [0.5, 0.5, -0.5, 0.5]];
const unwrapped = Interp.unwrapQuats(views);
check("unwrapped quaternions never flip",
      unwrapped.every((q, i) => i === 0 || Interp.dot4(unwrapped[i - 1], q) >= 0));
const pathFrames = unwrapped.map((q, i) => ({ t: i * 2, ease: "linear",
                                               value: { q, dist: 2 + i * 0.2, pan: [0, 0] } }));
let previous = null;
let flipped = 0;
let maxStep = 0;
for (let k = 0; k <= 240; k += 1) {
    const pose = Cam3.pathAt(pathFrames, (k / 240) * 6, { orientation: "squad", path: "catmull" });
    if (previous) {
        if (Interp.dot4(previous.q, pose.q) <= 0) flipped += 1;
        maxStep = Math.max(maxStep, Interp.angleBetween(previous.q, pose.q));
    }
    previous = pose;
}
check("a squad path through four views never flips", flipped === 0, flipped);
check("a squad path has no jumps", maxStep < 0.1, maxStep);

// -- arc length --------------------------------------------------------------------
const curve = (s) => [Math.cos(s * 3) * (1 + 3 * s * s), Math.sin(s * 3) * (1 + 3 * s * s), s];
const arc = Arc.table(curve, 512);
const cv = Arc.speedVariation(curve, arc, 100);
check("arc-length speed is constant (CV < 1 %)", cv < 0.01, cv);

// -- orbit: seamless loop, pivot fixed -------------------------------------------------
const start = { q: Interp.normalize([0.3, 0.2, 0.1, 0.9]), dist: 2.2, pan: [0.05, -0.02] };
const N = 120;
const orbitAt = (k) => Cam3.orbit(start, { axis: "y", turns: 1, direction: 1 }, k / N);
const f0 = orbitAt(0);
const fN = orbitAt(N);
const fLast = orbitAt(N - 1);
check("orbit frame N equals frame 0", Interp.angleBetween(f0.q, fN.q) < 1e-9);
check("orbit frame N-1 differs from frame 0", Interp.angleBetween(f0.q, fLast.q) > 1e-3);
const steps = [];
for (let k = 1; k <= N; k += 1) steps.push(Interp.angleBetween(orbitAt(k - 1).q, orbitAt(k).q));
check("orbit turns the same angle every frame",
      Math.max(...steps) - Math.min(...steps) < 1e-9, [Math.min(...steps), Math.max(...steps)]);
check("orbit step is 2 pi / N", near(steps[0], (2 * Math.PI) / N, 1e-9), steps[0]);

const facts = { image_size: [1538, 763], depth: 190,
                spacing: { x: { value: 0.5 }, z: { value: 2 } } };
const e = Cam3.extent(facts, null);
check("extent: longest side is 1", near(Math.max(...e), 1), e);
check("extent: anisotropic z", near(e[2] / e[0], (190 * 2) / (1538 * 0.5)), e);
const eBox = Cam3.extent(facts, { x: 100, y: 100, width: 200, height: 100 });
check("extent: an area box uses its own size", near(eBox[0] / eBox[1], 2) && near(eBox[2], 1), eBox);
const eNoZ = Cam3.extent({ image_size: [100, 100], depth: 50, spacing: { x: { value: 0.5 } } });
check("extent: missing z spacing falls back to x", near(eNoZ[2], 0.5), eNoZ);

const pivot = Cam3.modelPoint(facts, null, 1200, 200, 150);
const aspect = 16 / 9;
const p0 = Cam3.project(start, pivot, aspect);
let drift = 0;
for (let k = 0; k < 60; k += 1) {
    const pose = Cam3.orbit(start, { axis: "z", turns: 1, direction: 1, pivot }, k / 60);
    const p = Cam3.project(pose, pivot, aspect);
    drift = Math.max(drift, Math.abs(p[0] - p0[0]), Math.abs(p[1] - p0[1]), Math.abs(p[2] - p0[2]));
}
check("orbit about a pivot keeps it fixed on screen", drift < 1e-9, drift);
const rocked = Cam3.rock(start, { axis: "x", amplitude_deg: 30, cycles: 2 }, 1);
check("rock returns to the start", Interp.angleBetween(rocked.q, start.q) < 1e-9);
const tumbled = Cam3.tumble(start, { turns: 2, tilt_deg: 20 }, 1);
check("tumble loops", Interp.angleBetween(tumbled.q, start.q) < 1e-9);

// -- 2-D ------------------------------------------------------------------------------
check("log zoom midpoint", near(Interp.logLerp(1000, 100, 0.5), 316.227766, 1e-5));
const wide = { cx: 5000, cy: 4000, w: 10000 };
const tight = { cx: 7000, cy: 3000, w: 1000 };
const fx = (wide.cx * tight.w - tight.cx * wide.w) / (tight.w - wide.w);
const fy = (wide.cy * tight.w - tight.cy * wide.w) / (tight.w - wide.w);
let screenDrift = 0;
for (let i = 0; i <= 20; i += 1) {
    const v = Cam2.between(wide, tight, i / 20);
    const sx = (fx - v.cx) / v.w;
    const sy = (fy - v.cy) / v.w;
    const s0 = (fx - wide.cx) / wide.w;
    screenDrift = Math.max(screenDrift, Math.abs(sx - s0), Math.abs(sy - (fy - wide.cy) / wide.w));
}
check("a 2-D zoom keeps the shared point still", screenDrift < 1e-9, screenDrift);
check("a 2-D zoom ends exactly", near(Cam2.between(wide, tight, 1).w, 1000)
      && near(Cam2.between(wide, tight, 1).cx, 7000));
check("rotation takes the short way", near(Interp.angleLerp(350, 10, 0.5), 0) || near(
    Interp.angleLerp(350, 10, 0.5), 360));

// -- the evaluator ------------------------------------------------------------------------
const project = {
    duration_s: 4, loop: false, source: { depth: 10, frames: 1, image_size: [100, 100] },
    base: { view: { t: 0, z: 0, mode: "plane", slab: null, composite: "mip",
                    clip: { x: [0, 1], y: [0, 1], z: [0, 1] } },
            camera3d: null, viewport: { cx: 50, cy: 50, w: 100 }, channels: [
                { key: "a", color: "#ff0000", window: [0, 100], visible: true, presence: 1,
                  opacity: 1 },
                { key: "b", color: "#00ff00", window: [0, 100], visible: true, presence: 1,
                  opacity: 1 }], cell_layers: [], layers: [] },
    tracks: [
        { track_id: "z", kind: "acquired_z", mode: "procedural", range: [0, 4], muted: false,
          procedural: { generator: "sweep", params: { from: 0, to: 9 }, ease: "linear" },
          keyframes: [] },
        { track_id: "c", kind: "channel", target: "b", mode: "keyframes", range: [0, 4],
          muted: false, keyframes: [
              { t: 0, value: { presence: 0, color: "#00ff00" }, ease: "linear" },
              { t: 2, value: { presence: 1, color: "#0000ff" }, ease: "linear" }] },
    ],
};
const plan = Anim.frames(project, 10);
check("frame count is round(duration x fps)", plan.length === 40, plan.length);
check("timestamps are k / fps", plan[7].timestampUs === 700000, plan[7].timestampUs);
const zs = plan.map((f) => Anim.evaluate(project, f.t).view.z);
check("acquired z is whole and monotone", zs.every((z, i) => Number.isInteger(z)
    && (i === 0 || z >= zs[i - 1])), zs);
check("the last frame shows the last plane", zs.at(-1) === 9, zs.at(-1));
const mid = Anim.evaluate(project, 1);
const channelB = mid.channels.find((c) => c.key === "b");
check("presence interpolates", near(channelB.presence, 0.5), channelB.presence);
check("colour is held until its keyframe", channelB.color === "#00ff00", channelB.color);
check("a moving presence is flagged as a display transition",
      mid.display_transition.includes("channel:b"), mid.display_transition);
check("colour changes at its keyframe",
      Anim.evaluate(project, 2).channels.find((c) => c.key === "b").color === "#0000ff");

// -- history --------------------------------------------------------------------------------
const history = History.create();
history.push({ label: "a", forward: [{ op: "x" }], inverse: [{ op: "y" }] });
const undone = history.undo();
check("undo returns the inverse", undone && undone.inverse[0].op === "y");
check("redo is available after undo", history.canRedo && !history.canUndo);
history.redo();
history.push({ label: "b", forward: [], inverse: [] });
check("a new edit clears redo", !history.canRedo && history.size === 2);

// -- parity with the Python presets ------------------------------------------------------------
const fixturePath = process.argv[2];
if (fixturePath) {
    const fixtures = JSON.parse(readFileSync(fixturePath, "utf8"));
    for (const fixture of fixtures) {
        const doc = fixture.document;
        const fps = fixture.fps;
        const frames = Anim.frames(doc, fps);
        check(`${fixture.name}: frame count matches Python`, frames.length === fixture.plan.length,
              [frames.length, fixture.plan.length]);
        let mismatch = null;
        frames.forEach((frame, k) => {
            if (mismatch) return;
            const ours = Anim.acquiredAt(doc, frame.t);
            const full = Anim.evaluate(doc, frame.t).view;
            const theirs = fixture.plan[k];
            const same = ours.t === theirs.t && ours.z === theirs.z && ours.mode === theirs.mode
                && JSON.stringify(ours.slab) === JSON.stringify(theirs.slab)
                && full.z === theirs.z && full.t === theirs.t;
            if (!same) mismatch = { k, ours, theirs };
        });
        check(`${fixture.name}: acquired data matches Python on every frame`, !mismatch, mismatch);
        if (fixture.document.base.camera3d && fixture.loop_check) {
            const n = frames.length;
            const first = Anim.evaluate(doc, 0).camera3d;
            const wrap = Anim.evaluate(doc, doc.duration_s).camera3d;
            const last = Anim.evaluate(doc, frames[n - 1].t).camera3d;
            check(`${fixture.name}: loop seam`, Interp.angleBetween(first.q, wrap.q) < 1e-6
                  && Interp.angleBetween(first.q, last.q) > 1e-4,
                  [Interp.angleBetween(first.q, wrap.q), Interp.angleBetween(first.q, last.q)]);
        }
    }
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
