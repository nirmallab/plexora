/*
 * pagekit.js - page-side helpers the scenarios call (window.__bench.k).
 *
 * Injected beside probes.js. Everything here reaches the app only through what
 * a user's actions reach: viewState.set, the sidebar's slot methods, the
 * viewport. Waiting is always for something visible: a surface's committed
 * token (probes.js transitions), items fully loaded, the 3-D renderer ready.
 */
(function () {
    "use strict";
    const B = window.__bench;
    if (!B || B.k) return;
    const now = () => performance.now();
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const P = () => window.__plexora || {};
    const viewer = () => P().seaDragonViewer?.viewer;
    const vs = () => P().viewState;

    function items(world) {
        const out = [];
        for (let i = 0; i < (world?.getItemCount?.() || 0); i += 1) out.push(world.getItemAt(i));
        return out;
    }
    function channelItems(world) {
        return items(world).filter((item) => item.source?.tileFormat === 16);
    }
    function loaded(world) {
        const list = items(world);
        return list.length > 0 && list.every((item) => item.getFullyLoaded());
    }
    /** A prefetched plane (planeMotion's standby, a 3-D mask's label
     *  standby): on the world at opacity 0, loading, never drawn. */
    function standby(item) {
        return Boolean(item?.source?.plexoraStandby || item?.source?.plexoraLabelStandby);
    }
    /** loaded(), over what the plane draws or hands over to: the planes
     *  prefetched ahead of the hand are not the plane on screen. */
    function shownLoaded(world) {
        const list = items(world).filter((item) => !standby(item));
        return list.length > 0 && list.every((item) => item.getFullyLoaded());
    }
    /** The views every visible channel item shows, e.g. ["z12"]; [] if none. */
    function views(sig) {
        if (!sig) return [];
        return sig.split("#")[0].split("|").map((v) => v.replace(/^hd:/, ""));
    }
    function shows(sig, token) {
        const v = views(sig);
        return v.length > 0 && v.every((x) => x === token);
    }
    /** The share of the viewport the item's drawn tiles cover (best level). */
    function coverage(item, bounds) {
        const drawn = item._lastDrawn || [];
        const byLevel = new Map();
        const area = bounds.width * bounds.height;
        for (const info of drawn) {
            const tile = info.tile || info;
            if (!tile?.loaded || !tile.positionedBounds) continue;
            const b = tile.positionedBounds;
            const x0 = Math.max(b.x, bounds.x);
            const y0 = Math.max(b.y, bounds.y);
            const x1 = Math.min(b.x + b.width, bounds.x + bounds.width);
            const y1 = Math.min(b.y + b.height, bounds.y + bounds.height);
            if (x1 <= x0 || y1 <= y0) continue;
            const level = info.level ?? tile.level;
            byLevel.set(level, (byLevel.get(level) || 0) + ((x1 - x0) * (y1 - y0)) / area);
        }
        let best = 0;
        for (const share of byLevel.values()) best = Math.max(best, share);
        return Math.min(1, best);
    }
    function meaningful() {
        const v = viewer();
        if (!v) return false;
        const list = channelItems(v.world).filter((item) => item.getOpacity() > 0);
        if (!list.length) return false;
        const bounds = v.viewport.getBounds(true);
        return list.every((item) => coverage(item, bounds) >= 0.5);
    }
    function booted() {
        const state = vs();
        if (!viewer() || !(viewer().world.getItemCount() > 0)) return false;
        // A planar image has no view state at all.
        return !state || state.appliedSpec?.() === state.spec?.();
    }
    function panesLoaded() {
        const sp = P().sectionPanes;
        if (!sp?.enabled || !sp.panes) return true;
        return Object.values(sp.panes).every((pane) => loaded(pane.osd?.world));
    }
    function volumeReady() {
        const state = vs()?.get?.();
        if (state?.mode !== "volume") return true;
        const r = P().volumeView?.renderer;
        return Boolean(r && r.status === "ready" && !(r._loads?.size));
    }
    function settled() {
        return booted() && loaded(viewer().world) && panesLoaded() && volumeReady();
    }
    /** Wait until settled() has held for `hold` ms (max `ms`). */
    async function quiet(hold = 400, ms = 120000) {
        const end = now() + ms;
        let since = null;
        while (now() < end) {
            if (settled()) {
                since ??= now();
                if (now() - since >= hold) return true;
            } else {
                since = null;
            }
            await sleep(20);
        }
        return false;
    }
    function dismissToasts() {
        document.querySelectorAll(".plx-toast-dismiss").forEach((button) => button.click());
    }
    function slots() {
        return P().viewerSidebar?.channelSlots || [];
    }
    /** Enable exactly the slots in `wanted` (indexes). */
    function setChannels(wanted) {
        const sidebar = P().viewerSidebar;
        const set = new Set(wanted);
        for (const slot of slots()) {
            if (!slot.name) continue;
            const on = set.has(slot.index);
            if (Boolean(slot.enabled) !== on) sidebar.setSlotEnabled(slot.index, on);
        }
    }
    function enabled() {
        return slots().filter((slot) => slot.enabled && slot.name).map((slot) => slot.index);
    }
    /** Put the viewer in a known state: plane mode, one layout, a plane, channels. */
    async function reset({ z = null, layout = "single", channels = null, home = true } = {}) {
        const state = vs();
        if (channels) setChannels(channels);
        const depth = state.depth || 1;
        const target = z === null ? Math.floor(depth / 2) : Math.max(0, Math.min(depth - 1, z));
        state.set({ mode: "plane", layout, z: target });
        if (home) viewer().viewport.goHome(true);
        await sleep(50);
        await quiet(500);
        dismissToasts();
        return state.spec();
    }
    /** When the plane shows `token` on every channel item: {commit, full}. */
    async function waitPlane(token, t0, ms = 60000) {
        const commit = await B.until((sig) => shows(sig.main, token), ms);
        if (commit === null) return { commit: null, full: null };
        const end = now() + ms;
        // Not the standby planes (shownLoaded): they are not this plane, and
        // waitFull, which a settle is timed by, starts only once this returns.
        while (now() < end && !shownLoaded(viewer().world)) await sleep(4);
        const firstTrans = B.firstTrans("main", t0, (value) => shows(value, token));
        return { commit: (firstTrans?.at ?? commit) - t0, full: now() - t0 };
    }
    function counters() { return { ...B.counters }; }
    function delta(before, after) {
        const out = {};
        for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
            const d = (after[key] || 0) - (before[key] || 0);
            if (d) out[key] = d;
        }
        return out;
    }
    /** Frame statistics over [t0, t1]: intervals in ms, dropped frames vs F. */
    function frameStats(t0, t1, F) {
        const ts = B.frames.filter((t) => t >= t0 && t <= t1);
        const gaps = ts.slice(1).map((v, i) => v - ts[i]);
        let dropped = 0;
        for (const g of gaps) if (g > 1.5 * F) dropped += Math.round(g / F) - 1;
        const sorted = gaps.slice().sort((a, b) => a - b);
        const pct = (p) => (sorted.length ? sorted[Math.min(sorted.length - 1, Math.ceil(p * sorted.length) - 1)] : null);
        const span = ts.length > 1 ? ts[ts.length - 1] - ts[0] : 0;
        return { frames: ts.length, fps: span ? (1000 * (ts.length - 1)) / span : null,
                 p50: pct(0.5), p95: pct(0.95), p99: pct(0.99), max: sorted[sorted.length - 1] ?? null,
                 dropped };
    }
    /** Luminance excursions in [t0, t1] beyond the band [before, after] per cell. */
    function flashFrames(t0, t1, before, after, eps = 6, cells = 0.02) {
        let flash = 0;
        let worst = 0;
        const frames = B.lum.filter(([t]) => t >= t0 && t <= t1);
        for (const [, grid] of frames) {
            let bad = 0;
            for (let i = 0; i < grid.length; i += 1) {
                const lo = Math.min(before[i], after[i]) - eps;
                const hi = Math.max(before[i], after[i]) + eps;
                const v = grid[i];
                const over = v > hi ? v - hi : (v < lo ? lo - v : 0);
                if (over > 0) bad += 1;
                worst = Math.max(worst, over);
            }
            if (bad / grid.length > cells) flash += 1;
        }
        return { flash, worst, frames: frames.length };
    }
    function meanLum(grid) {
        return grid.reduce((a, b) => a + b, 0) / Math.max(1, grid.length);
    }

    // -- plane tokens, the app's own marks --------------------------------------

    /** The plane of a plane token ("z12", "t3z12"), else null (a projection). */
    function planeOf(token) {
        const match = /^(?:t\d+)?z(\d+)$/.exec(String(token || "").replace(/^hd:/, ""));
        return match ? Number(match[1]) : null;
    }
    /** The plane a surface signature shows, or null. */
    function zOfSig(sig) {
        for (const token of views(sig)) {
            const z = planeOf(token);
            if (z !== null) return z;
        }
        return null;
    }
    function perf() {
        return P().perf || window.PlexoraViewPerf || null;
    }
    /** The app's marks (services/perfProbe.js) `surface:name` at or after `since`;
     *  [] on a build without them. */
    function perfMarks(surface, name, since = -Infinity) {
        const list = perf()?.marks?.() || [];
        return list.filter((m) => m.surface === surface && m.name === name && m.at >= since);
    }
    /** An app counter (services/perfProbe.js), or null on a build without them. */
    function perfCount(name) {
        const counts = perf()?.counters?.();
        return counts ? (counts[name] || 0) : null;
    }

    /** Zoom the plane so one image pixel is `ratio` screen pixels (1: level 0
     *  at 1:1), centred on image pixel `at` (default the image's centre). */
    function zoomToLevel0(ratio = 1, at = null) {
        const v = viewer();
        const item = channelItems(v.world).find((i) => i.getOpacity() > 0) || v.world.getItemAt(0);
        if (!item) return null;
        const size = item.source?.dimensions || item.getContentSize?.();
        const [x, y] = at || [size.x / 2, size.y / 2];
        v.viewport.zoomTo(item.imageToViewportZoom(ratio), null, true);
        v.viewport.panTo(item.imageToViewportCoordinates(x, y), true);
        v.viewport.applyConstraints(true);
        return { zoom: v.viewport.getZoom(true), maxLevel: item.source?.maxLevel ?? null };
    }
    /** The finest pyramid level any visible channel item drew last frame. */
    function finestDrawn() {
        let best = null;
        for (const item of channelItems(viewer()?.world).filter((i) => i.getOpacity() > 0)) {
            for (const info of item._lastDrawn || []) {
                const level = info.level ?? info.tile?.level;
                if (Number.isFinite(level)) best = best === null ? level : Math.max(best, level);
            }
        }
        return best;
    }

    // -- a luminance series: means per frame -------------------------------------

    /** [[t, mean], ...] of the luminance frames in [t0, t1]; over the cells
     *  in `mask` (indexes) only, when given. */
    function lumMeans(t0, t1, mask = null) {
        const mean = mask
            ? (grid) => mask.reduce((sum, i) => sum + grid[i], 0) / mask.length
            : meanLum;
        return B.lum.filter(([t]) => t >= t0 && t <= t1).map(([t, grid]) => [t, mean(grid)]);
    }
    /** The cells the picture over [t0, t1] lights up: at least a quarter of
     *  its brightest cell (per-cell median); every cell if fewer than four. */
    function litCells(t0, t1) {
        return litCellsOf(B.lum.filter(([t]) => t >= t0 && t <= t1).map(([, grid]) => grid));
    }
    function litCellsOf(grids) {
        if (!grids.length) return null;
        const ref = grids[0].map((_, i) => {
            const values = grids.map((grid) => grid[i]).sort((a, b) => a - b);
            return values[Math.floor(values.length / 2)];
        });
        const top = Math.max(...ref);
        const lit = ref.map((v, i) => (v >= Math.max(1, 0.25 * top) ? i : -1)).filter((i) => i >= 0);
        return lit.length >= 4 ? lit : null;
    }
    /** The largest change of mean between consecutive frames. */
    function maxStep(values) {
        let most = 0;
        for (let i = 1; i < values.length; i += 1) most = Math.max(most, Math.abs(values[i] - values[i - 1]));
        return most;
    }
    /**
     * Frames whose mean leaves the band of BOTH its neighbours by more than
     * `eps`, the same way up or down: flashFrames' band test, with the frames
     * either side as "before" and "after" (a moving 3-D view has no fixed
     * before/after). A run of up to two such frames counts each.
     */
    function spikeFrames(values, eps = 6) {
        let n = 0;
        let i = 1;
        while (i < values.length - 1) {
            const a = values[i - 1];
            let hit = 0;
            for (let w = 1; w <= 2 && i + w < values.length; w += 1) {
                const b = values[i + w];
                const run = values.slice(i, i + w);
                if (run.every((x) => x - a > eps && x - b > eps)
                        || run.every((x) => a - x > eps && b - x > eps)) {
                    hit = w;
                    break;
                }
            }
            n += hit;
            i += Math.max(1, hit);
        }
        return n;
    }
    /** Frames darker than `share` of `ref` (null when `ref` is too dark to tell). */
    function darkFrames(values, ref, share = 0.1) {
        if (!(ref > 2)) return null;
        return values.filter((v) => v < share * ref).length;
    }
    /**
     * The luminance means over [from, to] and what they say: the largest
     * frame-to-frame step, spike frames, and frames darker than a tenth of the
     * settled picture (the median mean over [refFrom, to]).
     */
    function lumTrace(from, to, refFrom) {
        const series = lumMeans(from, to).map(([, m]) => m);
        const tail = lumMeans(refFrom, to).map(([, m]) => m).sort((a, b) => a - b);
        const ref = tail.length ? tail[Math.floor(tail.length / 2)] : null;
        return { frames: series.length, ref, jump: maxStep(series), flash: spikeFrames(series),
                 black: ref === null ? null : darkFrames(series, ref),
                 series: series.slice(0, 200).map((m) => Math.round(m * 10) / 10) };
    }

    /**
     * A still picture changing from one state to another over [t0, to]:
     * flashFrames' band test on the mean -- frames outside [before, after]
     * (medians of the frames before t0 and from refFrom) by more than
     * max(2, 5 % of the brighter) -- and frames under a tenth of `after`.
     * Means over the cells the settled picture lights up (litCells), so a
     * sparse sample on a dark ground still moves the mean.
     */
    function lumBand(t0, to, refFrom) {
        const median = (list) => {
            const sorted = list.slice().sort((a, b) => a - b);
            return sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
        };
        const mask = litCells(refFrom, to);
        const before = median(lumMeans(t0 - 150, t0, mask).map(([, m]) => m));
        const after = median(lumMeans(refFrom, to, mask).map(([, m]) => m));
        const series = lumMeans(t0, to, mask).map(([, m]) => m);
        if (before === null || after === null) {
            return { frames: series.length, before, ref: after, flash: null, black: null, series: [] };
        }
        const eps = Math.max(2, 0.05 * Math.max(before, after));
        const lo = Math.min(before, after) - eps;
        const hi = Math.max(before, after) + eps;
        return { frames: series.length, before, ref: after, cells: mask ? mask.length : null,
                 flash: series.filter((m) => m < lo || m > hi).length,
                 black: darkFrames(series, after),
                 series: series.slice(0, 200).map((m) => Math.round(m * 10) / 10) };
    }

    // -- what moved first, what was stale, what the panes showed -------------------

    /**
     * ms from t0 to the first luminance frame (at or after t0) in which any
     * cell differs from `before` by `eps` levels or more: a crossfade between
     * two similar planes changes little per frame, so any cell, not an
     * average. Null when nothing moved -- or, given `after`, when the settled
     * picture does not differ from `before` either (nothing to see move).
     */
    function firstChange(frames, t0, before, eps = 2, after = null) {
        if (!before) return null;
        if (after && !after.some((v, j) => Math.abs(v - before[j]) >= eps)) return null;
        for (const [t, grid] of frames) {
            if (t < t0) continue;
            if (grid.some((v, j) => Math.abs(v - before[j]) >= eps)) return t - t0;
        }
        return null;
    }
    function lumFirstChange(t0, before, eps = 2, after = null) {
        return firstChange(B.lum, t0, before, eps, after);
    }
    /** The pane's last grid before t0 (what it showed at rest), or null. */
    function paneBefore(axis, t0) {
        let grid = null;
        for (const [t, g] of B.lumPanes?.[axis] || []) {
            if (t >= t0) break;
            grid = g;
        }
        return grid;
    }
    /** lumFirstChange over a section pane's grids (probes.js `lumPanes`);
     *  `before` defaults to the pane's last grid before t0. */
    function paneFirstChange(axis, t0, before = null, eps = 2, after = null) {
        return firstChange(B.lumPanes?.[axis] || [], t0, before ?? paneBefore(axis, t0), eps, after);
    }
    /** The mean of the brightest quarter of a grid's cells: what a frame
     *  shows wherever it shows it -- near 0 only when (nearly) all of it is
     *  ground, and unmoved by a picture that slides or changes plane. */
    function brightMean(grid) {
        const sorted = grid.slice().sort((a, b) => b - a);
        const n = Math.max(1, Math.floor(sorted.length / 4));
        let sum = 0;
        for (let i = 0; i < n; i += 1) sum += sorted[i];
        return sum / n;
    }
    /**
     * Black frames in a list of [t, grid]: each frame's brightMean over
     * [from, to], `ref` their median over the settled [refFrom, to], `black`
     * the frames under a tenth of it (null when `ref` is too dark to tell),
     * `jump` the largest frame-to-frame step. Not a mean over the settled
     * picture's lit cells: a pane mid-pan or a plane mid-drag shows other
     * content at those cells without being black.
     */
    function blackTrace(frames, from, to, refFrom) {
        const series = frames.filter(([t]) => t >= from && t <= to).map(([, grid]) => brightMean(grid));
        const tail = frames.filter(([t]) => t >= refFrom && t <= to).map(([, grid]) => brightMean(grid))
            .sort((a, b) => a - b);
        const ref = tail.length ? tail[Math.floor(tail.length / 2)] : null;
        return { frames: series.length, ref, jump: maxStep(series),
                 black: ref === null ? null : darkFrames(series, ref),
                 series: series.slice(0, 200).map((m) => Math.round(m * 10) / 10) };
    }
    /** blackTrace over the plane's luminance (probes.js `lum`). */
    function lumBlack(from, to, refFrom) {
        return blackTrace(B.lum, from, to, refFrom);
    }
    /** blackTrace over a section pane's luminance (probes.js `lumPanes`). */
    function paneTrace(axis, from, to, refFrom) {
        return blackTrace(B.lumPanes?.[axis] || [], from, to, refFrom);
    }
    /** [[at, plane], ...] of the app's `view:<name>` marks (time order). */
    function planeMarks(name) {
        return perfMarks("view", name).map((m) => [m.at, planeOf(m.extra)])
            .filter(([, z]) => z !== null);
    }
    /** The plane of the newest entry of `list` ([[t, plane], ...], time
     *  order) at or before `t`, or null. */
    function planeAt(list, t) {
        let plane = null;
        for (const [at, z] of list) {
            if (at > t) break;
            plane = z;
        }
        return plane;
    }
    /**
     * Frames in [t0, t1] (the frame loop's timestamps) at which the revision
     * the app had started (`view:requested` mark) was `minLag` planes or more
     * from the one it had last committed (`view:committed`): the plane on
     * screen trailing what the pipeline was already fetching. {stale, frames,
     * maxLag, share}; null on a build without the marks.
     */
    function staleFrames(t0, t1, minLag = 2) {
        const requested = planeMarks("requested");
        const committed = planeMarks("committed");
        if (!requested.length || !committed.length) return null;
        let stale = 0;
        let frames = 0;
        let maxLag = 0;
        for (const t of B.frames) {
            if (t < t0 || t > t1) continue;
            const r = planeAt(requested, t);
            const c = planeAt(committed, t);
            if (r === null || c === null) continue;
            frames += 1;
            const lag = Math.abs(r - c);
            maxLag = Math.max(maxLag, lag);
            if (lag >= minLag) stale += 1;
        }
        return { stale, frames, maxLag, share: frames ? stale / frames : null };
    }
    /**
     * The most planes between the hand (every `plexora:view-requested`,
     * probes.js `hand`) and the plane committed (`view:committed` mark; the
     * plane's transitions on a build without marks) at any frame in [t0, t1].
     */
    function handLagMax(t0, t1) {
        const hand = (B.hand || []).map(([t, spec]) => [t, planeOf(spec)]).filter(([, z]) => z !== null);
        let committed = planeMarks("committed");
        if (!committed.length) {
            committed = B.trans.filter(([, surface]) => surface === "main")
                .map(([t, , value]) => [t, zOfSig(value)]).filter(([, z]) => z !== null);
        }
        if (!hand.length || !committed.length) return null;
        let most = null;
        for (const t of B.frames) {
            if (t < t0 || t > t1) continue;
            const h = planeAt(hand, t);
            const c = planeAt(committed, t);
            if (h === null || c === null) continue;
            most = Math.max(most ?? 0, Math.abs(h - c));
        }
        return most;
    }
    /** How many times the surface's loading indicator has shown
     *  (views/paneStatus.js), or null on a build without one. */
    function statusShows(surface) {
        const status = P().paneStatus || window.PlexoraPaneStatus;
        const value = status?.state?.(surface)?.shows;
        return Number.isFinite(value) ? value : null;
    }

    /** The 3-D view at rest: nothing moving, nothing loading (the renderer's
     *  own settled() where the build has one), held for `hold` ms. */
    async function volumeRest(hold = 200, ms = 30000) {
        const end = now() + ms;
        let since = null;
        while (now() < end) {
            const r = P().volumeView?.renderer;
            const still = r && !r.animating?.() && !r.interacting;
            const ready = r && (typeof r.settled === "function" ? r.settled()
                : (r.status === "ready" && !r._loads?.size));
            if (still && ready) {
                since ??= now();
                if (now() - since >= hold) return true;
            } else {
                since = null;
            }
            await sleep(16);
        }
        return false;
    }

    /**
     * Jump the plane to `dest` through the app's entry point (viewState.set)
     * and time it: commit and full as waitPlane; `wait` until the destination's
     * revision started (perfProbe's `view:requested` mark, null on a build
     * without marks); `from`/`to` the request and the full load as epoch ms,
     * to match the CDP request log's wall times. Recording must be on.
     */
    async function jumpTo(dest) {
        const state = vs();
        const origin = zOfSig(B.sig.main) ?? state.get().z;
        const level = finestDrawn();   // the view's level, as the plane at rest drew it
        const t0 = now();
        state.set({ z: dest });
        const spec = state.spec();
        const r = await waitPlane(spec, t0);
        const full = r.commit === null ? null : await waitFull(spec, t0, level);
        const started = perfMarks("view", "requested", t0).find((m) => m.extra === spec);
        return { origin, dest, spec, level, commit: r.commit, full,
                 wait: started ? started.at - t0 : null,
                 from: performance.timeOrigin + t0,
                 to: performance.timeOrigin + t0 + (full ?? 60000) };
    }

    /**
     * Once `token` is on screen: until every item is fully loaded AND drawing
     * at least `level` (the finest level the plane drew at rest before; null:
     * any), for two polls running. ms from t0, or null after `ms`.
     *
     * waitPlane's `full` alone can end at the swap: items handed over while
     * they cover the view at a coarser level report fully loaded before their
     * sharpening has asked for a tile.
     *
     * Standby planes (shownLoaded) are not waited for: after a fast drag they
     * are built while the plane is still sharpening, and six of them loading
     * every level read as a settle of ~0.7-1 s whenever they made it onto the
     * world before the plane's last tile -- a race, not the plane's time.
     */
    async function waitFull(token, t0, level = null, ms = 60000) {
        const end = now() + ms;
        let since = null;
        let polls = 0;
        while (now() < end) {
            const done = shows(B.sig.main, token) && shownLoaded(viewer().world)
                && (level === null || (finestDrawn() ?? -1) >= level);
            if (done) {
                since ??= now();
                polls += 1;
                if (polls >= 2) return since - t0;
            } else {
                since = null;
                polls = 0;
            }
            await sleep(4);
        }
        return null;
    }

    B.k = { items, channelItems, loaded, standby, shownLoaded, views, shows, coverage, meaningful, booted, settled,
            quiet, dismissToasts, slots, setChannels, enabled, reset, waitPlane, counters,
            delta, frameStats, flashFrames, meanLum, panesLoaded, volumeReady, sleep, now,
            viewer, vs, P, planeOf, zOfSig, perfMarks, perfCount, zoomToLevel0, finestDrawn,
            lumMeans, maxStep, spikeFrames, darkFrames, lumTrace, lumBand, volumeRest, jumpTo,
            waitFull, lumFirstChange, paneFirstChange, paneBefore, litCellsOf, brightMean,
            blackTrace, lumBlack, paneTrace, planeMarks, staleFrames, handLagMax, statusShows };
})();
