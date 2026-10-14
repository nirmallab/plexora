// scenarios.mjs - the benchmark scenarios (docs/internal/3d/PERF.md; README for the newer ones).
//
// Each scenario is `run(ctx)` -> {metrics: {name: [values]}, extra}. Values are
// milliseconds unless the name says otherwise; one value per repetition (or
// per event, for the per-step series). `ctx.browser` is drive.mjs's Browser.
// Every scenario first puts the viewer in a known state (pagekit.reset), so
// the order they run in does not matter.
import { netSummary, sleep } from "./drive.mjs";

const KEY_NEXT = { key: "]", code: "BracketRight", keyCode: 221 };

/** Push a value or a list of values onto metrics[name]. */
function put(metrics, name, value) {
    if (value === null || value === undefined) return;
    (metrics[name] ||= []).push(...(Array.isArray(value) ? value : [value]));
}

async function boot(ctx, { navigate = true, z = null } = {}) {
    const { browser } = ctx;
    // `z`: open on that plane (the URL's ?z= wins over the saved view state,
    // which is whatever plane the last page left).
    const query = z === null ? "" : `?z=${z}`;
    if (navigate) await browser.navigate(`${ctx.base}/${encodeURIComponent(ctx.dataset)}${query}`);
    const ok = await browser.waitFor(() => Boolean(window.__bench?.k?.booted?.()), 180000);
    if (!ok) throw new Error("the viewer did not boot");
    await browser.eval(() => __bench.k.quiet(500, 120000));
    const info = await browser.eval(() => {
        const k = __bench.k;
        k.dismissToasts();
        const slots = k.slots().filter((s) => s.name).map((s) => s.index);
        return { depth: k.vs()?.depth || 1, slots, enabled: k.enabled(), spec: k.vs()?.spec?.() || "",
                 hd: Boolean(k.P().seaDragonViewer?.viewerManagerVMain?.isHdMode?.()) };
    });
    ctx.info = info;
    // The channels a run draws: the dataset's choice, else the first three.
    ctx.base = ctx.base;
    ctx.channels = ctx.params.channels || info.slots.slice(0, Math.min(3, info.slots.length));
    ctx.extra = ctx.params.extra ?? info.slots.find((i) => !ctx.channels.includes(i));
    return info;
}

async function reset(ctx, options = {}) {
    return ctx.browser.eval((opts) => __bench.k.reset(opts),
                            { channels: ctx.channels, z: ctx.params.z0 ?? null, ...options });
}

async function period(ctx) {
    if (!ctx.F) ctx.F = await ctx.browser.eval(() => __bench.period(1000));
    return ctx.F;
}

// -- 1. open -> first meaningful image -------------------------------------------

async function open(ctx) {
    const metrics = {};
    await boot(ctx);
    await reset(ctx);
    for (let rep = 0; rep < ctx.reps; rep += 1) {
        const cold = rep === 0;
        if (cold) await ctx.browser.clearCache();
        const mark = ctx.browser.net.length;
        await ctx.browser.navigate(`${ctx.base}/${encodeURIComponent(ctx.dataset)}`);
        const ok = await ctx.browser.waitFor(() => Boolean(window.__bench?.k), 30000);
        if (!ok) throw new Error("no page");
        const r = await ctx.browser.eval(async () => {
            const k = __bench.k;
            const end = performance.now() + 120000;
            let meaningful = null;
            while (performance.now() < end) {
                if (meaningful === null && k.booted() && k.meaningful()) meaningful = performance.now();
                if (meaningful !== null && k.booted() && k.loaded(k.viewer().world)) break;
                await k.sleep(5);
            }
            return { meaningful, full: performance.now() };
        });
        const net = netSummary(ctx.browser.netSince(mark));
        const lut = Math.max(0, ...(net.tile?.phases?.lut || [0]));
        put(metrics, cold ? "tMeaningful_cold" : "tMeaningful_warm", r.meaningful);
        put(metrics, cold ? "tFull_cold" : "tFull_warm", r.full);
        put(metrics, "lutMax", lut);
        if (cold) ctx.log.push({ rep, net });
        await ctx.browser.eval(() => __bench.k.quiet(300, 60000));
    }
    return { metrics };
}

// -- 2/3. channel enable/disable and switch ------------------------------------------

async function channels(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.extra === undefined || ctx.extra === null) return { metrics, skipped: "one channel" };
    await reset(ctx);
    await period(ctx);
    for (let rep = 0; rep < ctx.reps; rep += 1) {
        // enable (the first is cold: this channel was never drawn on the page)
        const on = await ctx.browser.eval(async (extra) => {
            const k = __bench.k;
            __bench.start();
            const before = k.counters();
            const n0 = k.channelItems(k.viewer().world).filter((i) => i.getOpacity() > 0).length;
            const t0 = performance.now();
            k.P().viewerSidebar.setSlotEnabled(extra, true);
            const commit = await __bench.until((sig) => Number((sig.main || "#0").split("#")[1]) > n0, 20000);
            const end = performance.now() + 60000;
            while (performance.now() < end && !k.loaded(k.viewer().world)) await k.sleep(4);
            const ready = performance.now();
            await k.sleep(300);
            return { commit: commit === null ? null : commit - t0, ready: ready - t0,
                     counters: k.delta(before, k.counters()) };
        }, ctx.extra);
        put(metrics, rep === 0 ? "enable_cold_commit" : "enable_commit", on.commit);
        put(metrics, rep === 0 ? "enable_cold_paint" : "enable_paint", on.ready);
        put(metrics, "enable_shaderCompiles", on.counters.shaderCompiles || 0);
        put(metrics, "enable_shaderXhrs", on.counters.shaderXhrs || 0);
        put(metrics, "enable_tileRequests", on.counters["req:tile"] || 0);
        put(metrics, "enable_decodes", on.counters["worker:tileDecoder.js"] || 0);
        await ctx.browser.eval(() => __bench.k.quiet(400));
        // disable
        const off = await ctx.browser.eval(async (extra) => {
            const k = __bench.k;
            const before = k.counters();
            const n0 = k.channelItems(k.viewer().world).filter((i) => i.getOpacity() > 0).length;
            const t0 = performance.now();
            k.P().viewerSidebar.setSlotEnabled(extra, false);
            const commit = await __bench.until((sig) => Number((sig.main || "#0").split("#")[1]) < n0, 20000);
            await k.sleep(200);
            return { commit: commit === null ? null : commit - t0, counters: k.delta(before, k.counters()) };
        }, ctx.extra);
        put(metrics, "disable_commit", off.commit);
        put(metrics, "disable_shaderCompiles", off.counters.shaderCompiles || 0);
        await ctx.browser.eval(() => __bench.k.quiet(400));
        // switch: the last base channel for the extra one, and back
        const sw = await ctx.browser.eval(async (a, b) => {
            const k = __bench.k;
            const sidebar = k.P().viewerSidebar;
            const visible = () => k.channelItems(k.viewer().world).filter((i) => i.getOpacity() > 0);
            const n0 = visible().length;
            const old = new Set(visible());
            const t0 = performance.now();
            sidebar.setSlotEnabled(a, false);
            sidebar.setSlotEnabled(b, true);
            const end = performance.now() + 60000;
            // Time on screen with the switch half done: the old channel gone
            // and the new one not drawn yet (missing, or visible but empty).
            let mixed = 0;
            let last = performance.now();
            while (performance.now() < end) {
                const list = visible();
                const fresh = list.filter((i) => !old.has(i));
                const t = performance.now();
                const half = list.length < n0 || fresh.some((i) => !i.getFullyLoaded());
                if (half) mixed += t - last;
                last = t;
                if (list.length === n0 && fresh.length && !half && k.loaded(k.viewer().world)) break;
                await k.sleep(2);
            }
            const done = performance.now();
            await k.sleep(200);
            sidebar.setSlotEnabled(b, false);
            sidebar.setSlotEnabled(a, true);
            await k.quiet(400);
            return { done: done - t0, mixed };
        }, ctx.channels[ctx.channels.length - 1], ctx.extra);
        put(metrics, "switch_paint", sw.done);
        put(metrics, "switch_mixedMs", sw.mixed);
    }
    await ctx.browser.eval(() => __bench.stop());
    return { metrics, F: ctx.F };
}

// -- 4. Z navigation: single steps, scrub, chunk crossings, flash --------------------------

async function zsteps(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    const z0 = ctx.params.z0 ?? Math.floor(ctx.info.depth / 2) - 10;
    await reset(ctx, { z: Math.max(0, z0) });
    const F = await period(ctx);
    // Single steps, each from rest. TIMED WITH NO PIXEL READBACK: reading
    // the plane's canvas back every frame (the luminance trace) makes Chrome
    // move that canvas off the GPU, and every later draw then pays for it --
    // measured: 25-33 ms frames for the rest of the page's life. So the
    // timings run first, and the luminance trace on a page of its own below.
    const singles = await ctx.browser.eval(async (reps) => {
        const k = __bench.k;
        const vs = k.vs();
        __bench.start();
        const out = [];
        for (let i = 0; i < reps; i += 1) {
            await k.quiet(500);
            await k.sleep(300);
            const c0 = k.counters();
            const t0 = performance.now();
            vs.step(1);
            const r = await k.waitPlane(vs.spec(), t0);
            await k.quiet(300);
            out.push({ ...r, counters: k.delta(c0, k.counters()) });
        }
        __bench.stop();
        return out;
    }, ctx.reps);
    for (const s of singles) {
        put(metrics, "step_commit", s.commit);
        put(metrics, "step_full", s.full);
        put(metrics, "step_shaderCompiles", s.counters.shaderCompiles || 0);
        put(metrics, "step_shaderXhrs", s.counters.shaderXhrs || 0);
    }
    // a scrub: 20 steps 50 ms apart, as a held key would send them
    const scrubMark = ctx.browser.net.length;
    await reset(ctx, { z: Math.max(0, z0) });
    const scrub = await ctx.browser.eval(async (F) => {
        const k = __bench.k;
        const vs = k.vs();
        __bench.reset({ counters: false });
        __bench.start();
        const c0 = k.counters();
        const requested = new Map();
        const t0 = performance.now();
        for (let i = 0; i < 20; i += 1) {
            vs.step(1);
            requested.set(vs.spec(), performance.now());
            await k.sleep(50);
        }
        const inputEnd = performance.now();
        const final = vs.spec();
        await k.waitPlane(final, inputEnd);
        await k.quiet(300);
        const settled = performance.now();
        __bench.stop();
        // what the plane showed, in order
        const painted = __bench.trans.filter(([, s, v]) => s === "main" && v)
            .map(([t, , v]) => ({ t, z: Number(k.views(v)[0]?.replace(/^.*z/, "")) }))
            .filter((p) => Number.isFinite(p.z));
        let stale = 0;
        let top = -Infinity;
        const lat = [];
        for (const p of painted) {
            if (p.z < top) stale += 1;
            top = Math.max(top, p.z);
            const asked = requested.get(`z${p.z}`);
            if (asked !== undefined) lat.push(p.t - asked);
        }
        const fs = k.frameStats(t0, settled, F);
        return { inputEnd: inputEnd - t0, settle: settled - inputEnd, painted: painted.length,
                 stale, lat, fs, applied: __bench.events.filter((e) => e[1] === "plexora:view-changed").length,
                 counters: k.delta(c0, k.counters()) };
    }, F);
    const scrubNet = netSummary(ctx.browser.netSince(scrubMark));
    put(metrics, "scrub_settle", scrub.settle);
    put(metrics, "scrub_paintedPlanes", scrub.painted);
    put(metrics, "scrub_stale", scrub.stale);
    put(metrics, "scrub_paintLatency", scrub.lat);
    put(metrics, "scrub_fps", scrub.fs.fps);
    put(metrics, "scrub_frameP95", scrub.fs.p95);
    put(metrics, "scrub_frameP99", scrub.fs.p99);
    put(metrics, "scrub_dropped", scrub.fs.dropped);
    put(metrics, "scrub_tileRequests", scrubNet.tile?.n || 0);
    put(metrics, "scrub_tileMB", (scrubNet.tile?.bytes || 0) / 1e6);
    put(metrics, "scrub_canceled", scrubNet.tile?.canceled || 0);
    put(metrics, "scrub_shaderCompiles", scrub.counters.shaderCompiles || 0);
    ctx.log.push({ scrubNet: Object.fromEntries(Object.entries(scrubNet).map(([k, v]) => [k, { n: v.n, bytes: v.bytes, canceled: v.canceled }])) });
    // planes across a chunk boundary (Imaris: 64-plane chunks), each from rest
    for (const z of ctx.params.crossings || []) {
        await reset(ctx, { z });
        const r = await ctx.browser.eval(async () => {
            const k = __bench.k;
            const vs = k.vs();
            __bench.start();
            await k.sleep(300);
            const t0 = performance.now();
            vs.step(1);
            const out = await k.waitPlane(vs.spec(), t0);
            __bench.stop();
            return out;
        });
        put(metrics, "crossing_commit", r.commit);
        put(metrics, "crossing_full", r.full);
    }
    // held key (real key events with auto-repeat), headed runs only
    if (ctx.headed) {
        await reset(ctx, { z: Math.max(0, z0) });
        await ctx.browser.eval(() => { __bench.reset({ counters: false }); __bench.start(); return true; });
        const t0 = await ctx.browser.eval(() => performance.now());
        for (let i = 0; i < 30; i += 1) {
            await ctx.browser.key(KEY_NEXT.key, { code: KEY_NEXT.code, keyCode: KEY_NEXT.keyCode,
                                                   repeat: i > 0, up: false });
            await sleep(33);
        }
        await ctx.browser.send("Input.dispatchKeyEvent", { type: "keyUp", key: "]", code: "BracketRight",
                                                           windowsVirtualKeyCode: 221 });
        const held = await ctx.browser.eval(async (t0, F) => {
            const k = __bench.k;
            const end = performance.now();
            await k.waitPlane(k.vs().spec(), end);
            await k.quiet(300);
            __bench.stop();
            return k.frameStats(t0, end, F);
        }, t0, F);
        put(metrics, "held_fps", held.fps);
        put(metrics, "held_frameP95", held.p95);
        put(metrics, "held_frameP99", held.p99);
        put(metrics, "held_dropped", held.dropped);
    }
    // What the screen showed through each step: a page of its own (see above).
    await boot(ctx);
    await reset(ctx, { z: Math.max(0, z0) });
    const traced = await ctx.browser.eval(async (reps) => {
        const k = __bench.k;
        const vs = k.vs();
        __bench.start({ lum: true });
        const out = [];
        for (let i = 0; i < reps; i += 1) {
            await k.quiet(500);
            await k.sleep(300);
            const before = __bench.lum[__bench.lum.length - 1]?.[1];
            const t0 = performance.now();
            vs.step(1);
            const r = await k.waitPlane(vs.spec(), t0);
            await k.quiet(300);
            await k.sleep(400);   // the fade's end
            const after = __bench.lum[__bench.lum.length - 1]?.[1];
            const fl = before && after ? k.flashFrames(t0, t0 + r.full + 400, before, after) : null;
            // The first frame the picture moves at all: a crossfade between
            // two similar planes changes little per frame, so any cell moving
            // by two levels or more, not an average (pagekit lumFirstChange).
            const change = before && after ? k.lumFirstChange(t0, before, 2, after) : null;
            out.push({ flash: fl?.flash ?? null, worst: fl?.worst ?? null, change });
        }
        __bench.stop();
        return out;
    }, ctx.reps);
    for (const s of traced) {
        put(metrics, "step_firstVisibleChange", s.change);
        put(metrics, "step_flashFrames", s.flash);
        put(metrics, "step_flashWorst", s.worst);
    }
    return { metrics, F };
}

// -- 4b. the flash test: two planes, 25 % and 75 % of one window ----------------------------

async function flash(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx, { z: 0 });
    await period(ctx);
    const r = await ctx.browser.eval(async (reps) => {
        const k = __bench.k;
        const vs = k.vs();
        // Both planes mid-tones (the case a translucent fade gets wrong):
        // the whole window, 25 % and 75 % of it.
        const sidebar = k.P().viewerSidebar;
        const slot = sidebar.channelSlots[k.enabled()[0]];
        sidebar.setSlotWindow(slot.index, sidebar.isHdMode() ? [0, 65535] : [0, 255]);
        await k.quiet(400);
        __bench.start({ lum: true, region: [0.3, 0.3, 0.4, 0.4], grid: [8, 8] });
        const out = [];
        for (let i = 0; i < reps * 2; i += 1) {
            await k.quiet(400);
            await k.sleep(300);
            const before = __bench.lum[__bench.lum.length - 1][1];
            const t0 = performance.now();
            vs.set({ z: vs.get().z === 0 ? 1 : 0 });
            const res = await k.waitPlane(vs.spec(), t0);
            await k.sleep(500);
            const after = __bench.lum[__bench.lum.length - 1][1];
            const fl = k.flashFrames(t0, performance.now(), before, after, 3, 0.05);
            const series = __bench.lum.filter(([t]) => t >= t0).map(([, g]) => Math.round(k.meanLum(g)));
            out.push({ flash: fl.flash, worst: fl.worst, from: k.meanLum(before), to: k.meanLum(after),
                       commit: res.commit, series: series.slice(0, 40) });
        }
        __bench.stop();
        return out;
    }, ctx.reps);
    for (const s of r) {
        put(metrics, "flash_frames", s.flash);
        put(metrics, "flash_worst", s.worst);
        put(metrics, "flash_commit", s.commit);
    }
    return { metrics, extra: { series: r.slice(0, 2) } };
}

// -- 5. XY / XZ / YZ synchronisation -------------------------------------------------

async function sync(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx, { layout: "sections" });
    await ctx.browser.eval(() => __bench.k.quiet(800, 120000));
    const F = await period(ctx);
    // z steps: the panes' z-line against the plane's picture
    const steps = await ctx.browser.eval(async (reps) => {
        const k = __bench.k;
        const vs = k.vs();
        __bench.start();
        const out = [];
        for (let i = 0; i < reps; i += 1) {
            await k.quiet(500);
            await k.sleep(200);
            const t0 = performance.now();
            vs.step(1);
            const r = await k.waitPlane(vs.spec(), t0);
            await k.sleep(100);
            const xz = __bench.firstTrans("xzLine", t0);
            const yz = __bench.firstTrans("yzLine", t0);
            const lines = [xz, yz].filter(Boolean).map((x) => x.at - t0);
            out.push({ commit: r.commit, lines });
        }
        __bench.stop();
        return out;
    }, ctx.reps);
    for (const s of steps) {
        if (s.commit === null) continue;
        for (const line of s.lines) put(metrics, "zline_leadMs", s.commit - line);
        put(metrics, "zstep_spread", Math.max(s.commit, ...s.lines) - Math.min(s.commit, ...s.lines));
    }
    // pans: the panes' content against the plane's position
    const box = await ctx.browser.eval(() => {
        const r = document.getElementById("openseadragon").getBoundingClientRect();
        return { x: r.left + r.width * 0.35, y: r.top + r.height * 0.35 };
    });
    for (let rep = 0; rep < ctx.reps; rep += 1) {
        await ctx.browser.eval(() => { __bench.reset({ counters: false }); __bench.start(); return __bench.k.quiet(500); });
        const before = await ctx.browser.eval(() => ({ xz: __bench.sig.xz, yz: __bench.sig.yz, shows: __bench.counters.paneShows || 0 }));
        const dx = rep % 2 ? -120 : 120;
        const moves = [["press", box.x, box.y, 16]];
        for (let i = 1; i <= 10; i += 1) moves.push(["move", box.x + (dx * i) / 10, box.y + (dx * i) / 20, 16]);
        moves.push(["release", box.x + dx, box.y + dx / 2, 0]);
        await ctx.browser.mouse(moves);
        const r = await ctx.browser.eval(async (before) => {
            const k = __bench.k;
            const t0 = performance.now();
            const xz = await __bench.until((sig) => sig.xz && sig.xz !== before.xz, 20000);
            const yz = await __bench.until((sig) => sig.yz && sig.yz !== before.yz, 20000);
            await k.quiet(300, 30000);
            const panes = k.P().sectionPanes.panes;
            const final = { xz: panes.xz.token, yz: panes.yz.token };
            let stale = 0;
            for (const [t, s, v] of __bench.trans) {
                if (t < t0 || (s !== "xz" && s !== "yz") || !v) continue;
                if (!k.views(v).every((x) => x === final[s])) stale += 1;
            }
            __bench.stop();
            return { xz: xz === null ? null : xz - t0, yz: yz === null ? null : yz - t0, stale,
                     shows: (__bench.counters.paneShows || 0) - before.shows };
        }, before);
        put(metrics, "pan_paneLag", Math.max(r.xz ?? 0, r.yz ?? 0));
        put(metrics, "pan_staleTokens", r.stale);
        put(metrics, "pan_paneShows", r.shows);
    }
    await reset(ctx, { layout: "single" });
    return { metrics, F };
}

// -- 6. the 3-D view ---------------------------------------------------------------

/** Enter 3-D and wait until it has drawn with nothing loading (or refused). */
async function enterVolume(ctx) {
    return ctx.browser.eval(async () => {
        const k = __bench.k;
        const c0 = k.counters();
        const t0 = performance.now();
        k.vs().set({ mode: "volume", layout: "single" });
        const end = performance.now() + 180000;
        const d0 = __bench.counters.volumeDraws || 0;
        let ended = null;
        while (performance.now() < end) {
            __bench.hookApp();
            const r = k.P().volumeView?.renderer;
            if (r && r.status === "ready" && !r._loads?.size && (__bench.counters.volumeDraws || 0) > d0) break;
            // Refused or failed: nothing more is coming.
            if (r && ["unavailable", "error"].includes(r.status)) { ended = r.status; break; }
            await k.sleep(2);
        }
        const ready = performance.now() - t0;
        return { ready, ended, counters: k.delta(c0, k.counters()) };
    });
}

async function leaveVolume(ctx) {
    return ctx.browser.eval(async () => {
        __bench.k.vs().set({ mode: "plane" });
        await __bench.k.quiet(400);
        return true;
    });
}

/** The rotation drag: 60 moves right with a wobble, released moving (a coast). */
function rotateDrag(centre) {
    const moves = [["press", centre.x, centre.y, 8]];
    for (let i = 1; i <= 60; i += 1) moves.push(["move", centre.x + 3 * i, centre.y + Math.sin(i / 8) * 20, 8]);
    moves.push(["release", centre.x + 180, centre.y, 0]);
    return moves;
}

async function volume(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx);
    const F = await period(ctx);
    const enter = () => enterVolume(ctx);
    const leave = () => leaveVolume(ctx);
    const entryMark = ctx.browser.net.length;
    const first = await enter();
    // What entering read: every /level response's bytes, its server read
    // phase (Server-Timing `read`), and any the server refused or failed.
    const levels = ctx.browser.netSince(entryMark).filter((r) => r.kind === "level");
    put(metrics, "enter_levelMB", levels.reduce((sum, r) => sum + (r.bytes || 0), 0) / 1e6);
    put(metrics, "enter_serverReadMs", levels.reduce((sum, r) => sum + (r.timing?.read || 0), 0));
    put(metrics, "enter_errors", levels.filter((r) => r.status >= 400 || (r.failed && !r.canceled)).length
        + (first.ended ? 1 : 0));
    if (first.ended) {
        // A stack no level of which fits: there is nothing to rotate.
        put(metrics, "enter_refused", first.ready);
        await leave();
        return { metrics, F, extra: { ended: first.ended } };
    }
    put(metrics, "enter_first", first.ready);
    put(metrics, "enter_levelFetches", first.counters["fetch:level"] || 0);
    put(metrics, "enter_texImage3D", first.counters.texImage3DCalls || 0);
    // Every channel drawn from one level (the renderer's textures, detail aside).
    put(metrics, "volume_levelSpread", await ctx.browser.eval(() => {
        const r = __bench.k.P().volumeView?.renderer;
        const held = [...(r?.textures?.entries?.() || [])]
            .filter(([key]) => !String(key).includes("|D"))
            .map(([, texture]) => texture?.level)
            .filter(Number.isFinite);
        return held.length ? Math.max(...held) - Math.min(...held) : null;
    }));
    // rotation by a real drag, then the coast
    const centre = await ctx.browser.eval(() => {
        const c = __bench.k.P().volumeView.renderer.canvas.getBoundingClientRect();
        return { x: c.left + c.width / 2, y: c.top + c.height / 2 };
    });
    for (let rep = 0; rep < Math.min(ctx.reps, 5); rep += 1) {
        const t0 = await ctx.browser.eval(() => { __bench.reset({ counters: false }); __bench.start(); return performance.now(); });
        await ctx.browser.mouse(rotateDrag(centre));
        const r = await ctx.browser.eval(async (t0, F) => {
            const k = __bench.k;
            const t1 = performance.now();
            const end = t1 + 4000;
            while (performance.now() < end && k.P().volumeView.renderer.animating()) await k.sleep(16);
            const t2 = performance.now();
            await k.sleep(200);
            __bench.stop();
            return { drag: k.frameStats(t0, t1, F), coast: k.frameStats(t1, t2, F),
                     draws: __bench.counters.volumeDraws || 0 };
        }, t0, F);
        put(metrics, "rotate_fps", r.drag.fps);
        put(metrics, "rotate_frameP99", r.drag.p99);
        put(metrics, "rotate_dropped", r.drag.dropped);
        put(metrics, "coast_fps", r.coast.fps);
        put(metrics, "coast_dropped", r.coast.dropped);
    }
    // a channel off and on again in 3-D
    if (ctx.channels.length > 1) {
        for (let rep = 0; rep < Math.min(ctx.reps, 5); rep += 1) {
            const r = await ctx.browser.eval(async (ch) => {
                const k = __bench.k;
                const sidebar = k.P().viewerSidebar;
                const r = k.P().volumeView.renderer;
                __bench.hookApp();
                const timeTo = async (enabled) => {
                    const c0 = k.counters();
                    const d0 = __bench.counters.volumeDraws || 0;
                    const t0 = performance.now();
                    sidebar.setSlotEnabled(ch, enabled);
                    const end = performance.now() + 60000;
                    // drawn with the channel (or without it) and nothing loading
                    while (performance.now() < end) {
                        const drawn = (__bench.counters.volumeDraws || 0) > d0;
                        if (drawn && r.status === "ready" && !r._loads?.size) break;
                        await k.sleep(2);
                    }
                    return { ms: performance.now() - t0, counters: k.delta(c0, k.counters()) };
                };
                const off = await timeTo(false);
                await k.sleep(300);
                const on = await timeTo(true);
                await k.sleep(300);
                return { off, on };
            }, ctx.channels[ctx.channels.length - 1]);
            put(metrics, "toggle3d_off", r.off.ms);
            put(metrics, "toggle3d_on", r.on.ms);
            put(metrics, "toggle3d_levelFetches", (r.on.counters["fetch:level"] || 0) + (r.off.counters["fetch:level"] || 0));
            put(metrics, "toggle3d_texImage3D", (r.on.counters.texImage3DCalls || 0) + (r.off.counters.texImage3DCalls || 0));
        }
    }
    // leave and come back to the same place
    for (let rep = 0; rep < Math.min(ctx.reps, 3); rep += 1) {
        await leave();
        const again = await enter();
        put(metrics, "reenter", again.ready);
        put(metrics, "reenter_levelFetches", again.counters["fetch:level"] || 0);
    }
    // zoomed in at rest: the part seen, finer (volumeRenderer's detail)
    {
        await leave();
        const again = await enter();
        put(metrics, "reenter_beforeZoom", again.ready);
        const at = await ctx.browser.eval(() => {
            const c = __bench.k.P().volumeView.renderer.canvas.getBoundingClientRect();
            __bench.reset({ counters: false });
            __bench.start();
            return { x: c.left + c.width / 2, y: c.top + c.height / 2, t0: performance.now() };
        });
        for (let i = 0; i < 14; i += 1) {
            await ctx.browser.wheel(at.x, at.y, -120);
            await sleep(30);
        }
        const d = await ctx.browser.eval(async (t0, F) => {
            const k = __bench.k;
            const r = k.P().volumeView.renderer;
            const end = performance.now() + 30000;
            let shown = null;
            while (performance.now() < end) {
                if (r.detail && r.detail.lo) { shown = performance.now(); break; }
                await k.sleep(20);
            }
            const rest = performance.now();
            await k.sleep(300);
            __bench.stop();
            const detailKey = [...r.textures.entries()].find(([key]) => key.includes("|D"));
            return { ms: shown === null ? null : shown - t0, fs: k.frameStats(t0, rest, F),
                     gain: detailKey ? r.level - detailKey[1].level : 0, gpu: r.textureBytes };
        }, at.t0, F);
        put(metrics, "detail_ms", d.ms);
        put(metrics, "detail_levelGain", d.gain);
        put(metrics, "detail_fps", d.fs.fps);
        put(metrics, "detail_dropped", d.fs.dropped);
        put(metrics, "volume_gpuMB", d.gpu / 1e6);
        await ctx.browser.eval(() => { const r = __bench.k.P().volumeView.renderer; r.camera.dist = 2.2; r.zoomTarget = null; r.invalidate(); return true; });
    }
    // a reload, then 3-D again: what the browser's cache and the server keep
    await leave();
    await boot(ctx);
    await reset(ctx);
    const mark = ctx.browser.net.length;
    const reload = await enter();
    const net = netSummary(ctx.browser.netSince(mark));
    put(metrics, "reload_volumeReady", reload.ready);
    put(metrics, "reload_levelRequests", net.level?.n || 0);
    put(metrics, "reload_level304", net.level?.s304 || 0);
    put(metrics, "reload_levelMB", (net.level?.bytes || 0) / 1e6);
    await ctx.browser.eval(() => __bench.k.quiet(300).then(() => { __bench.k.vs().set({ mode: "plane" }); return __bench.k.quiet(300); }));
    return { metrics, F };
}

// -- 7. zoom and pan ---------------------------------------------------------------

async function zoom(ctx) {
    const metrics = {};
    await boot(ctx);
    await reset(ctx);
    await period(ctx);
    for (let rep = 0; rep < ctx.reps; rep += 1) {
        const r = await ctx.browser.eval(async () => {
            const k = __bench.k;
            const viewport = k.viewer().viewport;
            await k.quiet(400);
            const measure = async (factor) => {
                const c0 = k.counters();
                const t0 = performance.now();
                viewport.zoomBy(factor, null, true);
                viewport.applyConstraints(true);
                await new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
                const end = performance.now() + 60000;
                while (performance.now() < end && !k.loaded(k.viewer().world)) await k.sleep(4);
                return { ms: performance.now() - t0, counters: k.delta(c0, k.counters()) };
            };
            const zin = await measure(2.5);
            await k.quiet(300);
            const zout = await measure(1 / 2.5);
            await k.quiet(300);
            return { zin, zout };
        });
        put(metrics, "zoomIn_full", r.zin.ms);
        put(metrics, "zoomIn_decodes", r.zin.counters["worker:tileDecoder.js"] || 0);
        put(metrics, "zoomOut_full", r.zout.ms);
        put(metrics, "zoomOut_decodes", r.zout.counters["worker:tileDecoder.js"] || 0);
        put(metrics, "zoomOut_tileRequests", r.zout.counters["req:tile"] || 0);
    }
    return { metrics };
}

// -- 8. contrast drag -> visible on every surface --------------------------------------------

async function contrast(ctx) {
    const metrics = {};
    await boot(ctx);
    await reset(ctx, { layout: ctx.info.depth > 1 ? "sections" : "single" });
    await ctx.browser.eval(() => __bench.k.quiet(800, 120000));
    await period(ctx);
    const watch = async (surfaces) => ctx.browser.eval(async (reps, surfaces) => {
        const k = __bench.k;
        const sidebar = k.P().viewerSidebar;
        const slot = sidebar.channelSlots[k.enabled()[0]];
        const canvases = () => {
            const P = k.P();
            const out = {};
            if (surfaces.includes("main")) out.main = k.viewer().drawer.canvas;
            const panes = P.sectionPanes?.enabled ? P.sectionPanes.panes : null;
            if (panes && surfaces.includes("xz")) out.xz = panes.xz.osd.drawer.canvas;
            if (panes && surfaces.includes("yz")) out.yz = panes.yz.osd.drawer.canvas;
            if (surfaces.includes("volume")) out.volume = P.volumeView?.renderer?.canvas;
            return out;
        };
        const probe = document.createElement("canvas");
        probe.width = 16; probe.height = 16;
        const pc = probe.getContext("2d");
        // A section pane: only the part on screen (probes.js paneCrop) -- a
        // scrolled pane's canvas reaches rows past both edges, never shown.
        const mean = (canvas, name) => {
            const pane = name === "xz" || name === "yz" ? k.P().sectionPanes?.panes?.[name] : null;
            const src = pane ? __bench.paneCrop?.(name, pane, canvas) : null;
            pc.clearRect(0, 0, 16, 16);
            if (src) pc.drawImage(canvas, ...src, 0, 0, 16, 16);
            else pc.drawImage(canvas, 0, 0, 16, 16);
            const d = pc.getImageData(0, 0, 16, 16).data;
            let s = 0;
            for (let i = 0; i < d.length; i += 4) s += d[i] + d[i + 1] + d[i + 2] + d[i + 3];
            return s / (d.length / 4);
        };
        const out = [];
        const original = slot.range.slice();
        for (let i = 0; i < reps; i += 1) {
            await k.quiet(300);
            await k.sleep(200);
            const list = canvases();
            const base = Object.fromEntries(Object.entries(list).map(([n, c]) => [n, mean(c, n)]));
            const c0 = k.counters();
            const lo = original[0];
            const hi = original[1];
            const next = i % 2 === 0 ? [lo, lo + (hi - lo) * 0.6] : original;
            const t0 = performance.now();
            sidebar.setSlotWindow(slot.index, next);
            const seen = {};
            const end = t0 + 3000;
            await new Promise((resolve) => {
                const tick = () => {
                    for (const [n, c] of Object.entries(list)) {
                        if (seen[n] === undefined && Math.abs(mean(c, n) - base[n]) > 0.5) seen[n] = performance.now() - t0;
                    }
                    if (Object.keys(seen).length === Object.keys(list).length || performance.now() > end) resolve();
                    else requestAnimationFrame(() => setTimeout(tick, 0));
                };
                requestAnimationFrame(() => setTimeout(tick, 0));
            });
            out.push({ seen, surfaces: Object.keys(list), counters: k.delta(c0, k.counters()) });
        }
        sidebar.setSlotWindow(slot.index, original);
        return out;
    }, ctx.reps, surfaces);
    const plane = await watch(["main", "xz", "yz"]);
    for (const s of plane) {
        for (const n of s.surfaces) put(metrics, `contrast_${n}`, s.seen[n] ?? 3000);
        put(metrics, "contrast_fetches", (s.counters["req:tile"] || 0) + (s.counters["req:section"] || 0) + (s.counters["req:level"] || 0));
    }
    if (ctx.info.depth > 1) {
        await ctx.browser.eval(async () => {
            const k = __bench.k;
            k.vs().set({ mode: "volume", layout: "single" });
            const end = performance.now() + 120000;
            while (performance.now() < end && !(k.P().volumeView?.renderer?.status === "ready" && !k.P().volumeView.renderer._loads?.size)) await k.sleep(20);
            await k.sleep(500);
        });
        const vol = await watch(["volume"]);
        for (const s of vol) put(metrics, "contrast_volume", s.seen.volume ?? 3000);
        await ctx.browser.eval(() => { __bench.k.vs().set({ mode: "plane" }); return __bench.k.quiet(300); });
    }
    await reset(ctx, { layout: "single" });
    return { metrics };
}

// -- 9/10. a navigation tour: network, decodes, compiles, memory -----------------------------

async function tourOnce(ctx) {
    const b = ctx.browser;
    await reset(ctx);
    await b.eval(async (extra, depth) => {
        const k = __bench.k;
        const vs = k.vs();
        const sidebar = k.P().viewerSidebar;
        for (let i = 0; i < 5 && depth > 1; i += 1) { vs.step(1); await k.sleep(120); }
        await k.quiet(300);
        if (extra !== null && extra !== undefined) {
            sidebar.setSlotEnabled(extra, true); await k.quiet(300);
            sidebar.setSlotEnabled(extra, false); await k.quiet(300);
        }
        const viewport = k.viewer().viewport;
        viewport.zoomBy(2); await k.quiet(300);
        viewport.panBy(new OpenSeadragon.Point(0.05, 0.03)); await k.quiet(300);
        if (depth > 1) {
            vs.set({ layout: "sections" }); await k.quiet(600);
            for (let i = 0; i < 3; i += 1) { vs.step(-1); await k.sleep(150); }
            await k.quiet(300);
            vs.set({ layout: "single", mode: "volume" });
            const end = performance.now() + 120000;
            while (performance.now() < end && !(k.P().volumeView?.renderer?.status === "ready" && !k.P().volumeView.renderer._loads?.size)) await k.sleep(20);
            await k.sleep(300);
            vs.set({ mode: "plane" }); await k.quiet(300);
        }
        viewport.goHome(); await k.quiet(300);
        return true;
    }, ctx.extra ?? null, ctx.info.depth);
}

async function tour(ctx) {
    const metrics = {};
    await boot(ctx);
    const passes = [];
    for (let pass = 0; pass < 2; pass += 1) {
        if (pass === 1) await boot(ctx);   // a reload: what the caches keep
        const mark = ctx.browser.net.length;
        await ctx.browser.eval(() => { __bench.reset(); return true; });
        await tourOnce(ctx);
        const counters = await ctx.browser.eval(() => __bench.k.counters());
        const net = netSummary(ctx.browser.netSince(mark));
        passes.push({ counters, net });
        const tiles = net.tile || { n: 0, disk: 0 };
        const label = pass ? "reload" : "first";
        put(metrics, `${label}_tileRequests`, tiles.n);
        put(metrics, `${label}_tileDiskHits`, tiles.disk);
        put(metrics, `${label}_tileMB`, (tiles.bytes || 0) / 1e6);
        put(metrics, `${label}_sectionRequests`, net.section?.n || 0);
        put(metrics, `${label}_levelRequests`, net.level?.n || 0);
        put(metrics, `${label}_level304`, net.level?.s304 || 0);
        put(metrics, `${label}_shaderCompiles`, counters.shaderCompiles || 0);
        put(metrics, `${label}_shaderXhrs`, counters.shaderXhrs || 0);
        const decodes = counters["worker:tileDecoder.js"] || 0;
        const fetched = (tiles.n || 0) + (net.section?.n || 0);
        put(metrics, `${label}_decodes`, decodes);
        put(metrics, `${label}_imageBitmaps`, counters.imageBitmaps || 0);
        put(metrics, `${label}_decodeRatio`, fetched ? (decodes + (counters.imageBitmaps || 0)) / fetched : 0);
        for (const kind of ["tile", "section", "level"]) {
            for (const [phase, values] of Object.entries(net[kind]?.phases || {})) {
                if (!["read", "lut", "enc", "total", "dec", "wait", "pool", "quant", "hint"].includes(phase)) continue;
                put(metrics, `${label}_${kind}_${phase}`, values);
            }
        }
    }
    return { metrics, extra: { passes: passes.map((p) => ({ counters: p.counters })) } };
}

async function memory(ctx) {
    const metrics = {};
    await boot(ctx);
    const read = async () => {
        await ctx.browser.send("HeapProfiler.collectGarbage").catch(() => null);
        await sleep(300);
        const m = await ctx.browser.metrics();
        const page = await ctx.browser.eval(() => __bench.memory());
        return { heap: m.JSHeapUsedSize, ...page };
    };
    const start = await read();
    put(metrics, "heap_startMB", start.heap / 1e6);
    let peak = start.heap;
    let first = null;
    for (let pass = 0; pass < 3; pass += 1) {
        await tourOnce(ctx);
        const m = await read();
        first ??= m.heap;
        peak = Math.max(peak, m.heap);
        put(metrics, "heap_afterTourMB", m.heap / 1e6);
        put(metrics, "volumeTextureMB", m.volumeBytes / 1e6);
        if (m.glTileBytes !== null) put(metrics, "glTileMB", m.glTileBytes / 1e6);
        if (m.tiles !== null) put(metrics, "osdTiles", m.tiles);
    }
    const end = await read();
    put(metrics, "heap_peakMB", peak / 1e6);
    // after the third tour against after the first: what a session keeps
    put(metrics, "heap_endGrowth", end.heap / Math.max(1, first));
    return { metrics };
}

// -- 2-D parity: a planar image's tile URLs and its settled picture -----------------------

async function parity2d(ctx) {
    await boot(ctx);
    await ctx.browser.eval(() => { __bench.k.viewer().viewport.goHome(true); return __bench.k.quiet(800); });
    const mark = ctx.browser.net.length;
    await ctx.browser.navigate(`${ctx.base}/${encodeURIComponent(ctx.dataset)}`);
    await ctx.browser.waitFor(() => Boolean(window.__bench?.k?.booted?.()), 120000);
    await ctx.browser.eval(() => __bench.k.quiet(1500, 120000));
    await ctx.browser.eval(() => { __bench.k.dismissToasts(); return true; });
    await sleep(500);
    const urls = ctx.browser.netSince(mark).filter((r) => r.kind === "tile")
        .map((r) => r.url.replace(/^https?:\/\/[^/]+/, "").replace(/([?&])v=[^&]*/g, "$1").replace(/[?&]$/, ""))
        .sort();
    const shot = await ctx.browser.send("Page.captureScreenshot", { format: "png", clip: await ctx.browser.eval(() => {
        const r = document.getElementById("openseadragon").getBoundingClientRect();
        return { x: r.left, y: r.top, width: r.width, height: r.height, scale: 1 };
    }) });
    const marks = await ctx.browser.eval(() => (window.__plexora?.perf?.marks?.() || []).length);
    return { metrics: { perfMarks: [marks] }, extra: { urls: [...new Set(urls)], png: shot.result?.data || null } };
}

// -- 4c. far jumps (plan 4.0) -------------------------------------------------------------

/** The plane view at `zoom` ("home", or "L0": one image pixel per screen pixel,
 *  the finest level), on plane `z`, at rest with nothing on the wire. */
async function atZoom(ctx, zoom, z) {
    await reset(ctx, { z });
    let info = null;
    if (zoom === "L0") {
        info = await ctx.browser.eval(async (at) => {
            const k = __bench.k;
            const r = k.zoomToLevel0(1, at);
            await k.quiet(500);
            return { ...r, finest: k.finestDrawn() };
        }, ctx.params.zoomAt || null);
    }
    await ctx.browser.netIdle(300, 8000);
    return info;
}

/** From rest: wait for the view, the plane's standby and the wire to settle. */
async function atRest(ctx) {
    await ctx.browser.eval(() => __bench.k.quiet(500));
    await sleep(300);
    await ctx.browser.netIdle(300, 8000);
}

/** Tile requests (the CDP log) issued within [fromMs, toMs] (epoch ms, the
 *  page's timeOrigin + now()), by the plane their `view=` names: Map(z -> n). */
function tilesByPlane(records, fromMs, toMs) {
    const out = new Map();
    for (const r of records) {
        if (r.kind !== "tile" || !Number.isFinite(r.wall)) continue;
        const at = r.wall * 1000;
        if (at < fromMs || at > toMs) continue;
        const match = /[?&]view=([^&]*)/.exec(r.url);
        const plane = match ? /^(?:t\d+)?z(\d+)$/.exec(decodeURIComponent(match[1])) : null;
        if (!plane) continue;   // a 2-D mask, a projection: not a plane
        const z = Number(plane[1]);
        const first = out.get(z);
        out.set(z, { n: (first?.n || 0) + 1, first: Math.min(first?.first ?? Infinity, at) });
    }
    return out;
}

/** Metrics of one jump (`r` from pagekit's jumpTo) under `name`, `suffix`
 *  naming the zoom ("" home, "_L0" zoomed in to the finest level). */
function jumpMetrics(metrics, name, suffix, r, records) {
    const tiles = tilesByPlane(records, r.from, r.to);
    let wasted = 0;
    for (const [z, entry] of tiles) if (z !== r.origin && z !== r.dest) wasted += entry.n;
    const firstDest = tiles.get(r.dest)?.first;
    put(metrics, `${name}_commit${suffix}`, r.commit);
    put(metrics, `${name}_full${suffix}`, r.full);
    put(metrics, `${name}_wastedTiles${suffix}`, wasted);
    put(metrics, `${name}_originTiles${suffix}`, tiles.get(r.origin)?.n || 0);
    // The app's mark where it has one, else the first destination tile's request.
    put(metrics, `${name}_waitMs${suffix}`,
        r.wait ?? (Number.isFinite(firstDest) ? Math.max(0, firstDest - r.from) : null));
    return Object.fromEntries([...tiles].map(([z, entry]) => [z, entry.n]));
}

async function jump(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 10) return { metrics, skipped: "depth under 10" };
    const F = await period(ctx);
    const top = ctx.info.depth - 1;
    const o = Math.max(0, Math.min(top, ctx.params.z0 ?? Math.floor(ctx.info.depth / 2)));
    let plus = Math.min(top, o + 50);
    if (plus === o) plus = Math.max(0, o - 50);
    const far = plus >= top / 2 ? 0 : top;   // the end farther away
    const route = [plus, far, o].filter((z, i, all) => z !== (i ? all[i - 1] : o));
    // The busy case starts from a plane the other scenarios leave alone, so
    // the step out of it is cold; the jump is the same +50.
    const o2 = Math.max(0, Math.min(top - 1, Math.floor(o / 2)));
    const busyDest = Math.min(top, o2 + 50);
    const extra = { route, o, o2, busyDest, l0: null, tiles: {} };
    for (const zoom of ["home", "L0"]) {
        const suffix = zoom === "L0" ? "_L0" : "";
        for (let rep = 0; rep < Math.min(ctx.reps, 3); rep += 1) {
            // A fresh page and HTTP cache each time: OSD's in-memory tile cache
            // would otherwise carry one pass's planes into the next.
            await ctx.browser.clearCache();
            await boot(ctx, { z: o });
            const info = await atZoom(ctx, zoom, o);
            if (info && !extra.l0) extra.l0 = info;
            await ctx.browser.eval(() => { __bench.reset({ counters: false }); __bench.start(); return true; });
            for (const dest of route) {
                await atRest(ctx);
                const mark = ctx.browser.net.length;
                const r = await ctx.browser.eval((dest) => __bench.k.jumpTo(dest), dest);
                const tiles = jumpMetrics(metrics, "jump", suffix, r, ctx.browser.netSince(mark));
                // What the first jump of each zoom fetched, by plane: a check on the window.
                extra.tiles[zoom] ??= { origin: r.origin, dest: r.dest, tiles };
            }
            // The same jump issued 100 ms into a cold single step (Z3: does it
            // wait behind the revision in flight?).
            if (busyDest > o2 + 1) {
                await atRest(ctx);
                await ctx.browser.clearCache();
                const mark = ctx.browser.net.length;
                const busy = await ctx.browser.eval(async (o2, dest) => {
                    const k = __bench.k;
                    const vs = k.vs();
                    const ta = performance.now();
                    vs.set({ z: o2 });
                    // Loaded, and on at once: its standby has not started.
                    await k.waitPlane(vs.spec(), ta);
                    vs.step(1);
                    const stepSpec = vs.spec();
                    await k.sleep(100);
                    const inFlight = (vs.appliedSpec?.() ?? "") !== stepSpec
                        && !k.shows(__bench.sig.main, stepSpec);
                    return { ...(await k.jumpTo(dest)), inFlight };
                }, o2, busyDest);
                jumpMetrics(metrics, "jumpBusy", suffix, busy, ctx.browser.netSince(mark));
                put(metrics, `jumpBusy_inFlight${suffix}`, busy.inFlight ? 1 : 0);
            }
            await ctx.browser.eval(() => { __bench.stop(); return true; });
        }
    }
    return { metrics, F, extra };
}


// -- 4d. a held key (plan 4.0) --------------------------------------------------------------

const KEY_NEXT_EVENT = { key: "]", code: "BracketRight", windowsVirtualKeyCode: 221,
                         nativeVirtualKeyCode: 221 };

async function held(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 10) return { metrics, skipped: "depth under 10" };
    const F = await period(ctx);
    const top = ctx.info.depth - 1;
    const planes = Math.min(40, top);
    const start = Math.max(0, Math.min(ctx.params.z0 ?? 0, top - planes));
    // The real key where the page takes it (depthStrip's `]`), else the same
    // steps from inside the page at the same cadence.
    await reset(ctx, { z: start });
    await ctx.browser.key("]", { code: "BracketRight", keyCode: 221 });
    const moved = await ctx.browser.eval(async (z) => {
        await __bench.k.sleep(80);
        return __bench.k.vs().get().z !== z;
    }, start);
    const input = moved ? "key" : "step";
    const extra = { input, planes, start };
    for (const zoom of ["home", "L0"]) {
        const suffix = zoom === "L0" ? "_L0" : "";
        for (const rate of [8, 15]) {
            const name = rate === 8 ? "held" : `held${rate}`;
            const interval = 1000 / rate;
            for (let rep = 0; rep < Math.min(ctx.reps, 3); rep += 1) {
                await ctx.browser.clearCache();
                await boot(ctx, { z: start });
                await atZoom(ctx, zoom, start);
                await atRest(ctx);
                const t0 = await ctx.browser.eval(() => {
                    __bench.reset({ counters: false });
                    __bench.start();
                    __bench.heldFrom = { adopted: __bench.k.perfCount("standbyAdopted"),
                                         level: __bench.k.finestDrawn() };
                    __bench.heldUp = null;
                    return performance.now();
                });
                if (input === "key") {
                    // Down, auto-repeats on a fixed clock, up one interval after
                    // the last: the key held for `planes` intervals.
                    const begin = Date.now();
                    for (let i = 0; i < planes; i += 1) {
                        const wait = begin + i * interval - Date.now();
                        if (wait > 0) await sleep(wait);
                        await ctx.browser.key("]", { code: "BracketRight", keyCode: 221,
                                                     repeat: i > 0, up: false });
                    }
                    const wait = begin + planes * interval - Date.now();
                    if (wait > 0) await sleep(wait);
                    await ctx.browser.send("Input.dispatchKeyEvent", { type: "keyUp", ...KEY_NEXT_EVENT });
                } else {
                    await ctx.browser.eval(async (planes, interval) => {
                        const vs = __bench.k.vs();
                        const begin = performance.now();
                        for (let i = 0; i < planes; i += 1) {
                            const wait = begin + i * interval - performance.now();
                            if (wait > 0) await __bench.k.sleep(wait);
                            vs.step(1);
                        }
                        const wait = begin + planes * interval - performance.now();
                        if (wait > 0) await __bench.k.sleep(wait);
                        __bench.heldUp = performance.now();
                        return true;
                    }, planes, interval);
                }
                const r = await ctx.browser.eval(async (t0) => {
                    const k = __bench.k;
                    const vs = k.vs();
                    const evalAt = performance.now();
                    const ups = __bench.inputs.filter(([t, type]) => type === "keyup" && t >= t0 && t <= evalAt);
                    const tUp = __bench.heldUp ?? (ups.length ? ups[ups.length - 1][0] : evalAt);
                    // What the hand had asked for at release, and what the plane showed.
                    let requested = null;
                    for (const [t, name, detail] of __bench.events) {
                        if (name !== "plexora:view-requested" || t > tUp || !detail?.spec) continue;
                        requested = k.planeOf(detail.spec) ?? requested;
                    }
                    requested ??= vs.get().z;
                    let committed = null;
                    for (const [t, surface, value] of __bench.trans) {
                        if (surface !== "main" || t > tUp) continue;
                        committed = k.zOfSig(value) ?? committed;
                    }
                    const r = await k.waitPlane(vs.spec(), tUp);
                    // Settled: the final plane on screen, loaded at the level the
                    // plane drew at rest before the hold.
                    const settle = r.commit === null ? null
                        : await k.waitFull(vs.spec(), tUp, __bench.heldFrom?.level ?? null);
                    const painted = new Set();
                    for (const [t, surface, value] of __bench.trans) {
                        if (surface === "main" && t >= t0) painted.add(k.zOfSig(value));
                    }
                    painted.delete(null);
                    const adopted = k.perfCount("standbyAdopted");
                    const from = __bench.heldFrom?.adopted;
                    __bench.stop();
                    return { lag: committed === null ? null : requested - committed,
                             settle, settleCommit: r.commit, painted: painted.size,
                             adopted: adopted === null || from === null || from === undefined ? null : adopted - from,
                             requested, committed, final: vs.get().z };
                }, t0);
                put(metrics, `${name}_lag${suffix}`, r.lag);
                put(metrics, `${name}_settle${suffix}`, r.settle);
                put(metrics, `${name}_standbyAdopted${suffix}`, r.adopted);
                put(metrics, `${name}_painted${suffix}`, r.painted);
                if (rep === 0) extra[`${name}${suffix}`] = { requested: r.requested, committed: r.committed, final: r.final };
            }
        }
    }
    return { metrics, F, extra };
}

// -- 6b. 3-D flashes: rest after a rotation, filling in, a window handover (plan 3.6) ------

const V3D_LUM = { lum: true, region: [0.25, 0.25, 0.5, 0.5], grid: [16, 12] };

async function volumeFlash(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx);
    const F = await period(ctx);
    // The default camera, not the one the last page saved (a close-up between
    // two cells would leave the traced centre empty): this page's renderer is
    // new, and an empty stored camera is one it does not take.
    await ctx.browser.eval(() => { __bench.k.vs().set({ volume: { camera: {} } }, { save: false }); return true; });
    const first = await enterVolume(ctx);
    if (first.ended) {
        await leaveVolume(ctx);
        return { metrics, F, skipped: `3-D ${first.ended}` };
    }
    const reps = Math.min(ctx.reps, 3);
    const extra = { ghost: false, handover: null, fill: [], series: {} };
    const centre = await ctx.browser.eval(() => {
        const r = __bench.k.P().volumeView.renderer;
        const c = r.canvas.getBoundingClientRect();
        return { x: c.left + c.width / 2, y: c.top + c.height / 2, dist: r.camera?.dist ?? null };
    });
    const startRecording = () => ctx.browser.eval(async (lum) => {
        const k = __bench.k;
        await k.volumeRest(300, 30000);
        __bench.reset({ counters: false });
        __bench.hookApp();
        __bench.start(lum);
        __bench.v3dFrom = { counters: k.counters(), at: performance.now() };
        await k.sleep(200);
        return performance.now();
    }, V3D_LUM);
    // 1. a rotation by a real drag, released moving: the coast, then rest.
    for (let rep = 0; rep < reps; rep += 1) {
        await startRecording();
        await ctx.browser.mouse(rotateDrag(centre));
        const r = await ctx.browser.eval(async () => {
            const k = __bench.k;
            const renderer = k.P().volumeView.renderer;
            const ups = __bench.inputs.filter(([, type]) => type === "pointerup");
            const tRel = ups.length ? ups[ups.length - 1][0] : performance.now();
            const end = performance.now() + 6000;
            while (performance.now() < end && renderer.animating()) await k.sleep(8);
            const tRest = performance.now();
            await k.sleep(700);   // rest + 600 ms, and the last bitmaps in
            __bench.stop();
            const all = k.lumTrace(tRel, tRest + 600, tRest + 400);
            const atRest = k.lumTrace(tRest - 50, tRest + 600, tRest + 400);
            const c = k.delta(__bench.v3dFrom.counters, k.counters());
            return { ...all, settleJump: atRest.jump, captures: c.vghostCaptures || 0,
                     ghostMs: __bench.vghostMs.map(([, ms]) => ms),
                     ghost: Boolean(k.P().volumeView.renderer?.ghost) };
        });
        put(metrics, "v3d_restJump", r.jump);
        put(metrics, "v3d_settleJump", r.settleJump);
        put(metrics, "v3d_flashFrames", r.flash);
        put(metrics, "v3d_blackFrames", r.black);
        put(metrics, "v3d_restFrames", r.frames);
        put(metrics, "v3d_lumRef", r.ref);
        if (rep === 0) extra.series.rest = r.series;
        if (r.ghost) {
            extra.ghost = true;
            put(metrics, "v3d_ghostCaptures", r.captures);
            put(metrics, "v3d_ghostMs", r.ghostMs);
        }
    }
    // 2. wheel in: the brick atlas or the detail sub-volume fills in.
    for (let rep = 0; rep < reps; rep += 1) {
        await ctx.browser.eval(async (dist) => {
            const r = __bench.k.P().volumeView.renderer;
            if (Number.isFinite(dist) && r.camera) { r.camera.dist = dist; r.zoomTarget = null; r.invalidate(); }
            await __bench.k.volumeRest(300, 30000);
            return true;
        }, centre.dist);
        const t0 = await startRecording();
        for (let i = 0; i < 8; i += 1) {
            await ctx.browser.wheel(centre.x, centre.y, -120);
            await sleep(30);
        }
        const r = await ctx.browser.eval(async (t0) => {
            const k = __bench.k;
            const renderer = k.P().volumeView.renderer;
            const end = performance.now() + 6000;
            while (performance.now() < end && renderer.animating()) await k.sleep(8);
            await k.volumeRest(200, 30000);
            const tRest = performance.now();
            await k.sleep(700);
            __bench.stop();
            const c = k.delta(__bench.v3dFrom.counters, k.counters());
            return { ...k.lumTrace(t0 - 100, tRest + 600, tRest + 400), bricks: c["req:brick"] || 0,
                     detail: Boolean(renderer.detail), captures: c.vghostCaptures || 0,
                     ghostMs: __bench.vghostMs.map(([, ms]) => ms), settledIn: tRest - t0 };
        }, t0);
        put(metrics, "v3d_fillFlash", r.flash);
        put(metrics, "v3d_fillBlack", r.black);
        put(metrics, "v3d_fillSettle", r.settledIn);
        if (extra.ghost) {
            put(metrics, "v3d_ghostCaptures", r.captures);
            put(metrics, "v3d_ghostMs", r.ghostMs);
        }
        extra.fill.push({ bricks: r.bricks, detail: r.detail, frames: r.frames, ref: r.ref });
        if (rep === 0) extra.series.fill = r.series;
    }
    // 3. a forced window handover (services/volumeWindows.js adopt), from the
    // starting camera: one shown channel's window made 10 % wider, as exact.
    await ctx.browser.eval(async (dist) => {
        const r = __bench.k.P().volumeView.renderer;
        if (Number.isFinite(dist) && r.camera) { r.camera.dist = dist; r.zoomTarget = null; r.invalidate(); }
        await __bench.k.volumeRest(300, 30000);
        return true;
    }, centre.dist);
    for (let rep = 0; rep < reps; rep += 1) {
        await startRecording();
        const r = await ctx.browser.eval(async () => {
            const k = __bench.k;
            const windows = k.P().volumeWindows;
            const renderer = k.P().volumeView.renderer;
            const name = (renderer.channels?.() || []).map((c) => c.name)
                .find((n) => windows?.peek?.(n));
            if (!name) { __bench.stop(); return { skipped: "no window held" }; }
            const current = windows.peek(name);
            // Redrawn every frame while the handover lands, as anything moving
            // or a contrast drag redraws it (an invalidate): a frame drawn
            // while the channel's new texture is missing is what goes black
            // or fades in from nothing. Left alone, a still view keeps its
            // last picture and shows nothing either way.
            let redrawing = true;
            const redraw = () => {
                if (!redrawing) return;
                renderer.invalidate?.();
                requestAnimationFrame(redraw);
            };
            requestAnimationFrame(redraw);
            const t0 = performance.now();
            windows.adopt(name, { ...current, qmax: current.qmax * 1.1, exact: true });
            await k.sleep(50);
            await k.volumeRest(200, 30000);
            const tRest = performance.now();
            await k.sleep(700);
            redrawing = false;
            __bench.stop();
            const c = k.delta(__bench.v3dFrom.counters, k.counters());
            return { ...k.lumBand(t0, tRest + 600, tRest + 400), name,
                     texmiss: k.perfMarks("volume", "texmiss", t0).length,
                     captures: c.vghostCaptures || 0, ghostMs: __bench.vghostMs.map(([, ms]) => ms),
                     loads: c.volumeLoads || 0, settledIn: tRest - t0 };
        });
        if (r.skipped) {
            extra.handover = r.skipped;
            break;
        }
        put(metrics, "v3d_handoverBlack", r.black);
        put(metrics, "v3d_handoverFlash", r.flash);
        put(metrics, "v3d_handoverSettle", r.settledIn);
        put(metrics, "v3d_texmiss", r.texmiss);
        if (extra.ghost) {
            put(metrics, "v3d_ghostCaptures", r.captures);
            put(metrics, "v3d_ghostMs", r.ghostMs);
        }
        extra.handover = { name: r.name, loads: r.loads, frames: r.frames, before: r.before, ref: r.ref,
                           cells: r.cells };
        if (rep === 0) extra.series.handover = r.series;
    }
    await leaveVolume(ctx);
    return { metrics, F, extra };
}

// -- 5b. resizing the XZ pane by its divider (plan 2.5) ---------------------------------------

async function panesResize(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx, { layout: "sections" });
    await ctx.browser.eval(() => __bench.k.quiet(800, 120000));
    const F = await period(ctx);
    const extra = { labels: [], shots: {} };
    for (let rep = 0; rep < Math.min(ctx.reps, 3); rep += 1) {
        // From the default size each time: a size is saved for the user and
        // outlives the page.
        const at = await ctx.browser.eval(async () => {
            const k = __bench.k;
            const sp = k.P().sectionPanes;
            if (!sp?.enabled || !sp.panes) return null;
            sp.resetSize?.("xz");
            await k.quiet(600, 60000);
            await k.sleep(300);
            __bench.hookApp();
            const divider = document.querySelector(".viewer-section-divider.is-xz");
            if (!divider || divider.hidden) return null;
            const box = divider.getBoundingClientRect();
            if (!(box.width > 0 && box.height > 0)) return null;
            const x = box.left + box.width / 2;
            const y = box.top + box.height / 2;
            const hit = document.elementFromPoint(x, y);
            return { x, y, onDivider: Boolean(hit && (hit === divider || divider.contains(hit))) };
        });
        if (!at) return { metrics, F, skipped: "no XZ divider" };
        extra.onDivider = at.onDivider;
        if (rep === 0) extra.shots.before = await ctx.browser.shot();
        await ctx.browser.eval(() => {
            const wrapper = document.getElementById("openseadragon_wrapper");
            const read = () => parseFloat(wrapper.style.getPropertyValue("--plx-section-xz-h"));
            __bench.reset({ counters: false });
            __bench.start();
            __bench.xzh = [[performance.now(), read()]];
            __bench.xzhObserver?.disconnect();
            __bench.xzhObserver = new MutationObserver(() => {
                const value = read();
                const last = __bench.xzh[__bench.xzh.length - 1];
                if (!last || last[1] !== value) __bench.xzh.push([performance.now(), value]);
            });
            __bench.xzhObserver.observe(wrapper, { attributes: true, attributeFilter: ["style"] });
            return true;
        });
        // Up 200 px in 20 steps, a frame apart: the pane grows.
        const moves = [["press", at.x, at.y, 16]];
        for (let i = 1; i <= 20; i += 1) moves.push(["move", at.x, at.y - 10 * i, 16]);
        moves.push(["release", at.x, at.y - 200, 0]);
        await ctx.browser.mouse(moves);
        const r = await ctx.browser.eval(async (F) => {
            const k = __bench.k;
            const last = (type) => {
                const list = __bench.inputs.filter(([, t]) => t === type);
                return list.length ? list[list.length - 1][0] : null;
            };
            const tDown = last("pointerdown") ?? __bench.xzh[0][0];
            const tUp = last("pointerup") ?? performance.now();
            await k.sleep(Math.max(0, tUp + 2100 - performance.now()));
            __bench.xzhObserver?.disconnect();
            __bench.stop();
            // Pane reloads: shows that started a load (probes.js, by generation).
            const loads = __bench.events.filter(([, name]) => /^(xz|yz):load$/.test(name)).map(([t]) => t);
            const sizes = __bench.xzh.filter(([t]) => t <= tUp).map(([, v]) => v).filter(Number.isFinite);
            let monotonic = 1;
            for (let i = 1; i < sizes.length; i += 1) if (sizes[i] < sizes[i - 1] - 0.5) monotonic = 0;
            return { during: loads.filter((t) => t >= tDown && t <= tUp).length,
                     after: loads.filter((t) => t > tUp && t <= tUp + 2000).length,
                     monotonic, grown: sizes.length ? sizes[sizes.length - 1] - sizes[0] : null,
                     label: k.P().sectionPanes?.panes?.xz?.label?.textContent || "",
                     fs: k.frameStats(tDown, tUp, F), dragMs: tUp - tDown };
        }, F);
        put(metrics, "panes_showsDuring", r.during);
        put(metrics, "panes_showsAfter", r.after);
        put(metrics, "panes_monotonic", r.monotonic);
        put(metrics, "panes_grownPx", r.grown);
        put(metrics, "panes_labelStretch", /z\s*[×x]\s*\d/.test(r.label) ? 1 : 0);
        put(metrics, "resize_frameP95", r.fs.p95);
        put(metrics, "resize_dropped", r.fs.dropped);
        extra.labels.push(r.label);
        if (rep === 0) extra.shots.after = await ctx.browser.shot();
    }
    extra.panes_label = extra.labels[extra.labels.length - 1] ?? null;
    // Leave the default size behind for the scenarios after this one.
    await ctx.browser.eval(async () => {
        __bench.k.P().sectionPanes?.resetSize?.("xz");
        await __bench.k.quiet(400);
        return true;
    });
    await reset(ctx, { layout: "single" });
    return { metrics, F, extra };
}

// -- 3-D Slice & Clip (docs/internal/3d/SLICE_CLIP.md) -----------------------------

/**
 * Cutting the 3-D view: a crop face dragged by its handle with the real mouse
 * (frames, dropped frames, requests -- a cut is uniforms, so none -- and
 * whether the camera moved, which it must not), the same with two axes
 * previewed at once, and a Z section moved through the stack with nothing
 * else drawn: how long until its exact planes are in (`renderer.resolved()`).
 * A build without the cut (no PlexoraInspect) is skipped.
 */
async function clip(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx);
    const F = await period(ctx);
    const first = await enterVolume(ctx);
    if (first.ended) return { metrics, F, skipped: first.ended };
    const can = await ctx.browser.eval(() => Boolean(window.PlexoraInspect && window.PlexoraVolumeHandles));
    if (!can) {
        await leaveVolume(ctx);
        return { metrics, F, skipped: "no cut in this build" };
    }
    // One axis: an X crop's far face, by its grip.
    const start = await ctx.browser.eval(async () => {
        const k = __bench.k;
        const vs = k.vs();
        const info = vs.info || {};
        window.PlexoraInspect.set({ inspect: { mode: "crop", axes: { x: true, y: false, z: false },
                                               crop: { x: [0, Math.round((info.width || 1000) * 0.8)] } } });
        window.PlexoraViewerPrefs?.set?.({ handles: { visible: true } }, { save: false });
        await k.sleep(300);
        const grip = window.PlexoraVolumeHandles.instance?.grips?.get("x:hi")?.group;
        const box = grip?.getBoundingClientRect?.();
        return box && box.width ? { x: box.left + box.width / 2, y: box.top + box.height / 2 } : null;
    });
    if (start) {
        for (let rep = 0; rep < Math.min(ctx.reps, 5); rep += 1) {
            const before = await ctx.browser.eval(() => {
                __bench.reset({ counters: false });
                __bench.start();
                const r = __bench.k.P().volumeView.renderer;
                return { t0: performance.now(), c0: __bench.k.counters(), camera: JSON.stringify(r.cameraState()) };
            });
            const moves = [["press", start.x, start.y, 8]];
            for (let i = 1; i <= 60; i += 1) moves.push(["move", start.x - 2 * i, start.y + (i % 2), 8]);
            moves.push(["release", start.x - 120, start.y, 0]);
            await ctx.browser.mouse(moves);
            const r = await ctx.browser.eval(async (t0, c0, camera, F) => {
                const k = __bench.k;
                const t1 = performance.now();
                await k.sleep(200);
                __bench.stop();
                const renderer = k.P().volumeView.renderer;
                const counts = k.delta(c0, k.counters());
                const out = { fs: k.frameStats(t0, t1, F), fetches: (counts["fetch:level"] || 0) + (counts["fetch:brick"] || 0),
                              turned: JSON.stringify(renderer.cameraState()) !== camera ? 1 : 0 };
                window.PlexoraInspect.undo();
                await k.sleep(150);
                return out;
            }, before.t0, before.c0, before.camera, F);
            put(metrics, "clip_drag_fps", r.fs.fps);
            put(metrics, "clip_drag_frameP95", r.fs.p95);
            put(metrics, "clip_drag_frameP99", r.fs.p99);
            put(metrics, "clip_drag_dropped", r.fs.dropped);
            put(metrics, "clip_drag_fetches", r.fetches);
            put(metrics, "clip_drag_turned", r.turned);
        }
    }
    // Two axes at once (X and Y ranges previewed every frame), against one.
    for (const [name, axes] of [["clip_one", ["x"]], ["clip_two", ["x", "y"]]]) {
        const r = await ctx.browser.eval(async (axes, F) => {
            const k = __bench.k;
            const info = k.vs().info || {};
            const size = { x: info.width || 1000, y: info.height || 1000 };
            const cutter = window.PlexoraInspect;
            cutter.set({ inspect: { mode: "crop", axes: { x: axes.includes("x"), y: axes.includes("y"), z: false } } });
            await k.sleep(200);
            __bench.reset({ counters: false });
            __bench.start();
            const t0 = performance.now();
            for (let i = 0; i < 90; i += 1) {
                const f = 0.5 + 0.4 * Math.sin(i / 10);
                const crop = {};
                for (const axis of axes) crop[axis] = [0, Math.round(size[axis] * f)];
                cutter.preview({ inspect: { crop } });
                await new Promise((resolve) => requestAnimationFrame(resolve));
            }
            const t1 = performance.now();
            cutter.cancel();
            await k.sleep(150);
            __bench.stop();
            return k.frameStats(t0, t1, F);
        }, axes, F);
        put(metrics, `${name}_frameP95`, r.p95);
        put(metrics, `${name}_dropped`, r.dropped);
    }
    // A Z section drawn alone, moved: until its exact planes are in.
    for (let rep = 0; rep < Math.min(ctx.reps, 4); rep += 1) {
        const r = await ctx.browser.eval(async (rep) => {
            const k = __bench.k;
            const vs = k.vs();
            const renderer = k.P().volumeView.renderer;
            const cutter = window.PlexoraInspect;
            const c0 = k.counters();
            const z = Math.max(0, Math.min(vs.depth - 1, Math.round(vs.depth * (0.2 + 0.15 * rep))));
            const t0 = performance.now();
            cutter.set({ inspect: { mode: "section", axes: { x: false, y: false, z: true }, context: 0 }, z });
            await k.sleep(50);
            const end = performance.now() + 30000;
            // The detail (or the bricks) for the plane, once the camera rests.
            while (performance.now() < end) {
                renderer.refine?.();
                if (renderer.resolved() && (renderer.detail?.lo || renderer.atlas?.active?.()
                        || renderer.level === 0)) break;
                await k.sleep(25);
            }
            return { ms: performance.now() - t0, counters: k.delta(c0, k.counters()) };
        }, rep);
        put(metrics, "clip_sectionResolved", r.ms);
        put(metrics, "clip_sectionLevelFetches", r.counters["fetch:level"] || 0);
    }
    await ctx.browser.eval(async () => {
        window.PlexoraInspect.reset("full");
        await __bench.k.sleep(100);
        return true;
    });
    await leaveVolume(ctx);
    return { metrics, F };
}

// -- 7. Z navigation as seen: preview first, never black, three panes in step ----------
//
// zreveal, raildrag and panesync (README). Luminance always through the bitmap
// path (probes.js luminanceAsync), never a read of the plane's canvas.

/** The server's cache counters (`GET /volume/<ds>/perf`), fetched from node,
 *  not the page: null where the route does not exist (404) or does not answer. */
async function serverPerf(ctx) {
    try {
        const reply = await fetch(`${ctx.base}/volume/${encodeURIComponent(ctx.dataset)}/perf`,
                                  { signal: AbortSignal.timeout(10000) });
        if (!reply.ok) return null;
        return await reply.json();
    } catch (error) {
        return null;
    }
}

/** What the server's decoded-chunk and encoded-tile caches did between two
 *  serverPerf() snapshots; null when either is missing. */
function perfDelta(before, after) {
    if (!before || !after) return null;
    const d = (group, key) => {
        const a = Number(after?.[group]?.[key]);
        const b = Number(before?.[group]?.[key]);
        return Number.isFinite(a) && Number.isFinite(b) ? a - b : null;
    };
    const chunkCacheHits = d("chunk_cache", "hits");
    const chunkCacheMisses = d("chunk_cache", "misses");
    const chunks = (chunkCacheHits ?? 0) + (chunkCacheMisses ?? 0);
    return { chunkCacheHits, chunkCacheMisses,
             tileCacheHits: d("tile_cache", "hits"), tileCacheMisses: d("tile_cache", "misses"),
             chunkHitRate: chunks > 0 ? chunkCacheHits / chunks : null };
}

/** Nearest-rank percentile of a list of numbers, or null. */
function percentile(values, p) {
    const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
    return sorted.length ? sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(p * sorted.length) - 1))] : null;
}

/** One window's requests (CDP log) and server caches under `prefix`: plane
 *  tile requests and cancels, section requests, the P95 of the server's
 *  `read` phase and the share its tile cache held (Server-Timing
 *  `cache;desc=hit`) over plane and section tiles both (a read that hit the
 *  cache has no `read`), and perfDelta's cache counts. */
function loadMetrics(metrics, prefix, records, delta) {
    const net = netSummary(records);
    const tile = net.tile || {};
    const section = net.section || {};
    put(metrics, `${prefix}_tileRequests`, tile.n || 0);
    put(metrics, `${prefix}_tileCanceled`, tile.canceled || 0);
    put(metrics, `${prefix}_sectionRequests`, section.n || 0);
    put(metrics, `${prefix}_serverReadP95`,
        percentile([...(tile.phases?.read || []), ...(section.phases?.read || [])], 0.95));
    const hits = (tile.cacheHit || 0) + (section.cacheHit || 0);
    const verdicts = hits + (tile.cacheMiss || 0) + (section.cacheMiss || 0);
    put(metrics, `${prefix}_serverCacheHitShare`, verdicts ? hits / verdicts : null);
    for (const [name, value] of Object.entries(delta || {})) put(metrics, `${prefix}_${name}`, value);
    return net;
}

/**
 * A step and a far jump, as the screen shows them. Warm steps (one plane on,
 * from rest, the page's standby planes in): `step_previewMs` the first frame
 * any cell of the picture moves (lumFirstChange), `step_fullResMs` loaded at
 * the level the plane drew at rest (waitFull), `step_blackFrames` frames under
 * a tenth of the settled picture over [t0, full + 400 ms], `step_statusShows`
 * the times the plane's loading indicator showed (a build with one). Cold
 * jumps (HTTP cache cleared, a fresh page, +50 planes or as far as the stack
 * allows, a different destination each repetition so the server's caches are
 * cold too): `jump_previewMs`, `jump_fullResMs`, `jump_blackFrames`,
 * `jump_statusShows`. Each with its requests and server caches (loadMetrics).
 */
async function zreveal(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    const F = await period(ctx);
    const top = ctx.info.depth - 1;
    const z0 = Math.max(0, Math.min(top, ctx.params.z0 ?? Math.floor(ctx.info.depth / 2)));
    const extra = { z0, steps: [], jumps: [] };
    // Warm steps, the luminance traced on this page throughout.
    await reset(ctx, { z: z0 });
    await atRest(ctx);
    await ctx.browser.eval(() => { __bench.reset({ counters: false }); __bench.start({ lum: true }); return true; });
    for (let rep = 0; rep < ctx.reps; rep += 1) {
        await atRest(ctx);
        const perf0 = await serverPerf(ctx);
        const mark = ctx.browser.net.length;
        const s = await ctx.browser.eval(async () => {
            const k = __bench.k;
            const vs = k.vs();
            await k.sleep(200);   // frames of the picture at rest
            const before = __bench.lum[__bench.lum.length - 1]?.[1] || null;
            const level = k.finestDrawn();
            const shows0 = k.statusShows("main");
            const dir = vs.get().z >= vs.depth - 1 ? -1 : 1;
            const t0 = performance.now();
            vs.step(dir);
            const spec = vs.spec();
            const r = await k.waitPlane(spec, t0);
            const full = r.commit === null ? null : await k.waitFull(spec, t0, level);
            await k.quiet(300, 30000);
            await k.sleep(400);   // the fade's end, and the settled picture
            const after = __bench.lum[__bench.lum.length - 1]?.[1] || null;
            const trace = full === null ? null : k.lumBlack(t0, t0 + full + 400, t0 + full);
            const shows1 = k.statusShows("main");
            return { spec, commit: r.commit, full, preview: k.lumFirstChange(t0, before, 2, after),
                     black: trace?.black ?? null, ref: trace?.ref ?? null,
                     shows: shows0 === null || shows1 === null ? null : shows1 - shows0 };
        });
        loadMetrics(metrics, "step", ctx.browser.netSince(mark), perfDelta(perf0, await serverPerf(ctx)));
        put(metrics, "step_previewMs", s.preview);
        put(metrics, "step_fullResMs", s.full);
        put(metrics, "step_blackFrames", s.black);
        put(metrics, "step_statusShows", s.shows);
        extra.steps.push({ spec: s.spec, commit: s.commit, full: s.full, preview: s.preview, ref: s.ref });
    }
    await ctx.browser.eval(() => { __bench.stop(); return true; });
    // Cold jumps.
    let plus = Math.min(top, z0 + 50);
    if (plus === z0) plus = Math.max(0, z0 - 50);
    if (plus !== z0) {
        const sign = plus > z0 ? 1 : -1;
        // Destinations 4 planes apart (out of the last one's standby), all
        // more than 8 planes from the origin.
        const room = Math.max(1, Math.floor((Math.abs(plus - z0) - 8) / 4) + 1);
        for (let rep = 0; rep < ctx.reps; rep += 1) {
            const dest = plus - sign * 4 * (rep % room);
            await ctx.browser.clearCache();
            await boot(ctx, { z: z0 });
            await reset(ctx, { z: z0 });
            await atRest(ctx);
            await ctx.browser.eval(async () => {
                __bench.reset({ counters: false });
                __bench.start({ lum: true });
                await __bench.k.sleep(300);
                return true;
            });
            const perf0 = await serverPerf(ctx);
            const mark = ctx.browser.net.length;
            const j = await ctx.browser.eval(async (dest) => {
                const k = __bench.k;
                const before = __bench.lum[__bench.lum.length - 1]?.[1] || null;
                const shows0 = k.statusShows("main");
                const r = await k.jumpTo(dest);
                const t0 = r.from - performance.timeOrigin;
                await k.quiet(300, 30000);
                await k.sleep(400);
                const after = __bench.lum[__bench.lum.length - 1]?.[1] || null;
                const trace = r.full === null ? null : k.lumBlack(t0, t0 + r.full + 400, t0 + r.full);
                const shows1 = k.statusShows("main");
                __bench.stop();
                return { dest, commit: r.commit, full: r.full, wait: r.wait,
                         preview: k.lumFirstChange(t0, before, 2, after),
                         black: trace?.black ?? null, ref: trace?.ref ?? null,
                         series: trace?.series?.slice(0, 40) ?? null,
                         shows: shows0 === null || shows1 === null ? null : shows1 - shows0 };
            }, dest);
            loadMetrics(metrics, "jump", ctx.browser.netSince(mark), perfDelta(perf0, await serverPerf(ctx)));
            put(metrics, "jump_previewMs", j.preview);
            put(metrics, "jump_fullResMs", j.full);
            put(metrics, "jump_blackFrames", j.black);
            put(metrics, "jump_statusShows", j.shows);
            extra.jumps.push(j);
        }
    }
    return { metrics, F, extra };
}

//: Rail drags: planes per second, and how far one leg goes.
const DRAG_SPEEDS = { slow: { rate: 3, span: 9 }, fast: { rate: 40, span: 40 } };
const DRAG_MOVE_MS = 16;

/** The Z rail's track on screen: {x, top, height, depth, onRail}, or null. */
async function railBox(ctx) {
    return ctx.browser.eval(() => {
        const k = __bench.k;
        const strip = k.P().depthStrip;
        const track = strip?.track;
        if (!track) return null;
        const r = track.getBoundingClientRect();
        if (!(r.height > 0)) return null;
        const x = r.left + r.width / 2;
        const hit = document.elementFromPoint(x, r.top + r.height / 2);
        const rail = strip.rail || track.parentElement;
        return { x, top: r.top, height: r.height, depth: k.vs().depth,
                 onRail: Boolean(hit && rail && rail.contains(hit)) };
    });
}

/** The pointer's y for plane `z` (fractional): depthStrip.zExact inverted. */
function railY(box, z) {
    return box.top + (z / Math.max(1, box.depth - 1)) * box.height;
}

/** [[ms, plane], ...]: down `span` planes from `start` and back, `sweeps`
 *  times, at `rate` planes/s, a move every DRAG_MOVE_MS. */
function sweepPath(start, span, rate, sweeps) {
    const leg = (span / rate) * 1000;
    const total = 2 * leg * sweeps;
    const path = [];
    for (let ms = 0; ms < total; ms += DRAG_MOVE_MS) {
        const phase = (ms % (2 * leg)) / leg;
        path.push([ms, start + (phase <= 1 ? phase : 2 - phase) * span]);
    }
    path.push([total, start]);
    return path;
}

/** The real mouse along `path` on the rail: a hover, a press, the moves on a
 *  fixed clock (a late one is not made up), the release where it ends. */
async function dragRail(ctx, box, path) {
    const event = (type, z, buttons) => ctx.browser.send("Input.dispatchMouseEvent", {
        type, x: box.x, y: railY(box, z), button: type === "mouseMoved" && !buttons ? "none" : "left",
        buttons, clickCount: type === "mouseMoved" ? 0 : 1 });
    await event("mouseMoved", path[0][1], 0);
    await sleep(60);
    const begin = Date.now();
    await event("mousePressed", path[0][1], 1);
    for (const [ms, z] of path.slice(1)) {
        const wait = begin + ms - Date.now();
        if (wait > 0) await sleep(wait);
        await event("mouseMoved", z, 1);
    }
    await event("mouseReleased", path[path.length - 1][1], 0);
}

/**
 * The Z rail dragged with the real mouse (CDP), as a hand drags it: slow
 * (3 planes/s) and fast (40 planes/s), down a leg and back, with the plane
 * alone and with the section panes on (`_sections`); a stack under 40 planes
 * gets three fast sweeps. Each repetition of each case is two passes on fresh
 * pages (HTTP cache cleared): A without the luminance trace for the frame
 * statistics -- fps, frame P95/P99, dropped frames over the drag; stale frames
 * (staleFrames: the plane committed two or more planes behind the revision the
 * app had started), their share and the largest lag; `handLagMax` (the hand
 * against the plane committed); planes painted (the plane's transitions, the
 * drag and its settling) and requested (the hand's); `settleMs` pointer up to
 * the final plane loaded at the level drawn at rest; the requests and caches
 * (loadMetrics) -- and B with it, for `blackFrames` (under a tenth of the
 * settled picture, from the press to 400 ms after the settle).
 */
async function raildrag(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 4) return { metrics, skipped: "depth under 4" };
    const F = await period(ctx);
    const top = ctx.info.depth - 1;
    const extra = { cases: {} };
    for (const layout of ["single", "sections"]) {
        for (const [speed, { rate, span: wanted }] of Object.entries(DRAG_SPEEDS)) {
            const span = Math.min(wanted, top);
            const start = Math.max(0, Math.min(top - span, ctx.params.z0 ?? Math.floor((top - span) / 2)));
            const sweeps = speed === "fast" && ctx.info.depth < 40 ? 3 : 1;
            const path = sweepPath(start, span, rate, sweeps);
            const name = `drag_${speed}${layout === "sections" ? "_sections" : ""}`;
            extra.cases[name] = { start, span, sweeps, rate, moves: path.length };
            for (let rep = 0; rep < ctx.reps; rep += 1) {
                for (const lum of [false, true]) {
                    await ctx.browser.clearCache();
                    await boot(ctx, { z: start });
                    await reset(ctx, { z: start, layout });
                    if (layout === "sections") await ctx.browser.eval(() => __bench.k.quiet(800, 120000));
                    await atRest(ctx);
                    const box = await railBox(ctx);
                    if (!box) return { metrics, F, extra, skipped: "no Z rail" };
                    extra.cases[name].onRail = box.onRail;
                    const rest = await ctx.browser.eval((lum) => {
                        __bench.reset({ counters: false });
                        __bench.start({ lum });
                        return { level: __bench.k.finestDrawn(), shows: __bench.k.statusShows("main") };
                    }, lum);
                    await sleep(200);
                    const perf0 = lum ? null : await serverPerf(ctx);
                    const mark = ctx.browser.net.length;
                    await dragRail(ctx, box, path);
                    const r = await ctx.browser.eval(async (F, rest, lum) => {
                        const k = __bench.k;
                        const vs = k.vs();
                        const last = (type) => {
                            const list = __bench.inputs.filter(([, t]) => t === type);
                            return list.length ? list[list.length - 1][0] : null;
                        };
                        const tDown = last("pointerdown") ?? __bench.frames[0] ?? performance.now();
                        const tUp = last("pointerup") ?? performance.now();
                        const spec = vs.spec();
                        const w = await k.waitPlane(spec, tUp);
                        const settle = w.commit === null ? null : await k.waitFull(spec, tUp, rest.level);
                        await k.quiet(300, 30000);
                        await k.sleep(400);
                        const tEnd = performance.now();
                        const fs = k.frameStats(tDown, tUp, F);
                        const stale = k.staleFrames(tDown, tUp);
                        const painted = new Set();
                        for (const [t, surface, value] of __bench.trans) {
                            if (surface === "main" && t >= tDown && t <= tEnd) painted.add(k.zOfSig(value));
                        }
                        painted.delete(null);
                        const requested = new Set(__bench.hand.filter(([t]) => t >= tDown && t <= tUp)
                            .map(([, s]) => k.planeOf(s)).filter((z) => z !== null));
                        const trace = lum ? k.lumBlack(tDown, tEnd, tEnd - 400) : null;
                        const shows = k.statusShows("main");
                        __bench.stop();
                        return { fs, stale, handLag: k.handLagMax(tDown, tUp), painted: painted.size,
                                 requested: requested.size, settle, black: trace?.black ?? null,
                                 ref: trace?.ref ?? null, series: trace?.series?.slice(0, 120) ?? null,
                                 dragMs: tUp - tDown, final: spec,
                                 shows: shows === null || rest.shows === null ? null : shows - rest.shows };
                    }, F, rest, lum);
                    if (lum) {
                        put(metrics, `${name}_blackFrames`, r.black);
                        // The plane's means from the press on, to look at.
                        if (rep === 0) extra.cases[name].lum = { ref: r.ref, black: r.black, series: r.series };
                        continue;
                    }
                    put(metrics, `${name}_fps`, r.fs.fps);
                    put(metrics, `${name}_frameP95`, r.fs.p95);
                    put(metrics, `${name}_frameP99`, r.fs.p99);
                    put(metrics, `${name}_dropped`, r.fs.dropped);
                    put(metrics, `${name}_staleFrames`, r.stale?.stale ?? null);
                    put(metrics, `${name}_staleShare`, r.stale?.share ?? null);
                    put(metrics, `${name}_staleMax`, r.stale?.maxLag ?? null);
                    put(metrics, `${name}_handLagMax`, r.handLag);
                    put(metrics, `${name}_paintedPlanes`, r.painted);
                    put(metrics, `${name}_requestedPlanes`, r.requested);
                    put(metrics, `${name}_settleMs`, r.settle);
                    put(metrics, `${name}_statusShows`, r.shows);
                    loadMetrics(metrics, name, ctx.browser.netSince(mark), perfDelta(perf0, await serverPerf(ctx)));
                    if (rep === 0) extra.cases[name].first = { dragMs: r.dragMs, frames: r.fs.frames, final: r.final };
                }
            }
        }
    }
    await reset(ctx, { layout: "single" });
    return { metrics, F, extra };
}

/**
 * The `sync` scenario's pan with the section panes on, watched as pixels:
 * the plane's and each pane's luminance (probes.js, panes too).
 * `pan_planeChangeMs` press to the plane's first visible change;
 * `pan_{xz,yz}LeadMs` the pane's first change minus the plane's (0: in step;
 * positive: the pane trails); `pan_{xz,yz}RecutMs` pointer up to the pane's
 * new section on screen (its token; negative: during the pan);
 * `pan_{xz,yz}BlackFrames` pane frames under a tenth of its settled picture;
 * `pan_{xz,yz}StatusShows` its loading indicator (a build with one); and the
 * requests and caches (loadMetrics, `pan_`).
 */
async function panesync(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx, { layout: "sections" });
    const on = await ctx.browser.eval(async () => {
        await __bench.k.quiet(800, 120000);
        const sp = __bench.k.P().sectionPanes;
        return Boolean(sp?.enabled && sp.panes);
    });
    if (!on) return { metrics, skipped: "no section panes" };
    const F = await period(ctx);
    const box = await ctx.browser.eval(() => {
        const r = document.getElementById("openseadragon").getBoundingClientRect();
        return { x: r.left + r.width * 0.35, y: r.top + r.height * 0.35 };
    });
    const extra = { pans: [] };
    for (let rep = 0; rep < ctx.reps; rep += 1) {
        await atRest(ctx);
        const before = await ctx.browser.eval(async () => {
            const k = __bench.k;
            __bench.reset({ counters: false });
            __bench.start({ lum: true, panes: true });
            await k.sleep(300);   // frames of all three at rest
            return { xz: __bench.sig.xz, yz: __bench.sig.yz,
                     main: __bench.lum[__bench.lum.length - 1]?.[1] ?? null,
                     shows: { xz: k.statusShows("xz"), yz: k.statusShows("yz") } };
        });
        const perf0 = await serverPerf(ctx);
        const mark = ctx.browser.net.length;
        const dx = rep % 2 ? -120 : 120;
        const moves = [["press", box.x, box.y, 16]];
        for (let i = 1; i <= 10; i += 1) moves.push(["move", box.x + (dx * i) / 10, box.y + (dx * i) / 20, 16]);
        moves.push(["release", box.x + dx, box.y + dx / 2, 0]);
        await ctx.browser.mouse(moves);
        const r = await ctx.browser.eval(async (before) => {
            const k = __bench.k;
            const last = (type) => {
                const list = __bench.inputs.filter(([, t]) => t === type);
                return list.length ? list[list.length - 1][0] : null;
            };
            const tDown = last("pointerdown") ?? performance.now();
            const tUp = last("pointerup") ?? performance.now();
            await __bench.until((sig) => sig.xz && sig.xz !== before.xz, 20000);
            await __bench.until((sig) => sig.yz && sig.yz !== before.yz, 20000);
            await k.quiet(300, 30000);
            await k.sleep(400);
            const tEnd = performance.now();
            const plane = k.lumFirstChange(tDown, before.main);
            const out = { plane, panes: {} };
            for (const axis of ["xz", "yz"]) {
                const first = k.paneFirstChange(axis, tDown);
                const recut = __bench.firstTrans(axis, tDown, (value) => Boolean(value) && value !== before[axis]);
                const trace = k.paneTrace(axis, tDown, tEnd, tEnd - 400);
                const shows = k.statusShows(axis);
                out.panes[axis] = {
                    first, lead: first === null || plane === null ? null : first - plane,
                    recut: recut ? recut.at - tUp : null, black: trace.black, frames: trace.frames,
                    ref: trace.ref,
                    shows: shows === null || before.shows[axis] === null ? null : shows - before.shows[axis],
                    // The means from the press on, to look at (black: < ref / 10).
                    series: trace.series.slice(0, 60),
                };
            }
            __bench.stop();
            return out;
        }, before);
        loadMetrics(metrics, "pan", ctx.browser.netSince(mark), perfDelta(perf0, await serverPerf(ctx)));
        put(metrics, "pan_planeChangeMs", r.plane);
        for (const axis of ["xz", "yz"]) {
            const p = r.panes[axis];
            put(metrics, `pan_${axis}LeadMs`, p.lead);
            put(metrics, `pan_${axis}RecutMs`, p.recut);
            put(metrics, `pan_${axis}BlackFrames`, p.black);
            put(metrics, `pan_${axis}StatusShows`, p.shows);
        }
        extra.pans.push(r);
    }
    await reset(ctx, { layout: "single" });
    return { metrics, F, extra };
}

export const SCENARIOS = {
    open: { run: open },
    channels: { run: channels },
    zsteps: { run: zsteps },
    flash: { run: flash },
    sync: { run: sync },
    volume: { run: volume },
    zoom: { run: zoom },
    contrast: { run: contrast },
    tour: { run: tour },
    memory: { run: memory },
    parity2d: { run: parity2d },
    jump: { run: jump },
    held: { run: held },
    volume_flash: { run: volumeFlash },
    panes_resize: { run: panesResize },
    clip: { run: clip },
    zreveal: { run: zreveal },
    raildrag: { run: raildrag },
    panesync: { run: panesync },
};
