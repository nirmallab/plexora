/**
 * videoComposite.js - one output frame from the viewer's canvases.
 *
 * Once the barrier says a frame is ready, its pixels are gathered in one task:
 * the ground colour (unless the export is transparent), then every canvas
 * that is part of the picture, each at its place inside the viewer scaled to
 * the output -- the tile canvas, the overlay canvas (centroids, outlines,
 * plugin geometry), the section panes and their overlays when the layout
 * shows them, or the 3-D renderer's frame -- and then the scientific
 * overlays at output resolution. The app's chrome is never part of it.
 *
 * Reading back pixels (`getImageData`) only ever happens on the output
 * canvas this module owns, never on OpenSeadragon's: reading the viewer's own
 * canvas every frame moves it off the GPU and slows everything after
 * (docs/internal/3d/PERF.md).
 *
 * Classic script: `window.PlexoraVideoComposite`.
 */
(function (global) {
    "use strict";

    function groundColour(env) {
        const node = document.getElementById("openseadragon");
        const colour = node ? global.getComputedStyle(node).backgroundColor : "";
        return colour && colour !== "rgba(0, 0, 0, 0)" && colour !== "transparent" ? colour : "#000";
    }

    /** Where `element` sits inside the wrapper, scaled to the output. */
    function placeOf(element, wrapperRect, scale) {
        const r = element.getBoundingClientRect();
        return { x: (r.left - wrapperRect.left) * scale, y: (r.top - wrapperRect.top) * scale,
                 width: r.width * scale, height: r.height * scale };
    }

    function drawCanvas(ctx, source, place) {
        if (!source || !source.width || !source.height || !(place.width > 0) || !(place.height > 0)) {
            return;
        }
        ctx.drawImage(source, 0, 0, source.width, source.height, place.x, place.y, place.width,
                      place.height);
    }

    function create() {
        let canvas = null;
        let ctx = null;

        function surface(width, height, transparent) {
            if (!canvas || canvas.width !== width || canvas.height !== height
                    || canvas.dataset.alpha !== String(Boolean(transparent))) {
                canvas = document.createElement("canvas");
                canvas.width = width;
                canvas.height = height;
                canvas.dataset.alpha = String(Boolean(transparent));
                ctx = canvas.getContext("2d", { alpha: Boolean(transparent), willReadFrequently: true });
            }
            return { canvas, ctx };
        }

        /**
         * Compose frame `state` at `width x height`. `options`: `transparent`,
         * `overlays` (output.overlays), `facts`, `datasetName`, `attribution`,
         * `muteOverlay` (leave the viewer's overlay canvas out).
         * Returns `{canvas, drawn}`.
         */
        function compose(env, state, { width, height, transparent = false, overlays = {}, facts = {},
                                        datasetName = "", attribution = "", muteOverlay = false,
                                        disclose = false } = {}) {
            const { canvas: out, ctx: c } = surface(width, height, transparent);
            c.save();
            c.setTransform(1, 0, 0, 1, 0, 0);
            c.globalAlpha = 1;
            c.globalCompositeOperation = "source-over";
            c.clearRect(0, 0, width, height);
            if (!transparent) {
                c.fillStyle = groundColour(env);
                c.fillRect(0, 0, width, height);
            }
            const wrapper = env.wrapper;
            const wrapperRect = wrapper.getBoundingClientRect();
            const scale = width / Math.max(1, wrapperRect.width);
            if (state.view.mode === "volume") {
                const frame = env.renderer?.renderFrame?.();
                if (frame) c.drawImage(frame, 0, 0, frame.width, frame.height, 0, 0, width, height);
            } else {
                const sources = env.iv?.exportSources?.() || {};
                if (sources.drawer) drawCanvas(c, sources.drawer, placeOf(sources.drawer, wrapperRect, scale));
                if (sources.overlay && !muteOverlay) {
                    drawCanvas(c, sources.overlay, placeOf(sources.overlay, wrapperRect, scale));
                }
                const panes = env.panes;
                if (panes?.enabled && panes.panes && env.vs?.get?.().layout === "sections") {
                    for (const pane of Object.values(panes.panes)) {
                        if (!pane?.root || pane.root.hidden) continue;
                        const tiles = pane.osd?.drawer?.canvas;
                        if (tiles) drawCanvas(c, tiles, placeOf(tiles, wrapperRect, scale));
                        if (pane.overlay) drawCanvas(c, pane.overlay, placeOf(pane.overlay, wrapperRect, scale));
                    }
                }
            }
            const drawn = global.PlexoraVideoOverlays.draw(c, { width, height }, state, facts, overlays,
                                                          { datasetName, attribution, disclose });
            // Last, over the overlays too: a fade to black takes everything.
            if (state.transition && state.transition.kind === "fade_black") {
                c.fillStyle = `rgba(0,0,0,${Math.max(0, Math.min(1, state.transition.alpha))})`;
                c.fillRect(0, 0, width, height);
            }
            c.restore();
            return { canvas: out, drawn };
        }

        return {
            compose,
            get canvas() { return canvas; },
            /** RGBA bytes of the last composed frame (for a server encoder). */
            rgba() {
                return ctx ? ctx.getImageData(0, 0, canvas.width, canvas.height).data : null;
            },
            png() {
                return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
            },
        };
    }

    global.PlexoraVideoComposite = { create, groundColour };
})(typeof window !== "undefined" ? window : globalThis);
