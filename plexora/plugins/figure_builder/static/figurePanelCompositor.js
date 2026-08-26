/**
 * FigurePanelCompositor - channels into a picture, in the browser.
 *
 * The arithmetic is `server/render.render_panel`'s, which is itself the
 * transcription of `frag.glsl`: clip each channel into its window, multiply by
 * its colour and the shared alpha, add, clip. Two copies of that in two
 * languages is one too many, but the alternative is a preview that disagrees
 * with the export -- and the windows were chosen against this arithmetic, by
 * eye. This file exists so that there are exactly two: this and the Python.
 * Quick Edit's mini view used to carry a third.
 *
 * ## Why previews are made here and not on the server
 *
 * Every preview in Figure Builder is already client-produced: capture cuts one
 * out of the live viewer's canvas, and Quick Edit cuts one out of its own. A
 * server route that rendered them instead would be a second renderer to keep in
 * step with the exporter, reachable from the browser, with a figure's whole
 * channel state in its query string -- and it would still need the same
 * arithmetic. What it costs is a fetch per visible channel, which is the same
 * traffic Quick Edit already makes and for the same reason (see server/pixels).
 */
class FigurePanelCompositor {

    /** The alpha every channel is drawn with. `render.CHANNEL_ALPHA`, which is
     *  frag.glsl's, which is what the user chose their windows against. */
    static get CHANNEL_ALPHA() { return 0.9; }

    /** Widest preview written back. A preview is a convenience raster, never
     *  the master -- the export re-renders from the source. */
    static get PREVIEW_WIDTH() { return 640; }

    /** Biggest region one `/pixels` read returns, per side. `server/pixels`'s
     *  `MAX_OUT_PIXELS`; asking for more is a 400, not a big answer. */
    static get MAX_FETCH() { return 1024; }

    /**
     * Additively composite planes of raw uint16 into one canvas.
     *
     * Every plane must be the same size -- they are reads of one region at one
     * output size, so they are; a plane that is not is skipped rather than
     * stretched, because a stretched channel is a picture of the right tissue
     * in the wrong place.
     *
     * `planes` are `{data, width, height, window: [lo, hi], color: {r,g,b}}`
     * with the window in RAW units, which is what a captured scene stores and
     * what the export renders from.
     */
    static composite(planes) {
        const first = planes && planes[0];
        if (!first) return null;
        const pixels = new ImageData(first.width, first.height);
        const out = pixels.data;
        for (let index = 0; index < first.width * first.height; index += 1) {
            out[index * 4 + 3] = 255;
        }
        for (const plane of planes) {
            if (plane.width !== first.width || plane.height !== first.height) continue;
            const low = plane.window[0];
            const span = Math.max(1e-6, plane.window[1] - plane.window[0]);
            const weight = FigurePanelCompositor.CHANNEL_ALPHA;
            const colour = [plane.color.r * weight,
                            plane.color.g * weight,
                            plane.color.b * weight];
            for (let index = 0; index < plane.data.length; index += 1) {
                const t = Math.min(1, Math.max(0, (plane.data[index] - low) / span));
                if (t <= 0) continue;
                const at = index * 4;
                out[at] = Math.min(255, out[at] + t * colour[0]);
                out[at + 1] = Math.min(255, out[at + 1] + t * colour[1]);
                out[at + 2] = Math.min(255, out[at + 2] + t * colour[2]);
            }
        }
        const canvas = document.createElement("canvas");
        canvas.width = first.width;
        canvas.height = first.height;
        canvas.getContext("2d").putImageData(pixels, 0, 0);
        return canvas;
    }

    /**
     * A fresh preview for one panel, from its own stored scene.
     *
     * Channels only, which is what the EXPORT renders -- a preview showing a
     * phenotype colouring that the exported figure will not have would be the
     * one place in this tool where the raster lies about the deliverable.
     *
     * The region is the panel's `scene.viewport`, clamped to the image and then
     * drawn at its own offset inside the full frame: a panel that overhangs the
     * edge of the slide is an ordinary thing to have captured, and centring the
     * part that exists would move the field.
     *
     * Returns `{canvas, blob, dataURL, width, height}`, or null when there is
     * nothing to draw -- no visible channels, a degenerate viewport, or a read
     * that failed.
     */
    static async renderPreview(api, figureId, panel, source, options) {
        const channels = (panel.scene.channels || [])
            .filter((channel) => channel.visible !== false && channel.key);
        const viewport = panel.scene.viewport || {};
        if (!channels.length || !(viewport.w > 0) || !(viewport.h > 0)) return null;

        const settings = options || {};
        const image = source.image || {};
        const width = Math.max(1, Math.min(
            settings.maxWidth || FigurePanelCompositor.PREVIEW_WIDTH,
            Math.round(viewport.w)));
        const height = Math.max(1, Math.round(width * viewport.h / viewport.w));
        const perPixel = viewport.w / width;

        const clamped = { x: Math.max(0, viewport.x), y: Math.max(0, viewport.y) };
        clamped.w = Math.min(image.width || (viewport.x + viewport.w),
                             viewport.x + viewport.w) - clamped.x;
        clamped.h = Math.min(image.height || (viewport.y + viewport.h),
                             viewport.y + viewport.h) - clamped.y;
        if (!(clamped.w > 0) || !(clamped.h > 0)) return null;
        const out = {
            w: Math.max(1, Math.min(FigurePanelCompositor.MAX_FETCH,
                                    Math.round(clamped.w / perPixel))),
            h: Math.max(1, Math.min(FigurePanelCompositor.MAX_FETCH,
                                    Math.round(clamped.h / perPixel))),
        };

        const planes = [];
        for (const channel of channels) {
            const result = await api.readPixels(figureId, panel.source_id, {
                channel: channel.key, x: clamped.x, y: clamped.y,
                w: clamped.w, h: clamped.h, out_w: out.w, out_h: out.h,
            });
            if (!result.ok) return null;
            planes.push({
                data: result.data, width: result.width, height: result.height,
                window: channel.window || [0, 65535],
                color: channel.color || { r: 255, g: 255, b: 255 },
            });
        }
        const sheet = FigurePanelCompositor.composite(planes);
        if (!sheet) return null;

        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        context.fillStyle = "#000000";
        context.fillRect(0, 0, width, height);
        context.drawImage(sheet,
            (clamped.x - viewport.x) / perPixel, (clamped.y - viewport.y) / perPixel,
            clamped.w / perPixel, clamped.h / perPixel);

        const blob = await new Promise((resolve) =>
            canvas.toBlob(resolve, "image/webp", 0.9));
        if (!blob) return null;
        return {
            canvas: canvas, blob: blob, width: width, height: height,
            dataURL: canvas.toDataURL("image/webp", 0.9),
        };
    }
}
