/**
 * performanceTelemetry.js - how the viewer performs, as histograms.
 *
 * Nothing here is per tile, per frame or per gesture on the wire: every
 * measurement lands in one of nine timing bins in the tab's aggregate
 * (services/telemetry.js), which posts to this tab's own server every few
 * minutes. Nothing here keeps a URL either. A tile request is classified by
 * its path's SHAPE -- `/generated/data/.../<channel>_<n>/...` is a channel
 * tile -- into `{path, kind}` and the URL is dropped in the same expression;
 * the project's name is in every tile URL and must never be kept.
 *
 * Sources, all passive:
 *
 * - a `PerformanceObserver` over resource timings: fetch time per tile, split
 *   by where it came from (`local`, through a `proxy` prefix, or `direct` from
 *   a node), plus the server's own `Server-Timing` phases for it;
 * - the OSD viewer's `animation` / `animation-finish` (frame gaps while a
 *   gesture runs), its first `tile-drawn` (first paint) and `tile-load-failed`;
 * - `decoded()` from tileDecode.js, and the GL texture cache's own hit/miss
 *   counters read as deltas when the aggregate is folded (no call on the
 *   per-frame path);
 * - `longtask` entries, where the browser reports them;
 * - `webglcontextlost` on the canvases that draw.
 *
 * Browser, OS and GPU are FAMILIES (`chrome`, `mac`, `nvidia`): each is
 * parsed with a short table and the raw string is discarded in the same
 * expression. GPU family is only attached in diagnostics mode.
 */
window.PlexoraPerf = (function () {
    "use strict";

    const telemetry = () => window.PlexoraTelemetry;
    const EVENT = "render.summary";

    const BROWSERS = [[/Edg\//, "edge"], [/Firefox\//, "firefox"], [/Chrome\//, "chrome"],
                      [/Safari\//, "safari"]];
    const SYSTEMS = [[/Mac OS X|Macintosh/, "mac"], [/Windows/, "windows"], [/Linux|X11/, "linux"]];
    const GPUS = [[/nvidia|geforce|quadro|rtx|tesla/i, "nvidia"],
                  [/amd|radeon/i, "amd"], [/intel/i, "intel"],
                  [/apple|m1|m2|m3|m4/i, "apple"],
                  [/swiftshader|llvmpipe|software|basic render/i, "software"]];

    function family(table, text, fallback) {
        const value = String(text || "");
        for (const [pattern, name] of table) if (pattern.test(value)) return name;
        return fallback;
    }

    const base = {
        browser: family(BROWSERS, navigator.userAgent, "other"),
        os: family(SYSTEMS, navigator.userAgent, "other"),
        label_renderer: "none",
    };
    let gpu = "unknown";

    function dims(extra) {
        const out = Object.assign({}, base, extra || {});
        if (telemetry()?.mode === "diagnostics") out.gpu = gpu;
        return out;
    }

    function observe(key, ms, extra) {
        telemetry()?.observe(EVENT, key, ms, dims(extra));
    }

    function count(key, n, extra) {
        telemetry()?.count(EVENT, key, n, dims(extra));
    }

    // -- where a tile came from ----------------------------------------------

    const CHANNEL = /\/generated\/data\/[^/]+\/([^/]+)\/[^/]+\/[^/]+$/;
    const LAYER = /\/generated\/layer\//;
    const POINTS = /\/(?:get_centroid_tiles|plugins\/transcripts\/points|plugins\/visium_hd\/(?:bin|spot_values))(?:$|\/)/;
    const NODE_IMAGE = /\/node\/v1\/image\/[^/]+\/tile\//;
    const NODE_LABEL = /\/node\/v1\/seg\/[^/]+\/tile\//;

    /**
     * `{path, kind}` for a tile request, or null for anything else. Pure over
     * the URL, and the only thing it returns is those two words.
     */
    function classify(name) {
        let url;
        try {
            url = new URL(name, window.location.href);
        } catch (error) {
            return null;
        }
        const own = url.origin === window.location.origin;
        const direct = !own && Boolean(window.PlexoraRouting?.isDirectOrigin?.(url.origin));
        if (!own && !direct) return null;
        const pathname = url.pathname;
        let kind = null;
        const channel = pathname.match(CHANNEL);
        if (channel) {
            const key = channel[1];
            if (key === "rgb") kind = "rgb";
            else if (!/_\d+$/.test(key)) kind = "label";
            else kind = url.searchParams.get("q") === "hd" ? "hd" : "channel";
        } else if (LAYER.test(pathname) || NODE_IMAGE.test(pathname)) {
            kind = "channel";
        } else if (NODE_LABEL.test(pathname)) {
            kind = "label";
        } else if (POINTS.test(pathname)) {
            kind = "points";
        }
        if (kind === null) return null;
        const prefixed = Boolean(window.PLEXORA_BASE_URL && window.PLEXORA_BASE_URL !== "/");
        return { path: direct ? "direct" : (prefixed ? "proxy" : "local"), kind };
    }

    let tilesThisMinute = 0;
    let minuteStart = performance.now();
    let peakPerMinute = 0;
    let bytes = 0;

    function onResource(entry) {
        if (entry.initiatorType !== "xmlhttprequest" && entry.initiatorType !== "fetch"
                && entry.initiatorType !== "img") return;
        const where = classify(entry.name);
        if (!where) return;
        const duration = entry.responseEnd - entry.startTime;
        if (duration >= 0) observe("tile_ms", duration, where);
        bytes += entry.transferSize || 0;
        const now = performance.now();
        if (now - minuteStart > 60000) {
            peakPerMinute = Math.max(peakPerMinute, tilesThisMinute);
            tilesThisMinute = 0;
            minuteStart = now;
        }
        tilesThisMinute += 1;
        for (const timing of entry.serverTiming || []) {
            if (timing.name === "total") observe("server_total_ms", timing.duration, { path: where.path });
            else if (timing.name === "read" || timing.name === "node") {
                observe("server_read_ms", timing.duration, { path: where.path });
            } else if (timing.name === "enc") observe("server_encode_ms", timing.duration, { path: where.path });
            else if (timing.name === "cache" && (timing.description === "hit" || timing.description === "miss")) {
                count("server_cache", 1, { path: where.path, result: timing.description });
            }
        }
    }

    function watchResources() {
        if (typeof PerformanceObserver !== "function") return;
        try {
            new PerformanceObserver((list) => {
                if (telemetry()?.enabled === false) return;
                for (const entry of list.getEntries()) onResource(entry);
            }).observe({ type: "resource", buffered: true });
        } catch (error) {
            // An engine without resource timing: there is nothing to watch.
        }
        try {
            new PerformanceObserver((list) => {
                if (telemetry()?.enabled === false) return;
                for (const entry of list.getEntries()) observe("long_task_ms", entry.duration);
            }).observe({ type: "longtask", buffered: true });
        } catch (error) {
            // `longtask` is Chromium-only.
        }
    }

    // -- the viewer ----------------------------------------------------------

    function watchViewer(viewer) {
        if (!viewer || !viewer.addHandler || viewer.__plexoraPerfWatched) return;
        viewer.__plexoraPerfWatched = true;
        const created = performance.now();
        let frame = null;
        let last = 0;
        const tick = (now) => {
            if (last) observe("frame_gap_ms", now - last);
            last = now;
            frame = requestAnimationFrame(tick);
        };
        viewer.addHandler("animation", () => {
            if (frame !== null || telemetry()?.enabled === false) return;
            last = 0;
            frame = requestAnimationFrame(tick);
        });
        viewer.addHandler("animation-finish", () => {
            if (frame === null) return;
            cancelAnimationFrame(frame);
            frame = null;
            count("gestures", 1);
        });
        const drawn = () => {
            viewer.removeHandler("tile-drawn", drawn);
            observe("first_paint_ms", performance.now());
            observe("boot_ms", created);
        };
        viewer.addHandler("tile-drawn", drawn);
        viewer.addHandler("tile-load-failed", (event) => {
            const where = classify(event?.tile?.getUrl?.() || "");
            count("tile_failures", 1, { path: where ? where.path : "local" });
        });
    }

    // -- decode, textures, GL ------------------------------------------------

    function decoded(format, where, ms) {
        observe("decode_ms", ms, { format, where });
    }

    function decodeFallback() {
        count("decode_fallback", 1);
    }

    const caches = [];
    function noteGl(renderer, where) {
        const gl = renderer?.gl || renderer;
        if (gl && gpu === "unknown") {
            try {
                const info = gl.getExtension && gl.getExtension("WEBGL_debug_renderer_info");
                gpu = info ? family(GPUS, gl.getParameter(info.UNMASKED_RENDERER_WEBGL), "other")
                    : "other";
            } catch (error) {
                gpu = "other";
            }
        }
        const cache = renderer?._tileTextureCache;
        if (cache && !caches.some((c) => c.cache === cache)) {
            caches.push({ cache, hits: 0, misses: 0 });
        }
        const canvas = gl?.canvas;
        if (canvas && canvas.addEventListener && !canvas.__plexoraPerfLost) {
            canvas.__plexoraPerfLost = true;
            canvas.addEventListener("webglcontextlost", () => {
                count("gl_context_lost", 1, { where: where || "image" });
            });
        }
    }

    function labelRenderer(kind) {
        base.label_renderer = kind === "gpu" || kind === "cpu" ? kind : "none";
    }

    let reportedMachine = false;
    function fold() {
        for (const entry of caches) {
            const hits = entry.cache.hits || 0;
            const misses = entry.cache.misses || 0;
            if (hits > entry.hits) count("texture_cache", hits - entry.hits, { result: "hit" });
            if (misses > entry.misses) count("texture_cache", misses - entry.misses, { result: "miss" });
            entry.hits = hits;
            entry.misses = misses;
        }
        const peak = Math.max(peakPerMinute, tilesThisMinute);
        if (peak) count("peak_tiles_per_min", 1, { band: telemetry().band10(peak) });
        peakPerMinute = 0;
        if (bytes) count("tile_bytes", 1, { band: telemetry().band10(bytes) });
        bytes = 0;
        if (!reportedMachine) {
            reportedMachine = true;
            count("hw_concurrency", 1, { band: telemetry().bandPow2(navigator.hardwareConcurrency || 0) });
            const ratio = window.devicePixelRatio || 1;
            count("dpr", 1, { band: ratio >= 2.5 ? "3+" : ratio >= 1.75 ? "2" : ratio >= 1.25 ? "1.5" : "1" });
        }
    }

    watchResources();
    telemetry()?.beforePost(fold);

    return { classify, watchViewer, decoded, decodeFallback, noteGl, labelRenderer,
             _fold: fold, _onResource: onResource };
})();
