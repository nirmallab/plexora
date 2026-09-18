/**
 * Transcripts on screen: density when there are too many to see, points when
 * there are not.
 *
 * The renderer is core's -- this asks `ctx.layers.addOverlay` to draw and core
 * owns the canvas, the transform, the anchor guard and the frame coalescing.
 * What is here is the part that interprets: which genes, what colour, and which
 * of the two representations the current view calls for.
 *
 * THE LOD IS A RATIO, NEVER A ZOOM THRESHOLD. Transcript density varies by two
 * orders of magnitude between a tonsil and a skin biopsy, so a fixed "switch to
 * points below 4x" is right for one sample and wrong for the next. What matters
 * is how many points would land on one screen pixel, and the manifest's per-tile
 * counts answer that without fetching anything:
 *
 *     estimated = points in view / screen pixels
 *       > 0.25   density only    -- the dots would be a solid slab
 *       0.02-0.25  both          -- density under, points over
 *       < 0.02   points only     -- individually visible, individually clickable
 *
 * with 1.5x hysteresis on each boundary, because a view sitting exactly on one
 * would otherwise flip representation on every pointer twitch.
 *
 * DENSITY IS NOT DRAWN HERE AT ALL. It is a uint16 raster served through core's
 * layer tile route, added as an ordinary TiledImage with `lighter` and a colour,
 * and drawn by the existing WebGL colorize shader. That is the whole reason the
 * low-zoom view costs no new rendering code -- and why it survives into Figure
 * Builder's server-side export, which re-renders channels and cannot reproduce
 * an overlay.
 *
 * POINTS ARE CANVAS2D, deliberately, and this is a departure from the original
 * design worth stating. A dedicated WebGL context with a VBO per tile would draw
 * more of them; it would also be a second GL context over OpenSeadragon's
 * canvas, and at the counts that actually survive the LOD above -- tens of
 * thousands in a viewport at the zoom where individual molecules are worth
 * seeing -- canvas2d is not the bottleneck. `drawPoints` is the only function
 * that would change.
 */
class TranscriptLayer {

    constructor(ctx, layerId) {
        this.ctx = ctx;
        this.layerId = layerId;
        this.manifest = null;
        this.handle = null;
        this.points = null;          // the records currently loaded
        this.selected = [];          // gene NAMES, in the order the user picked
        this.colors = new Map();     // gene name -> "#rrggbb"
        this.mode = "auto";          // "auto" | "points" | "density"
        this.pointRadius = 1.4;
        this.opacity = 0.9;
        this._request = 0;
        this._lastLod = null;
        this._offViewport = null;
        //: The density raster's world item, when the LOD says to draw one.
        //: A TILED layer rather than a canvas overlay, and that is the whole
        //: reason low zoom is cheap: the server bins the transcripts into a
        //: uint16 raster and the browser draws an image, instead of this
        //: plugin fetching two million points to plot them a pixel apart.
        this._density = null;
    }

    //: The density raster's colour. One colour for the whole layer, not one
    //: per gene: density is "how much is here", and a sum cannot be several
    //: colours at once. The per-gene colours are the points' business, which
    //: is what the user is looking at by the time they can tell genes apart.
    static get DENSITY_COLOR() { return "#4da3ff"; }

    //: Points per screen pixel. Above the first, dots merge into a slab and the
    //: honest picture is density; below the second, every dot is its own thing.
    static get DENSITY_ABOVE() { return 0.25; }
    static get POINTS_BELOW() { return 0.02; }
    //: How far past a boundary the view has to go before the representation
    //: changes back. Without it a view parked on a threshold flips on every
    //: pointer twitch, which reads as flicker rather than as a decision.
    static get HYSTERESIS() { return 1.5; }
    //: Hard ceiling on what is drawn as points in one frame, whatever the LOD
    //: concluded. A cap that is never reached in normal use, and is the
    //: difference between a slow frame and a hung tab when it is.
    static get MAX_POINTS_ON_SCREEN() { return 300_000; }
    //: Distinct colours a person can tell apart on a slide. Past this the
    //: selector stops assigning new ones and says so -- twenty similar reds is
    //: a legend nobody can read.
    static get MAX_DISTINCT_GENES() { return 12; }
    static get OTHER_COLOR() { return "#9aa0aa"; }

    async attach() {
        this.manifest = await this.loadManifest();
        if (!this.manifest || this.manifest.status !== "ready") return null;
        this.handle = this.ctx.layers?.addOverlay?.({
            id: `points:${this.layerId}`,
            kind: "points",
            layerId: this.layerId,
            draw: (opts) => this.draw(opts),
        }) || null;
        this._offViewport = this.ctx.layers?.onViewportChange?.(() => this.refresh());
        this.refresh();
        return this.handle;
    }

    destroy() {
        this._offViewport?.();
        this._offViewport = null;
        this.handle?.remove();
        this.handle = null;
        this._density?.remove();
        this._density = null;
        this.points = null;
    }

    /**
     * Show or hide the density raster.
     *
     * Core draws no modality: what makes this transcripts is the manifest and
     * the tile route behind it, and `ctx.layers.addTiled` is core handing out
     * the same primitive it uses for a registered image. Hiding REMOVES the
     * world item rather than fading it, so a view zoomed in far enough to be
     * drawing points is not also fetching density tiles for the same region.
     */
    setDensity(on) {
        if (!on) {
            this._density?.setVisible(false);
            return;
        }
        if (this._density) {
            this._density.setVisible(true);
            return;
        }
        const manifest = this.manifest || {};
        this._density = this.ctx.layers?.addTiled?.({
            id: `density:${this.layerId}`,
            layerId: this.layerId,
            // `/generated/layer/<sample>/<layer>/<channel>/` -- core's layer
            // tile route, which serves this layer's density because the layer
            // is `points` and has a manifest. The channel segment is a label
            // rather than an index: a density raster has one plane.
            src: this.ctx.url(
                `generated/layer/${encodeURIComponent(this.ctx.datasource)}`
                + `/${encodeURIComponent(this.layerId)}/density/`),
            style: `color=${TranscriptLayer.DENSITY_COLOR.replace("#", "")}`,
            // `lighter`, as a fluorescence channel is: density adds to what is
            // under it, and on a dark morphology image that is the picture.
            compositeOperation: "lighter",
            geometry: {
                width: manifest.width,
                height: manifest.height,
                // Levels enough to zoom out to the whole sample. The tile
                // cache is built at level 0 and coarser levels are summed on
                // demand (see transcript_tiles.density_tile), so this is how
                // far out the viewer may ask rather than what is stored.
                maxLevel: TranscriptLayer.densityLevels(manifest),
                tileWidth: manifest.tile_size,
                tileHeight: manifest.tile_size,
                // Already in reference pixels: the reader converted microns on
                // the way in, which is the one place that knows both the
                // file's units and the image's calibration.
                transform: null,
            },
        }) || null;
    }

    /** How many halvings it takes to get this layer down to one tile. */
    static densityLevels(manifest) {
        const tile = Math.max(1, manifest.tile_size || 1024);
        let longest = Math.max(manifest.width || tile, manifest.height || tile);
        let levels = 1;
        while (longest > tile) {
            longest = Math.ceil(longest / 2);
            levels += 1;
        }
        return levels;
    }

    async loadManifest() {
        const url = this.ctx.url(
            `plugins/transcripts/manifest?datasource=${encodeURIComponent(this.ctx.datasource)}`
            + `&layer=${encodeURIComponent(this.layerId)}`);
        try {
            const response = await fetch(url);
            return response.ok ? await response.json() : null;
        } catch (error) {
            console.error("transcripts: the manifest could not be read", error);
            return null;
        }
    }

    /** Every gene in this panel, for the selector. */
    genes() {
        return this.manifest?.genes || [];
    }

    /**
     * Pick genes to draw.
     *
     * Past MAX_DISTINCT_GENES the extras keep drawing but share one grey: a
     * selector that refused the thirteenth would be arguing with the user, and
     * one that invented a thirteenth distinguishable colour would be lying about
     * being able to tell them apart.
     */
    setGenes(names) {
        this.selected = [...new Set(names || [])];
        this.refresh();
    }

    setColor(gene, color) {
        this.colors.set(gene, color);
        this.handle?.invalidate();
    }

    colorFor(gene) {
        const rank = this.selected.indexOf(gene);
        if (rank >= TranscriptLayer.MAX_DISTINCT_GENES) return TranscriptLayer.OTHER_COLOR;
        return this.colors.get(gene) || TranscriptLayer.OTHER_COLOR;
    }

    setMode(mode) {
        this.mode = mode || "auto";
        this.refresh();
    }

    /**
     * How many points the current view holds, from the manifest alone.
     *
     * No fetch: the per-tile counts are in the manifest precisely so this
     * question can be answered before deciding whether to fetch. Asking the
     * server would mean requesting the thing we are deciding whether to request.
     */
    estimateInView(bounds) {
        const m = this.manifest;
        if (!m || !bounds) return 0;
        const size = m.tile_size || 512;
        const counts = m.tile_counts || [];
        const x0 = Math.max(0, Math.floor(bounds.minX / size));
        const y0 = Math.max(0, Math.floor(bounds.minY / size));
        const x1 = Math.min((m.columns || 1) - 1, Math.floor(bounds.maxX / size));
        const y1 = Math.min((m.rows || 1) - 1, Math.floor(bounds.maxY / size));
        let total = 0;
        for (let y = y0; y <= y1; y += 1) {
            const row = counts[y] || [];
            for (let x = x0; x <= x1; x += 1) total += row[x] || 0;
        }
        // Scaled by the fraction of the panel selected, because the counts are
        // for every gene. Crude, and it only has to be right to within a factor
        // of two to pick the same representation.
        const panel = (this.manifest.genes || []).length || 1;
        const chosen = this.selected.length || panel;
        return total * (chosen / panel);
    }

    /**
     * Which representation this view calls for.
     *
     * @returns { points, density } -- both may be true at once, which is the
     *   middle band: density underneath for the shape of it, points over the
     *   top for the ones that are individually visible.
     */
    lodFor(bounds, screenPixels) {
        if (this.mode === "points") return { points: true, density: false };
        if (this.mode === "density") return { points: false, density: true };

        const ratio = this.estimateInView(bounds) / Math.max(1, screenPixels);
        const last = this._lastLod;
        const h = TranscriptLayer.HYSTERESIS;

        // The boundaries move depending on which side the view was last on, so
        // a view parked on one does not flip on every pointer twitch.
        const densityAbove = last?.density && !last?.points
            ? TranscriptLayer.DENSITY_ABOVE / h
            : TranscriptLayer.DENSITY_ABOVE;
        const pointsBelow = last?.points && !last?.density
            ? TranscriptLayer.POINTS_BELOW * h
            : TranscriptLayer.POINTS_BELOW;

        let next;
        if (ratio > densityAbove) next = { points: false, density: true };
        else if (ratio < pointsBelow) next = { points: true, density: false };
        else next = { points: true, density: true };
        this._lastLod = next;
        return next;
    }

    /** Fetch the points the current view needs, if it needs any. */
    async refresh() {
        if (!this.manifest || this.manifest.status !== "ready") return;
        const bounds = this.ctx.layers?.viewport?.();
        if (!bounds) return;

        const canvas = document.getElementById("openseadragon");
        const screenPixels = Math.max(
            1, (canvas?.clientWidth || 1) * (canvas?.clientHeight || 1));
        const lod = this.lodFor(bounds, screenPixels);
        this.setDensity(lod.density);

        if (!lod.points || !this.selected.length) {
            this.points = null;
            this.handle?.invalidate();
            return;
        }

        // Every request carries a sequence number and only the latest one is
        // allowed to land: a pan issues several, and an earlier one arriving
        // after a later one would draw where the user used to be.
        const request = ++this._request;
        const params = new URLSearchParams({
            datasource: this.ctx.datasource,
            layer: this.layerId,
            genes: this.selected.join(","),
            minX: String(Math.floor(bounds.minX)),
            minY: String(Math.floor(bounds.minY)),
            maxX: String(Math.ceil(bounds.maxX)),
            maxY: String(Math.ceil(bounds.maxY)),
            max: String(TranscriptLayer.MAX_POINTS_ON_SCREEN),
        });
        try {
            const response = await fetch(
                this.ctx.url(`plugins/transcripts/points?${params}`));
            if (!response.ok || request !== this._request) return;
            const buffer = await response.arrayBuffer();
            if (request !== this._request) return;
            this.points = TranscriptLayer.decode(buffer);
            this.truncated = response.headers.get("X-Transcript-Truncated") === "1";
            this.handle?.invalidate();
        } catch (error) {
            console.error("transcripts: points could not be fetched", error);
        }
    }

    /**
     * The wire format, unpacked.
     *
     * 10 bytes per record: u2 gene, f4 x, f4 y. Packed, so the offsets are
     * 0/2/6 rather than 0/4/8 -- reading it as though it were aligned puts every
     * transcript in the wrong place and the picture still looks like transcripts.
     */
    static decode(buffer) {
        const view = new DataView(buffer);
        const stride = 10;
        const count = Math.floor(view.byteLength / stride);
        const genes = new Uint16Array(count);
        const xs = new Float32Array(count);
        const ys = new Float32Array(count);
        for (let i = 0; i < count; i += 1) {
            const at = i * stride;
            genes[i] = view.getUint16(at, true);
            xs[i] = view.getFloat32(at + 2, true);
            ys[i] = view.getFloat32(at + 6, true);
        }
        return { genes, xs, ys, count };
    }

    draw(opts) {
        if (!this.points?.count) return;
        const context = opts.context;
        // Radius in SCREEN pixels: opts.px is image pixels per screen pixel, so
        // multiplying is what stops a dot being a 20px blob at 10x zoom and
        // invisible at 0.1x.
        const radius = Math.max(0.35, this.pointRadius * opts.px);
        this.drawPoints(context, radius);
    }

    /**
     * The only function a WebGL renderer would replace.
     *
     * Batched by gene rather than one path per point: a fill per point is a
     * state change per point, and grouping them turns tens of thousands of
     * setter calls into one per gene.
     */
    drawPoints(context, radius) {
        const { genes, xs, ys, count } = this.points;
        const names = this.manifest.genes || [];
        const previousAlpha = context.globalAlpha;
        context.globalAlpha = this.opacity;

        const byGene = new Map();
        const limit = Math.min(count, TranscriptLayer.MAX_POINTS_ON_SCREEN);
        for (let i = 0; i < limit; i += 1) {
            const gene = genes[i];
            let bucket = byGene.get(gene);
            if (!bucket) {
                bucket = [];
                byGene.set(gene, bucket);
            }
            bucket.push(i);
        }

        for (const [gene, indices] of byGene) {
            context.fillStyle = this.colorFor(names[gene] || String(gene));
            context.beginPath();
            for (const i of indices) {
                context.moveTo(xs[i] + radius, ys[i]);
                context.arc(xs[i], ys[i], radius, 0, Math.PI * 2);
            }
            context.fill();
        }
        context.globalAlpha = previousAlpha;
    }

    /**
     * The transcript nearest a point, or null.
     *
     * Linear over what is loaded, which is bounded by MAX_POINTS_ON_SCREEN and
     * only runs on a click. A spatial index here would be a structure rebuilt on
     * every pan to answer a question asked once a minute.
     */
    nearest(x, y, withinPixels = 6) {
        if (!this.points?.count) return null;
        const { genes, xs, ys, count } = this.points;
        const names = this.manifest.genes || [];
        let best = null;
        let bestDistance = withinPixels * withinPixels;
        for (let i = 0; i < count; i += 1) {
            const dx = xs[i] - x;
            const dy = ys[i] - y;
            const distance = dx * dx + dy * dy;
            if (distance < bestDistance) {
                bestDistance = distance;
                best = { gene: names[genes[i]] || String(genes[i]), x: xs[i], y: ys[i] };
            }
        }
        return best;
    }
}

if (typeof window !== "undefined") window.TranscriptLayer = TranscriptLayer;
if (typeof globalThis !== "undefined") globalThis.TranscriptLayer = TranscriptLayer;
