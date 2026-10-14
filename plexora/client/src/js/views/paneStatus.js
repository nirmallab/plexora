/**
 * paneStatus.js - a small light on each of the three views while it loads.
 *
 * WHAT IT SHOWS. A z-stack on screen is up to three pictures: the plane, and
 * beside it the XZ and YZ sections (views/sectionPanes.js). Each says how its
 * loading goes in a `plexora:surface-status` event on `window`
 * (viewerManager.planeSwapGroup for the plane, the panes for theirs):
 *
 *     detail = {surface: "main" | "xz" | "yz", rev, spec | token,
 *               phase: "idle" | "fetching" | "preview" | "refining"
 *                      | "slow" | "error",
 *               loaded, needed, failed, level, since, retry}
 *
 * and this draws one quiet pill per view: a breathing dot while the new
 * picture is on its way (fetching, slow), a ring filling as a preview
 * sharpens (preview, refining -- when the counts mean something), a retry
 * button when it could not finish (error). The plane's sits just above the
 * depth strip, the band nothing else uses, and is hidden in 3-D (the cut
 * panel has its own dot); XZ's at the top right of the part of it that shows;
 * YZ's at its bottom centre. No words: the navbar's chip says what is
 * loading, to assistive tech too, so nothing here is live.
 *
 * WHEN. Timers in JS, not CSS delays, so the probe can drive them:
 *
 * - a status that is not idle shows the pill after DELAY_MS; idle before
 *   then cancels it, so a step that lands in a blink never shows anything;
 * - once up it stays at least MIN_VISIBLE_MS, then fades over FADE_MS;
 * - an error shows at once and stays until a status replaces it.
 *
 * The ring's fraction never goes back within one revision (more tiles needed
 * at a finer level is not less loaded); a new revision starts it again.
 *
 * ONE STATUS, ONE TRUTH. For the plane, the depth strip's breathing thumb is
 * driven from the same status (depthStrip.loadingSource), so the thumb and
 * the pill never disagree. Until the first status arrives the strip keeps its
 * own reading (requested spec vs applied spec).
 *
 * Mounts resolve lazily: nothing is built until a view first says it is not
 * idle, so a planar project (no strip, no events) never gets a node.
 *
 * Exposes window.PlexoraPaneStatus = {PaneStatus, DELAY_MS, MIN_VISIBLE_MS,
 * FADE_MS, meaningful}; main.js builds one as __plexora.paneStatus.
 * `state(surface)` is what the bench (tools/bench3d) reads.
 */
(function (global) {
    "use strict";

    const DELAY_MS = 220;
    const MIN_VISIBLE_MS = 300;
    //: The opacity transition in viewer.css (.plx-pane-status) is the same.
    const FADE_MS = 150;

    const EVENT = "plexora:surface-status";
    const SURFACES = ["main", "xz", "yz"];
    const PHASES = new Set(["idle", "fetching", "preview", "refining", "slow", "error"]);
    //: Phases whose picture is on screen and sharpening: the ring, if the
    //: counts mean something. Before that (fetching, slow) it is the dot.
    const RING_PHASES = new Set(["preview", "refining"]);
    //: Phases during which the plane asked for is not on screen yet: the
    //: depth strip's thumb breathes.
    const THUMB_PHASES = new Set(["fetching", "slow"]);
    //: 2 * PI * 5.5, the ring's circumference (viewBox 0 0 14 14, r 5.5).
    const ARC = 34.56;
    const NS = "http://www.w3.org/2000/svg";
    const WHAT = { main: "plane", xz: "XZ section", yz: "YZ section" };

    /** Whether `loaded` of `needed` is worth a ring: at least two tiles,
     *  and a count that is a count. */
    function meaningful(loaded, needed) {
        return Number.isFinite(loaded) && Number.isFinite(needed)
            && needed >= 2 && loaded >= 0 && loaded <= needed;
    }

    function svg(doc, tag, attrs = {}) {
        const node = doc.createElementNS(NS, tag);
        for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
        return node;
    }

    class PaneStatus {
        constructor({
            mounts = {}, busyHosts = {}, strip = null, now = null, setTimeout: setT = null,
            clearTimeout: clearT = null, requestAnimationFrame: raf = null, target = null,
            document: doc = null,
        } = {}) {
            //: surface -> () => the element the pill is put in, or null.
            this.mounts = mounts;
            //: surface -> () => the element that is aria-busy while it loads.
            this.busyHosts = busyHosts;
            //: () => the depth strip (views/depthStrip.js), or null.
            this.strip = typeof strip === "function" ? strip : () => strip;
            const timeoutFn = setT || global.setTimeout;
            const clearFn = clearT || global.clearTimeout;
            const frameFn = raf || global.requestAnimationFrame;
            this._now = now || (() => (global.performance?.now ? global.performance.now() : Date.now()));
            this._set = (fn, ms) => timeoutFn.call(global, fn, ms);
            this._clear = (id) => { if (id !== null && id !== undefined) clearFn.call(global, id); };
            this._frame = frameFn ? (fn) => frameFn.call(global, fn) : (fn) => this._set(fn, 16);
            this.target = target || global;
            this.document = doc || global.document;
            this.surfaces = {};
            for (const surface of SURFACES) this.surfaces[surface] = this._blank();
            this._listener = (event) => this.onStatus(event?.detail);
            this._mounted = false;
            //: What the depth strip reads to decide whether its thumb breathes.
            this.loadingSource = () => THUMB_PHASES.has(this.surfaces.main.phase);
        }

        _blank() {
            return {
                node: null, dot: null, ring: null, arc: null, retry: null,
                phase: "idle", status: null, retryFn: null, mode: null,
                visible: false, shownAt: 0, shows: 0, frame: 0,
                delayTimer: null, hideTimer: null, fadeTimer: null,
                rev: undefined, tag: undefined, best: null,
            };
        }

        mount() {
            if (this._mounted) return false;
            this._mounted = true;
            this.target?.addEventListener?.(EVENT, this._listener);
            return true;
        }

        unmount() {
            if (this._mounted) this.target?.removeEventListener?.(EVENT, this._listener);
            this._mounted = false;
            for (const surface of SURFACES) {
                const s = this.surfaces[surface];
                this._clearTimers(s);
                s.frame += 1;           // a show's pending frame does nothing
                s.node?.remove?.();
                if (s.status) this.busyHosts[surface]?.()?.setAttribute?.("aria-busy", "false");
                this.surfaces[surface] = this._blank();
            }
            const strip = this.strip();
            if (strip && strip.loadingSource === this.loadingSource) {
                strip.loadingSource = null;
                strip.paintLoading?.(false);
            }
        }

        /** {visible, phase, ratio, shows} for one view; `ratio` is the ring's
         *  fraction this revision (null without counts that mean something),
         *  `shows` how often the pill has appeared since the page opened. */
        state(surface) {
            const s = this.surfaces[surface];
            if (!s) return null;
            return { visible: s.visible, phase: s.phase, ratio: s.best, shows: s.shows };
        }

        /** One view's status (the event's detail). */
        onStatus(detail) {
            if (!detail || !SURFACES.includes(detail.surface) || !PHASES.has(detail.phase)) return;
            const surface = detail.surface;
            const s = this.surfaces[surface];
            const phase = detail.phase;
            // The ring's fraction, held for one revision.
            const tag = detail.token !== undefined ? detail.token : detail.spec;
            if (detail.rev !== s.rev || tag !== s.tag) {
                s.rev = detail.rev;
                s.tag = tag;
                s.best = null;
            }
            const loaded = Number(detail.loaded);
            const needed = Number(detail.needed);
            const counts = meaningful(loaded, needed);
            if (counts) s.best = Math.max(s.best === null ? 0 : s.best, loaded / needed);
            const wasError = s.phase === "error";
            s.status = detail;
            s.phase = phase;
            s.retryFn = typeof detail.retry === "function" ? detail.retry : null;
            this._sideEffects(surface, phase);
            if (phase === "idle") {
                this._settle(surface, wasError);
                return;
            }
            this._ensure(surface);
            this._paint(surface, counts && RING_PHASES.has(phase));
            this._busy(surface, phase);
        }

        // -- what a status does besides the pill ---------------------------------

        _sideEffects(surface, phase) {
            this.busyHosts[surface]?.()?.setAttribute?.("aria-busy", phase !== "idle" ? "true" : "false");
            if (surface !== "main") return;
            const strip = this.strip();
            if (!strip) return;
            // From the first status on, the strip's thumb reads it too (paint).
            if (strip.loadingSource !== this.loadingSource) strip.loadingSource = this.loadingSource;
            strip.paintLoading?.(THUMB_PHASES.has(phase));
        }

        // -- timers -------------------------------------------------------------

        _busy(surface, phase) {
            const s = this.surfaces[surface];
            // Busy again before the hold ran out: it stays up.
            this._clear(s.hideTimer);
            s.hideTimer = null;
            if (phase === "error") {
                this._clear(s.delayTimer);
                s.delayTimer = null;
                if (!s.visible) this._show(surface);
                return;
            }
            if (s.visible) return;
            // Still fading out: back up at once rather than out and in again.
            if (s.fadeTimer !== null) {
                this._show(surface);
                return;
            }
            if (s.delayTimer !== null) return;
            s.delayTimer = this._set(() => {
                s.delayTimer = null;
                if (s.phase !== "idle") this._show(surface);
            }, DELAY_MS);
        }

        _settle(surface, wasError) {
            const s = this.surfaces[surface];
            if (s.delayTimer !== null) {
                // Landed inside the delay: never shown.
                this._clear(s.delayTimer);
                s.delayTimer = null;
            }
            if (!s.visible || !s.node) return;
            s.node.dataset.phase = "idle";
            // Loaded: a ring held up for the rest of its time is a full one.
            if (s.mode === "ring") this._ratio(s, 1);
            if (wasError) {
                // An error is never taken down by a timer, and once its view
                // has loaded there is nothing to hold a retry button up for.
                s.retry.hidden = true;
                s.node.setAttribute("aria-hidden", "true");
                this._hide(surface);
                return;
            }
            if (s.hideTimer !== null) return;
            const wait = Math.max(0, MIN_VISIBLE_MS - (this._now() - s.shownAt));
            if (wait <= 0) {
                this._hide(surface);
                return;
            }
            s.hideTimer = this._set(() => {
                s.hideTimer = null;
                this._hide(surface);
            }, wait);
        }

        _show(surface) {
            const s = this.surfaces[surface];
            this._clear(s.fadeTimer);
            this._clear(s.delayTimer);
            s.fadeTimer = null;
            s.delayTimer = null;
            s.visible = true;
            s.shownAt = this._now();
            s.shows += 1;
            const node = this._ensure(surface);
            if (!node) return;
            node.hidden = false;
            // On the next frame, so the opacity transition has a start.
            const frame = ++s.frame;
            this._frame(() => {
                if (s.frame === frame && s.visible) node.classList.add("is-visible");
            });
        }

        _hide(surface) {
            const s = this.surfaces[surface];
            this._clear(s.hideTimer);
            s.hideTimer = null;
            s.visible = false;
            s.frame += 1;
            const node = s.node;
            if (!node) return;
            node.classList.remove("is-visible");
            this._clear(s.fadeTimer);
            s.fadeTimer = this._set(() => {
                s.fadeTimer = null;
                if (!s.visible) node.hidden = true;
            }, FADE_MS);
        }

        _clearTimers(s) {
            this._clear(s.delayTimer);
            this._clear(s.hideTimer);
            this._clear(s.fadeTimer);
            s.delayTimer = s.hideTimer = s.fadeTimer = null;
        }

        // -- the pill -----------------------------------------------------------

        /** The surface's pill, built on first need and put in its mount
         *  whenever that resolves (a pane rebuilt gets it back). */
        _ensure(surface) {
            const s = this.surfaces[surface];
            if (!s.node) this._build(surface);
            const host = this.mounts[surface]?.();
            if (host && s.node.parentNode !== host) host.appendChild(s.node);
            return s.node;
        }

        _build(surface) {
            const doc = this.document;
            const s = this.surfaces[surface];
            const node = doc.createElement("span");
            node.className = `plx-pane-status is-${surface}`;
            node.setAttribute("aria-hidden", "true");
            node.dataset.phase = "idle";
            node.hidden = true;
            const dot = doc.createElement("span");
            dot.className = "plx-pane-status-dot";
            const ring = svg(doc, "svg", { class: "plx-pane-status-ring", viewBox: "0 0 14 14",
                                           "aria-hidden": "true", focusable: "false" });
            ring.appendChild(svg(doc, "circle", { class: "plx-pane-status-track", cx: 7, cy: 7, r: 5.5 }));
            const arc = svg(doc, "circle", { class: "plx-pane-status-arc", cx: 7, cy: 7, r: 5.5,
                                             "stroke-dasharray": ARC, "stroke-dashoffset": ARC });
            ring.appendChild(arc);
            ring.setAttribute("hidden", "");
            const retry = doc.createElement("button");
            retry.className = "plx-pane-status-retry";
            retry.type = "button";
            retry.hidden = true;
            retry.title = "This view did not finish loading. Click to retry.";
            retry.setAttribute("aria-label", `Retry loading the ${WHAT[surface]}`);
            // A turning arrow, drawn inline (no icon font in the panes).
            const glyph = svg(doc, "svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" });
            glyph.appendChild(svg(doc, "path", { d: "M20 12a8 8 0 1 1-2.34-5.66" }));
            glyph.appendChild(svg(doc, "path", { d: "M20 4v4h-4" }));
            retry.appendChild(glyph);
            retry.addEventListener("click", (event) => {
                // The pane's own click moves the plane (sectionPanes onClick).
                event?.stopPropagation?.();
                event?.preventDefault?.();
                const fn = s.retryFn;
                s.retryFn = null;          // once per error
                if (fn) fn();
            });
            node.append(dot, ring, retry);
            Object.assign(s, { node, dot, ring, arc, retry });
            return node;
        }

        _paint(surface, ring) {
            const s = this.surfaces[surface];
            const node = s.node;
            if (!node) return;
            node.dataset.phase = s.phase;
            const error = s.phase === "error";
            s.mode = error ? "error" : ring ? "ring" : "dot";
            node.classList.toggle("has-ring", s.mode === "ring");
            s.dot.hidden = s.mode !== "dot";
            if (s.mode === "ring") s.ring.removeAttribute("hidden");
            else s.ring.setAttribute("hidden", "");
            s.retry.hidden = !error;
            // In an error the button is the one thing to reach; otherwise
            // the pill is decoration (the navbar chip says it in words).
            if (error) node.removeAttribute("aria-hidden");
            else node.setAttribute("aria-hidden", "true");
            if (s.mode === "ring") this._ratio(s, s.best || 0);
        }

        _ratio(s, ratio) {
            const r = Math.max(0, Math.min(1, ratio));
            s.arc.style.strokeDashoffset = String(+(ARC * (1 - r)).toFixed(3));
        }
    }

    global.PlexoraPaneStatus = { PaneStatus, DELAY_MS, MIN_VISIBLE_MS, FADE_MS, meaningful };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraPaneStatus;
    }
})(typeof window !== "undefined" ? window : globalThis);
