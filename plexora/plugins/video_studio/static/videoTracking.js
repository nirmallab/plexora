/**
 * videoTracking.js - pointing the camera at something: a region, an ROI.
 *
 * "Focus on…" in the path editor frames a target so the next camera position
 * can be added there: in 2-D the view fitted to the target's bounding box with
 * a margin; in 3-D the current orientation kept and the camera moved to look
 * at the target's centre from far enough back that it fills most of the view.
 *
 * Targets come from the ROI tool's own REST (`/plugins/roi/api/state`, read
 * from this plugin's JavaScript; no Python import between plugins), whose
 * regions are polygons in full-resolution image pixels, plus "the region on
 * screen". A target is stored in the document by id with its resolved box, so
 * a video keeps working if the ROI is later edited, and says so when it is
 * gone (`status: "missing"`).
 *
 * Classic script: `window.PlexoraVideoTracking`.
 */
(function (global) {
    "use strict";

    const MARGIN = 1.15;

    function bboxOf(geometry) {
        const coords = [];
        const walk = (value) => {
            if (!Array.isArray(value)) return;
            if (typeof value[0] === "number" && typeof value[1] === "number") coords.push(value);
            else value.forEach(walk);
        };
        walk(geometry?.coordinates);
        if (!coords.length) return null;
        const xs = coords.map((c) => c[0]);
        const ys = coords.map((c) => c[1]);
        const x = Math.min(...xs);
        const y = Math.min(...ys);
        return [x, y, Math.max(...xs) - x, Math.max(...ys) - y];
    }

    /** The ROI tool's regions for this image: `[{kind, id, label, bbox, z}]`,
     *  or [] when the ROI tool is not installed or has none. */
    async function listRois(env) {
        const base = global.PLEXORA_BASE_URL || "";
        try {
            const response = await fetch(`${base}/plugins/roi/api/state?datasource=${encodeURIComponent(env.datasource)}`);
            if (!response.ok) return [];
            const body = await response.json();
            return (body.features || []).map((feature) => {
                const props = feature.properties || {};
                const bbox = bboxOf(feature.geometry);
                if (!bbox) return null;
                const planes = props.planes || props.z_range || null;
                return { kind: "roi", id: String(feature.id ?? props.id ?? ""), label: props.name || props.category || "Region",
                         bbox, z: Array.isArray(planes) && planes.length === 2 ? planes : null };
            }).filter((t) => t && t.id);
        } catch (error) {
            return [];
        }
    }

    /** Resolve a target to `{bbox, model, radius}` for this image. */
    function resolve(facts, target, box = null) {
        const Camera3d = global.PlexoraVideoCamera3d;
        const [x, y, w, h] = target.bbox;
        const depth = Math.max(1, facts.depth || 1);
        const z = target.z ? (target.z[0] + target.z[1]) / 2 : (depth - 1) / 2;
        const model = Camera3d.modelPoint(facts, box, x + w / 2, y + h / 2, z);
        const corner = Camera3d.modelPoint(facts, box, x, y, target.z ? target.z[0] : 0);
        const radius = Math.max(1e-3, Math.hypot(model[0] - corner[0], model[1] - corner[1],
                                                 target.z ? model[2] - corner[2] : 0));
        return { bbox: target.bbox, model, radius };
    }

    /** The pose that frames `target`: `{camera3d}` in 3-D, `{viewport}` in 2-D. */
    function framing(env, facts, target, aspect) {
        const volume = env.vs?.get?.().mode === "volume";
        const resolved = resolve(facts, target, env.renderer?.box || null);
        if (volume && env.renderer) {
            const live = global.PlexoraVideoCamera3d.fromRenderer(env.renderer.camera);
            return { camera3d: global.PlexoraVideoCamera3d.lookAt(live.q, resolved.model, resolved.radius * MARGIN),
                     resolved };
        }
        const [x, y, w, h] = target.bbox;
        const pad = { x: x - w * (MARGIN - 1) / 2, y: y - h * (MARGIN - 1) / 2, width: w * MARGIN, height: h * MARGIN };
        const transform = env.iv?.viewTransform?.get?.() || {};
        return { viewport: global.PlexoraVideoCamera2d.fit(pad, aspect, { degrees: transform.degrees || 0,
                                                                           flip_h: transform.flipH,
                                                                           flip_v: transform.flipV }),
                 resolved };
    }

    /** Move the live viewer to frame `target` (so a position can be added). */
    function focus(env, facts, target, aspect) {
        const pose = framing(env, facts, target, aspect);
        if (pose.camera3d) global.PlexoraVideoSceneState.applyCamera(env, pose.camera3d);
        else if (pose.viewport) global.PlexoraVideoSceneState.applyViewport(env, pose.viewport, aspect);
        return pose;
    }

    global.PlexoraVideoTracking = { listRois, resolve, framing, focus, bboxOf };
})(typeof window !== "undefined" ? window : globalThis);
