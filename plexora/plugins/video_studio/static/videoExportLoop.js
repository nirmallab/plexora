/**
 * videoExportLoop.js - render every frame on a fixed clock, encode, save.
 *
 * Deterministic and offline: frame k shows `evaluate(project, content_time(k))`
 * and is presented at k / fps, whatever the machine's speed. Nothing here
 * reads a wall clock to decide what a frame shows; the clock only measures
 * how long rendering took.
 *
 *   plan     every frame state up front (videoAnimation.js)
 *   open     the render session (videoRenderCoordinator.js)
 *   prime    exact windows, prepared sections (videoPrefetch.js)
 *   for k:   stop? -> wait while the tab is hidden -> warm frames k+1, k+2 ->
 *            apply + readiness barrier + compose -> encoder.push (waits for the
 *            encoder when it is behind) -> progress
 *   finish   the encoder writes the file; the manifest goes with it
 *   close    the viewer exactly as it was -- always, in `finally`
 *
 * A frame the viewer cannot settle within the timeout is retried once and then
 * the export stops, naming what it waited on. No unresolved frame is ever
 * encoded.
 *
 * Classic script: `window.PlexoraVideoExportLoop`.
 */
(function (global) {
    "use strict";

    const now = () => (global.performance?.now?.() ?? Date.now());
    const yieldToUi = () => new Promise((resolve) => setTimeout(resolve, 0));

    function whenVisible(signal) {
        if (!document.hidden) return Promise.resolve();
        return new Promise((resolve) => {
            const check = () => {
                if (!document.hidden || signal?.aborted) {
                    document.removeEventListener("visibilitychange", check);
                    resolve();
                }
            };
            document.addEventListener("visibilitychange", check);
        });
    }

    const EXTENSIONS = { mp4: "mp4", webm: "webm", mov: "mov", gif: "gif", png: "zip" };

    function fileName(project, settings) {
        const stem = String(project.title || "video").normalize("NFKD")
            .replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-").slice(0, 80) || "video";
        return `${stem}-${settings.width}x${settings.height}-${settings.fps}fps.${EXTENSIONS[settings.format] || "mp4"}`;
    }

    /** A blob onto the user's disk: the desktop app's save dialog, or a
     *  download. Resolves `{saved, where}`. */
    async function saveBlob(blob, name) {
        const desktop = global.PlexoraDesktop;
        if (desktop?.saveBlob) {
            const path = await desktop.saveBlob(blob, name);
            return { saved: Boolean(path), where: path || null };
        }
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = name;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        return { saved: true, where: name };
    }

    /** A rough time and size, from the frame count and what kind of frames
     *  they are -- labelled "about" wherever it is shown. */
    function estimate(project, settings) {
        const Schema = global.PlexoraVideoSchema;
        const frames = Schema.frameCount(project.duration_s, settings.fps);
        const volume = project.base?.view?.mode === "volume"
            || project.tracks.some((t) => t.kind === "camera3d");
        const pixels = settings.width * settings.height / (1920 * 1080);
        const perFrameMs = (volume ? 160 : 70) * Math.max(0.6, pixels);
        const mbps = settings.format === "png" ? 60 * pixels * settings.fps / 30
            : settings.format === "mov" ? 120 * pixels * settings.fps / 30
                : settings.format === "gif" ? 12 * pixels
                    : (global.PlexoraVideoEncoders.bitrate(settings) / 1e6) * 0.7;
        return { frames, seconds: Math.round(frames * perFrameMs / 1000),
                 megabytes: Math.max(0.1, (mbps * project.duration_s) / 8) };
    }

    /**
     * Render and encode `project`. `options`: `settings` (normalised output:
     * width, height, fps, format, quality, transparent, overlays), `route`
     * (from videoEncoders.available), `api`, `videoId`, `signal`,
     * `onProgress({done, total, frameMs, canvas})`, `extra` (dataset name,
     * attribution), `remote`.
     * Resolves `{result, manifest, saved}`.
     */
    async function run(env, project, options) {
        const { settings, route, api, videoId, signal = null, onProgress = null,
                extra = {}, remote = false, timeoutMs = null } = options;
        const Animation = global.PlexoraVideoAnimation;
        const Coordinator = global.PlexoraVideoRenderCoordinator;
        const frames = Animation.frames(project, settings.fps);
        const cache = {};
        const states = frames.map((f) => Animation.evaluate(project, f.t, { cache }));
        const volume = states.some((s) => s.view.mode === "volume");
        const frameTimeout = timeoutMs
            || ((volume || settings.width * settings.height > 2560 * 1440) ? 60000 : 30000);
        const started = now();
        const session = await Coordinator.open(env, {
            width: settings.width, height: settings.height, transparent: settings.transparent,
            overlays: settings.overlays, onCancel: options.onCancel });
        let encoder = null;
        const record = [];
        const perFrame = [];
        try {
            session.cover.message("Preparing…");
            await session.prefetch.prime(project, states);
            encoder = global.PlexoraVideoEncoders.create(route, { ...settings, frames: frames.length },
                                                         { videoId, api, composer: session.composer,
                                                           remote });
            await encoder.start();
            for (let k = 0; k < frames.length; k += 1) {
                if (signal?.aborted) throw new DOMException("stopped", "AbortError");
                await whenVisible(signal);
                session.prefetch.ahead(states, k);
                const t0 = now();
                const { canvas, waitedMs } = await Coordinator.frame(session, states[k], {
                    signal, timeoutMs: frameTimeout, extra });
                if (signal?.aborted) throw new DOMException("stopped", "AbortError");
                await encoder.push({ index: k, canvas, timestampUs: frames[k].timestampUs });
                const ms = now() - t0;
                perFrame.push(ms);
                record.push({ k, t: states[k].view.t, z: states[k].view.z, mode: states[k].view.mode,
                              content_s: Math.round(frames[k].t * 1e6) / 1e6,
                              waited_ms: Math.round(waitedMs) });
                session.cover.progress(k + 1, frames.length);
                onProgress?.({ done: k + 1, total: frames.length, frameMs: ms, canvas });
                if (k % 4 === 3) await yieldToUi();
            }
            session.cover.message("Writing the file…");
            const manifest = {
                plugin_version: options.pluginVersion || "",
                user_agent: global.navigator?.userAgent || "",
                frames: record,
                unresolved_frames: [],
                retries: session.retries,
                render_s: Math.round((now() - started) / 10) / 100,
                per_frame_ms: { mean: Math.round(perFrame.reduce((a, b) => a + b, 0) / Math.max(1, perFrame.length)),
                                max: Math.round(Math.max(0, ...perFrame)) },
                readiness_waits_ms: { mean: Math.round(session.waits.reduce((a, b) => a + b, 0)
                                                       / Math.max(1, session.waits.length)),
                                      max: Math.round(Math.max(0, ...session.waits)) },
                missing_channels: session.missingChannels || [],
            };
            const result = await encoder.finish(manifest);
            manifest.encoder = result.encoder;
            let saved = null;
            if (result.kind === "blob") {
                const name = fileName(project, settings);
                result.name = name;
                saved = await saveBlob(result.blob, name);
                try {
                    const recorded = await api.recordExport(videoId, {
                        output: { width: settings.width, height: settings.height, fps: settings.fps,
                                  format: settings.format, quality: settings.quality,
                                  transparent: Boolean(settings.transparent), overlays: settings.overlays },
                        manifest, frames: frames.length, bytes: result.bytes, complete: true });
                    result.manifest = recorded.manifest;
                    result.exportId = recorded.export_id;
                } catch (error) {
                    console.warn("Video Studio: the export's manifest could not be kept", error);
                }
            }
            return { result, manifest, saved };
        } catch (error) {
            if (encoder) await encoder.cancel().catch(() => null);
            throw error;
        } finally {
            await Coordinator.close(session);
        }
    }

    global.PlexoraVideoExportLoop = { run, estimate, fileName, saveBlob };
})(typeof window !== "undefined" ? window : globalThis);
