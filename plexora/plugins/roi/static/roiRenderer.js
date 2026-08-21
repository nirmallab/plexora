/**
 * roiRenderer.js - drawing the regions over the image.
 *
 * Built on OpenSeadragon.CanvasOverlayHd, which core already loads (base.html)
 * and which does the one hard part: it hands `onRedraw` a 2D context already
 * transformed into FULL-RESOLUTION IMAGE PIXEL space, and re-fires it on every
 * pan and zoom. So this file draws polygons at the coordinates they are stored
 * at and never converts anything.
 *
 * The plugin gets its OWN overlay instance rather than sharing the viewer's.
 * Core's is wired to `selectionPolygonToDraw`, a leftover from the removed
 * lasso that nothing writes to any more, and its onRedraw also paints
 * centroids; stacking a second canvas on the same parent costs nothing and
 * keeps the two from having to know about each other.
 *
 * Two things about that overlay are easy to get wrong and expensive to debug:
 *
 * **onRedraw fires once per world item**, i.e. once per active channel. Drawing
 * unconditionally paints every region N times -- invisible at full opacity,
 * obvious the moment anything is translucent, which regions are. Hence the
 * index guard.
 *
 * **Everything is in image pixels, including line widths.** A 2px stroke is 2
 * IMAGE pixels, so at 10x zoom it is a 20px slab and at 0.1x it vanishes.
 * Dividing by `opts.zoom` is what keeps an outline looking like an outline.
 */
class RoiRenderer {

    constructor(ctx, store) {
        this.ctx = ctx;
        this.store = store;
        this.viewer = ctx.viewer?.viewer || null;

        this.enabled = false;
        this.overlay = null;
        this.draft = null;          // {points, tool, closing} while drawing
        this.hoverId = null;
        this._frame = null;
        this._paths = new Map();    // feature id -> {path, geometry}
    }

    //: Screen-space appearance, divided by the zoom at draw time.
    static get STROKE() { return 1.6; }
    static get SELECTED_STROKE() { return 2.6; }
    static get HANDLE_RADIUS() { return 4.5; }
    static get FILL_ALPHA() { return 0.15; }

    attach() {
        if (this.overlay || !this.viewer) return;
        this.overlay = new OpenSeadragon.CanvasOverlayHd(this.viewer, {
            onRedraw: (opts) => this.draw(opts),
        });
        this.enabled = true;
        this.schedule();
    }

    /** Stop drawing without tearing the overlay down.
     *
     * The overlay registers its own `update-viewport` handler and offers no way
     * to remove it, so "detached" means the callback returns immediately rather
     * than that it stopped being called. Cheap, and the canvas is cleared so
     * nothing of this plugin's is left on screen. */
    setEnabled(enabled) {
        this.enabled = enabled;
        if (!enabled) this.overlay?.clear();
        else this.schedule();
    }

    destroy() {
        this.enabled = false;
        if (this._frame) cancelAnimationFrame(this._frame);
        this._frame = null;
        this._paths.clear();
        if (this.overlay) {
            this.overlay.clear();
            this.overlay._canvasdiv?.remove();
            this.overlay = null;
        }
    }

    /** Repaint on the next frame, however many times this is called first.
     *
     * A pointer-move handler can fire several times between two frames, and a
     * geometry rebuild per event is work whose result is thrown away. */
    schedule() {
        if (!this.enabled || !this.overlay || this._frame) return;
        this._frame = requestAnimationFrame(() => {
            this._frame = null;
            if (!this.enabled || !this.overlay) return;
            this.overlay.resize();
            this.overlay.clear();
            this.overlay._updateCanvas();
        });
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
    pathFor(feature) {
        const cached = this._paths.get(feature.id);
        if (cached && cached.geometry === feature.geometry) return cached.path;

        const path = new Path2D();
        for (const ring of RoiGeometry.rings(feature.geometry)) {
            const points = RoiGeometry.closeRing(ring);
            if (!points.length) continue;
            path.moveTo(points[0][0], points[0][1]);
            for (let i = 1; i < points.length; i++) path.lineTo(points[i][0], points[i][1]);
            path.closePath();
        }
        this._paths.set(feature.id, { path, geometry: feature.geometry });
        return path;
    }

    draw(opts) {
        // Once per world item, and one canvas: draw for the first and let the
        // rest fall through, or every translucent fill is composited N times.
        if (!this.enabled || opts.index !== 0) return;

        const context = opts.context;
        const zoom = opts.zoom || 1;
        const view = this.viewportBounds();

        for (const feature of this.store.visibleFeatures()) {
            if (view && !this.intersects(feature, view)) continue;
            this.drawFeature(context, feature, zoom);
        }
        if (this.draft) this.drawDraft(context, zoom);
    }

    /** The image-pixel rectangle currently on screen, for culling.
     *
     * Culling matters here rather than being premature: a project with several
     * thousand regions zoomed into one corner would otherwise stroke every one
     * of them, every frame, for shapes entirely off screen. */
    viewportBounds() {
        try {
            const item = this.viewer?.world?.getItemAt(0);
            if (!item) return null;
            const rect = item.viewportToImageRectangle(this.viewer.viewport.getBounds(true));
            const scale = 2 ** (this.ctx.config?.extraZoomLevels || 0);
            const pad = 16;
            return {
                minX: rect.x / scale - pad,
                minY: rect.y / scale - pad,
                maxX: (rect.x + rect.width) / scale + pad,
                maxY: (rect.y + rect.height) / scale + pad,
            };
        } catch (error) {
            return null;
        }
    }

    intersects(feature, view) {
        const box = RoiGeometry.bounds(feature.geometry);
        if (!box) return false;
        return !(box.maxX < view.minX || box.minX > view.maxX
            || box.maxY < view.minY || box.minY > view.maxY);
    }

    drawFeature(context, feature, zoom) {
        const category = this.store.category(feature.category_id) || {};
        const color = category.color || "#8b93a6";
        const selected = feature.id === this.store.selectionId;
        const path = this.pathFor(feature);

        context.save();
        // Even-odd so an imported polygon's interior rings read as holes rather
        // than being filled over. Nonzero would paint straight across them.
        context.fillStyle = color;
        context.globalAlpha = selected ? RoiRenderer.FILL_ALPHA * 1.6 : RoiRenderer.FILL_ALPHA;
        context.fill(path, "evenodd");

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
