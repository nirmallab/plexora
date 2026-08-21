/**
 * @class ViewerControls - wires the core, module-independent sidebar controls:
 * how cells are drawn, and HD tiles. These render whenever the viewer has
 * channels (see index.html's image_kind != 'rgb' gate) regardless of which tool,
 * if any, is currently open -- so their listeners live here instead of inside a
 * specific tool module (previously csvGatingList.js, which meant they silently
 * did nothing unless Thresholding happened to be the active tool on this page
 * view).
 *
 * **Cells is one choice, not several switches.** None / Centroids / Outlines /
 * Filled, exactly one active. This replaced two independent checkboxes, which
 * could express states the renderer had to arbitrate ("Outlines" and
 * "Centroids" both on) and could not express one that matters (Filled). The
 * boundary it draws is the important part: geometry is core's, so a plugin that
 * colours cells supplies a colour per cell id and gets all three
 * representations, rather than shipping a centroid renderer, an outline
 * renderer and a mask renderer of its own.
 *
 * Nothing is drawn over the image on load. A cell layer costs a manifest fetch,
 * a mask pyramid read and a full repaint, and a user who opened a project to
 * look at the image wanted the image. It is turned on by `enableCellLayer()`
 * when a plugin that colours cells activates, using the layer the project
 * recorded (server/models/project.py's CELL_LAYERS).
 *
 * This used to default Outlines on whenever a mask existed, and fall back to
 * centroids when it did not. That produced the worst version of both: a project
 * whose mask pyramid was still being built in the background reported no
 * segmentation yet, so it silently opened on centroids -- for a slide the user
 * had just supplied a mask for -- and the mask only appeared minutes later, if
 * they thought to toggle it.
 */
class ViewerControls {

    //: Every representation the control offers, in the order it shows them.
    static MODES = ["none", "centroids", "outlines", "filled"];

    /**
     * @constructor
     * @param seaDragonViewer - the ImageViewer instance
     * @param config - the configuration file (json)
     * @param eventHandler - the event handler for distributing interface and data updates
     */
    constructor(seaDragonViewer, config, eventHandler) {
        this.seaDragonViewer = seaDragonViewer;
        this.config = config;
        this.eventHandler = eventHandler;
        this.mode = "none";
        this.control = null;
        this.buttons = new Map();
        //: The point-size row, shown only while centroids are the drawing. It
        //: is meaningless for the other three, and a control that is present
        //: but inert reads as broken rather than as not applicable.
        this.pointSizeRow = null;
    }

    /**
     * @function currentFilter - ranges the plugin holding the cell layer wants
     * drawn, or {} when no plugin holds it (a plain viewer, or one whose tools
     * are all closed). Core asks the viewer who the owner is rather than
     * reading a named plugin off window, so this works for any plugin.
     */
    currentFilter() {
        return window.__plexora?.seaDragonViewer?.cellLayer?.getColorCodedRanges?.() || {};
    }

    /**
     * @function init - binds the Cells control and the HD checkbox. No-ops on
     * datasources that don't render them at all (e.g. RGB quick-view, where
     * index.html never emits the controls).
     */
    init() {
        const control = document.querySelector('#cell_display_control');
        const hdEl = document.querySelector('#viewer_controls_hd');
        if (!control || !hdEl) return;

        this.control = control;
        control.querySelectorAll('[data-cell-mode]').forEach((button) => {
            this.buttons.set(button.dataset.cellMode, button);
        });

        // config.segmentation is the authoritative signal -- it's set only when
        // a segmentation file was actually registered. imageData[0].src is NOT
        // a reliable proxy: it's the label/"Area" channel only when segmentation
        // exists, otherwise it's just the first real image channel (always has
        // a real src), which used to make this evaluate true even with no
        // segmentation at all.
        //
        // Recorded here rather than acted on: the viewer needs to know there is
        // no mask to fetch, whether or not anything asks to draw one.
        if (!this.hasSegmentation()) {
            this.seaDragonViewer.noLabel = true;
        }

        this.refreshAvailability();

        // Delegated, so the disabled check happens in one place and a button
        // added later needs no wiring of its own.
        control.addEventListener('click', (event) => {
            const button = event.target.closest?.('[data-cell-mode]');
            if (!button || button.disabled || !control.contains(button)) return;
            // A click is a decision, so it outranks whatever the automatic
            // fallback chose -- main.js's adoptSegmentation reads this to know
            // whether a mask arriving later may take the drawing over.
            this.seaDragonViewer.centroidsFromFallback = false;
            this.selectMode(button.dataset.cellMode);
        });

        // Left/right arrows across the group, which is what a radiogroup is
        // expected to do and the only way to reach it without a pointer.
        control.addEventListener('keydown', (event) => {
            const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1
                : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1
                : 0;
            if (!step) return;
            const enabled = ViewerControls.MODES
                .filter((mode) => !this.buttons.get(mode)?.disabled);
            const index = enabled.indexOf(this.mode);
            const next = enabled[(index + step + enabled.length) % enabled.length];
            if (!next) return;
            event.preventDefault();
            this.seaDragonViewer.centroidsFromFallback = false;
            this.buttons.get(next)?.focus();
            this.selectMode(next);
        });

        // Toggle HD (full-precision 16-bit) tile quality
        hdEl.addEventListener('change', e => {
            this.seaDragonViewer.viewerManagerVMain.setHdMode(e.target.checked);
        });

        this.bindPointSize();
    }

    /**
     * @function bindPointSize - the centroid dot size slider.
     *
     * Geometry is core's, so this is core's: every plugin that colours cells
     * gets it, and none of them ships a centroid renderer to put it on. Fires
     * on `input` rather than `change` because it is a redraw of what is already
     * in view -- there is nothing to defer to the end of the drag.
     */
    bindPointSize() {
        const slider = document.querySelector('#cell_point_size');
        this.pointSizeRow = document.querySelector('#cell_point_size_row');
        if (!slider) return;
        slider.value = String(this.seaDragonViewer.centroidPointScale ?? 1);
        slider.addEventListener('input', (event) => {
            this.seaDragonViewer.setCentroidPointScale?.(Number(event.target.value));
        });
        this.paintPointSize();
    }

    /** Show the size slider exactly when there are points to size. */
    paintPointSize() {
        if (this.pointSizeRow) this.pointSizeRow.hidden = this.mode !== "centroids";
    }

    /**
     * @function selectMode - draw cells the given way.
     *
     * The one path that changes what the cell layer shows. Both halves of the
     * old pair of handlers live here because they are not independent: turning
     * outlines on means turning centroids off, and the two checkboxes could
     * only express that by each undoing the other after the fact.
     *
     * @param mode - "none" | "centroids" | "outlines" | "filled"
     */
    async selectMode(mode) {
        const next = ViewerControls.MODES.includes(mode) ? mode : "none";
        if (next === this.mode) return;
        const button = this.buttons.get(next);
        if (button?.disabled) return;

        const previous = this.mode;
        this.paint(next);
        // Told before the work below, so the label tiles that ensureSegmentationReady
        // renders on their way in are already rendered the right way -- otherwise
        // switching to Filled would draw outlines once and re-render them.
        this.seaDragonViewer.setCellDisplayMode(next);

        const wantsMask = next === "outlines" || next === "filled";
        const wantsPoints = next === "centroids";

        try {
            if (wantsMask) {
                this.seaDragonViewer.viewerManagerVMain.sel_outlines = true;
                await this.seaDragonViewer.updateCentroidVisibility(false);
                await this.seaDragonViewer.ensureSegmentationReady(true);
                await this.seaDragonViewer.updateSegmentationFilter(this.currentFilter(), true);
            } else {
                this.seaDragonViewer.viewerManagerVMain.sel_outlines = false;
                await this.seaDragonViewer.updateCentroidVisibility(wantsPoints);
                if (wantsPoints) {
                    this.seaDragonViewer.setLoading(true);
                    try {
                        this.seaDragonViewer.updateCentroidFilter(this.currentFilter(), true);
                    } finally {
                        this.seaDragonViewer.setLoading(false);
                    }
                }
                this.seaDragonViewer.viewer.forceRedraw();
            }
        } catch (error) {
            console.warn(`Unable to draw cells as "${next}".`, error);
            // Centroids are the fallback only when there are positions to draw:
            // a project with no table, or one whose coordinate roles nobody has
            // answered, would spend the fetch on a manifest with no points in it.
            if (wantsMask && this.canDrawCentroids()) {
                this.seaDragonViewer.viewerManagerVMain.sel_outlines = false;
                await this.seaDragonViewer.updateCentroidFallback(true);
            } else {
                this.paint(previous === next ? "none" : previous);
                this.seaDragonViewer.setCellDisplayMode(this.mode);
            }
        }

        this.eventHandler.trigger("SELECTION_CHANGED", this.currentFilter());
        this.announce();
    }

    /**
     * @function adoptMode - reflect a mode the viewer switched to on its own.
     *
     * For the automatic paths -- a mask that failed to load, a pyramid that
     * arrived late -- where the drawing has already changed and the control has
     * to agree with it. Deliberately does none of the loading work: the caller
     * has done it, and re-entering selectMode from inside one of those paths
     * would recurse.
     */
    adoptMode(mode) {
        if (!ViewerControls.MODES.includes(mode) || mode === this.mode) return;
        this.paint(mode);
        this.seaDragonViewer.setCellDisplayMode(mode);
        this.announce();
    }

    /** Move the selection. Visual only -- see selectMode for the work. */
    paint(mode) {
        this.mode = mode;
        this.buttons.forEach((button, name) => {
            const active = name === mode;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-checked", active ? "true" : "false");
        });
        this.paintPointSize();
    }

    /**
     * Tell the rest of the page. The two legacy events are still fired because
     * they are the cross-file, non-module notification other views already
     * listen for (navbarControls.js, and any plugin that hooked them); the mode
     * event is what a listener that understands all four should use.
     */
    announce() {
        const detail = { mode: this.mode, available: this.availability() };
        window.dispatchEvent(new CustomEvent("plexora:cell-mode-changed", { detail }));
        window.dispatchEvent(new CustomEvent("plexora:outlines-changed", {
            detail: { enabled: this.mode === "outlines" || this.mode === "filled" },
        }));
        window.dispatchEvent(new CustomEvent("plexora:centroids-changed", {
            detail: { enabled: this.mode === "centroids" },
        }));
    }

    /**
     * @function hasSegmentation - whether a mask pyramid exists and can be
     * drawn right now. False while the background conversion job is still
     * running, which is a state the viewer sees often: import starts the job
     * and opens the project without waiting for it.
     */
    hasSegmentation() {
        return Boolean(this.config?.segmentation);
    }

    canDrawOutlines() {
        return this.hasSegmentation() && !this.seaDragonViewer.noLabel;
    }

    /**
     * Filled needs the labels stored whole. A pyramid written already reduced
     * to boundaries (`segmentationMode` "outlines") has no interior pixels, so
     * there is literally nothing to fill -- offering it would be a button that
     * changes nothing.
     */
    canDrawFilled() {
        return this.canDrawOutlines() && this.config?.segmentationMode === "filled";
    }

    canDrawCentroids() {
        return PlexoraDataset.hasCentroids(this.config);
    }

    availability() {
        return {
            none: true,
            centroids: this.canDrawCentroids(),
            outlines: this.canDrawOutlines(),
            filled: this.canDrawFilled(),
        };
    }

    /**
     * @function refreshAvailability - enable what this project can draw.
     *
     * Called at init and again whenever the answer changes -- which it does
     * exactly once, when a background mask conversion lands (main.js's
     * adoptSegmentation). Every option ships disabled from the template, so a
     * Filled button is never briefly clickable on a project that cannot fill.
     */
    refreshAvailability() {
        if (!this.control) return;
        const available = this.availability();
        const reasons = {
            centroids: "Needs cell coordinates",
            outlines: "Needs a segmentation mask",
            filled: this.canDrawOutlines()
                ? "This mask is stored as outlines, so there is nothing to fill"
                : "Needs a segmentation mask",
        };
        this.buttons.forEach((button, name) => {
            const usable = Boolean(available[name]);
            button.disabled = !usable;
            if (usable) {
                button.removeAttribute("title");
            } else if (reasons[name]) {
                button.title = reasons[name];
            }
        });
    }

    /**
     * @function enableCellLayer - turn on the layer this project draws cells
     * with, for a plugin that colours them.
     *
     * Two separate questions, deliberately kept apart:
     *
     * WHICH LAYER -- mask or points -- is the project's, recorded on it
     * (server/models/project.py's CELL_LAYERS). A plugin does not get to
     * overrule that, or the answer would change depending on which tool was
     * opened first.
     *
     * HOW TO DRAW THE MASK -- filled or outlines -- is the plugin's, because it
     * depends on what the plugin is showing. A tool that colours every cell by
     * a phenotype wants filled; one that marks a few cells wants outlines over
     * visible tissue.
     *
     * @param preference - "filled" | "outlines" | "centroids" | null, from the
     *   plugin's `preferredCellMode`. "segmentation" is accepted as an older
     *   spelling of "outlines". Anything the project or the mask cannot
     *   actually do falls back rather than failing.
     *
     * The recorded layer is a preference, not a promise: a mask whose pyramid
     * is still being built cannot be drawn yet, so this falls back to centroids
     * for now and leaves the stored choice alone. main.js polls the conversion
     * job and swaps the drawing over when it lands.
     *
     * Does nothing if something is already showing -- the user's own choice
     * outranks a tool's opinion, and opening a second tool must not undo it.
     */
    async enableCellLayer(preference) {
        if (!this.control || this.mode !== "none") return;

        const wanted = preference === 'centroids' ? 'centroids'
            : this.config?.cellLayer || null;
        const canOutline = this.canDrawOutlines();
        const canCentroid = this.canDrawCentroids();
        const onMask = this.maskMode(preference);

        // Segmentation first whenever it is wanted and ready: it shows the real
        // cell shape, and a user who supplied a mask supplied it to be used.
        const target = (wanted !== 'centroids' && canOutline) ? onMask
            : canCentroid ? "centroids"
            : canOutline ? onMask
            : null;
        if (!target) return;

        await this.selectMode(target);

        // Set after the switch, which clears it: selectMode treats a click as a
        // user decision, and this one was not. Centroids reached here despite a
        // mask being wanted means the pyramid is still building, so main.js may
        // swap to outlines once it lands.
        if (this.mode === "centroids" && wanted !== 'centroids' && !canOutline) {
            this.seaDragonViewer.centroidsFromFallback = true;
        }
    }

    /**
     * How to draw the mask for a plugin that asked for `preference`.
     *
     * Filled only when the plugin wants it AND this mask can do it -- a pyramid
     * written already reduced to boundaries has no interior to fill, so asking
     * for filled there has to land on outlines rather than on a mode the
     * control itself has disabled.
     */
    maskMode(preference) {
        return preference === 'filled' && this.canDrawFilled() ? "filled" : "outlines";
    }

    /**
     * @function ownerMaskPreference - how the plugin currently holding the cell
     * layer would like the mask drawn, or null when nothing holds it.
     *
     * Asked of the viewer rather than of a named plugin, the same way
     * currentFilter() is -- core never learns which plugins exist. main.js puts
     * the preference on the provider when the layer is claimed.
     *
     * This exists for the paths that turn the mask on WITHOUT a plugin
     * activating: a pyramid that finishes converting minutes into a session is
     * the one that matters. That used to hardcode outlines, so a project whose
     * mask was attached from the edit page came up as outlines for the rest of
     * the session however the active tool wanted to draw it.
     */
    ownerMaskPreference() {
        return this.seaDragonViewer?.cellLayer?.preferredCellMode || null;
    }
}
