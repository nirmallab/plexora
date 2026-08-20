/**
 * @class ViewerControls - wires the core, module-independent sidebar toggles
 * (Centroids, HD tiles, segmentation Outlines). These render whenever the
 * viewer has channels (see index.html's image_kind != 'rgb' gate) regardless
 * of which tool, if any, is currently open -- so their listeners live here
 * instead of inside a specific tool module (previously csvGatingList.js,
 * which meant they silently did nothing unless Thresholding happened to be
 * the active tool on this page view).
 *
 * Nothing is drawn over the image on load. A cell layer -- outlines or
 * centroids -- costs a manifest fetch, a mask pyramid read and a full repaint,
 * and a user who opened a project to look at the image wanted the image. It is
 * turned on by `enableCellLayer()` when a plugin that colours cells activates,
 * using the layer the project recorded (server/models/project.py's CELL_LAYERS).
 *
 * This used to default Outlines on whenever a mask existed, and fall back to
 * centroids when it did not. That produced the worst version of both: a project
 * whose mask pyramid was still being built in the background reported no
 * segmentation yet, so it silently opened on centroids -- for a slide the user
 * had just supplied a mask for -- and the mask only appeared minutes later, if
 * they thought to toggle it.
 */
class ViewerControls {

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
     * @function init - binds the Centroids/HD/Outlines checkboxes. No-ops on datasources
     * that don't render them at all (e.g. RGB quick-view, where index.html never emits
     * the controls).
     */
    init() {
        const outlinesEl = document.querySelector('#seg_controls_outlines');
        const centroidsEl = document.querySelector('#seg_controls_centroids');
        const hdEl = document.querySelector('#viewer_controls_hd');
        if (!outlinesEl || !centroidsEl || !hdEl) return;

        // Toggle outlined / filled cell selections
        outlinesEl.addEventListener('change', async e => {
            this.seaDragonViewer.viewerManagerVMain.sel_outlines = e.target.checked;
            if (e.target.checked) {
                try {
                    await this.seaDragonViewer.ensureSegmentationReady(true);
                    await this.seaDragonViewer.updateSegmentationFilter(this.currentFilter(), true);
                } catch (error) {
                    console.warn("Unable to load segmentation outlines.", error);
                    e.target.checked = false;
                    this.seaDragonViewer.viewerManagerVMain.sel_outlines = false;
                    // Only when there are positions to draw. A project with no
                    // table, or one whose coordinate roles nobody has answered,
                    // would spend the fetch on a manifest with no points in it.
                    if (PlexoraDataset.hasCentroids(this.config)) {
                        await this.seaDragonViewer.updateCentroidFallback(true);
                    }
                }
            } else {
                this.seaDragonViewer.viewer.forceRedraw();
            }
            this.eventHandler.trigger("SELECTION_CHANGED", this.currentFilter());
            // Cross-file, non-module notification for navbarControls.js's mirrored
            // View > Show Outlines checkbox -- same pattern as
            // plexora:hd-mode-changed in viewerManager.js.
            window.dispatchEvent(new CustomEvent("plexora:outlines-changed", { detail: { enabled: outlinesEl.checked } }));
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

        // Toggle centroid visibility
        centroidsEl.addEventListener('change', async e => {
            // A click is a decision, so it outranks whatever the automatic
            // fallback chose -- main.js's adoptSegmentation reads this to know
            // whether a mask arriving later may take the drawing over.
            this.seaDragonViewer.centroidsFromFallback = false;
            await this.seaDragonViewer.updateCentroidVisibility(e.target.checked);
            if (e.target.checked) {
                this.seaDragonViewer.setLoading(true);
                try {
                    this.seaDragonViewer.updateCentroidFilter(this.currentFilter(), true);
                } finally {
                    this.seaDragonViewer.setLoading(false);
                }
            }
            window.dispatchEvent(new CustomEvent("plexora:centroids-changed", { detail: { enabled: e.target.checked } }));
        });

        // Toggle HD (full-precision 16-bit) tile quality
        hdEl.addEventListener('change', e => {
            this.seaDragonViewer.viewerManagerVMain.setHdMode(e.target.checked);
        });
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

    /**
     * @function enableCellLayer - turn on the layer this project draws cells
     * with, for a plugin that colours them.
     *
     * @param preference - "segmentation" | "centroids". Falls back to the
     *   override recorded on the project, and to the mask when there is
     *   neither: a user who supplied a mask supplied it to be used, so this is
     *   defaulted rather than asked for before a tool can open.
     *
     * The recorded preference is a preference, not a promise: a mask whose
     * pyramid is still being built cannot be drawn yet, so this falls back to
     * centroids for now and leaves the stored choice alone. main.js polls the
     * conversion job and reloads when it lands, and the reload gets the mask.
     *
     * Does nothing if a layer is already showing -- the user's own toggling
     * outranks a tool's opinion, and opening a second tool must not undo it.
     */
    async enableCellLayer(preference) {
        const outlinesEl = document.querySelector('#seg_controls_outlines');
        const centroidsEl = document.querySelector('#seg_controls_centroids');
        if (!outlinesEl || !centroidsEl) return;
        if (outlinesEl.checked || centroidsEl.checked) return;

        const wanted = preference || this.config?.cellLayer || null;
        const canOutline = this.hasSegmentation() && !this.seaDragonViewer.noLabel;
        const canCentroid = PlexoraDataset.hasCentroids(this.config);

        // Segmentation first whenever it is wanted and ready: it shows the real
        // cell shape, and a user who supplied a mask supplied it to be used.
        const target = (wanted !== 'centroids' && canOutline) ? outlinesEl
            : canCentroid ? centroidsEl
            : canOutline ? outlinesEl
            : null;
        if (!target) return;

        // Dispatched rather than called directly so the one handler above does
        // the loading, and navbarControls.js's mirrored View menu item follows
        // -- the same path a click takes.
        target.checked = true;
        target.dispatchEvent(new Event('change', { bubbles: true }));

        // Set after the dispatch, which clears it: the handler treats a change
        // event as a user decision, and this one was not. Centroids reached
        // here despite a mask being wanted means the pyramid is still building,
        // so main.js may swap to outlines once it lands.
        if (target === centroidsEl && wanted !== 'centroids' && !canOutline) {
            this.seaDragonViewer.centroidsFromFallback = true;
        }
    }
}
