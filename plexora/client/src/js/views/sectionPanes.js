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
 * RESIZABLE. Each pane's inner edge is a divider: drag it, or focus it and use
 * the arrow keys; double-click (or Home) gives the default size back. A pane
 * is at most the size chosen and never more than its stack is tall at the
 * plane's scale -- the section keeps the plane's proportions, so a bigger pane
 * shows more planes when zoomed in rather than stretched ones. The size is a
 * share of the plane's height (XZ) or width (YZ), saved for the user rather
 * than the image (services/viewerPrefs.js), so it holds across images, modes
 * and sessions. Resizing moves nothing else: the plane, its zoom, the channels
 * and the plane on screen stay put.
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
    const MAX_SHARE = 0.75;
    //: Pixels per arrow key on a divider; Shift for KEY_STEP_LARGE.
    const KEY_STEP = 16;
    const KEY_STEP_LARGE = 64;
    const REBUILD_DELAY_MS = 180;
    //: How long the view stays put before its area is prepared in 3-D.
    const PREPARE_DELAY_MS = 2000;
    const HANDOVER_TIMEOUT_MS = 6000;

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
                imageLoaderLimit: 6,
                timeout: 90000,
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
        }

        tileSource(srcIdx, channel, token) {
            const owner = this.owner;
            const geometry = this.geometry;
            const levels = geometry.level_shapes.length;
            const config = owner.config;
            const entry = config.imageData[srcIdx] || {};
            const hd = owner.isHd();
            return {
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
                getTileUrl(level, x, y) {
                    const serverLevel = this.maxLevel - level;
                    const query = [hd ? "q=hd" : "", entry.srcQuery || "",
                                   `view=${encodeURIComponent(token)}`]
                        .filter(Boolean).join("&");
                    return `${this.src}${serverLevel}/${x}_${y}.png?${query}`;
                },
                getTileKey(level, x, y) {
                    return `16-${srcIdx}-${token}${hd ? "-hd" : ""}-${this.maxLevel - level}-${x}-${y}`;
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
            };
        }

        /** Show the section at `token`: new items load invisibly, the old
         *  ones go once the new can draw -- the plane swap's handover. */
        show(token) {
            if (!this.osd || !this.geometry) return;
            if (token === this.token && this.items.size === this.owner.channelCount()) return;
            this.token = token;
            const channels = this.owner.channels();
            const outgoing = [...this.items.values()];
            this.items = new Map();
            const added = [];
            let pending = channels.length;
            const finish = () => {
                for (const item of added) item.setOpacity(1);
                for (const item of outgoing) {
                    if (this.osd.world.getIndexOfItem(item) >= 0) this.osd.world.removeItem(item);
                }
                this.align();
            };
            if (!pending) {
                finish();
                return;
            }
            let handed = false;
            const hand = () => {
                if (handed) return;
                handed = true;
                finish();
            };
            setTimeout(hand, HANDOVER_TIMEOUT_MS);
            for (const [srcIdx, channel] of channels) {
                this.osd.addTiledImage({
                    tileSource: this.tileSource(srcIdx, channel, token),
                    compositeOperation: "lighter",
                    opacity: outgoing.length ? 0 : 1,
                    preload: true,
                    success: (event) => {
                        this.items.set(srcIdx, event.item);
                        added.push(event.item);
                        // Decoding is asynchronous and finishes after OSD's own
                        // draw for the tile; draw again once the item has
                        // everything it needs.
                        event.item.addHandler("fully-loaded-change", () => {
                            requestAnimationFrame(() => this.redraw());
                        });
                        this.align();
                        const ready = () => {
                            pending -= 1;
                            if (pending <= 0) hand();
                        };
                        if (!outgoing.length || event.item.getFullyLoaded?.()) {
                            ready();
                        } else {
                            const onChange = () => {
                                if (!event.item.getFullyLoaded()) return;
                                event.item.removeHandler("fully-loaded-change", onChange);
                                ready();
                            };
                            event.item.addHandler("fully-loaded-change", onChange);
                        }
                    },
                    error: () => {
                        pending -= 1;
                        if (pending <= 0) hand();
                    },
                });
            }
        }

        /** Match the plane's scale and position (x for XZ, y for YZ). */
        align() {
            if (!this.layout()) return;
            this.owner.locateCut();
            this.apply();
        }

        /**
         * Size the pane: as tall (XZ) or wide (YZ) as its stack at the plane's
         * scale, up to the user's chosen share of the plane or, unchosen, the
         * default; scrolled to keep the plane on screen in view when the stack
         * is the bigger.
         */
        layout() {
            const owner = this.owner;
            const frame = owner.frame;
            if (!frame || !this.geometry) return null;
            const rows = this.geometry.height;
            const scale = frame.scale;                       // screen px per image px
            const across = this.axis === "xz" ? frame.height : frame.width;
            const share = owner.shareOf(this.axis);
            const maxPx = Math.max(MIN_PANE_PX, across * (share ?? MAX_PANE_FRACTION));
            const wanted = rows * scale;
            const shown = Math.round(Math.max(MIN_PANE_PX, Math.min(maxPx, wanted)));
            const rowsShown = Math.min(rows, shown / scale);
            const zRow = owner.zRow(rows);
            let top = 0;
            if (rowsShown < rows) {
                top = Math.max(0, Math.min(rows - rowsShown, zRow - rowsShown / 2));
            }
            //: Whether the pane holds every plane (to the pixel it is rounded to).
            const whole = wanted <= maxPx + 0.5;
            this.view = { top, rows: rowsShown, scale, size: shown, whole };
            if (this.axis === "xz") {
                this.root.style.height = `${shown}px`;
                owner.wrapper?.style.setProperty("--plx-section-xz-h", `${shown}px`);
            } else {
                this.root.style.width = `${shown}px`;
                owner.wrapper?.style.setProperty("--plx-section-yz-w", `${shown}px`);
            }
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
            const { top, rows: rowsShown, scale, size: shown } = view;
            const along = this.axis === "xz" ? [frame.x0, frame.x1] : [frame.y0, frame.y1];
            const width = geometry.width;                    // image px along the section
            const containerWidth = this.axis === "xz" ? frame.width : shown;
            const containerHeight = this.axis === "xz" ? shown : frame.height;
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
            const offset = (this.owner.zRow(this.geometry.height) - view.top) * view.scale;
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
         * image pixels a0..a1 along the cut.
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
                            t: owner.viewState.get()?.t || 0 };
            const rows = this.geometry.height;
            const start = xz ? frame.x0 : frame.y0;
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
                    for (const span of shape.spans || []) {
                        const along0 = (span.a0 - start) * view.scale;
                        const along1 = (span.a1 - start) * view.scale;
                        const across0 = ((span.z0 / query.depth) * rows - view.top) * view.scale;
                        const across1 = (((span.z1 + 1) / query.depth) * rows - view.top) * view.scale;
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
                }
            }
            context.globalAlpha = 1;
        }

        onClick(event) {
            const box = this.root.getBoundingClientRect();
            const view = this.view;
            if (!view || !this.geometry) return;
            const along = this.axis === "xz" ? event.clientY - box.top : event.clientX - box.left;
            const row = view.top + along / view.scale;
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
        }

        /** A pane's chosen share of the plane, or null for the default. */
        shareOf(axis) {
            if (Number.isFinite(this.liveShare[axis])) return this.liveShare[axis];
            const saved = this.prefs?.sectionShare?.(axis);
            return Number.isFinite(saved) ? saved : null;
        }

        /** Resize a pane to `px` (its height for XZ, width for YZ). Saved
         *  only with `save`, which a drag passes once, at its end. */
        resize(axis, px, { save = false } = {}) {
            const frame = this.frame || this.measure();
            if (!frame) return;
            const across = axis === "xz" ? frame.height : frame.width;
            const share = Math.max(MIN_SHARE, Math.min(MAX_SHARE, px / Math.max(1, across)));
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
            const state = this.viewState.get();
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
            viewer.addHandler("animation", () => this.follow(false));
            viewer.addHandler("animation-finish", () => this.follow(true));
            viewer.addHandler("resize", () => this.follow(true));
            global.addEventListener("plexora:hd-mode-changed", () => this.rebuild());
            // A size restored in the Depth card, or loaded after the panes.
            global.addEventListener("plexora:viewer-prefs-changed", () => this.follow(true));
            return true;
        }

        /** The plane's frame in image pixels, and how big it is on screen. */
        measure() {
            const viewer = this.imageViewer?.viewer;
            const item = viewer?.world?.getItemAt?.(0);
            if (!viewer || !item) return null;
            const bounds = viewer.viewport.getBounds(true);
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
        locateCut() {
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
                viewer.viewport.pointFromPixel(new OpenSeadragon.Point(cutX, cutY), true));
            if (!Number.isFinite(point?.x) || !Number.isFinite(point?.y)) return;
            Object.assign(frame, { cx: point.x, cy: point.y, cutX, cutY });
        }

        tokens() {
            const frame = this.frame;
            const info = this.viewState.info || {};
            const width = Number(info.width) || 1;
            const height = Number(info.height) || 1;
            const t = this.viewState.get()?.t || 0;
            const prefix = this.viewState.frames > 1 && t ? `t${t}` : "";
            const y = Math.max(0, Math.min(height - 1, Math.round(frame.cy)));
            const x = Math.max(0, Math.min(width - 1, Math.round(frame.cx)));
            return { xz: `${prefix}xzy${y}`, yz: `${prefix}yzx${x}`, x, y };
        }

        follow(settled) {
            if (!this.enabled || !this.panes) return;
            this.frame = this.measure();
            if (!this.frame) return;
            for (const pane of Object.values(this.panes)) pane.layout();
            this.locateCut();
            for (const pane of Object.values(this.panes)) pane.apply();
            this.paintCrosshair();
            clearTimeout(this.timer);
            const apply = () => {
                const tokens = this.tokens();
                this.panes.xz.show(tokens.xz);
                this.panes.yz.show(tokens.yz);
                this.paintLabels(tokens);
            };
            if (settled) {
                this.timer = setTimeout(apply, REBUILD_DELAY_MS);
                clearTimeout(this.prepareTimer);
                this.prepareTimer = setTimeout(() => this.prepareArea(), PREPARE_DELAY_MS);
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
            for (const pane of Object.values(this.panes)) pane.token = "";
            this.follow(true);
        }

        redraw() {
            for (const pane of Object.values(this.panes || {})) pane.redraw();
        }

        paintCrosshair() {
            const host = this.mainElement;
            if (!host || !this.frame) return;
            const box = host.getBoundingClientRect();
            const outer = this.wrapper.getBoundingClientRect();
            this.crossX.style.left = `${box.left - outer.left + this.frame.cutX}px`;
            this.crossY.style.top = `${box.top - outer.top + this.frame.cutY}px`;
        }

        paintLabels(tokens) {
            // "all planes" when the pane already holds the whole stack -- which
            // is also why dragging its edge further does nothing until you
            // zoom in.
            const whole = (pane) => (pane.view?.whole ? " · all planes" : "");
            this.panes.xz.label.textContent = `XZ · y ${tokens.y.toLocaleString()}${whole(this.panes.xz)}`;
            this.panes.yz.label.textContent = `YZ · x ${tokens.x.toLocaleString()}${whole(this.panes.yz)}`;
            const assumed = (this.viewState.info?.spacing?.z?.source || "missing");
            const note = ["metadata", "manual"].includes(assumed) ? "" : " · z step assumed";
            if (note) {
                this.panes.xz.label.textContent += note;
                this.panes.yz.label.textContent += note;
            }
        }

        async setEnabled(on) {
            this.enabled = Boolean(on);
            if (this.enabled && !(await this.ensureBuilt())) {
                this.enabled = false;
                return;
            }
            this.wrapper?.classList.toggle("has-sections", this.enabled);
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
            if (!this.enabled || !this.panes) return;
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
            for (const pane of Object.values(this.panes)) pane.token = "";
            this.follow(true);
        }
    }

    /** Wire the panes to the viewer. Null for a single-plane stack (a time
     *  series of planes has no sections to cut). */
    function attach(viewState, options) {
        if (!viewState?.enabled?.() || viewState.depth < 2) return null;
        const panes = new SectionPanes({ viewState, ...options });
        viewState.subscribe((state) => panes.onState(state));
        const lists = typeof ChannelList !== "undefined" ? ChannelList : global.ChannelList;
        const handler = options.imageViewer?.eventHandler;
        if (handler?.bind && lists?.events) {
            handler.bind(lists.events.CHANNELS_CHANGE, () => panes.channelsChanged());
            handler.bind(lists.events.COLOR_TRANSFER_CHANGE, () => panes.redraw());
        }
        panes.onState(viewState.get());
        return panes;
    }

    global.PlexoraSectionPanes = { attach, SectionPanes, Pane };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraSectionPanes;
    }
})(typeof window !== "undefined" ? window : globalThis);
