/**
 * cellExplorerSidebarController.js - the panel, and the plugin's registration.
 *
 * Orchestration only. What a colour is lives in cellExplorerColors.js, what is
 * on the wire in cellExplorerApi.js, what the user chose in
 * cellExplorerState.js, and the two mode-specific panels in their own files.
 *
 * The costs of the three kinds of change are kept apart on purpose, because
 * keeping them apart is what makes the tool usable on a real slide:
 *
 *   palette / colour / visibility / range / opacity
 *       -> rebuild the lookup table, redraw. No request. No geometry.
 *   a different column
 *       -> one request, cached afterwards. Geometry untouched.
 *   a different project
 *       -> a page load, which is the only time any of this is rebuilt.
 *
 * Registration is at the bottom because this file is loaded last (see
 * PLUGIN.scripts): by then every class it names exists.
 */
class CellExplorerSidebarController {

    /** Long enough that a run of clicks is one save, short enough that closing
     *  the tab straight after a change does not lose it. */
    static AUTOSAVE_MS = 800;

    //: Every block of the panel below the two empty states, in document order.
    //: Listed because the mask wait replaces all of them at once; render() puts
    //: each one back according to what the current column is. A block added to
    //: panel.html and forgotten here stays visible behind the wait.
    static PANEL_BLOCKS = [
        "cell_explorer_toolbar",
        "cell_explorer_roi_launch",
        "cell_explorer_override",
        "cell_explorer_status",
        "cell_explorer_legend_section",
        "cell_explorer_continuous",
    ];

    constructor(ctx) {
        this.ctx = ctx;
        this.api = new CellExplorerApi(ctx);
        this.state = new CellExplorerState();
        this.legend = null;
        //: Listens for ROI hovers and answers them with a composition summary.
        //: Null until setup(), like every other module here.
        this.roiBridge = null;
        this.continuous = null;
        this.variableSelect = null;
        this.controller = null;
        this._saveTimer = null;
        this._saving = null;
        //: A pending lookup-table rebuild, so a run of changes inside one frame
        //: costs one. See recolor().
        this._recolorFrame = null;
        //: The last reading from core's mask-conversion poll, and whether the
        //: user has chosen not to wait for it. See renderMaskWait().
        this._maskProgress = { progress: null, message: "" };
        this._maskSkipped = false;
    }

    // -- lifecycle ----------------------------------------------------------

    setup() {
        this.legend = new CellExplorerLegend(this.el("cell_explorer_legend"), {
            onColor: (label, hex) => {
                this.state.setColor(this.state.column, label, hex);
                this.recolor();
                this.scheduleSave();
            },
            onVisibility: (label, hidden) => {
                this.state.setHidden(this.state.column, label, hidden);
                this.recolor();
                this.render();
                this.scheduleSave();
            },
        });

        this.continuous = new CellExplorerContinuous(
            this.el("cell_explorer_continuous"), {
                onRange: (low, high) => {
                    this.state.setRange(this.state.column, low, high);
                    this.recolor();
                    this.render();
                    this.scheduleSave();
                },
                onPalette: (palette) => {
                    this.state.setPalette(this.state.column, palette);
                    this.recolor();
                    this.render();
                    this.scheduleSave();
                },
                onCustomColor: (end, hex) => {
                    this.state.setCustomColor(this.state.column, end, hex);
                    this.recolor();
                    this.render();
                    this.scheduleSave();
                },
                onHidden: (hidden) => {
                    this.state.setContinuousHidden(this.state.column, hidden);
                    this.recolor();
                    this.render();
                    this.scheduleSave();
                },
            });

        // The bridge from this plugin's variable to the ROI plugin's geometry.
        // Built unconditionally: with no ROI plugin loaded it simply never
        // hears an event, which is cheaper than asking whether one is there.
        this.roiBridge = new CellExplorerRoiBridge(this.ctx, this.state);

        this.bindVariablePicker();
        this.bindLegendControls();
        this.bindOverride();

        this.el("cell_explorer_close")?.addEventListener("click", () => {
            window.PlexoraToolLoader?.hideToolPanel("cell_explorer");
        });

        // Alongside, not instead of. An ROI summarised against an overlay that
        // has just been folded away answers a question about a picture the user
        // can no longer see -- so this is the one place in the app that asks for
        // two tools at once. See toolLoader's coexisting pair.
        this.el("cell_explorer_open_roi")?.addEventListener("click", () => {
            // Asking for the ROI tool is the first honest sign that regions are
            // about to be hovered, and the positions behind a summary are a
            // whole-slide fetch. Started here, it is usually finished before the
            // first region even exists.
            this.roiBridge?.warm();
            window.PlexoraToolLoader?.openToolAlongside("roi", "cell_explorer");
        });

        this.el("cell_explorer_requirements")?.addEventListener("click", async () => {
            // Core asks for what this plugin declared, centrally, so another
            // plugin needing the same thing finds it already answered. A plugin
            // must never grow its own "type a column name" box.
            const satisfied = await this.ctx.requirements?.require(
                ["segmentation", "role:x", "role:y"]);
            if (satisfied) window.location.reload();
        });

        // The mask this panel is waiting for. Core owns the poll -- one loop
        // asking the server, whatever number of plugins care about the answer --
        // so all three of these are readings, not requests.
        const onMaskProgress = (event) => {
            this._maskProgress = {
                progress: event.detail?.progress ?? null,
                message: event.detail?.message || "",
            };
            this.renderMaskWait();
        };
        const onMaskReady = () => {
            this._maskProgress = { progress: 100, message: "" };
            // Core has already turned the layer on by now (adoptSegmentation),
            // so the colours have somewhere to land -- but the table was built
            // while nothing was drawing, and applyLUT is owner-gated.
            this.recolor();
            this.render();
        };
        const onMaskFailed = (event) => {
            // Nothing is coming, so waiting is no longer honest. The panel falls
            // back to what it can draw and says what happened, in the same place
            // it would have said the mask was on its way.
            this._maskSkipped = true;
            this._maskProgress = {
                progress: null,
                message: event.detail?.error || "",
            };
            this.state.error = "The segmentation mask could not be prepared.";
            window.__plexora?.viewerControls?.fallBackToCentroids?.();
            this.render();
        };
        window.addEventListener("plexora:segmentation-progress", onMaskProgress);
        window.addEventListener("plexora:segmentation-ready", onMaskReady);
        window.addEventListener("plexora:segmentation-failed", onMaskFailed);
        this.ctx.onCleanup?.(() => {
            window.removeEventListener("plexora:segmentation-progress", onMaskProgress);
            window.removeEventListener("plexora:segmentation-ready", onMaskReady);
            window.removeEventListener("plexora:segmentation-failed", onMaskFailed);
        });

        this.el("cell_explorer_mask_wait_skip")?.addEventListener("click", async () => {
            // An explicit "I would rather see something", which is a different
            // thing from the silent substitution this wait replaced. The mask
            // still takes over when it lands -- fallBackToCentroids marks these
            // centroids as standing in for it.
            await window.__plexora?.viewerControls?.fallBackToCentroids?.();
            this._maskSkipped = true;
            this.recolor();
            this.render();
        });

        // The Cells control is core's, and the panel follows it rather than
        // duplicating it: which representation is showing is worth remembering
        // per project, and it is the user's choice wherever they made it.
        //
        // Filtered on the layer the event names. With several tools loaded, that
        // control edits ONE of them, and a mode change belonging to another
        // plugin used to be written straight into this project's saved
        // preference -- so opening a second tool and clicking Outlines quietly
        // replaced Cell Explorer's Filled for every future session.
        const onMode = (event) => {
            if (event.detail?.layer !== "cell_explorer") return;
            const mode = event.detail?.mode;
            if (!mode || mode === this.state.settings.display.mode) return;
            this.state.setMode(mode);
            this.scheduleSave();
        };
        window.addEventListener("plexora:cell-mode-changed", onMode);

        // Same arrangement for opacity, which used to be a slider in this panel.
        // Core owns the control; this plugin owns remembering where the user put
        // it for THIS project.
        const onOpacity = (event) => {
            if (event.detail?.layer !== "cell_explorer") return;
            const value = Number(event.detail?.value);
            if (!Number.isFinite(value)) return;
            this.state.setOpacity(value);
            this.scheduleSave();
        };
        window.addEventListener("plexora:cell-layer-opacity-changed", onOpacity);

        this.ctx.onCleanup?.(() => {
            window.removeEventListener("plexora:cell-mode-changed", onMode);
            window.removeEventListener("plexora:cell-layer-opacity-changed", onOpacity);
        });
        this.ctx.onCleanup?.(() => this.destroy());
    }

    /** ViewerSidebar's restore hook, awaited alongside every other module's. */
    async fetchSaved() {
        try {
            const [stored, catalogue] = await Promise.all([
                this.api.state(), this.api.variables(),
            ]);
            this.state.adopt(stored);
            this.state.descriptors = catalogue.variables || [];
            this.state.canDraw = catalogue.can_draw || this.state.canDraw;
        } catch (error) {
            console.error("Cell Explorer: could not load", error);
            this.state.error = "Could not load this project's metadata.";
        }
        return null;
    }

    async applyOrDefault() {
        this.restoreOpacity();
        this.restoreMode();
        const column = this.state.chooseColumn(this.ctx.dataset?.schema?.celltype);
        this.render();
        if (column) await this.select(column, { persist: false });
    }

    /**
     * Called by toolLoader when this panel becomes the selected one.
     *
     * The colours are re-applied rather than merely redrawn because they are
     * cheap to rebuild and this is the one moment the panel and the picture are
     * guaranteed to agree. The layer itself survived being switched away from --
     * it kept its table and only stopped drawing -- so this costs a table
     * rebuild and no request at all.
     */
    onShow() {
        this.restoreOpacity();
        this.restoreMode();
        this.recolor();
        this.render();
    }

    /** Called when another tool is opened over this one, or this one is closed.
     *
     *  Nothing to stand down -- this panel owns no viewer handlers and no
     *  document shortcuts. Core takes the cell layer away by itself. What is
     *  worth doing is writing out anything the debounce has not got to yet. */
    onHide() {
        this.flushSave();
    }

    destroy() {
        if (this._saveTimer) clearTimeout(this._saveTimer);
        this._saveTimer = null;
        // A frame scheduled from a change made on the way out would run against
        // a layer this plugin no longer owns.
        if (this._recolorFrame) cancelAnimationFrame(this._recolorFrame);
        this._recolorFrame = null;
        this.controller?.abort();
        this.controller = null;
        // Each of these parks an element on <body> that outlives this panel's
        // markup, so they are handed back explicitly rather than left to go
        // with the DOM the panel was rendered into. The ROI bridge holds two
        // window listeners on top of its card.
        this.legend?.destroy();
        this.variableSelect?.destroy();
        this.variableSelect = null;
        this.roiBridge?.destroy();
        this.roiBridge = null;
    }

    // -- selection ----------------------------------------------------------

    /**
     * Show a column.
     *
     * The generation is what makes rapid switching safe. Two selections in
     * flight can finish in either order, and the older one finishing last would
     * paint the previous variable's colours under the current variable's
     * legend. The outgoing request is aborted too, but an abort is best-effort
     * -- a response already on the wire still resolves, so the check on arrival
     * is the one that decides.
     */
    async select(column, { persist = true } = {}) {
        const generation = this.state.nextGeneration();
        this.controller?.abort();
        this.controller = new AbortController();

        this.state.column = column;
        this.state.error = null;
        // The overlay goes rather than lingering at reduced opacity: a stale
        // picture that is still recognisable is worse than none, because there
        // is nothing to say it is stale.
        this.state.data = null;
        this.state.status = "loading";
        this.applyLUT(null);
        // Same reasoning one step earlier: with no values loaded there is
        // nothing to summarise a region by, so an open card goes with the
        // overlay rather than sitting there full of the old column's counts.
        this.roiBridge?.refreshOpenCard();
        this.render();

        if (persist) {
            this.state.dirty = true;
            this.scheduleSave();
        }
        if (!column) {
            this.state.status = "idle";
            this.render();
            return;
        }

        try {
            const data = await this.api.values(
                column, this.state.requestedKind(column), this.controller.signal);
            if (!this.state.isCurrent(generation)) return;
            this.state.data = data;
            this.state.status = "ready";
            this.recolor();
            // A region summary is a statement about THIS variable, so a card
            // left open across a switch has to be re-tallied rather than left
            // describing the column before it. Membership is untouched: the
            // shapes have not moved.
            this.roiBridge?.refreshOpenCard();
        } catch (error) {
            if (error.name === "AbortError" || !this.state.isCurrent(generation)) return;
            console.error(`Cell Explorer: could not load "${column}"`, error);
            this.state.status = "error";
            this.state.error = `Could not load "${column}".`;
        }
        this.render();
    }

    /**
     * Rebuild the lookup table and hand it to core. No request, no geometry --
     * and at most once per frame.
     *
     * The frame is what makes dragging a colour bearable. A native colour input
     * fires `input` continuously while the pointer moves around the OS picker,
     * tens of times a second, and each one of those would otherwise rebuild a
     * table with an entry per cell id and re-render every label tile on screen.
     * On a slide with a few hundred thousand cells the drag turns into a
     * slideshow that keeps painting long after the pointer has stopped.
     *
     * Coalescing costs nothing anywhere else: every caller here is a display
     * change whose result is a repaint, and a repaint cannot show more than one
     * state per frame however many times it is asked to.
     */
    recolor() {
        if (this._recolorFrame) return;
        this._recolorFrame = requestAnimationFrame(() => {
            this._recolorFrame = null;
            this.applyLUT(this.state.buildLUT());
            // Every hide, show, All/None and colour change funnels through here,
            // which makes it the one place an open region card can be kept
            // truthful: it reports the visible categories only, in their current
            // colours, so anything that changes either has to reach it.
            this.roiBridge?.refreshOpenCard();
        });
    }

    applyLUT(lut) {
        // A table handed over directly supersedes a pending rebuild. select()
        // clears the overlay this way, and a frame scheduled before it that
        // landed afterwards would paint the previous column's colours back on.
        if (this._recolorFrame) {
            cancelAnimationFrame(this._recolorFrame);
            this._recolorFrame = null;
        }
        const viewer = this.ctx.viewer;
        if (!viewer?.setCellColorLUT) return;
        // Owner-gated on the other side. Being turned away is the correct
        // outcome when this tool is not the visible one -- see onShow, which is
        // where it is applied again.
        viewer.setCellColorLUT("cell_explorer", lut);
    }

    /**
     * Put the Cells control back where this project left it.
     *
     * Only when the user has not chosen a mode for THIS layer in this session.
     * Core turns a layer on when a colouring plugin activates
     * (viewerControls.enableCellLayer), and whatever the user has since chosen
     * outranks a stored preference -- so this fills in a gap rather than
     * overruling a decision.
     *
     * The question used to be asked of the control as a whole ("is anything
     * showing?"), which stopped being the right one the moment two plugins could
     * draw at once: another tool having turned the mask on is not a decision
     * about this layer, and it silently suppressed the restore every time.
     */
    restoreMode() {
        const controls = window.__plexora?.viewerControls;
        const layer = this.ctx.viewer?.getCellLayer?.("cell_explorer");
        const mode = this.state.settings.display.mode;
        if (!controls || !mode || mode === "none") return;
        if (layer ? layer.userMode : controls.mode !== "none") return;
        if (!controls.offeredModes()[mode]) return;
        controls.selectMode(mode);
    }

    /**
     * Put the shared opacity slider back where this project left it.
     *
     * Applied to this plugin's own layer by name, so it lands on the right one
     * whether or not this tool is the selected one at the time.
     */
    restoreOpacity() {
        const value = this.state.settings.display.opacity;
        if (!Number.isFinite(value)) return;
        this.ctx.viewer?.setLayerOpacity?.("cell_explorer", value);
        window.__plexora?.viewerControls?.paintLayerOpacity?.();
    }

    // -- panel --------------------------------------------------------------

    el(id) {
        return document.getElementById(id);
    }

    /**
     * The Colour-by control: core's searchable combobox in its button shape.
     *
     * A button rather than a field, for two reasons that happen to point the
     * same way. It is sized to the column name instead of to a text input, so
     * it can share its line with the two controls that act on the legend. And
     * the search lives inside the menu it opens, where somebody looking for a
     * search box will look -- a field that doubles as the value display gives
     * no sign at all that it can be typed into.
     *
     * Every metadata column is in the list -- the server stopped deciding which
     * ones were worth offering (see variables.eligible_columns). What used to
     * be a refusal is a hint here: the dot and the hover text say a column
     * behaves like an identifier, and picking it anyway works.
     */
    bindVariablePicker() {
        const mount = this.el("cell_explorer_variable");
        if (!mount) return;
        this.variableSelect = new SearchableSelect(mount, {
            options: [],
            value: "",
            trigger: "button",
            emptyLabel: "Choose a column",
            searchPlaceholder: "Search metadata...",
            ariaLabel: "Colour by",
            emptyText: "No columns match",
            describeOption: (name) => this.describeColumn(name),
            getIndicator: (name) => this.warnAboutColumn(name),
            onChange: (name) => this.select(name || null),
        });
    }

    /** The hint shown beside a column name in the dropdown. */
    describeColumn(name) {
        const descriptor = this.state.descriptor(name);
        if (!descriptor) return "";
        if (descriptor.kind === "continuous") return "numeric";
        return `${descriptor.n_categories || 0} categories`;
    }

    /**
     * A dot, and why, for a column that will not draw well -- or "" for the
     * ordinary case, which is SearchableSelect's way of saying "no dot".
     */
    warnAboutColumn(name) {
        const descriptor = this.state.descriptor(name);
        if (!descriptor?.identifier_like) return "";
        return `${(descriptor.n_unique || 0).toLocaleString()} distinct values -- `
            + "this behaves like an identifier, and will draw about as many colours "
            + "as there are cells.";
    }

    bindLegendControls() {
        this.el("cell_explorer_search")?.addEventListener("input", (event) => {
            // Filters the legend only. Which cells are drawn does not change --
            // typing "T" must not hide the macrophages.
            this.legend.setFilter(event.target.value);
            this.renderLegend();
        });

        // All / None, as one delegated handler over the pair. Clicking the lit
        // one is a no-op rather than a toggle: these say which state the legend
        // is in, and clicking "All" while everything already shows should not
        // hide everything.
        this.el("cell_explorer_visibility")?.addEventListener("click", (event) => {
            const button = event.target.closest?.("[data-visibility]");
            if (!button || !this.state.column) return;
            const hide = button.dataset.visibility === "none";
            if (this.state.visibilityState(this.state.column) === button.dataset.visibility) return;
            this.state.setAllHidden(this.state.column, hide);
            this.recolor();
            this.render();
            this.scheduleSave();
        });
    }

    bindOverride() {
        this.el("cell_explorer_override")?.addEventListener("click", (event) => {
            const button = event.target.closest?.("[data-kind]");
            if (!button || button.disabled) return;
            const column = this.state.column;
            const descriptor = this.state.descriptor(column);
            if (!descriptor) return;
            const kind = button.dataset.kind;
            this.state.setOverride(column, kind === descriptor.kind ? null : kind);
            // No cache flush: the cache key carries the kind, so the two
            // encodings of one column are separate entries and flipping back
            // and forth is free after the first of each.
            this.select(column);
        });
    }

    render() {
        // First, because it can take the whole panel over: there is nothing
        // useful to choose a colour-by column for until the mask this project
        // draws cells on exists.
        if (this.renderMaskWait()) return;

        // The two blocks the wait may have taken away that nothing further down
        // owns. Everything else in PANEL_BLOCKS has its visibility decided
        // below, or by renderStatus/renderOverride.
        const toolbar = this.el("cell_explorer_toolbar");
        if (toolbar) toolbar.hidden = false;
        const launch = this.el("cell_explorer_roi_launch");
        if (launch) launch.hidden = false;

        this.renderVariablePicker();
        this.renderStatus();
        this.renderOverride();

        const column = this.state.column;
        const kind = column ? this.state.kindFor(column) : null;
        const legend = this.el("cell_explorer_legend_section");
        const continuous = this.el("cell_explorer_continuous");
        const ready = this.state.status === "ready" && Boolean(column);
        const categorical = ready && kind === "categorical";

        if (legend) legend.hidden = !categorical;
        if (continuous) continuous.hidden = !(ready && kind === "continuous");
        // The filter and All/None sit in the toolbar, on the colour-by control's
        // line, but they belong to the list -- so they come and go with it
        // rather than staying behind as two controls with nothing to act on.
        const categoryControls = this.el("cell_explorer_category_controls");
        if (categoryControls) categoryControls.hidden = !categorical;

        if (!ready) return;
        if (kind === "categorical") {
            this.renderLegend();
        } else {
            this.continuous.render(
                this.state.current,
                this.state.continuous(column),
                this.state.domainFor(column),
                this.state.isAutoRange(column),
            );
        }
    }

    /**
     * The whole panel, replaced by "the mask is still being made".
     *
     * Only while a mask genuinely IS on its way -- `pending` is a live getter
     * over the config core rewrites when the job lands, so this stops being true
     * on its own. Never for a project that simply has no mask: that one has
     * nothing to wait for and is handled by the "nothing to draw these on"
     * state above.
     *
     * Everything else is hidden rather than left visible behind the wait,
     * because a colour-by control that recolours a picture nobody can see is an
     * invitation to make a choice with no feedback. The panel comes back whole
     * the moment the mask lands or the user asks for centroids instead.
     *
     * @returns whether the wait took the panel over.
     */
    renderMaskWait() {
        const wait = this.el("cell_explorer_mask_wait");
        if (!wait) return false;
        const waiting = Boolean(this.ctx.dataset?.segmentation?.pending)
            && !this._maskSkipped
            && this.state.descriptors.length > 0;

        wait.hidden = !waiting;
        // Blocks named by id rather than a wrapper hidden as one: the panel is a
        // flat list, and wrapping every block inside a new element for the sake
        // of one state would change the markup everything else is written
        // against. Only the hiding is done here -- render() decides what each
        // block should be when the wait is over.
        if (waiting) {
            for (const id of CellExplorerSidebarController.PANEL_BLOCKS) {
                const block = this.el(id);
                if (block) block.hidden = true;
            }
            this.renderMaskProgress();
            return true;
        }
        return false;
    }

    renderMaskProgress() {
        const bar = this.el("cell_explorer_mask_wait_bar");
        const percent = this._maskProgress.progress;
        if (bar) {
            // No reading yet, or a job that reports none: a full-width bar at low
            // opacity says "working" without claiming a position it does not
            // know. See the .is-indeterminate rule.
            const known = Number.isFinite(percent);
            bar.classList.toggle("is-indeterminate", !known);
            bar.style.width = known ? `${Math.min(100, Math.max(0, percent))}%` : "100%";
        }
        const message = this.el("cell_explorer_mask_wait_message");
        if (message) {
            message.textContent = this._maskProgress.message
                || "Converting segmentation mask…";
        }
        // Offered only where there is something to fall back TO. On a project
        // with no coordinates the choice does not exist, and a button that
        // cannot do anything is worse than no button at all.
        const skip = this.el("cell_explorer_mask_wait_skip");
        if (skip) skip.hidden = !this.state.canDraw.centroids;
    }

    renderLegend() {
        const column = this.state.column;
        if (!column) return;
        this.legend.render(this.state.current, this.state.categorical(column));
        this.renderVisibilityState();
    }

    /** Which of All / None the legend is currently in, or neither. */
    renderVisibilityState() {
        const group = this.el("cell_explorer_visibility");
        if (!group) return;
        const state = this.state.visibilityState(this.state.column);
        group.querySelectorAll("[data-visibility]").forEach((button) => {
            const on = button.dataset.visibility === state;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", on ? "true" : "false");
        });
    }

    renderVariablePicker() {
        if (!this.variableSelect) return;
        // Every column, in the table's own order. The combobox does the
        // filtering itself, so this is set once per catalogue rather than
        // rebuilt per keystroke.
        this.variableSelect.setOptions(
            this.state.descriptors.map((descriptor) => descriptor.name));
        this.variableSelect.setValue(this.state.column || "");
    }

    renderStatus() {
        const status = this.el("cell_explorer_status");
        const empty = this.el("cell_explorer_empty");
        const nothing = this.el("cell_explorer_nowhere");
        if (empty) empty.hidden = this.state.descriptors.length > 0;

        // The state where the tool cannot work at all: cells to colour, and
        // nowhere to draw them. Distinct from having no metadata, and the fix
        // is different -- this one is a file, that one is the column screen.
        const canDraw = this.state.canDraw.segmentation || this.state.canDraw.centroids;
        if (nothing) nothing.hidden = canDraw || this.state.descriptors.length === 0;

        if (!status) return;
        const descriptor = this.state.current;
        if (this.state.error) {
            status.textContent = this.state.error;
        } else if (this.state.status === "loading") {
            status.textContent = `Loading ${this.state.column}...`;
        } else if (this.ctx.dataset?.segmentation?.pending) {
            // Reached only once the wait screen has been dismissed, or was never
            // shown: it hides this line along with the rest of the panel. So
            // whatever is drawing now is standing in for a mask still on its way,
            // and this is where the panel keeps saying so.
            status.textContent = "Preparing the segmentation mask...";
        } else if (descriptor?.notice === "high_cardinality") {
            status.textContent = `${descriptor.n_categories.toLocaleString()} categories `
                + "-- search or hide categories for a clearer view.";
        } else if (descriptor?.notice === "many_categories") {
            status.textContent = `${descriptor.n_categories} categories.`;
        } else if (this.state.readOnly) {
            status.textContent = "These settings were saved by a newer version of "
                + "Plexora, so changes here are not being saved.";
        } else {
            status.textContent = "";
        }
        status.hidden = !status.textContent;
    }

    renderOverride() {
        const section = this.el("cell_explorer_override");
        if (!section) return;
        const descriptor = this.state.current;
        // Shown only where the inference was a guess. An override on every
        // variable is noise on the ninety percent that were never in doubt.
        section.hidden = !descriptor?.ambiguous;
        if (section.hidden) return;
        const active = this.state.kindFor(this.state.column);
        section.querySelectorAll("[data-kind]").forEach((button) => {
            const on = button.dataset.kind === active;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", on ? "true" : "false");
        });
    }

    // -- persistence --------------------------------------------------------

    /**
     * Write after a pause, and only for a committed change.
     *
     * Every caller here is a decision that has landed -- a colour chosen, a
     * category hidden, a slider released. The opacity slider's own drag never
     * reaches this: core fires the event it saves from once, on release.
     */
    scheduleSave() {
        if (this.state.readOnly) return;
        this.state.dirty = true;
        if (this._saveTimer) clearTimeout(this._saveTimer);
        this._saveTimer = setTimeout(() => this.persistIfNeeded(),
            CellExplorerSidebarController.AUTOSAVE_MS);
    }

    flushSave() {
        if (this._saveTimer) clearTimeout(this._saveTimer);
        this._saveTimer = null;
        return this.persistIfNeeded();
    }

    async persistIfNeeded() {
        if (!this.state.dirty || this.state.readOnly) return;
        if (this._saving) return this._saving;
        this.state.dirty = false;

        this._saving = (async () => {
            try {
                const result = await this.api.save(
                    this.state.revision, this.state.toSettings());
                if (result.conflict) {
                    // Another tab wrote first. Theirs is taken rather than
                    // overwritten -- both sides hold a full copy, and clobbering
                    // silently reverts every change made in the other one.
                    console.info("Cell Explorer: settings changed in another session");
                    const fresh = await this.api.state();
                    this.state.adopt(fresh);
                    this.recolor();
                    this.render();
                    return;
                }
                this.state.revision = result.revision;
            } catch (error) {
                console.error("Cell Explorer: could not save settings", error);
                // Put it back, so the next committed change tries again.
                this.state.dirty = true;
            } finally {
                this._saving = null;
            }
        })();
        return this._saving;
    }
}


if (window.Plexora) {
    window.Plexora.registerPlugin({
        name: "cell_explorer",
        ownsCellLayer: true,
        /**
         * Filled, not outlines. This tool gives every cell a colour that means
         * something, and an outline shows that colour in a one-pixel ring
         * around tissue that is still the tissue's own colour -- at any zoom
         * where more than a few hundred cells are on screen, the phenotype map
         * this plugin exists to draw is simply not legible. Opacity is the
         * control for seeing the tissue through it.
         *
         * Core falls back to outlines by itself when the mask was stored
         * already reduced to boundaries, so this is a preference and not an
         * assumption about the file.
         */
        preferredCellMode: "filled",
        /**
         * The selection-provider shape core expects from whoever holds the cell
         * layer. Cell Explorer answers "nothing" to all three on purpose: that
         * interface is about GATING -- which cells to draw at all, and the
         * shader's colour-coded range table. This plugin decides what colour a
         * cell is, through setCellColorLUT, which is a different channel
         * entirely. Answering otherwise would put an empty gate on the layer and
         * hide every cell.
         */
        createInstance() {
            return {
                getSelectedIds: async () => new Set(),
                supportsColorCoding: () => false,
                getColorCodedRanges: () => ({}),
            };
        },
        createSidebarController(ctx) {
            return new CellExplorerSidebarController(ctx);
        },
    });
}
