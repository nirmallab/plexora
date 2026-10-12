/**
 * videoSchema.js - the client's copy of what a video document may say.
 *
 * The server normalises every document it stores (server/schema.py) and is the
 * authority; this mirrors the parts the browser needs before a round trip:
 * the track kinds (which change in whole steps, which may ease), the frame
 * arithmetic, and the sweep that maps time to an acquired index. A probe
 * (tests/js/video_studio_project_probe.mjs) holds the frame arithmetic and the
 * sweep to the Python on the same fixtures.
 *
 * Classic script: `window.PlexoraVideoSchema`, and `module.exports`.
 */
(function (global) {
    "use strict";

    const SCHEMA_VERSION = 1;

    const TRACK_KINDS = {
        camera3d: { class: "camera", interp: "smooth", label: "Camera" },
        camera2d: { class: "camera", interp: "smooth", label: "View" },
        acquired_t: { class: "acquired_time", interp: "step", label: "Time" },
        acquired_z: { class: "slice", interp: "step", label: "Plane" },
        slab: { class: "slice", interp: "step", label: "Slab" },
        view_mode: { class: "slice", interp: "step", label: "Mode" },
        clip: { class: "slice", interp: "smooth", label: "Clip" },
        channel: { class: "visualization", interp: "smooth", label: "Channel" },
        cell_layer: { class: "visualization", interp: "smooth", label: "Cells" },
        layer: { class: "visualization", interp: "smooth", label: "Layer" },
        emphasis: { class: "visualization", interp: "smooth", label: "Emphasis" },
        hud: { class: "annotation", interp: "smooth", label: "Captions" },
        transition: { class: "display_transition", interp: "smooth", label: "Transition" },
    };

    const STEP_KINDS = Object.keys(TRACK_KINDS).filter((k) => TRACK_KINDS[k].interp === "step");
    const CHANNEL_STEP_FIELDS = ["visible", "color"];

    const isStepKind = (kind) => STEP_KINDS.includes(kind);

    function frameCount(duration, fps) {
        return Math.max(1, Math.round(Number(duration) * Number(fps)));
    }

    /** The animation time frame k of n shows: a loop never renders frame n
     *  (it would equal frame 0); a non-loop shows both ends exactly. */
    function contentTime(k, n, duration, loop) {
        if (n <= 1) return 0;
        return (k * duration) / (loop ? n : n - 1);
    }

    /** Frame k's presentation timestamp, microseconds. */
    function timestampUs(k, fps) {
        return Math.round((k * 1e6) / fps);
    }

    /** The acquired index a sweep shows at progress u (server/schema.py
     *  sweep_index): every index gets an equal share; integer always. */
    function sweepIndex(params, u) {
        const start = Math.round(params.from);
        const end = Math.round(params.to);
        const step = end >= start ? 1 : -1;
        let run = [];
        for (let i = start; step > 0 ? i <= end : i >= end; i += step) run.push(i);
        if (params.bounce && run.length > 1) run = run.concat(run.slice(1, -1).reverse());
        const repeat = Math.max(1, Math.floor(params.repeat || 1));
        if (repeat > 1) run = [].concat(...Array(repeat).fill(run));
        const clamped = Math.min(1, Math.max(0, u));
        // The epsilon absorbs float error (frame 9 of 12 is 8.999999999999998
        // planes in): a sweep at one frame per plane must show every plane.
        return run[Math.min(run.length - 1, Math.floor(clamped * run.length + 1e-9))];
    }

    function progress(track, t) {
        const [start, end] = track.range || [0, 0];
        if (end <= start) return t >= start ? 1 : 0;
        return (t - start) / (end - start);
    }

    /** A short id, the server's shape (`kf_1a2b3c4d`). */
    function newId(prefix) {
        const bytes = new Uint8Array(4);
        (global.crypto || {}).getRandomValues?.(bytes);
        const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")
            || Math.random().toString(16).slice(2, 10);
        return `${prefix}_${hex}`;
    }

    /** Deep copy for plain JSON values. */
    const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

    const api = {
        SCHEMA_VERSION, TRACK_KINDS, STEP_KINDS, CHANNEL_STEP_FIELDS, isStepKind, frameCount,
        contentTime, timestampUs, sweepIndex, progress, newId, clone,
    };
    global.PlexoraVideoSchema = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
