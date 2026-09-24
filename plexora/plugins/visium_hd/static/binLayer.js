/**
 * The Visium HD bin layer: which genes are drawn, how, and the one tiled
 * world item that draws them.
 *
 * NOTHING IS RENDERED HERE. A counted grid is core's primitive: the store,
 * the pooling and the colouring are `server/models/bin_tiles.py`, served
 * through core's one layer tile route, and the browser composites the RGBA
 * tiles it gets back. What this file owns is the STYLE -- a query string --
 * and the geometry that tells OpenSeadragon where those tiles go. Both are
 * pure functions of the state and the manifest, which is what lets
 * tests/js/visium_hd_layer_probe.mjs check them without a viewer.
 *
 * THE FRAME IS GRID x SUPERSAMPLE. One grid unit is one finest bin (2
 * microns on Space Ranger's grid), and the store draws each of them as
 * `supersample` pixels a side so an 8 micron square has a gutter to show.
 * The layer's own transform maps GRID units to reference pixels, so the
 * tiled item's transform is that one with its linear part divided by the
 * supersample: tile pixels -> grid -> reference. The translation is already
 * in reference pixels and is left alone. On the real slide that transform is
 * a half turn and a mirror, which core turns into OSD's controls
 * (`PlexoraLayerStack.placementFor`); this passes it through untouched.
 *
 * SOURCE-OVER, ALWAYS. Unlike the transcript density raster there is no mode
 * in which this ADDS: a bin tile is transparent wherever a square is empty or
 * off the tissue, so the H&E shows through the holes and the colour replaces
 * it where there is signal. `lighter` would wash every square towards white
 * on a pale brightfield ground.
 */
class BinLayer {

    constructor(ctx, layerId, api = null) {
        this.ctx = ctx;
        this.layerId = layerId;
        this.api = api || new VisiumHdApi(ctx);
        this.manifest = null;
        this.visible = true;
        //: The tiled world item, once added. Core owns it; this holds the
        //: handle (`setStyle`, `setVisible`, `setOpacity`, `remove`).
        this._tiles = null;
        this._style = "";
        this._styleTimer = null;
        //: The transform the item was added with, so a re-registration on the
        //: Layers card can be told apart from an opacity drag.
        this._transformKey = "";
        //: name -> index, and name -> count. 18,085 genes; an `indexOf` per
        //: row per repaint is the difference between a list and a stall.
        this._index = new Map();
        //: `${genes}|${pooling}` -> the /stats answer.
        this._stats = new Map();
        this.state = BinLayer.defaultState();
    }

    /** The panel's state, as it is saved and restored. */
    static defaultState() {
        return {
            //: Gene NAMES, in the order the user added them -- the legend's
            //: order and the colour-assignment order.
            selected: [],
            colors: {},
            //: Selected but switched off. Not removal: a hidden gene keeps its
            //: colour and its place.
            hidden: [],
            //: "heatmap" -- one field, the selected genes summed, read off a
            //: ramp; "composite" -- each gene in its own colour. Heatmap
            //: first, because with nothing picked the honest picture is the
            //: UMI density, and that is one field.
            mode: "heatmap",
            ramp: "viridis",
            //: The square's side in MICRONS, snapped to the store's ladder
            //: (`binLadder`). 8 is Space Ranger's own default binning and
            //: roughly a cell; 2 is the raw grid and mostly empty squares.
            binUm: 8,
            //: log1p before the window. On: 2 micron counts are 0, 1, 2 and
            //: the occasional 40, and linear spends the ramp on the forty.
            log: true,
            //: The window, as FRACTIONS of the automatic one -- which follows
            //: the pooling on screen, so "the bottom tenth" keeps meaning the
            //: bottom tenth when the squares merge on zoom-out.
            dlo: 0,
            dhi: 1,
            //: One number for the layer and the same one the Layers card
            //: holds. Not 1: a heatmap covers every square with signal, which
            //: on an H&E is all of the tissue, and the morphology is what the
            //: colour is being read against.
            opacity: 0.8,
        };
    }

    //: Assignment-order colours: distinguishable on an H&E's pale ground and
    //: on black. Same list the transcripts panel starts from, copied rather
    //: than read -- that plugin's globals exist only when its section mounts.
    static get PALETTE() {
        return ["#ff4d4d", "#4dd2ff", "#7cff4d", "#ffd24d", "#c77dff", "#ff8c42",
                "#4dffd2", "#ff6ec7", "#9be564", "#6d9eff", "#ffe066", "#e0e0e0"];
    }

    /** The server's own ramps, by name. Core's copy (gradientRange.js), which
     *  mirrors `server/utils/colormaps.py` to the digit -- never a third. */
    static get RAMPS() {
        return (typeof PlexoraColorRamps !== "undefined" && PlexoraColorRamps.RAMPS)
            || { viridis: [] };
    }

    //: The stand-in `ramp` value for the per-gene composite in the gradient
    //: control's palette list; it never reaches a url.
    static get GENE_COLOURS() { return "genes"; }

    //: Poolings the panel offers, in grid squares: 2, 8 and 16 microns on a
    //: 2 micron grid. Powers of two only, so a square is always a whole block
    //: of Space Ranger's own nesting.
    static get POOLINGS() { return [1, 4, 8]; }

    //: What `genes=` names when nothing is drawn: every gene summed.
    static get TOTAL() { return "total"; }

    static get STYLE_DEBOUNCE_MS() { return 200; }

    // -- lifecycle ---------------------------------------------------------

    async attach() {
        try {
            this.manifest = await this.api.manifest(this.layerId);
        } catch (error) {
            console.error("visium_hd: the manifest could not be read", error);
            this.manifest = null;
        }
        if (!this.manifest || this.manifest.status !== "ready") return null;
        this.indexGenes();
        this.normalizeState();

        // THE LAYERS CARD IS WHERE THIS LAYER IS SWITCHED. Claiming it is
        // what gives it the eye, the drag and the X; they come back through
        // `onLayerChange` like any image layer's.
        this.ctx.layers?.claim?.(this.layerId);
        const record = this.ctx.layers?.get?.(this.layerId);
        if (record) {
            this.visible = record.visible !== false;
            // The panel's opacity wins and is pushed onto the card: core does
            // not persist the opacity of a layer somebody else draws, so the
            // stack's 1 is a placeholder and this state is the saved choice.
            this.ctx.layers?.setOpacity?.(this.layerId, this.state.opacity);
        }
        this.show();
        return this;
    }

    destroy() {
        if (this._styleTimer) window.clearTimeout(this._styleTimer);
        this._styleTimer = null;
        this._tiles?.remove?.();
        this._tiles = null;
    }

    indexGenes() {
        this._index = new Map();
        this.genes().forEach((name, index) => this._index.set(name, index));
    }

    /**
     * Saved state brought into line with THIS store.
     *
     * A bin size from a store on a different grid snaps to the nearest rung,
     * a gene this panel has not got is dropped, and a ramp the server cannot
     * draw becomes the default -- each of which would otherwise be a url the
     * server answers with a picture nobody asked for.
     */
    normalizeState() {
        const known = (gene) => this._index.has(gene);
        const s = this.state;
        s.selected = [...new Set((s.selected || []).filter(known))];
        s.hidden = (s.hidden || []).filter((gene) => s.selected.includes(gene));
        s.colors = { ...(s.colors || {}) };
        s.selected.forEach((gene, index) => {
            if (!s.colors[gene]) {
                s.colors[gene] = BinLayer.PALETTE[index % BinLayer.PALETTE.length];
            }
        });
        if (s.mode !== "composite") s.mode = "heatmap";
        if (!BinLayer.RAMPS[s.ramp]) s.ramp = "viridis";
        s.binUm = this.snapMicrons(s.binUm);
        s.log = s.log !== false;
        s.dlo = BinLayer.clamp01(s.dlo, 0);
        s.dhi = BinLayer.clamp01(s.dhi, 1);
        if (s.dhi <= s.dlo) { s.dlo = 0; s.dhi = 1; }
        s.opacity = BinLayer.clamp01(s.opacity, 0.8);
    }

    static clamp01(value, fallback) {
        const n = Number(value);
        return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : fallback;
    }

    // -- the vocabulary ------------------------------------------------------

    genes() { return this.manifest?.genes || []; }

    countOf(gene) {
        const index = this._index.get(gene);
        return index === undefined ? 0 : (this.manifest?.gene_counts?.[index] || 0);
    }

    has(gene) { return this._index.has(gene); }

    /** Selected and not switched off, in the user's order. */
    drawnGenes() {
        const hidden = new Set(this.state.hidden);
        return this.state.selected.filter((gene) => !hidden.has(gene));
    }

    isHidden(gene) { return this.state.hidden.includes(gene); }

    /** Genes are picked and every one of them is off: draw NOTHING, rather
     *  than letting an empty `genes=` fall through to the whole panel. */
    allGenesHidden() {
        return this.state.selected.length > 0 && this.drawnGenes().length === 0;
    }

    colorFor(gene) { return this.state.colors[gene] || "#e0e0e0"; }

    addGene(name) {
        if (!name || !this.has(name) || this.state.selected.includes(name)) return false;
        this.state.selected = [...this.state.selected, name];
        if (!this.state.colors[name]) {
            const palette = BinLayer.PALETTE;
            this.state.colors[name] = palette[(this.state.selected.length - 1) % palette.length];
        }
        this.restyle();
        return true;
    }

    removeGene(name) {
        this.state.selected = this.state.selected.filter((gene) => gene !== name);
        this.state.hidden = this.state.hidden.filter((gene) => gene !== name);
        this.restyle();
    }

    clearGenes() {
        this.state.selected = [];
        this.state.hidden = [];
        this.restyle();
    }

    setGeneHidden(name, hidden) {
        const set = new Set(this.state.hidden);
        if (hidden) set.add(name); else set.delete(name);
        this.state.hidden = [...set];
        this.restyle();
    }

    setColor(gene, color) {
        this.state.colors[gene] = color;
        this.restyle();
    }

    /** Several state keys at once, then one restyle. */
    set(values = {}) {
        Object.assign(this.state, values);
        if (values.binUm !== undefined) this.state.binUm = this.snapMicrons(values.binUm);
        this.restyle();
    }

    // -- the bin ladder --------------------------------------------------------

    /** The finest square, in microns, as the store recorded it. */
    gridMicrons() {
        const value = Number(this.manifest?.bin_um);
        return Number.isFinite(value) && value > 0 ? value : 2;
    }

    /** `[{pooling, microns}]` the panel offers: 2 / 8 / 16 on a 2 micron grid. */
    binLadder() {
        const base = this.gridMicrons();
        return BinLayer.POOLINGS.map((pooling) => ({
            pooling, microns: Math.round(base * pooling * 100) / 100,
        }));
    }

    /** The rung nearest a micron value, as microns. */
    snapMicrons(microns) {
        const ladder = this.binLadder();
        const asked = Number(microns);
        if (!Number.isFinite(asked) || asked <= 0) {
            return (ladder.find((rung) => rung.pooling === 4) || ladder[0]).microns;
        }
        let best = ladder[0];
        for (const rung of ladder) {
            if (Math.abs(rung.microns - asked) < Math.abs(best.microns - asked)) best = rung;
        }
        return best.microns;
    }

    /**
     * The `bin=` the url carries: the pooling in GRID SQUARES.
     *
     * The chosen microns over the grid's own, rounded down to a power of two
     * -- 8 microns on a 2 micron grid is 4. The server floors it again per
     * level (`bin_tiles.effective_pooling`), because at a whole-slide zoom a
     * tile pixel is coarser than a 2 micron square and a finer one has
     * nowhere to be drawn.
     */
    pooling() {
        const ratio = Number(this.state.binUm) / this.gridMicrons();
        if (!Number.isFinite(ratio) || ratio < 1) return 1;
        return 2 ** Math.floor(Math.log2(ratio) + 1e-9);
    }

    // -- the style ---------------------------------------------------------------

    /**
     * One field off a ramp, or a colour per gene.
     *
     * The heatmap when asked for, and ALSO whenever nothing is drawn: with no
     * gene picked the picture is `total`, the UMI density, which is one
     * number per square and has no colour of its own to be drawn in.
     */
    usesRamp() {
        return this.state.mode === "heatmap" || this.drawnGenes().length === 0;
    }

    /** The genes the url names: the drawn ones, or `total`. */
    styleGenes() {
        const drawn = this.drawnGenes();
        return drawn.length ? drawn : [BinLayer.TOTAL];
    }

    /**
     * The query string core's layer tile route reads.
     *
     * `color=ffffff` FIRST AND ALWAYS: `layer_sources.parse_style` returns
     * None without a colour, and None means "serve this as a grey channel
     * plane" -- a bin layer would come back as nothing it can draw. The
     * value itself is only the composite's fallback colour.
     *
     * `v=` makes a rebuilt store or a new plugin version a different url. The
     * tiles are served `max-age` a year and the browser never asks again, so
     * without it a rebuild would be invisible until the cache was cleared.
     * The server ignores the key.
     */
    tileStyle() {
        const genes = this.styleGenes();
        const parts = ["color=ffffff"];
        parts.push(`genes=${genes.map(encodeURIComponent).join(",")}`);
        if (this.usesRamp()) {
            parts.push(`ramp=${encodeURIComponent(this.state.ramp || "viridis")}`);
        } else {
            parts.push(`colors=${genes.map(
                (gene) => this.colorFor(gene).replace("#", "")).join(",")}`);
        }
        parts.push(`bin=${this.pooling()}`);
        parts.push(`dlo=${Number(this.state.dlo || 0).toFixed(4)}`);
        parts.push(`dhi=${Number(this.state.dhi === undefined ? 1 : this.state.dhi).toFixed(4)}`);
        if (this.state.log) parts.push("log=1");
        const version = [this.manifest?.version, this.manifest?.revision]
            .filter((part) => part !== undefined && part !== null && part !== "")
            .join("-");
        if (version) parts.push(`v=${encodeURIComponent(version)}`);
        return parts.join("&");
    }

    /** Where the tiles are: core's route, `<level>/<x>_<y>.png` appended by
     *  the tile source. `bins` is the channel segment, which a bin layer's
     *  tile ignores and the route requires. */
    tileSource() {
        return this.ctx.url(
            `generated/layer/${encodeURIComponent(this.ctx.datasource)}`
            + `/${encodeURIComponent(this.layerId)}/bins/`);
    }

    /** The layer's own GRID -> reference affine, from the stack or /config. */
    layerTransform() {
        const record = this.ctx.layers?.get?.(this.layerId);
        const own = record?.transform
            || (this.ctx.config?.layers || []).find(
                (layer) => layer.id === this.layerId)?.transform;
        return Array.isArray(own) && own.length === 6 ? own.map(Number) : [1, 0, 0, 1, 0, 0];
    }

    /**
     * TILE pixels -> reference pixels: the grid transform with its linear
     * part divided by the supersample.
     */
    static tileTransform(transform, supersample) {
        const [a, b, c, d, e, f] = Array.isArray(transform) && transform.length === 6
            ? transform.map(Number) : [1, 0, 0, 1, 0, 0];
        const s = Math.max(1, Number(supersample) || 1);
        return [a / s, b / s, c / s, d / s, e, f];
    }

    /**
     * What `ctx.layers.addTiled` takes.
     *
     * `maxLevel` IS A LEVEL COUNT HERE, not OSD's top index: core's
     * `addTiledLayer` subtracts one on the way to the tile source, which is
     * why the transcript density raster passes `densityLevels` (a count) in
     * the same slot. The level in each tile url is then OSD's flipped back
     * (`toTileLevels`), so 0 is full resolution and L covers `tile_size *
     * 2^L` layer pixels -- the ladder `bin_tiles` builds its `level_count`
     * from (`transcript_tiles.aggregate_levels`).
     */
    geometry() {
        const m = this.manifest || {};
        const supersample = Math.max(1, Number(m.supersample) || 1);
        return {
            width: Number(m.columns) * supersample,
            height: Number(m.rows) * supersample,
            tileWidth: Number(m.tile_size) || 512,
            tileHeight: Number(m.tile_size) || 512,
            maxLevel: Math.max(1, Number(m.level_count) || 1),
            transform: BinLayer.tileTransform(this.layerTransform(), supersample),
        };
    }

    /** The whole `addTiled` spec, in one place so the probe can read it. */
    tiledSpec() {
        return {
            id: `bins:${this.layerId}`,
            layerId: this.layerId,
            src: this.tileSource(),
            style: this._style,
            opacity: this.state.opacity,
            compositeOperation: "source-over",
            visible: true,
            geometry: this.geometry(),
        };
    }

    // -- drawing -----------------------------------------------------------------

    /** Whether anything should be on screen at all. */
    shows() {
        return this.visible && !this.allGenesHidden();
    }

    /**
     * Put the item up, or take it down.
     *
     * Hidden REMOVES the world item (core's `setVisible(false)`), because an
     * item at opacity 0 still fetches every tile in view -- and a 30 million
     * square layer is the last one to keep paying for while it is off.
     */
    show() {
        const on = this.shows();
        if (!this._tiles) {
            if (!on) return;
            this._style = this.tileStyle();
            this._transformKey = this.layerTransform().join(",");
            this._tiles = this.ctx.layers?.addTiled?.(this.tiledSpec()) || null;
            return;
        }
        if (!on) {
            this._tiles.setVisible?.(false);
            return;
        }
        // Style BEFORE visibility. On a hidden item core's `setStyle` only
        // records the url, so the add that `setVisible(true)` makes is
        // already the new picture -- the other order adds the old one and
        // then replaces it, a viewport of tiles fetched to be thrown away.
        this.flushStyle();
        this._tiles.setVisible?.(true);
    }

    /**
     * A new url, at most once per 200 ms.
     *
     * `setStyle` loads the replacement INVISIBLY behind the item it replaces
     * and hands over once it can draw (viewerManager `addTiledLayer`), and
     * its generation counter drops a replacement that a later one overtook
     * -- so a drag across the window slider is never a blank layer and never
     * a leaked item. The debounce is what keeps it from being a viewport of
     * tile requests per pointer move.
     */
    restyle() {
        if (this._styleTimer) window.clearTimeout(this._styleTimer);
        this._styleTimer = window.setTimeout(() => {
            this._styleTimer = null;
            this.show();
        }, BinLayer.STYLE_DEBOUNCE_MS);
    }

    flushStyle() {
        const style = this.tileStyle();
        if (style === this._style) return;
        this._style = style;
        this._tiles?.setStyle?.(style);
    }

    setVisible(on) {
        this.visible = Boolean(on);
        this.show();
        this.ctx.layers?.setVisible?.(this.layerId, this.visible);
    }

    /** A blend on the item, not a re-add: an opacity slider is dragged. */
    setOpacity(value) {
        const next = BinLayer.clamp01(value, this.state.opacity);
        if (next === this.state.opacity) return;
        this.state.opacity = next;
        this._tiles?.setOpacity?.(next);
    }

    /**
     * The layer was re-registered on its card: re-add at the new placement.
     *
     * `setStyle` keeps the placement it was built with, so a new transform
     * is the one change that has to go through remove-and-add.
     */
    syncTransform() {
        const key = this.layerTransform().join(",");
        if (!this._tiles || key === this._transformKey) return;
        this._tiles.remove?.();
        this._tiles = null;
        this.show();
    }

    // -- the legend and the cursor ------------------------------------------------

    /** Each styled gene's automatic window at the current pooling, cached. */
    async windows() {
        const genes = this.styleGenes();
        const pooling = this.pooling();
        const key = `${genes.join(",")}|${pooling}`;
        if (!this._stats.has(key)) {
            let answer = {};
            try {
                answer = await this.api.stats(this.layerId, genes, pooling);
            } catch (error) {
                answer = {};
            }
            this._stats.set(key, answer || {});
        }
        return this._stats.get(key);
    }

    /** Reference pixels -> GRID coordinates, through the layer's inverse. */
    static toGrid(transform, x, y) {
        const [a, b, c, d, e, f] = transform;
        const det = a * d - b * c;
        if (!det || !Number.isFinite(det)) return null;
        const dx = x - e;
        const dy = y - f;
        return { x: (d * dx - c * dy) / det, y: (a * dy - b * dx) / det };
    }

    /** The grid square under a reference-pixel position, or null off-grid. */
    gridAt(x, y) {
        const point = BinLayer.toGrid(this.layerTransform(), x, y);
        const m = this.manifest;
        if (!point || !m) return null;
        const column = Math.floor(point.x);
        const row = Math.floor(point.y);
        if (column < 0 || row < 0 || column >= m.columns || row >= m.rows) return null;
        return { column, row };
    }
}

if (typeof window !== "undefined") window.BinLayer = BinLayer;
if (typeof globalThis !== "undefined") globalThis.BinLayer = BinLayer;
