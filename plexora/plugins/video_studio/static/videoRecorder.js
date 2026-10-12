/**
 * videoRecorder.js - record an exploration, keep it as an editable video.
 *
 * A screen recording is pixels and cannot be edited; this records what the
 * user DID -- where the view went, which plane and timepoint, which channels --
 * as a stream of viewer states, and turns it into keyframed tracks. The result
 * opens as an ordinary video: re-timed, smoothed, extended, exported at any
 * resolution, the camera path editable like any other.
 *
 * Sampling: the 2-D view on every viewport change (at most 10 per second), the
 * 3-D camera at 10 Hz, plane / timepoint / mode on every committed view
 * change, channels when their state differs. The raw samples are kept with the
 * video (`recording.raw`), so the cleanup passes can be re-run differently:
 *
 *   simplify   Ramer-Douglas-Peucker on the camera path, in (cx, cy, log w)
 *              for 2-D and (eye, orientation) for 3-D: fewer, well-placed
 *              keyframes along the same route
 *   dedupe     a state that repeats the one before is dropped
 *   retime     an optional speed factor; pauses longer than `maxPause` are
 *              shortened (an exploration has long looks that a video does not
 *              need)
 *
 * Acquired planes and timepoints become step tracks: a recording that paused
 * on plane 40 shows plane 40, never "plane 39.6".
 *
 * Classic script: `window.PlexoraVideoRecorder`.
 */
(function (global) {
    "use strict";

    const SAMPLE_MS = 100;

    function rdp(points, epsilon, distance) {
        if (points.length < 3) return points.slice();
        const first = points[0];
        const last = points[points.length - 1];
        let worst = 0;
        let index = 0;
        for (let i = 1; i < points.length - 1; i += 1) {
            const d = distance(points[i], first, last);
            if (d > worst) {
                worst = d;
                index = i;
            }
        }
        if (worst <= epsilon) return [first, last];
        const left = rdp(points.slice(0, index + 1), epsilon, distance);
        const right = rdp(points.slice(index), epsilon, distance);
        return left.slice(0, -1).concat(right);
    }

    /** Distance of sample p from the straight interpolation between a and b
     *  at p's time, in the sample's own coordinates. */
    function interpolatedDistance(p, a, b) {
        const span = b.t - a.t;
        const u = span > 0 ? (p.t - a.t) / span : 0;
        let sum = 0;
        for (let k = 0; k < p.v.length; k += 1) {
            const expected = a.v[k] + (b.v[k] - a.v[k]) * u;
            sum += (p.v[k] - expected) ** 2;
        }
        return Math.sqrt(sum);
    }

    function vector2d(view) {
        return [view.cx / view.w, view.cy / view.w, Math.log(view.w)];
    }

    function vector3d(camera) {
        const eye = global.PlexoraVideoCamera3d.eye(camera);
        return [eye[0], eye[1], eye[2], camera.q[0], camera.q[1], camera.q[2], camera.q[3]];
    }

    function retime(samples, { speed = 1, maxPause = 1.5 } = {}) {
        if (!samples.length) return samples;
        const out = [];
        let shift = 0;
        let previous = samples[0];
        for (const sample of samples) {
            const gap = sample.t - previous.t;
            if (gap > maxPause && sample !== previous) shift += gap - maxPause;
            out.push({ ...sample, t: (sample.t - samples[0].t - shift) / speed });
            previous = sample;
        }
        return out;
    }

    /**
     * Raw recording -> tracks. `options`: `epsilon2d` (default 0.004),
     * `epsilon3d` (0.01), `speed`, `maxPause`.
     */
    function toTracks(raw, options = {}) {
        const views = retime(raw.views || [], options);
        const cameras = retime(raw.cameras || [], options);
        const acquired = retime(raw.acquired || [], options);
        const tracks = [];
        if (views.length > 1) {
            const kept = rdp(views.map((s) => ({ ...s, v: vector2d(s.view) })), options.epsilon2d ?? 0.004,
                             interpolatedDistance);
            tracks.push({ kind: "camera2d", mode: "keyframes", label: "Recorded view",
                          interp: { orientation: "slerp", path: "catmull", speed: "time" },
                          keyframes: kept.map((s) => ({ t: s.t, value: s.view, ease: "linear" })) });
        }
        if (cameras.length > 1) {
            const kept = rdp(cameras.map((s) => ({ ...s, v: vector3d(s.camera) })), options.epsilon3d ?? 0.01,
                             interpolatedDistance);
            tracks.push({ kind: "camera3d", mode: "keyframes", label: "Recorded camera",
                          interp: { orientation: "squad", path: "catmull", speed: "time" },
                          keyframes: kept.map((s) => ({ t: s.t, value: s.camera, ease: "linear" })) });
        }
        for (const key of ["z", "t", "mode"]) {
            const kind = key === "z" ? "acquired_z" : key === "t" ? "acquired_t" : "view_mode";
            const steps = [];
            for (const sample of acquired) {
                if (!steps.length || steps[steps.length - 1].value !== sample[key]) {
                    steps.push({ t: sample.t, value: sample[key], ease: "step" });
                }
            }
            if (steps.length > 1) tracks.push({ kind, mode: "keyframes", label: "Recorded", keyframes: steps });
        }
        const end = Math.max(0.5, ...[views, cameras, acquired].map((list) => (list.length ? list[list.length - 1].t : 0)));
        return { tracks, duration_s: Math.round(end * 100) / 100 };
    }

    function create(controller) {
        const env = controller.env;
        let recording = null;

        function now() {
            return (global.performance?.now?.() ?? Date.now()) / 1000;
        }

        function start() {
            if (recording) return;
            const scene = global.PlexoraVideoSceneState.capture(env);
            const started = now();
            const raw = { started_at: new Date().toISOString(), views: [], cameras: [], acquired: [] };
            const push = (list, sample) => list.push({ t: now() - started, ...sample });
            push(raw.acquired, { z: scene.view.z, t: scene.view.t, mode: scene.view.mode });
            if (scene.viewport) push(raw.views, { view: scene.viewport });
            if (scene.camera3d) push(raw.cameras, { camera: scene.camera3d });
            let lastView = 0;
            const offViewport = env.ctx?.layers?.onViewportChange?.(() => {
                const t = now();
                if (t - lastView < SAMPLE_MS / 1000) return;
                lastView = t;
                const view = global.PlexoraVideoSceneState.capture(env).viewport;
                if (view) push(raw.views, { view });
            });
            const onView = () => {
                const v = global.PlexoraVideoSceneState.capture(env).view;
                push(raw.acquired, { z: v.z, t: v.t, mode: v.mode });
            };
            global.addEventListener("plexora:view-changed", onView);
            let lastCamera = null;
            const timer = setInterval(() => {
                const camera = env.renderer?.camera;
                if (!camera || env.vs?.get?.().mode !== "volume") return;
                const pose = global.PlexoraVideoCamera3d.fromRenderer(camera);
                const key = JSON.stringify(pose);
                if (key === lastCamera) return;
                lastCamera = key;
                push(raw.cameras, { camera: pose });
            }, SAMPLE_MS);
            recording = { raw, base: scene, stop() {
                offViewport?.();
                global.removeEventListener("plexora:view-changed", onView);
                clearInterval(timer);
            } };
        }

        /** Stop, and return a project body for `POST /api/videos`. */
        function stop(options = {}) {
            if (!recording) return null;
            recording.stop();
            const { raw, base } = recording;
            recording = null;
            const { tracks, duration_s: duration } = toTracks(raw, options);
            return {
                title: `Recording · ${new Date().toLocaleString()}`,
                source: controller.project?.source || { datasource: env.datasource, ...controller.facts },
                base, duration_s: duration, loop: false, tracks,
                recording: { raw, options },
                provenance: { created_by: "recording" },
            };
        }

        return { start, stop, get active() { return Boolean(recording); } };
    }

    global.PlexoraVideoRecorder = { create, toTracks, rdp, retime };
    if (typeof module !== "undefined" && module.exports) module.exports = global.PlexoraVideoRecorder;
})(typeof window !== "undefined" ? window : globalThis);
