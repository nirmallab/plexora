/*
 * probes.js - what the 3-D benchmark watches in the page, from the outside.
 *
 * Injected before any of the page's own scripts (drive.mjs,
 * Page.addScriptToEvaluateOnNewDocument), so the same observers run against
 * every revision of the viewer: a "before" build has no marks of its own, and
 * numbers compared across builds must come from the same place.
 *
 * - counters: shader compiles, programme links, 2-D and 3-D texture uploads,
 *   shader XHRs, ImageBitmap decodes, worker messages by script, uniform
 *   lookups -- patched onto the prototypes, so nothing in the app changes;
 * - a frame loop: every requestAnimationFrame timestamp while recording, and
 *   after each frame (a task posted from the frame, which runs once it has been
 *   painted) what every surface showed: the plane's committed view token,
 *   each section pane's token and z-line, the 3-D renderer's textures -- kept
 *   as TRANSITIONS, so a run of thousands of frames stays small;
 * - optionally a luminance grid of what is on screen: the viewer's ground,
 *   the plane canvas over it, the fade's ghost at its current opacity over that,
 *   the 3-D canvas -- composited the way the browser composites them;
 * - input timestamps (capture phase) and the viewer's own events.
 *
 * Everything lives on `window.__bench`. Nothing here reads anything the app
 * would not show the user, and nothing is written into the app.
 */
(function () {
    "use strict";
    if (window.__bench) return;
    const now = () => performance.now();
    const B = window.__bench = {
        version: 1,
        counters: Object.create(null),
        events: [],
        inputs: [],
        trans: [],
        frames: [],
        lum: [],
        longTasks: [],
        recording: false,
        lumOn: false,
        lumGrid: [32, 20],
        lumRegion: null,
        sig: Object.create(null),
    };
    const count = (key, n = 1) => { B.counters[key] = (B.counters[key] || 0) + n; };
    B.count = count;
    B.event = (name, detail) => { B.events.push([now(), name, detail === undefined ? null : detail]); };

    // -- counters ---------------------------------------------------------

    function patch(proto, name, before) {
        const original = proto && proto[name];
        if (typeof original !== "function" || original.__benchPatched) return;
        const wrapped = function (...args) {
            try { before.call(this, args); } catch (error) { /* never break the app */ }
            return original.apply(this, args);
        };
        wrapped.__benchPatched = true;
        proto[name] = wrapped;
    }
    const lastBytes = (args) => {
        const data = args[args.length - 1];
        return data && typeof data.byteLength === "number" ? data.byteLength : 0;
    };
    for (const Ctx of [window.WebGL2RenderingContext, window.WebGLRenderingContext]) {
        if (!Ctx) continue;
        const p = Ctx.prototype;
        patch(p, "compileShader", () => count("shaderCompiles"));
        patch(p, "linkProgram", () => count("programLinks"));
        patch(p, "texImage3D", (a) => { count("texImage3DCalls"); count("texImage3DBytes", lastBytes(a)); });
        patch(p, "texSubImage3D", (a) => { count("texSubImage3DCalls"); count("texSubImage3DBytes", lastBytes(a)); });
        patch(p, "texImage2D", () => count("texImage2DCalls"));
        patch(p, "texSubImage2D", () => count("texSubImage2DCalls"));
        patch(p, "getUniformLocation", () => count("uniformLookups"));
        patch(p, "deleteTexture", () => count("deleteTexture"));
    }
    patch(XMLHttpRequest.prototype, "open", (args) => {
        const address = String(args[1] || "");
        if (/\.glsl(\?|$)/.test(address)) count("shaderXhrs");
        const kind = kindOf(address);
        if (kind) count(`req:${kind}`);
    });
    if (typeof window.createImageBitmap === "function") {
        const native = window.createImageBitmap;
        window.createImageBitmap = function (...args) {
            count("imageBitmaps");
            return native.apply(this, args);
        };
    }
    if (typeof window.Worker === "function") {
        const NativeWorker = window.Worker;
        const named = function Worker(address, options) {
            const worker = new NativeWorker(address, options);
            try {
                worker.__benchName = String(address).split("/").pop().split("?")[0] || "worker";
            } catch (error) { /* a Blob URL */ }
            return worker;
        };
        named.prototype = NativeWorker.prototype;
        window.Worker = named;
        patch(NativeWorker.prototype, "postMessage", function () {
            count(`worker:${this.__benchName || "worker"}`);
        });
    }
    const nativeFetch = window.fetch;
    if (typeof nativeFetch === "function") {
        window.fetch = function (...args) {
            const address = String(args[0]?.url || args[0] || "");
            const kind = kindOf(address);
            if (kind) { count(`fetch:${kind}`); count(`req:${kind}`); }
            const result = nativeFetch.apply(this, args);
            if (kind) {
                result.catch((error) => {
                    if (error?.name === "AbortError") count(`aborted:${kind}`);
                });
            }
            return result;
        };
    }
    function kindOf(address) {
        if (/\/volume\/[^/]+\/level/.test(address)) return "level";
        if (/\/volume\/[^/]+\/bricks?\b/.test(address)) return "brick";
        if (/view=(t\d+)?(xzy|yzx)/.test(address)) return "section";
        if (/\/generated\/data\//.test(address)) return "tile";
        if (/\.glsl(\?|$)/.test(address)) return "shader";
        if (/\/(stats|gmm|quantization)/.test(address)) return "stats";
        return "";
    }
    B.kindOf = kindOf;

    // -- input --------------------------------------------------------------

    for (const type of ["keydown", "keyup", "pointerdown", "pointerup", "wheel"]) {
        window.addEventListener(type, (event) => {
            B.inputs.push([event.timeStamp, type, event.key || event.button || 0]);
        }, { capture: true, passive: true });
    }
    let moveSeen = 0;
    window.addEventListener("pointermove", (event) => {
        if (event.buttons) B.inputs.push([event.timeStamp, "drag", moveSeen++]);
    }, { capture: true, passive: true });
    for (const name of ["plexora:view-requested", "plexora:view-changed", "plexora:view-class-changed",
                        "plexora:hd-mode-changed", "plexora:volume-progress", "plexora:channels-applied"]) {
        window.addEventListener(name, (event) => {
            const d = event.detail || {};
            B.event(name, d.spec !== undefined ? { spec: d.spec, rev: d.rev ?? null }
                : (d.status !== undefined ? { status: d.status } : null));
        });
    }
    try {
        new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) {
                B.longTasks.push([entry.startTime, entry.duration]);
            }
        }).observe({ type: "longtask", buffered: true });
    } catch (error) { /* not supported */ }

    // -- app hooks (lazy: the app's objects exist only after boot) ------------

    function hookApp() {
        const P = window.__plexora;
        if (!P) return false;
        const fade = P.planeMotion?.fade;
        if (fade && !fade.__bench) {
            fade.__bench = true;
            const capture = fade.capture.bind(fade);
            const release = fade.release.bind(fade);
            fade.capture = (...a) => { const r = capture(...a); B.event("ghost:capture", r); count("ghostCaptures"); return r; };
            fade.release = (...a) => { const r = release(...a); B.event("ghost:release", null); return r; };
        }
        const panes = P.sectionPanes?.panes;
        if (panes) {
            for (const [axis, pane] of Object.entries(panes)) {
                if (pane.__bench) continue;
                pane.__bench = true;
                const show = pane.show.bind(pane);
                pane.show = (token, ...rest) => {
                    if (token !== pane.token) { B.event(`${axis}:show`, token); count("paneShows"); }
                    return show(token, ...rest);
                };
            }
        }
        const renderer = P.volumeView?.renderer;
        if (renderer && !renderer.__bench) {
            renderer.__bench = true;
            const draw = renderer.draw.bind(renderer);
            renderer.draw = (...a) => { count("volumeDraws"); B.volumeDrawAt = now(); return draw(...a); };
            if (renderer.loadChannel) {
                const load = renderer.loadChannel.bind(renderer);
                renderer.loadChannel = (...a) => { count("volumeLoads"); B.event("volume:load", a[0]); return load(...a); };
            }
        }
        return true;
    }
    B.hookApp = hookApp;

    // -- what each surface shows ----------------------------------------------

    function worldSig(world) {
        if (!world) return "";
        const views = new Set();
        let n = 0;
        for (let i = 0; i < world.getItemCount(); i += 1) {
            const item = world.getItemAt(i);
            const source = item.source || {};
            if (source.tileFormat !== 16 || !(item.getOpacity() > 0)) continue;
            n += 1;
            views.add(`${source.hd ? "hd:" : ""}${source.view || ""}`);
        }
        return n ? `${[...views].sort().join("|")}#${n}` : "";
    }
    /** Where the pane marks the plane: its z-line, and -- for a pane smaller
     *  than its stack, which scrolls instead of moving the line -- its scroll. */
    function zline(pane) {
        const line = pane.zline;
        if (!line || line.hidden || pane.root?.hidden) return "";
        const at = Math.round(parseFloat(line.style.top || line.style.left || "0"));
        const top = Number.isFinite(pane.view?.top) ? Math.round(pane.view.top * 4) / 4 : "";
        return `${at}|${top}`;
    }
    function sample(frameTs) {
        const P = window.__plexora;
        if (!P) return;
        hookApp();
        const t = now();
        const viewer = P.seaDragonViewer?.viewer;
        const sigs = { main: worldSig(viewer?.world) };
        const panes = P.sectionPanes?.enabled ? P.sectionPanes?.panes : null;
        if (panes) {
            for (const [axis, pane] of Object.entries(panes)) {
                sigs[axis] = worldSig(pane.osd?.world);
                sigs[`${axis}Line`] = zline(pane);
            }
        }
        const renderer = P.volumeView?.renderer;
        if (renderer?.canvas && !renderer.canvas.hidden) {
            sigs.volume = `${renderer.status}:${[...(renderer.textures?.keys?.() || [])].sort().join("|")}`;
        } else {
            sigs.volume = "";
        }
        const ghost = P.planeMotion?.fade?.ghost;
        sigs.ghost = ghost && !ghost.hidden ? "on" : "";
        for (const [surface, value] of Object.entries(sigs)) {
            if (B.sig[surface] !== value) {
                B.sig[surface] = value;
                B.trans.push([t, surface, value, frameTs]);
            }
        }
        if (B.lumOn) {
            if (B.lumMode === "bitmap") {
                luminanceAsync(t);
            } else {
                const grid = luminance();
                if (grid) B.lum.push([t, grid, ghostOpacity()]);
            }
        }
    }

    /**
     * The same grid, without reading the plane's canvas back: each canvas is
     * downscaled by createImageBitmap (a snapshot taken now, resized off the
     * main thread) and only the 32x20 results are read. Reading a full canvas
     * back every frame makes Chrome move it off the GPU, which slows every
     * later draw of the page.
     */
    let lumBusy = 0;
    function luminanceAsync(t) {
        const P = window.__plexora;
        const main = P?.seaDragonViewer?.viewer?.drawer?.canvas;
        if (!main || !main.width || lumBusy > 3) return;
        const [gw, gh] = B.lumGrid;
        const region = B.lumRegion;
        const crop = (canvas) => (region
            ? [region[0] * canvas.width, region[1] * canvas.height, region[2] * canvas.width,
               region[3] * canvas.height]
            : [0, 0, canvas.width, canvas.height]);
        const resize = { resizeWidth: gw, resizeHeight: gh, resizeQuality: "medium" };
        const renderer = P.volumeView?.renderer;
        const volume = renderer?.canvas && !renderer.canvas.hidden && renderer.canvas.width ? renderer.canvas : null;
        const ghost = P.planeMotion?.fade?.ghost;
        const opacity = ghostOpacity();
        const ground = groundOf(document.getElementById("openseadragon"));
        const snap = (canvas) => createImageBitmap(canvas, ...crop(canvas), resize);
        lumBusy += 1;
        Promise.all([snap(volume || main), !volume && ghost && opacity > 0 && ghost.width ? snap(ghost) : null])
            .then(([plane, held]) => {
                if (!lumCanvas) {
                    lumCanvas = document.createElement("canvas");
                    lumCanvas.width = gw;
                    lumCanvas.height = gh;
                    lumContext = lumCanvas.getContext("2d", { willReadFrequently: true });
                }
                const ctx = lumContext;
                ctx.globalAlpha = 1;
                ctx.fillStyle = ground;
                ctx.fillRect(0, 0, gw, gh);
                ctx.drawImage(plane, 0, 0);
                plane.close();
                if (held) {
                    ctx.globalAlpha = opacity;
                    ctx.drawImage(held, 0, 0);
                    ctx.globalAlpha = 1;
                    held.close();
                }
                const data = ctx.getImageData(0, 0, gw, gh).data;
                const grid = new Array(gw * gh);
                for (let i = 0, j = 0; i < data.length; i += 4, j += 1) {
                    grid[j] = Math.round(0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]);
                }
                B.lum.push([t, grid, opacity]);
                B.lum.sort((a, b) => a[0] - b[0]);
            })
            .catch(() => count("lumErrors"))
            .finally(() => { lumBusy -= 1; });
    }
    B.sample = sample;

    function ghostOpacity() {
        const ghost = window.__plexora?.planeMotion?.fade?.ghost;
        if (!ghost || ghost.hidden) return 0;
        const value = Number(getComputedStyle(ghost).opacity);
        return Number.isFinite(value) ? value : 0;
    }

    let lumCanvas = null;
    let lumContext = null;
    function groundOf(host) {
        const colour = host ? getComputedStyle(host).backgroundColor : "";
        return colour && colour !== "rgba(0, 0, 0, 0)" && colour !== "transparent" ? colour : "#000";
    }
    /** What the screen shows over the plane, as a grid of luminances 0..255. */
    function luminance() {
        const P = window.__plexora;
        const viewer = P?.seaDragonViewer?.viewer;
        const main = viewer?.drawer?.canvas;
        if (!main || !main.width) return null;
        const [gw, gh] = B.lumGrid;
        if (!lumCanvas) {
            lumCanvas = document.createElement("canvas");
            lumCanvas.width = gw;
            lumCanvas.height = gh;
            lumContext = lumCanvas.getContext("2d");
        }
        const ctx = lumContext;
        const region = B.lumRegion;   // [x, y, w, h] as shares of the canvas, or null
        const src = (canvas) => {
            if (!region) return [0, 0, canvas.width, canvas.height];
            return [region[0] * canvas.width, region[1] * canvas.height,
                    region[2] * canvas.width, region[3] * canvas.height];
        };
        ctx.globalCompositeOperation = "source-over";
        ctx.globalAlpha = 1;
        ctx.fillStyle = groundOf(document.getElementById("openseadragon"));
        ctx.fillRect(0, 0, gw, gh);
        const renderer = P.volumeView?.renderer;
        if (renderer?.canvas && !renderer.canvas.hidden && renderer.canvas.width) {
            ctx.drawImage(renderer.canvas, ...src(renderer.canvas), 0, 0, gw, gh);
        } else {
            ctx.drawImage(main, ...src(main), 0, 0, gw, gh);
            const ghost = P.planeMotion?.fade?.ghost;
            const opacity = ghostOpacity();
            if (ghost && opacity > 0 && ghost.width) {
                ctx.globalAlpha = opacity;
                ctx.drawImage(ghost, ...src(ghost), 0, 0, gw, gh);
                ctx.globalAlpha = 1;
            }
        }
        const data = ctx.getImageData(0, 0, gw, gh).data;
        const out = new Array(gw * gh);
        for (let i = 0, j = 0; i < data.length; i += 4, j += 1) {
            out[j] = Math.round(0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]);
        }
        return out;
    }
    B.luminance = luminance;

    // -- the frame loop --------------------------------------------------------

    const channel = new MessageChannel();
    let pendingTs = 0;
    //: One frame loop at a time: a stop and a start within a frame must not
    //: leave the old loop running beside the new one.
    let generation = 0;
    channel.port1.onmessage = () => {
        if (!B.recording) return;
        try { sample(pendingTs); } catch (error) { count("sampleErrors"); }
    };
    function loop(gen) {
        const frame = (ts) => {
            if (!B.recording || gen !== generation) return;
            B.frames.push(ts);
            pendingTs = ts;
            channel.port2.postMessage(0);
            requestAnimationFrame(frame);
        };
        return frame;
    }

    /** Start recording: frames, transitions, luminance when asked. */
    B.start = ({ lum = false, grid = null, region = null } = {}) => {
        B.lumOn = Boolean(lum);
        B.lumMode = lum === "sync" ? "sync" : "bitmap";
        if (grid) B.lumGrid = grid;
        B.lumRegion = region;
        if (B.recording) return now();
        B.recording = true;
        generation += 1;
        requestAnimationFrame(loop(generation));
        return now();
    };
    B.stop = () => { B.recording = false; B.lumOn = false; generation += 1; return now(); };
    /** Forget what was recorded (counters too, unless told otherwise). */
    B.reset = ({ counters = true } = {}) => {
        B.events.length = 0;
        B.inputs.length = 0;
        B.trans.length = 0;
        B.frames.length = 0;
        B.lum.length = 0;
        B.longTasks.length = 0;
        B.sig = Object.create(null);
        if (counters) B.counters = Object.create(null);
        return now();
    };
    B.snapshot = () => ({ ...B.counters });

    /** The idle display period: the median rAF interval over `ms`. */
    B.period = (ms = 1000) => new Promise((resolve) => {
        const times = [];
        const end = now() + ms;
        const tick = (ts) => {
            times.push(ts);
            if (now() < end) requestAnimationFrame(tick);
            else {
                const gaps = times.slice(1).map((v, i) => v - times[i]).sort((a, b) => a - b);
                resolve(gaps.length ? gaps[Math.floor(gaps.length / 2)] : 16.7);
            }
        };
        requestAnimationFrame(tick);
    });

    /** Resolve on the first sample at which `test(sigs)` holds, or null after `ms`. */
    B.until = (test, ms = 20000) => new Promise((resolve) => {
        const start = now();
        const poll = () => {
            let ok = false;
            try { ok = test(B.sig); } catch (error) { ok = false; }
            if (ok) return resolve(now());
            if (now() - start > ms) return resolve(null);
            setTimeout(poll, 4);
        };
        poll();
    });
    B.sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    /** The first transition of `surface` at or after `t` whose value passes `test`. */
    B.firstTrans = (surface, t, test = () => true) => {
        for (const [at, name, value, frameTs] of B.trans) {
            if (name === surface && at >= t && test(value)) return { at, value, frameTs };
        }
        return null;
    };
    B.memory = () => {
        const P = window.__plexora;
        const viewer = P?.seaDragonViewer?.viewer;
        const cache = viewer?.tileCache;
        const textures = P?.volumeView?.renderer?.textures;
        let volumeBytes = 0;
        for (const entry of textures?.values?.() || []) {
            const s = entry.shape || [0, 0, 0];
            volumeBytes += s[0] * s[1] * s[2] * (entry.format === "f16" ? 2 : 1);
        }
        return {
            heap: performance.memory ? performance.memory.usedJSHeapSize : null,
            tiles: cache?.numTilesLoaded?.() ?? null,
            cacheRecords: cache?.numCachesLoaded?.() ?? null,
            glTileBytes: P?.seaDragonViewer?.glRenderer?._tileTextureCache?.bytes ?? null,
            volumeBytes,
            perf: P?.perf?.memory?.() || null,
        };
    };
})();
