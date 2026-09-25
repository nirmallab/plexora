/**
 * The Transcripts layer's controls.
 *
 * Modelled on Xenium Explorer, because that is what is open in the other
 * window: a search box that ADDS genes, a tree of what is selected with an
 * eye, a colour and an icon on every row, groups the user makes, and a block
 * of drawing controls underneath.
 *
 * It is a LAYER section and not a tool panel, and everything about its
 * lifecycle follows from that. It mounts on page load for any sample with a
 * transcript layer, and it has no close button because there is no state of
 * the app in which closing it and leaving the sample open means anything.
 * toolLoader never sees it, which is deliberate: that path stands a tool down
 * the moment another one opens, and opening Gating should not turn the
 * transcripts off.
 *
 * NOR A HEADING OF ITS OWN. This panel is the BODY of the transcript layer's
 * card in the Layers list -- core owns the grip, the chevron, the title, the
 * eye and the X, and moves this markup into the card (see
 * `views/layerManager.js`). So everything here is what a card cannot know:
 * which genes, in what colour, drawn how. The eye on that card and the
 * opacity slider in here both land on the same stack record, which
 * `syncVisibility` below reads back.
 *
 * Its state is saved PER PROJECT. A gene selection is an analysis decision --
 * these forty genes, in these colours, grouped like this -- and coming back
 * the next day to an empty panel would reasonably read as the tool having
 * lost the work.
 *
 * What it deliberately does NOT do is as much a part of the design as what it
 * does: no counts per region, no differential expression, no co-expression,
 * no clustering. Those interpret the data, and the moment one appears here
 * the viewer's Transcripts section has become an analysis application.
 */
class TranscriptsSidebarController {

    constructor(ctx) {
        this.ctx = ctx;
        //: Every route this plugin knows, in one file -- see
        //: transcriptsApi.js. Core's DataLayer must never learn a plugin's
        //: addresses, and a fetch buried in a controller is one nothing can
        //: exercise in isolation.
        this.api = new TranscriptsApi(ctx);
        this.layer = null;
        this.select = null;
        this.poll = null;
        //: The shared gene tree (PlexoraGeneTree), once the layer is up.
        this.tree = null;
        this.saveTimer = null;
        this.saved = null;
        this.pendingGroupFor = null;
        //: range element id -> the PlexoraSlider wrapped around it. The panel
        //: is MOVED into a layer card rather than copied, so these survive the
        //: move -- and the guard in `bindSlider` makes a second bind a no-op
        //: rather than a second set of handlers on the same element.
        this.sliders = new Map();
        this.binSlider = null;
    }

    el(id) { return document.getElementById(id); }

    // -- lifecycle ---------------------------------------------------------

    /**
     * The project's saved panel state, as `viewerSidebar.init` wants it.
     *
     * An ARRAY, because that lifecycle tests `saved.length` to decide whether
     * this module had anything stored -- see viewerSidebar.init. One element
     * or none.
     */
    async fetchSaved() {
        try {
            const body = await this.api.getState();
            return body && Object.keys(body).length ? [body] : [];
        } catch (error) {
            return [];
        }
    }

    async applyOrDefault(saved) {
        this.saved = Array.isArray(saved) ? saved[0] : (saved || null);
        // Kept as well as awaited. `applyCarryState` can only run once the
        // layer has attached -- before that the manifest is null and the panel
        // does not yet know one gene from another -- and a layer section is
        // applied by a `forEach` that does not await, so the promise is the
        // only handle on "has this finished".
        this._started = this.start();
        await this._started;
    }

    persistIfNeeded() { /* saved on change, not on boot */ }

    // -- walking to a sibling sample (services/carryOver.js) ---------------

    /**
     * Which genes are being looked at, and how they are drawn.
     *
     * All of this is an arrangement rather than a measurement: a gene list is
     * a question about the panel, and the display controls (points or density,
     * size, opacity, bin size, ramp, quality floor) are how somebody chose to
     * look at it. Nothing here is derived from this sample's numbers -- the
     * density contrast window travels as the FRACTIONS it is stored as, which
     * is exactly why it is stored that way, so "the top fifth" stays the top
     * fifth of whatever the next sample's counts turn out to be.
     */
    captureCarryState() {
        const state = this.layer && this.layer.state;
        if (!state) return null;
        return {
            selected: [...(state.selected || [])],
            colors: { ...(state.colors || {}) },
            icons: { ...(state.icons || {}) },
            hidden: [...(state.hidden || [])],
            groups: (state.groups || []).map((group) => ({
                name: group.name, genes: [...(group.genes || [])],
            })),
            viewAs: state.viewAs,
            pointStyle: state.pointStyle,
            size: state.size,
            opacity: state.opacity,
            binMicrons: state.binMicrons,
            colormap: state.colormap,
            densityLow: state.densityLow,
            densityHigh: state.densityHigh,
            minQ: state.minQ,
        };
    }

    /**
     * Look at the same genes here, where this sample's panel has them.
     *
     * Awaits the attach first: until the manifest has landed `genes()` is
     * empty, and filtering against it then would drop every gene and report
     * the whole selection as missing on a sample that has all of it.
     *
     * The display controls are taken on whole -- they describe how to draw,
     * not what -- and the gene-shaped state is filtered to what this panel
     * actually holds. Deliberately no save(): this is one page view's
     * arrangement, and writing it would make the sample next door's gene list
     * this sample's own for good.
     */
    async applyCarryState(state) {
        if (!state) return { skipped: [] };
        try {
            await this._started;
        } catch (error) {
            return { skipped: ["Transcripts: this sample's layer did not open"] };
        }
        if (!this.layer || !this.layer.manifest) {
            return { skipped: ["Transcripts: no transcript layer on this sample"] };
        }

        const panel = new Set(this.layer.genes());
        const wanted = (state.selected || []).filter((gene) => panel.has(gene));
        const missing = (state.selected || []).filter((gene) => !panel.has(gene));

        const live = this.layer.state;
        live.selected = wanted;
        live.colors = {};
        (state.selected || []).forEach((gene) => {
            if (panel.has(gene) && state.colors && state.colors[gene]) {
                live.colors[gene] = state.colors[gene];
            }
        });
        live.icons = {};
        (state.selected || []).forEach((gene) => {
            if (panel.has(gene) && state.icons && state.icons[gene]) {
                live.icons[gene] = state.icons[gene];
            }
        });
        live.hidden = (state.hidden || []).filter((gene) => panel.has(gene));
        // A group whose genes are all absent goes with them; one that is
        // partly here keeps the part that is, because the grouping is the
        // user's own organisation of the panel and half of it is still useful.
        live.groups = (state.groups || [])
            .map((group) => ({
                name: group.name,
                genes: (group.genes || []).filter((gene) => panel.has(gene)),
            }))
            .filter((group) => group.genes.length);

        ["viewAs", "pointStyle", "size", "opacity", "binMicrons", "colormap",
         "densityLow", "densityHigh", "minQ"].forEach((key) => {
            if (state[key] !== undefined) live[key] = state[key];
        });

        this.fill();
        this.paintTree();
        this.paintControls();
        // The layer's own "bring what is loaded into line with what the view
        // needs" -- the mode, the tiles and the raster all follow from the
        // state just written, so nothing here has to know which of them moved.
        this.layer.refresh?.();

        if (!missing.length) return { skipped: [] };
        const named = missing.slice(0, 3).join(", ");
        const rest = missing.length - Math.min(3, missing.length);
        return {
            skipped: [`Transcripts: ${named}${rest ? ` and ${rest} more` : ""}`
                + " not in this panel"],
        };
    }

    /**
     * Bind only what exists before any data has been read.
     *
     * `viewerSidebar` calls setup(), then fetchSaved(), then applyOrDefault()
     * -- so the real start belongs in the last of the three, or the panel
     * would be built once with no saved state and again with it.
     */
    setup() { this.bindStaticControls(); }

    async start() {
        this.bindStaticControls();
        const layerId = this.firstTranscriptLayer();
        if (!layerId) {
            this.show("empty");
            this.watchForALayer();
            return;
        }

        this.layer = new TranscriptLayer(this.ctx, layerId, this.api);
        if (this.saved) Object.assign(this.layer.state, this.saved);
        const attached = await this.layer.attach();
        if (!attached) {
            // Registered but not tiled yet -- or tiled by a version whose
            // record layout this one cannot read. Both are a build.
            this.show("building");
            await this.requestBuild(layerId);
            this.watchBuild(layerId);
            return;
        }
        this.show("content");
        this.fill();
        this.bind();
        this.paintTree();
        this.paintControls();
        this.ctx.layers?.onLayerChange?.(() => this.syncVisibility());
        this.ctx.onCleanup?.(() => this.destroy());
    }

    show(which) {
        for (const [key, id] of [["empty", "transcripts_empty"],
                                 ["building", "transcripts_building"],
                                 ["content", "transcripts_content"]]) {
            const node = this.el(id);
            if (node) node.hidden = key !== which;
        }
    }

    /**
     * The project's transcript layer.
     *
     * By MODALITY, which is what the data means, rather than by kind -- which
     * is a rendering strategy shared with cell centroids and Visium spots.
     * First rather than a chooser, because a run has one transcript table.
     */
    firstTranscriptLayer() {
        const layers = this.ctx.layers?.find?.({ modality: "transcripts" })
            || this.ctx.layers?.list?.().filter(
                (layer) => layer.kind === "points" && layer.id !== "__centroids__")
            || [];
        return layers[0]?.id || null;
    }

    watchForALayer() {
        this.ctx.layers?.onLayerChange?.(() => {
            if (this.layer || this.poll) return;
            if (!this.firstTranscriptLayer()) return;
            this.start();
        });
    }

    /** Ask for the tiles, unless a build is already running for this layer. */
    async requestBuild(layerId) {
        try {
            await this.api.build(layerId);
        } catch (error) {
            // The poll below reports whatever state the server is really in,
            // which is a better answer than whatever this request's failure
            // suggests.
        }
    }

    watchBuild(layerId) {
        this.poll = setInterval(async () => {
            try {
                const state = await this.api.status(layerId);
                const label = this.el("transcripts_building_label");
                // `pending`, `ready`, `failed` -- core's one job vocabulary
                // (server/models/layer_jobs.py).
                if (state.status === "pending" && label) {
                    label.textContent = state.message
                        || `Preparing transcripts… (${state.stage || "working"})`;
                }
                if (state.status === "ready") {
                    clearInterval(this.poll);
                    this.poll = null;
                    await this.start();
                }
                if (state.status === "failed") {
                    clearInterval(this.poll);
                    this.poll = null;
                    // The install line, not the stack trace: a missing pyarrow
                    // is something the user can act on, and
                    // "ModuleNotFoundError" is not what to hand a biologist.
                    if (label) {
                        label.textContent = state.install
                            ? `${state.error} — ${state.install}`
                            : `Transcripts could not be prepared: ${state.error}`;
                    }
                }
            } catch (error) {
                clearInterval(this.poll);
                this.poll = null;
            }
        }, 1500);
    }

    // -- the gene search ----------------------------------------------------

    fill() {
        const genes = this.layer.genes();
        // Rounded, because this is a subtitle and not a readout: "19.1M"
        // is the fact, and eight digits of it in 11px type is a line nobody
        // reads and everybody has to look past. The exact count is on the
        // element's title for anyone who wants it.
        const total = this.layer.manifest.point_count || 0;
        this.el("transcripts_count").textContent =
            TranscriptsSidebarController.compact(total);
        this.el("transcripts_gene_count").textContent = genes.length.toLocaleString();
        const meta = this.el("transcripts_meta");
        if (meta) {
            meta.title = `${total.toLocaleString()} molecules · `
                + `${genes.length.toLocaleString()} genes in this panel`;
        }

        const mount = this.el("transcripts_gene_select");
        if (mount && typeof SearchableSelect !== "undefined") {
            // Core's own combobox, so a five-hundred-gene panel behaves the
            // way every other long list in Plexora does. It is single-select
            // and that is the right shape here: picking a gene ADDS it and
            // clears the box, ready for the next.
            this.select = new SearchableSelect(mount, {
                options: genes,
                placeholder: "Search genes…",
                emptyText: "No genes match",
                ariaLabel: "Search genes",
                describeOption: (name) => {
                    const count = this.layer.countOf(name);
                    return count ? count.toLocaleString() : "none";
                },
                onChange: (name) => this.addGene(name),
            });
        }
    }

    addGene(name) {
        if (!name) return;
        if (this.layer.addGene(name)) {
            this.paintTree();
            this.save();
        }
        this.select?.setValue?.("");
    }

    // -- the tree ------------------------------------------------------------

    /**
     * The shared gene tree (core's PlexoraGeneTree, views/geneList.js), built
     * once the layer is up. Core's because the Visium HD panel draws the same
     * one; what is this panel's own is the icon button on every row, the
     * molecule count, and what a change means -- a redraw of points already
     * fetched, never a refetch.
     */
    ensureTree() {
        if (this.tree || !this.layer || typeof PlexoraGeneTree === "undefined") {
            if (this.tree) this.tree.options.layer = this.layer;
            return this.tree;
        }
        this.tree = new PlexoraGeneTree(this.el("transcripts_tree"), {
            layer: this.layer,
            count: (gene) => {
                const count = this.layer.countOf(gene) || 0;
                return { text: count.toLocaleString(),
                         title: `${count.toLocaleString()} molecules in this sample` };
            },
            rowExtras: (gene) => [this.buildIconButton(gene)],
            onChange: (kind) => {
                // A colour pick repaints the ramp's per-gene swatch and
                // nothing else: the picker that made it is still open in the
                // row, and rebuilding the tree would close it under the hand.
                if (kind === "color") this.paintRamp();
                else this.paintTree();
                this.save();
            },
        });
        this.tree.bindListActions(this.el("transcripts_all_eye"),
                                  this.el("transcripts_collapse_all"));
        return this.tree;
    }

    /**
     * One row per selected gene, under its group.
     *
     * Rebuilt rather than patched (see PlexoraGeneTree.paint): a row owns no
     * state of its own, which is what makes this safe to call on every
     * change.
     */
    paintTree() {
        if (!this.layer) return;
        // Whatever the pointer was over is about to be removed from the
        // page, so no `mouseout` will ever arrive for it and the lift would
        // stay on a gene nothing is pointing at.
        this.hoverGenes([]);
        // NOT the sliders. They live in the panel above this tree, they are
        // bound once, and the tree's rebuild does not touch them -- but
        // destroying them here once did, and because each one has ADOPTED the
        // template's own `<input type="range">`, it took that input out of
        // the page with it: Point size, Opacity and Min Q-score lost their
        // controls within a moment of the panel opening.
        this.ensureTree()?.paint();

        const selected = this.layer.state.selected.length;
        const panel = this.layer.genes().length;
        this.el("transcripts_counter").textContent = `${selected}/${panel}`;
        const none = this.el("transcripts_none");
        if (none) none.hidden = selected > 0;

        // The ramp is a function of the SELECTION as well as of the palette:
        // its two numbers are molecules per bin, scaled by what the chosen
        // genes are worth (`densityRange`), and the per-gene swatch is drawn
        // from the colours that are on. Repainted here rather than at every
        // call site that touches a gene, because every one of them ends up
        // in this method.
        this.paintRamp();
    }

    /** The row's shape: this panel's own, because only molecules are drawn
     *  as glyphs. Cycles through the renderer's shapes on click. */
    buildIconButton(gene) {
        const icon = document.createElement("button");
        icon.type = "button";
        icon.className = "transcripts-icon-button";
        const icons = TranscriptPointRenderer?.ICONS || ["circle"];
        const current = icons[this.layer.iconFor(gene)] || "circle";
        icon.textContent = TranscriptsSidebarController.ICON_GLYPH[current] || "●";
        icon.title = `${gene}: ${current}. Click for the next shape.`;
        icon.setAttribute("aria-label", icon.title);
        icon.addEventListener("click", () => {
            const next = icons[(icons.indexOf(current) + 1) % icons.length];
            this.layer.setIcon(gene, next);
            this.paintTree();
            this.save();
        });
        return icon;
    }

    //: What each shape looks like in a sidebar row. Text rather than an SVG
    //: because the row is 14 pixels tall and the shader draws the real one.
    static get ICON_GLYPH() {
        return {
            circle: "●", square: "■", diamond: "◆", triangle: "▲",
            plus: "✚", cross: "✖", ring: "◯", star: "★",
        };
    }

    // -- the controls ---------------------------------------------------------

    /**
     * The one control bound before there is a layer behind it.
     *
     * It hangs off the gene list's own button now, and not off a kebab in the
     * card header. A header kebab is where a card's actions go when they are
     * about the CARD; these three are about the gene list -- name a group of
     * them, put the colours back, empty it -- and the list is at the far end
     * of the panel from the header.
     *
     * The button it took over was dead. It was "add the gene in the box", and
     * it could not ever fire: picking a gene adds it and CLEARS the box (see
     * `addGene`), so by the time a hand reached the button there was never
     * anything in the box left to add.
     */
    bindStaticControls() {
        const button = this.el("transcripts_add");
        if (button && !button.dataset.bound) {
            button.dataset.bound = "1";
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                this.openMenu(button);
            });
        }
    }

    bind() {
        for (const [id, attribute, key] of [
            ["transcripts_view_control", "data-view-as", "viewAs"],
            ["transcripts_style_control", "data-point-style", "pointStyle"],
        ]) {
            const control = this.el(id);
            control?.addEventListener("click", (event) => {
                const button = event.target.closest(`[${attribute}]`);
                if (!button) return;
                this.layer.set({ [key]: button.getAttribute(attribute) });
                this.paintControls();
                this.save();
            });
        }

        // `set` refreshes, which re-decides points vs density -- and the size
        // is half of that decision now: the same molecules are a scatter at
        // two pixels and a slab at six.
        // `fieldMax`: the track stops at 20 and the box does not -- a figure
        // wanting a 40-pixel dot types it, and the thumb sits at the end of
        // its travel, which is the track saying what it can say.
        this.bindSlider("transcripts_size", "transcripts_size_value",
                        (value) => this.layer.set({ size: value }), "",
                        { fieldMax: TranscriptLayer.POINT_SIZE_MAX });
        // Straight at the layer rather than through `set`: this is the same
        // number the Layers card's slider holds, and it is a blend on both
        // representations rather than anything that changes what is fetched.
        this.bindSlider("transcripts_opacity", "transcripts_opacity_value",
                        (value) => {
                            this.layer.setOpacity(value / 100);
                            this.ctx.layers?.setOpacity?.(
                                this.layer.layerId, value / 100);
                        }, "%");
        this.bindSlider("transcripts_minq", "transcripts_minq_value",
                        (value) => this.layer.set({ minQ: value }));

        this.bindBinSize();
        this.bindHover();
    }

    /**
     * The pointer on a gene in the list picks that gene out on the slide.
     *
     * DELEGATED TO THE TREE rather than bound per row, for two reasons that
     * are both about a 480-gene panel: a listener pair per row is a
     * thousand listeners to attach and detach on every repaint, and a row
     * that is replaced while the pointer is on it never fires its own
     * `mouseout`. One pair on the container survives every repaint.
     *
     * A gene row wins over the group box around it, because `data-gene` is
     * the closer match -- so pointing at a group's heading lifts all of it
     * and pointing at one of its rows lifts just that one.
     */
    bindHover() {
        const tree = this.el("transcripts_tree");
        if (!tree) return;
        tree.addEventListener("mouseover", (event) => {
            const row = event.target.closest?.("[data-gene]");
            if (row) {
                this.hoverGenes([row.getAttribute("data-gene")]);
                return;
            }
            const box = event.target.closest?.("[data-group]");
            const group = box && this.layer?.state.groups.find(
                (entry) => entry.name === box.getAttribute("data-group"));
            this.hoverGenes(group ? [...group.genes] : []);
        });
        tree.addEventListener("mouseleave", () => this.hoverGenes([]));
    }

    /**
     * Lift these genes, unless they are already the ones lifted.
     *
     * The guard is what makes this cheap enough to hang off `mouseover`,
     * which fires again for every child the pointer crosses inside one row
     * -- the eye button, the swatch, the count. Without it, moving across
     * a single row would rewrite the gene table four times.
     */
    hoverGenes(names) {
        const key = (names || []).join(",");
        if (key === this._hovered) return;
        this._hovered = key;
        this.layer?.emphasize(names || []);
    }

    /**
     * Bin size: a ladder on the slider, microns in the box.
     *
     * Two controls over one number, and they are not the same control. The
     * slider steps between the four sizes anybody asks for; the box takes any
     * number, because somebody matching a figure to a published one needs 25
     * and the ladder has not got it. The slider then shows the nearest rung,
     * which is honest -- it is a slider, and 25 is between two of its stops.
     */
    bindBinSize() {
        const range = this.el("transcripts_bin");
        const number = this.el("transcripts_bin_value");
        const ladder = TranscriptLayer.BIN_LADDER;
        // `field: false`: the box beside this one is not this slider's readout.
        // The slider holds a rung INDEX and the box holds microns, so a field
        // built from the slider's own value would show "2".
        if (range && !this.binSlider) {
            this.binSlider = new PlexoraSlider(range, {
                field: false, ariaLabel: "Bin size",
                className: "transcripts-bin-slider",
                onInput: (index) => this.setBin(
                    ladder[Math.max(0, Math.min(ladder.length - 1, index))]),
            });
        }
        number?.addEventListener("change", () => this.setBin(Number(number.value)));
    }

    setBin(microns) {
        const value = Math.max(1, Math.round(Number(microns) || 1));
        this.layer.set({ binMicrons: value });
        this.paintBin();
        this.paintRamp();
        this.save();
    }

    /**
     * A range and a number that are the same value.
     *
     * Both, because they answer different questions: the slider is for "a bit
     * bigger" and the box is for "exactly six", and a figure that has to match
     * another figure needs the second one. This panel had that pairing first;
     * `PlexoraSlider` is what made it every slider in the app.
     *
     * `onInput` and no `onChange`: what is behind these is a redraw of points
     * already fetched, and the write to the server on the same path is the
     * 800ms debounce in `save()`.
     *
     * `options` goes straight through to the slider, which is how point size
     * gets a box that types past the end of its track.
     */
    bindSlider(rangeId, fieldId, apply, unit = "", options = {}) {
        const range = this.el(rangeId);
        if (!range || this.sliders.has(rangeId)) return;
        this.sliders.set(rangeId, new PlexoraSlider(range, {
            fieldId, unit, decimals: 0, ...options,
            onInput: (value) => {
                apply(value);
                this.save();
            },
        }));
    }

    paintControls() {
        if (!this.layer) return;
        this.paintViewControl();
        for (const button of document.querySelectorAll("#transcripts_style_control [data-point-style]")) {
            const on = button.getAttribute("data-point-style") === this.layer.state.pointStyle;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", String(on));
        }
        // Silently: this is the panel catching the controls up with state it
        // already applied, and an announcement here would re-apply it and
        // schedule a save nobody asked for.
        for (const [id, value] of [
            ["transcripts_size", this.layer.state.size],
            ["transcripts_opacity", Math.round(this.layer.state.opacity * 100)],
            ["transcripts_minq", this.layer.state.minQ],
        ]) {
            this.sliders.get(id)?.set(value, { silent: true });
        }

        // Only the controls the chosen representation has, REPLACED rather
        // than disabled: a point style beside a density map moves nothing,
        // and a bin size beside individual molecules is a control for a
        // picture that is not up. Opacity and Min Q-score are outside both
        // blocks, so they keep their place on the switch -- the two rows
        // that apply either way are the two that must not jump.
        const density = this.layer.state.viewAs === "density";
        const points = this.el("transcripts_point_controls");
        const densityBox = this.el("transcripts_density_controls");
        if (points) points.hidden = density;
        if (densityBox) densityBox.hidden = !density;

        this.paintBin();
        this.paintRamp();
    }

    /** 19,131,744 -> "19.1M". A subtitle's worth of a number. */
    static compact(value) {
        const count = Number(value) || 0;
        if (count >= 1e9) return `${(count / 1e9).toFixed(1)}B`;
        if (count >= 1e6) return `${(count / 1e6).toFixed(1)}M`;
        if (count >= 10_000) return `${Math.round(count / 1e3)}K`;
        return count.toLocaleString();
    }

    /** A count of molecules in a bin, for the two numbers under the ramp. */
    static countLabel(value) {
        if (!Number.isFinite(value)) return "—";
        // Zero is zero, not "0.00": the two decimals are there for a window
        // narrowed down to a fraction of a molecule, and an empty bin has no
        // precision to report.
        if (value === 0) return "0";
        if (value >= 10) return Math.round(value).toLocaleString();
        return value.toFixed(2);
    }

    paintBin() {
        const ladder = TranscriptLayer.BIN_LADDER;
        const microns = Number(this.layer.state.binMicrons) || ladder[0];
        const range = this.el("transcripts_bin");
        const number = this.el("transcripts_bin_value");
        // The nearest rung. A box set to 25 leaves the handle between 20 and
        // 40, and putting it on one of them is the slider saying what it can
        // say rather than overwriting what was typed.
        let nearest = 0;
        ladder.forEach((value, index) => {
            if (Math.abs(value - microns) < Math.abs(ladder[nearest] - microns)) {
                nearest = index;
            }
        });
        if (range) range.max = String(ladder.length - 1);
        this.binSlider?.setBounds({ min: 0, max: ladder.length - 1 });
        this.binSlider?.set(nearest, { silent: true });
        if (number) number.value = String(microns);

        // Microns where the project knows how big a pixel is, and pixels
        // where it does not -- rather than converting with a made-up scale
        // and labelling bins with a size they are not.
        const sized = Boolean(this.layer.pixelSize());
        const unit = this.el("transcripts_bin_unit");
        if (unit) unit.textContent = sized ? "µm" : "px";
        const label = document.querySelector('label[for="transcripts_bin"]');
        if (label) {
            label.title = sized
                ? "The side of one square bin, in microns"
                : "In pixels: this project has no pixel size recorded, so a "
                  + "bin cannot be given a physical size";
        }

        // The rungs, under the slider that snaps to them. Bare numbers: the
        // unit is already at the end of the row, and "10x10µm 20x20µm
        // 40x40µm 80x80µm" across a 200px sidebar was four ellipses.
        const ticks = this.el("transcripts_bin_ticks");
        if (ticks) {
            ticks.replaceChildren();
            ladder.forEach((value, index) => {
                const tick = document.createElement("span");
                tick.textContent = String(value);
                tick.classList.toggle("is-current", index === nearest);
                ticks.appendChild(tick);
            });
        }
    }

    /**
     * The colour map: core's gradient control, over the density's own scale.
     *
     * THE SAME OBJECT CELL EXPLORER USES for a numeric column, and not a
     * lookalike -- `client/src/js/views/gradientRange.js`. What it replaced
     * here was a <select> of palette names, a swatch under it, a legend
     * under that, and a separate "Density map scale threshold" section with
     * its own pair of handles and two number boxes: five rows to say what
     * one bar says by being painted as the mapping it describes.
     *
     * THE HANDLES MOVE OVER 0..1, not over counts, because that is what the
     * state is: `densityLow` and `densityHigh` are fractions of the
     * automatic window, so a threshold set at one zoom keeps its meaning at
     * the next. The formatter turns each end into molecules per bin on the
     * way to the label, which is the number a reader wants and not the
     * number the url carries.
     */
    paintRamp() {
        const mount = this.el("transcripts_ramp");
        if (!mount) return;
        if (!this.ramp) {
            if (typeof PlexoraGradientRange === "undefined") return;
            this.ramp = new PlexoraGradientRange(mount, {
                onRange: (low, high) => {
                    // Auto hands back two nulls, which is the whole window.
                    this.layer.set({
                        densityLow: low === null ? 0 : low,
                        densityHigh: high === null ? 1 : high,
                    });
                    this.paintRamp();
                    this.save();
                },
                onPalette: (palette) => {
                    this.layer.set({ colormap: palette });
                    this.paintRamp();
                    this.save();
                },
            });
        }
        // Re-pointed every time rather than only at construction: the panel's
        // markup is re-mounted when the layer card is rebuilt, and a control
        // holding the old node renders into a div that is no longer on the
        // page -- which looks exactly like a colour map that stopped working.
        this.ramp.container = mount;

        const scale = this.layer.densityRange();
        const ceiling = scale?.ceiling || 0;
        const low = Number(this.layer.state.densityLow) || 0;
        const high = this.layer.state.densityHigh === undefined
            ? 1 : Number(this.layer.state.densityHigh);
        this.ramp.render({
            min: 0,
            max: 1,
            low,
            high,
            palette: this.layer.state.colormap,
            // The four ramps the SERVER can draw (colormaps.py), plus the
            // per-gene composite -- which is a different question about the
            // same picture, has no ramp of its own, and paints its swatch
            // from the genes that are on. Core's "custom" two-colour entry
            // is left out: the density tile is drawn server side, and a
            // ramp it has no name for is one it cannot produce.
            palettes: [...Object.keys(TranscriptLayer.RAMPS),
                       TranscriptLayer.GENE_COLOURS],
            labels: TranscriptLayer.RAMP_LABELS,
            auto: low === 0 && high === 1,
            format: (fraction) => (ceiling
                ? TranscriptsSidebarController.countLabel(fraction * ceiling)
                : `${Math.round(fraction * 100)}%`),
            swatch: (name) => (name === TranscriptLayer.GENE_COLOURS
                ? this.geneSwatch() : null),
            // A heat map with no legend is a picture of WHERE something is
            // and not of how much.
            caption: ceiling ? "molecules per bin" : "",
        });
    }

    /**
     * The per-gene composite's swatch: the colours that are actually on.
     *
     * Hard stops and not a blend, because the picture is not a blend -- each
     * gene is drawn in its own colour and they add where they overlap. With
     * nothing selected it is the one colour a density raster is drawn in
     * when no gene has been picked.
     */
    geneSwatch() {
        const drawn = this.layer.drawnGenes();
        if (!drawn.length) return TranscriptLayer.DENSITY_COLOR;
        const step = 100 / drawn.length;
        const stops = drawn.map((gene, index) => {
            const colour = this.layer.colorFor(gene);
            return `${colour} ${(index * step).toFixed(2)}%,`
                + ` ${colour} ${((index + 1) * step).toFixed(2)}%`;
        });
        return `linear-gradient(to right, ${stops.join(", ")})`;
    }

    paintViewControl() {
        for (const button of document.querySelectorAll("#transcripts_view_control [data-view-as]")) {
            const on = button.getAttribute("data-view-as") === this.layer.state.viewAs;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", String(on));
        }
    }

    /**
     * The gene list's actions -- make groups, reset the colours and icons,
     * empty the list -- on the button beside the search box. Core's menu
     * (PlexoraGeneTree.openListMenu), in the order it gives and for the
     * reason it gives: the first thing under a plus sign builds, and the
     * two that undo work sit under a rule.
     */
    openMenu(anchor) {
        this.ensureTree()?.openListMenu(anchor, {
            onCreateGroups: () => this.openGroupModal(),
            resetLabel: "Reset all icons and colours to default",
        });
    }

    /**
     * Name a group, or bring a file that already has the groups in it.
     *
     * Core's dialog (views/geneGroupModal.js); the file is read by this
     * plugin's own route, against this layer's panel.
     */
    openGroupModal() {
        if (!this.layer || typeof PlexoraGeneGroupModal === "undefined") return;
        const layerId = this.layer.layerId;
        PlexoraGeneGroupModal.open({
            parse: (chosen) => this.api.parseGroups(layerId, chosen),
            genes: this.layer.genes(),
            existing: this.layer.state.groups.map((group) => group.name),
            onApply: (groups) => this.addGroups(groups),
        });
    }

    /** A batch of groups into the tree, each gene selected by its group. */
    addGroups(groups) {
        this.ensureTree()?.addGroups(groups);
    }

    // -- visibility and state -------------------------------------------------

    /**
     * The Layers card, reflected back into what is drawn.
     *
     * The eye, the opacity slider and the drag on that card all land on the
     * stack record, and this is the one place they arrive. Visibility and
     * opacity are read separately because they are separately wrong to skip:
     * an early return on either would make the other silently dead, which is
     * exactly how a card ends up with controls that move nothing.
     */
    syncVisibility() {
        if (!this.layer) return;
        const record = this.ctx.layers?.get?.(this.layer.layerId);
        if (!record) return;
        if (record.visible !== this.layer.visible) {
            this.layer.setVisible(record.visible);
        }
        if (Number.isFinite(record.opacity)
                && record.opacity !== this.layer.state.opacity) {
            this.layer.setOpacity(record.opacity);
            this.paintControls();
            this.save();
        }
    }

    onHide() {
        this.hoverGenes([]);
        this.layer?.renderer?.setVisible(false);
    }

    onShow() { this.layer?.setVisible(this.layer.visible); }

    /** Write the panel's state back, at most once a second. */
    save() {
        if (!this.layer) return;
        if (this.saveTimer) window.clearTimeout(this.saveTimer);
        this.saveTimer = window.setTimeout(() => {
            this.saveTimer = null;
            this.api.putState(this.layer.state).catch(() => {});
        }, 800);
    }

    destroy() {
        if (this.poll) clearInterval(this.poll);
        this.poll = null;
        if (this.saveTimer) window.clearTimeout(this.saveTimer);
        this.saveTimer = null;
        this.tree?.destroyPickers();
        this.tree = null;
        this.layer?.destroy();
        this.layer = null;
    }
}

if (typeof window !== "undefined") {
    window.TranscriptsSidebarController = TranscriptsSidebarController;
    window.Plexora?.registerPlugin?.({
        name: "transcripts",
        createSidebarController: (ctx) => new TranscriptsSidebarController(ctx),
    });
}
if (typeof globalThis !== "undefined") {
    globalThis.TranscriptsSidebarController = TranscriptsSidebarController;
}
