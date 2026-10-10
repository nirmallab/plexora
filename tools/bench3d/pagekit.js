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
        while (now() < end && !loaded(viewer().world)) await sleep(4);
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

    B.k = { items, channelItems, loaded, views, shows, coverage, meaningful, booted, settled,
            quiet, dismissToasts, slots, setChannels, enabled, reset, waitPlane, counters,
            delta, frameStats, flashFrames, meanLum, panesLoaded, volumeReady, sleep, now,
            viewer, vs, P };
})();
