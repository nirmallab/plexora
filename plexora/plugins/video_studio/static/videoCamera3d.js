/**
 * videoCamera3d.js - camera moves for the 3-D view, in the renderer's own terms.
 *
 * THE CAMERA is volumeRenderer.js's `{q, dist, panX, panY}`, stored here as
 * `{q, dist, pan: [x, y]}`: q turns the image's axes to the screen's, the
 * stack is shifted by the pan and pushed `dist` back. A point p of the box's
 * model space (the box centred on the origin, its longest side 1, x along a
 * row, y down the rows, z deeper into the stack) is at
 *
 *     view(p) = R(q) p - (panX, panY, dist)
 *
 * in front of the camera (volumeRenderer.js cameraMatrices). Everything here
 * produces poses the renderer draws exactly as written; there is no
 * eye/target/up conversion to drift.
 *
 * MOVES ARE PROCEDURAL where that is what keeps them exact:
 *
 *   orbit    q turned by theta about an axis -- the stack's own (x, y, z) or the
 *            screen's (screen_x, screen_y). A 360° loop samples
 *            theta_k = 2 pi k / N for k in [0, N): frame N would be frame 0, so
 *            the loop has no seam. Two keyframes cannot describe a full turn
 *            (slerp takes the short way); a generator can.
 *   rock     theta = A sin(2 pi cycles u): back and forth, seamless.
 *   tumble   a turn about the vertical with a tilt that rises and falls once
 *            per turn: both periodic in u, so it loops.
 *   dolly    the distance from where it is to `to_dist` (log scale), with an
 *            optional turn: a fly-in.
 *
 * With a PIVOT (a model-space point), the pan and distance are corrected every
 * frame so the pivot keeps its exact screen position and depth while the
 * stack turns about it.
 *
 * Classic script: `window.PlexoraVideoCamera3d`, and `module.exports`.
 */
(function (global) {
    "use strict";

    const Interp = global.PlexoraVideoInterp
        || (typeof require === "function" ? require("./videoInterp.js") : null);
    const Easing = global.PlexoraVideoEasing
        || (typeof require === "function" ? require("./videoEasing.js") : null);

    const IMAGE_AXES = { x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1] };
    const SCREEN_AXES = { screen_x: [1, 0, 0], screen_y: [0, 1, 0] };
    //: The renderer's vertical field of view and depth range (volumeRenderer.js).
    const FOV_Y = 0.7;
    const NEAR = 0.01;
    const FAR = 20;

    const TAU = 2 * Math.PI;
    const DEG = Math.PI / 180;

    function toRenderer(camera) {
        return { q: camera.q.slice(), dist: camera.dist,
                 panX: camera.pan ? camera.pan[0] : camera.panX || 0,
                 panY: camera.pan ? camera.pan[1] : camera.panY || 0 };
    }

    function fromRenderer(camera) {
        return { q: Array.from(camera.q), dist: camera.dist,
                 pan: [camera.panX || 0, camera.panY || 0] };
    }

    function copy(camera) {
        return { q: camera.q.slice(), dist: camera.dist, pan: camera.pan.slice(),
                 ...(camera.fov ? { fov: camera.fov } : {}) };
    }

    /** q turned by `angle` about `axis` ("x" | "y" | "z" image axes, or
     *  "screen_x" | "screen_y"). */
    function turn(q, axis, angle) {
        if (!angle) return q.slice();
        if (SCREEN_AXES[axis]) {
            return Interp.normalize(Interp.multiply(Interp.axisAngle(SCREEN_AXES[axis], angle), q));
        }
        return Interp.normalize(Interp.multiply(q, Interp.axisAngle(IMAGE_AXES[axis] || IMAGE_AXES.y,
                                                                     angle)));
    }

    /** The pose `camera` turned to `q`, with the pan and distance moved so the
     *  model point `pivot` stays exactly where it was on screen. */
    function keepPivot(camera, q, pivot) {
        if (!pivot) return { ...copy(camera), q };
        const before = Interp.rotate(camera.q, pivot);
        const after = Interp.rotate(q, pivot);
        return {
            ...copy(camera), q,
            pan: [camera.pan[0] + after[0] - before[0], camera.pan[1] + after[1] - before[1]],
            dist: camera.dist + after[2] - before[2],
        };
    }

    function orbit(camera, params, u) {
        const theta = TAU * (params.turns || 1) * (params.direction < 0 ? -1 : 1) * u;
        return keepPivot(camera, turn(camera.q, params.axis, theta), params.pivot || null);
    }

    function rock(camera, params, u) {
        const amplitude = (params.amplitude_deg || 30) * DEG;
        const theta = amplitude * (params.direction < 0 ? -1 : 1)
            * Math.sin(TAU * (params.cycles || 1) * u);
        return keepPivot(camera, turn(camera.q, params.axis, theta), params.pivot || null);
    }

    function tumble(camera, params, u) {
        const spin = TAU * Math.max(1, Math.round(params.turns || 1)) * u;
        const tilt = (params.tilt_deg || 25) * DEG * Math.sin(TAU * u);
        const q = turn(turn(camera.q, "screen_y", spin), "screen_x", tilt);
        return keepPivot(camera, q, params.pivot || null);
    }

    function dolly(camera, params, u) {
        const dist = Interp.logLerp(camera.dist, params.to_dist || camera.dist, u);
        const q = turn(camera.q, params.axis || "screen_y", (params.turn_deg || 0) * DEG * u);
        return { ...copy(camera), q, dist };
    }

    const GENERATORS = { orbit, rock, tumble, dolly };

    /** A procedural move's pose at progress u (already eased) from `camera`. */
    function procedural(generator, camera, params, u) {
        const fn = GENERATORS[generator];
        return fn ? fn(camera, params || {}, u) : copy(camera);
    }

    // -- keyframed paths ------------------------------------------------------

    /** The camera's position in model space: where the eye is. */
    function eye(camera) {
        const back = Interp.conjugate(camera.q);
        return Interp.rotate(back, [camera.pan[0], camera.pan[1], camera.dist]);
    }

    /**
     * A pose on the path through `frames` (`[{t, value: camera, ease}]`, values
     * resolved). Orientation: slerp between two, squad through three or more,
     * over the unwrapped list. Position (pan, log distance): linear, or
     * centripetal Catmull-Rom with `path: "catmull"`. With `speed: "arc"`,
     * time maps to distance travelled by the eye, so the camera moves at
     * constant speed whatever the keyframe spacing.
     */
    function pathAt(frames, t, interp = {}, cache = null) {
        if (!frames.length) return null;
        if (frames.length === 1 || t <= frames[0].t) return copy(frames[0].value);
        const last = frames[frames.length - 1];
        if (t >= last.t) return copy(last.value);
        const quats = (cache && cache.quats) || Interp.unwrapQuats(frames.map((f) => f.value.q));
        if (cache) cache.quats = quats;
        const points = frames.map((f) => [f.value.pan[0], f.value.pan[1], Math.log(f.value.dist)]);

        let index;
        let local;
        if (interp.speed === "arc" && frames.length > 1) {
            const arc = (cache && cache.arc) || arcTable(frames, quats, points, interp);
            if (cache) cache.arc = arc;
            const span = last.t - frames[0].t;
            const fraction = span > 0 ? (t - frames[0].t) / span : 1;
            const s = arc.at(Easing.ease(frames[0].ease, fraction));
            const scaled = s * (frames.length - 1);
            index = Math.min(frames.length - 2, Math.floor(scaled));
            local = scaled - index;
        } else {
            index = frames.findIndex((f, i) => i + 1 < frames.length && t < frames[i + 1].t);
            if (index < 0) index = frames.length - 2;
            const a = frames[index];
            const b = frames[index + 1];
            const start = a.t + (a.hold || 0);
            const span = b.t - start;
            const raw = span > 0 ? (t - start) / span : 1;
            local = Easing.ease(a.ease, raw);
        }
        return segment(frames, quats, points, index, local, interp);
    }

    function segment(frames, quats, points, index, local, interp) {
        const q = interp.orientation === "slerp" || frames.length < 3
            ? Interp.slerpRaw(quats[index], quats[index + 1], local)
            : Interp.squad(quats, index, local);
        const point = interp.path === "catmull" && frames.length > 2
            ? Interp.catmullAt(points, index, local)
            : Interp.lerpVec(points[index], points[index + 1], local);
        return { q, dist: Math.exp(point[2]), pan: [point[0], point[1]] };
    }

    function arcTable(frames, quats, points, interp) {
        const ArcLength = global.PlexoraVideoArcLength
            || (typeof require === "function" ? require("./videoArcLength.js") : null);
        const n = frames.length - 1;
        return ArcLength.table((s) => {
            const scaled = s * n;
            const index = Math.min(n - 1, Math.floor(scaled));
            const pose = segment(frames, quats, points, index, scaled - index, interp);
            // The eye's position, plus the orientation scaled to a comparable
            // length, so a turn in place still takes time.
            const e = eye(pose);
            return [e[0], e[1], e[2], pose.q[0] * 0.5, pose.q[1] * 0.5, pose.q[2] * 0.5,
                    pose.q[3] * 0.5];
        }, 128 * n);
    }

    // -- framing -------------------------------------------------------------------

    /** The box's physical extent, longest side 1 -- volumeRenderer.js extent(),
     *  from the image's facts: `{image_size: [w, h], depth, spacing}` and the
     *  area shown in 3-D (`box`, full-resolution pixels) if any. */
    function extent(facts, box) {
        const [width, height] = facts.image_size || [1, 1];
        const sx = Number(facts.spacing?.x?.value) || 1;
        const sz = Number(facts.spacing?.z?.value) || sx;
        const planes = Math.max(1, Number(facts.depth) || 1);
        const across = box ? [box.width, box.height] : [width || 1, height || 1];
        const physical = [across[0] * sx, across[1] * sx, planes * sz];
        const longest = Math.max(...physical) || 1;
        return physical.map((side) => side / longest);
    }

    /** Full-resolution image coordinates (x, y in pixels, z a plane index) to
     *  the box's model space, as the shader maps them (`world / u_extent + 0.5`
     *  is the texture coordinate). */
    function modelPoint(facts, box, x, y, z) {
        const e = extent(facts, box);
        const [width, height] = facts.image_size || [1, 1];
        const x0 = box ? box.x : 0;
        const y0 = box ? box.y : 0;
        const w = box ? box.width : width || 1;
        const h = box ? box.height : height || 1;
        const planes = Math.max(1, Number(facts.depth) || 1);
        return [((x - x0) / w - 0.5) * e[0], ((y - y0) / h - 0.5) * e[1],
                ((z + 0.5) / planes - 0.5) * e[2]];
    }

    /** A pose looking at model point `target` from orientation `q`, far enough
     *  back that a sphere of radius `radius` fills `fill` of the view height. */
    function lookAt(q, target, radius = 0.25, fill = 0.8, fovY = FOV_Y) {
        const r = Interp.rotate(q, target);
        const distance = radius / Math.max(1e-3, Math.tan((fovY * fill) / 2));
        return { q: q.slice(), pan: [r[0], r[1]], dist: r[2] + distance };
    }

    // -- projection (for the path editor and the probes) --------------------

    function mat4Multiply(a, b) {
        const out = new Float64Array(16);
        for (let col = 0; col < 4; col += 1) {
            for (let row = 0; row < 4; row += 1) {
                let sum = 0;
                for (let k = 0; k < 4; k += 1) sum += a[k * 4 + row] * b[col * 4 + k];
                out[col * 4 + row] = sum;
            }
        }
        return out;
    }

    function toMat4(q) {
        const [x, y, z, w] = q;
        return new Float64Array([
            1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0,
            2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0,
            2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0,
            0, 0, 0, 1]);
    }

    /** volumeRenderer.js cameraMatrices().forward for `camera` at `aspect`. */
    function forward(camera, aspect, fovY = FOV_Y) {
        const shift = new Float64Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0,
                                        -camera.pan[0], -camera.pan[1], -camera.dist, 1]);
        const view = mat4Multiply(shift, toMat4(camera.q));
        const f = 1 / Math.tan(fovY / 2);
        const nf = 1 / (NEAR - FAR);
        const projection = new Float64Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0,
                                             (FAR + NEAR) * nf, -1, 0, 0, 2 * FAR * NEAR * nf, 0]);
        return mat4Multiply(projection, view);
    }

    /** Model point -> normalised device coordinates `[x, y, depth]` (y up). */
    function project(camera, point, aspect, fovY = FOV_Y) {
        const m = forward(camera, aspect, fovY);
        const [x, y, z] = point;
        const cx = m[0] * x + m[4] * y + m[8] * z + m[12];
        const cy = m[1] * x + m[5] * y + m[9] * z + m[13];
        const cz = m[2] * x + m[6] * y + m[10] * z + m[14];
        const cw = m[3] * x + m[7] * y + m[11] * z + m[15];
        return [cx / cw, cy / cw, cz / cw];
    }

    const api = {
        IMAGE_AXES, SCREEN_AXES, FOV_Y, GENERATORS, turn, keepPivot, orbit, rock, tumble, dolly,
        procedural, pathAt, eye, extent, modelPoint, lookAt, forward, project, copy, toRenderer,
        fromRenderer,
    };
    global.PlexoraVideoCamera3d = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
