/**
 * A standard Visium run's spots: the HD panel's genes, drawn as circles.
 *
 * EVERYTHING THE PANEL SAYS IS BinLayer's -- the gene list, the groups, the
 * colours, the mode, the scale, how several genes combine, the window as
 * fractions of the automatic one, the opacity, the saved state. What differs
 * is the drawing. A Visium HD slide is a counted grid, which core stores and
 * colours as tiles; a standard slide is a few thousand 55 micron spots with
 * no grid to pool, so there are no tiles and no bin sizes. The spots' values
 * come whole from `/plugins/visium_hd/spot_values` -- one gene is 4,000
 * numbers -- and are drawn here, on core's overlay canvas (`addOverlay`), in
 * the spots layer's own full-res frame: its transform takes them into the
 * reference like any layer's.
 *
 * THE SAME TWO PICTURES. The heatmap is one colour per spot off the ramp,
 * stretched the way `bin_tiles.stretch` stretches a square: the window is
 * the combined genes' automatic windows (the 99th percentile of each gene's
 * non-empty spots) times the handles' fractions, linear or through log1p.
 * The composition is each spot as a pie of its genes' and groups' shares --
 * the round form of the HD square's treemap, with the same rule: a group
 * earns its share by its members combined (mean, sum, max, min) and splits
 * it among them by their counts.
 *
 * Drawn in colour BATCHES -- every spot of one ramp colour is one path,
 * every wedge of one gene is one -- and drawn ONCE, into a bitmap that each
 * frame of a pan or zoom only copies (`draw`).
 */
class SpotLayer extends BinLayer {

    constructor(ctx, layerId, api = null) {
        super(ctx, layerId, api);
        //: gene -> Float32Array over spots, and gene -> automatic window.
        this._values = new Map();
        this._windows = new Map();
        //: What `draw` paints, rebuilt by `show` whenever the style changes.
        this._paint = null;
        this._overlay = null;
        this._showToken = 0;
        //: The hovered gene's picture, drawn in `_paint`'s place while the
        //: pointer is on it in the list, and the last few kept -- with their
        //: bitmaps -- so going back up the list costs nothing.
        this._hoverPaint = null;
        this._hoverPaints = new Map();
        this._hoverToken = 0;
    }

    //: Hover pictures kept. Each carries a bitmap of a few megabytes.
    static get HOVER_CACHE() { return 6; }

    //: Genes per `/spot_values` request -- the route's own cap.
    static get SPOT_VALUE_BATCH() { return 64; }

    /** No grid, so no ladder: the panel hides the bin-size row. */
    binLadder() { return []; }

    snapMicrons(microns) { return Number(microns) || 55; }

    pooling() { return 1; }

    gridMicrons() { return Number(this.manifest?.spot_um) || 55; }

    spotCount() { return Number(this.manifest?.spot_count) || 0; }

    async attach() {
        try {
            this.manifest = await this.api.spots(this.layerId);
        } catch (error) {
            console.error("visium_hd: the spots could not be read", error);
            this.manifest = null;
        }
        if (!this.manifest || this.manifest.status !== "ready") return null;
        this._x = Float32Array.from(this.manifest.x || []);
        this._y = Float32Array.from(this.manifest.y || []);
        this.indexGenes();
        this.normalizeState();
        this.ctx.layers?.claim?.(this.layerId);
        const record = this.ctx.layers?.get?.(this.layerId);
        if (record) {
            this.visible = record.visible !== false;
            this.ctx.layers?.setOpacity?.(this.layerId, this.state.opacity);
        }
        this._overlay = this.ctx.layers?.addOverlay?.({
            id: `spots:${this.layerId}`,
            layerId: this.layerId,
            kind: "points",
            draw: (opts) => this.draw(opts),
        }) || null;
        await this.show();
        return this;
    }

    destroy() {
        if (this._styleTimer) window.clearTimeout(this._styleTimer);
        this._styleTimer = null;
        this._overlay?.remove?.();
        this._overlay = null;
        this._hover = null;
        this._hoverPaint = null;
        this._hoverPaints.clear();
    }

    // -- values -----------------------------------------------------------------

    /** Every named field loaded, in one request for the ones that are not. */
    async load(names) {
        const missing = names.filter((name) => !this._values.has(name));
        // In batches: the route answers at most SPOT_VALUE_BATCH names.
        for (let start = 0; start < missing.length; start += SpotLayer.SPOT_VALUE_BATCH) {
            const batch = missing.slice(start, start + SpotLayer.SPOT_VALUE_BATCH);
            let answer = null;
            try {
                answer = await this.api.spotValues(this.layerId, batch);
            } catch (error) {
                answer = null;
            }
            for (const name of batch) {
                const field = answer?.values?.[name];
                this._values.set(name, field ? Float32Array.from(field)
                                             : new Float32Array(this.spotCount()));
                this._windows.set(name, Number(answer?.windows?.[name]) || 1);
            }
        }
        return names.map((name) => this._values.get(name));
    }

    /** Each styled gene's automatic window, in the shape `ceiling` reads. */
    async windows() {
        const genes = this.styleGenes();
        await this.load(genes);
        const out = {};
        for (const gene of genes) out[gene] = { window: this._windows.get(gene) || 1 };
        return out;
    }

    /** Several fields combined spot by spot: `bin_tiles.aggregate`'s rules. */
    static combine(fields, how, count) {
        const out = new Float32Array(count);
        if (!fields.length) return out;
        if (fields.length === 1) {
            out.set(fields[0]);
            return out;
        }
        for (let i = 0; i < count; i += 1) {
            let value;
            switch (how) {
            case "sum":
                value = 0;
                for (const field of fields) value += field[i];
                break;
            case "max":
                value = -Infinity;
                for (const field of fields) value = Math.max(value, field[i]);
                break;
            case "min":
                value = Infinity;
                for (const field of fields) value = Math.min(value, field[i]);
                break;
            default:
                value = 0;
                for (const field of fields) value += field[i];
                value /= fields.length;
            }
            out[i] = value;
        }
        return out;
    }

    /** Counts to 0..1 through `[low, high]` -- `bin_tiles.stretch`. */
    static stretch(value, low, high, log) {
        const span = Math.max(high - low, 1e-6);
        const shifted = Math.max(0, value - low);
        const t = log ? Math.log1p(shifted) / Math.log1p(span) : shifted / span;
        return Math.max(0, Math.min(1, t));
    }

    // -- the picture --------------------------------------------------------------

    /** Rebuild what `draw` paints, then repaint. Hidden: nothing drawn. */
    async show() {
        const token = ++this._showToken;
        this._overlay?.setVisible?.(this.shows());
        if (!this.shows()) {
            this._overlay?.invalidate?.();
            return;
        }
        const paint = this.usesComposition()
            ? await this.compositionPaint() : await this.heatmapPaint();
        if (token !== this._showToken) return;
        this._paint = paint;
        // A new ramp or scale is a different preview too.
        this._hoverPaints.clear();
        if (this._hover) {
            this.showHover();
            return;
        }
        this._overlay?.invalidate?.();
    }

    /**
     * The preview, drawn here rather than served: the values are already in
     * the page. Built once per gene and kept (`HOVER_CACHE`), so the first
     * hover of a gene is one pass over its spots and every later one is a
     * lookup. Leaving the list is instant: the layer's own picture was never
     * thrown away.
     */
    async showHover() {
        const token = ++this._hoverToken;
        if (!this._hover) {
            if (this._hoverPaint) {
                this._hoverPaint = null;
                this._overlay?.invalidate?.();
            }
            return;
        }
        const key = this._hoverKey;
        let paint = this._hoverPaints.get(key);
        if (paint) {
            this._hoverPaints.delete(key);
        } else {
            paint = await this.heatmapPaint(this._hover.genes, this._hover.agg, 0, 1);
            if (token !== this._hoverToken) return;
        }
        this._hoverPaints.set(key, paint);
        while (this._hoverPaints.size > SpotLayer.HOVER_CACHE) {
            this._hoverPaints.delete(this._hoverPaints.keys().next().value);
        }
        this._hoverPaint = paint;
        this._overlay?.invalidate?.();
    }

    /**
     * One colour per spot, off the ramp, as ONE PATH PER COLOUR.
     *
     * Bucketed by a counting sort over typed arrays -- a stop per spot, a
     * count per stop, then every spot written once into its stop's slice of
     * one `Uint32Array` -- so there is no array per colour to grow and no
     * Map to hash into. Each colour's spots are a `subarray` view of it.
     */
    async heatmapPaint(genes = this.styleGenes(), agg = this.state.agg,
                       dlo = this.state.dlo, dhi = this.state.dhi) {
        const fields = await this.load(genes);
        const count = this.spotCount();
        const combined = SpotLayer.combine(fields, agg, count);
        const ceiling = BinLayer.aggregateWindow(
            genes.map((gene) => this._windows.get(gene) || 1), agg);
        const low = (dlo || 0) * ceiling;
        const high = (dhi === undefined ? 1 : dhi) * ceiling;
        const stops = 256;
        const ramp = typeof PlexoraColorRamps !== "undefined"
            ? PlexoraColorRamps.ramp(this.state.ramp || "viridis", null, stops)
            : null;
        const { order, offsets } = SpotLayer.bucket(
            combined, low, high, Boolean(this.state.log), stops);
        const groups = [];
        for (let stop = 0; stop < stops; stop += 1) {
            if (offsets[stop + 1] === offsets[stop]) continue;
            const colour = ramp
                ? `rgb(${ramp[stop * 3]},${ramp[stop * 3 + 1]},${ramp[stop * 3 + 2]})`
                : `rgb(${stop},${stop},${stop})`;
            groups.push({ colour, spots: order.subarray(offsets[stop], offsets[stop + 1]) });
        }
        return { kind: "heatmap", groups };
    }

    /**
     * Spots sorted by ramp stop: `order` holds every spot once, stop by stop,
     * and `offsets[k]..offsets[k + 1]` is stop k's slice. `stretch`'s rule,
     * inlined so the loop is arithmetic on typed arrays and nothing else.
     */
    static bucket(values, low, high, log, stops = 256) {
        const n = values.length;
        const span = Math.max(high - low, 1e-6);
        const denom = log ? Math.log1p(span) : span;
        const top = stops - 1;
        const stopOf = new Uint16Array(n);
        const offsets = new Uint32Array(stops + 1);
        for (let i = 0; i < n; i += 1) {
            let v = values[i] - low;
            if (!(v > 0)) v = 0;
            let t = (log ? Math.log1p(v) : v) / denom;
            if (t > 1) t = 1;
            const stop = Math.round(t * top);
            stopOf[i] = stop;
            offsets[stop + 1] += 1;
        }
        for (let k = 0; k < stops; k += 1) offsets[k + 1] += offsets[k];
        const cursor = offsets.slice(0, stops);
        const order = new Uint32Array(n);
        for (let i = 0; i < n; i += 1) order[cursor[stopOf[i]]++] = i;
        return { order, offsets };
    }

    /**
     * Each spot a pie: parts by their share of the spot's selected signal,
     * a group's slice split among its members by their counts.
     */
    async compositionPaint() {
        const parts = this.composition();
        const genes = parts.flatMap((part) => part.genes);
        const fields = new Map();
        (await this.load(genes)).forEach((field, i) => fields.set(genes[i], field));
        const count = this.spotCount();
        const outer = parts.map((part) => (part.genes.length === 1 && !part.agg
            ? fields.get(part.genes[0])
            : SpotLayer.combine(part.genes.map((g) => fields.get(g)),
                                part.agg || BinLayer.DEFAULT_AGGREGATION, count)));
        // gene -> flat [spot, start, end, spot, start, end, ...] in radians
        // from 12 o'clock: numbers in one array per gene rather than an
        // array per wedge, and a Float64Array once built.
        const flat = genes.map(() => []);
        const members = parts.map((part) => part.genes.map((gene) => fields.get(gene)));
        const firstOf = [];
        let at = 0;
        for (const part of parts) {
            firstOf.push(at);
            at += part.genes.length;
        }
        const top = -Math.PI / 2;
        const turn = Math.PI * 2;
        for (let i = 0; i < count; i += 1) {
            let total = 0;
            for (let k = 0; k < outer.length; k += 1) {
                const v = outer[k][i];
                if (v > 0) total += v;
            }
            if (!(total > 0)) continue;
            let angle = top;
            for (let k = 0; k < parts.length; k += 1) {
                const share = outer[k][i] / total;
                if (!(share > 0)) continue;
                const span = share * turn;
                const own = members[k];
                let sum = 0;
                for (let m = 0; m < own.length; m += 1) if (own[m][i] > 0) sum += own[m][i];
                let inner = angle;
                for (let m = 0; m < own.length; m += 1) {
                    const piece = sum > 0 ? (own[m][i] > 0 ? own[m][i] / sum : 0)
                        : 1 / own.length;
                    if (piece > 0) {
                        const end = inner + span * piece;
                        flat[firstOf[k] + m].push(i, inner, end);
                        inner = end;
                    }
                }
                angle += span;
            }
        }
        return {
            kind: "composite",
            groups: genes.map((gene, g) => ({
                colour: this.colorFor(gene), wedges: Float64Array.from(flat[g]),
            })),
        };
    }

    /**
     * The overlay's frame, in the layer's own (full-res) pixels.
     *
     * NOT A PATH PER SPOT PER FRAME. Four thousand pies are some ten thousand
     * arcs, and rebuilding them on every frame of a pan made the whole viewer
     * crawl. So the picture is painted ONCE into a bitmap (`sprite`) at a
     * few pixels a spot and that bitmap is what a frame draws -- one
     * `drawImage`, whatever the zoom. Only once the spots are bigger on
     * screen than the bitmap holds them are they drawn as vectors, and then
     * only the ones in view, which zoomed in is a few hundred.
     */
    draw({ context }) {
        const paint = this._hoverPaint || this._paint;
        if (!paint || !context || !this.shows()) return;
        const radius = Number(this.manifest?.radius) || 1;
        const m = context.getTransform ? context.getTransform() : null;
        const onScreen = m ? radius * Math.hypot(m.a, m.b) : 0;
        context.globalAlpha = this.state.opacity;
        const sprite = this.sprite(paint, radius);
        if (sprite && onScreen <= SpotLayer.SPRITE_RADIUS * 1.5) {
            context.imageSmoothingEnabled = true;
            context.drawImage(sprite.canvas, sprite.x, sprite.y, sprite.width, sprite.height);
            return;
        }
        const view = m ? SpotLayer.visibleBounds(context, m, radius) : null;
        this.paintSpots(context, paint, radius, view);
    }

    //: A spot's radius in the bitmap, in bitmap pixels.
    static get SPRITE_RADIUS() { return 8; }

    //: The bitmap's longest side, whatever the slide.
    static get SPRITE_MAX() { return 4096; }

    /** The layer-space rectangle the canvas shows, grown by a spot. */
    static visibleBounds(context, m, pad) {
        let inverse;
        try {
            inverse = m.inverse();
        } catch (error) {
            return null;
        }
        const w = context.canvas?.width || 0;
        const h = context.canvas?.height || 0;
        const xs = [];
        const ys = [];
        for (const [cx, cy] of [[0, 0], [w, 0], [0, h], [w, h]]) {
            xs.push(inverse.a * cx + inverse.c * cy + inverse.e);
            ys.push(inverse.b * cx + inverse.d * cy + inverse.f);
        }
        return {
            x0: Math.min(...xs) - pad, x1: Math.max(...xs) + pad,
            y0: Math.min(...ys) - pad, y1: Math.max(...ys) + pad,
        };
    }

    /** The painted picture as a bitmap, built once per paint and kept ON it,
     *  so a cached hover picture keeps its bitmap and an evicted one takes
     *  its bitmap with it. */
    sprite(paint, radius) {
        if (paint.sprite) return paint.sprite;
        const count = this.spotCount();
        if (!count || typeof document === "undefined") return null;
        // The spots' extent never changes: measured once.
        if (!this._bounds) {
            let bx0 = Infinity; let by0 = Infinity; let bx1 = -Infinity; let by1 = -Infinity;
            for (let i = 0; i < count; i += 1) {
                const px = this._x[i];
                const py = this._y[i];
                if (px < bx0) bx0 = px;
                if (px > bx1) bx1 = px;
                if (py < by0) by0 = py;
                if (py > by1) by1 = py;
            }
            this._bounds = [bx0, by0, bx1, by1];
        }
        const x0 = this._bounds[0] - radius;
        const y0 = this._bounds[1] - radius;
        const x1 = this._bounds[2] + radius;
        const y1 = this._bounds[3] + radius;
        const span = Math.max(x1 - x0, y1 - y0, 1);
        const scale = Math.min(SpotLayer.SPRITE_RADIUS / radius, SpotLayer.SPRITE_MAX / span);
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.ceil((x1 - x0) * scale));
        canvas.height = Math.max(1, Math.ceil((y1 - y0) * scale));
        const context = canvas.getContext("2d");
        if (!context) return null;
        context.setTransform(scale, 0, 0, scale, -x0 * scale, -y0 * scale);
        this.paintSpots(context, paint, radius, null);
        paint.sprite = {
            canvas, x: x0, y: y0, width: canvas.width / scale, height: canvas.height / scale,
        };
        return paint.sprite;
    }

    /** Every spot (or every one inside `view`), one path per colour. */
    paintSpots(context, paint, radius, view) {
        const x = this._x;
        const y = this._y;
        const inside = view
            ? (i) => x[i] >= view.x0 && x[i] <= view.x1 && y[i] >= view.y0 && y[i] <= view.y1
            : () => true;
        for (const group of paint.groups) {
            context.fillStyle = group.colour;
            context.beginPath();
            if (paint.kind === "heatmap") {
                for (const i of group.spots) {
                    if (!inside(i)) continue;
                    context.moveTo(x[i] + radius, y[i]);
                    context.arc(x[i], y[i], radius, 0, Math.PI * 2);
                }
            } else {
                const w = group.wedges;
                for (let j = 0; j < w.length; j += 3) {
                    const i = w[j];
                    if (!inside(i)) continue;
                    context.moveTo(x[i], y[i]);
                    context.arc(x[i], y[i], radius, w[j + 1], w[j + 2]);
                    context.closePath();
                }
            }
            context.fill();
        }
    }

    // -- what BinLayer does with tiles, done with the overlay ---------------------

    restyle() {
        if (this._styleTimer) window.clearTimeout(this._styleTimer);
        this._styleTimer = window.setTimeout(() => {
            this._styleTimer = null;
            this.show();
        }, BinLayer.STYLE_DEBOUNCE_MS);
    }

    flushStyle() { this.show(); }

    setOpacity(value) {
        const next = BinLayer.clamp01(value, this.state.opacity);
        if (next === this.state.opacity) return;
        this.state.opacity = next;
        this._overlay?.invalidate?.();
    }

    /** The overlay reads the layer's transform every frame: nothing to redo. */
    syncTransform() { this._overlay?.invalidate?.(); }
}

if (typeof window !== "undefined") window.SpotLayer = SpotLayer;
if (typeof globalThis !== "undefined") globalThis.SpotLayer = SpotLayer;
