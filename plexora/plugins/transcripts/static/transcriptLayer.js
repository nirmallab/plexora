/**
 * The transcript layer: what is drawn, where the data for it comes from, and
 * how much of the slide one dot stands for at the current zoom.
 *
 * THE TILE CACHE IS THE DESIGN. Points are fetched one TILE at a time and
 * kept, so panning costs only the tiles that came into view. The version
 * before this refetched the entire viewport on every pan, including every
 * point that had not moved -- dragging across a slide was a continuous
 * stream of multi-megabyte responses for data the browser already had.
 *
 * POINTS MODE STAYS POINTS, AND THE LEVEL OF DETAIL IS A MERGE. Zooming out
 * used to hand the view over to the density raster once the dots would have
 * covered the screen, which answered the cost question and got the meaning
 * wrong: the user had asked for molecules and was shown a heat map, so the
 * picture changed representation without anybody choosing it. What changes
 * with the zoom now is only how many molecules one dot stands for. The
 * server merges the molecules in a bin, per gene, into one record carrying
 * a count and the position of one of the molecules it merged; the count
 * moves the size by a tenth per doubling and no further, because size is
 * deliberately a weak channel here -- see AGGREGATE_GROWTH. A bin is a
 * fixed fraction of a tile, so it doubles in image pixels with every level
 * and holds still on screen; `pickLevel` chooses the level that makes it
 * about `AGGREGATE_SPACING` screen pixels.
 *
 * AND THE DOT SHRINKS WITH THE ZOOM, which is the other half of the same
 * idea and the half that was missing. Merging alone cannot make a
 * zoomed-out view restrained: dots held at a fixed SCREEN size, spaced just
 * far enough to clear each other, cover the same fraction of the screen at
 * every zoom -- so a whole section came out as a lattice of circles with
 * the morphology invisible beneath it. So the slider sets the size at
 * `fullZoom`, the zoom at which a dot is one molecule, and below that the
 * dot fades towards a single pixel while the bins get FINER rather than
 * coarser. A hundred thousand one-pixel dots read as a tint of gene colour
 * over visible tissue, which is what a whole-slide view is for; seven
 * thousand twelve-pixel ones read as a pegboard laid on top of it.
 *
 * WHICH MAKES THE TWO CACHE PROPERTIES DIFFERENT AT DIFFERENT LEVELS, and
 * that is worth saying out loud. At level 0 a tile holds every gene and the
 * shader picks, so changing the selection or the quality threshold costs
 * nothing -- which is the right trade at the zoom where somebody is looking
 * at individual molecules and toggling genes to compare them. An aggregate
 * cannot work that way: a count is a count of the genes asked for, above the
 * threshold asked for, so both ride the request and a change of either is a
 * refetch of about six small tiles. `tagFor` names that combination, and
 * only the tiles of the current tag are drawn -- so a level change uploads
 * the new tiles underneath the old ones and switches in one assignment,
 * rather than blinking.
 *
 * DENSITY IS A MODE, NOT A FALLBACK. It is an RGB raster composited server
 * side from the selected genes and their colours, served through core's
 * layer tile route and added as an ordinary tiled image -- which is why it
 * costs no client rendering code and survives into Figure Builder's
 * server-side export, where an overlay could not. Nothing switches to it on
 * the user's behalf: it is reached by asking for it, and `showMode` takes
 * the other representation down when it does.
 */
class TranscriptLayer {

    constructor(ctx, layerId, api = null) {
        this.ctx = ctx;
        this.layerId = layerId;
        //: This plugin's own HTTP client. Held rather than fetched inline so
        //: every route this plugin knows lives in one file -- see
        //: transcriptsApi.js and tests/test_datalayer_requests.py.
        this.api = api || new TranscriptsApi(ctx);
        this.manifest = null;
        this.renderer = null;
        this.fallback = null;        // core's 2-D overlay, when there is no WebGL2
        //: `tag#x_y` -> `{ data, count, level, tag }`. The tag is in the
        //: key because a tile for one gene selection is not the same tile as
        //: one for another, and keying on the address alone is how you end
        //: up drawing yesterday's selection.
        this.tiles = new Map();
        this.pending = new Set();
        this.visible = true;
        //: The level of detail the view calls for, and the one currently on
        //: screen. They differ only while the new level's tiles are in the
        //: air -- see `settle`.
        this.level = 0;
        this.drawnLevel = 0;
        this.drawnTag = null;
        //: Genes the pointer is over, and how much bigger they are drawn.
        //: Not part of `state`: it is a reading aid that lasts as long as
        //: the pointer does, and saving it would restore somebody's hover
        //: from last Tuesday.
        this.emphasized = new Set();
        this.emphasisLift = 0;
        this._offViewport = null;
        this._density = null;
        this._densityTimer = null;
        this._densityStyle = "";
        this._refreshTimer = null;

        this.state = TranscriptLayer.defaultState();
    }

    /** The panel's state, as it is saved and restored. */
    static defaultState() {
        return {
            //: Gene NAMES, in the order the user added them. Order is the
            //: legend's order and the colour-assignment order, so it is part
            //: of the state rather than incidental.
            selected: [],
            colors: {},
            icons: {},
            //: Genes that are selected but switched off. A separate set from
            //: `selected` because turning a gene off and removing it are
            //: different intentions -- one keeps its colour and its place.
            hidden: [],
            //: `[{name, genes: []}]`. Xenium Explorer calls them gene groups
            //: and they are how a forty-gene selection stays readable.
            groups: [],
            //: Group NAMES that are rolled up. Saved, like the colours and
            //: the hidden set, because which groups somebody is working on
            //: is part of the view they built -- a marker list of eight
            //: groups is unreadable if every one of them reopens on load.
            collapsed: [],
            viewAs: "points",        // "points" | "density"
            pointStyle: "circles",   // "circles" | "icons"
            size: 6,
            //: ONE opacity for the layer, whichever way it is being drawn,
            //: and the same number the Layers card's slider holds. Two --
            //: one for points and one for density -- would be two controls
            //: for "how strongly are the transcripts drawn", only one of
            //: which ever did anything at a time.
            //:
            //: Not 1, because the density map now covers the whole layer: a
            //: ramp that paints every bin and replaces what is under it is
            //: opaque tissue-coloured paint over the morphology at full
            //: strength, and the first thing anybody does is reach for this
            //: slider. At 0.6 the nuclei read through the boxes as well as
            //: through the gaps between them, which is the picture the
            //: density map is FOR -- where the molecules are, against the
            //: tissue. Points are dimmed by the same number and lose
            //: nothing anybody will notice.
            opacity: 0.6,
            //: -- the density map ------------------------------------------
            //: How big a bin is, in MICRONS. A bin size is a physical size:
            //: "40 by 40 microns" is a sentence about tissue and "188
            //: pixels" is one about this particular scan, so the slider asks
            //: in microns and the conversion happens against the manifest's
            //: pixel size on the way to the url.
            //:
            //: One step up `BIN_LADDER` from where it started. At 20 microns
            //: a bin is about two cells across, which on a Xenium scan draws
            //: a grid fine enough that the boxes -- and the gaps the server
            //: now leaves between them -- read as texture rather than as a
            //: map somebody can point at. 40 is a small neighbourhood, and
            //: the ladder is right there for anyone who wants the finer one.
            binMicrons: 40,
            //: Which colour ramp reads the density. `genes` is the other
            //: answer and a different question: a gene per colour says WHICH
            //: gene is here, a ramp says HOW MUCH is here, and the second is
            //: what a density map is usually being asked.
            colormap: "viridis",
            //: The window, as fractions of the automatic one. Fractions
            //: because the count that means "dense" quadruples with every
            //: zoom level, and a threshold set at one zoom has to keep its
            //: meaning at the next.
            densityLow: 0,
            densityHigh: 1,
            //: Xenium's own recommended floor. A default rather than a filter
            //: baked into the cache: the score is per point and the shader
            //: does the discarding, so this is a slider that redraws.
            minQ: 20,
        };
    }

    //: The starting colours, in assignment order. Chosen to stay
    //: distinguishable on the black ground a fluorescence composite is drawn
    //: on and on the pale one an H&E is -- which rules out the darkest end of
    //: most palettes.
    static get PALETTE() {
        return ["#ff4d4d", "#4dd2ff", "#7cff4d", "#ffd24d", "#c77dff", "#ff8c42",
                "#4dffd2", "#ff6ec7", "#9be564", "#6d9eff", "#ffe066", "#e0e0e0"];
    }

    /**
     * The density ramps.
     *
     * CORE'S, `client/src/js/views/gradientRange.js` -- the same four Cell
     * Explorer colours a numeric column with, because the control that picks
     * them is now the same control. Not redefined here, and not redefined
     * there either: they MIRROR `plexora/server/utils/colormaps.py`, to the
     * digit, and the server is the one that draws them. Two copies of a
     * palette is exactly the kind of thing that drifts, so
     * `transcript_points_probe.mjs` reads both files and fails when they
     * disagree.
     */
    static get RAMPS() { return PlexoraColorRamps.RAMPS; }

    /**
     * What the picker calls each one, `genes` included.
     *
     * Core's labels for core's ramps, minus "custom": the density tile is
     * drawn server side from a ramp NAME, so a two-colour ramp the user
     * mixed in the browser is one the server has no way to produce.
     */
    static get RAMP_LABELS() {
        const labels = {};
        for (const name of Object.keys(TranscriptLayer.RAMPS)) {
            labels[name] = PlexoraColorRamps.PALETTE_LABELS[name] || name;
        }
        labels[TranscriptLayer.GENE_COLOURS] = "One colour per gene";
        return labels;
    }

    /**
     * The bin sizes the slider stops at, in microns.
     *
     * A ladder and not a free range, because these are the sizes anybody
     * actually asks for and a continuous slider over them would mostly
     * produce 37 microns. Doubling each time, so the four stops cover two
     * orders of magnitude of tissue scale -- 10 microns is about one cell,
     * 80 is a small neighbourhood.
     */
    static get BIN_LADDER() { return [10, 20, 40, 80]; }

    /**
     * How big a point may be TYPED, past where its track ends.
     *
     * The track (1..20, in the panel's markup with every other slider's
     * bounds) keeps the range that is worth dragging through: between one
     * pixel and twenty is every view of a section from "a dusting" to "dots
     * that touch", and a track that ran to a hundred would spend four
     * fifths of its length on sizes nobody drags to. The box goes further
     * because one figure at a time needs a dot that reads at print size in
     * a panel two inches wide, and that is a number somebody has measured
     * rather than dragged to.
     *
     * A hundred is generous rather than exact: what actually stops a dot
     * growing is the bin it stands for (a dot may not outgrow its patch of
     * slide) and then the GL implementation's own sprite limit, and both of
     * those are runtime numbers the panel cannot know. The ceiling is here
     * so that a typo of 10000 cannot send `pickLevel` to the coarsest level
     * in the pyramid for a picture nobody asked for.
     */
    static get POINT_SIZE_MAX() { return 100; }

    //: Hard ceiling on dots drawn in one frame. A cost limit, and now one
    //: that can always be met: past it `pickLevel` goes a level coarser,
    //: which merges four bins into one and quarters the count. It is no
    //: longer a reason to draw a different picture.
    static get MAX_POINTS_ON_SCREEN() { return 400_000; }
    //: How far apart aggregated dots are kept, as a MULTIPLE of the widest
    //: a dot can be AT THAT ZOOM.
    //:
    //: ROOM BETWEEN DOTS, AND NOT THE CROWDING CONTROL ANY MORE. It was,
    //: on the reasoning that the two ways to stop a zoomed-out view
    //: becoming a mat of transcripts are to draw fewer things or to draw
    //: them smaller, and that drawing them smaller loses them. That was
    //: wrong in the direction that mattered: merging hard enough to clear
    //: twelve-pixel dots leaves a visible lattice of discs over the tissue,
    //: while the same molecules as one-pixel specks read as the
    //: distribution they are. Both move now -- the dot fades with the zoom
    //: and the spread keeps its neighbours off it -- and of the two this is
    //: much the smaller effect, because at low zoom AGGREGATE_SPACING is
    //: the floor that actually decides the level.
    //:
    //: 2.5, WHICH IS AN INK BUDGET AND CAN BE READ AS ONE. A dot that is
    //: a 2.5th of the distance to the next one covers (pi/4)/2.5^2 of the
    //: ground where every bin is full -- about a eighth for the fattest
    //: dots the growth allows and about a twentieth for ordinary ones. At
    //: 1.6 those numbers were a third and an eighth, which is the mat: a
    //: dot 62% of the way to its neighbour reads as a disc laid over the
    //: tissue however small the disc is.
    //:
    //: This is NOT the knob for how many dots there are. Raising it merges
    //: harder, but only in whole levels -- a level is a quadtree step, so
    //: it quarters the count or leaves it alone, and which of the two you
    //: get depends on where the view happens to sit inside a band. Asking
    //: for half the dots was tried here first and gave 69% fewer at one
    //: zoom and none at the next. The grain of the bin grid is the
    //: continuous control, and it lives on the server: see
    //: `transcript_tiles.AGGREGATE_BINS`.
    //:
    //: Not more than that, because level 0 has to stay reachable at a
    //: sensible zoom: the bin there is a forty-fifth of a tile, about 23
    //: image pixels on a typical run, so a spread of 2.5 puts `fullZoom` at
    //: about one CSS pixel per image pixel -- a third of a millimetre in
    //: view, which is where somebody looking at individual molecules
    //: already is.
    static get AGGREGATE_SPREAD() { return 2.5; }
    //: A floor in CSS pixels under the same number, and the SMALLEST the
    //: overlay's grain is ever allowed to get: bins four CSS pixels apart
    //: holding one-pixel dots is a fine speckle of gene colour over
    //: morphology you can still see, which is what a whole-slide view is
    //: for. It was 8 while dots were twelve pixels wide and merging harder
    //: was the only crowding control there was; at that grain a whole
    //: section is seven thousand dots, which is a lattice rather than a
    //: distribution.
    //:
    //: It is a floor and not the grain itself. What the whole-slide view
    //: actually lands on is set by the bin grid the server aggregates into
    //: (`transcript_tiles.AGGREGATE_BINS`), which moves continuously; this
    //: only catches the case where that would put bins closer together than
    //: a dot can sit.
    //:
    //: Not an independent number: it is what the rule above already says
    //: about the SMALLEST dot there is, MIN_DOT * MAX_GROWTH *
    //: AGGREGATE_SPREAD. Written out anyway, because it is the one that
    //: binds at the zoom this was all redesigned for, and because a floor
    //: that silently disagreed with the rule it floors would be a bin size
    //: nothing chose.
    //:
    //: It is also the cost limit that binds in the worst case: it caps the
    //: dots on screen at about (width/4) x (height/4) per gene however many
    //: molecules lie underneath, and MAX_POINTS_ON_SCREEN coarsens the
    //: level further when a wide selection would still exceed that.
    static get AGGREGATE_SPACING() { return 4; }
    //: Bins across an aggregate tile, in each axis. The manifest's own value
    //: wins; this is what to assume before it has been read. Mirrors
    //: `transcript_tiles.AGGREGATE_BINS`, where the reasoning for the number
    //: is -- including why it is not round.
    static get AGGREGATE_BINS() { return 45; }
    //: Tiles kept beyond the ones on screen. One ring, so a slow pan has the
    //: next tile already uploaded.
    static get TILE_MARGIN() { return 1; }
    //: Tiles kept in GPU memory at once.
    static get MAX_TILES() { return 180; }
    //: And what they may weigh, which is the cap that actually binds.
    //:
    //: A TILE COUNT IS NOT A MEMORY BUDGET. Level-0 tiles vary by an order
    //: of magnitude between background and dense tissue -- on the Xenium run
    //: this was measured against, fifteen thousand molecules on average and
    //: fifty thousand in the middle of the section -- and the cache now
    //: holds tiles from several levels at once, because keeping the level
    //: you just zoomed away from is what makes zooming back instant. 180 of
    //: the big ones is 140 MB, which is not a fraction of anything. Counted
    //: in bytes it is a budget; counted in tiles it was a guess that
    //: happened to be right for the average tile.
    static get MAX_TILE_BYTES() { return 64 * 1024 * 1024; }
    //: How much of the bin a hover gives up, at the point where the gene
    //: has a dot in every one of them. See `emphasisFill`.
    static get EMPHASIS_CROWDING() { return 0.55; }
    static get UNSELECTED_COLOR() { return "#9aa0aa"; }

    // -- lifecycle ---------------------------------------------------------

    async attach() {
        this.manifest = await this.loadManifest();
        if (!this.manifest || this.manifest.status !== "ready") return null;

        const viewer = this.ctx.viewer?.viewer;
        if (viewer && typeof TranscriptPointRenderer !== "undefined") {
            const renderer = new TranscriptPointRenderer(viewer, {
                anchorIndex: () => this.ctx.viewer?.layerStack?.anchorIndex?.() || 0,
            });
            this.renderer = renderer.isSupported() ? renderer : null;
            if (!this.renderer) renderer.destroy?.();
        }
        if (!this.renderer) {
            // No WebGL2 -- a headless probe, a locked-down browser. Core's
            // 2-D overlay draws the same points from the same tiles, slower,
            // which is a viewer rather than a blank rectangle.
            this.fallback = this.ctx.layers?.addOverlay?.({
                id: `points:${this.layerId}`,
                kind: "points",
                draw: (opts) => this.drawFallback(opts),
            }) || null;
        }

        // THE LAYERS PANEL IS WHERE A LAYER IS SWITCHED, and this is what
        // puts this one there. Core cards what core draws; a points layer
        // gets a card once something says it draws it, and then the eye, the
        // opacity slider and the drag on that card all arrive back here
        // through `onLayerChange` -- the same route an image layer's do.
        // Before this the section had a checkbox of its own in its heading,
        // which was one layer switched in a way no other layer was.
        this.ctx.layers?.claim?.(this.layerId);
        const record = this.ctx.layers?.get?.(this.layerId);
        if (record) {
            this.visible = record.visible !== false;
            // THE PANEL'S OPACITY WINS, and pushes itself onto the card --
            // the other way round, which this used to do, made both of them
            // unreachable. Core does not persist the opacity of a layer
            // somebody else draws (`layerManager` only writes
            // `render.opacity` for an unclaimed one), so the stack's 1 is a
            // placeholder and not a choice, while this state is saved per
            // project and holds either the user's last drag or the default
            // above. Reading the placeholder back turned a restored 60% into
            // 100% on the way in, and meant no default here could ever be
            // anything but core's.
            this.ctx.layers?.setOpacity?.(this.layerId, this.state.opacity);
        }

        this.applyStyle();
        this._offViewport = this.ctx.layers?.onViewportChange?.(
            () => this.scheduleRefresh()) || null;
        if (this.renderer) {
            // The renderer's own canvas is not repainted by OpenSeadragon, so
            // it has to hear the same two events core's overlay does.
            viewer?.addHandler("update-viewport", () => this.renderer.invalidate());
            viewer?.addHandler("resize", () => this.renderer.invalidate());
        }
        this.refresh();
        return this;
    }

    destroy() {
        if (this._refreshTimer) window.clearTimeout(this._refreshTimer);
        if (this._densityTimer) window.clearTimeout(this._densityTimer);
        this._offViewport?.();
        this._offViewport = null;
        this.renderer?.destroy();
        this.renderer = null;
        this.fallback?.remove();
        this.fallback = null;
        this._density?.remove();
        this._density = null;
        this.tiles.clear();
    }

    async loadManifest() {
        try {
            return await this.api.manifest(this.layerId);
        } catch (error) {
            console.error("transcripts: the manifest could not be read", error);
            return null;
        }
    }

    // -- what the panel asks for -------------------------------------------

    genes() { return this.manifest?.genes || []; }

    geneCounts() { return this.manifest?.gene_counts || []; }

    countOf(gene) {
        const index = this.genes().indexOf(gene);
        return index < 0 ? 0 : (this.geneCounts()[index] || 0);
    }

    /** Selected and not switched off, in the user's own order. */
    drawnGenes() {
        const hidden = new Set(this.state.hidden);
        return this.state.selected.filter((gene) => !hidden.has(gene));
    }

    isHidden(gene) { return this.state.hidden.includes(gene); }

    /** Every gene that is selected but switched off. */
    allGenesHidden() {
        const selected = this.state.selected;
        return selected.length > 0 && this.drawnGenes().length === 0;
    }

    /**
     * Every selected gene on, or every one off.
     *
     * The list's own eye. Hiding is not removing -- a gene keeps its colour,
     * its icon and its place in its group -- which is what makes this the
     * undoable half of the pair it sits next to in the menu ("Clear all
     * genes" is the other half, and is not).
     */
    setAllGenesHidden(hidden) {
        this.state.hidden = hidden ? [...this.state.selected] : [];
        this.applyStyle();
        this.refresh();
    }

    // The folds and the groups are core's bookkeeping (PlexoraGeneGroups,
    // views/geneList.js), shared with the Visium HD bin layer so the one
    // gene tree both panels draw means the same thing over either.

    isCollapsed(name) { return PlexoraGeneGroups.isCollapsed(this.state, name); }

    setGroupCollapsed(name, collapsed) {
        PlexoraGeneGroups.setCollapsed(this.state, name, collapsed);
    }

    /** Every group rolled up, or every one open. */
    collapseAll(collapsed) { PlexoraGeneGroups.collapseAll(this.state, collapsed); }

    /** True while there is a group and none of them is open. */
    allCollapsed() { return PlexoraGeneGroups.allCollapsed(this.state); }

    colorFor(gene) {
        return this.state.colors[gene] || TranscriptLayer.UNSELECTED_COLOR;
    }

    iconFor(gene) {
        const named = this.state.icons[gene];
        const icons = TranscriptPointRenderer?.ICONS || ["circle"];
        const found = icons.indexOf(named);
        if (found >= 0) return found;
        // Assigned by position rather than at random, so the same selection
        // always produces the same legend.
        return Math.max(0, this.state.selected.indexOf(gene)) % icons.length;
    }

    /** Add a gene, with the next colour in the palette. */
    addGene(name) {
        if (!name || this.state.selected.includes(name)) return false;
        this.state.selected = [...this.state.selected, name];
        if (!this.state.colors[name]) {
            const palette = TranscriptLayer.PALETTE;
            this.state.colors[name] =
                palette[(this.state.selected.length - 1) % palette.length];
        }
        this.applyStyle();
        this.refresh();
        return true;
    }

    removeGene(name) {
        this.state.selected = this.state.selected.filter((gene) => gene !== name);
        this.state.hidden = this.state.hidden.filter((gene) => gene !== name);
        PlexoraGeneGroups.dropGene(this.state, name);
        this.applyStyle();
        this.refresh();
    }

    clearGenes() {
        this.state.selected = [];
        this.state.hidden = [];
        PlexoraGeneGroups.empty(this.state);
        this.applyStyle();
        this.refresh();
    }

    setGeneHidden(name, hidden) {
        const set = new Set(this.state.hidden);
        if (hidden) set.add(name); else set.delete(name);
        this.state.hidden = [...set];
        this.applyStyle();
        this.refresh();
    }

    setColor(gene, color) {
        this.state.colors[gene] = color;
        this.applyStyle();
        this.scheduleDensity();
    }

    setIcon(gene, icon) {
        this.state.icons[gene] = icon;
        this.applyStyle();
    }

    /** Back to palette order and positional icons, the way Xenium Explorer's
     *  own "reset icons and colors" does. */
    resetAppearance() {
        const palette = TranscriptLayer.PALETTE;
        this.state.colors = {};
        this.state.icons = {};
        this.state.selected.forEach((gene, index) => {
            this.state.colors[gene] = palette[index % palette.length];
        });
        this.applyStyle();
        this.scheduleDensity();
    }

    //: State the DENSITY RASTER is a function of. Changing one of these is a
    //: new tile url, so it has to go through `scheduleDensity` -- which is
    //: debounced, because every one of them is on a slider somebody drags.
    static get DENSITY_KEYS() {
        return ["minQ", "binMicrons", "colormap", "densityLow", "densityHigh"];
    }

    set(values = {}) {
        const restyle = TranscriptLayer.DENSITY_KEYS.some(
            (key) => values[key] !== undefined && values[key] !== this.state[key]);
        Object.assign(this.state, values);
        this.applyStyle();
        if (restyle) this.scheduleDensity();
        this.refresh();
    }

    /**
     * How strongly the layer is drawn, whichever way it is being drawn.
     *
     * One number for both representations. The shader multiplies it into
     * every molecule; the density raster is an OpenSeadragon world item and
     * gets it as the item's own opacity, which is a blend rather than a
     * re-add -- an opacity slider is dragged, and rebuilding the world item
     * per pointer move would refetch the viewport on every tick.
     */
    setOpacity(value) {
        const next = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(next) || next === this.state.opacity) return;
        this.state.opacity = next;
        this.renderer?.set({ opacity: next });
        this.fallback?.invalidate?.();
        this._density?.setOpacity?.(next);
    }

    // -- groups -------------------------------------------------------------

    createGroup(name) { return PlexoraGeneGroups.create(this.state, name); }

    renameGroup(from, to) { return PlexoraGeneGroups.rename(this.state, from, to); }

    deleteGroup(name) { PlexoraGeneGroups.remove(this.state, name); }

    /** Move a gene into a group, or out of every group when `name` is null. */
    assignToGroup(gene, name) { PlexoraGeneGroups.assign(this.state, gene, name); }

    /** Selected genes that are in no group -- the tree's top level. */
    ungrouped() { return PlexoraGeneGroups.ungrouped(this.state); }

    // -- drawing ------------------------------------------------------------

    setVisible(on) {
        this.visible = Boolean(on);
        this.showMode(this.showsDensity());
        this.ctx.layers?.setVisible?.(this.layerId, this.visible);
    }

    /**
     * Which of the two pictures is on screen.
     *
     * THE CHOSEN MODE, AND NOTHING ELSE. It used to be "or there are too
     * many points to draw", which meant zooming out turned a molecule view
     * into a heat map on its own -- the one thing a level of detail must not
     * do, because it changes what is being shown rather than how finely.
     * Points mode now answers a dense view by merging molecules into larger
     * dots (`pickLevel`), so there is nothing left for this to be but the
     * mode the user picked.
     */
    showsDensity() {
        return this.state.viewAs === "density";
    }

    /**
     * Put one representation up and take the other DOWN.
     *
     * One call for both, in one place, because "switch to points" and
     * "remove the density raster" are the same event and any arrangement
     * that lets them be separate statements eventually lets one of them be
     * missed. Taking the other one down is not cosmetic: a hidden density
     * layer that is still in the world keeps requesting a tile per screenful
     * on every pan, and the point renderer holds its last frame on a canvas
     * until something clears it.
     */
    showMode(density) {
        // A SELECTION WITH EVERY EYE OFF DRAWS NOTHING, which needs saying
        // here because the two pictures answer it differently on their own.
        // Points draw per gene, so switching them all off empties the view
        // by itself; the density raster is one summed field, and the server
        // reads "no genes named" as THE WHOLE PANEL -- which is right for a
        // panel nobody has picked from yet (see
        // `layer_sources._selected_indices`) and exactly wrong the moment
        // somebody turns their four genes off and the map gets brighter.
        // The list's own eye made that one click away.
        const on = this.visible && !this.allGenesHidden();
        this.setDensity(on && density);
        this.renderer?.setVisible(on && !density);
        this.fallback?.setVisible?.(on && !density);
    }

    /** Push colours, visibility, icons and the sliders at the renderer. */
    applyStyle() {
        const drawn = new Set(this.drawnGenes());
        const entries = this.genes().map((gene) => ({
            color: TranscriptLayer.toRgb(this.colorFor(gene)),
            visible: drawn.has(gene),
            icon: this.iconFor(gene),
            emphasis: this.emphasized.has(gene),
        }));
        this.renderer?.setGeneTable(entries);
        this.renderer?.set({
            pointSize: this.state.size,
            opacity: this.state.opacity,
            minQ: this.state.minQ,
            styleMode: this.state.pointStyle === "icons" ? 1 : 0,
            // How big a dot standing for a whole bin may get. The level ON
            // SCREEN and not the one being fetched: the shader has to cap
            // the picture it is drawing, which until `settle` is still the
            // old one.
            binPixels: this.drawnLevel > 0 ? this.binPixelsAt(this.drawnLevel) : 0,
            // The zoom at which the slider is taken literally. Below it the
            // renderer fades the dot every frame, which is where the
            // zoomed-out restraint comes from -- see ZOOM_FADE.
            fullZoom: this.fullZoom(),
            emphasisFill: this.emphasisFill(),
        });
        this.fallback?.invalidate?.();
    }

    /**
     * Pick a gene, or a group of them, out of the field for as long as the
     * pointer is on it.
     *
     * WHAT MAKES THIS FREE. The set of emphasised genes is a byte per gene
     * in the table the shader already reads -- so changing which gene is
     * lifted is a write of a few hundred bytes, and HOW FAR it is lifted is
     * one uniform the renderer eases. No tile is refetched, no aggregation
     * is recomputed, no geometry is rebuilt; running the pointer down a
     * five-hundred-gene list costs five hundred small texture writes.
     *
     * Clearing leaves the MASK in place and only eases the amount back to
     * 0, which is what makes leaving a row a shrink rather than a snap. A
     * mask with no lift behind it draws exactly as no mask at all.
     */
    emphasize(names) {
        const wanted = new Set((names || []).filter(Boolean));
        this.emphasisLift = wanted.size ? 1 : 0;
        if (wanted.size) {
            this.emphasized = wanted;
            this.applyStyle();
        }
        this.renderer?.set({ emphasis: this.emphasisLift });
        this.fallback?.invalidate?.();
    }

    /**
     * How much of its bin a HOVERED dot may fill, between 0 and 1.
     *
     * A LIFT THAT FILLS EVERY BIN IS NOT A HIGHLIGHT, IT IS A FLOOD FILL.
     * The lift is sized to be unmistakable against a field of one-pixel
     * dots, which is right for the gene somebody is usually looking for and
     * wrong for the two or three in a panel that are expressed everywhere:
     * ACTB on this Xenium run has a dot in every bin of the section at
     * whole-slide zoom, so lifting all of them to the full bin painted the
     * tissue solid red and answered the question by erasing the picture.
     *
     * So the lift gives up to EMPHASIS_CROWDING of the bin back, in
     * proportion to how many of the bins in view the gene actually
     * occupies. A sparse gene -- the case hover exists for -- is untouched
     * and still reads as separate dots; an abundant one comes back as a
     * dense stipple with the morphology visible between, which says "this
     * is everywhere" just as clearly and leaves the section on screen.
     *
     * Estimated from the manifest's per-gene counts, not measured from the
     * tiles: a hover must not read a megabyte of vertex data, and the
     * answer only has to be right to within a fraction of a dot.
     */
    emphasisFill() {
        if (!this.emphasized.size || this.drawnLevel <= 0) return 1;
        const bounds = this.ctx.layers?.viewport?.();
        if (!bounds) return 1;
        const drawn = this.drawnGenes();
        const total = drawn.reduce((sum, gene) => sum + this.countOf(gene), 0);
        if (!total) return 1;
        const bin = this.binPixelsAt(this.drawnLevel);
        const cells = Math.max(1, ((bounds.maxX - bounds.minX) / bin)
                                  * ((bounds.maxY - bounds.minY) / bin));
        const molecules = this.estimateInView(bounds);
        let crowded = 0;
        for (const gene of this.emphasized) {
            if (!drawn.includes(gene)) continue;
            crowded = Math.max(crowded, Math.min(
                1, molecules * (this.countOf(gene) / total) / cells));
        }
        return 1 - TranscriptLayer.EMPHASIS_CROWDING * crowded;
    }

    static toRgb(hex) {
        const clean = String(hex || "").replace("#", "");
        const full = clean.length === 3
            ? clean.split("").map((c) => c + c).join("") : clean;
        if (full.length !== 6) return [154, 160, 170];
        return [0, 2, 4].map((at) => parseInt(full.slice(at, at + 2), 16));
    }

    // -- the density raster ---------------------------------------------------

    /**
     * The style string core's layer tile route reads.
     *
     * The genes and their colours ride the URL and are composited server
     * side -- one request per tile instead of one per gene per tile, and one
     * world item instead of one per gene. `minq` and not `q`: `q` is already
     * that route's encoding-quality parameter.
     *
     * The bin size goes over in LAYER PIXELS and is chosen in microns, which
     * is the one conversion this file makes: only the manifest knows how big
     * a pixel is, and only the panel knows that a biologist thinks in
     * microns.
     */
    densityStyle() {
        const drawn = this.drawnGenes();
        const colours = drawn.map((gene) => this.colorFor(gene).replace("#", ""));
        const parts = [`color=${TranscriptLayer.DENSITY_COLOR.replace("#", "")}`];
        if (drawn.length) {
            parts.push(`genes=${drawn.map(encodeURIComponent).join(",")}`);
            parts.push(`colors=${colours.join(",")}`);
        }
        parts.push(`minq=${Math.round(this.state.minQ)}`);
        parts.push(`bin=${this.binPixels()}`);
        // Absent for the per-gene composite, so the server keeps drawing the
        // picture it always drew rather than being handed a ramp name it has
        // to know to ignore.
        if (this.usesRamp()) parts.push(`ramp=${this.state.colormap}`);
        parts.push(`dlo=${Number(this.state.densityLow || 0).toFixed(4)}`);
        parts.push(`dhi=${Number(
            this.state.densityHigh === undefined ? 1 : this.state.densityHigh).toFixed(4)}`);
        // THE PLUGIN'S VERSION, for the same reason every asset url carries
        // it. A density tile is served `max-age=31536000` and its ETag names
        // the project and the style, NOT the code that drew the pixels -- so
        // a change to the rasterizer is invisible for a year: the browser
        // re-uses the bytes it already has without even asking, and the fix
        // looks like it did not work. The server ignores this key (see
        // `layer_sources.parse_style`, which reads only the ones it knows);
        // it is here to make the url different, which is the only thing that
        // reaches a cache entry that is never revalidated.
        const version = this.manifest?.version;
        if (version) parts.push(`v=${encodeURIComponent(version)}`);
        return parts.join("&");
    }

    /** Whether the density is one field read off a ramp, or a gene per colour. */
    usesRamp() {
        return Boolean(this.state.colormap)
            && this.state.colormap !== TranscriptLayer.GENE_COLOURS;
    }

    /**
     * How the density raster meets the morphology image under it.
     *
     * THE OTHER HALF OF `density_ramp_tile`'s decision to paint empty bins.
     * A ramp covers the layer edge to edge -- zero is the ramp's bottom
     * colour, not a hole -- so it has to REPLACE what is under it: added
     * with `lighter`, a dark end painted over the whole slide would be a
     * flat wash on top of the image. The opacity slider is what puts the
     * tissue back, and that is the control for it.
     *
     * The per-gene composite is the opposite picture: a handful of coloured
     * clouds on nothing, which is a fluorescence channel and adds like one.
     */
    densityBlend() {
        return this.usesRamp() ? "source-over" : "lighter";
    }

    /** The value of `colormap` that means "a colour per gene, as the tree shows". */
    static get GENE_COLOURS() { return "genes"; }

    /** Microns per reference pixel, or null where the project never said. */
    pixelSize() {
        const value = Number(this.manifest?.pixel_size);
        return Number.isFinite(value) && value > 0 ? value : null;
    }

    /**
     * The bin size the url carries: layer pixels, from the microns asked for.
     *
     * Falls back to reading the slider AS pixels when the project has no
     * pixel size. The panel relabels itself to match, rather than converting
     * with a made-up scale and drawing bins that are not the size they say.
     */
    binPixels() {
        const microns = Math.max(1, Number(this.state.binMicrons) || 1);
        const size = this.pixelSize();
        return Math.max(1, Math.round(size ? microns / size : microns));
    }

    /**
     * The two ends of the colour ramp, in molecules per bin.
     *
     * What the legend under the ramp says. Derived from exactly what the
     * server stretches against (`layer_sources.density_scale`), with the
     * multiple sent in the manifest rather than repeated here -- a legend
     * that disagreed with the picture would be worse than no legend.
     *
     * No zoom level in it, and that is the bin size doing its job: a bin is
     * a fixed number of IMAGE pixels, so it holds the same number of
     * molecules however far out the view is, and the legend keeps its
     * meaning while somebody zooms. It only stops being true at the levels
     * where a tile has fewer pixels than the bin size asks for bins, where
     * the server widens the bins because it has nowhere finer to draw.
     */
    densityRange() {
        const manifest = this.manifest || {};
        const points = Number(manifest.point_count) || 0;
        const area = (Number(manifest.width) || 0) * (Number(manifest.height) || 0);
        const stretch = Number(manifest.density_stretch) || 8;
        if (!points || !area) return null;
        const bin = this.binPixels();
        // Scaled by what the SELECTION is worth, exactly as the server
        // scales the window it stretches against: the ceiling is a multiple
        // of the average bin over the whole panel, and three genes out of
        // 480 fill a bin with three genes' worth.
        const ceiling = stretch * (points / area) * bin * bin * this.selectedShare();
        const low = Number(this.state.densityLow) || 0;
        const high = this.state.densityHigh === undefined
            ? 1 : Number(this.state.densityHigh);
        return { low: ceiling * low, high: ceiling * high, ceiling };
    }

    /**
     * The drawn genes' share of the layer's molecules, or 1 for all of them.
     *
     * Mirrors `layer_sources._selection_share`. Here only so the legend
     * under the ramp claims the numbers the picture was actually stretched
     * against -- the server does the stretching.
     */
    selectedShare() {
        const drawn = this.drawnGenes();
        const total = Number(this.manifest?.point_count) || 0;
        if (!drawn.length || !total) return 1;
        const picked = drawn.reduce((sum, gene) => sum + this.countOf(gene), 0);
        return Math.max(picked / total, 1e-6);
    }

    //: What a density raster is drawn in when no gene has been picked: the
    //: whole panel at once, which is "where is there anything at all".
    static get DENSITY_COLOR() { return "#4da3ff"; }

    /**
     * Re-style the density raster, at most once per animation settle.
     *
     * `setStyle` re-adds the world item, so a colour DRAG would otherwise
     * tear the raster down and rebuild it on every pointer move.
     */
    scheduleDensity() {
        if (this._densityTimer) window.clearTimeout(this._densityTimer);
        this._densityTimer = window.setTimeout(() => {
            this._densityTimer = null;
            const style = this.densityStyle();
            if (style === this._densityStyle) return;
            this._densityStyle = style;
            // Before the style, because `setStyle` re-adds the world item and
            // an item added with the wrong blend would draw one frame of a
            // ramp ADDED to the image before the next change corrected it.
            this._density?.setBlend?.(this.densityBlend());
            this._density?.setStyle?.(style);
        }, 200);
    }

    setDensity(on) {
        if (!on) {
            this._density?.setVisible(false);
            return;
        }
        if (this._density) {
            this._density.setVisible(this.visible);
            this._density.setOpacity?.(this.state.opacity);
            // Unconditionally, and not only when the style moved: the blend
            // follows the colormap, and switching between a ramp and the
            // per-gene colours is exactly the change that has to land on an
            // item this branch is otherwise leaving alone.
            this._density.setBlend?.(this.densityBlend());
            const style = this.densityStyle();
            if (style !== this._densityStyle) {
                this._densityStyle = style;
                this._density.setStyle?.(style);
            }
            return;
        }
        const manifest = this.manifest || {};
        this._densityStyle = this.densityStyle();
        this._density = this.ctx.layers?.addTiled?.({
            id: `density:${this.layerId}`,
            layerId: this.layerId,
            src: this.ctx.url(
                `generated/layer/${encodeURIComponent(this.ctx.datasource)}`
                + `/${encodeURIComponent(this.layerId)}/density/`),
            style: this._densityStyle,
            opacity: this.state.opacity,
            // Mode-dependent, and `densityBlend` says why: a ramp covers the
            // slide and replaces, a gene-per-colour composite is a channel
            // and adds.
            compositeOperation: this.densityBlend(),
            geometry: {
                width: manifest.width,
                height: manifest.height,
                maxLevel: TranscriptLayer.densityLevels(manifest),
                tileWidth: manifest.tile_size,
                tileHeight: manifest.tile_size,
                // Already in reference pixels: the reader applied the layer's
                // own transform on the way in, which is the one place that
                // knows both the file's units and the registration.
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

    // -- the level of detail ---------------------------------------------------

    /**
     * Image pixels one aggregation bin covers at this level.
     *
     * A tile is cut into the same number of bins at every level and covers
     * twice as much ground each time, so the bin doubles with the level --
     * which is what lets `pickLevel` hold its size on SCREEN constant while
     * the zoom changes by orders of magnitude.
     */
    binPixelsAt(level) {
        const tile = this.manifest?.tile_size || 512;
        const bins = Number(this.manifest?.aggregate_bins)
            || TranscriptLayer.AGGREGATE_BINS;
        return (tile / Math.max(1, bins)) * (2 ** Math.max(0, level));
    }

    /** Image pixels one tile covers at this level, in each axis. */
    tileSpan(level) {
        return (this.manifest?.tile_size || 512) * (2 ** Math.max(0, level));
    }

    /**
     * The coarsest level, at which one tile holds the whole layer.
     *
     * Deliberately the ladder the density pyramid climbs, and the same one
     * `transcript_tiles.aggregate_levels` computes on the server: "level 3"
     * has to mean one thing across the plugin, or a tile address means a
     * different patch of slide at each end of the wire.
     */
    maxLevel() {
        return Math.max(
            0, TranscriptLayer.densityLevels(this.manifest || {}) - 1);
    }

    /** The point size the user asked for, in CSS pixels. */
    baseSize() {
        return Math.max(1, Number(this.state.size) || 6);
    }

    /**
     * The zoom -- CSS pixels per image pixel -- at which the slider is taken
     * literally.
     *
     * Also the zoom at which `pickLevel` reaches level 0, and that is not a
     * coincidence but the definition: "the dots are full size" and "a dot is
     * one molecule" are the same moment, and tying them together is what
     * stops the fade and the merge from arguing with each other. Below it
     * both act -- the dot fades, the level merges; above it neither has
     * anything left to do.
     */
    fullZoom() {
        const grow = (typeof TranscriptPointRenderer !== "undefined"
            && TranscriptPointRenderer.MAX_GROWTH) || 1.6;
        const spacing = Math.max(TranscriptLayer.AGGREGATE_SPACING,
            this.baseSize() * grow * TranscriptLayer.AGGREGATE_SPREAD);
        return spacing / Math.max(1e-9, this.binPixelsAt(0));
    }

    /** How big one dot is drawn at rest at this zoom, in CSS pixels. */
    restingSize(zoom) {
        const renderer = typeof TranscriptPointRenderer !== "undefined"
            ? TranscriptPointRenderer : null;
        if (!renderer || typeof renderer.restingSize !== "function") {
            return this.baseSize();
        }
        return renderer.restingSize(this.baseSize(), zoom, this.fullZoom());
    }

    /**
     * The widest a single dot can be drawn at this zoom, in CSS pixels.
     *
     * The resting size there times the most an aggregate may grow for its
     * count, so a level leaves room for the BIGGEST dot it will produce and
     * not the smallest. Called with no zoom it answers for the top of the
     * range, which is what "how big can a dot get" means to the panel.
     */
    dotSize(zoom = null) {
        const grow = (typeof TranscriptPointRenderer !== "undefined"
            && TranscriptPointRenderer.MAX_GROWTH) || 1.6;
        const size = zoom === null ? this.baseSize() : this.restingSize(zoom);
        return size * grow;
    }

    /** How wide the viewer is, in CSS pixels. */
    screenWidth() {
        const container = this.ctx.viewer?.viewer?.container
            || (typeof document !== "undefined"
                && document.getElementById("openseadragon"));
        return Math.max(1, container?.clientWidth || 1200);
    }

    /** The tile range the current view covers at this level, plus a ring. */
    tilesInView(bounds, level = 0) {
        const manifest = this.manifest;
        if (!manifest || !bounds) return [];
        const span = this.tileSpan(level);
        const margin = TranscriptLayer.TILE_MARGIN;
        // Derived from the layer's size rather than read off the manifest,
        // because the manifest's `columns`/`rows` count LEVEL 0 tiles and
        // every level above it has a quarter as many.
        const columns = Math.max(1, Math.ceil((manifest.width || span) / span));
        const rows = Math.max(1, Math.ceil((manifest.height || span) / span));
        const x0 = Math.max(0, Math.floor(bounds.minX / span) - margin);
        const y0 = Math.max(0, Math.floor(bounds.minY / span) - margin);
        const x1 = Math.min(columns - 1, Math.floor(bounds.maxX / span) + margin);
        const y1 = Math.min(rows - 1, Math.floor(bounds.maxY / span) + margin);
        const keys = [];
        for (let y = y0; y <= y1; y += 1) {
            for (let x = x0; x <= x1; x += 1) keys.push(`${x}_${y}`);
        }
        return keys;
    }

    /**
     * How many of the SELECTED molecules the view holds, from the manifest.
     *
     * No fetch: the per-tile counts and the per-gene counts are both in the
     * manifest precisely so this can be answered before deciding whether to
     * fetch. Asking the server would be requesting the thing we are deciding
     * whether to request.
     */
    estimateInView(bounds) {
        const manifest = this.manifest;
        if (!manifest || !bounds) return 0;
        const size = manifest.tile_size || 512;
        const counts = manifest.tile_counts || [];
        const x0 = Math.max(0, Math.floor(bounds.minX / size));
        const y0 = Math.max(0, Math.floor(bounds.minY / size));
        const x1 = Math.min((manifest.columns || 1) - 1, Math.floor(bounds.maxX / size));
        const y1 = Math.min((manifest.rows || 1) - 1, Math.floor(bounds.maxY / size));
        // PRO-RATED BY HOW MUCH OF EACH TILE IS ON SCREEN. Whole tiles was
        // the obvious reading and it has a floor nobody could get under: a
        // view inside ONE tile counts that whole tile however far in the
        // user zooms, so on a slide whose busiest tile holds 80,000
        // molecules the estimate never drops -- which used to mean the view
        // stayed density at 40x and now would mean it stayed merged at 40x,
        // wrong in the same way for the same reason. Within a tile the
        // molecules are near enough uniform at the scale of a screen, so the
        // overlap fraction is the right correction and it is exact once the
        // view covers whole tiles.
        const span = (lo, hi, index) => Math.max(
            0, Math.min(hi, (index + 1) * size) - Math.max(lo, index * size));
        let inView = 0;
        for (let y = y0; y <= y1; y += 1) {
            const row = counts[y] || [];
            const height = span(bounds.minY, bounds.maxY, y) / size;
            for (let x = x0; x <= x1; x += 1) {
                const width = span(bounds.minX, bounds.maxX, x) / size;
                inView += (row[x] || 0) * width * height;
            }
        }
        // Scaled by the selection's own share of the panel, from the per-gene
        // counts -- so one rare gene out of five hundred is drawn molecule
        // by molecule rather than merged because the panel is dense.
        const total = manifest.point_count || 0;
        if (!total) return inView;
        const drawn = this.drawnGenes();
        if (!drawn.length) return 0;
        const selected = drawn.reduce(
            (sum, gene) => sum + this.countOf(gene), 0);
        return inView * (selected / total);
    }

    /**
     * How many DOTS this level would put on screen, from the manifest alone.
     *
     * Not the same question as `estimateInView`, and the difference is the
     * whole point of aggregating: a bin can only ever contribute one dot per
     * gene however many molecules are in it, so the count is capped by the
     * number of bins in view. Per gene and not in total, because the bins
     * are per gene -- ten genes over a screenful of four thousand bins is up
     * to forty thousand dots, not four.
     */
    estimateAggregates(bounds, level) {
        const drawn = this.drawnGenes();
        if (!drawn.length || !bounds) return 0;
        const molecules = this.estimateInView(bounds);
        if (level <= 0) return molecules;
        const total = drawn.reduce((sum, gene) => sum + this.countOf(gene), 0);
        if (!total) return 0;
        const bin = this.binPixelsAt(level);
        const cells = Math.max(1, ((bounds.maxX - bounds.minX) / bin)
                                  * ((bounds.maxY - bounds.minY) / bin));
        let dots = 0;
        for (const gene of drawn) {
            dots += Math.min(molecules * (this.countOf(gene) / total), cells);
        }
        return dots;
    }

    /**
     * The level of detail this view calls for.
     *
     * THE ZOOM DECIDES, AND THE BUDGET ONLY EVER COARSENS IT. A bin is
     * `binPixelsAt(level)` image pixels, so it is that times the zoom on
     * screen; the level wanted is the first at which that reaches the
     * spacing the dots need, which keeps them from landing on top of each
     * other. Everything finer than level 0 is level 0: at that zoom the
     * molecules are already further apart than a dot and there is nothing
     * to merge.
     *
     * THE SPACING IS ITSELF A FUNCTION OF THE ZOOM, because the dot is
     * (`restingSize`). Pulling back shrinks the dot, which asks for LESS
     * room, which chooses a finer level than the same view would have got
     * under a fixed dot size -- more dots, each a fraction of the ink. That
     * is the whole of the zoomed-out redesign: below the point where the
     * fade bottoms out, `AGGREGATE_SPACING` is the floor that decides, and
     * the result is a grain of colour rather than a lattice of discs.
     *
     * Then the cost check, which is what replaced the density fallback. If
     * the level the zoom asked for would still put more dots on screen than
     * the frame budget allows -- a hundred genes at once, say -- the answer
     * is a COARSER level, not a different picture: four bins become one, the
     * dot that stands for them grows by the square root of what it now
     * holds, and the count on screen quarters. There is always a level that
     * fits, because the coarsest one is a single tile.
     */
    pickLevel(bounds, screenWidth = null) {
        if (!this.manifest || !bounds) return 0;
        const width = Math.max(1e-6, bounds.maxX - bounds.minX);
        const zoom = (screenWidth || this.screenWidth()) / width;
        const spacing = Math.max(
            TranscriptLayer.AGGREGATE_SPACING,
            this.dotSize(zoom) * TranscriptLayer.AGGREGATE_SPREAD);
        const wanted = Math.log2(spacing / Math.max(this.binPixelsAt(0) * zoom,
                                                    1e-9));
        const top = this.maxLevel();
        let level = Number.isFinite(wanted) ? Math.ceil(wanted) : 0;
        level = Math.max(0, Math.min(top, level));
        while (level < top
               && this.estimateAggregates(bounds, level)
                  > TranscriptLayer.MAX_POINTS_ON_SCREEN) {
            level += 1;
        }
        return level;
    }

    /**
     * What an aggregate tile's CONTENTS depend on, level aside.
     *
     * A count is a count of the genes that were asked for, above the
     * threshold that was asked for. Two tiles at the same address with
     * different answers to either are different tiles, and treating them as
     * one is how a gene that was switched off goes on being drawn.
     */
    signature() {
        return `${this.drawnGenes().join(",")}|${Math.round(this.state.minQ)}`;
    }

    /**
     * The name of the picture a tile belongs to.
     *
     * Level 0 is just "0": those tiles hold every gene and carry their own
     * quality scores, so one of them is good for any selection and any
     * threshold, which is what keeps toggling a gene free at the zoom where
     * somebody is comparing genes molecule by molecule.
     */
    tagFor(level) {
        return level <= 0 ? "0" : `${level}|${this.signature()}`;
    }

    static tileKey(tag, address) { return `${tag}#${address}`; }

    scheduleRefresh() {
        if (this._refreshTimer) return;
        this._refreshTimer = window.setTimeout(() => {
            this._refreshTimer = null;
            this.refresh();
        }, 100);
    }

    /** Bring what is loaded into line with what the view needs. */
    refresh() {
        if (!this.manifest || this.manifest.status !== "ready") return;
        const bounds = this.ctx.layers?.viewport?.();

        // Settled without asking the view anything, which is what lets a
        // mode change land before the viewport is readable: the raster has
        // to come down the moment the user clicks Points, even on a frame
        // too early to know what is in view.
        const density = this.showsDensity();
        this.showMode(density);
        if (!bounds) return;

        if (density || !this.drawnGenes().length) {
            this.renderer?.invalidate();
            this.fallback?.invalidate?.();
            return;
        }
        this.level = this.pickLevel(bounds);
        this.loadTiles(this.tilesInView(bounds, this.level), this.level);
    }

    /**
     * Fetch the tiles this view needs and forget the ones nothing will draw.
     *
     * At level 0 a tile holds EVERY gene and the shader picks, so the tag is
     * just the level and a tile survives any change of selection. Above it
     * the tag carries the selection and the threshold too, because an
     * aggregate is a count OF those -- see `tagFor`.
     */
    loadTiles(addresses, level = 0) {
        const tag = this.tagFor(level);
        const keys = [];
        for (const address of addresses) {
            const key = TranscriptLayer.tileKey(tag, address);
            keys.push(key);
            if (this.tiles.has(key) || this.pending.has(key)) continue;
            this.fetchTile(key, address, level, tag);
        }
        this.settle(keys, level, tag);
        this.renderer?.invalidate();
        this.fallback?.invalidate?.();
    }

    /**
     * Show a new level only once every tile it needs has arrived.
     *
     * THE ALTERNATIVE IS A BLINK. Switching the moment the level changes
     * leaves a frame or two with nothing drawn, because the new tiles are
     * still in the air; drawing both in the meantime doubles every dot
     * where the levels overlap. Holding the old picture until the new one
     * is complete costs a little GPU memory for the overlap and is the only
     * one of the three that looks like a zoom.
     */
    settle(keys, level, tag) {
        if (keys.some((key) => this.pending.has(key))) return;
        if (this.drawnTag !== tag) {
            this.drawnTag = tag;
            this.drawnLevel = level;
            // Before `setActive`, so the first frame of the new level is
            // already capped to the new bin rather than to the old one.
            this.applyStyle();
            this.renderer?.setActive(tag);
        }
        this.evict(keys);
        this.renderer?.invalidate();
        this.fallback?.invalidate?.();
    }

    /** Re-check whether the level the view wants is ready to be shown. */
    settleCurrent() {
        const bounds = this.ctx.layers?.viewport?.();
        if (!bounds || this.showsDensity()) return;
        const tag = this.tagFor(this.level);
        this.settle(
            this.tilesInView(bounds, this.level).map(
                (address) => TranscriptLayer.tileKey(tag, address)),
            this.level, tag);
    }

    /** Forget tiles nothing will draw again, and trim the rest to the cap. */
    evict(wanted) {
        const current = this.signature();
        const keep = new Set(wanted || []);
        let bytes = 0;
        for (const tile of this.tiles.values()) bytes += tile.data?.byteLength || 0;
        // Insertion order, so what goes first is what was fetched longest
        // ago -- and what is on screen is never a candidate at all.
        for (const [key, tile] of [...this.tiles]) {
            if (keep.has(key)) continue;
            // A tile for a gene selection or a threshold that is no longer
            // current can never be drawn again whatever the view does; one
            // for another LEVEL of the current selection can, the moment
            // somebody zooms back, so it is kept while there is room.
            const reusable = tile.level === 0 || tile.signature === current;
            const room = this.tiles.size <= TranscriptLayer.MAX_TILES
                && bytes <= TranscriptLayer.MAX_TILE_BYTES;
            if (reusable && room) continue;
            bytes -= tile.data?.byteLength || 0;
            this.tiles.delete(key);
            this.renderer?.dropTile(key);
        }
    }

    async fetchTile(key, address, level, tag) {
        this.pending.add(key);
        const aggregated = level > 0;
        try {
            const buffer = await this.api.points(this.layerId, address, {
                level,
                // Level 0 asks for the whole panel on purpose; an aggregate
                // can only be computed for a stated selection.
                genes: aggregated ? this.drawnGenes() : null,
                minq: aggregated ? this.state.minQ : null,
            });
            if (!buffer) return;
            const packed = TranscriptLayer.repack(buffer, aggregated);
            packed.level = level;
            packed.tag = tag;
            packed.signature = aggregated ? this.signature() : "";
            this.tiles.set(key, packed);
            this.renderer?.setTile(key, packed, tag);
        } catch (error) {
            console.error(`transcripts: tile ${key} could not be fetched`, error);
        } finally {
            this.pending.delete(key);
            // The last tile of a level is what lets that level come up.
            this.settleCurrent();
        }
    }

    /** The wire records, repacked for the GPU. See TranscriptPointRenderer. */
    static repack(buffer, aggregated = false) {
        if (typeof TranscriptPointRenderer !== "undefined") {
            return TranscriptPointRenderer.repack(buffer, aggregated);
        }
        // The same layout, written out again for the case where the renderer
        // never loaded: 11 or 14 bytes in, 16 out, x and y first.
        const view = new DataView(buffer);
        const stride = aggregated ? 14 : 11;
        const count = Math.floor(view.byteLength / stride);
        const out = new ArrayBuffer(count * 16);
        const floats = new Float32Array(out);
        const shorts = new Uint16Array(out);
        const bytes = new Uint8Array(out);
        for (let index = 0; index < count; index += 1) {
            const at = index * stride;
            floats[index * 4] = view.getFloat32(at + 2, true);
            floats[index * 4 + 1] = view.getFloat32(at + 6, true);
            floats[index * 4 + 3] = aggregated ? view.getUint32(at + 10, true) : 1;
            shorts[index * 8 + 4] = view.getUint16(at, true);
            bytes[index * 16 + 10] = aggregated ? 255 : view.getUint8(at + 10);
        }
        return { data: out, count };
    }

    /**
     * The no-WebGL2 path: the same tiles, through core's 2-D overlay.
     *
     * Batched by gene rather than one path per point -- a fill per point is a
     * state change per point. Slower than the GL path by roughly the ratio
     * you would expect, and a viewer rather than a blank rectangle.
     *
     * The SAME arithmetic as the shader, on purpose: only the tiles of the
     * drawn tag, the same square-root growth, the same cap. A fallback that
     * sized its aggregates differently would make "what the picture means"
     * depend on which browser was open.
     */
    drawFallback(opts) {
        const context = opts.context;
        const drawn = new Set(this.drawnGenes());
        if (!drawn.size || !this.tiles.size) return;
        const names = this.genes();
        // The same dot the shader draws: radius 0.95/2 of the point size, in
        // SCREEN pixels, and `px` is one screen pixel in image units -- so
        // its reciprocal is the zoom the fade is a function of. It used to
        // be size/4, which drew the fallback at half the size the GL path
        // does for the same slider value.
        const zoom = opts.px > 0 ? 1 / opts.px : 1;
        const radius = Math.max(0.35, this.restingSize(zoom) * 0.475 * opts.px);
        // The same cap the shader applies, in image units rather than
        // screen ones: a dot may not outgrow the patch it stands for.
        const ceiling = this.drawnLevel > 0
            ? this.binPixelsAt(this.drawnLevel) / 2 : Infinity;
        // And the same lift, off the size the SLIDER asks for rather than
        // the faded one, held to that same ceiling.
        const emphasis = (typeof TranscriptPointRenderer !== "undefined"
            && TranscriptPointRenderer.EMPHASIS_SCALE) || 1.8;
        const lifted = Math.min(
            this.baseSize() * emphasis * 0.475 * opts.px,
            ceiling * this.emphasisFill());
        const step = (typeof TranscriptPointRenderer !== "undefined"
            && TranscriptPointRenderer.AGGREGATE_GROWTH) || 0.10;
        const most = (typeof TranscriptPointRenderer !== "undefined"
            && TranscriptPointRenderer.MAX_GROWTH) || 1.6;
        const previous = context.globalAlpha;
        context.globalAlpha = this.state.opacity;
        const byGene = new Map();
        for (const packed of this.tiles.values()) {
            if (this.drawnTag !== null && packed.tag !== this.drawnTag) continue;
            const floats = new Float32Array(packed.data);
            const shorts = new Uint16Array(packed.data);
            const bytes = new Uint8Array(packed.data);
            for (let index = 0; index < packed.count; index += 1) {
                if (bytes[index * 16 + 10] < this.state.minQ) continue;
                const gene = shorts[index * 8 + 4];
                const name = names[gene];
                if (!drawn.has(name)) continue;
                let bucket = byGene.get(name);
                if (!bucket) {
                    bucket = [];
                    byGene.set(name, bucket);
                }
                const grow = Math.min(
                    1 + step * Math.log2(Math.max(floats[index * 4 + 3], 1)),
                    most);
                const rest = Math.min(radius * grow, ceiling);
                bucket.push(floats[index * 4], floats[index * 4 + 1],
                            this.emphasisLift && this.emphasized.has(name)
                                ? Math.max(rest, lifted) : rest);
            }
        }
        // Emphasised genes last, so a hovered gene lands ON TOP of the field
        // it is being picked out of -- the same reason the GL path splits
        // its draw into two.
        const order = [...byGene.keys()].sort((a, b) =>
            Number(this.emphasized.has(a)) - Number(this.emphasized.has(b)));
        for (const gene of order) {
            const points = byGene.get(gene);
            context.fillStyle = this.colorFor(gene);
            context.beginPath();
            for (let at = 0; at < points.length; at += 3) {
                const size = points[at + 2];
                context.moveTo(points[at] + size, points[at + 1]);
                context.arc(points[at], points[at + 1], size, 0, Math.PI * 2);
            }
            context.fill();
        }
        context.globalAlpha = previous;
    }
}

if (typeof window !== "undefined") window.TranscriptLayer = TranscriptLayer;
if (typeof globalThis !== "undefined") globalThis.TranscriptLayer = TranscriptLayer;
