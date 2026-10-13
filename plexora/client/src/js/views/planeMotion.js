/**
 * planeMotion.js - moving through a stack's planes without the picture jumping.
 *
 * TWO PARTS, both about the moment one plane replaces another.
 *
 * PlaneFade: the plane on screen dissolves into the next. A plane change is a
 * held handover (viewerManager.setView): every channel is rebuilt at the new
 * plane while the old keeps drawing, and all of them are swapped at once when
 * the new can draw. At that instant the picture on screen -- the viewer's
 * ground with the canvas OSD draws every channel into over it -- is copied,
 * opaque, to a GHOST canvas laid over it (compose explains why opaque), the
 * new plane is drawn underneath, and the ghost's opacity runs to zero on the
 * compositor (the Web Animations API: no JavaScript per frame, no texture
 * uploaded). A step is a dissolve rather than a cut, and a scrub through many
 * planes reads as continuous movement.
 *
 * WHAT IS BLENDED IS THE PICTURE, NOT THE DATA. Nothing reaches the tiles, the
 * windows or z: the planes are the planes the file holds, every capture reads
 * the canvas underneath (never the ghost), and when a fade ends -- in at most
 * FADE_MS -- the screen is the plane the readout names, unmodified. A fade
 * caught by a newer step is blended into it as it stands on screen, so a fast
 * scrub never jumps back; and a fade's length follows the pace of the steps,
 * so it never trails the hand. A pan, zoom or turn during one ends it at once
 * (the ghost is a picture of the screen, which would no longer line up), and with
 * motion off -- the system's reduced-motion setting, or the Depth card's
 * switch (services/viewerPrefs.js) -- there is no ghost at all.
 *
 * NeighbourPrefetch: once a plane has loaded, the planes the hand is heading
 * to are made ready. With the tile scheduler (tileFetch.js) they are BUILT --
 * every channel's pair at the next planes and the one behind, loaded
 * invisibly and last in line (ViewerManager.prepareStandby) -- so a step to
 * one is a flip of opacity. How far ahead follows the hand's pace, measured
 * over consecutive steps (a jump sets none): two planes for a step now and
 * then, up to six for a held key (k = the planes it covers in
 * STANDBY_LOOKAHEAD_MS), and while it moves they are asked for as soon as
 * each plane covers the view rather than once it is sharp and the hand still.
 * Without it, the tiles on screen are asked for at the next plane and the one
 * before at low priority, so the next step is served from the browser's cache.
 *
 * And the plane the pointer RESTS on along the Z strip (depthStrip.js raises
 * `plexora:plane-hover`): a far jump is a click on the rail, and the moment
 * between the pointer arriving there and the click is time the plane can
 * spend loading. It joins the neighbours as one more standby plane, last in
 * line like them, and is let go when the pointer leaves the rail.
 *
 * VolumeGhost: the same opaque crossfade for the 3-D view's canvas
 * (views/volumeRenderer.js draw), held when what the volume is drawn WITH
 * changes under a still camera, never across a camera move.
 */
(function (global) {
    "use strict";

    //: A fade's longest: one step reads as a dissolve, not as a lag.
    const FADE_MS = 160;
    //: Its shortest: a fast scrub still blends, without trailing behind.
    const MIN_FADE_MS = 60;
    //: The ghost holds one viewport of pixels; given back after this long idle.
    const RELEASE_AFTER_MS = 4000;
    //: Quick to show the new plane, soft to land: a decelerating curve, so the
    //: frame after a step already shows it changing. (--ease-standard, which
    //: this was, starts flat -- the old plane held still for the first third
    //: of the fade, which read as the step arriving late.)
    const EASING = "cubic-bezier(0, 0, 0.2, 1)";

    const PREFETCH_IDLE_MS = 150;
    //: While the hand moves (STANDBY_FAST_RATE planes a second or more), the
    //: next planes are asked for this soon after each lands.
    const PREFETCH_MOVING_MS = 30;
    const STANDBY_FAST_RATE = 3;
    //: Planes ahead: those the hand reaches in this long at its pace, within
    //: STANDBY_AHEAD.
    const STANDBY_LOOKAHEAD_MS = 400;
    const STANDBY_AHEAD = [2, 6];
    //: Requests further apart than this are not one movement.
    const RATE_GAP_MS = 1000;
    //: A request more planes than this from the last is a jump (viewState's
    //: NEAR_PLANES), not travel: it sets no pace, and neither does the step
    //: after it.
    const JUMP_PLANES = 2;
    const PREFETCH_LIMIT = 48;
    const PREFETCH_PARALLEL = 4;

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }

    class PlaneFade {
        constructor(viewer, { prefs = null, wrapper = null } = {}) {
            this.viewer = viewer;
            this.prefs = prefs;
            this.wrapper = wrapper;
            this.ghost = null;
            this.animation = null;
            this.lastCapture = null;
            this.gap = Infinity;
            this._freeTimer = 0;
            //: Where the view was when the picture was held (see moved).
            this.heldView = null;
            //: The view-perf mark a capture leaves (the benchmark's count).
            this.perfKey = "ghost";
            // OSD raises "animation" for more than pans and zooms -- adding
            // and removing items, which every swap does, starts one with
            // nothing moving -- so a fade ends only if the view really moved.
            const check = () => {
                if (this.moved()) this.cancel();
            };
            for (const name of ["animation", "resize", "rotate", "flip"]) {
                viewer?.addHandler?.(name, check);
            }
        }

        /** The view as it is drawn now: centre, scale, turn, mirror. */
        viewNow() {
            const viewport = this.viewer?.viewport;
            if (!viewport?.getBounds) return null;
            const bounds = viewport.getBounds(true);
            const size = viewport.getContainerSize?.() || { x: 1, y: 1 };
            return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height,
                     px: size.x, py: size.y,
                     rotation: viewport.getRotation?.(true) ?? 0,
                     flip: Boolean(viewport.getFlip?.()) };
        }

        /** Whether the view has moved by half a screen pixel or more since the
         *  picture was held: past that the ghost no longer lines up. */
        moved() {
            const held = this.heldView;
            if (!held || !this.animation) return false;
            const now = this.viewNow();
            if (!now) return true;
            if (now.px !== held.px || now.py !== held.py) return true;
            if (now.rotation !== held.rotation || now.flip !== held.flip) return true;
            const perUnit = held.px / Math.max(1e-12, held.width);
            return Math.abs(now.x - held.x) * perUnit > 0.5
                || Math.abs(now.y - held.y) * perUnit > 0.5
                || Math.abs(now.width / held.width - 1) * held.px > 0.5;
        }

        enabled() {
            if (this.prefs && !this.prefs.smoothMotion()) return false;
            // In 3-D the tiles are under the volume's own canvas.
            return !this.wrapper?.classList?.contains("is-volume-mode");
        }

        source() {
            return this.viewer?.drawer?.canvas || null;
        }

        ensureGhost(source) {
            if (this.ghost?.isConnected) return this.ghost;
            const ghost = document.createElement("canvas");
            ghost.className = "viewer-plane-ghost";
            ghost.setAttribute("aria-hidden", "true");
            ghost.hidden = true;
            // Directly above the tiles and below everything drawn over them:
            // outlines, regions and the crosshair stay crisp through a fade.
            source.after(ghost);
            this.ghost = ghost;
            return ghost;
        }

        /** How much of the ghost is showing (0 when no fade is running). */
        /** Whether no fade is running and no ghost of the last plane is on
         *  screen: what a frame capture waits for. */
        idle() {
            return !this.animation && (!this.ghost || this.ghost.hidden);
        }

        showing() {
            if (!this.animation || !this.ghost || this.ghost.hidden) return 0;
            const value = Number(global.getComputedStyle?.(this.ghost)?.opacity);
            return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
        }

        /**
         * The colour the plane is drawn over: the viewer's ground, which is
         * CSS on the canvas's container (black, a slide's off-white, a layer's
         * own background) and never pixels in the canvas. Null when nothing
         * up the tree paints one.
         */
        ground(source) {
            for (let node = source?.parentElement; node; node = node.parentElement) {
                const colour = global.getComputedStyle?.(node)?.backgroundColor;
                if (colour && colour !== "transparent" && !/^rgba\(.*,\s*0\)$/.test(colour)) {
                    return colour;
                }
            }
            return null;
        }

        /**
         * What the screen shows under the ghost, as one OPAQUE picture: the
         * ground, then the canvas over it, through the canvas's own CSS filter
         * (brightfieldAdjust's). The canvas alone is not that picture: OSD
         * draws each channel with alpha = its coverage, over a transparent
         * canvas, and the ground shows through. A translucent copy faded over
         * the next plane lets that plane through it -- on screen
         * o*old + (1 - o*alpha_old)*new, brighter than either wherever the old
         * plane was dim but not black -- which read as a flash of the wrong
         * contrast on every step. An opaque copy is a true crossfade:
         * o*old + (1 - o)*new, always between the two.
         */
        compose(source, ground) {
            let picture = this.picture;
            if (!picture) {
                picture = this.picture = global.document.createElement("canvas");
            }
            if (picture.width !== source.width || picture.height !== source.height) {
                picture.width = source.width;
                picture.height = source.height;
            }
            const context = picture.getContext("2d");
            if (!context) return null;
            context.globalAlpha = 1;
            context.filter = "none";
            context.fillStyle = ground;
            context.fillRect(0, 0, picture.width, picture.height);
            const filter = global.getComputedStyle?.(source)?.filter;
            if (filter && filter !== "none") context.filter = filter;
            context.drawImage(source, 0, 0);
            context.filter = "none";
            return picture;
        }

        /**
         * Hold what is on screen, before the swap. Returns whether a fade will
         * follow; false when motion is off, nothing is drawn yet, or the
         * ground the plane is drawn over cannot be found (a fade over an
         * unknown ground is not a faithful one).
         */
        capture() {
            if (!this.enabled()) {
                this.cancel();
                return false;
            }
            const source = this.source();
            if (!source?.width || !source?.height || typeof source.after !== "function") {
                return false;
            }
            const ground = this.ground(source);
            if (!ground) {
                this.cancel();
                return false;
            }
            const ghost = this.ensureGhost(source);
            const context = ghost.getContext("2d");
            if (!context) return false;
            const picture = this.compose(source, ground);
            if (!picture) return false;
            const showing = this.showing();
            const sameSize = ghost.width === source.width && ghost.height === source.height;
            this.animation?.cancel?.();
            this.animation = null;
            if (showing > 0 && sameSize) {
                // A fade still running: keep the mix that is on screen, so a
                // fast scrub never jumps back to a plane already left. Both are
                // opaque, so this is exactly showing*ghost + (1-showing)*new.
                context.globalAlpha = 1 - showing;
                context.drawImage(picture, 0, 0);
                context.globalAlpha = 1;
            } else {
                if (!sameSize) {
                    ghost.width = source.width;
                    ghost.height = source.height;
                }
                // Opaque: it covers whatever the ghost held before.
                context.globalAlpha = 1;
                context.drawImage(picture, 0, 0);
            }
            ghost.style.opacity = "1";
            ghost.hidden = false;
            global.PlexoraViewPerf?.mark?.(this.perfKey, "captured");
            this.heldView = this.viewNow();
            clearTimeout(this._freeTimer);
            const at = now();
            this.gap = this.lastCapture === null ? Infinity : at - this.lastCapture;
            this.lastCapture = at;
            return true;
        }

        /** As long as the steps are apart, within MIN_FADE_MS..FADE_MS. */
        duration() {
            const gap = this.gap;
            if (!Number.isFinite(gap) || gap >= FADE_MS * 3) return FADE_MS;
            return Math.round(Math.max(MIN_FADE_MS, Math.min(FADE_MS, gap * 0.85)));
        }

        /** Fade the held picture out over the new one, now under it. */
        release() {
            const ghost = this.ghost;
            if (!ghost || ghost.hidden) return;
            const ms = this.duration();
            if (typeof ghost.animate === "function") {
                const animation = ghost.animate([{ opacity: 1 }, { opacity: 0 }],
                    { duration: ms, easing: EASING, fill: "forwards" });
                this.animation = animation;
                animation.onfinish = () => {
                    if (this.animation === animation) this.finish();
                };
            } else {
                // No Web Animations: the same curve by hand.
                const start = now();
                const token = {};
                this.animation = { cancel: () => { token.cancelled = true; } };
                const frame = () => {
                    if (token.cancelled) return;
                    const f = Math.min(1, (now() - start) / ms);
                    ghost.style.opacity = String(1 - (1 - (1 - f) ** 3));
                    if (f < 1) global.requestAnimationFrame(frame);
                    else this.finish();
                };
                global.requestAnimationFrame(frame);
            }
        }

        finish() {
            this.animation = null;
            if (this.ghost) this.ghost.hidden = true;
            clearTimeout(this._freeTimer);
            this._freeTimer = setTimeout(() => {
                if (this.animation || !this.ghost) return;
                this.ghost.width = 1;
                this.ghost.height = 1;
                if (this.picture) {
                    this.picture.width = 1;
                    this.picture.height = 1;
                }
            }, RELEASE_AFTER_MS);
        }

        /** End any fade now: the new plane, unblended. */
        cancel() {
            if (!this.animation && (!this.ghost || this.ghost.hidden)) return;
            this.animation?.cancel?.();
            this.finish();
        }
    }

    /**
     * The 3-D view's crossfade (views/volumeRenderer.js draw): the picture on
     * the volume's canvas held, opaque, on a ghost laid over it, and faded out
     * over the next frame -- when the next frame is the same camera drawn with
     * something new (the full frame after a turn stops, a texture, a detail or
     * a batch of bricks landing, a channel switched off), so a refinement
     * dissolves in instead of jumping. `sync` with a different camera ends it
     * at once; a pinned size (a video being rendered) never holds one, nor
     * does motion off. Captures read the canvas underneath, never the ghost.
     */
    class VolumeGhost extends PlaneFade {
        constructor(renderer, { prefs = null } = {}) {
            super(null, { prefs });
            this.renderer = renderer;
            this.perfKey = "vghost";
        }

        enabled() {
            if (this.prefs && !this.prefs.smoothMotion()) return false;
            return Boolean(this.renderer) && !this.renderer._pinnedSize;
        }

        source() {
            return this.renderer?.canvas || null;
        }

        /** The volume canvas's own CSS ground (black): the GL picture is
         *  drawn over it wherever it is not opaque (surfaces). */
        ground(source) {
            const colour = global.getComputedStyle?.(source)?.backgroundColor;
            return colour && colour !== "transparent" && !/^rgba\(.*,\s*0\)$/.test(colour) ? colour : "#000";
        }

        ensureGhost(source) {
            if (this.ghost?.isConnected) return this.ghost;
            const ghost = document.createElement("canvas");
            ghost.className = "viewer-volume-ghost";
            ghost.setAttribute("aria-hidden", "true");
            ghost.hidden = true;
            // Over the volume's canvas, under its gizmo, note and everything
            // else in the viewer.
            source.after(ghost);
            this.ghost = ghost;
            return ghost;
        }

        /** The camera key (volumeRenderer.cameraKey): equal keys line up. */
        viewNow() {
            return this.renderer?.cameraKey?.() ?? null;
        }

        moved() {
            return Boolean(this.animation) && this.viewNow() !== this.heldView;
        }

        /** The camera of the frame about to be drawn: a ghost held at
         *  another one no longer lines up, and ends now. */
        sync(key) {
            if (!this.idle() && key !== this.heldView) this.cancel();
        }
    }

    /** The view token in a tile address, swapped for another. */
    function withView(address, token) {
        const value = `view=${encodeURIComponent(token)}`;
        return /([?&])view=[^&]*/.test(address)
            ? address.replace(/([?&])view=[^&]*/, `$1${value}`)
            : null;
    }

    class NeighbourPrefetch {
        constructor(viewer, viewState, { fetchImpl = null, idleMs = PREFETCH_IDLE_MS,
                                         limit = PREFETCH_LIMIT, viewerManager = null } = {}) {
            this.viewer = viewer;
            this.viewState = viewState;
            this.viewerManager = viewerManager;
            this._fetch = fetchImpl || ((...args) => global.fetch(...args));
            this.idleMs = idleMs;
            this.limit = limit;
            this.direction = 1;
            this.lastZ = null;
            //: The hand's pace, planes a second (a moving average), and when
            //: it last asked for a plane.
            this.rate = 0;
            this.lastAt = null;
            //: The plane the pointer rests on along the Z strip, or null.
            this.hoverZ = null;
            this.timer = 0;
            this.controller = null;
            this.done = new Set();
        }

        attach() {
            global.addEventListener?.("plexora:view-requested", (event) => this.onRequested(event));
            global.addEventListener?.("plexora:view-changed", () => this.onSettled());
            // A channel or the tile quality changed under prefetched planes.
            global.addEventListener?.("plexora:standby-dropped", () => this.onSettled());
            global.addEventListener?.("plexora:plane-hover", (event) => this.hover(event?.detail?.z));
            return this;
        }

        /** The pointer rests on plane `z` of the Z strip (null: it left). */
        hover(z) {
            const next = Number.isFinite(z) ? Math.round(z) : null;
            if (next === this.hoverZ) return;
            this.hoverZ = next;
            this.onSettled();
        }

        onRequested(event) {
            const z = event?.detail?.state?.z;
            if (Number.isFinite(z)) {
                if (this.lastZ !== null && z !== this.lastZ) {
                    this.direction = Math.sign(z - this.lastZ);
                    const at = now();
                    const gap = this.lastAt === null ? Infinity : at - this.lastAt;
                    const jump = Math.abs(z - this.lastZ) > JUMP_PLANES;
                    if (!jump && gap < RATE_GAP_MS && gap > 0) {
                        const pace = (Math.abs(z - this.lastZ) * 1000) / gap;
                        this.rate = this.rate ? this.rate * 0.6 + pace * 0.4 : pace;
                    } else {
                        this.rate = 0;
                    }
                    // A jump measures nothing about the hand: 50 planes in
                    // 100 ms read as a held key and sent six planes ahead of
                    // the destination before it had sharpened.
                    this.lastAt = jump ? null : at;
                }
                this.lastZ = z;
            }
            this.abort();
        }

        /** The hand's pace now: none once it has been still a while. */
        pace() {
            if (this.lastAt === null || now() - this.lastAt > RATE_GAP_MS) return 0;
            return this.rate;
        }

        /** Planes to have ready ahead of the hand at its pace. */
        ahead() {
            const [least, most] = STANDBY_AHEAD;
            const reach = Math.ceil((this.pace() * STANDBY_LOOKAHEAD_MS) / 1000);
            return Math.max(least, Math.min(most, reach));
        }

        onSettled() {
            clearTimeout(this.timer);
            if (this.paused) return;
            const wait = this.pace() >= STANDBY_FAST_RATE ? Math.min(this.idleMs, PREFETCH_MOVING_MS)
                : this.idleMs;
            this.timer = setTimeout(() => this.run(), wait);
        }

        /** Stop (or resume) warming neighbouring planes: for a caller that
         *  knows the planes it will need next better than the direction of
         *  travel does (Video Studio prepares its own). */
        pause(on) {
            this.paused = Boolean(on);
            if (this.paused) this.abort();
            else this.onSettled();
        }

        abort() {
            clearTimeout(this.timer);
            this.controller?.abort?.();
            this.controller = null;
        }

        /** Every tile address the reference channels drew last frame at `token`. */
        visible(token) {
            const world = this.viewer?.world;
            const out = new Set();
            for (let i = 0; world && i < world.getItemCount(); i += 1) {
                const item = world.getItemAt(i);
                if (item?.source?.view !== token || item.getOpacity?.() === 0) continue;
                for (const info of item._lastDrawn || []) {
                    const address = info?.tile?.getUrl?.() || info?.tile?.url;
                    if (address) out.add(address);
                }
            }
            return [...out];
        }

        /** The addresses to warm, nearest plane in the direction of travel first. */
        plan() {
            const vs = this.viewState;
            const state = vs?.get?.();
            if (!state || state.mode !== "plane" || vs.depth < 2) return [];
            const current = vs.spec(state);
            const shown = this.visible(current);
            if (!shown.length) return [];
            const out = [];
            for (const dz of [this.direction || 1, -(this.direction || 1)]) {
                const z = state.z + dz;
                if (z < 0 || z >= vs.depth) continue;
                const token = vs.spec({ ...state, z });
                for (const address of shown) {
                    const next = withView(address, token);
                    if (next && !this.done.has(next)) out.push(next);
                    if (out.length >= this.limit) return out;
                }
            }
            return out;
        }

        /** The views worth having ready: ahead as far as the hand's pace
         *  reaches (ahead()), behind once -- planes, or a projection that
         *  follows the plane (it moves with every step). */
        neighbourStates() {
            const vs = this.viewState;
            const state = vs?.get?.();
            const follows = state?.mode === "slab" && state.slab?.follow;
            if (!state || (state.mode !== "plane" && !follows) || vs.depth < 2) return [];
            const dir = this.direction || 1;
            const steps = [];
            for (let k = 1; k <= this.ahead(); k += 1) steps.push(k * dir);
            steps.push(-dir);
            const planes = steps.map((dz) => state.z + dz);
            // The plane the pointer rests on along the rail: a click away.
            if (this.hoverZ !== null && this.hoverZ !== state.z && !planes.includes(this.hoverZ)) {
                planes.push(this.hoverZ);
            }
            const out = [];
            for (const z of planes) {
                if (z < 0 || z >= vs.depth) continue;
                out.push(follows && vs.movedTo ? vs.movedTo(z, state) : { ...state, z });
            }
            return out;
        }

        /** Their tile tokens. */
        neighbours() {
            return this.neighbourStates().map((state) => this.viewState.spec(state));
        }

        /** Whether every item of the plane on screen has what it needs. While
         *  the hand moves, covering the view at some level (channelFade.covers)
         *  is enough: the hand will be gone before it sharpens, and its
         *  sharpening outranks a prefetch anyway (tileFetch.js). After a step
         *  or a jump it is fully loaded first, so a prefetch never shares the
         *  wire or the server with the plane being looked at. */
        planeLoaded() {
            const world = this.viewer?.world;
            const covers = this.pace() >= STANDBY_FAST_RATE ? global.PlexoraChannelFade?.covers : null;
            for (let i = 0; world && i < world.getItemCount(); i += 1) {
                const item = world.getItemAt(i);
                if (item?.source?.plexoraStandby || !(item?.getOpacity?.() > 0)) continue;
                if (typeof item.getFullyLoaded !== "function" || item.getFullyLoaded()) continue;
                if (typeof covers === "function" && covers(item)) continue;
                return false;
            }
            return true;
        }

        async run() {
            if (typeof this.viewerManager?.prepareStandby === "function"
                    && this.viewerManager.imageViewer?.tileFetch) {
                // Not while the plane on screen is still loading (a channel
                // just switched on, a zoom): what it needs comes first.
                if (!this.planeLoaded()) {
                    clearTimeout(this.timer);
                    this.timer = setTimeout(() => this.run(), this.idleMs);
                    return;
                }
                // A 3-D mask's outlines come too: a projection's are one plane's,
                // so their tokens are not the picture's.
                const states = this.neighbourStates();
                this.viewerManager.prepareStandby(
                    states.map((state) => this.viewState.spec(state)),
                    states.map((state) => this.viewState.labelSpec?.(state) || ""));
                return;
            }
            const queue = this.plan();
            if (!queue.length) return;
            const controller = typeof AbortController === "function" ? new AbortController() : null;
            this.controller = controller;
            const worker = async () => {
                while (queue.length && this.controller === controller) {
                    const address = queue.shift();
                    try {
                        const response = await this._fetch(address, {
                            signal: controller?.signal, priority: "low",
                            credentials: "same-origin" });
                        // Read to the end: a response is cached once complete.
                        await response?.arrayBuffer?.();
                        if (response?.ok) this.done.add(address);
                    } catch (error) {
                        // Aborted by the next step, or the server said no: the
                        // tile will be fetched the ordinary way if needed.
                    }
                }
            };
            await Promise.all(Array.from({ length: PREFETCH_PARALLEL }, worker));
            if (this.done.size > 4096) this.done.clear();
        }
    }

    /** Wire both to the viewer; nothing for a planar image. */
    function attach({ viewer, viewState, viewerManager, prefs = global.PlexoraViewerPrefs } = {}) {
        if (!viewState?.enabled?.() || viewState.depth < 2 || !viewer) return null;
        const wrapper = document.getElementById("openseadragon_wrapper");
        const fade = new PlaneFade(viewer, { prefs, wrapper });
        if (viewerManager) viewerManager.planeFade = fade;
        const prefetch = new NeighbourPrefetch(viewer, viewState, { viewerManager }).attach();
        return { fade, prefetch };
    }

    global.PlexoraPlaneMotion = { attach, PlaneFade, VolumeGhost, NeighbourPrefetch, withView,
                                  FADE_MS, MIN_FADE_MS, STANDBY_AHEAD, STANDBY_LOOKAHEAD_MS };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraPlaneMotion;
    }
})(typeof window !== "undefined" ? window : globalThis);
