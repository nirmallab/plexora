/**
 * figureBookmarkStore.js - the captures bin as core's bookmarks store.
 *
 * Core's bookmarks (client/src/js/services/bookmarks.js: the tray, K, the
 * toolbar icon) keep nothing of their own. A bookmark is a capture in this
 * plugin's bin with `bookmarked` set and a `label` (server/captures.py), so
 * it is badged and filterable on the captures page and can be put in a
 * figure like any other capture. This file is the only code that knows the
 * routes, and it is loaded on every viewer page this plugin applies to
 * (Plugin.viewer_scripts), whether the tool is open or not -- with the scene
 * (figureSceneSnapshot.js, figureSchema.js) and the API it needs.
 *
 * Registration does not depend on load order: this runs in <head>, before
 * core's deferred scripts, so it usually waits for `plexora:bookmarks-ready`;
 * where core came first, it registers at once.
 */
(function () {
    "use strict";

    const api = new FigureBuilderApi();
    //: This image described once per page, as the sidebar does
    //: (FigureSidebarController.describeThisImage).
    const described = new Map();

    function describe(datasource) {
        if (!described.has(datasource)) {
            described.set(datasource, api.describeCaptureSource(datasource).catch(() => null));
        }
        return described.get(datasource);
    }

    /** A row of the bin as core lists a bookmark. */
    function entry(row) {
        return {
            id: row.capture_id,
            label: row.label || row.caption || "",
            created_at: row.created_at || "",
            preview_url: row.has_preview ? api.capturePreviewUrl(row.capture_id) : null,
            scene: row.scene || null,
        };
    }

    function changed() {
        // The sidebar's strip, if open, reads the bin again.
        window.dispatchEvent(new CustomEvent("plexora:bookmarks-changed",
                                             { detail: { origin: "figure_builder" } }));
    }

    const store = {
        async list(datasource) {
            const result = await api.listCaptures(datasource, { bookmarked: true });
            return result.ok ? (result.data.captures || []).map(entry) : [];
        },

        capture(ctx) {
            return FigureScene.capture(ctx, "", FigureScene.currentViewport(ctx));
        },

        restore(ctx, scene, options) {
            return FigureScene.restore(ctx, scene, { persist: Boolean(options && options.persist) });
        },

        async add({ id, datasource, scene, label }) {
            const source = await describe(datasource);
            const caption = source ? FigureBookmarkCaption(source, scene) : "";
            const result = await api.addCapture({
                capture_id: id, datasource: datasource, source: source || {}, scene: scene,
                caption: caption, bookmarked: true, label: label || "",
            });
            return Boolean(result.ok);
        },

        async putPreview(id, blob, size) {
            const result = await api.putCapturePreview(id, blob, size);
            return Boolean(result.ok);
        },

        async update(id, changes) {
            const result = await api.patchCapture(id, changes);
            if (result.ok) changed();
            return Boolean(result.ok);
        },

        locateUrl(id) {
            return api.capturesHref() + "?bookmarked=1#" + encodeURIComponent(id);
        },
    };

    /** The caption a capture gets in the bin: how wide its field is. */
    function FigureBookmarkCaption(source, scene) {
        const viewport = (scene && scene.viewport) || { w: 0 };
        const span = FigureSchema.physicalWidthUm(source, viewport);
        return span
            ? FigureSchema.formatMicrons(span) + " wide"
            : Math.round(viewport.w ? FigureSchema.frameSize(viewport).w : 0) + " px wide";
    }

    if (window.PlexoraBookmarks?.registerStore) {
        window.PlexoraBookmarks.registerStore(store);
    } else {
        window.addEventListener("plexora:bookmarks-ready",
                                (event) => event.detail?.registerStore?.(store), { once: true });
    }
})();
