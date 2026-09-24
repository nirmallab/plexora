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
 * Nothing from the transcripts plugin is used, although much is modelled on
 * it: that plugin's globals exist only on a page where its section mounted,
 * and a Visium HD sample has no transcript layer. What is shared is core's --
 * SearchableSelect, ColorSwatchPicker, PlexoraGradientRange, PlexoraSlider.
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
        this.pickers = [];
        this.saveTimer = null;
        this.saved = null;
        //: The gene vocabulary prepared once for the picker: lower-cased
        //: names and an abundance order. See `matchGenes`.
        this.vocabulary = null;
        //: Bumped per legend request, so a slow /stats for the previous
        //: selection cannot paint over the current one.
        this._legendToken = 0;
        //: The summed window of the styled genes, for the gradient's numbers.
        this._ceiling = 0;
        this._hover = null;
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
        this.layer = new BinLayer(this.ctx, layerId, this.api);
        if (this.saved) Object.assign(this.layer.state, this.saved);
        const attached = await this.layer.attach();
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
     *  shares with transcripts and Visium spots. */
    firstBinLayer() {
        const layers = this.ctx.layers?.find?.({ modality: "visium_bins" }) || [];
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
        if (meta) {
            const bins = Number(m.bin_count) || 0;
            meta.textContent = `${VisiumHdSidebarController.compact(bins)} bins · `
                + `${genes.length.toLocaleString()} genes · `
                + `${VisiumHdSidebarController.micronLabel(this.layer.gridMicrons())} µm`;
            meta.title = `${bins.toLocaleString()} non-empty squares, `
                + `${(Number(m.total_count) || 0).toLocaleString()} UMIs`;
        }

        this.vocabulary = VisiumHdSidebarController.prepareVocabulary(
            genes, m.gene_counts || []);
        const mount = this.el("vhd_gene_select");
        const GeneSelect = VisiumHdSidebarController.geneSelectClass();
        if (mount && GeneSelect && !this.select) {
            this.select = new GeneSelect(mount, {
                placeholder: `Search ${VisiumHdSidebarController.compact(genes.length)} genes…`,
                emptyText: "No genes match",
                ariaLabel: "Search genes",
                match: (query) => VisiumHdSidebarController.matchGenes(
                    this.vocabulary, query),
                describeOption: (name) => {
                    const count = this.layer?.countOf(name) || 0;
                    return count ? VisiumHdSidebarController.compact(count) : "none";
                },
                onChange: (name) => this.addGene(name),
            });
        }
    }

    /**
     * SearchableSelect, capped.
     *
     * Core's combobox renders EVERY option that matches, which is right for a
     * forty-marker panel and a stall for eighteen thousand genes: opening it
     * would build 18k rows. This keeps everything else about it -- the
     * keyboard, the portal, the look -- and replaces only the two places that
     * decide what is listed. Built on first use rather than at load, so the
     * file does not depend on script order and a probe can load it bare.
     */
    static geneSelectClass() {
        if (VisiumHdSidebarController._GeneSelect) return VisiumHdSidebarController._GeneSelect;
        if (typeof SearchableSelect === "undefined") return null;
        class VisiumHdGeneSelect extends SearchableSelect {
            constructor(mount, options = {}) {
                // No options handed to the base: it would copy 18k names it
                // never reads again.
                super(mount, { ...options, options: [] });
                this.match = options.match || (() => []);
            }

            filter(query) {
                this.filtered = this.match(query || "");
                this.activeIndex = this.filtered.length ? 0 : -1;
                this.renderMenu();
                this.open(false);
            }

            open(reset) {
                if (reset) {
                    this.filtered = this.match(this.field?.value || "");
                    this.activeIndex = this.filtered.length ? 0 : -1;
                    this.renderMenu();
                    this.field?.select?.();
                }
                super.open(false);
            }
        }
        VisiumHdSidebarController._GeneSelect = VisiumHdGeneSelect;
        return VisiumHdGeneSelect;
    }

    /** Names, their lower-case forms, and indices by descending count. */
    static prepareVocabulary(names, counts) {
        const lower = names.map((name) => String(name).toLowerCase());
        const order = names.map((_, index) => index)
            .sort((a, b) => (Number(counts[b]) || 0) - (Number(counts[a]) || 0));
        return { names, lower, order };
    }

    /**
     * At most `limit` genes for a query: exact, then prefix, then substring,
     * each in order of abundance.
     *
     * An empty query lists the most abundant genes, which is a better first
     * screen than the first fifty alphabetically -- those are mostly
     * `A1BG`-style names nobody is looking for. One pass over the vocabulary
     * per keystroke, stopping once the prefixes alone fill the list.
     */
    static matchGenes(vocabulary, query, limit = VisiumHdSidebarController.MATCH_LIMIT) {
        if (!vocabulary) return [];
        const { names, lower, order } = vocabulary;
        const q = String(query || "").trim().toLowerCase();
        if (!q) return order.slice(0, limit).map((index) => names[index]);
        const exact = [];
        const prefix = [];
        const inside = [];
        for (const index of order) {
            const name = lower[index];
            if (name === q) exact.push(names[index]);
            else if (name.startsWith(q)) {
                prefix.push(names[index]);
                if (prefix.length >= limit) break;
            } else if (inside.length < limit && name.includes(q)) {
                inside.push(names[index]);
            }
        }
        return [...exact, ...prefix, ...inside].slice(0, limit);
    }

    static get MATCH_LIMIT() { return 50; }

    addGene(name) {
        if (!name) return;
        if (this.layer.addGene(name)) {
            this.paintTree();
            this.paintControls();
            this.save();
        }
        this.select?.setValue?.("");
    }

    // -- the list ------------------------------------------------------------

    /** One row per selected gene. Rebuilt, not patched: the layer holds all
     *  of the state, so a rebuild cannot lose any. */
    paintTree() {
        const tree = this.el("vhd_tree");
        if (!tree || !this.layer) return;
        const scrollTop = tree.scrollTop;
        for (const picker of this.pickers) picker.destroy?.();
        this.pickers = [];
        tree.innerHTML = "";
        for (const gene of this.layer.state.selected) tree.appendChild(this.buildRow(gene));
        tree.scrollTop = scrollTop;

        const selected = this.layer.state.selected.length;
        const counter = this.el("vhd_counter");
        if (counter) counter.textContent = String(selected);
        const none = this.el("vhd_none");
        if (none) none.hidden = selected > 0;
    }

    buildRow(gene) {
        const row = document.createElement("div");
        row.className = "vhd-gene-row";
        row.setAttribute("data-gene", gene);

        // Two glyphs and a class, never a glyph swap: FontAwesome has turned
        // both spans into svgs before anything can click them.
        const hidden = this.layer.isHidden(gene);
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = "vhd-eye";
        eye.classList.toggle("is-off", hidden);
        eye.title = hidden ? `Show ${gene}` : `Hide ${gene}`;
        eye.setAttribute("aria-label", eye.title);
        eye.innerHTML = '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>';
        eye.addEventListener("click", () => {
            this.layer.setGeneHidden(gene, !this.layer.isHidden(gene));
            this.paintTree();
            this.paintControls();
            this.save();
        });
        row.appendChild(eye);

        const swatch = document.createElement("span");
        swatch.className = "vhd-swatch-mount";
        row.appendChild(swatch);
        if (typeof ColorSwatchPicker !== "undefined") {
            this.pickers.push(new ColorSwatchPicker(swatch, {
                value: this.layer.colorFor(gene),
                title: `Colour for ${gene}`,
                onChange: (color) => {
                    this.layer.setColor(gene, color);
                    this.paintRamp();
                    this.paintLegend();
                    this.save();
                },
            }));
        }

        const name = document.createElement("span");
        name.className = "vhd-gene-name";
        name.textContent = gene;
        name.title = gene;
        row.appendChild(name);

        const total = this.layer.countOf(gene);
        const count = document.createElement("span");
        count.className = "vhd-gene-count";
        count.textContent = VisiumHdSidebarController.compact(total);
        count.title = `${total.toLocaleString()} UMIs in this sample`;
        row.appendChild(count);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "vhd-remove";
        remove.title = `Stop drawing ${gene}`;
        remove.setAttribute("aria-label", remove.title);
        remove.innerHTML = '<span class="fas fa-xmark"></span>';
        remove.addEventListener("click", () => {
            this.layer.removeGene(gene);
            this.paintTree();
            this.paintControls();
            this.save();
        });
        row.appendChild(remove);
        return row;
    }

    // -- the controls ---------------------------------------------------------

    bind() {
        if (this._bound) return;
        this._bound = true;
        this.el("vhd_mode_control")?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-vhd-mode]");
            if (!button) return;
            this.layer.set({ mode: button.getAttribute("data-vhd-mode") });
            this.paintControls();
            this.save();
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
        this.bindHover();
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
        this.paintRamp();
        this.paintLegend();
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
     * HEATMAP: the ramp is the bar and the palette button beside it chooses
     * among the four the server can draw (core's "custom" is left out -- a
     * tile is drawn from a ramp NAME). COMPOSITE: the bar is the drawn genes'
     * own colours in hard stops, and there is nothing to choose, so the
     * palette button is hidden (`.vhd-ramp.is-composite` in visium_hd.css).
     * The handles are the same `dlo`/`dhi` fractions either way.
     */
    paintRamp() {
        const mount = this.el("vhd_ramp");
        if (!mount || !this.layer) return;
        if (!this.ramp) {
            if (typeof PlexoraGradientRange === "undefined") return;
            this.ramp = new PlexoraGradientRange(mount, {
                onRange: (low, high) => {
                    // Auto hands back two nulls: the whole automatic window.
                    this.layer.set({
                        dlo: low === null ? 0 : low,
                        dhi: high === null ? 1 : high,
                    });
                    this.paintRamp();
                    this.paintLegend();
                    this.save();
                },
                onPalette: (palette) => {
                    if (palette === BinLayer.GENE_COLOURS) return;
                    this.layer.set({ ramp: palette });
                    this.paintRamp();
                    this.save();
                },
            });
        }
        // Re-pointed every time: the card can be rebuilt around this markup.
        this.ramp.container = mount;
        const heat = this.layer.usesRamp();
        mount.classList.toggle("is-composite", !heat);
        const state = this.layer.state;
        const ceiling = this._ceiling;
        const labels = {};
        for (const name of Object.keys(BinLayer.RAMPS)) {
            labels[name] = (typeof PlexoraColorRamps !== "undefined"
                && PlexoraColorRamps.PALETTE_LABELS[name]) || name;
        }
        labels[BinLayer.GENE_COLOURS] = "One colour per gene";
        this.ramp.render({
            min: 0,
            max: 1,
            low: state.dlo,
            high: state.dhi,
            palette: heat ? state.ramp : BinLayer.GENE_COLOURS,
            palettes: heat ? Object.keys(BinLayer.RAMPS) : [BinLayer.GENE_COLOURS],
            labels,
            auto: state.dlo === 0 && state.dhi === 1,
            // Counts for one summed field; a percentage of each gene's own
            // window for the composite, whose genes each have a different
            // one -- those are in the legend underneath.
            format: (fraction) => (heat && ceiling
                ? VisiumHdSidebarController.countLabel(fraction * ceiling)
                : `${Math.round(fraction * 100)}%`),
            swatch: (name) => (name === BinLayer.GENE_COLOURS ? this.geneSwatch() : null),
            caption: heat && ceiling
                ? `UMI per ${VisiumHdSidebarController.micronLabel(state.binUm)} µm`
                : "of each gene's range",
        });
    }

    /** The composite's bar: the drawn genes' colours, as hard stops. */
    geneSwatch() {
        const drawn = this.layer.drawnGenes();
        if (!drawn.length) return "#ffffff";
        const step = 100 / drawn.length;
        return `linear-gradient(to right, ${drawn.map((gene, index) => {
            const colour = this.layer.colorFor(gene);
            return `${colour} ${(index * step).toFixed(2)}%, `
                + `${colour} ${((index + 1) * step).toFixed(2)}%`;
        }).join(", ")})`;
    }

    /**
     * What the window means in counts, gene by gene, from /stats.
     *
     * The same `auto_window` the tiles are stretched against, at the pooling
     * chosen. On a zoomed-out view the server may pool coarser than that (a
     * tile pixel is bigger than the square), and then these are the numbers
     * for the size asked for, not the size drawn -- said in the caption.
     */
    async paintLegend() {
        const box = this.el("vhd_legend");
        if (!box || !this.layer) return;
        const token = ++this._legendToken;
        const windows = await this.layer.windows();
        if (token !== this._legendToken || !this.layer) return;
        const state = this.layer.state;
        const genes = this.layer.styleGenes();
        const heat = this.layer.usesRamp();
        const ceiling = genes.reduce(
            (sum, gene) => sum + (Number(windows[gene]?.window) || 0), 0);
        const moved = ceiling !== this._ceiling;
        this._ceiling = ceiling;
        if (moved && heat) this.paintRamp();

        box.replaceChildren();
        if (this.layer.allGenesHidden()) {
            box.textContent = "Every gene is hidden.";
            return;
        }
        const row = (label, colour, window, title = "") => {
            const line = document.createElement("div");
            line.className = "vhd-legend-row";
            const dot = document.createElement("span");
            dot.className = "vhd-legend-dot";
            if (colour) dot.style.background = colour;
            else dot.classList.add("is-none");
            const name = document.createElement("span");
            name.className = "vhd-legend-name";
            name.textContent = label;
            name.title = title || label;
            const range = document.createElement("span");
            range.className = "vhd-legend-range";
            range.textContent = Number.isFinite(window)
                ? `${VisiumHdSidebarController.countLabel(state.dlo * window)}`
                  + `–${VisiumHdSidebarController.countLabel(state.dhi * window)}`
                : "—";
            line.append(dot, name, range);
            box.appendChild(line);
        };
        for (const gene of genes) {
            const window = Number(windows[gene]?.window);
            const label = gene === BinLayer.TOTAL ? "All genes (UMI)" : gene;
            row(label, heat ? null : this.layer.colorFor(gene),
                Number.isFinite(window) ? window : NaN,
                gene === BinLayer.TOTAL ? "Every gene summed, per square" : "");
        }
        if (heat && genes.length > 1) row("Sum", null, ceiling, "The field the ramp reads");
        const caption = document.createElement("div");
        caption.className = "vhd-legend-caption";
        caption.textContent = `UMI per ${VisiumHdSidebarController.micronLabel(state.binUm)} µm square`
            + (state.log ? ", log scale" : "");
        box.appendChild(caption);
    }

    // -- the square under the cursor -------------------------------------------

    /**
     * A readout of the square the pointer is over, from /bin.
     *
     * Pointer -> OSD viewport -> reference pixels (the world is one reference
     * width wide, as `placementFor` lays it out) -> GRID, through the
     * inverse of the layer's own transform. Throttled, and skipped while the
     * pointer stays inside one pooled square, so moving the mouse is not a
     * request per pixel.
     */
    bindHover() {
        const viewer = this.ctx.viewer?.viewer;
        const surface = viewer?.canvas || viewer?.container;
        if (!surface || typeof OpenSeadragon === "undefined") return;
        let timer = null;
        let last = "";
        let event = null;
        const readout = () => this.el("vhd_readout");
        const clear = () => {
            last = "";
            const node = readout();
            if (node) node.hidden = true;
        };
        const sample = async () => {
            timer = null;
            if (!this.layer || !event || !this.layer.shows()) return clear();
            const width = Number(this.ctx.config?.width
                || this.ctx.viewer?.imageViewer?.config?.width) || 0;
            if (!width) return undefined;
            const box = surface.getBoundingClientRect();
            // Core's view transform: OSD's pointFromPixel ignores a flip.
            const pixel = new OpenSeadragon.Point(
                event.clientX - box.left, event.clientY - box.top);
            const point = window.PlexoraViewTransform
                ? window.PlexoraViewTransform.pointFromPixel(viewer, pixel)
                : viewer.viewport.pointFromPixel(pixel);
            const square = this.layer.gridAt(point.x * width, point.y * width);
            if (!square) return clear();
            const pooling = this.layer.pooling();
            const genes = this.layer.drawnGenes();
            const key = `${Math.floor(square.column / pooling)}_${Math.floor(square.row / pooling)}`
                + `|${pooling}|${genes.join(",")}`;
            if (key === last) return undefined;
            last = key;
            let answer = null;
            try {
                answer = await this.api.square(this.layer.layerId, square.column,
                                               square.row, genes, pooling);
            } catch (error) {
                answer = null;
            }
            if (last !== key) return undefined;
            this.paintReadout(answer);
            return undefined;
        };
        const move = (next) => {
            event = next;
            if (!timer) timer = window.setTimeout(sample, 120);
        };
        const leave = () => {
            event = null;
            if (timer) window.clearTimeout(timer);
            timer = null;
            clear();
        };
        surface.addEventListener("pointermove", move);
        surface.addEventListener("pointerleave", leave);
        this._hover = () => {
            surface.removeEventListener("pointermove", move);
            surface.removeEventListener("pointerleave", leave);
            if (timer) window.clearTimeout(timer);
        };
    }

    paintReadout(answer) {
        const node = this.el("vhd_readout");
        if (!node) return;
        if (!answer || !answer.inside) {
            node.hidden = true;
            return;
        }
        const size = VisiumHdSidebarController.micronLabel(answer.size_um);
        const parts = Object.entries(answer.counts || {})
            .filter(([name]) => name !== BinLayer.TOTAL)
            .map(([name, count]) => `${name} ${Number(count).toLocaleString()}`);
        parts.push(`${Number(answer.total || 0).toLocaleString()} UMI`);
        node.textContent = `${size} µm square · ${parts.join(" · ")}`;
        node.title = `Grid column ${answer.column}, row ${answer.row}`;
        node.hidden = false;
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
        if (this.poll) clearInterval(this.poll);
        this.poll = null;
        if (this.saveTimer) window.clearTimeout(this.saveTimer);
        this.saveTimer = null;
        this._hover?.();
        this._hover = null;
        for (const picker of this.pickers) picker.destroy?.();
        this.pickers = [];
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
