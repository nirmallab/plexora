// scenarios.mjs - the ten benchmark scenarios (docs/internal/3d/PERF.md).
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

async function boot(ctx, { navigate = true } = {}) {
    const { browser } = ctx;
    if (navigate) await browser.navigate(`${ctx.base}/${encodeURIComponent(ctx.dataset)}`);
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
    // single steps, each from rest, with the screen's luminance recorded
    const singles = await ctx.browser.eval(async (reps, F) => {
        const k = __bench.k;
        const vs = k.vs();
        __bench.start({ lum: true });
        const out = [];
        for (let i = 0; i < reps; i += 1) {
            await k.quiet(500);
            await k.sleep(300);
            const before = __bench.lum[__bench.lum.length - 1]?.[1];
            const c0 = k.counters();
            const t0 = performance.now();
            vs.step(1);
            const token = vs.spec();
            const r = await k.waitPlane(token, t0);
            await k.quiet(300);
            await k.sleep(400);   // the fade's end
            const after = __bench.lum[__bench.lum.length - 1]?.[1];
            const fl = before && after ? k.flashFrames(t0, t0 + r.full + 400, before, after) : null;
            let change = null;
            if (before) {
                for (const [t, grid] of __bench.lum) {
                    if (t < t0) continue;
                    const d = grid.reduce((s, v, j) => s + Math.abs(v - before[j]), 0) / grid.length;
                    if (d > 1) { change = t - t0; break; }
                }
            }
            out.push({ ...r, flash: fl?.flash ?? null, worst: fl?.worst ?? null, change,
                       counters: k.delta(c0, k.counters()) });
        }
        __bench.stop();
        return out;
    }, ctx.reps, F);
    for (const s of singles) {
        put(metrics, "step_commit", s.commit);
        put(metrics, "step_full", s.full);
        put(metrics, "step_firstVisibleChange", s.change);
        put(metrics, "step_flashFrames", s.flash);
        put(metrics, "step_flashWorst", s.worst);
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

async function volume(ctx) {
    const metrics = {};
    await boot(ctx);
    if (ctx.info.depth < 2) return { metrics, skipped: "no depth" };
    await reset(ctx);
    const F = await period(ctx);
    const enter = async () => ctx.browser.eval(async () => {
        const k = __bench.k;
        const c0 = k.counters();
        const t0 = performance.now();
        k.vs().set({ mode: "volume", layout: "single" });
        const end = performance.now() + 180000;
        const d0 = __bench.counters.volumeDraws || 0;
        while (performance.now() < end) {
            __bench.hookApp();
            const r = k.P().volumeView?.renderer;
            if (r && r.status === "ready" && !r._loads?.size && (__bench.counters.volumeDraws || 0) > d0) break;
            await k.sleep(2);
        }
        const ready = performance.now() - t0;
        return { ready, counters: k.delta(c0, k.counters()) };
    });
    const leave = async () => ctx.browser.eval(async () => {
        __bench.k.vs().set({ mode: "plane" });
        await __bench.k.quiet(400);
        return true;
    });
    const first = await enter();
    put(metrics, "enter_first", first.ready);
    put(metrics, "enter_levelFetches", first.counters["fetch:level"] || 0);
    put(metrics, "enter_texImage3D", first.counters.texImage3DCalls || 0);
    // rotation by a real drag, then the coast
    const centre = await ctx.browser.eval(() => {
        const c = __bench.k.P().volumeView.renderer.canvas.getBoundingClientRect();
        return { x: c.left + c.width / 2, y: c.top + c.height / 2 };
    });
    for (let rep = 0; rep < Math.min(ctx.reps, 5); rep += 1) {
        const t0 = await ctx.browser.eval(() => { __bench.reset({ counters: false }); __bench.start(); return performance.now(); });
        const moves = [["press", centre.x, centre.y, 8]];
        for (let i = 1; i <= 60; i += 1) moves.push(["move", centre.x + 3 * i, centre.y + Math.sin(i / 8) * 20, 8]);
        moves.push(["release", centre.x + 180, centre.y, 0]);
        await ctx.browser.mouse(moves);
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
        const mean = (canvas) => {
            pc.clearRect(0, 0, 16, 16);
            pc.drawImage(canvas, 0, 0, 16, 16);
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
            const base = Object.fromEntries(Object.entries(list).map(([n, c]) => [n, mean(c)]));
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
                        if (seen[n] === undefined && Math.abs(mean(c) - base[n]) > 0.5) seen[n] = performance.now() - t0;
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
};
