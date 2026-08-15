/**
 * @class ViewerControls - wires the core, module-independent sidebar toggles
 * (Centroids, HD tiles, segmentation Outlines). These render whenever the
 * viewer has channels (see index.html's image_kind != 'rgb' gate) regardless
 * of which tool, if any, is currently open -- so their listeners live here
 * instead of inside a specific tool module (previously csvGatingList.js,
 * which meant they silently did nothing unless Thresholding happened to be
 * the active tool on this page view).
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
     * @function currentFilter - the active gate selections, when a gating-like tool is
     * open; falls back to no filter (show everything) so Outlines/Centroids remain
     * fully functional with no tool active at all.
     */
    currentFilter() {
        return window.__plexora?.csv_gatingList?.selections || {};
    }

    /**
     * @function init - binds the Centroids/HD/Outlines checkboxes. No-ops on datasources
     * that don't render them at all (e.g. RGB quick-view, where index.html never emits
     * the controls).
     */
    init() {
        const outlinesEl = document.querySelector('#gating_controls_outlines');
        const centroidsEl = document.querySelector('#gating_controls_centroids');
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
                    await this.seaDragonViewer.updateCentroidFallback(true);
                }
            } else {
                this.seaDragonViewer.viewer.forceRedraw();
            }
            this.eventHandler.trigger("GATING_BRUSH_END", this.currentFilter());
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
        // segmentation at all, defaulting Outlines on instead of falling back
        // to centroids.
        const hasSegmentation = Boolean(this.config?.segmentation);
        if (hasSegmentation && !this.seaDragonViewer.noLabel) {
            outlinesEl.checked = true;
            window.setTimeout(async () => {
                try {
                    await this.seaDragonViewer.ensureSegmentationReady(true);
                    this.seaDragonViewer.viewerManagerVMain.sel_outlines = true;
                    await this.seaDragonViewer.updateSegmentationFilter(this.currentFilter(), true);
                    this.eventHandler.trigger("GATING_BRUSH_END", this.currentFilter());
                } catch (error) {
                    console.warn("Unable to load default segmentation outlines.", error);
                    outlinesEl.checked = false;
                    this.seaDragonViewer.viewerManagerVMain.sel_outlines = false;
                    if (this.config?.has_feature_data !== false) {
                        await this.seaDragonViewer.updateCentroidFallback(true);
                    }
                }
                window.dispatchEvent(new CustomEvent("plexora:outlines-changed", { detail: { enabled: outlinesEl.checked } }));
            }, 0);
        } else if (!hasSegmentation) {
            // No segmentation was registered at all -- previously this fell
            // back to centroids only as a side effect of ensureSegmentationReady()
            // being wrongly attempted (via the old imageData[0]-based check)
            // and failing. Now that hasSegmentation correctly short-circuits
            // that attempt, fall back explicitly instead of relying on an
            // error path that no longer runs.
            this.seaDragonViewer.noLabel = true;
            // Quick-view datasources (has_feature_data: false) have no real
            // per-cell coordinates -- only a synthesized stub point at the image
            // center (see datasource.py's _write_stub_point_csv) -- so falling
            // back to centroids here would just delay load fetching that stub
            // and draw a fake dot in the middle of the image.
            if (this.config?.has_feature_data !== false) {
                window.setTimeout(() => this.seaDragonViewer.updateCentroidFallback(true), 0);
            }
        }

        // Toggle centroid visibility
        centroidsEl.addEventListener('change', async e => {
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
}
