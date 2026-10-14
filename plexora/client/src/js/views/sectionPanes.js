/**
 * sectionPanes.js - orthogonal sections beside the plane: XZ under it, YZ beside it.
 *
 * The orthogonal view every confocal viewer has (Fiji's Orthogonal Views,
 * Imaris's Section): a cut through every plane of the stack along the
 * horizontal line through the centre of the view (XZ) and along the vertical
 * one (YZ), drawn ALIGNED with the plane -- the XZ strip shares the main view's
 * x scale and position, the YZ strip its y -- so a cell in the plane and the
 * same cell in the sections line up on screen. Pan the plane and the sections
 * follow; the dashed crosshair on the plane is where they cut.
 *
 * Each pane is a small OpenSeadragon viewer of its own: a section has its own
 * frame (rows are z, drawn at the stack's z step), so it cannot live in the
 * main viewer's world. It shares everything else -- the decode workers, the
 * GL colorize renderer, the channels' colours and windows, which are read off
 * the main sidebar exactly as the plane's tiles read them -- and its tiles come
 * down the ordinary tile route with `view=xzy<y>` / `view=yzx<x>`.
 *
 * The YZ section is served rows = z, cols = y and shown TRANSPOSED (rotated a
 * quarter turn and mirrored) so that y runs down the screen beside the plane.
 *
 * A pane's own pan and zoom follow the plane's; clicking in one moves to the
 * plane under the pointer. Off by default; the strip's Sections button
 * toggles it and the choice is saved with the view state (`layout`).
 *
 * RESIZABLE, both ways. Each pane's inner edge is a divider: drag it, or focus
 * it and use the arrow keys; double-click (or Home) gives the default size
 * back. The size chosen is the size shown. While the stack at the plane's
 * scale is taller than the pane, the pane shows a window of its planes at the
 * plane's proportions, scrolled to keep the plane on screen in view; once the
 * pane is the taller, every plane is shown and z is stretched to fill it (a
 * CSS scale on the pane's own viewer, `factor`, said in the label as "z ×2.4"
 * -- the rows are the same planes, drawn taller, never new ones). A size
 * within SNAP_PX of the stack's own height snaps to it. A pane never grows
 * over the controls: it leaves MIN_XY_PX of the plane uncovered.
 *
 * The size is a share of the plane's height (XZ) or width (YZ), saved for the
 * user rather than the image (services/viewerPrefs.js), so it holds across
 * images, modes and sessions. Resizing moves nothing else: the plane, its
 * zoom, the channels and the plane on screen stay put, and the sections are
 * not asked for again until the divider is let go (the cut is held still
 * while it moves).
 *
 * The cut runs through the centre of the part of the plane NOT under a pane,
 * so a pane made large never hides the line it is a section along.
 */
(function (global) {
    "use strict";

    //: The default: a pane as tall as its stack, up to this share of the plane.
    const MAX_PANE_FRACTION = 0.32;
    const MIN_PANE_PX = 40;
    //: A size the user chose, as a share (server/routes/volume_routes.py
    //: SECTION_SHARE holds the same bounds).
    const MIN_SHARE = 0.05;
    const MAX_SHARE = 0.9;
    //: What a pane leaves of the plane, at its largest: room for the plane
    //: itself and the controls over it.
    const MIN_XY_PX = 240;
    //: A size this close to the stack's own height at the plane's scale is
    //: that height: z drawn at the plane's proportions, the detent between a
    //: window of planes and a stretched whole stack.
    const SNAP_PX = 4;
    //: Pixels per arrow key on a divider; Shift for KEY_STEP_LARGE.
    const KEY_STEP = 16;
    const KEY_STEP_LARGE = 64;
    //: A pan that stopped moving (the hand still, or let go): the section at
    //: the place the view is heading to is asked for after this long, while
    //: the view still eases there -- not 1.2 s later, once it has.
    const TARGET_STILL_MS = 60;
    //: How long the view stays put before its area is prepared in 3-D.
    const PREPARE_DELAY_MS = 2000;
    //: How long a new section waits for its full resolution once it covers
    //: the pane at some level, before it comes in with that level: the
    //: plane's (viewerManager PLANE_SWAP_GRACE_MS), for the same reason -- a
    //: warm section arrives sharp, a cold one shows at the level that covers
    //: and sharpens in place. None while the view moves (SectionPanes.grace).
    const PANE_GRACE_MS = 140;
    //: A handover still waiting after this says so (`slow`, the status
    //: event). It never hands over for want of tiles: the old section stays
    //: until the new one covers the pane.
    const PANE_SLOW_MS = 2000;
    //: Rows kept loaded just outside a pane too short for its stack, before
    //: its first row and after its last: a z step that scrolls the pane never
    //: brings rows on screen that have not loaded.
    const PANE_ROW_MARGIN_PX = 96;

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }

    /** The frame a section token is at (`t3xzy10` -> 3; no prefix -> 0). */
    function frameOf(token) {
        const match = /^t(\d+)/.exec(token || "");
        return match ? Number(match[1]) : 0;
    }

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined) continue;
            if (key === "text") node.textContent = value;
            else node.setAttribute(key, value);
        }
        return node;
    }

    function url(path) {
        return typeof global.plexoraUrl === "function" ? global.plexoraUrl(path) : `/${path}`;
    }

    /**
     * One cell's outline where a pane cuts it: its runs plane by plane
     * (`{z0, a0, a1}`, a0..a1 inclusive), stroked as ONE stepped boundary --
     * each run's two ends, and its top and bottom only where the plane above
     * or below has no run of the same cell, so a cell crossing nine planes is
     * one shape and not nine boxes. `along` and `across` map image pixels and
     * planes to the pane's own; YZ swaps them on screen.
     */
    function strokeCellOutline(context, spans, { along, across, xz }) {
        const byPlane = new Map();
        for (const span of spans) {
            const row = byPlane.get(span.z0) || [];
            row.push([span.a0, span.a1 + 1]);
            byPlane.set(span.z0, row);
        }
        const uncovered = (a0, a1, row) => {
            let pieces = [[a0, a1]];
            for (const [b0, b1] of row || []) {
                pieces = pieces.flatMap(([c0, c1]) => (b1 <= c0 || b0 >= c1 ? [[c0, c1]]
                    : [[c0, b0], [b1, c1]].filter(([d0, d1]) => d1 > d0)));
            }
            return pieces;
        };
        const line = (a0, z0, a1, z1) => {
            const [p, q] = [along(a0), across(z0)];
            const [r, s] = [along(a1), across(z1)];
            if (xz) {
                context.moveTo(p, q);
                context.lineTo(r, s);
            } else {
                context.moveTo(q, p);
                context.lineTo(s, r);
            }
        };
        context.globalAlpha = 1;
        context.beginPath();
        for (const [z, row] of byPlane) {
            for (const [a0, a1] of row) {
                line(a0, z, a0, z + 1);
                line(a1, z, a1, z + 1);
                for (const [c0, c1] of uncovered(a0, a1, byPlane.get(z - 1))) line(c0, z, c1, z);
                for (const [c0, c1] of uncovered(a0, a1, byPlane.get(z + 1))) line(c0, z + 1, c1, z + 1);
            }
        }
        context.stroke();
    }

    /** Uniforms the channel shader reads that only mean something for the
     *  plane's own cells; a section has none to draw. */
    function centerProps(imageViewer) {
        const gl = imageViewer?.glRenderer?.gl;
        const magnitude = imageViewer?.toTextureShape && gl
            ? imageViewer.toTextureShape(gl, imageViewer.idCount || 0) : [1, 1];
        return () => ({
            pie_radius_1f: 8.5,
            magnitude_2iv: magnitude,
            id_end_1i: 0,
            picked_end_1i: -1,
            modes_2i: [0, 0],
            key_end_1i: 0,
            marker_sample_4iv: [-1, -1, -1, -1],
            tile_scale_1f: 1.0,
            tile_fraction_1f: 1.0,
            // The whole tile is data (frag.glsl's in_tile_bounds): a section
            // tile is never magnified past its level the way the plane's are.
            x_bounds_2fv: new Float32Array([0, 1]),
            y_bounds_2fv: new Float32Array([0, 1]),
            origin_2fv: new Float32Array([0, 0]),
        });
    }

    class Pane {
        constructor(owner, axis) {
            this.owner = owner;
            this.axis = axis;               // "xz" or "yz"
            this.geometry = null;
            this.token = "";
            this.items = new Map();         // srcIdx -> TiledImage
            //: The show() still loading, if any: {token, generation, promise,
            //: cancel, defer, rev, landed, claims}. Each show() takes a new
            //: generation; anything a superseded one still has in flight
            //: drops itself on arrival.
            this.loading = null;
            this.generation = 0;
            //: A pan's section waiting for a new frame's handover to land
            //: (show): {token, options, behind, resolve}, or null.
            this.after = null;
            //: The loading status of the last handover (statusBegin), and
            //: the frame its next event goes out in.
            this.tracker = null;
            this.statusFrame = 0;
            //: Set by a reload of the same cut (SectionPanes.rebuild): its
            //: handover waits for full resolution, as the plane's quality
            //: swap does, rather than dipping to a coarser level.
            this.refresh = false;
            this.view = { top: 0, rows: 0, scale: 1 };
            this.root = el("div", `viewer-section-pane is-${axis}`, {
                role: "img", "aria-label": axis === "xz" ? "XZ section" : "YZ section" });
            this.canvasHost = el("div", "viewer-section-pane-osd");
            //: What plugins draw where the cut crosses their shapes
            //: (SectionPanes.addOverlay): regions through the planes, say.
            this.overlay = el("canvas", "viewer-section-pane-overlay", { "aria-hidden": "true" });
            this.label = el("div", "viewer-section-pane-label");
            this.zline = el("div", "viewer-section-pane-zline");
            this.root.append(this.canvasHost, this.overlay, this.zline, this.label);
            this.root.addEventListener("click", (event) => this.onClick(event));
            //: The inner edge, for resizing; on the wrapper, beside the pane,
            //: because the pane clips anything that hangs over its edge.
            const name = axis === "xz" ? "XZ" : "YZ";
            this.divider = el("div", `viewer-section-divider is-${axis}`, {
                role: "separator", tabindex: "0",
                "aria-orientation": axis === "xz" ? "horizontal" : "vertical",
                "aria-label": `Resize the ${name} section`,
                "aria-valuemin": String(Math.round(MIN_SHARE * 100)),
                "aria-valuemax": String(Math.round(MAX_SHARE * 100)),
                title: `Drag to resize the ${name} section. Double-click for its default size.`,
            });
            this.divider.appendChild(el("span", "viewer-section-divider-grip",
                                        { "aria-hidden": "true" }));
        }

        build() {
            const owner = this.owner;
            this.osd = OpenSeadragon({
                element: this.canvasHost,
                drawer: "canvas",
                showNavigationControl: false,
                mouseNavEnabled: false,
                loadTilesWithAjax: true,
                compositeOperation: "lighter",
                immediateRender: false,
                maxImageCacheCount: 96,
                // The tile scheduler (tileFetch.js) decides the order across
                // the plane and both panes; this only hands it the jobs. 64,
                // not 16: OSD's ImageLoader starts at most this many jobs and
                // holds the rest in its own queue, first come first served,
                // where the scheduler cannot see them -- and a handover now
                // asks for a whole section at every level at once
                // (maxTilesPerFrame). The wire is still the scheduler's
                // MAX_IN_FLIGHT: a started job is only a place in its line.
                // Without the scheduler OSD's own loader runs them, 6 at a time.
                imageLoaderLimit: owner.imageViewer?.tileFetch ? 64 : 6,
                // How many tiles one update of an item may ask for (OSD's
                // default is 1). OSD caps each update's load candidates at
                // `_currentMaxTilesPerFrame` -- ten times this on an item's
                // first update, halved per level it visits down to this
                // (TiledImage._updateLevel) -- so at 1 a new section hands
                // over ten tiles and then ONE A FRAME, and the scheduler can
                // only order what it has been handed. At 16 the first update
                // asks for every level's tiles in the pane at once, before
                // any are loading; OSD ranks the coarsest first, the
                // scheduler then decides. Half the plane's 32: a pane is a
                // strip, a few tiles a level.
                maxTilesPerFrame: 16,
                timeout: 90000,
                // A failed tile is otherwise never asked for again (OSD's
                // default is 0): a hole until the section changes. As the plane's.
                tileRetryMax: 3,
                tileRetryDelay: 1500,
                preserveViewport: true,
                visibilityRatio: 0,
                minZoomImageRatio: 0,
                maxZoomPixelRatio: 128,
                animationTime: 0,
                blendTime: 0,
                springStiffness: 100,
                gestureSettingsMouse: { clickToZoom: false, scrollToZoom: false },
            });
            if (this.axis === "yz") {
                // rows = z, cols = y on the wire; y down the screen here.
                this.osd.viewport.setRotation(90, true);
                this.osd.viewport.setFlip(true);
            }
            const smooth = () => {
                const context = this.osd?.drawer?.canvas?.getContext?.("2d");
                if (context) context.imageSmoothingEnabled = false;
            };
            this.osd.addHandler("open", smooth);
            this.osd.addHandler("resize", smooth);
            this.osd.addHandler("tile-loaded", owner.handleTileLoaded);
            this.osd.addHandler("tile-drawing",
                (event) => owner.tileDrawing.tileDrawingCustom(owner.tileDrawing.tileDrawingDefault, event));
            this.osd.addHandler("tile-unloaded", (event) => { delete event.tile._array; });
            owner.imageViewer?.tileFetch?.watch(this.osd);
        }

        tileSource(srcIdx, channel, token) {
            const owner = this.owner;
            const geometry = this.geometry;
            const levels = geometry.level_shapes.length;
            const config = owner.config;
            const entry = config.imageData[srcIdx] || {};
            const hd = owner.isHd();
            const windows = global.__plexora?.volumeWindows;
            const fullName = entry.fullname;
            const held = !hd && windows && fullName ? windows.peek(fullName) : null;
            const stamp = held ? windows.stamp(fullName) : "";
            const fetcher = owner.imageViewer?.tileFetch;
            const attach = (source) => (fetcher ? fetcher.attach(source, { owner: "pane" }) : source);
            return attach({
                height: geometry.height,
                width: geometry.width,
                tileWidth: config.tileWidth,
                tileHeight: config.tileHeight,
                maxLevel: levels - 1,
                minLevel: 0,
                tileFormat: 16,
                srcIdx,
                src: channel.url,
                view: token,
                //: The units this section's pixels are in (ImageViewer.itemRange):
                //: sections are always in the volume's window.
                hd,
                windowClass: "volume",
                //: The window the section's 8-bit bytes are quantized with
                //: (services/volumeWindows.js), in the address like the plane's.
                windowStamp: stamp,
                windowRange: held ? [held.qmin, held.qmax] : null,
                getTileUrl(level, x, y) {
                    const serverLevel = this.maxLevel - level;
                    const query = [hd ? "q=hd" : "", entry.srcQuery || "",
                                   `view=${encodeURIComponent(token)}`, stamp ? `w=${stamp}` : ""]
                        .filter(Boolean).join("&");
                    return `${this.src}${serverLevel}/${x}_${y}.png?${query}`;
                },
                getTileKey(level, x, y) {
                    return `16-${srcIdx}-${token}${hd ? "-hd" : ""}${stamp ? `-w${stamp}` : ""}-${this.maxLevel - level}-${x}-${y}`;
                },
                toTileLevels(level, x, y) {
                    const serverLevel = this.maxLevel - level;
                    const tile = { level: serverLevel, x, y };
                    return { inputFullScale: 2 ** serverLevel, outputFullScale: 2 ** serverLevel,
                             relativeImageScale: 2 ** serverLevel, inputTile: tile,
                             outputTile: { level, x, y } };
                },
                toMagnifiedBounds() {
                    return { x: [0, 1], y: [0, 1] };
                },
            });
        }

        /** The token on screen, or the one loading (or queued) to replace it. */
        wanted() {
            if (this.after) return this.after.token;
            return this.loading ? this.loading.token : this.token;
        }

        /**
         * Show the section at `token`: new items load invisibly, the old ones
         * go once the new can draw -- the plane swap's handover.
         *
         * CAN DRAW is coverage (services/channelFade.js whenCanDraw): every
         * item covers the pane at some level, and has had PANE_GRACE_MS to
         * finish its full resolution (none while the view moves:
         * SectionPanes.grace). A reload of the same cut waits for full
         * resolution (`refresh`). There is no deadline: a section that never
         * covers keeps the old one on screen, says `slow` at PANE_SLOW_MS,
         * and lands when it covers -- a tile whose retries are spent counts
         * as covered (a hole), so a section with holes still hands over, and
         * says `error`. One with NOTHING loaded never does
         * (channelFade.failedOut): the old section stays, the status says
         * `error`, and its retry asks again. An item that cannot be added
         * counts as answered.
         *
         * ONE HANDOVER AT A TIME, and only the newest ever lands: a show()
         * superseded by another (a pan that kept going, a new frame) drops its
         * items -- arrived or still to arrive -- and never fades up. `defer`
         * hands the commit back instead of running it (resolves to the
         * closure, or null): a pan commits it as it resolves, if no newer pan
         * came (SectionPanes.showAt); a navigation revision reveals both
         * panes in one task; `rev` superseded cancels it. A deferred
         * closure's `discard()` drops it, unless a newer pan asked for the
         * same section and holds it too.
         *
         * A pan while a new frame's sections load for a revision (`rev`, at
         * the same frame) does not cancel them -- the frame would never land
         * -- but waits its turn: queued (`after`), and asked for once they
         * land, or dropped with them.
         *
         * @returns a promise of the commit closure (with `defer`) or null
         */
        show(token, { defer = false, rev = null } = {}) {
            if (!this.osd || !this.geometry) return Promise.resolve(null);
            const count = this.owner.channelCount();
            // A reload asked for (`refresh`) is never answered by what is on
            // screen or already on its way: those have the old look.
            const current = token === this.token && this.items.size === count && !this.refresh;
            if (!this.loading && current) {
                this.dropQueued();
                return Promise.resolve(null);
            }
            const loading = this.loading;
            if (loading && !rev) {
                if (loading.rev) {
                    if (frameOf(token) === frameOf(loading.token)) {
                        if (token === loading.token && !this.refresh) {
                            // The revision is bringing this very section.
                            this.dropQueued();
                            return loading.landed.then(() => null);
                        }
                        return this.queue(token, { defer }, loading);
                    }
                } else if (loading.token === token && loading.defer === defer && !this.refresh) {
                    // Asked for again (a pan that ended where it was heading):
                    // the one loading answers both.
                    loading.claims += 1;
                    return loading.promise;
                } else if (current) {
                    // Back to the section on screen: nothing to load.
                    loading.cancel();
                    return Promise.resolve(null);
                }
            }
            this.dropQueued();
            this.loading?.cancel();
            const generation = ++this.generation;
            //: Dropped (cancel) with no newer show() yet: items still to
            //: arrive drop themselves all the same.
            let dead = false;
            const live = () => !dead && generation === this.generation;
            const world = this.osd.world;
            const channels = this.owner.channels();
            const outgoing = [...this.items.values()];
            //: A channel the pane is not drawing yet comes in rather than lands
            //: (services/channelFade.js); the others hand over as they are.
            const Fade = global.PlexoraChannelFade;
            const before = new Set(this.items.keys());
            const fresh = (srcIdx) => Boolean(Fade) && !before.has(srcIdx);
            const added = new Map();
            //: A reload of the cut on screen waits for full resolution; a new
            //: cut takes the level that covers (SectionPanes.grace).
            const full = this.refresh && outgoing.length > 0;
            this.refresh = false;
            const grace = () => {
                if (full) return null;
                if (rev?.scrub) return 0;
                const owned = this.owner.grace?.();
                return Number.isFinite(owned) ? owned : PANE_GRACE_MS;
            };
            let pending = channels.length;
            let settled = false;
            let finished = false;
            let timer = null;
            let resolve = null;
            let land = null;
            const promise = new Promise((done) => { resolve = done; });
            //: Settles when this handover is over, landed or dropped.
            const landed = new Promise((done) => { land = done; });
            //: Stops the waits for the items' coverage (whenCanDraw).
            const stops = [];
            const dropAdded = () => {
                for (const item of added.values()) {
                    if (world.getIndexOfItem(item) >= 0) world.removeItem(item);
                }
                added.clear();
            };
            /** A pan queued behind this handover (show): asked for once it
             *  landed; dropped with it, and the sections asked for afresh. */
            const release = (done) => {
                const after = this.after;
                if (!after || after.behind !== generation) return;
                this.after = null;
                if (done) {
                    after.resolve(this.show(after.token, after.options));
                } else {
                    after.resolve(null);
                    this.owner.requeue?.();
                }
            };
            const cancel = () => {
                if (finished) return;
                dead = true;
                if (timer !== null) clearTimeout(timer);
                timer = null;
                for (const stop of stops.splice(0)) stop();
                if (this.loading?.generation === generation) this.loading = null;
                if (!settled) {
                    settled = true;
                    this.owner.superseded += 1;
                    resolve(null);
                }
                dropAdded();
                land();
                this.statusLanded(generation, { dropped: true });
                release(false);
            };
            const finish = () => {
                if (!live()) {
                    dropAdded();
                    return;
                }
                finished = true;
                if (this.loading?.generation === generation) this.loading = null;
                this.token = token;
                this.items = new Map(added);
                const arriving = [];
                for (const [srcIdx, item] of added) {
                    if (fresh(srcIdx)) arriving.push(item);
                    else item.setOpacity(1);
                }
                // Together, once every one of them can draw.
                if (arriving.length) Fade.fadeIn(arriving);
                for (const item of outgoing) {
                    if (world.getIndexOfItem(item) >= 0) world.removeItem(item);
                }
                this.align();
                global.PlexoraViewPerf?.mark?.(this.axis, "committed", rev ? rev.id : null, token);
                this.statusLanded(generation);
                land();
                release(true);
            };
            const record = { token, generation, promise, cancel, defer, rev, landed, claims: 1 };
            finish.discard = () => {
                // Another pan asked for the same section and holds it too.
                record.claims -= 1;
                if (record.claims <= 0) cancel();
            };
            const hand = () => {
                if (settled || !live()) return;
                settled = true;
                if (timer !== null) clearTimeout(timer);
                timer = null;
                if (defer) {
                    resolve(finish);
                } else {
                    finish();
                    resolve(null);
                }
            };
            this.loading = record;
            this.statusBegin(generation, token, rev, () => [...added.values()]);
            rev?.onSupersede?.(cancel);
            if (dead) return promise;
            if (!pending) {
                hand();
                return promise;
            }
            // Not a deadline: the old section stays, and says it is waiting.
            timer = setTimeout(() => {
                timer = null;
                if (settled || !live()) return;
                this.owner.slow += 1;
                global.PlexoraViewPerf?.mark?.(this.axis, "slow", rev ? rev.id : null, token);
                this.statusSlow(generation);
            }, PANE_SLOW_MS);
            for (const [srcIdx, channel] of channels) {
                this.osd.addTiledImage({
                    tileSource: this.tileSource(srcIdx, channel, token),
                    compositeOperation: "lighter",
                    // Hidden until the handover, unless there is nothing on
                    // screen to hand over from -- or it is coming in.
                    opacity: outgoing.length || defer || fresh(srcIdx) ? 0 : 1,
                    preload: true,
                    success: (event) => {
                        if (!live()) {
                            world.removeItem(event.item);
                            return;
                        }
                        if (finished) {
                            // Late, after its handover: part of what is on
                            // screen now.
                            if (fresh(srcIdx)) Fade.fadeIn([event.item]);
                            else event.item.setOpacity(1);
                            this.items.set(srcIdx, event.item);
                            event.item.addHandler("fully-loaded-change", () => {
                                if (live()) requestAnimationFrame(() => this.redraw());
                            });
                            this.align();
                            return;
                        }
                        added.set(srcIdx, event.item);
                        // Decoding is asynchronous and finishes after OSD's own
                        // draw for the tile; draw again once the item has
                        // everything it needs.
                        event.item.addHandler("fully-loaded-change", () => {
                            if (live()) requestAnimationFrame(() => this.redraw());
                        });
                        this.align();
                        this.statusPoke();
                        const ready = () => {
                            pending -= 1;
                            if (pending <= 0) hand();
                        };
                        if (!outgoing.length && !defer) {
                            ready();
                        } else if (typeof Fade?.whenCanDraw === "function") {
                            stops.push(Fade.whenCanDraw(event.item, ready, {
                                grace: grace(), onFailed: () => this.statusPoke() }));
                        } else if (event.item.getFullyLoaded?.()) {
                            ready();
                        } else {
                            const onChange = () => {
                                if (!event.item.getFullyLoaded()) return;
                                event.item.removeHandler("fully-loaded-change", onChange);
                                ready();
                            };
                            event.item.addHandler("fully-loaded-change", onChange);
                            stops.push(() => event.item.removeHandler?.("fully-loaded-change", onChange));
                        }
                    },
                    error: () => {
                        pending -= 1;
                        if (pending <= 0) hand();
                    },
                });
            }
            return promise;
        }

        /** Wait behind `loading` (a new frame's handover) to show `token`;
         *  a pan queued before is superseded by this one. */
        queue(token, options, loading) {
            this.dropQueued();
            let resolve = null;
            const promise = new Promise((done) => { resolve = done; });
            this.after = { token, options, behind: loading.generation, resolve };
            return promise;
        }

        dropQueued() {
            const after = this.after;
            this.after = null;
            after?.resolve(null);
        }

        // -- loading status (views/paneStatus.js draws it) -----------------------
        //
        // One `plexora:surface-status` event at most a frame, from
        // ChannelFade.progress summed over the pane's items: `fetching` when
        // a handover starts, `slow` at PANE_SLOW_MS, then -- once it landed
        // -- `preview` while what shows is coarser than the pane's level,
        // `refining` from the first tile after, `idle` when every tile it
        // needs has loaded, `error` when the only ones missing failed for good
        // (`retry` asks for them again).

        /** A handover to `token` started: `items()` are the ones it loads. */
        statusBegin(generation, token, rev, items) {
            this.statusStop();
            this.tracker = {
                generation, token, rev: rev ? rev.id : null, since: now(), phase: "fetching",
                landed: false, refined: false, items, sent: "", off: null,
            };
            this.statusPoke();
        }

        statusSlow(generation) {
            const tracker = this.tracker;
            if (tracker?.generation !== generation || tracker.landed) return;
            tracker.phase = "slow";
            this.statusPoke();
        }

        /** The handover landed -- or was dropped, `dropped`, the section on
         *  screen staying: what the pane shows is counted from now on. */
        statusLanded(generation, { dropped = false } = {}) {
            const tracker = this.tracker;
            if (tracker?.generation !== generation || tracker.landed) return;
            tracker.landed = true;
            tracker.refined = dropped;
            tracker.items = () => [...this.items.values()];
            if (!dropped && this.statusPhase(tracker, this.statusCounts(tracker.items())) === "preview") {
                global.PlexoraViewPerf?.mark?.(this.axis, "preview", tracker.rev, tracker.token);
            }
            this.statusListen(tracker);
            this.statusPoke();
        }

        /**
         * Count again whenever one of the pane's items moves: a tile landing
         * or failing, or its fully-loaded flag. OSD raises `tile-loaded` and
         * `tile-load-failed` on the VIEWER, with the item as `tiledImage` --
         * the pane's viewer also hosts the next handover's hidden items, so
         * only the ones on screen count (as viewerManager.planeSwapGroup).
         */
        statusListen(tracker) {
            const osd = this.osd;
            if (tracker.off || typeof osd?.addHandler !== "function") return;
            const mine = new Set(tracker.items());
            const onTile = (event) => {
                if (!mine.has(event?.tiledImage)) return;
                tracker.refined = true;
                this.statusPoke();
                // Raised before OSD marks the tile loaded (its handlers are
                // awaited first, tileDecode.js among them); the event's
                // promise resolves once it has.
                event.promise?.then?.(() => this.statusPoke(), () => {});
            };
            const onFailed = (event) => {
                if (mine.has(event?.tiledImage)) this.statusPoke();
            };
            const onChange = () => this.statusPoke();
            osd.addHandler("tile-loaded", onTile);
            osd.addHandler("tile-load-failed", onFailed);
            for (const item of mine) item.addHandler?.("fully-loaded-change", onChange);
            tracker.off = () => {
                osd.removeHandler?.("tile-loaded", onTile);
                osd.removeHandler?.("tile-load-failed", onFailed);
                for (const item of mine) item.removeHandler?.("fully-loaded-change", onChange);
            };
        }

        statusStop() {
            const tracker = this.tracker;
            if (tracker?.off) {
                tracker.off();
                tracker.off = null;
            }
        }

        /** The status, at the next frame (once however often it is asked). */
        statusPoke() {
            if (!this.tracker || this.statusFrame) return;
            const run = () => {
                this.statusFrame = 0;
                this.statusEmit();
            };
            this.statusFrame = typeof global.requestAnimationFrame === "function"
                ? global.requestAnimationFrame(run) : setTimeout(run, 16);
        }

        /**
         * `{loaded, needed, failed, level, settled}` summed over `items`
         * (ChannelFade.progress); `level` the coarsest display level among
         * them (one geometry: one level). `settled`: nothing more is on its
         * way -- every tile in view has loaded or failed for good. OSD's own
         * `fully loaded` flips true while a failed tile waits for its retry,
         * so the counts decide; it only for an item OSD has given none yet.
         * `failedOut`: one of them has nothing loaded and its tiles failed
         * for good (ChannelFade.failedOut) -- a handover never lands on it.
         */
        statusCounts(items) {
            const Fade = global.PlexoraChannelFade;
            const out = { loaded: 0, needed: 0, failed: 0, level: null, settled: true, failedOut: false };
            for (const item of items) {
                if (Fade?.failedOut?.(item)) out.failedOut = true;
                const counted = typeof Fade?.progress === "function" ? Fade.progress(item) : null;
                if (counted && counted.level !== null) {
                    out.loaded += counted.loaded;
                    out.needed += counted.needed;
                    out.failed += counted.failed;
                    if (out.level === null || counted.level < out.level) out.level = counted.level;
                    if (counted.loaded + counted.failed < counted.needed) out.settled = false;
                } else if (!item.getFullyLoaded?.()) {
                    out.settled = false;
                }
            }
            return out;
        }

        /** The plane's rules (viewerManager.planeSwapGroup): settled is idle,
         *  or error when what is left failed for good -- and, before the
         *  handover, error while an item it waits on failed out. */
        statusPhase(tracker, counts) {
            if (!tracker.landed) return counts.failedOut ? "error" : tracker.phase;
            if (counts.settled) return counts.failed > 0 ? "error" : "idle";
            return tracker.refined ? "refining" : "preview";
        }

        statusEmit() {
            const tracker = this.tracker;
            if (!tracker) return;
            const counts = this.statusCounts(tracker.items());
            const phase = this.statusPhase(tracker, counts);
            const sent = [tracker.generation, phase, counts.loaded, counts.needed, counts.failed,
                          counts.level].join("|");
            if (sent === tracker.sent) return;
            tracker.sent = sent;
            // Before the handover the phase is fetching or slow; an error
            // then is read off the items, and gone once they are retried.
            if (tracker.landed) tracker.phase = phase;
            // Nothing more on its way: nothing more to say until the next
            // handover, or a retry (retryFailed listens again).
            if (phase === "idle" || phase === "error") this.statusStop();
            const detail = {
                surface: this.axis, rev: tracker.rev, token: tracker.token, phase,
                loaded: counts.loaded, needed: counts.needed, failed: counts.failed,
                level: counts.level, since: tracker.since,
                retry: () => this.retryFailed(),
            };
            this.lastStatus = detail;
            if (typeof global.CustomEvent !== "function" || typeof global.dispatchEvent !== "function") return;
            global.dispatchEvent(new global.CustomEvent("plexora:surface-status", { detail }));
        }

        /** Ask again for the tiles whose retries are spent
         *  (tileFetch.retryFailed) -- of the section on screen, and of one
         *  still waiting to hand over; how many were asked for. */
        retryFailed() {
            const fetcher = this.owner.imageViewer?.tileFetch;
            if (typeof fetcher?.retryFailed !== "function") return 0;
            const tracker = this.tracker;
            const waiting = tracker && !tracker.landed ? tracker.items() : [];
            const count = fetcher.retryFailed([...new Set([...this.items.values(), ...waiting])]);
            if (count && tracker?.landed) {
                tracker.refined = true;
                this.statusListen(tracker);
                this.statusPoke();
            } else if (count) {
                this.statusPoke();
            }
            return count;
        }

        /** Match the plane's scale and position (x for XZ, y for YZ). */
        align() {
            if (!this.layout()) return;
            this.owner.locateCut();
            this.apply();
        }

        /**
         * Size the pane: the user's chosen share of the plane or, unchosen, as
         * tall (XZ) or wide (YZ) as its stack at the plane's scale up to the
         * default share. A stack taller than the pane is scrolled to keep the
         * plane on screen in view; a pane taller than its stack shows every
         * plane, stretched along z by `factor` to fill it.
         *
         * `view`: {top, rows, scale, size, inner, factor, natural, whole,
         * margin} -- `size` the pane's screen px across, `inner` its own
         * viewer's (size / factor), `natural` the whole stack's at the plane's
         * scale, `margin` the screen px of rows its own viewer reaches past
         * each edge of a scrolled pane (PANE_ROW_MARGIN_PX; 0 for a pane that
         * holds every plane). The pane's root clips them: they are loaded,
         * not shown.
         */
        layout() {
            const owner = this.owner;
            const frame = owner.frame;
            if (!frame || !this.geometry) return null;
            const rows = this.geometry.height;
            const scale = frame.scale;                       // screen px per image px
            const across = this.axis === "xz" ? frame.height : frame.width;
            const share = owner.shareOf(this.axis);
            const natural = rows * scale;
            const limit = owner.maxPanePx(this.axis, frame);
            let asked = share !== null ? across * share : Math.min(across * MAX_PANE_FRACTION, natural);
            if (share !== null && Math.abs(asked - natural) <= SNAP_PX) asked = natural;
            const shown = Math.round(Math.max(MIN_PANE_PX, Math.min(limit, asked)));
            // Stretched only past the detent: a pane a pixel or two taller than
            // its stack (rounding) shows it at the plane's proportions.
            const factor = shown > natural + SNAP_PX ? shown / Math.max(1e-6, natural) : 1;
            const inner = shown / factor;
            const rowsShown = Math.min(rows, inner / scale);
            const zRow = owner.zRow(rows);
            let top = 0;
            if (rowsShown < rows) {
                top = Math.max(0, Math.min(rows - rowsShown, zRow - rowsShown / 2));
            }
            //: Whether the pane holds every plane (to the pixel it is rounded to).
            const whole = natural <= shown + 0.5;
            // A scrolled pane's viewer reaches past both edges by the same
            // amount, so its centre -- and everything apply() lines up -- is
            // where it was. Never a stretched one (factor > 1 is whole).
            const margin = whole ? 0
                : Math.floor(Math.min(PANE_ROW_MARGIN_PX, Math.max(0, (natural - shown) / 2)));
            this.view = { top, rows: rowsShown, scale, size: shown, inner, factor, natural, whole, margin };
            const stretch = factor > 1;
            this.root.classList.toggle("is-stretched", stretch);
            const reach = inner + 2 * margin;
            if (this.axis === "xz") {
                this.root.style.height = `${shown}px`;
                this.canvasHost.style.height = `${reach}px`;
                this.canvasHost.style.transform = stretch ? `scaleY(${factor})`
                    : margin ? `translateY(${-margin}px)` : "";
                owner.wrapper?.style.setProperty("--plx-section-xz-h", `${shown}px`);
            } else {
                this.root.style.width = `${shown}px`;
                this.canvasHost.style.width = `${reach}px`;
                this.canvasHost.style.transform = stretch ? `scaleX(${factor})`
                    : margin ? `translateX(${-margin}px)` : "";
                owner.wrapper?.style.setProperty("--plx-section-yz-w", `${shown}px`);
            }
            // The pane's band in the canvas layout (services/canvasLayout.js):
            // the legend, the mini-map and any plugin's dock step clear of it.
            global.PlexoraCanvasLayout?.reserve?.(`sections-${this.axis}`, {
                edge: this.axis === "xz" ? "bottom" : "right", size: shown, order: 10 });
            const percent = Math.round((shown / Math.max(1, across)) * 100);
            this.divider.setAttribute("aria-valuenow", String(percent));
            this.divider.setAttribute("aria-valuetext",
                `${percent}% of the plane${share === null ? " (default)" : ""}`);
            return this.view;
        }

        /** Point the pane's own viewer at its rows, lined up with the plane. */
        apply() {
            const owner = this.owner;
            const frame = owner.frame;
            const view = this.view;
            if (!frame || !view || !this.osd || !this.geometry) return;
            const geometry = this.geometry;
            const { top, rows: rowsShown, scale } = view;
            // The pane's own viewer is `inner` across; a stretched pane scales
            // it up to its size on screen (layout). A scrolled one reaches
            // `margin` past each edge (layout moves it back by as much): the
            // same centre, the same scale, rows loaded beyond what shows.
            const inner = view.inner ?? view.size;
            const reach = inner + 2 * (view.margin || 0);
            const along = this.axis === "xz" ? [frame.x0, frame.x1] : [frame.y0, frame.y1];
            const width = geometry.width;                    // image px along the section
            const containerWidth = this.axis === "xz" ? frame.width : reach;
            const containerHeight = this.axis === "xz" ? reach : frame.height;
            // The pane was just resized; tell the viewport now rather than at
            // its next frame, or the zoom below is computed for the old size.
            this.osd.viewport.resize(new OpenSeadragon.Point(containerWidth, containerHeight),
                                     false);
            const zoom = (scale * width) / Math.max(1, containerWidth);
            const centre = new OpenSeadragon.Point(
                ((along[0] + along[1]) / 2) / width,
                (top + rowsShown / 2) / width);
            this.osd.viewport.zoomTo(zoom, null, true);
            this.osd.viewport.panTo(centre, true);
            this.paintZLine();
            this.paintOverlay();
            // Resizing a canvas clears it, and the pane's canvas follows its
            // container a frame later -- after a draw that then never repeats,
            // because nothing in the world changed. So draw again, now and
            // once the canvas has its new size.
            this.redraw();
            cancelAnimationFrame(this._redraw);
            this._redraw = requestAnimationFrame(() => this.redraw());
        }

        /** Draw every item again. `forceRedraw` alone is not enough in OSD 6:
         *  an item that thinks it is clean draws nothing (glInit.js marks
         *  them the same way). */
        redraw() {
            const world = this.osd?.world;
            if (!world) return;
            for (let i = 0; i < world.getItemCount(); i += 1) world.getItemAt(i)._needsDraw = true;
            this.osd.forceRedraw();
        }

        paintZLine() {
            const view = this.view;
            if (!view || !this.geometry) return;
            const offset = (this.owner.zRow(this.geometry.height) - view.top) * view.scale
                * (view.factor || 1);
            const visible = offset >= 0 && offset <= view.size;
            this.zline.hidden = !visible;
            if (this.axis === "xz") {
                this.zline.style.top = `${offset}px`;
            } else {
                this.zline.style.left = `${offset}px`;
            }
        }

        /**
         * Every overlay's spans along this pane's cut. A provider answers
         * `{axis, at, depth, t}` -- the cut is the line y = `at` (XZ) or
         * x = `at` (YZ) of the plane, in image pixels -- with
         * `[{color, selected, spans: [{z0, z1, a0, a1}]}]`: planes z0..z1 by
         * image pixels a0..a1 along the cut. A shape with `outline: true` is a
         * cell (sectionCells.js): its spans, a0..a1 inclusive, are drawn as
         * one stepped outline and not filled, so the tissue shows through.
         */
        paintOverlay() {
            const canvas = this.overlay;
            const owner = this.owner;
            const view = this.view;
            const frame = owner.frame;
            const ratio = global.devicePixelRatio || 1;
            const width = Math.round(this.root.clientWidth * ratio);
            const height = Math.round(this.root.clientHeight * ratio);
            if (canvas.width !== width) canvas.width = width;
            if (canvas.height !== height) canvas.height = height;
            const context = canvas.getContext("2d");
            if (!context) return;
            context.setTransform(1, 0, 0, 1, 0, 0);
            context.clearRect(0, 0, width, height);
            if (!frame || !view || !this.geometry || !owner.overlays.size) return;
            context.scale(ratio, ratio);
            const xz = this.axis === "xz";
            const query = { axis: this.axis, at: Math.round(xz ? frame.cy : frame.cx),
                            depth: Math.max(1, owner.viewState.depth),
                            t: owner.viewState.get()?.t || 0, scale: view.scale };
            const rows = this.geometry.height;
            const start = xz ? frame.x0 : frame.y0;
            // Screen px per image row across the pane: z is stretched with it.
            const zScale = view.scale * (view.factor || 1);
            for (const provider of owner.overlays.values()) {
                let shapes = [];
                try {
                    shapes = provider(query) || [];
                } catch (error) {
                    console.error("sectionPanes: an overlay failed", error);
                }
                for (const shape of shapes) {
                    context.fillStyle = shape.color || "#8b93a6";
                    context.strokeStyle = shape.color || "#8b93a6";
                    context.lineWidth = shape.selected ? 2 : 1;
                    if (shape.outline) {
                        strokeCellOutline(context, shape.spans || [], {
                            along: (a) => (a - start) * view.scale,
                            across: (z) => ((z / query.depth) * rows - view.top) * zScale,
                            xz,
                        });
                        continue;
                    }
                    for (const span of shape.spans || []) {
                        const along0 = (span.a0 - start) * view.scale;
                        const along1 = (span.a1 - start) * view.scale;
                        const across0 = ((span.z0 / query.depth) * rows - view.top) * zScale;
                        const across1 = (((span.z1 + 1) / query.depth) * rows - view.top) * zScale;
                        const [x, y, w, h] = xz
                            ? [along0, across0, along1 - along0, across1 - across0]
                            : [across0, along0, across1 - across0, along1 - along0];
                        context.globalAlpha = shape.selected ? 0.42 : 0.3;
                        context.fillRect(x, y, w, h);
                        // The edges along the cut only: stacked planes then
                        // read as one stepped outline, not a pile of boxes.
                        context.globalAlpha = 0.9;
                        context.beginPath();
                        if (xz) {
                            context.moveTo(x + 0.5, y);
                            context.lineTo(x + 0.5, y + h);
                            context.moveTo(x + w - 0.5, y);
                            context.lineTo(x + w - 0.5, y + h);
                        } else {
                            context.moveTo(x, y + 0.5);
                            context.lineTo(x + w, y + 0.5);
                            context.moveTo(x, y + h - 0.5);
                            context.lineTo(x + w, y + h - 0.5);
                        }
                        context.stroke();
                    }
                    // Polylines, `[a0, z0, a1, z1, ...]` in image pixels along
                    // the cut and planes (a surface's contour where the pane
                    // cuts it: views/surfaceStudio.js).
                    if (shape.lines?.length) {
                        context.globalAlpha = 0.95;
                        context.lineWidth = shape.selected ? 2 : 1.5;
                        context.setLineDash(shape.dash || [4, 3]);
                        for (const line of shape.lines) {
                            context.beginPath();
                            for (let i = 0; i + 1 < line.length; i += 2) {
                                const along = (line[i] - start) * view.scale;
                                const across = (((line[i + 1] + 0.5) / query.depth) * rows - view.top)
                                    * zScale;
                                const [x, y] = xz ? [along, across] : [across, along];
                                if (i === 0) context.moveTo(x, y);
                                else context.lineTo(x, y);
                            }
                            context.stroke();
                        }
                        context.setLineDash([]);
                    }
                }
            }
            context.globalAlpha = 1;
        }

        onClick(event) {
            const box = this.root.getBoundingClientRect();
            const view = this.view;
            if (!view || !this.geometry) return;
            const along = this.axis === "xz" ? event.clientY - box.top : event.clientX - box.left;
            const row = view.top + along / (view.scale * (view.factor || 1));
            this.owner.setZFromRow(row, this.geometry.height);
        }

        destroy() {
            this.osd?.destroy?.();
            this.root.remove();
            this.divider.remove();
        }
    }

    class SectionPanes {
        constructor({ viewState, imageViewer, viewerManager, config, datasource }) {
            this.viewState = viewState;
            this.imageViewer = imageViewer;
            this.viewerManager = viewerManager;
            this.config = config;
            this.datasource = datasource;
            this.wrapper = document.getElementById("openseadragon_wrapper");
            this.mainElement = document.getElementById("openseadragon");
            this.panes = null;
            this.frame = null;
            this.timer = 0;
            this.enabled = false;
            this.overlays = new Map();
            this.prefs = global.PlexoraViewerPrefs || null;
            //: A size being dragged, before it is saved: {xz?, yz?} shares.
            this.liveShare = {};
            //: The view state the panes DRAW -- the revision on screen, which
            //: the z-line follows -- as opposed to the one asked for, which
            //: the rail follows. Null until the first revision (then: asked).
            this.shown = null;
            //: Whether navigation reaches the panes as revisions (main.js
            //: registers them as a surface); the old subscriber path otherwise.
            this.surface = false;
            //: Counters, for the benchmark and a person asking: handovers still
            //: waiting at PANE_SLOW_MS, and shows superseded before they landed.
            this.slow = 0;
            this.superseded = 0;
            //: Whether the plane's view is easing (OSD `animation` until
            //: `animation-finish`): a section then comes in at the level that
            //: covers, with no grace (grace()).
            this.moving = false;
            //: Each showAt() is a pan; only the newest commits what it loaded
            //: (each pane on its own, as it covers).
            this.panGeneration = 0;
            //: The frame `t` a navigation revision is loading the sections at
            //: (prepare), until it lands or is superseded: a pan meanwhile
            //: cuts at it (tokens), and waits for it (Pane.show).
            this.pendingT = null;
            this.pendingRev = null;
            this._requeue = 0;
            this.targetTimer = 0;
            this.targetKey = "";
            //: The pane whose divider is being dragged ("xz" | "yz"), or null:
            //: the cut is held still and nothing is reloaded until it is let go.
            this.resizing = null;
            //: The section tokens the labels last named.
            this.labelled = null;
        }

        /** The state the panes draw (see `shown`). */
        drawn() {
            return this.shown || this.viewState.get();
        }

        /** A pane's chosen share of the plane, or null for the default. */
        shareOf(axis) {
            if (Number.isFinite(this.liveShare[axis])) return this.liveShare[axis];
            const saved = this.prefs?.sectionShare?.(axis);
            return Number.isFinite(saved) ? saved : null;
        }

        /**
         * The largest a pane may be, in screen px: what leaves MIN_XY_PX of
         * the plane uncovered -- above XZ, and left of YZ, which stands beside
         * the depth strip's band.
         */
        maxPanePx(axis, frame = this.frame) {
            if (!frame) return Infinity;
            if (axis === "xz") return Math.max(MIN_PANE_PX, frame.height - MIN_XY_PX);
            const band = parseFloat(this.wrapper?.style?.getPropertyValue?.("--plx-band-sections-yz")) || 0;
            return Math.max(MIN_PANE_PX, frame.width - band - MIN_XY_PX);
        }

        /** Resize a pane to `px` (its height for XZ, width for YZ). Saved
         *  only with `save`, which a drag passes once, at its end. The size
         *  is clamped first, so a saved share is one that was on screen. */
        resize(axis, px, { save = false } = {}) {
            const frame = this.frame || this.measure();
            if (!frame) return;
            const across = axis === "xz" ? frame.height : frame.width;
            const clamped = Math.max(MIN_PANE_PX, Math.min(this.maxPanePx(axis, frame), px));
            const share = Math.max(MIN_SHARE, Math.min(MAX_SHARE, clamped / Math.max(1, across)));
            if (save) {
                delete this.liveShare[axis];
                this.prefs?.set?.({ sections: { [axis]: share } });
            } else {
                this.liveShare[axis] = share;
            }
            this.follow(save);
        }

        /** Back to the default size: as tall as the stack, up to a third. */
        resetSize(axis) {
            delete this.liveShare[axis];
            this.prefs?.set?.({ sections: { [axis]: null } });
            this.follow(true);
        }

        bindDivider(pane) {
            const divider = pane.divider;
            const axis = pane.axis;
            const wrapper = this.wrapper;
            divider.addEventListener("pointerdown", (event) => {
                if (event.button !== 0) return;
                event.preventDefault();
                event.stopPropagation();
                divider.setPointerCapture?.(event.pointerId);
                const start = axis === "xz" ? event.clientY : event.clientX;
                const startSize = pane.view?.size || MIN_PANE_PX;
                let size = startSize;
                this.resizing = axis;
                divider.classList.add("is-dragging");
                wrapper?.classList.add(`is-resizing-${axis}`);
                const move = (e) => {
                    const at = axis === "xz" ? e.clientY : e.clientX;
                    // Up (XZ) or left (YZ) makes the pane bigger.
                    size = startSize + (start - at);
                    this.resize(axis, size);
                };
                const up = () => {
                    divider.removeEventListener("pointermove", move);
                    divider.removeEventListener("pointerup", up);
                    divider.removeEventListener("pointercancel", up);
                    divider.classList.remove("is-dragging");
                    wrapper?.classList.remove(`is-resizing-${axis}`);
                    // Let go: the cut may move to the new centre, and the
                    // sections through it are asked for once.
                    this.resizing = null;
                    if (size !== startSize) {
                        this.resize(axis, size, { save: true });
                    } else {
                        // Back where it began: nothing to save, nothing held.
                        delete this.liveShare[axis];
                        this.follow(true);
                    }
                };
                divider.addEventListener("pointermove", move);
                divider.addEventListener("pointerup", up);
                divider.addEventListener("pointercancel", up);
            });
            divider.addEventListener("dblclick", (event) => {
                event.preventDefault();
                this.resetSize(axis);
            });
            divider.addEventListener("keydown", (event) => {
                const grow = axis === "xz" ? "ArrowUp" : "ArrowLeft";
                const shrink = axis === "xz" ? "ArrowDown" : "ArrowRight";
                const step = event.shiftKey ? KEY_STEP_LARGE : KEY_STEP;
                if (event.key === grow || event.key === shrink) {
                    event.preventDefault();
                    event.stopPropagation();
                    const size = (pane.view?.size || MIN_PANE_PX) + (event.key === grow ? step : -step);
                    this.resize(axis, size, { save: true });
                } else if (event.key === "Home") {
                    event.preventDefault();
                    event.stopPropagation();
                    this.resetSize(axis);
                }
            });
        }

        /** Draw `provider`'s shapes in both panes (see Pane.paintOverlay). */
        addOverlay(id, provider) {
            this.overlays.set(String(id), provider);
            this.paintOverlays();
            return () => this.removeOverlay(id);
        }

        removeOverlay(id) {
            this.overlays.delete(String(id));
            this.paintOverlays();
        }

        paintOverlays() {
            if (!this.enabled || !this.panes) return;
            for (const pane of Object.values(this.panes)) pane.paintOverlay();
        }

        channels() {
            const current = this.viewerManager?.channelList?.currentChannels || {};
            return Object.entries(current)
                .map(([srcIdx, channel]) => [Number(srcIdx), channel])
                .filter(([, channel]) => channel?.url);
        }

        channelCount() {
            return this.channels().length;
        }

        isHd() {
            return Boolean(this.viewerManager?.isHdMode?.());
        }

        zRow(rows) {
            const state = this.drawn();
            const depth = Math.max(1, this.viewState.depth);
            return ((state.z + 0.5) / depth) * rows;
        }

        setZFromRow(row, rows) {
            const depth = Math.max(1, this.viewState.depth);
            const z = Math.max(0, Math.min(depth - 1, Math.floor((row / rows) * depth)));
            this.viewState.set({ z });
        }

        async ensureBuilt() {
            if (this.panes) return true;
            if (!this.wrapper || typeof OpenSeadragon !== "function") return false;
            const imageViewer = this.imageViewer;
            this.handleTileLoaded = global.PlexoraTileDecode.createTileLoadedHandler({
                decoderPool: imageViewer.decoderPool,
                renderTileLayers: () => {},
                forceRepaint: () => this.redraw(),
            });
            this.tileDrawing = global.PlexoraTileColorize.createTileDrawing({
                renderer: imageViewer.glRenderer,
                floatRange: imageViewer.numericData?.floatRange,
                findCurrentChannel: (sub) => imageViewer.findCurrentChannel(sub),
                itemRange: imageViewer.itemRange
                    ? (channel, source) => imageViewer.itemRange(channel, source) : null,
                selectCenterProps: centerProps(imageViewer),
                labelOutlinesEnabled: () => false,
                modeFlags: () => ({ edge: 0, or: 0 }),
                maskDrawList: () => [],
            });
            const panes = { xz: new Pane(this, "xz"), yz: new Pane(this, "yz") };
            const ds = encodeURIComponent(this.datasource);
            const [xz, yz] = await Promise.all([
                fetch(url(`volume/${ds}/section?view=xzy0`)).then((r) => (r.ok ? r.json() : null)),
                fetch(url(`volume/${ds}/section?view=yzx0`)).then((r) => (r.ok ? r.json() : null)),
            ]).catch(() => [null, null]);
            if (!xz || !yz) return false;
            panes.xz.geometry = xz;
            panes.yz.geometry = yz;
            this.crossX = el("div", "viewer-section-crosshair is-vertical", { "aria-hidden": "true" });
            this.crossY = el("div", "viewer-section-crosshair is-horizontal", { "aria-hidden": "true" });
            for (const pane of Object.values(panes)) {
                this.wrapper.appendChild(pane.root);
                this.wrapper.appendChild(pane.divider);
                pane.build();
                this.bindDivider(pane);
            }
            this.wrapper.append(this.crossX, this.crossY);
            this.panes = panes;
            const viewer = imageViewer.viewer;
            viewer.addHandler("animation", () => {
                this.moving = true;
                this.follow(false);
            });
            viewer.addHandler("animation-finish", () => {
                this.moving = false;
                this.follow(true);
            });
            viewer.addHandler("resize", () => this.follow(true));
            global.addEventListener("plexora:hd-mode-changed", () => this.rebuild());
            // A channel's exact window replacing its provisional one: sections
            // made with it (services/volumeWindows.js).
            global.addEventListener("plexora:volume-window", () => this.rebuild());
            // A size restored in the Depth card, or loaded after the panes.
            global.addEventListener("plexora:viewer-prefs-changed", () => this.follow(true));
            return true;
        }

        /** The plane's frame in image pixels, and how big it is on screen --
         *  where the view is now, or with `target` where it is easing to. */
        measure(target = false) {
            const viewer = this.imageViewer?.viewer;
            const item = viewer?.world?.getItemAt?.(0);
            if (!viewer || !item) return null;
            const bounds = viewer.viewport.getBounds(!target);
            const rect = item.viewportToImageRectangle(bounds);
            const size = viewer.viewport.getContainerSize();
            return {
                x0: rect.x, x1: rect.x + rect.width, y0: rect.y, y1: rect.y + rect.height,
                width: size.x, height: size.y,
                scale: size.x / Math.max(1e-6, rect.width),
                // The view's centre until locateCut knows what the panes cover.
                cx: rect.x + rect.width / 2, cy: rect.y + rect.height / 2,
                cutX: size.x / 2, cutY: size.y / 2,
            };
        }

        /**
         * Where the sections cut: the centre of the part of the plane that no
         * pane covers -- left of YZ, above XZ -- in image pixels (`cx`, `cy`)
         * and in the plane's container pixels (`cutX`, `cutY`).
         */
        locateCut(target = false) {
            const frame = this.frame;
            const viewer = this.imageViewer?.viewer;
            const item = viewer?.world?.getItemAt?.(0);
            const host = this.mainElement;
            if (!frame || !item || !host || typeof OpenSeadragon !== "function") return;
            const box = host.getBoundingClientRect();
            let right = box.right;
            let bottom = box.bottom;
            if (this.enabled && this.panes) {
                const yz = this.panes.yz.root.getBoundingClientRect();
                const xz = this.panes.xz.root.getBoundingClientRect();
                if (yz.width > 0) right = Math.min(right, yz.left);
                if (xz.height > 0) bottom = Math.min(bottom, xz.top);
            }
            const cutX = Math.max(0, (right - box.left) / 2);
            const cutY = Math.max(0, (bottom - box.top) / 2);
            const point = item.viewportToImageCoordinates(
                viewer.viewport.pointFromPixel(new OpenSeadragon.Point(cutX, cutY), !target));
            if (!Number.isFinite(point?.x) || !Number.isFinite(point?.y)) return;
            Object.assign(frame, { cx: point.x, cy: point.y, cutX, cutY });
        }

        /**
         * The section tokens for `frame` (the view's, by default) at frame `t`
         * -- by default the frame a revision is loading the sections at, if
         * one is (`pendingT`), else the one they draw.
         *
         * A cut that moved by less than one screen pixel keeps the token the
         * pane already has: the section through the next image row is the
         * same picture at this zoom, and a new token is a new set of tiles for
         * every channel. Zoomed out, one screen pixel is many image rows.
         */
        tokens(frame = this.frame, t = this.pendingT ?? (this.drawn()?.t || 0)) {
            const info = this.viewState.info || {};
            const width = Number(info.width) || 1;
            const height = Number(info.height) || 1;
            const prefix = this.viewState.frames > 1 && t ? `t${t}` : "";
            let y = Math.max(0, Math.min(height - 1, Math.round(frame.cy)));
            let x = Math.max(0, Math.min(width - 1, Math.round(frame.cx)));
            const slack = Math.max(1, 1 / Math.max(1e-9, frame.scale || 1));
            const held = (pane, axis) => {
                const match = new RegExp(`^${prefix}${axis}(\\d+)$`).exec(pane?.wanted?.() || "");
                return match ? Number(match[1]) : null;
            };
            const heldY = held(this.panes?.xz, "xzy");
            const heldX = held(this.panes?.yz, "yzx");
            if (heldY !== null && Math.abs(heldY - frame.cy) < slack) y = heldY;
            if (heldX !== null && Math.abs(heldX - frame.cx) < slack) x = heldX;
            return { xz: `${prefix}xzy${y}`, yz: `${prefix}yzx${x}`, x, y };
        }

        /**
         * Ask both panes for the section through `frame` (the view's). EACH
         * PANE REVEALS ON ITS OWN, as soon as its new section can draw: the
         * pane slower to cover never holds the other back. Both load
         * deferred, and each is committed as it resolves if no newer pan came
         * since (`panGeneration`) -- an older pan's section never lands over a
         * newer one: it is dropped (`discard`), or, where the newer pan asked
         * for the same section and shares it (Pane.show's claims), left for
         * that pan to commit. A pane's label names its new section at the
         * pane's own commit, `data-pending` until then. Resolves true when
         * this pan was revealed in both panes (a pane whose section is
         * unchanged counts as revealed).
         */
        showAt(frame = this.frame) {
            if (!frame || !this.panes) return Promise.resolve(false);
            const tokens = this.tokens(frame);
            const pan = ++this.panGeneration;
            const axes = ["xz", "yz"];
            for (const axis of axes) {
                if (tokens[axis] !== this.panes[axis].token) this.flagPending(this.panes[axis], true);
            }
            const reveal = (axis) => this.panes[axis].show(tokens[axis], { defer: true })
                .then((closure) => {
                    if (pan !== this.panGeneration) {
                        closure?.discard?.();
                        return false;
                    }
                    closure?.();
                    this.paintLabels(tokens, [axis]);
                    this.settlePending();
                    return true;
                });
            return Promise.all(axes.map(reveal)).then((revealed) => revealed.every(Boolean));
        }

        /** Mark `pane`'s label as naming a section about to be replaced. */
        flagPending(pane, on) {
            const data = pane?.label?.dataset;
            if (!data) return;
            if (on) data.pending = "1";
            else delete data.pending;
        }

        /** After a commit: pending only where a newer section is still coming. */
        settlePending() {
            for (const pane of Object.values(this.panes || {})) {
                const loading = pane.loading;
                this.flagPending(pane, Boolean(pane.after || (loading && loading.token !== pane.token)));
            }
        }

        /** How long a section's handover waits for full resolution once it
         *  covers its pane (Pane.show): none while the view moves or the hand
         *  scrubs through planes -- where it is going is what it wants to see
         *  -- and PANE_GRACE_MS at rest. */
        grace() {
            return this.moving || this.viewState?.scrubbing?.() ? 0 : PANE_GRACE_MS;
        }

        /** showAt() once this task is done: a pan that was queued behind a new
         *  frame's sections, which were dropped (Pane.show). */
        requeue() {
            if (this._requeue) return;
            this._requeue = setTimeout(() => {
                this._requeue = 0;
                if (this.enabled && this.panes && this.frame) this.showAt(this.frame);
            }, 0);
        }

        /**
         * The panes follow the plane. Every frame the view moves, they are
         * laid out and aligned with it (`settled` false); the SECTIONS are
         * refetched only where the cut ends up. That used to be once the view
         * had finished easing and then 180 ms more -- over a second after the
         * hand let go. Now the cut the view is easing TO is known as soon as
         * the hand is still (TARGET_STILL_MS), and its sections load while the
         * view gets there.
         */
        follow(settled) {
            if (!this.enabled || !this.panes) return;
            const held = this.resizing ? this.frame : null;
            this.frame = this.measure();
            if (!this.frame) return;
            for (const pane of Object.values(this.panes)) pane.layout();
            if (held) {
                // A divider being dragged: the cut stays where it was, so the
                // sections on screen stay the right ones and nothing reloads.
                const { cx, cy, cutX, cutY } = held;
                Object.assign(this.frame, { cx, cy, cutX, cutY });
            } else {
                this.locateCut();
            }
            for (const pane of Object.values(this.panes)) pane.apply();
            this.paintCrosshair();
            if (this.labelled) this.paintLabels(this.labelled);
            if (held) return;
            if (settled) {
                clearTimeout(this.targetTimer);
                this.targetKey = "";
                this.showAt(this.frame);
                // Where the cuts now are, for what draws along them (surfaces).
                global.dispatchEvent?.(new CustomEvent("plexora:sections-moved",
                    { detail: { x: this.frame.cx, y: this.frame.cy } }));
                clearTimeout(this.prepareTimer);
                this.prepareTimer = setTimeout(() => this.prepareArea(), PREPARE_DELAY_MS);
                return;
            }
            // OSD raises `animation` every frame the view eases, so the wait
            // restarts only when the place it is heading to moves -- a hand
            // still dragging -- not on every frame of the ease.
            const target = this.measure(true);
            if (!target) return;
            const key = [target.x0, target.y0, target.x1, target.y1].map((v) => v.toFixed(2)).join(",");
            if (key === this.targetKey) return;
            this.targetKey = key;
            clearTimeout(this.targetTimer);
            this.targetTimer = setTimeout(() => {
                const at = this.measure(true);
                if (!at) return;
                this.locateCutIn(at);
                this.showAt(at);
            }, TARGET_STILL_MS);
        }

        /** locateCut for a frame other than the one on screen (a target). */
        locateCutIn(frame) {
            const current = this.frame;
            this.frame = frame;
            try {
                this.locateCut(true);
            } finally {
                this.frame = current;
            }
        }

        /**
         * A stack with a 3-D copy (server/volume/bricks.py): once the view
         * has settled, ask for the area on screen, through every plane, at
         * the finest level the copy can afford -- so the next cut through it
         * reads bricks rather than a tile per plane. Areas already held are
         * skipped by the server; nothing is asked for a stack with no copy.
         */
        prepareArea() {
            const state = this.viewState.info?.bricks?.state;
            if (!this.enabled || !this.frame || !state || state === "not_needed") return;
            const frame = this.frame;
            const sidebar = global.__plexora?.viewerSidebar;
            const channels = (sidebar?.channelSlots || []).filter((slot) => slot.enabled && slot.name)
                .map((slot) => sidebar.dataLayer?.getFullChannelName?.(slot.name) || slot.name);
            fetch(url(`volume/${encodeURIComponent(this.datasource)}/prepare`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    box: { x: frame.x0, y: frame.y0, width: frame.x1 - frame.x0,
                           height: frame.y1 - frame.y0 },
                    channels: channels.length ? channels : undefined,
                }),
            }).catch(() => {});
        }

        rebuild() {
            if (!this.enabled || !this.panes) return;
            for (const pane of Object.values(this.panes)) {
                pane.token = "";
                // The same cut again (another quality, window or channel set):
                // it never dips to a coarser level on the way.
                pane.refresh = true;
            }
            this.follow(true);
        }

        /**
         * The panes' part of a navigation revision (services/viewState.js):
         * the z-line (and the scroll of a pane too small for its stack) moves
         * in the same frame as the plane's picture, never ahead of it; a new
         * frame `t` rebuilds both sections invisibly and reveals them in that
         * frame too. Until then a pan cuts at the new frame (`pendingT`) and
         * waits for it rather than cancelling it (Pane.show).
         */
        prepare(rev) {
            const state = rev.state;
            const show = () => {
                this.shown = state;
                if (!this.enabled || !this.panes) return;
                this.follow(false);
            };
            if (!this.enabled || !this.panes || (this.drawn()?.t || 0) === (state.t || 0)) {
                return show;
            }
            this.frame = this.measure() || this.frame;
            if (!this.frame) return show;
            const t = state.t || 0;
            this.pendingT = t;
            this.pendingRev = rev;
            const release = () => {
                if (this.pendingRev !== rev) return;
                this.pendingT = null;
                this.pendingRev = null;
            };
            rev.onSupersede?.(release);
            const tokens = this.tokens(this.frame, t);
            for (const [axis, pane] of Object.entries(this.panes)) {
                if (tokens[axis] !== pane.token) this.flagPending(pane, true);
            }
            return Promise.all([
                this.panes.xz.show(tokens.xz, { defer: true, rev }),
                this.panes.yz.show(tokens.yz, { defer: true, rev }),
            ]).then(([xz, yz]) => {
                if (rev.superseded) {
                    release();
                    this.settlePending();
                    return null;
                }
                const commit = () => {
                    xz?.();
                    yz?.();
                    show();
                    release();
                    this.paintLabels(tokens);
                    this.settlePending();
                };
                commit.discard = () => {
                    xz?.discard?.();
                    yz?.discard?.();
                    release();
                    this.settlePending();
                };
                return commit;
            });
        }

        redraw() {
            for (const pane of Object.values(this.panes || {})) pane.redraw();
        }

        /** redraw() on the next frame, once however many ask before it (a
         *  contrast drag asks on every tick). */
        scheduleRedraw() {
            if (this._redrawFrame || !this.enabled) return;
            this._redrawFrame = requestAnimationFrame(() => {
                this._redrawFrame = 0;
                this.redraw();
            });
        }

        paintCrosshair() {
            const host = this.mainElement;
            if (!host || !this.frame) return;
            const box = host.getBoundingClientRect();
            const outer = this.wrapper.getBoundingClientRect();
            this.crossX.style.left = `${box.left - outer.left + this.frame.cutX}px`;
            this.crossY.style.top = `${box.top - outer.top + this.frame.cutY}px`;
        }

        /**
         * Name the section each pane draws. `axes`: the panes whose section
         * `tokens` names now (showAt paints a pane at its own commit; both by
         * default); the other keeps the one it named. `labelled` holds each
         * pane's, so a repaint (follow) names what each has on screen.
         */
        paintLabels(tokens, axes = ["xz", "yz"]) {
            const labelled = { ...this.labelled };
            if (axes.includes("xz")) labelled.y = tokens.y;
            if (axes.includes("yz")) labelled.x = tokens.x;
            this.labelled = labelled;
            // "z ×2.4" when z is stretched to fill the pane, "all planes" when
            // the pane holds the whole stack at the plane's proportions.
            const whole = (pane) => {
                const factor = pane.view?.factor || 1;
                if (factor > 1.001) return ` · z ×${factor.toFixed(1)}`;
                return pane.view?.whole ? " · all planes" : "";
            };
            const assumed = (this.viewState.info?.spacing?.z?.source || "missing");
            const note = ["metadata", "manual"].includes(assumed) ? "" : " · z step assumed";
            if (Number.isFinite(labelled.y)) {
                this.panes.xz.label.textContent =
                    `XZ · y ${labelled.y.toLocaleString()}${whole(this.panes.xz)}${note}`;
            }
            if (Number.isFinite(labelled.x)) {
                this.panes.yz.label.textContent =
                    `YZ · x ${labelled.x.toLocaleString()}${whole(this.panes.yz)}${note}`;
            }
        }

        async setEnabled(on) {
            this.enabled = Boolean(on);
            if (this.enabled && !(await this.ensureBuilt())) {
                this.enabled = false;
                return;
            }
            this.wrapper?.classList.toggle("has-sections", this.enabled);
            if (!this.enabled) {
                global.PlexoraCanvasLayout?.release?.("sections-xz");
                global.PlexoraCanvasLayout?.release?.("sections-yz");
            }
            for (const pane of Object.values(this.panes || {})) {
                pane.root.hidden = !this.enabled;
                pane.divider.hidden = !this.enabled;
            }
            if (this.crossX) this.crossX.hidden = !this.enabled;
            if (this.crossY) this.crossY.hidden = !this.enabled;
            if (this.enabled) {
                this.rebuild();
            }
        }

        onState(state) {
            const wanted = state.layout === "sections" && state.mode !== "volume";
            if (wanted !== this.enabled) {
                this.setEnabled(wanted);
                return;
            }
            // Planes and frames reach the panes as revisions (prepare): the
            // z-line moves with the picture, not with the hand.
            if (this.surface || !this.enabled || !this.panes) return;
            if (this._lastZ !== state.z) {
                // A pane too small for its whole stack scrolls with the plane.
                this._lastZ = state.z;
                this.follow(false);
            } else {
                for (const pane of Object.values(this.panes)) {
                    pane.paintZLine();
                    pane.paintOverlay();
                }
            }
            if (this._lastT !== state.t) {
                this._lastT = state.t;
                this.rebuild();
            }
        }

        channelsChanged() {
            if (!this.enabled || !this.panes) return;
            for (const pane of Object.values(this.panes)) {
                pane.token = "";
                pane.refresh = true;
            }
            this.follow(true);
        }
    }

    /** Wire the panes to the viewer. Null for a single-plane stack (a time
     *  series of planes has no sections to cut). */
    function attach(viewState, options) {
        if (!viewState?.enabled?.() || viewState.depth < 2) return null;
        const panes = new SectionPanes({ viewState, ...options });
        viewState.subscribe((state) => panes.onState(state));
        if (typeof viewState.registerSurface === "function") {
            panes.surface = true;
            viewState.registerSurface("panes", { order: 40, prepare: (rev) => panes.prepare(rev) });
        }
        const lists = typeof ChannelList !== "undefined" ? ChannelList : global.ChannelList;
        const handler = options.imageViewer?.eventHandler;
        if (handler?.bind && lists?.events) {
            handler.bind(lists.events.CHANNELS_CHANGE, () => panes.channelsChanged());
            handler.bind(lists.events.COLOR_TRANSFER_CHANGE, () => panes.redraw());
            // A contrast drag: the tiles colorize at draw time, so the panes
            // only need drawing again -- in the same frame as the plane.
            handler.bind(lists.events.BRUSH_MOVE, () => panes.scheduleRedraw());
        }
        panes.onState(viewState.get());
        return panes;
    }

    global.PlexoraSectionPanes = { attach, SectionPanes, Pane };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraSectionPanes;
    }
})(typeof window !== "undefined" ? window : globalThis);
