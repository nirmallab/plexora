/**
 * qcLayers.js - what QC draws on the tissue: its regions and its cells.
 *
 * REGIONS are ROIs (server/roi_link.py writes them into `qc_<category>`
 * categories, one of five), but the ROI plugin draws only while its own tool
 * is on screen, and opening QC stands that tool down. So QC draws its own
 * regions through core's shared overlay (`ctx.layers.addOverlay`), in their
 * category colours, the ROI renderer's weights: a translucent fill and a hairline that
 * is divided by the zoom so it stays a hairline. An EXCLUDE region is a solid
 * outline, a WARN one dashed -- the same filled-versus-ring distinction the
 * panel's swatches make. While the ROI tool is visible as well it is already
 * drawing these same shapes, so this layer stands aside rather than paint
 * every region twice.
 *
 * CELLS are a cell layer of core's (`ownsCellLayer` in the registration), so
 * they get the native Cells control, the card's eye and opacity, outlines when
 * there is a mask and centroids when there is not -- all core's. QC only
 * decides what colour each flagged cell is (`setCellColorLUT`): its reason's,
 * fully opaque when that reason excludes it and lighter when it only warns.
 * A cell with several reasons takes the most important visible one's.
 */
class QcRegionOverlay {

    constructor(ctx) {
        this.ctx = ctx;
        this.handle = null;
        this.enabled = true;
        this.regions = [];
        this.isVisible = () => true;
        this.selectedId = null;
        //: A stroke being drawn by hand (qcDraw.js): {points, color}.
        this.draft = null;
        //: Magic select's clicks for the outline being made ({x, y, label}).
        this.prompts = null;
        this.promptColor = null;
        this._paths = new Map();
    }

    //: Screen-space weights, the ROI renderer's.
    static get STROKE() { return 1.6; }
    static get SELECTED_STROKE() { return 2.6; }
    static get FILL_ALPHA() { return 0.14; }

    attach() {
        if (this.handle) return;
        this.handle = this.ctx.layers?.addOverlay?.({
            id: "regions",
            kind: "shapes",
            draw: (opts) => this.draw(opts),
            hitTest: (x, y, opts) => this.hitTest(x, y, opts),
        }) || null;
        this.schedule();
    }

    setEnabled(on) {
        this.enabled = Boolean(on);
        this.handle?.setVisible?.(this.enabled);
        this.schedule();
    }

    setRegions(regions) {
        const live = new Set(regions.map((r) => r.roi_id));
        for (const id of this._paths.keys()) if (!live.has(id)) this._paths.delete(id);
        this.regions = regions;
        this.schedule();
    }

    schedule() {
        this.handle?.invalidate?.();
    }

    destroy() {
        this.handle?.remove?.();
        this.handle = null;
        this._paths.clear();
    }

    pathFor(region) {
        const cached = this._paths.get(region.roi_id);
        if (cached && cached.geometry === region.geometry) return cached.path;
        const path = new Path2D();
        const geometry = region.geometry || {};
        const polygons = geometry.type === "Polygon" ? [geometry.coordinates]
            : geometry.type === "MultiPolygon" ? geometry.coordinates : [];
        for (const polygon of polygons || []) {
            for (const ring of polygon || []) {
                if (!ring || !ring.length) continue;
                path.moveTo(ring[0][0], ring[0][1]);
                for (let i = 1; i < ring.length; i++) path.lineTo(ring[i][0], ring[i][1]);
                path.closePath();
            }
        }
        this._paths.set(region.roi_id, { path, geometry: region.geometry });
        return path;
    }

    /**
     * The region drawn at image pixel (x, y), topmost (last drawn) first:
     * `{roi_id, region, edge}` -- `edge` when the point is on its outline
     * (within `tolerance` image pixels) rather than inside -- or null. Only
     * what `draw` paints can be hit: nothing while this layer is off or the
     * ROI tool is drawing these shapes itself.
     */
    hitTest(x, y, opts) {
        if (!this.enabled || !this.regions.length) return null;
        if (window.PlexoraToolLoader?.isToolVisible?.("roi")) return null;
        const scratch = QcRegionOverlay.scratch();
        if (!scratch) return null;
        const tolerance = Math.max(0, Number(opts?.tolerance) || 0);
        for (let i = this.regions.length - 1; i >= 0; i--) {
            const region = this.regions[i];
            if (!this.isVisible(region)) continue;
            const box = region.bbox;
            if (box && (x < box[0] - tolerance || x > box[2] + tolerance
                        || y < box[1] - tolerance || y > box[3] + tolerance)) continue;
            const path = this.pathFor(region);
            if (scratch.isPointInPath(path, x, y, "evenodd")) {
                return { roi_id: region.roi_id, region, edge: false };
            }
            if (tolerance > 0) {
                scratch.lineWidth = 2 * tolerance;
                if (scratch.isPointInStroke(path, x, y)) {
                    return { roi_id: region.roi_id, region, edge: true };
                }
            }
        }
        return null;
    }

    /** A 1x1 2D context to ask paths about points (never drawn on). */
    static scratch() {
        if (QcRegionOverlay._scratch !== undefined) return QcRegionOverlay._scratch;
        let context = null;
        try {
            context = typeof OffscreenCanvas === "function"
                ? new OffscreenCanvas(1, 1).getContext("2d")
                : document.createElement("canvas").getContext("2d");
        } catch (e) {
            context = null;
        }
        QcRegionOverlay._scratch = context || null;
        return QcRegionOverlay._scratch;
    }

    draw(opts) {
        const context = opts.context;
        const zoom = opts.zoom || 1;
        // The stroke in hand is drawn whatever else is: it is what the
        // pointer is doing right now.
        if (this.draft && this.draft.points.length > 1) this.drawDraft(context, zoom);
        if (this.prompts && this.prompts.length) this.drawPrompts(context, zoom);
        if (!this.enabled) return;
        // The ROI tool draws every ROI, QC's included, while it is on screen.
        if (window.PlexoraToolLoader?.isToolVisible?.("roi")) return;
        const view = this.ctx.viewer?.viewportImageBounds?.(16) || null;
        for (const region of this.regions) {
            if (!this.isVisible(region)) continue;
            const box = region.bbox;
            if (view && box && (box[2] < view.minX || box[0] > view.maxX
                                || box[3] < view.minY || box[1] > view.maxY)) continue;
            this.drawRegion(context, region, zoom);
        }
    }

    drawDraft(context, zoom) {
        const { points, color, scribble } = this.draft;
        context.save();
        if (scribble) {
            // Magic select's scribble: solid, with a white edge so it reads on
            // any channel colour; red when it marks what to leave out.
            context.beginPath();
            context.moveTo(points[0][0], points[0][1]);
            for (let i = 1; i < points.length; i++) context.lineTo(points[i][0], points[i][1]);
            context.lineJoin = "round";
            context.lineCap = "round";
            context.strokeStyle = "#ffffff";
            context.lineWidth = 6 / zoom;
            context.stroke();
            context.strokeStyle = scribble === "remove" ? "#ef4444" : (color || "#fbbf24");
            context.lineWidth = 3.5 / zoom;
            context.stroke();
            context.restore();
            return;
        }
        context.beginPath();
        context.moveTo(points[0][0], points[0][1]);
        for (let i = 1; i < points.length; i++) context.lineTo(points[i][0], points[i][1]);
        context.strokeStyle = color || "#fbbf24";
        context.lineJoin = "round";
        context.lineCap = "round";
        context.lineWidth = QcRegionOverlay.SELECTED_STROKE / zoom;
        context.setLineDash([5 / zoom, 4 / zoom]);
        context.stroke();
        context.restore();
    }

    /** Magic select's clicks: dots with a white ring, include in the
     *  category's colour, exclude red with a bar (the ROI renderer's). */
    drawPrompts(context, zoom) {
        const radius = 5 / zoom;
        context.save();
        context.setLineDash([]);
        context.lineWidth = 2 / zoom;
        for (const prompt of this.prompts) {
            context.beginPath();
            context.arc(prompt.x, prompt.y, radius, 0, Math.PI * 2);
            context.fillStyle = prompt.label ? (this.promptColor || "#fbbf24") : "#ef4444";
            context.fill();
            context.strokeStyle = "#ffffff";
            context.stroke();
            if (!prompt.label) {
                context.beginPath();
                context.moveTo(prompt.x - radius * 0.55, prompt.y);
                context.lineTo(prompt.x + radius * 0.55, prompt.y);
                context.stroke();
            }
        }
        context.restore();
    }

    drawRegion(context, region, zoom) {
        const selected = region.roi_id === this.selectedId;
        const path = this.pathFor(region);
        const color = region.color || "#9ca3af";
        context.save();
        context.fillStyle = color;
        context.globalAlpha = selected ? QcRegionOverlay.FILL_ALPHA * 1.7
            : QcRegionOverlay.FILL_ALPHA;
        // Even-odd, so an interior ring reads as a hole (the ROI renderer's).
        context.fill(path, "evenodd");
        context.globalAlpha = 1;
        context.strokeStyle = color;
        context.lineJoin = "round";
        context.lineWidth = (selected ? QcRegionOverlay.SELECTED_STROKE
            : QcRegionOverlay.STROKE) / zoom;
        if (region.action === "warn") context.setLineDash([6 / zoom, 4 / zoom]);
        context.stroke(path);
        context.restore();
    }
}

class QcCellLayer {

    constructor(ctx, name) {
        this.ctx = ctx;
        this.name = name;
        this.groups = [];
        this.isVisible = () => true;
        this._frame = null;
        // An empty table at once, never none: a layer with no table is drawn
        // in core's default white, which would paint every cell on the slide
        // the moment this tool opened (Cell Explorer's note on the same rule).
        this.apply(QcCellLayer.emptyLUT());
    }

    //: Alpha for a reason that only warns: the same hue, plainly lighter.
    static get WARN_ALPHA() { return 150; }
    //: Past this a dense table is more bytes than a map of the flagged few.
    static get DENSE_MAX_ID() { return 8_000_000; }

    static emptyLUT() {
        return { map: new Map() };
    }

    static rgb(hex) {
        const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
        if (!m) return [156, 163, 175];
        const n = parseInt(m[1], 16);
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    setGroups(groups) {
        this.groups = groups || [];
        this.recolor();
    }

    recolor() {
        if (this._frame) return;
        const run = () => {
            this._frame = null;
            this.apply(this.build());
        };
        this._frame = typeof requestAnimationFrame === "function"
            ? requestAnimationFrame(run) : setTimeout(run, 0);
    }

    build() {
        const visible = this.groups.filter((g) => this.isVisible(g)
            && g.ids && g.ids.length);
        if (!visible.length) return QcCellLayer.emptyLUT();
        let maxId = 0;
        for (const group of visible) {
            for (const id of group.ids) if (id > maxId) maxId = id;
        }
        const dense = maxId <= QcCellLayer.DENSE_MAX_ID;
        const table = dense ? new Uint8Array(4 * (maxId + 1)) : null;
        const map = dense ? null : new Map();
        // Least important first, so the most important visible reason is the
        // last write and wins. The server sends them most important first.
        for (let g = visible.length - 1; g >= 0; g--) {
            const group = visible[g];
            const [r, gg, b] = QcCellLayer.rgb(group.color);
            const a = group.status === "warn" ? QcCellLayer.WARN_ALPHA : 255;
            for (const id of group.ids) {
                if (dense) {
                    const o = id * 4;
                    table[o] = r;
                    table[o + 1] = gg;
                    table[o + 2] = b;
                    table[o + 3] = a;
                } else {
                    map.set(id, [r, gg, b, a]);
                }
            }
        }
        return dense ? { colors: table, maxId } : { map };
    }

    apply(lut) {
        this.ctx.viewer?.setCellColorLUT?.(this.name, lut);
    }

    destroy() {
        if (this._frame && typeof cancelAnimationFrame === "function") {
            cancelAnimationFrame(this._frame);
        }
        this._frame = null;
    }
}

window.QcRegionOverlay = QcRegionOverlay;
window.QcCellLayer = QcCellLayer;
