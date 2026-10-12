/**
 * videoEncoders.js - turning composed frames into a file, wherever that can happen.
 *
 * Two kinds of encoder share one interface -- `start()`, `push({index,
 * canvas, timestampUs})` (resolves once the encoder can take another frame),
 * `finish(manifest)`, `cancel()`:
 *
 *   WebCodecsEncoder  the browser encodes (videoEncodeWorker.js); the file
 *                     never leaves the machine the browser is on. MP4 (H.264)
 *                     and WebM (VP9) where the browser supports them -- the
 *                     default whenever it can, and the only sensible choice
 *                     when the server is remote.
 *   ServerEncoder     frames go to Plexora's server in chunks and a writer
 *                     there encodes them (server/encode_jobs.py): ffmpeg for
 *                     MP4/WebM/ProRes/GIF when installed, and always PNG
 *                     frames (zip) and a short GIF.
 *
 * `available(settings, report)` merges the browser's own probe with the
 * server's report (GET /api/encoders) into one list for the Export dialog;
 * every format is listed, an unavailable one with the reason.
 *
 * Classic script: `window.PlexoraVideoEncoders`.
 */
(function (global) {
    "use strict";

    const scriptUrl = (() => {
        const current = document.currentScript?.src;
        if (current) return current;
        const found = [...document.querySelectorAll("script[src]")]
            .find((s) => /\/videoEncoders\.js/.test(s.src));
        return found?.src || "";
    })();

    function workerUrl() {
        if (!scriptUrl) return null;
        return scriptUrl.replace(/videoEncoders\.js/, "videoEncodeWorker.js");
    }

    const QUALITY_MBPS_1080P30 = { draft: 4, good: 8, high: 14, max: 24 };

    function bitrate(settings) {
        if (settings.bitrate_mbps) return Math.round(settings.bitrate_mbps * 1e6);
        const base = QUALITY_MBPS_1080P30[settings.quality] || 8;
        return Math.round(base * 1e6 * (settings.width * settings.height) / (1920 * 1080)
                          * Math.max(1, settings.fps) / 30);
    }

    //: H.264 levels (ITU-T H.264 Table A-1): [level_idc hex, max macroblocks
    //: per frame, max macroblocks per second]. A level must hold both the
    //: frame and the rate: 4K at 30 fps needs 5.1 for its frame size alone.
    const AVC_LEVELS = [["1F", 3600, 108000], ["20", 5120, 216000], ["28", 8192, 245760],
                        ["2A", 8704, 522240], ["32", 22080, 589824], ["33", 36864, 983040],
                        ["34", 36864, 2073600], ["3C", 139264, 4177920], ["3D", 139264, 8355840],
                        ["3E", 139264, 16711680]];

    /** The H.264 High-profile codec string for a size and rate. */
    function avcCodec(width, height, fps) {
        const frame = Math.ceil(width / 16) * Math.ceil(height / 16);
        const rate = frame * Math.max(1, fps);
        const level = AVC_LEVELS.find(([, maxFrame, maxRate]) => frame <= maxFrame && rate <= maxRate)
            || AVC_LEVELS[AVC_LEVELS.length - 1];
        return `avc1.6400${level[0]}`;
    }

    async function probeWebCodecs(settings) {
        if (typeof global.VideoEncoder !== "function" || typeof global.VideoFrame !== "function") {
            return { mp4: "This browser has no video encoder (WebCodecs).",
                     webm: "This browser has no video encoder (WebCodecs)." };
        }
        if (typeof global.Worker !== "function" || !workerUrl()) {
            return { mp4: "Workers are unavailable here.", webm: "Workers are unavailable here." };
        }
        const out = {};
        const common = { width: settings.width, height: settings.height, bitrate: bitrate(settings),
                         framerate: settings.fps };
        for (const [format, codec] of [["mp4", avcCodec(settings.width, settings.height, settings.fps)],
                                       ["webm", "vp09.00.10.08"]]) {
            try {
                const answer = await global.VideoEncoder.isConfigSupported({ ...common, codec });
                out[format] = answer?.supported ? true
                    : `This browser cannot encode ${format === "mp4" ? "H.264" : "VP9"} at this size.`;
            } catch (error) {
                out[format] = "This browser could not check its encoder.";
            }
        }
        return out;
    }

    const LABELS = { mp4: "MP4 (H.264)", webm: "WebM (VP9)", mov: "ProRes (MOV)", gif: "GIF",
                     png: "PNG frames (zip)" };

    /**
     * One row per format, best route first: `{format, label, route:
     * "browser"|"server", writer, available, reason, alpha}`. `prefer`
     * "server" puts server routes first (an agent's export, which the server
     * must be able to follow).
     */
    async function available(settings, report, { prefer = "browser" } = {}) {
        const browser = await probeWebCodecs(settings);
        const rows = [];
        for (const format of ["mp4", "webm", "mov", "gif", "png"]) {
            const routes = [];
            if (format === "mp4" || format === "webm") {
                const ok = browser[format] === true;
                routes.push({ format, label: LABELS[format], route: "browser", writer: "webcodecs",
                              available: ok, reason: ok ? "" : browser[format], alpha: false });
            }
            for (const row of (report?.formats || []).filter((r) => r.format === format)) {
                routes.push({ format, label: row.label, route: "server", writer: row.writer,
                              available: row.available, reason: row.reason || "",
                              alpha: Boolean(row.alpha) });
            }
            routes.sort((a, b) => (b.available - a.available)
                || ((a.route === prefer ? 0 : 1) - (b.route === prefer ? 0 : 1)));
            rows.push(...routes);
        }
        return rows;
    }

    /** The default route: the first available MP4, else WebM, else PNG zip. */
    function defaultRoute(rows) {
        for (const format of ["mp4", "webm", "png"]) {
            const row = rows.find((r) => r.format === format && r.available);
            if (row) return row;
        }
        return rows.find((r) => r.available) || null;
    }

    // -- the browser encoder ----------------------------------------------------------

    class WebCodecsEncoder {
        constructor(settings) {
            this.settings = settings;
            this.inFlight = 0;
            this.maxInFlight = 4;
            this.waiters = [];
            this.error = null;
            this.done = null;
        }

        async start() {
            this.worker = new Worker(workerUrl(), { type: "module" });
            this.worker.onmessage = (event) => this.onMessage(event.data || {});
            this.worker.onerror = (event) => this.onMessage({ type: "error",
                                                              message: event.message || "worker failed" });
            const started = new Promise((resolve, reject) => {
                this.onStarted = resolve;
                this.onFailed = reject;
            });
            const s = this.settings;
            this.worker.postMessage({ type: "start", settings: {
                format: s.format, width: s.width, height: s.height, fps: s.fps,
                bitrate: bitrate(s), keyFrameInterval: 2, alpha: Boolean(s.transparent),
                streaming: s.width * s.height * s.frames > 1920 * 1080 * 3600,
            } });
            const answer = await started;
            this.codec = answer.codec;
            return answer;
        }

        onMessage(message) {
            if (message.type === "started") this.onStarted?.(message);
            else if (message.type === "ack") {
                this.inFlight -= 1;
                this.waiters.shift()?.();
            } else if (message.type === "done") this.onDone?.(message);
            else if (message.type === "cancelled") this.onCancelled?.();
            else if (message.type === "error") {
                this.error = new Error(message.message);
                this.onFailed?.(this.error);
                this.onDoneFailed?.(this.error);
                for (const waiter of this.waiters.splice(0)) waiter();
            }
        }

        async push({ index, canvas, timestampUs }) {
            if (this.error) throw this.error;
            while (this.inFlight >= this.maxInFlight) {
                await new Promise((resolve) => this.waiters.push(resolve));
                if (this.error) throw this.error;
            }
            const frame = new VideoFrame(canvas, { timestamp: timestampUs,
                                                   duration: Math.round(1e6 / this.settings.fps) });
            this.inFlight += 1;
            this.worker.postMessage({ type: "frame", frame, timestampUs, index,
                                      keyFrame: index % Math.max(1, 2 * this.settings.fps) === 0 },
                                    [frame]);
        }

        async finish() {
            if (this.error) throw this.error;
            const result = await new Promise((resolve, reject) => {
                this.onDone = resolve;
                this.onDoneFailed = reject;
                this.worker.postMessage({ type: "finish" });
            });
            this.worker.terminate();
            return { kind: "blob", blob: result.blob, bytes: result.bytes,
                     encoder: { encoder: "browser (WebCodecs)", codec: this.codec,
                                muxer: "mediabunny 1.60.0", bitrate: bitrate(this.settings) } };
        }

        async cancel() {
            if (!this.worker) return;
            await Promise.race([
                new Promise((resolve) => {
                    this.onCancelled = resolve;
                    this.worker.postMessage({ type: "cancel" });
                }),
                new Promise((resolve) => setTimeout(resolve, 2000)),
            ]);
            this.worker.terminate();
        }
    }

    // -- the server encoder ------------------------------------------------------------

    class ServerEncoder {
        constructor(settings, { videoId, api, composer, png = false }) {
            this.settings = settings;
            this.videoId = videoId;
            this.api = api;
            this.composer = composer;
            this.png = png;
            this.buffer = [];
            this.pending = null;
            this.sent = 0;
        }

        async start() {
            const answer = await this.api.startExport(this.videoId, {
                width: this.settings.width, height: this.settings.height, fps: this.settings.fps,
                format: this.settings.format, quality: this.settings.quality,
                bitrate_mbps: this.settings.bitrate_mbps || null,
                transparent: Boolean(this.settings.transparent), overlays: this.settings.overlays,
                writer: this.settings.writer,
            });
            this.exportId = answer.export_id;
            this.chunkFrames = Math.max(1, answer.chunk_frames || 8);
            if (this.png) this.chunkFrames = Math.min(this.chunkFrames, 8);
            return answer;
        }

        async push({ index }) {
            if (this.png) {
                const blob = await this.composer.png();
                this.buffer.push(new Uint8Array(await blob.arrayBuffer()));
            } else {
                this.buffer.push(new Uint8Array(this.composer.rgba().buffer));
            }
            if (this.buffer.length >= this.chunkFrames) await this.flush();
            void index;
        }

        async flush() {
            if (!this.buffer.length) return;
            if (this.pending) await this.pending;
            const frames = this.buffer.splice(0);
            let total = 0;
            for (const f of frames) total += f.byteLength + (this.png ? 4 : 0);
            const body = new Uint8Array(total);
            let at = 0;
            for (const f of frames) {
                if (this.png) {
                    new DataView(body.buffer).setUint32(at, f.byteLength, false);
                    at += 4;
                }
                body.set(f, at);
                at += f.byteLength;
            }
            const first = this.sent;
            this.sent += frames.length;
            this.pending = this.send(first, frames.length, body);
            await this.pending;
            this.pending = null;
        }

        async send(first, count, body) {
            for (let attempt = 0; ; attempt += 1) {
                try {
                    return await this.api.pushFrames(this.videoId, this.exportId, first, count, body,
                                                     this.png ? "png" : "rgba");
                } catch (error) {
                    if (error.status === 503 && attempt < 600) {
                        await new Promise((resolve) => setTimeout(resolve, (error.retryAfter || 0.25) * 1000));
                        continue;
                    }
                    throw error;
                }
            }
        }

        async finish(manifest) {
            await this.flush();
            if (this.pending) await this.pending;
            await this.api.finishExport(this.videoId, this.exportId, manifest);
            for (;;) {
                const status = await this.api.exportStatus(this.videoId, this.exportId);
                if (status.status === "done") {
                    return { kind: "download", url: this.api.downloadUrl(this.videoId, this.exportId),
                             name: status.name, bytes: status.bytes, exportId: this.exportId,
                             encoder: { encoder: "server", writer: status.writer, label: status.label } };
                }
                if (status.status === "failed" || status.status === "cancelled") {
                    const error = new Error(status.error || `the export ${status.status}`);
                    error.tail = status.error_tail;
                    throw error;
                }
                await new Promise((resolve) => setTimeout(resolve, 700));
            }
        }

        async cancel() {
            if (!this.exportId) return;
            try {
                await this.api.cancelExport(this.videoId, this.exportId);
            } catch (error) {
                // an export that already ended is cancelled enough
            }
        }
    }

    function create(route, settings, context) {
        if (route.route === "browser") return new WebCodecsEncoder(settings);
        // Over a remote link a raw 4K frame is 33 MB; PNG is a fraction of it.
        return new ServerEncoder({ ...settings, writer: route.writer }, {
            ...context, png: Boolean(context.remote) });
    }

    global.PlexoraVideoEncoders = { available, defaultRoute, create, bitrate, avcCodec,
                                    WebCodecsEncoder, ServerEncoder, LABELS };
})(typeof window !== "undefined" ? window : globalThis);
