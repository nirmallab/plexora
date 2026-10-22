/**
 * viewRaster.js - a picture of what the viewer has drawn, cropped.
 *
 * Figure Builder's capture preview (its FigureCaptureTool delegates here) and
 * a bookmark's thumbnail (services/bookmarks.js) are the same picture: every
 * canvas inside the viewer's own element, in DOM order -- the tile drawer
 * first, then each overlay -- cropped to a rectangle of the screen, and in
 * 3-D the volume renderer's canvas drawn again at print size. Nothing is
 * fetched: it is what is on screen.
 *
 * Classic script, `window.PlexoraViewRaster`.
 */
(function (global) {
    "use strict";

    //: A 2-D picture's longest side (px): enough for a panel's preview.
    const MAX_PREVIEW_EDGE = 1400;
    //: A 3-D picture's longest side: what a 3-D panel exports from (the
    //: server has no 3-D renderer).
    const MAX_VOLUME_EDGE = 2400;

    /** The 3-D view when it is the picture on screen: `{renderer, canvas}`, or null. */
    function volumeCanvas() {
        const renderer = global.__plexora?.volumeView?.renderer;
        const state = global.__plexora?.viewState;
        if (!renderer?.canvas || renderer.canvas.hidden || !state?.isVolume?.()) return null;
        if (typeof renderer.snapshot !== "function") return null;
        return { renderer, canvas: renderer.canvas };
    }

    /**
     * Crop what the viewer has already drawn: `screenRect` (`{x, y, width,
     * height}`, CSS pixels from the top left of the viewer's canvas element)
     * of the OpenSeadragon viewer `viewer`. A canvas, or null.
     *
     * Their backing stores can be at different scales -- an overlay is sized
     * in CSS pixels, the drawer in device pixels -- so each one's crop is
     * computed from its own bounding box rather than from a shared
     * devicePixelRatio, which is what makes this correct on a mixed-DPI setup
     * and on a browser mid-zoom.
     */
    function grab(viewer, screenRect) {
        const container = viewer?.canvas;
        if (!container || !screenRect) return null;
        const containerRect = container.getBoundingClientRect();

        const cssWidth = Math.max(1, screenRect.width);
        const cssHeight = Math.max(1, screenRect.height);
        const ratio = global.devicePixelRatio || 1;
        // In 3-D the picture is the volume renderer's own canvas, and it is
        // what a 3-D panel exports from: so it is drawn again at print size
        // rather than cropped from the screen.
        const volume = volumeCanvas();
        const scale = volume
            ? Math.min(3, MAX_VOLUME_EDGE / Math.max(cssWidth, cssHeight))
            : Math.min(ratio, MAX_PREVIEW_EDGE / Math.max(cssWidth, cssHeight));

        const out = document.createElement("canvas");
        out.width = Math.max(1, Math.round(cssWidth * scale));
        out.height = Math.max(1, Math.round(cssHeight * scale));
        const context = out.getContext("2d");
        // Black, not transparent: the viewer composites channels additively over
        // black, and a preview with an alpha hole in it would look like a hole
        // rather than like the dark tissue it is.
        context.fillStyle = "#000000";
        context.fillRect(0, 0, out.width, out.height);

        for (const element of container.querySelectorAll("canvas")) {
            const rect = element.getBoundingClientRect();
            if (!element.width || !element.height || !rect.width || !rect.height) continue;
            const scaleX = element.width / rect.width;
            const scaleY = element.height / rect.height;
            const sx = (screenRect.x - (rect.left - containerRect.left)) * scaleX;
            const sy = (screenRect.y - (rect.top - containerRect.top)) * scaleY;
            try {
                // drawImage clips a source rectangle that runs off the edge and
                // scales the destination to match, so a capture that overlaps
                // the edge of an overlay needs no special case.
                context.drawImage(element, sx, sy, cssWidth * scaleX, cssHeight * scaleY,
                    0, 0, out.width, out.height);
            } catch (error) {
                // A canvas that cannot be read must not cost the user their
                // capture: the scene is the master, and a partial preview
                // still re-renders correctly at export.
                console.error("viewRaster: a layer could not be drawn", error);
            }
        }
        if (volume) {
            try {
                const rect = volume.canvas.getBoundingClientRect();
                const drawn = volume.renderer.snapshot({ width: Math.round(rect.width * scale),
                                                         height: Math.round(rect.height * scale) });
                const sx = (screenRect.x - (rect.left - containerRect.left)) * scale;
                const sy = (screenRect.y - (rect.top - containerRect.top)) * scale;
                context.drawImage(drawn, sx, sy, cssWidth * scale, cssHeight * scale,
                    0, 0, out.width, out.height);
            } catch (error) {
                console.error("viewRaster: the 3-D view could not be drawn", error);
            }
        }
        return out;
    }

    /** `grab` as a WebP blob: resolves `{blob, width, height}`, or null.
     *  WebP over PNG: a crop of tissue is roughly a tenth the size. */
    function blob(viewer, screenRect) {
        const canvas = grab(viewer, screenRect);
        if (!canvas) return Promise.resolve(null);
        return new Promise((resolve) => {
            canvas.toBlob((made) => resolve(made ? { blob: made, width: canvas.width, height: canvas.height } : null),
                "image/webp", 0.9);
        });
    }

    /**
     * The viewer's whole canvas, `{x, y, width, height}` CSS pixels as `grab`
     * takes: the same rectangle as the viewport a scene records
     * (viewerScene.currentViewport), so a bookmark's picture is its view.
     */
    function fullViewRect(viewer) {
        const container = viewer?.canvas;
        if (!container) return null;
        const rect = container.getBoundingClientRect();
        return { x: 0, y: 0, width: rect.width, height: rect.height };
    }

    const api = { MAX_PREVIEW_EDGE, MAX_VOLUME_EDGE, volumeCanvas, grab, blob, fullViewRect };
    // On `window` for the page, and on the script's global as well, as
    // viewerScene.js does: Figure Builder's probes run in a vm context whose
    // `window` is a separate stand-in, and FigureCaptureTool names this bare.
    global.PlexoraViewRaster = api;
    if (typeof globalThis !== "undefined" && globalThis !== global) {
        globalThis.PlexoraViewRaster = api;
    }
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
