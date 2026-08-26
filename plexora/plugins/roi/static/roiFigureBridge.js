/**
 * roiFigureBridge.js - what ROI tells a figure about itself.
 *
 * Two DOM events, and no import in either direction. Figure Builder does not
 * know this plugin exists and this plugin does not know Figure Builder does;
 * the same arrangement `plexora:roi-hover` already uses between ROI and Cell
 * Explorer, for the same reason -- a build with one and not the other has to
 * work, and both of these listeners simply never fire when nobody dispatches.
 *
 *   plexora:figure-capture-state   "describe what you are drawing"
 *   plexora:figure-restore-state   "here is what you were drawing"
 *
 * ## What is restored, and what deliberately is not
 *
 * ROI's category visibility is PERSISTED -- it lives in the annotation document
 * beside the shapes. Restoring it would therefore be an edit to the user's
 * annotations, made because they looked at a figure panel. That is precisely
 * what a figure must not do: a panel edit may not silently rewrite the
 * project's own state.
 *
 * So the restore applies only what is transient -- whether the overlay is drawn
 * at all -- and REPORTS the rest. When the categories on screen no longer match
 * the ones the panel was captured with, that comes back as "partial" and the
 * figure says so. The panel's own preview is unaffected either way: it is a
 * raster of what was captured, and export re-renders from the recorded state
 * rather than from the live plugin.
 */
(function () {
    const PLUGIN_NAME = "roi";

    /** This plugin's live controller, or null when its tool is not open. */
    function controller() {
        return window.__plexora?.plugins?.get(PLUGIN_NAME)?.sidebarController || null;
    }

    function version() {
        return window.__plexora?.plugins?.get(PLUGIN_NAME)?.definition?.version || "";
    }

    /**
     * What is being drawn, as a blob only this plugin reads back.
     *
     * The state and nothing else. This used to compute a LEGEND as well -- a
     * row per visible category, stored with the panel so that a figure exported
     * on a machine without this plugin still carried the right labels. Figure
     * Builder no longer keeps them: its export re-renders channels from the
     * source and cannot reproduce an ROI overlay at all, so those rows keyed a
     * picture the exported figure does not contain. What survives is what the
     * blob was always for -- putting this panel's view back on the screen.
     */
    function describe() {
        const roi = controller();
        const store = roi?.store;
        if (!store) return null;

        const categories = {};
        for (const category of store.sortedCategories ? store.sortedCategories() : store.categories) {
            categories[category.id] = category.visible !== false;
        }
        return {
            state: {
                overlay_visible: Boolean(roi.renderer?.enabled),
                categories: categories,
                feature_count: store.features ? store.features.length : 0,
            },
        };
    }

    window.addEventListener("plexora:figure-capture-state", (event) => {
        const described = describe();
        if (!described) return;   // tool not open: contribute nothing, say nothing
        event.detail?.contribute?.(PLUGIN_NAME, {
            version: version(),
            state: described.state,
        });
    });

    window.addEventListener("plexora:figure-restore-state", (event) => {
        const wanted = event.detail?.plugins?.[PLUGIN_NAME];
        const report = event.detail?.report;
        if (!wanted) return;

        const roi = controller();
        if (!roi) return;   // not open; Figure Builder records "skipped" for us

        const state = wanted.state || {};
        // Transient and ours to set: whether the overlay draws at all.
        roi.renderer?.setEnabled?.(state.overlay_visible !== false);

        // Persisted and NOT ours to overwrite. Reported instead, so the figure
        // can tell the user which part of the panel the live viewer is not
        // reproducing -- rather than either lying about it or quietly editing
        // their annotations.
        const current = roi.store?.categories || [];
        const differs = current.some((category) =>
            state.categories
            && Object.prototype.hasOwnProperty.call(state.categories, category.id)
            && (category.visible !== false) !== state.categories[category.id]);

        report?.(PLUGIN_NAME, differs ? "partial" : "ok");
    });
})();
