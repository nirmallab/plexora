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
        //: The per-layer opacity row, shown only while a plugin layer is active
        //: -- there is nothing to fade a plain white cell layer against.
        this.opacityRow = null;
        this.opacitySlider = null;
    }

    /**
     * @function currentFilter - ranges the active layer's plugin wants drawn, or
     * {} when there is no active layer (a plain viewer, or one whose tools are
     * all closed). Core asks the viewer who is active rather than reading a
     * named plugin off window, so this works for any plugin.
     */
    currentFilter() {
        return window.__plexora?.seaDragonViewer?.cellLayer?.getColorCodedRanges?.() || {};
    }

    /**
     * @function activeLayer - the cell layer these controls are editing, or null
     * for a viewer with no plugin layers.
     *
     * "Active" and "visible" are different questions: other layers may well be
     * on screen. This is the one whose mode this control shows and whose opacity
     * the slider moves -- the tool the user selected in the sidebar.
     */
    activeLayer() {
        const viewer = this.seaDragonViewer;
        return viewer?.getCellLayer?.(viewer.cellLayerOwner) || null;
    }

    /** Every registered layer, bottom of the stack first. */
    layers() {
        return this.seaDragonViewer?.cellLayers?.() || [];
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
        this.bindLayerOpacity();
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
     * @function bindLayerOpacity - how strongly the ACTIVE layer sits over what
     * is under it.
     *
     * Core's, and shared, for the same reason the mode buttons are: geometry and
     * compositing belong to the viewer, so every plugin that colours cells gets
     * this without shipping a slider of its own. It used to live inside Cell
     * Explorer's panel, where a second such plugin would have had to grow a
     * duplicate -- and where it silently moved whichever layer happened to be
     * active rather than the one the panel was about.
     *
     * Two events, two costs. `input` fires per pixel of drag and only changes
     * the alpha the tile canvases composite at, which is a redraw. `change`
     * fires once on release, and is the only one a plugin needs to hear in order
     * to persist the value.
     */
    bindLayerOpacity() {
        const slider = document.querySelector('#cell_layer_opacity');
        this.opacityRow = document.querySelector('#cell_layer_opacity_row');
        if (!slider) return;
        this.opacitySlider = slider;
        slider.addEventListener('input', (event) => {
            const layer = this.activeLayer();
            if (!layer) return;
            const value = Number(event.target.value) / 100;
            this.seaDragonViewer.setLayerOpacity(layer.name, value);
            this.paintOpacityReadout(value);
        });
        slider.addEventListener('change', (event) => {
            const layer = this.activeLayer();
            if (!layer) return;
            window.dispatchEvent(new CustomEvent("plexora:cell-layer-opacity-changed", {
                detail: { layer: layer.name, value: Number(event.target.value) / 100 },
            }));
        });
        this.paintLayerOpacity();
    }

    /** Put the slider where the active layer actually is, and show it only when
     *  there is a layer for it to act on. */
    paintLayerOpacity() {
        const layer = this.activeLayer();
        if (this.opacityRow) this.opacityRow.hidden = !layer;
        if (!this.opacitySlider || !layer) return;
        this.opacitySlider.value = String(Math.round(layer.opacity * 100));
        this.paintOpacityReadout(layer.opacity);
    }

    paintOpacityReadout(value) {
        const readout = document.querySelector('#cell_layer_opacity_value');
        if (readout) readout.textContent = `${Math.round(value * 100)}%`;
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
        this.applyMode(next);

        // Not "what did the user just pick" but "what is on screen now": the
        // mask item and the point overlay are one each, shared by every layer,
        // so a second layer drawing outlines has to keep them on when the active
        // layer moves to centroids.
        const wantsMask = this.maskWanted();
        const wantsPoints = this.pointsWanted();

        try {
            this.seaDragonViewer.viewerManagerVMain.sel_outlines = wantsMask;
            await this.seaDragonViewer.updateCentroidVisibility(wantsPoints);
            if (wantsMask) {
                await this.seaDragonViewer.ensureSegmentationReady(true);
                await this.seaDragonViewer.updateSegmentationFilter(this.currentFilter(), true);
            }
            if (wantsPoints) {
                this.seaDragonViewer.setLoading(true);
                try {
                    this.seaDragonViewer.updateCentroidFilter(this.currentFilter(), true);
                } finally {
                    this.seaDragonViewer.setLoading(false);
                }
            }
            if (!wantsMask) {
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
                this.applyMode(this.mode);
            }
        }

        this.eventHandler.trigger("SELECTION_CHANGED", this.currentFilter());
        this.announce();
    }

    /**
     * Push a mode onto whatever this control is editing: the active layer, or
     * core itself when there is none.
     *
     * Choosing a mode for a layer the user had switched off is also a request to
     * see it -- otherwise the click would visibly do nothing.
     */
    applyMode(mode) {
        const layer = this.activeLayer();
        if (!layer) {
            this.seaDragonViewer.setCellDisplayMode(mode);
            return;
        }
        if (mode !== "none") {
            this.seaDragonViewer.setCellLayerVisible(layer.name, true);
        }
        this.seaDragonViewer.setCellLayerMode(layer.name, mode);
    }

    /** Whether anything on screen is drawn from the label tiles right now. */
    maskWanted() {
        const layers = this.layers();
        if (!layers.length) return this.mode === "outlines" || this.mode === "filled";
        return layers.some((layer) => layer.visible
            && (layer.mode === "outlines" || layer.mode === "filled"));
    }

    /** Whether anything on screen is drawn as points right now. */
    pointsWanted() {
        const layers = this.layers();
        if (!layers.length) return this.mode === "centroids";
        return layers.some((layer) => layer.visible && layer.mode === "centroids");
    }

    /**
     * @function refreshLayerSurfaces - bring the shared surfaces into line with
     * whatever the layers now want.
     *
     * The label item and the point overlay are one each and every layer draws
     * onto them, so neither can be switched by the layer that happened to
     * change. Called after a card's eye is toggled or a tool is removed --
     * selectMode does this work inline for the click that caused it.
     *
     * Without this, turning a layer back on while nothing else was drawing a
     * mask left the mask item unloaded: the layer was visible, its canvases were
     * built, and the eye did nothing anyone could see.
     */
    async refreshLayerSurfaces() {
        const manager = this.seaDragonViewer?.viewerManagerVMain;
        if (!this.control || !manager) return;
        const wantsMask = this.maskWanted();
        const wantsPoints = this.pointsWanted();
        // Loading the mask is the expensive half, so it is done only on the edge
        // -- turning it off and on again for a layer that was already drawing
        // one would re-read the pyramid for nothing.
        const maskArriving = wantsMask && !manager.sel_outlines;
        manager.sel_outlines = wantsMask;
        try {
            await this.seaDragonViewer.updateCentroidVisibility(wantsPoints);
            if (maskArriving) {
                await this.seaDragonViewer.ensureSegmentationReady(true);
                await this.seaDragonViewer.updateSegmentationFilter(this.currentFilter(), true);
            }
            if (wantsPoints) {
                this.seaDragonViewer.updateCentroidFilter(this.currentFilter(), true);
            }
        } catch (error) {
            console.warn("Unable to update what the cell layers draw.", error);
        }
        this.seaDragonViewer.viewer?.forceRedraw?.();
        this.announce();
    }

    /**
     * @function syncToActiveLayer - point the shared controls at whichever layer
     * is now active.
     *
     * Called when a plugin registers, when the user selects a different tool's
     * card, and when one is removed. Purely a repaint: nothing about what is
     * drawn changes here, only what the control says and offers.
     */
    syncToActiveLayer() {
        if (!this.control) return;
        const layer = this.activeLayer();
        this.refreshAvailability();
        this.paint(layer && ViewerControls.MODES.includes(layer.mode)
            ? layer.mode : this.mode);
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
        this.applyMode(mode);
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
        this.paintLayerOpacity();
    }

    /**
     * Tell the rest of the page. The two legacy events are still fired because
     * they are the cross-file, non-module notification other views already
     * listen for (navbarControls.js, and any plugin that hooked them); the mode
     * event is what a listener that understands all four should use.
     *
     * `layer` is the important addition: with several plugins loaded, a mode
     * change belongs to ONE of them, and a plugin storing the user's choice must
     * ignore the others' -- otherwise opening a second tool and clicking
     * Outlines silently rewrites the first tool's saved preference.
     */
    announce() {
        const layer = this.activeLayer();
        const detail = {
            mode: this.mode,
            available: this.availability(),
            offered: this.offeredModes(),
            layer: layer?.name || null,
            opacity: layer ? layer.opacity : null,
        };
        window.dispatchEvent(new CustomEvent("plexora:cell-mode-changed", { detail }));
        window.dispatchEvent(new CustomEvent("plexora:outlines-changed", {
            detail: { enabled: this.maskWanted() },
        }));
        window.dispatchEvent(new CustomEvent("plexora:centroids-changed", {
            detail: { enabled: this.pointsWanted() },
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
     * @function offeredModes - what the control actually shows right now.
     *
     * Two filters, in order. The PROJECT decides what can be drawn at all
     * (availability). An active layer then narrows that to what its plugin can
     * draw -- a tool that only marks a handful of cells has no use for Filled,
     * and offering it is offering a button whose result the tool did not design
     * for. A plugin that declares nothing gets everything the project can do.
     *
     * "None" is offered only while there is NO active layer. With one, the
     * card's own eye toggle is what turns a layer off, and a second control
     * meaning almost-but-not-quite the same thing is the kind of ambiguity that
     * makes both of them feel broken.
     */
    offeredModes() {
        const available = this.availability();
        const layer = this.activeLayer();
        const supported = layer?.supportedModes || null;
        const offered = {};
        ViewerControls.MODES.forEach((mode) => {
            offered[mode] = Boolean(available[mode])
                && (!supported || supported.includes(mode));
        });
        if (layer) offered.none = false;
        return offered;
    }

    /**
     * @function refreshAvailability - show and enable what can be drawn now.
     *
     * Called at init, whenever a background mask conversion lands (main.js's
     * adoptSegmentation), and whenever the active layer changes. Every option
     * ships disabled from the template, so a Filled button is never briefly
     * clickable on a project that cannot fill.
     *
     * Disabled and hidden mean different things here. A mode the PROJECT cannot
     * draw stays visible and disabled, with the reason on its tooltip -- that is
     * a fact about this dataset the user should be able to see. A mode the
     * active PLUGIN does not use is hidden outright, because "not applicable to
     * the tool you have open" has no explanation worth a tooltip and would
     * otherwise leave a row of permanently greyed buttons. The buttons stay in
     * the DOM either way: they belong to the control, not to whichever tool
     * happens to be open.
     */
    refreshAvailability() {
        if (!this.control) return;
        const available = this.availability();
        const offered = this.offeredModes();
        const reasons = {
            centroids: "Needs cell coordinates",
            outlines: "Needs a segmentation mask",
            filled: this.canDrawOutlines()
                ? "This mask is stored as outlines, so there is nothing to fill"
                : "Needs a segmentation mask",
        };
        this.buttons.forEach((button, name) => {
            const usable = Boolean(offered[name]);
            button.style.display = !usable && Boolean(available[name]) ? "none" : "";
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
     * @param name - the layer this is being decided for, or null for core's own
     *   control on a viewer with no layers.
     *
     * Does nothing if that layer is already drawing something -- the user's own
     * choice outranks a tool's opinion, and re-opening a tool must not undo it.
     * Asked per layer rather than of the control as a whole: with one shared
     * control, "something is already showing" was true as soon as ANY tool had
     * turned the mask on, so the second plugin to open never got the mode it
     * asked for.
     */
    async enableCellLayer(preference, name = null) {
        if (!this.control) return;
        const layer = name ? this.seaDragonViewer.getCellLayer?.(name) : null;
        if (layer ? layer.mode !== "none" : this.mode !== "none") return;

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
