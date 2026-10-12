/**
 * videoAnimation.js - what the viewer shows at time t, for a video document.
 *
 * `evaluate(project, t)` is the whole of the animation: a pure function from a
 * document and a content time to a FRAME STATE -- the plane, timepoint, view
 * mode and slab; the 3-D camera or the 2-D view; every channel's visibility,
 * presence, window and colour; the layers; the captions. Preview and export
 * both call it, so what plays is what exports; the export then applies each
 * frame's state to the viewer and waits for it to settle (videoReadiness.js).
 * Nothing here reads a clock.
 *
 * TRACKS COMPOSE IN ORDER. The base scene is the first frame; each track then
 * changes what it owns:
 *
 *   - a procedural track does nothing before its range starts, runs across its
 *     range, and holds its end after;
 *   - a keyframed track does nothing before its first keyframe and holds its
 *     last after it;
 *   - a camera move "from $current" starts from the pose the earlier tracks
 *     produce at the same moment, so moves chain (a turn, then a zoom from
 *     wherever the turn left the camera) and overlap (a turn while dollying).
 *
 * ACQUIRED DATA STEPS. Planes, timepoints, slabs and the view mode take the
 * value of the last keyframe at or before t, or a sweep's whole index -- never
 * a blend (server/schema.py says why; `acquiredAt` mirrors its acquired_at).
 *
 * Classic script: `window.PlexoraVideoAnimation`, and `module.exports`.
 */
(function (global) {
    "use strict";

    const need = (name, file) => global[name]
        || (typeof require === "function" ? require(file) : null);
    const Easing = need("PlexoraVideoEasing", "./videoEasing.js");
    const Interp = need("PlexoraVideoInterp", "./videoInterp.js");
    const Schema = need("PlexoraVideoSchema", "./videoSchema.js");
    const Camera3d = need("PlexoraVideoCamera3d", "./videoCamera3d.js");
    const Camera2d = need("PlexoraVideoCamera2d", "./videoCamera2d.js");

    const clone = Schema.clone;
    const clamp01 = (u) => (u <= 0 ? 0 : u >= 1 ? 1 : u);

    // -- keyframe fields ---------------------------------------------------------

    /** One field of a keyframed track at time t: undefined before the first
     *  keyframe that sets it; stepped or interpolated after. */
    function fieldAt(frames, field, t, mode) {
        const defining = frames.filter((f) => f.value && typeof f.value === "object"
            && f.value[field] !== undefined);
        if (!defining.length || t < defining[0].t - 1e-9) return { value: undefined, moving: false };
        let i = 0;
        while (i + 1 < defining.length && defining[i + 1].t <= t + 1e-9) i += 1;
        const a = defining[i];
        if (mode === "step" || i === defining.length - 1 || Easing.isStep(a.ease)) {
            return { value: clone(a.value[field]), moving: false };
        }
        const b = defining[i + 1];
        const start = a.t + (a.hold || 0);
        const span = b.t - start;
        const u = span > 0 ? clamp01((t - start) / span) : 1;
        const e = Easing.ease(a.ease, u);
        const from = a.value[field];
        const to = b.value[field];
        const moving = u > 0 && u < 1 && JSON.stringify(from) !== JSON.stringify(to);
        if (Array.isArray(from) && Array.isArray(to)) return { value: Interp.lerpVec(from, to, e), moving };
        if (typeof from === "number" && typeof to === "number") {
            return { value: Interp.lerp(from, to, e), moving };
        }
        return { value: clone(from), moving: false };
    }

    /** A whole-value step track's value at t (acquired_z/t, slab, view_mode). */
    function stepAt(frames, t, fallback) {
        if (!frames.length || t < frames[0].t - 1e-9) return fallback;
        let value = frames[0].value;
        for (const f of frames) {
            if (f.t <= t + 1e-9) value = f.value;
            else break;
        }
        return value === "$current" ? fallback : clone(value);
    }

    // -- cameras --------------------------------------------------------------------

    function cameraBefore(project, index, t, cache) {
        let camera = project.base.camera3d ? Camera3d.copy(project.base.camera3d) : null;
        for (let i = 0; i < index; i += 1) {
            const track = project.tracks[i];
            if (track.muted || track.kind !== "camera3d") continue;
            camera = camera3dTrack(project, track, i, t, camera, cache);
        }
        return camera;
    }

    function viewportBefore(project, index, t, cache) {
        let view = project.base.viewport ? Camera2d.copy(project.base.viewport) : null;
        for (let i = 0; i < index; i += 1) {
            const track = project.tracks[i];
            if (track.muted || track.kind !== "camera2d") continue;
            view = camera2dTrack(project, track, i, t, view, cache);
        }
        return view;
    }

    function resolvedFrames(project, track, index, cache, before) {
        const key = `frames:${track.track_id}`;
        if (cache && cache[key]) return cache[key];
        const frames = track.keyframes.map((kf) => ({
            t: kf.t, ease: kf.ease, hold: kf.hold || 0,
            value: kf.value === "$current" ? before(kf.t) : resolveTarget(project, kf.value),
        })).filter((f) => f.value);
        if (cache) cache[key] = frames;
        return frames;
    }

    /** A `{look_at: target}` camera keyframe made concrete from the project's
     *  resolved targets; any other value unchanged. */
    function resolveTarget(project, value) {
        if (!value || typeof value !== "object" || !value.look_at) return value;
        const target = project.targets?.[value.look_at];
        const resolved = target?.resolved;
        if (!resolved || !resolved.model) return null;
        const q = project.base.camera3d?.q || [1, 0, 0, 0];
        return Camera3d.lookAt(q, resolved.model, (resolved.radius || 0.15) * (value.fit || 1));
    }

    function camera3dTrack(project, track, index, t, camera, cache) {
        if (track.mode === "procedural") {
            const proc = track.procedural;
            if (!proc || t < track.range[0] - 1e-9) return camera;
            const u = clamp01(Schema.progress(track, t));
            const params = proc.params || {};
            const from = params.from && params.from !== "$current" ? params.from : camera;
            if (!from) return camera;
            return Camera3d.procedural(proc.generator, from, params, Easing.ease(proc.ease, u));
        }
        const frames = resolvedFrames(project, track, index, cache,
                                      (time) => cameraBefore(project, index, time, cache));
        if (!frames.length || t < frames[0].t - 1e-9) return camera;
        const pathCache = cache ? (cache[`path:${track.track_id}`] ||= {}) : null;
        return Camera3d.pathAt(frames, t, track.interp || {}, pathCache);
    }

    function camera2dTrack(project, track, index, t, view, cache) {
        if (track.mode === "procedural") {
            const proc = track.procedural;
            if (!proc || t < track.range[0] - 1e-9) return view;
            const params = proc.params || {};
            const from = params.from && params.from !== "$current" ? params.from : view;
            if (!from) return view;
            return Camera2d.pan(from, params, Easing.ease(proc.ease, clamp01(Schema.progress(track, t))));
        }
        const frames = resolvedFrames(project, track, index, cache,
                                      (time) => viewportBefore(project, index, time, cache));
        if (!frames.length || t < frames[0].t - 1e-9) return view;
        return Camera2d.pathAt(frames, t);
    }

    // -- acquired data ------------------------------------------------------------

    const ACQUIRED_KEY = { acquired_t: "t", acquired_z: "z", slab: "slab", view_mode: "mode" };

    function acquiredTrack(track, t, view) {
        const key = ACQUIRED_KEY[track.kind];
        if (track.mode === "procedural") {
            if (t < track.range[0] - 1e-9) return;
            const u = Schema.progress(track, t);
            const params = track.procedural.params;
            if (track.kind === "acquired_t" || track.kind === "acquired_z") {
                view[key] = Schema.sweepIndex(params, u);
            } else if (track.kind === "slab") {
                const edge = Schema.sweepIndex({ from: params.from, to: params.to }, u);
                view.slab = { z0: Math.min(params.from, edge), z1: Math.max(params.from, edge),
                              op: params.op || "max" };
            }
            return;
        }
        view[key] = stepAt(track.keyframes, t, view[key]);
    }

    /** Which acquired data the video shows at t -- server/schema.py acquired_at. */
    function acquiredAt(project, t) {
        const base = project.base.view;
        const view = { t: base.t, z: base.z, mode: base.mode, slab: clone(base.slab) };
        for (const track of project.tracks) {
            if (!track.muted && ACQUIRED_KEY[track.kind]) acquiredTrack(track, t, view);
        }
        return view;
    }

    // -- the frame ---------------------------------------------------------------------

    function clipTrack(project, track, t, view) {
        if (track.mode === "procedural") {
            if (t < track.range[0] - 1e-9) return false;
            const p = track.procedural.params;
            const u = Easing.ease(track.procedural.ease, clamp01(Schema.progress(track, t)));
            let edge = Interp.lerp(p.from, p.to, u);
            if (p.snap) {
                const planes = p.axis === "z" ? Math.max(1, project.source?.depth || 1)
                    : Math.max(1, (project.source?.image_size || [1, 1])[p.axis === "x" ? 0 : 1]);
                edge = Math.round(edge * planes) / planes;
            }
            const range = view.clip[p.axis].slice();
            if (p.side === "lo") range[0] = Math.min(edge, range[1]);
            else range[1] = Math.max(edge, range[0]);
            view.clip[p.axis] = range;
            return u > 0 && u < 1;
        }
        let moving = false;
        for (const axis of ["x", "y", "z"]) {
            const found = fieldAt(track.keyframes, axis, t, "lerp");
            if (found.value !== undefined) view.clip[axis] = found.value;
            moving = moving || found.moving;
        }
        return moving;
    }

    function channelTrack(track, t, channels) {
        let channel = channels.find((c) => c.key === track.target);
        if (!channel) {
            channel = { key: track.target, name: track.target, color: "#ffffff", window: null,
                        visible: false, opacity: 1, presence: 1 };
            channels.push(channel);
        }
        let moving = false;
        for (const [field, mode] of [["visible", "step"], ["color", "step"], ["presence", "lerp"],
                                     ["opacity", "lerp"], ["window", "lerp"]]) {
            const found = fieldAt(track.keyframes, field, t, mode);
            if (found.value !== undefined) channel[field] = found.value;
            moving = moving || found.moving;
        }
        return moving;
    }

    function layerTrack(track, t, list, idKey) {
        let layer = list.find((l) => l[idKey] === track.target);
        if (!layer) {
            layer = { [idKey]: track.target, visible: true, opacity: 1 };
            list.push(layer);
        }
        let moving = false;
        for (const [field, mode] of [["visible", "step"], ["mode", "step"], ["opacity", "lerp"]]) {
            const found = fieldAt(track.keyframes, field, t, mode);
            if (found.value !== undefined) layer[field] = found.value;
            moving = moving || found.moving;
        }
        return moving;
    }

    /**
     * The frame state at content time t. `options.cache` (an object kept for
     * one document revision) saves re-resolving keyframes on every frame.
     */
    function evaluate(project, t, options = {}) {
        const cache = options.cache || null;
        const base = project.base;
        const view = clone(base.view);
        const state = {
            t,
            view,
            dims: base.dims,
            box: clone(base.box),
            camera3d: base.camera3d ? Camera3d.copy(base.camera3d) : null,
            viewport: base.viewport ? Camera2d.copy(base.viewport) : null,
            channels: clone(base.channels || []),
            cell_layers: clone(base.cell_layers || []),
            layers: clone(base.layers || []),
            hd_mode: base.hd_mode,
            hud: { caption: "", opacity: 1 },
            transition: null,
            emphasis: null,
            display_transition: [],
        };
        project.tracks.forEach((track, index) => {
            if (track.muted) return;
            switch (track.kind) {
                case "camera3d":
                    state.camera3d = camera3dTrack(project, track, index, t, state.camera3d, cache);
                    break;
                case "camera2d":
                    state.viewport = camera2dTrack(project, track, index, t, state.viewport, cache);
                    break;
                case "acquired_t":
                case "acquired_z":
                case "slab":
                case "view_mode":
                    acquiredTrack(track, t, view);
                    break;
                case "clip":
                    if (clipTrack(project, track, t, view)) state.display_transition.push("clip");
                    break;
                case "channel":
                    if (channelTrack(track, t, state.channels)) {
                        state.display_transition.push(`channel:${track.target}`);
                    }
                    break;
                case "cell_layer":
                    if (layerTrack(track, t, state.cell_layers, "name")) {
                        state.display_transition.push(`cells:${track.target}`);
                    }
                    break;
                case "layer":
                    if (layerTrack(track, t, state.layers, "id")) {
                        state.display_transition.push(`layer:${track.target}`);
                    }
                    break;
                case "emphasis": {
                    const mode = fieldAt(track.keyframes, "mode", t, "step").value;
                    const strength = fieldAt(track.keyframes, "strength", t, "lerp");
                    if (mode !== undefined || strength.value !== undefined) {
                        const target = project.targets?.[track.target];
                        state.emphasis = { target: track.target, mode: mode || "dim_others",
                                           strength: strength.value ?? 0.6,
                                           bbox: target?.resolved?.bbox || null };
                        if (strength.moving) state.display_transition.push(`emphasis:${track.target}`);
                    }
                    break;
                }
                case "hud":
                    for (const field of Object.keys(track.keyframes[0]?.value || {})
                        .concat(["caption", "opacity", "title"])) {
                        const found = fieldAt(track.keyframes, field, t,
                                              field === "opacity" ? "lerp" : "step");
                        if (found.value !== undefined) state.hud[field] = found.value;
                    }
                    break;
                case "transition": {
                    const alpha = fieldAt(track.keyframes, "alpha", t, "lerp");
                    const kind = fieldAt(track.keyframes, "kind", t, "step").value;
                    if (alpha.value !== undefined) {
                        state.transition = { kind: kind || "fade_black", alpha: alpha.value };
                        if (alpha.moving) state.display_transition.push("transition");
                    }
                    break;
                }
                default:
                    break;
            }
        });
        if (view.mode === "volume" && !state.camera3d) {
            state.camera3d = { q: [1, 0, 0, 0], dist: 2.2, pan: [0, 0] };
        }
        return state;
    }

    /** Every frame of the video at `fps`: `{k, t (content s), timestampUs}`. */
    function frames(project, fps) {
        const n = Schema.frameCount(project.duration_s, fps);
        const out = new Array(n);
        for (let k = 0; k < n; k += 1) {
            out[k] = { k, t: Schema.contentTime(k, n, project.duration_s, project.loop),
                       timestampUs: Schema.timestampUs(k, fps) };
        }
        return out;
    }

    /** The content time for a normalised position p in [0, 1] of playback --
     *  what keeps preview playing from the same place when a parameter
     *  changes the duration. */
    function timeAt(project, p) {
        return clamp01(p) * project.duration_s;
    }

    const api = { evaluate, frames, acquiredAt, fieldAt, stepAt, timeAt, cameraBefore };
    global.PlexoraVideoAnimation = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
