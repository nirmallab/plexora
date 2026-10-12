/**
 * videoReadiness.js - no frame is captured while anything is still arriving.
 *
 * After a frame state is applied the viewer needs time: tiles to fetch and
 * decode, a navigation revision to commit on every surface, section panes to
 * hand over, the 3-D renderer to load a level or bricks, a provisional
 * contrast window to be replaced by the exact one. The barrier asks each of
 * those, every 16 ms, and calls the frame ready only when NOTHING is blocking
 * on two consecutive polls. Every view of a multi-view frame passes the same
 * barrier, so the XY plane, its sections and the 3-D view of one frame are
 * always of one state.
 *
 * `blockers(env, options)` names what is in the way (an empty list is
 * ready); `wait(env, options)` polls until ready, or rejects with a
 * ReadinessTimeout naming the signals that never cleared. A hidden tab pauses
 * the clock rather than counting toward the timeout (browsers throttle hidden
 * tabs, and a slow frame is not a failed one).
 *
 * Other plugins may hold a frame back: they listen for
 * `plexora:video-readiness` and call `detail.wait(promise)`.
 *
 * Classic script: `window.PlexoraVideoReadiness`.
 */
(function (global) {
    "use strict";

    const POLL_MS = 16;
    const STABLE_POLLS = 2;

    class ReadinessTimeout extends Error {
        constructor(blocked, waitedMs) {
            super(`the viewer did not settle in ${Math.round(waitedMs / 1000)} s (waiting on: `
                  + `${blocked.join(", ") || "unknown"})`);
            this.name = "ReadinessTimeout";
            this.blocked = blocked;
        }
    }

    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const now = () => (global.performance?.now?.() ?? Date.now());

    /** Items of an OSD viewer that are drawn: not standby copies, not invisible. */
    function shownItems(osd) {
        const world = osd?.world;
        const out = [];
        for (let i = 0; world && i < world.getItemCount(); i += 1) {
            const item = world.getItemAt(i);
            if (!item || item.source?.plexoraStandby) continue;
            if (!(item.getOpacity?.() > 0)) continue;
            out.push(item);
        }
        return out;
    }

    function coarseDrawn(items) {
        for (const item of items) {
            for (const info of item._lastDrawn || []) {
                if (info?.tile?._plexoraCoarse) return true;
            }
        }
        return false;
    }

    function osdBlockers(osd, name, out) {
        if (!osd) return;
        const items = shownItems(osd);
        if (items.some((item) => typeof item.getFullyLoaded === "function" && !item.getFullyLoaded())) {
            out.push(`${name}-tiles`);
        }
        const loader = osd.imageLoader;
        if (loader && (loader.jobsInProgress > 0 || loader.jobQueue?.length > 0)) out.push(`${name}-loader`);
        if (osd.world?.needsDraw?.()) out.push(`${name}-draw`);
        if (coarseDrawn(items)) out.push(`${name}-coarse`);
    }

    function blockers(env, { volume = false, pending = null } = {}) {
        const out = [];
        const vs = env.vs;
        if (vs) {
            if (vs._applying || vs._frame || vs.lateCommits?.() > 0) out.push("view");
            else if (!volume && vs.appliedSpec?.() !== vs.spec?.(vs.get())) out.push("view-spec");
        }
        const sidebar = env.sidebar;
        if ((sidebar?.channelSlots || []).some((slot) => slot && slot.enabled && slot.autoLeveling)) {
            out.push("levels");
        }
        if (volume) {
            const renderer = env.renderer;
            if (!renderer) out.push("volume-renderer");
            else if (!renderer.settled?.()) out.push("volume");
            const windows = env.volumeWindows;
            if (windows?.peek) {
                for (const entry of global.PlexoraVideoSceneState?.slots?.(env) || []) {
                    if (!entry.slot.enabled) continue;
                    const window = windows.peek(entry.fullName);
                    if (window && window.exact === false) {
                        out.push("window-provisional");
                        break;
                    }
                }
            }
        } else {
            const iv = env.iv;
            osdBlockers(iv?.viewer, "main", out);
            if (iv?.tileFetch && iv.tileFetch.idle && !iv.tileFetch.idle()) out.push("fetch");
            const fade = env.planeMotion?.fade;
            if (fade?.idle && !fade.idle()) out.push("fade");
            if (iv?.viewerManagerVMain?.viewSwapPending) out.push("swap");
            if (iv?.overlaysSettled && !iv.overlaysSettled()) out.push("overlay");
            const panes = env.panes;
            if (panes?.enabled && panes.panes && vs?.get?.().layout === "sections") {
                for (const [name, pane] of Object.entries(panes.panes)) {
                    if (!pane || pane.root?.hidden) continue;
                    if (pane.loading) out.push(`pane-${name}-loading`);
                    osdBlockers(pane.osd, `pane-${name}`, out);
                }
            }
        }
        if (env.network && env.network.inFlight > 0) out.push("network");
        if (pending && pending.size) out.push("plugin");
        return out;
    }

    /** Ask other plugins whether they need this frame held back. */
    function pluginWaits(frame) {
        const pending = new Set();
        global.dispatchEvent(new CustomEvent("plexora:video-readiness", {
            detail: {
                frame,
                wait(promise) {
                    const p = Promise.resolve(promise).catch(() => null)
                        .finally(() => pending.delete(p));
                    pending.add(p);
                },
            },
        }));
        return pending;
    }

    /**
     * Resolves `{waitedMs, polls}` once nothing blocks on STABLE_POLLS polls
     * in a row; rejects with ReadinessTimeout after `timeoutMs` of visible
     * time, or with an AbortError when `signal` aborts.
     */
    async function wait(env, { volume = false, timeoutMs = 30000, signal = null, frame = null,
                               onBlocked = null } = {}) {
        const pending = pluginWaits(frame);
        const started = now();
        let visibleMs = 0;
        let last = started;
        let stable = 0;
        let polls = 0;
        let blocked = [];
        for (;;) {
            if (signal?.aborted) throw new DOMException("stopped", "AbortError");
            const t = now();
            if (!document.hidden) visibleMs += t - last;
            last = t;
            blocked = blockers(env, { volume, pending });
            polls += 1;
            if (!blocked.length) {
                stable += 1;
                if (stable >= STABLE_POLLS) return { waitedMs: t - started, polls };
            } else {
                stable = 0;
                onBlocked?.(blocked);
            }
            if (visibleMs > timeoutMs) throw new ReadinessTimeout(blocked, visibleMs);
            await sleep(POLL_MS);
        }
    }

    /**
     * Count the page's own data requests while an export runs (tiles, volume
     * levels, stats, centroids), so the barrier also waits for one the
     * viewer issued that none of the signals above covers. Only those paths:
     * a long-poll or a telemetry beacon must not hold every frame.
     */
    function trackNetwork() {
        const DATA = /\/(generated|node\/v1|volume|centroid|get_image_channel_stats|tiles?)\b/;
        const original = global.fetch;
        const tracker = { inFlight: 0 };
        if (typeof original !== "function") return { tracker, stop() {} };
        global.fetch = function trackedFetch(input, init) {
            const url = typeof input === "string" ? input : input?.url || "";
            if (!DATA.test(url) || url.includes("/plugins/video_studio/")) {
                return original.call(this, input, init);
            }
            tracker.inFlight += 1;
            let settled = false;
            const done = () => {
                if (settled) return;
                settled = true;
                tracker.inFlight -= 1;
            };
            return original.call(this, input, init).then((response) => {
                done();
                return response;
            }, (error) => {
                done();
                throw error;
            });
        };
        return {
            tracker,
            stop() {
                if (global.fetch !== original) global.fetch = original;
            },
        };
    }

    global.PlexoraVideoReadiness = { blockers, wait, trackNetwork, ReadinessTimeout, POLL_MS,
                                     STABLE_POLLS };
})(typeof window !== "undefined" ? window : globalThis);
