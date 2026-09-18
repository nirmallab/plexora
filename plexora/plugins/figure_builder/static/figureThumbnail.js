/**
 * FigureThumbnail - what a figure looks like, small.
 *
 * The store has held thumbnails since it was written (`PUT .../thumbnail`) and
 * the library has rendered `has_thumbnail` for just as long. Nothing ever
 * produced one, so every card in the library and every card in the destination
 * picker was a grey placeholder icon -- and a picker built out of pictures that
 * are all the same picture is a list of names with extra steps.
 *
 * ## Drawn in the browser, from the previews that are already there
 *
 * Server-side it would mean rendering the figure -- reading tiles, compositing
 * channels, laying out text -- which is the export path: seconds to minutes,
 * for a 320px image, on every save. The canvas page already has every placed
 * panel's preview raster decoded and on screen, so the cheap and honest
 * thumbnail is a scaled-down copy of the sheet: the page's own background, each
 * placement's rectangle, the preview drawn into it, and text and shapes as pale
 * blocks.
 *
 * Pale blocks and not real text is a deliberate limit. At 320px a caption is
 * two pixels tall and rendering it properly would mean reimplementing the
 * text layout that `render.py` owns; a block in the right place says "there is
 * a label here" truthfully, which is all a thumbnail has room to say.
 *
 * ## The layout is pure, so it can be checked
 *
 * `layout()` takes a document and gives back a list of rectangles in pixels. It
 * touches no DOM and no canvas, which is what lets the probe assert that a
 * panel 20mm from the left of a 210mm page lands a tenth of the way across --
 * the sort of arithmetic that is wrong by a factor of 25.4 in a way no
 * screenshot shows.
 *
 * `render()` is the impure half and is the only part that needs a browser.
 */
class FigureThumbnail {

    /** How wide a stored thumbnail is. Twice the 160px the library's cards are
     *  drawn at, so it is still sharp on a 2x display and still a few
     *  kilobytes. */
    static get WIDTH() { return 320; }

    /** Up to four tray previews, in a 2x2 grid, for a figure with nothing
     *  placed yet. A blank white sheet is an accurate picture of an empty page
     *  and a useless picture of a figure the user has captured six panels into
     *  -- and "captured, not yet composed" is the state a figure spends its
     *  first sitting in. */
    static get MOSAIC() { return 4; }

    /**
     * Where everything on a page lands, in thumbnail pixels.
     *
     * @param {object} document_ the figure document.
     * @param {?string} pageId which page; the first one when omitted.
     * @param {number} width the thumbnail's width in pixels.
     * @returns {?object} `{width, height, background, kind, items}` where each
     *          item is `{kind, panel_id?, x, y, w, h}`, or null when there is
     *          no page to draw.
     */
    static layout(document_, pageId, width) {
        if (!document_ || !Array.isArray(document_.pages) || !document_.pages.length) return null;
        const page = document_.pages.find((entry) => entry.page_id === pageId)
            || document_.pages[0];
        const size = page.size_mm || { w: 210, h: 297 };
        if (!(size.w > 0) || !(size.h > 0)) return null;

        const box = Math.max(16, Math.round(Number(width) || FigureThumbnail.WIDTH));
        const scale = box / size.w;
        const out = {
            width: box,
            height: Math.max(16, Math.round(size.h * scale)),
            background: page.background || "#ffffff",
            kind: "page",
            items: [],
        };

        const placed = Object.values(document_.panels || {})
            .filter((panel) => panel.placement && panel.placement.page_id === page.page_id)
            .sort((a, b) => (a.placement.z || 0) - (b.placement.z || 0));
        for (const panel of placed) {
            out.items.push({
                kind: "panel",
                panel_id: panel.panel_id,
                render_revision: panel.render_revision || 1,
                ...FigureThumbnail._rect(panel.placement, scale),
            });
        }
        for (const annotation of Object.values(document_.annotations || {})) {
            if (annotation.page_id !== page.page_id) continue;
            out.items.push({
                kind: annotation.kind === "text" ? "text" : "shape",
                ...FigureThumbnail._rect(annotation, scale),
            });
        }

        if (out.items.length) return out;

        // Nothing on the page. The tray is what the figure actually consists of
        // at this point, so that is what the card shows -- squared up, because
        // a mosaic is not a page and pretending otherwise would put four
        // panels on a sheet they are not on.
        const tray = Object.values(document_.panels || {})
            .filter((panel) => !panel.placement)
            .slice(0, FigureThumbnail.MOSAIC);
        if (!tray.length) return out;

        out.kind = "mosaic";
        out.height = box;
        const columns = tray.length > 1 ? 2 : 1;
        const rows = Math.ceil(tray.length / columns);
        const cellW = box / columns;
        const cellH = box / rows;
        tray.forEach((panel, index) => {
            out.items.push({
                kind: "panel",
                panel_id: panel.panel_id,
                render_revision: panel.render_revision || 1,
                x: Math.round((index % columns) * cellW),
                y: Math.round(Math.floor(index / columns) * cellH),
                w: Math.round(cellW),
                h: Math.round(cellH),
            });
        });
        return out;
    }

    /** A placement or annotation box in thumbnail pixels. Lines carry a SIGNED
     *  offset in w_mm/h_mm rather than a size, so the box is normalised here --
     *  a negative width would otherwise draw nothing at all. */
    static _rect(box, scale) {
        const w = (Number(box.w_mm) || 0) * scale;
        const h = (Number(box.h_mm) || 0) * scale;
        const x = (Number(box.x_mm) || 0) * scale;
        const y = (Number(box.y_mm) || 0) * scale;
        return {
            x: Math.round(w < 0 ? x + w : x),
            y: Math.round(h < 0 ? y + h : y),
            w: Math.max(1, Math.round(Math.abs(w))),
            h: Math.max(1, Math.round(Math.abs(h))),
        };
    }

    /**
     * Draw the layout and hand back a WebP blob.
     *
     * Resolves null rather than throwing on anything missing -- no canvas, no
     * page, an image that would not load. A thumbnail is a convenience and the
     * figure is already saved; a failure here must never surface as an error
     * beside work that is safely stored.
     *
     * @param {object} document_ the figure document.
     * @param {?string} pageId
     * @param {function} previewUrlFor `(panelId, renderRevision) -> url`.
     */
    static async render(document_, pageId, previewUrlFor, options) {
        const plan = FigureThumbnail.layout(document_, pageId,
            (options && options.width) || FigureThumbnail.WIDTH);
        if (!plan) return null;

        const canvas = document.createElement("canvas");
        if (typeof canvas.getContext !== "function" || typeof Image !== "function") return null;
        canvas.width = plan.width;
        canvas.height = plan.height;
        const context = canvas.getContext("2d");
        if (!context) return null;

        // A mosaic has no sheet, so it gets the page's colour as a backdrop
        // behind the tiles rather than as the picture.
        context.fillStyle = plan.background === "transparent" ? "#ffffff" : plan.background;
        context.fillRect(0, 0, plan.width, plan.height);

        for (const item of plan.items) {
            if (item.kind === "panel") {
                const image = await FigureThumbnail._load(
                    previewUrlFor(item.panel_id, item.render_revision));
                if (image) {
                    FigureThumbnail._cover(context, image, item);
                    continue;
                }
                // A panel whose preview has not been rendered yet is still a
                // panel and still occupies its part of the page: a grey block
                // is the truthful thing to draw, and leaving a hole would make
                // the layout unrecognisable.
                context.fillStyle = "rgba(15, 23, 42, 0.12)";
                context.fillRect(item.x, item.y, item.w, item.h);
            } else {
                context.fillStyle = item.kind === "text"
                    ? "rgba(15, 23, 42, 0.22)" : "rgba(15, 23, 42, 0.14)";
                context.fillRect(item.x, item.y, item.w, item.h);
            }
        }

        return new Promise((resolve) => {
            try {
                canvas.toBlob((blob) => resolve(blob || null), "image/webp", 0.8);
            } catch (error) {
                resolve(null);
            }
        });
    }

    /** Draw an image filling a box, cropping the overflow -- `object-fit:
     *  cover`. Letterboxing instead would put grey bars inside a panel's
     *  rectangle, which reads as part of the figure. */
    static _cover(context, image, box) {
        const source = { w: image.naturalWidth || image.width || 1,
                         h: image.naturalHeight || image.height || 1 };
        const scale = Math.max(box.w / source.w, box.h / source.h);
        const w = source.w * scale;
        const h = source.h * scale;
        context.save();
        context.beginPath();
        context.rect(box.x, box.y, box.w, box.h);
        context.clip();
        context.drawImage(image, box.x + (box.w - w) / 2, box.y + (box.h - h) / 2, w, h);
        context.restore();
    }

    static _load(url) {
        return new Promise((resolve) => {
            if (!url) return resolve(null);
            const image = new Image();
            // Same origin, always -- these are this app's own routes -- but
            // stated so a future absolute URL cannot silently taint the canvas
            // and make toBlob throw.
            image.crossOrigin = "anonymous";
            image.onload = () => resolve(image);
            image.onerror = () => resolve(null);
            image.src = url;
        });
    }
}
