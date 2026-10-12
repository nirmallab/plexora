/**
 * videoFigureBridge.js - Video Studio and Figure Builder, both ways.
 *
 * Neither plugin imports the other. They meet through what Figure Builder
 * already publishes:
 *
 *   - its scene-capture events: while Video Studio is open it answers
 *     `plexora:figure-capture-state` with the plane, timepoint and 3-D camera
 *     (which a Figure Builder scene has no fields for), and reads them back on
 *     `plexora:figure-restore-state`, so a panel captured during a session
 *     reopens on the same plane and angle;
 *   - its captures REST (`POST /plugins/figure_builder/api/captures`, then the
 *     preview): "Send frame to Figure Builder" puts the current frame in the
 *     captures bin, scene and raster together, exactly as its own shutter
 *     does;
 *   - a sessionStorage note (`plexora:video-studio-animate`) a figure can leave
 *     to open Video Studio on a panel's scene ("Animate in Video Studio"):
 *     read once when Video Studio opens.
 *
 * A 3-D frame sent to Figure Builder exports from its preview raster: Figure
 * Builder's server renderer has no 3-D. The capture's caption says so.
 *
 * Classic script: `window.PlexoraVideoFigureBridge`.
 */
(function (global) {
    "use strict";

    const ANIMATE_NOTE = "plexora:video-studio-animate";

    function attach(controller) {
        const onCapture = (event) => {
            const state = controller.currentFrameState?.();
            if (!state) return;
            event.detail?.contribute?.("video_studio", {
                version: 1,
                state: { view: state.view, camera3d: state.camera3d, t_seconds: state.t,
                         video_id: controller.saved?.videoId || null,
                         revision: controller.saved?.revision ?? null },
            });
        };
        const onRestore = (event) => {
            const part = event.detail?.plugins?.video_studio;
            if (!part || !part.state) return;
            const ok = controller.restoreFigurePart?.(part.state);
            event.detail?.report?.("video_studio", ok ? "ok" : "partial");
        };
        global.addEventListener("plexora:figure-capture-state", onCapture);
        global.addEventListener("plexora:figure-restore-state", onRestore);
        return () => {
            global.removeEventListener("plexora:figure-capture-state", onCapture);
            global.removeEventListener("plexora:figure-restore-state", onRestore);
        };
    }

    /** The note a figure left, once (it is removed as it is read). */
    function takeAnimateNote(datasource) {
        let raw = null;
        try {
            raw = global.sessionStorage?.getItem(ANIMATE_NOTE);
            if (raw) global.sessionStorage.removeItem(ANIMATE_NOTE);
        } catch (error) {
            return null;
        }
        if (!raw) return null;
        try {
            const note = JSON.parse(raw);
            if (note?.source && note.source !== datasource && note.datasource !== datasource) return null;
            return note && note.scene ? note : null;
        } catch (error) {
            return null;
        }
    }

    /** Put a frame in Figure Builder's captures bin. Resolves the capture id. */
    async function sendFrame(env, state, canvas, { videoId = null, revision = null, aspect = 16 / 9 } = {}) {
        const base = `${global.PLEXORA_BASE_URL || ""}/plugins/figure_builder/api`;
        const scene = global.PlexoraVideoSceneState.toFigureScene(env, state,
                                                                  { video_id: videoId, revision, aspect });
        const facts = env.facts || {};
        const captureId = `cap_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
        const volume = state.view.mode === "volume";
        const response = await fetch(`${base}/captures`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                capture_id: captureId,
                datasource: env.datasource,
                source: { kind: "plexora_project", datasource: env.datasource,
                          display_name: env.datasource,
                          image: { width: facts.image_size?.[0] || 0, height: facts.image_size?.[1] || 0 },
                          channels: (facts.channels || []).map((c) => ({ key: c.key,
                                                                         fullname_at_capture: c.name })),
                          pixel_size: facts.pixel_size ? { value: facts.pixel_size, unit: "µm" } : null },
                scene,
                caption: volume ? "3-D frame from Video Studio (exports from this preview)"
                    : "Frame from Video Studio",
            }),
        });
        if (response.status === 404) throw new Error("Figure Builder is not installed here.");
        if (!response.ok) throw new Error(`Figure Builder refused the frame (${response.status}).`);
        if (canvas) {
            const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/webp", 0.92));
            if (blob) {
                await fetch(`${base}/captures/${encodeURIComponent(captureId)}/preview?width=${canvas.width}`
                            + `&height=${canvas.height}`, {
                    method: "POST", headers: { "Content-Type": blob.type || "image/webp" }, body: blob });
            }
        }
        return captureId;
    }

    global.PlexoraVideoFigureBridge = { attach, takeAnimateNote, sendFrame, ANIMATE_NOTE };
})(typeof window !== "undefined" ? window : globalThis);
