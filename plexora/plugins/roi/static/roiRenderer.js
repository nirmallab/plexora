/**
 * roiRenderer.js - drawing the regions over the image.
 *
 * Drawn through core's shared overlay (`ctx.layers.addOverlay`), which hands the
 * draw callback a 2D context already transformed into FULL-RESOLUTION IMAGE
 * PIXEL space. So this file draws polygons at the coordinates they are stored at
 * and never converts anything.
 *
 * It used to build its own OpenSeadragon.CanvasOverlayHd. Three things came with
 * that, and core now owns all three:
 *
 * **onRedraw fires once per world item**, i.e. once per active channel. Drawing
 * unconditionally paints every region N times -- invisible at full opacity,
 * obvious the moment anything is translucent, which regions are. This file used
 * to guard with `opts.index !== 0`, which is not the test it needs to be: item 0
 * is the RGB base for a brightfield project, whichever channel was added first
 * for a fluorescence one, and whatever was last dragged after any setItemIndex.
 * Core guards with the layer stack's anchor instead.
 *
 * **Every CanvasOverlayHd registers its own `update-viewport` handler and offers
 * no way to remove it**, so this plugin used to leave one behind for the life of
 * the page. There is one host now, and leaving is one entry removed from a map.
 *
 * **Everything is in image pixels, including line widths.** A 2px stroke is 2
 * IMAGE pixels, so at 10x zoom it is a 20px slab and at 0.1x it vanishes.
 * Dividing by the zoom is what keeps an outline looking like an outline. Core
 * passes `opts.px` (= 1 / zoom) so new code can multiply; the methods below keep
 * dividing by `zoom`, because changing the arithmetic would move the last bit of
 * every stroke width for no reason.
 */
class RoiRenderer {

    constructor(ctx, store) {
        this.ctx = ctx;
        this.store = store;
        this.viewer = ctx.viewer?.viewer || null;

        this.enabled = false;
        //: The handle core hands back for this plugin's overlay:
        //: { invalidate, remove, setVisible, setOrder }.
        this.handle = null;
        this.draft = null;          // {points, tool, closing} while drawing
        //: Magic select's clicks ({x, y, label}) for the outline being made:
        //: an include point in the category colour, an exclude point in red.
        this.prompts = null;
        this.hoverId = null;
        this._paths = new Map();    // feature id -> {path, geometry}
    }

    //: Screen-space appearance, divided by the zoom at draw time.
    static get STROKE() { return 1.6; }
    static get SELECTED_STROKE() { return 2.6; }
    static get HANDLE_RADIUS() { return 4.5; }
    static get FILL_ALPHA() { return 0.15; }

    attach() {
        if (this.handle) return;
        // Named for what it draws rather than for the plugin: core prefixes the
        // id with the plugin's name itself, so two plugins cannot collide and
        // everything this one drew can be removed without listing it.
        this.handle = this.ctx.layers?.addOverlay?.({
            id: "regions",
            kind: "shapes",
            draw: (opts) => this.draw(opts),
        }) || null;
        this.enabled = true;
        // A z-stack: a region limited to some planes is drawn differently
        // once those planes leave the screen, so the change of plane repaints.
        this._offView = this.ctx.view?.onChange?.(() => this.schedule()) || null;
        this.schedule();
    }

    /** Stop drawing without giving the overlay up.
     *
     * A hidden overlay is skipped by the host, so this genuinely stops costing
     * anything rather than returning early from a callback that still fires --
     * which is what it meant while this plugin owned its own canvas. */
    setEnabled(enabled) {
        this.enabled = enabled;
        this.handle?.setVisible(enabled);
        if (enabled) this.schedule();
    }

    destroy() {
        this.enabled = false;
        this._offView?.();
        this._offView = null;
        this._paths.clear();
        this.handle?.remove();
        this.handle = null;
    }

    /** Repaint on the next frame, however many times this is called first.
     *
     * Core coalesces to one frame, so calling this from a pointer-move handler
     * that fires several times between two paints costs one repaint. */
    schedule() {
        if (!this.enabled) return;
        this.handle?.invalidate();
        // The section panes draw where they cut the regions (sectionSpans).
        this.ctx.view?.repaintSections?.();
    }

    /**
     * The section panes' question (views/sectionPanes.js): where does the cut
     * `axis` at `at` cross each region, plane by plane. A flat region is one
     * span over the planes it covers; one with a shape through the planes is
     * its outline's crossing on each plane.
     */
    sectionSpans({ axis, at, depth, t = 0 }) {
        if (!this.enabled) return [];
        const out = [];
        for (const feature of this.store.visibleFeatures()) {
            const extent = feature.extent && feature.extent.kind !== "all" ? feature.extent : null;
            if (extent && (extent.t || 0) !== t) continue;
            const [low, high] = !extent ? [0, depth - 1]
                : extent.kind === "plane" ? [extent.z, extent.z] : [extent.z0, extent.z1];
            const spans = [];
            if (!feature.solid) {
                for (const [a0, a1] of RoiGeometry.lineCuts(feature.geometry, axis, at)) {
                    spans.push({ z0: low, z1: high, a0, a1 });
                }
            } else {
                for (let z = low; z <= high; z++) {
                    const section = RoiGeometry.sectionAt(feature, z);
                    if (!section) continue;
                    for (const [a0, a1] of RoiGeometry.lineCuts(section, axis, at)) {
                        spans.push({ z0: z, z1: z, a0, a1 });
                    }
                }
            }
            if (!spans.length) continue;
            const category = this.store.category(feature.category_id) || {};
            out.push({ color: category.color || "#8b93a6", spans,
                       selected: feature.id === this.store.selectionId });
        }
        return out;
    }

    /** Drop a cached path because its geometry changed. */
    invalidate(featureId) {
        if (featureId) this._paths.delete(featureId);
        else this._paths.clear();
    }

    /**
     * A Path2D for one feature, cached.
     *
     * Cacheable because the coordinates are in image space and therefore do not
     * change when the user pans or zooms -- the context transform does. So
     * scrolling around a project with a thousand regions rebuilds nothing, and
     * dragging one vertex rebuilds one shape.
     */
    pathFor(feature, geometry = feature.geometry) {
        const cached = this._paths.get(feature.id);
        if (cached && cached.geometry === geometry) return cached.path;

        const path = new Path2D();
        for (const ring of RoiGeometry.rings(geometry)) {
            const points = RoiGeometry.closeRing(ring);
            if (!points.length) continue;
            path.moveTo(points[0][0], points[0][1]);
            for (let i = 1; i < points.length; i++) path.lineTo(points[i][0], points[i][1]);
            path.closePath();
        }
        this._paths.set(feature.id, { path, geometry });
        return path;
    }

    /**
     * What a region looks like on the planes on screen: a region with a shape
     * through a z-stack is its outline on the one plane shown, and its
     * footprint under a projection (which shows many planes at once) or on a
     * plane it does not reach (drawn dashed: see onScreenPlanes).
     */
    shownGeometry(feature) {
        if (!feature.solid) return feature.geometry;
        const shown = this.ctx.view?.planes?.();
        if (!shown || shown[0] !== shown[1]) return feature.geometry;
        return RoiGeometry.sectionAt(feature, shown[0]) || feature.geometry;
    }

    draw(opts) {
        // No index guard: core draws overlays on the anchor item's pass only,
        // and knows which item that is. See OverlayHost.drawAll.
        if (!this.enabled) return;

        const context = opts.context;
        const zoom = opts.zoom || 1;
        const view = this.viewportBounds();

        for (const feature of this.store.visibleFeatures()) {
            if (view && !this.intersects(feature, view)) continue;
            this.drawFeature(context, feature, zoom);
        }
        if (this.draft) this.drawDraft(context, zoom);
        if (this.prompts && this.prompts.length) this.drawPrompts(context, zoom);
    }

    /** Magic select's clicks: filled dots with a white ring, so they read on
     *  any channel colour; include in the category's colour, exclude red. */
    drawPrompts(context, zoom) {
        const color = (this.store.activeCategory || {}).color || "#38bdf8";
        const radius = RoiRenderer.HANDLE_RADIUS / zoom;
        context.save();
        context.setLineDash([]);
        context.lineWidth = 2 / zoom;
        for (const prompt of this.prompts) {
            context.beginPath();
            context.arc(prompt.x, prompt.y, radius, 0, Math.PI * 2);
            context.fillStyle = prompt.label ? color : "#ef4444";
            context.fill();
            context.strokeStyle = "#ffffff";
            context.stroke();
            if (!prompt.label) {
                const arm = radius * 0.55;
                context.beginPath();
                context.moveTo(prompt.x - arm, prompt.y);
                context.lineTo(prompt.x + arm, prompt.y);
                context.stroke();
            }
        }
        context.restore();
    }

    /** The image-pixel rectangle currently on screen, for culling.
     *
     * Culling matters here rather than being premature: a project with several
     * thousand regions zoomed into one corner would otherwise stroke every one
     * of them, every frame, for shapes entirely off screen.
     *
     * Core's, not this plugin's. The conversion involves dividing by
     * `2 ** extraZoomLevels`, which is the part that is silently wrong by a
     * power of two when it is forgotten -- and every plugin that culls needs it.
     */
    viewportBounds() {
        return this.ctx.viewer?.viewportImageBounds?.(16) || null;
    }

    intersects(feature, view) {
        const box = RoiGeometry.bounds(feature.geometry);
        if (!box) return false;
        return !(box.maxX < view.minX || box.minX > view.maxX
            || box.maxY < view.minY || box.minY > view.maxY);
    }

    /**
     * Whether a region covers a plane that is on screen.
     *
     * A region with no extent covers every plane -- every region on a flat
     * image, and every one drawn before z-stacks existed -- so this is true
     * for all of them and nothing about how they draw changes.
     */
    onScreenPlanes(feature) {
        const extent = feature.extent;
        if (!extent || extent.kind === "all") return true;
        const shown = this.ctx.view?.planes?.();
        if (!shown) return true;
        // A time series: a region drawn on one frame is not on the others.
        if ((extent.t || 0) !== (this.ctx.view?.frame?.() || 0)) return false;
        const [low, high] = extent.kind === "plane"
            ? [extent.z, extent.z] : [extent.z0, extent.z1];
        return !(high < shown[0] || low > shown[1]);
    }

    drawFeature(context, feature, zoom) {
        const category = this.store.category(feature.category_id) || {};
        const color = category.color || "#8b93a6";
        const selected = feature.id === this.store.selectionId;
        const path = this.pathFor(feature, this.shownGeometry(feature));
        // On another plane: an outline to find it by, never a filled region
        // that reads as being here.
        const elsewhere = !this.onScreenPlanes(feature);

        context.save();
        // Even-odd so an imported polygon's interior rings read as holes rather
        // than being filled over. Nonzero would paint straight across them.
        if (!elsewhere) {
            context.fillStyle = color;
            context.globalAlpha = selected ? RoiRenderer.FILL_ALPHA * 1.6 : RoiRenderer.FILL_ALPHA;
            context.fill(path, "evenodd");
        }

        context.globalAlpha = 1;
        context.strokeStyle = color;
        context.lineWidth = (selected ? RoiRenderer.SELECTED_STROKE : RoiRenderer.STROKE) / zoom;
        context.lineJoin = "round";

        if (feature.flags && feature.flags.self_intersecting) {
            // Marked, never corrected. A dashed outline is the whole
            // notification: the shape is stored exactly as drawn, and what to
            // do about a bow-tie is the user's call, not this renderer's.
            context.setLineDash([7 / zoom, 4 / zoom]);
        }
        if (this.store.isLocked(feature)) {
            context.globalAlpha = 0.75;
        }
        if (elsewhere) {
            context.setLineDash([3 / zoom, 5 / zoom]);
            context.globalAlpha = 0.5;
        }
        context.stroke(path);
        context.setLineDash([]);

        if (feature.id === this.hoverId && !selected) {
            context.globalAlpha = 0.35;
            context.lineWidth = (RoiRenderer.SELECTED_STROKE + 2) / zoom;
            context.stroke(path);
        }
        context.restore();

        if (selected) this.drawHandles(context, feature, zoom, color);
    }

    /** Vertex handles, on the selected shape only.
     *
     * Not drawn for a locked shape or one whose vertices cannot be edited (a
     * hole-bearing or multi-part import): a handle that does nothing when
     * dragged is worse than no handle. */
    drawHandles(context, feature, zoom, color) {
        if (this.store.isLocked(feature)) return;
        // A shape through the planes is reshaped by carrying it again, not by
        // dragging the vertices of whichever plane is on screen.
        if (feature.solid) return;
        if (!RoiGeometry.isVertexEditable(feature.geometry)) return;

        const radius = RoiRenderer.HANDLE_RADIUS / zoom;
        const points = RoiGeometry.openRing(feature.geometry.coordinates[0]);

        context.save();
        context.lineWidth = 1.4 / zoom;
        for (const [x, y] of points) {
            context.beginPath();
            context.arc(x, y, radius, 0, Math.PI * 2);
            context.fillStyle = "#0b0f15";
            context.fill();
            context.strokeStyle = color;
            context.stroke();
        }
        context.restore();
    }

    /** The shape being drawn right now: dashed, so it never looks committed. */
    drawDraft(context, zoom) {
        const { points, tool } = this.draft;
        if (!points.length) return;
        const color = (this.store.activeCategory || {}).color || "#38bdf8";

        if (tool === "scribble" || tool === "scribble-remove") {
            // Magic select's scribble: an open line, solid, in the colour its
            // points will be (the category's to include, red to leave out),
            // with a white edge so it reads on any channel colour.
            context.save();
            context.lineJoin = "round";
            context.lineCap = "round";
            context.setLineDash([]);
            context.beginPath();
            context.moveTo(points[0][0], points[0][1]);
            for (let i = 1; i < points.length; i++) context.lineTo(points[i][0], points[i][1]);
            context.strokeStyle = "#ffffff";
            context.lineWidth = 6 / zoom;
            context.stroke();
            context.strokeStyle = tool === "scribble" ? color : "#ef4444";
            context.lineWidth = 3.5 / zoom;
            context.stroke();
            context.restore();
            return;
        }

        context.save();
        context.strokeStyle = color;
        context.fillStyle = color;
        context.lineWidth = RoiRenderer.SELECTED_STROKE / zoom;
        context.lineJoin = "round";
        context.setLineDash([8 / zoom, 5 / zoom]);

        context.beginPath();
        context.moveTo(points[0][0], points[0][1]);
        for (let i = 1; i < points.length; i++) context.lineTo(points[i][0], points[i][1]);
        if (tool !== "polygon") context.closePath();
        context.stroke();

        if (points.length > 2) {
            context.globalAlpha = 0.1;
            context.setLineDash([]);
            context.fill();
        }
        context.restore();

        if (tool === "polygon") {
            // Only the polygon tool has committed vertices worth showing: the
            // others are a single continuous gesture with nothing to click back
            // to.
            const radius = (RoiRenderer.HANDLE_RADIUS - 1) / zoom;
            context.save();
            context.fillStyle = color;
            for (const [x, y] of points) {
                context.beginPath();
                context.arc(x, y, radius, 0, Math.PI * 2);
                context.fill();
            }
            context.restore();
        }
    }
}
