/**
 * The Visium HD bin layer's controls.
 *
 * A LAYER section, with the transcripts panel's lifecycle for the same
 * reasons: mounted on page load for any sample with a `visium_bins` layer,
 * never closed, never stood down by toolLoader when a tool opens. Its markup
 * is the BODY of the layer's card (core moves it there -- see
 * views/layerManager.js), so the eye and the drag on that card land on the
 * stack record and arrive back here through `syncVisibility`.
 *
 * THE GENE LIST IS THE TRANSCRIPTS PANEL'S -- the same search, the same tree
 * of rows and groups, the same list menu and group dialog -- through core
 * (views/geneList.js, views/geneGroupModal.js), because that plugin's own
 * globals exist only on a page where its section mounted and a Visium HD
 * sample has no transcript layer. What is this panel's own is what a bin
 * layer draws: the mode, the bin size, the scale, the colour map, how a
 * heatmap combines several genes (`aggregationButton`), and how the
 * composition combines each group (`groupAggregationButton`, on the group's
 * heading in the tree).
 *
 * Its state is saved PER PROJECT, because a gene selection in chosen colours
 * at a chosen bin size is an analysis decision, and an empty panel the next
 * morning would read as lost work.
 */
class VisiumHdSidebarController {

    constructor(ctx) {
        this.ctx = ctx;
        this.api = new VisiumHdApi(ctx);
        this.layer = null;
        this.select = null;
        this.ramp = null;
        this.opacity = null;
        this.poll = null;
        //: Core's gene tree (PlexoraGeneTree), once the layer is up.
        this.tree = null;
        this.saveTimer = null;
        this.saved = null;
        //: The gene vocabulary prepared once for the picker and the group
        //: dialog (PlexoraGeneVocabulary): lower-cased names and an
        //: abundance order, so a keystroke is one capped pass.
        this.vocabulary = null;
        //: The aggregation glyph beside the colour bar. One node, handed to
        //: the gradient control as an extra on every render.
        this.aggButton = null;
        //: Bumped per /stats request, so a slow answer for the previous
        //: selection cannot overwrite the current ceiling.
        this._statsToken = 0;
        //: The aggregated window of the styled genes, for the bar's numbers.
        this._ceiling = 0;
        this._bound = false;
    }

    el(id) { return document.getElementById(id); }

    // -- lifecycle ---------------------------------------------------------

    /** An ARRAY, because `viewerSidebar.init` tests `saved.length`. */
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
        // Kept as well as awaited: `applyCarryState` has to wait for the
        // manifest, and a layer section is applied by a forEach that does not.
        this._started = this.start();
        await this._started;
    }

    persistIfNeeded() { /* saved on change, not on boot */ }

    setup() { /* everything binds once the manifest is in -- see start() */ }

    async start() {
        const layerId = this.firstBinLayer();
        if (!layerId) {
            this.show("empty");
            this.watchForALayer();
            return;
        }
        // A standard run's spots are the same panel over a different drawing:
        // no store to build, their values are the table's (spotLayer.js).
        const spots = layerId === this.firstSpotLayer();
        this.layer = spots ? new SpotLayer(this.ctx, layerId, this.api)
                           : new BinLayer(this.ctx, layerId, this.api);
        if (this.saved) Object.assign(this.layer.state, this.saved);
        const attached = await this.layer.attach();
        if (!attached && spots) {
            this.show("building");
            const node = this.el("vhd_building_label");
            if (node) node.textContent = "The spots could not be read from this sample's table.";
            this.el("vhd_building")?.querySelector(".fa-spin")?.classList.remove("fa-spin");
            return;
        }
        if (!attached) {
            // `missing` (never built) or `stale` (built by a store version
            // this one cannot read) -- both are a build. A null manifest is a
            // broken server, and the poll below will say so.
            this.show("building");
            await this.requestBuild(layerId);
            this.watchBuild(layerId);
            return;
        }
        this.show("content");
        this.fill();
        this.bind();
        this.ensureTree();
        this.paintTree();
        this.paintControls();
        this.ctx.layers?.onLayerChange?.(() => this.syncVisibility());
        this.ctx.onCleanup?.(() => this.destroy());
    }

    show(which) {
        for (const [key, id] of [["empty", "vhd_empty"],
                                 ["building", "vhd_building"],
                                 ["content", "vhd_content"]]) {
            const node = this.el(id);
            if (node) node.hidden = key !== which;
        }
    }

    /** By MODALITY -- what the data means -- not by kind, which a bin layer
     *  shares with transcripts and Visium spots. HD bins first; a standard
     *  run's spots otherwise. */
    firstBinLayer() {
        const layers = this.ctx.layers?.find?.({ modality: "visium_bins" }) || [];
        return layers[0]?.id || this.firstSpotLayer();
    }

    firstSpotLayer() {
        const layers = this.ctx.layers?.find?.({ modality: "visium_spots" }) || [];
        return layers[0]?.id || null;
    }

    watchForALayer() {
        this.ctx.layers?.onLayerChange?.(() => {
            if (this.layer || this.poll) return;
            if (!this.firstBinLayer()) return;
            this.start();
        });
    }

    async requestBuild(layerId) {
        try {
            await this.api.build(layerId);
        } catch (error) {
            // The poll reports what the server is really doing, which says
            // more than this request's failure would.
        }
    }

    watchBuild(layerId) {
        let misses = 0;
        const stop = () => {
            clearInterval(this.poll);
            this.poll = null;
        };
        const label = () => this.el("vhd_building_label");
        this.poll = setInterval(async () => {
            let state;
            try {
                state = await this.api.status(layerId);
            } catch (error) {
                stop();
                return;
            }
            // `pending`, `ready`, `failed` -- core's job vocabulary
            // (server/models/layer_jobs.py) -- plus `missing` when no job
            // exists and no store does either.
            if (state.status === "pending") {
                misses = 0;
                const node = label();
                if (node) {
                    const stage = state.stage_label || "Preparing bins";
                    const percent = Number.isFinite(state.progress)
                        ? ` · ${Math.round(state.progress)}%` : "";
                    node.textContent = `${stage}…${percent}`;
                    node.title = state.message || "";
                }
            } else if (state.status === "ready") {
                stop();
                this.layer?.destroy();
                this.layer = null;
                await this.start();
            } else if (state.status === "failed") {
                stop();
                const node = label();
                // The install line where there is one: it is something the
                // user can act on, a traceback is not.
                if (node) {
                    node.textContent = state.install
                        ? `${state.error} — ${state.install}`
                        : `Bins could not be prepared: ${state.error || "unknown error"}`;
                }
            } else if (++misses >= 3) {
                // No job and no store, three times running: the build was
                // refused (no source matrix on this layer). Polling on would
                // be a spinner that never ends.
                stop();
                const node = label();
                if (node) node.textContent = "This bin layer has no matrix to build from.";
            }
        }, 1500);
    }

    // -- the gene search ----------------------------------------------------

    fill() {
        const m = this.layer.manifest || {};
        const genes = this.layer.genes();
        const meta = this.el("vhd_meta");
        if (meta && m.spot_count !== undefined) {
            const spots = Number(m.spot_count) || 0;
            meta.textContent = `${spots.toLocaleString()} spots · `
                + `${genes.length.toLocaleString()} features · `
                + `${VisiumHdSidebarController.micronLabel(this.layer.gridMicrons())} µm`;
            meta.title = `${spots.toLocaleString()} spots under tissue, `
                + `${Math.round(Number(m.total_count) || 0).toLocaleString()} UMIs`;
        } else if (meta) {
            const bins = Number(m.bin_count) || 0;
            meta.textContent = `${VisiumHdSidebarController.compact(bins)} bins · `
                + `${genes.length.toLocaleString()} genes · `
                + `${VisiumHdSidebarController.micronLabel(this.layer.gridMicrons())} µm`;
            meta.title = `${bins.toLocaleString()} non-empty squares, `
                + `${(Number(m.total_count) || 0).toLocaleString()} UMIs`;
        }

        this.vocabulary = typeof PlexoraGeneVocabulary !== "undefined"
            ? new PlexoraGeneVocabulary(genes, m.gene_counts || []) : null;
        const mount = this.el("vhd_gene_select");
        if (mount && typeof SearchableSelect !== "undefined" && !this.select) {
            // Core's combobox with core's capped search: only the best fifty
            // matches are ever rendered, of eighteen thousand.
            this.select = new SearchableSelect(mount, {
                placeholder: `Search ${VisiumHdSidebarController.compact(genes.length)} genes…`,
                emptyText: "No genes match",
                ariaLabel: "Search genes",
                match: (query) => this.vocabulary?.match(query) || [],
                describeOption: (name) => {
                    const count = this.layer?.countOf(name) || 0;
                    return count ? VisiumHdSidebarController.compact(count) : "none";
                },
                onChange: (name) => this.addGene(name),
            });
        }
    }

    addGene(name) {
        if (!name) return;
        if (this.layer.addGene(name)) this.changed("list");
        this.select?.setValue?.("");
    }

    /** Anything that changed which genes are drawn or how: the list, the
     *  bar (its numbers follow the selection) and the saved state. */
    changed(kind) {
        if (kind !== "color") this.paintTree();
        this.paintControls();
        this.save();
    }

    // -- the list ------------------------------------------------------------

    /**
     * Core's gene tree over this layer: the rows, the groups, the heading's
     * eye and fold, and the list's menu, exactly as the Transcripts panel has
     * them. The count at the end of a row is UMIs, compacted -- 18k genes
     * run from a handful to millions.
     */
    ensureTree() {
        if (this.tree) {
            this.tree.options.layer = this.layer;
            return this.tree;
        }
        if (typeof PlexoraGeneTree === "undefined") return null;
        this.tree = new PlexoraGeneTree(this.el("vhd_tree"), {
            layer: this.layer,
            count: (gene) => {
                const total = this.layer?.countOf(gene) || 0;
                return { text: VisiumHdSidebarController.compact(total),
                         title: `${total.toLocaleString()} UMIs in this sample` };
            },
            // The composition's per-group rule, on the group's own heading --
            // the one place a group's settings belong.
            groupExtras: (group) => [this.groupAggregationButton(group)],
            onChange: (kind) => this.changed(kind),
        });
        this.tree.bindListActions(this.el("vhd_all_eye"), this.el("vhd_collapse_all"));
        this.bindHover();
        const menu = this.el("vhd_list_menu");
        if (menu && !menu.dataset.bound) {
            menu.dataset.bound = "1";
            menu.addEventListener("click", (event) => {
                event.stopPropagation();
                this.tree?.openListMenu(menu, {
                    onCreateGroups: () => this.openGroupModal(),
                    resetLabel: "Reset colours to default",
                });
            });
        }
        return this.tree;
    }

    /**
     * The pointer on a gene previews it alone; on a group's box, the group
     * combined by its own rule. One delegated pair on the container, as the
     * Transcripts panel has it: the rows are rebuilt on every change.
     */
    bindHover() {
        const tree = this.el("vhd_tree");
        if (!tree || tree.dataset.hoverBound) return;
        tree.dataset.hoverBound = "1";
        tree.addEventListener("mouseover", (event) => {
            const row = event.target.closest?.("[data-gene]");
            if (row) {
                this.hoverGenes([row.getAttribute("data-gene")]);
                return;
            }
            const box = event.target.closest?.("[data-group]");
            const name = box?.getAttribute("data-group");
            const group = name && this.layer?.state.groups.find((entry) => entry.name === name);
            this.hoverGenes(group ? [...group.genes] : [],
                            group ? this.layer.groupAggregation(group.name) : null);
        });
        tree.addEventListener("mouseleave", () => this.hoverGenes([]));
    }

    /** Preview these genes, unless they already are the preview: `mouseover`
     *  fires again for every child the pointer crosses inside one row. */
    hoverGenes(names, agg = null) {
        const key = `${(names || []).join(",")}|${agg || ""}`;
        if (key === this._hovered) return;
        this._hovered = key;
        this.layer?.emphasize?.(names || [], agg);
    }

    /** One row per selected gene, under its group. Rebuilt, not patched: the
     *  layer holds all of the state, so a rebuild cannot lose any. */
    paintTree() {
        if (!this.layer) return;
        // The row under the pointer is about to be replaced, so no
        // `mouseout` will come for it and the preview would stay up.
        this.hoverGenes([]);
        this.ensureTree()?.paint();
        const counter = this.el("vhd_counter");
        if (counter) {
            counter.textContent = `${this.layer.state.selected.length}/`
                + VisiumHdSidebarController.compact(this.layer.genes().length);
        }
    }

    /**
     * Name a group, or bring a marker list that already has them -- core's
     * dialog, the file read by this plugin's `/groups` against this layer's
     * vocabulary. The dialog's gene search is the same capped one as the
     * panel's: eighteen thousand names would be eighteen thousand rows.
     */
    openGroupModal() {
        if (!this.layer || typeof PlexoraGeneGroupModal === "undefined") return;
        const layerId = this.layer.layerId;
        PlexoraGeneGroupModal.open({
            parse: (chosen) => this.api.parseGroups(layerId, chosen),
            genes: this.layer.genes(),
            match: (query) => this.vocabulary?.match(query) || [],
            existing: this.layer.state.groups.map((group) => group.name),
            taken: PlexoraGeneGroups.placements(this.layer.state),
            onApply: (groups) => this.tree?.addGroups(groups),
        });
    }

    // -- the controls ---------------------------------------------------------

    bind() {
        if (this._bound) return;
        this._bound = true;
        this.el("vhd_mode_control")?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-vhd-mode]");
            if (!button) return;
            this.layer.set({ mode: button.getAttribute("data-vhd-mode") });
            // "list": the group headings gain or lose their rule buttons.
            this.changed("list");
        });
        this.el("vhd_log_control")?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-vhd-log]");
            if (!button) return;
            this.layer.set({ log: button.getAttribute("data-vhd-log") === "1" });
            this.paintControls();
            this.save();
        });
        // Delegated: the rungs are rebuilt by `paintBins`.
        this.el("vhd_bin_control")?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-vhd-bin]");
            if (!button) return;
            this.layer.set({ binUm: Number(button.getAttribute("data-vhd-bin")) });
            this.paintControls();
            this.save();
        });

        const range = this.el("vhd_opacity");
        if (range && !this.opacity && typeof PlexoraSlider !== "undefined") {
            // Straight at the layer and the card: a blend, not a new url.
            this.opacity = new PlexoraSlider(range, {
                fieldId: "vhd_opacity_value", unit: "%", decimals: 0,
                onInput: (value) => {
                    this.layer.setOpacity(value / 100);
                    this.ctx.layers?.setOpacity?.(this.layer.layerId, value / 100);
                    this.save();
                },
            });
        }
    }

    paintControls() {
        if (!this.layer) return;
        const state = this.layer.state;
        this.paintRadios("#vhd_mode_control [data-vhd-mode]",
                         (button) => button.getAttribute("data-vhd-mode") === state.mode);
        this.paintRadios("#vhd_log_control [data-vhd-log]",
                         (button) => (button.getAttribute("data-vhd-log") === "1") === Boolean(state.log));
        this.paintBins();
        this.opacity?.set(Math.round(state.opacity * 100), { silent: true });
        // The Scale row and the colour map describe a ramp. The composition
        // has neither -- its legend is the tree's own swatches -- so they go
        // rather than sit there doing nothing.
        const heat = this.layer.usesRamp();
        for (const id of ["vhd_scale_row", "vhd_ramp_block"]) {
            const node = this.el(id);
            if (node) node.hidden = !heat;
        }
        this.paintWarning();
        this.paintRamp();
        this.refreshCeiling();
    }

    /** One compact line once a composition has more parts than a glyph can
     *  show legibly. Not a limit: the picture is still drawn. */
    paintWarning() {
        const row = this.el("vhd_comp_warning");
        if (!row) return;
        const over = this.layer.tooManyComponents();
        row.hidden = !over;
        if (!over) return;
        const text = this.el("vhd_comp_warning_text");
        const count = this.layer.componentCount();
        const cap = BinLayer.COMPONENT_SOFT_CAP;
        const message = `${count} parts per square: cells get small past ${cap}. Group or hide some genes.`;
        if (text) text.textContent = message;
        row.title = `Each square is a treemap of ${count} cells. Put related genes `
            + "in a group or hide some to keep the cells readable.";
    }

    paintRadios(selector, isOn) {
        for (const button of document.querySelectorAll(selector)) {
            const on = isOn(button);
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", String(on));
        }
    }

    /** The three rungs, labelled from the store's own grid. */
    paintBins() {
        const box = this.el("vhd_bin_options");
        if (!box) return;
        // Spots are one size; there is no grid to pool.
        const row = this.el("vhd_bin_row");
        const ladder = this.layer.binLadder();
        if (row) row.hidden = !ladder.length;
        const current = this.layer.state.binUm;
        box.replaceChildren();
        for (const rung of this.layer.binLadder()) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "cell-mode-option";
            button.setAttribute("role", "radio");
            button.setAttribute("data-vhd-bin", String(rung.microns));
            const on = rung.microns === current;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", String(on));
            button.textContent = `${VisiumHdSidebarController.micronLabel(rung.microns)} µm`;
            button.title = rung.pooling === 1
                ? "The raw grid, one square per spot"
                : `${rung.pooling} × ${rung.pooling} grid squares pooled`;
            box.appendChild(button);
        }
    }

    /**
     * The colour bar: core's gradient control over the window.
     *
     * The ramp is the bar and the palette button beside it chooses among the
     * four the server can draw (core's "custom" is left out -- a tile is
     * drawn from a ramp NAME). The heatmap's only: the composition has no
     * continuous scale, and its block is hidden (`paintControls`).
     */
    paintRamp() {
        const mount = this.el("vhd_ramp");
        if (!mount || !this.layer || !this.layer.usesRamp()) return;
        if (!this.ramp) {
            if (typeof PlexoraGradientRange === "undefined") return;
            this.ramp = new PlexoraGradientRange(mount, {
                onRange: (low, high) => {
                    // Auto hands back two nulls: the whole automatic window.
                    // Otherwise the bar's units -- counts, or log1p of them
                    // on the Log scale -- back to fractions of the window.
                    const toFraction = (value) => Math.min(1, Math.max(0,
                        this._rampCounts(value) / (this._rampCeiling || 1)));
                    this.layer.set({
                        dlo: low === null ? 0 : toFraction(low),
                        dhi: high === null ? 1 : toFraction(high),
                    });
                    this.paintRamp();
                    this.save();
                },
                onPalette: (palette) => {
                    this.layer.set({ ramp: palette });
                    this.paintRamp();
                    this.save();
                },
            });
        }
        // Re-pointed every time: the card can be rebuilt around this markup.
        this.ramp.container = mount;
        const state = this.layer.state;
        // THE EXTENT IN COUNTS once /stats has answered: the handles' two
        // number fields print the extent's own units and never the caller's
        // `format`, so an extent of 0..1 read "0.000 / 1.000" -- a colour bar
        // that looked like it was already normalised, whatever the scale.
        // The layer still keeps the window as fractions of the automatic
        // one (`dlo`/`dhi`), which keep their meaning when the genes, the
        // bin size or the aggregation move the ceiling.
        //
        // ON THE LOG SCALE THE BAR IS IN log1p(count), which is what the
        // colours are spaced by: the ends read 0 .. log1p(window), and a
        // handle dragged halfway is halfway in log. The Linear bar and the
        // Log bar over the same counts therefore say different numbers, as
        // they draw different pictures.
        const ceiling = this._ceiling > 0 ? this._ceiling : 0;
        const log = Boolean(state.log);
        const toBar = (count) => (log ? Math.log1p(count) : count);
        this._rampCeiling = ceiling || 1;
        this._rampCounts = (value) => (log ? Math.expm1(value) : value);
        const extent = ceiling ? toBar(ceiling) : 1;
        const labels = {};
        for (const name of Object.keys(BinLayer.RAMPS)) {
            labels[name] = (typeof PlexoraColorRamps !== "undefined"
                && PlexoraColorRamps.PALETTE_LABELS[name]) || name;
        }
        this.ramp.render({
            min: 0,
            max: extent,
            low: ceiling ? toBar(state.dlo * ceiling) : state.dlo,
            high: ceiling ? toBar(state.dhi * ceiling) : state.dhi,
            palette: state.ramp,
            palettes: Object.keys(BinLayer.RAMPS),
            labels,
            auto: state.dlo === 0 && state.dhi === 1,
            // Counts for the one aggregated field; a percentage until the
            // /stats answer arrives.
            format: (value) => (!ceiling ? `${Math.round(value * 100)}%`
                : log ? value.toFixed(2)
                    : VisiumHdSidebarController.countLabel(value)),
            // No caption on the linear bar: the bin size is two rows up and
            // the unit is the count. The log bar says it is one.
            caption: log && ceiling ? "log(1 + count)" : "",
            // Whole counts from ten up, one decimal below: a mean of 2 µm
            // squares is a fraction, and 0.000 beside it is not a count.
            decimals: log ? 2 : (ceiling >= 10 ? 0 : (ceiling ? 1 : 2)),
            extras: [this.aggregationButton()],
        });
    }

    // -- how a heatmap combines genes -------------------------------------------

    /**
     * One glyph beside the colour bar, and the four answers behind it.
     *
     * NOT A ROW OF ITS OWN. A dropdown under the bar would be a full-width
     * control for a choice that is made once and then left; the glyph costs
     * the bar twenty-odd pixels and says what it is on hover ("Combine
     * genes: Mean"). Shown only while the choice changes the picture -- a
     * heatmap of two or more drawn genes -- because one gene is its own mean,
     * sum, max and min, and a control that does nothing is one the eye has
     * to learn to skip. The menu is core's (PlexoraMenu) with the current
     * answer ticked, and closes on the pick.
     */
    aggregationButton() {
        if (!this.aggButton) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "vhd-agg-button";
            button.setAttribute("aria-haspopup", "menu");
            button.setAttribute("aria-expanded", "false");
            button.innerHTML = '<span class="fas fa-layer-group" aria-hidden="true"></span>';
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                this.openAggregationMenu(button, {
                    current: this.aggregationEntry().key,
                    onSelect: (how) => this.setAggregation(how),
                });
            });
            this.aggButton = button;
        }
        const button = this.aggButton;
        const current = this.aggregationEntry();
        const label = `Combine genes: ${current.label}`;
        button.title = label;
        button.setAttribute("aria-label", label);
        button.hidden = !this.layer?.aggregates();
        return button;
    }

    aggregationEntry(how = this.layer?.state.agg) {
        return BinLayer.AGGREGATIONS.find((entry) => entry.key === how)
            || BinLayer.AGGREGATIONS[0];
    }

    /** Core's menu of the four rules, the current one ticked. Shared by the
     *  heatmap's glyph and every group's button. */
    openAggregationMenu(anchor, { current, onSelect }) {
        if (typeof PlexoraMenu === "undefined" || !this.layer) return;
        PlexoraMenu.open(anchor, BinLayer.AGGREGATIONS.map((entry) => ({
            label: entry.label,
            title: entry.title,
            checked: entry.key === current,
            onSelect: () => onSelect(entry.key),
        })));
    }

    /**
     * A group's composition rule, as a small word on its heading.
     *
     * Only in the Composition and only for a group of two or more genes: the
     * rule decides how much of a square the group earns, and one gene is its
     * own mean, sum, max and min. The word IS the current answer ("max"), so
     * the heading says how the group is combined without a hover. Built
     * fresh on every tree paint -- the tree rebuilds its rows.
     */
    groupAggregationButton(group) {
        if (!this.layer || this.layer.state.mode !== "composite") return null;
        if ((group.genes || []).length < 2) return null;
        const entry = this.aggregationEntry(this.layer.groupAggregation(group.name));
        const button = document.createElement("button");
        button.type = "button";
        button.className = "vhd-agg-button vhd-agg-button--group";
        button.setAttribute("aria-haspopup", "menu");
        button.setAttribute("aria-expanded", "false");
        button.textContent = entry.key;
        const label = `Combine ${group.name}: ${entry.label} — ${entry.title}`;
        button.title = label;
        button.setAttribute("aria-label", label);
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openAggregationMenu(button, {
                current: entry.key,
                onSelect: (how) => this.setGroupAggregation(group.name, how),
            });
        });
        return button;
    }

    setGroupAggregation(name, how) {
        if (!this.layer?.setGroupAggregation(name, how)) return;
        this.changed("list");
    }

    setAggregation(how) {
        if (!this.layer?.setAggregation(how)) return;
        this.paintRamp();
        this.refreshCeiling();
        this.save();
    }

    /**
     * The count the heatmap saturates at, for the two numbers under the bar.
     *
     * Each styled gene's automatic window from /stats -- the same
     * `auto_window` the tiles are stretched against -- combined the way the
     * tiles combine the counts (`BinLayer.ceiling`), so the numbers are the
     * ones the colours mean under Mean, Sum, Max or Min alike.
     */
    async refreshCeiling() {
        // No /stats for the composition: it has no scale to put numbers on.
        if (!this.layer || !this.layer.usesRamp()) return;
        const token = ++this._statsToken;
        const windows = await this.layer.windows();
        if (token !== this._statsToken || !this.layer) return;
        const ceiling = this.layer.ceiling(windows);
        if (ceiling === this._ceiling) return;
        this._ceiling = ceiling;
        if (this.layer.usesRamp()) this.paintRamp();
    }

    // -- walking to a sibling sample (services/carryOver.js) --------------------

    /** How the bins are being looked at. All arrangement, no measurement: the
     *  window travels as the fractions it is stored as. */
    captureCarryState() {
        const state = this.layer?.state;
        if (!state) return null;
        return JSON.parse(JSON.stringify(state));
    }

    async applyCarryState(carried) {
        if (!carried) return { skipped: [] };
        try {
            await this._started;
        } catch (error) {
            return { skipped: ["Visium HD: this sample's bin layer did not open"] };
        }
        if (!this.layer?.manifest) {
            return { skipped: ["Visium HD: no bin layer on this sample"] };
        }
        const missing = (carried.selected || []).filter((gene) => !this.layer.has(gene));
        // Deliberately no save(): one page view's arrangement, not this
        // sample's own for good.
        Object.assign(this.layer.state, JSON.parse(JSON.stringify(carried)));
        this.layer.normalizeState();
        this.layer.setOpacity(this.layer.state.opacity);
        this.ctx.layers?.setOpacity?.(this.layer.layerId, this.layer.state.opacity);
        this.layer.restyle();
        this.ensureTree();
        this.paintTree();
        this.paintControls();
        if (!missing.length) return { skipped: [] };
        const named = missing.slice(0, 3).join(", ");
        const rest = missing.length - Math.min(3, missing.length);
        return { skipped: [`Visium HD: ${named}${rest ? ` and ${rest} more` : ""} not in this panel`] };
    }

    // -- visibility and state -----------------------------------------------------

    /**
     * The Layers card, reflected back into what is drawn: its eye, its
     * opacity, and a re-registration. Each read on its own, so an early
     * return on one cannot make another silently dead.
     */
    syncVisibility() {
        if (!this.layer) return;
        const record = this.ctx.layers?.get?.(this.layer.layerId);
        if (!record) return;
        if (record.visible !== this.layer.visible) this.layer.setVisible(record.visible);
        if (Number.isFinite(record.opacity) && record.opacity !== this.layer.state.opacity) {
            this.layer.setOpacity(record.opacity);
            this.opacity?.set(Math.round(this.layer.state.opacity * 100), { silent: true });
            this.save();
        }
        this.layer.syncTransform();
    }

    onHide() { this.layer?._tiles?.setVisible?.(false); }

    onShow() { this.layer?.show(); }

    /** Write the panel's state back, at most once every 800 ms. */
    save() {
        if (!this.layer) return;
        if (this.saveTimer) window.clearTimeout(this.saveTimer);
        this.saveTimer = window.setTimeout(() => {
            this.saveTimer = null;
            if (this.layer) this.api.putState(this.layer.state).catch(() => {});
        }, 800);
    }

    destroy() {
        this.hoverGenes([]);
        if (this.poll) clearInterval(this.poll);
        this.poll = null;
        if (this.saveTimer) window.clearTimeout(this.saveTimer);
        this.saveTimer = null;
        this.tree?.destroyPickers();
        this.tree = null;
        this.select?.destroy?.();
        this.select = null;
        this.layer?.destroy();
        this.layer = null;
    }

    // -- numbers ---------------------------------------------------------------

    /** 19,912,345 -> "19.9M". A subtitle's worth of a number. */
    static compact(value) {
        const count = Number(value) || 0;
        if (count >= 1e9) return `${(count / 1e9).toFixed(1)}B`;
        if (count >= 1e6) return `${(count / 1e6).toFixed(1)}M`;
        if (count >= 10_000) return `${Math.round(count / 1e3)}k`;
        return count.toLocaleString();
    }

    /** 2 -> "2", 2.5 -> "2.5": microns without a trailing ".0". */
    static micronLabel(value) {
        const n = Number(value) || 0;
        return String(Math.round(n * 100) / 100);
    }

    /** A count per square, for the window's two ends. */
    static countLabel(value) {
        if (!Number.isFinite(value)) return "—";
        if (value === 0) return "0";
        if (value >= 10) return Math.round(value).toLocaleString();
        return value.toFixed(1);
    }
}

if (typeof window !== "undefined") {
    window.VisiumHdSidebarController = VisiumHdSidebarController;
    window.Plexora?.registerPlugin?.({
        name: "visium_hd",
        createSidebarController: (ctx) => new VisiumHdSidebarController(ctx),
    });
}
if (typeof globalThis !== "undefined") {
    globalThis.VisiumHdSidebarController = VisiumHdSidebarController;
}
