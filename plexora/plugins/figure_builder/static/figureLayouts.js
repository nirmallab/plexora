/**
 * FigureLayouts - the arrangements a set of panels can be laid out in.
 *
 * Pure geometry, no DOM and no document: given a preset, a box to fill and the
 * two gaps, where does each cell go. The canvas applies the answer
 * (`FigureCanvas.applyLayout`) and the server keeps a record of what was asked
 * for on the group (`schema.normalize_group_layout`), so the arrangement can be
 * asked for again with a different gap, a different preset or the panels in a
 * different order -- which is what makes a layout editable rather than a
 * one-off move.
 *
 * ## Cells keep the images' proportions
 *
 * Every cell has the same shape, taken from the first panel: a microscopy
 * figure is almost always a set of fields captured through the same frame, and
 * a grid of cells of one shape is what lets their edges line up. The cells are
 * then sized to the WIDTH available and the whole arrangement is shrunk, gaps
 * kept, until it is no taller than the room below it. Nothing is ever cropped
 * to fit: in "fit" a panel of another shape sits centred inside its cell, and
 * "fill" -- which does change the field each panel shows -- is a choice the
 * user makes and the inspector names.
 *
 * Lazy, like FigureShapeDefs.PRESETS: nothing here reads another file at load
 * time, so the order the scripts are declared in does not matter.
 */
class FigureLayouts {

    /** Every preset, in the order the picker shows them. `n` is how many
     *  panels it holds; see `fits` for which selection sizes each accepts. */
    static get PRESETS() {
        return [
            { id: "2x1", label: "2 × 1", kind: "grid", cols: 2, rows: 1 },
            { id: "3x1", label: "3 × 1", kind: "grid", cols: 3, rows: 1 },
            { id: "4x1", label: "4 × 1", kind: "grid", cols: 4, rows: 1 },
            { id: "1x2", label: "1 × 2", kind: "grid", cols: 1, rows: 2 },
            { id: "2x2", label: "2 × 2", kind: "grid", cols: 2, rows: 2 },
            { id: "3x2", label: "3 × 2", kind: "grid", cols: 3, rows: 2 },
            { id: "2x3", label: "2 × 3", kind: "grid", cols: 2, rows: 3 },
            { id: "3x3", label: "3 × 3", kind: "grid", cols: 3, rows: 3 },
            { id: "1x3", label: "1 × 3", kind: "grid", cols: 1, rows: 3 },
            { id: "4x2", label: "4 × 2", kind: "grid", cols: 4, rows: 2 },
            { id: "top2", label: "Top + 2", kind: "top", count: 2 },
            { id: "top3", label: "Top + 3", kind: "top", count: 3 },
            { id: "left2", label: "Left + 2", kind: "left", count: 2 },
            { id: "left3", label: "Left + 3", kind: "left", count: 3 },
        ].map((preset) => ({
            ...preset,
            n: preset.kind === "grid" ? preset.cols * preset.rows : preset.count + 1,
        }));
    }

    static byId(id) {
        return FigureLayouts.PRESETS.find((preset) => preset.id === id) || null;
    }

    /** Whether `preset` can hold exactly `count` panels without an empty
     *  cell in the middle. A grid may leave its last row short (seven panels in
     *  a 4 x 2); the asymmetric layouts need every cell, because an empty
     *  "large" cell is not a layout anybody wants. */
    static fits(preset, count) {
        if (!preset || count < 2) return false;
        if (preset.kind !== "grid") return count === preset.n;
        return count <= preset.n && count > preset.n - preset.cols;
    }

    /** The presets worth suggesting first for a selection of `count`. */
    static suggested(count) {
        const order = {
            2: ["2x1", "1x2"], 3: ["3x1", "top2", "left2", "1x3"],
            4: ["2x2", "4x1", "top3", "left3"], 5: ["3x2", "top3"],
            6: ["3x2", "2x3"], 7: ["4x2", "3x3"], 8: ["4x2", "3x3"], 9: ["3x3"],
        }[count] || [];
        return order.map((id) => FigureLayouts.byId(id))
            .filter((preset) => FigureLayouts.fits(preset, count));
    }

    /**
     * The cells of `preset`, in reading order, filling `box` = {x, y, w, maxH}.
     *
     * `aspect` is width over height of one ordinary cell. The arrangement is
     * computed at the full width, then -- if it would run past `maxH` --
     * recomputed at whatever width makes it exactly that tall. Height is
     * linear in width for every preset here (cells scale, gaps do not), so two
     * evaluations are the whole solve. Centred across the box when narrowed.
     */
    static cells(preset, box, gapX, gapY, aspect) {
        const a = aspect > 0 ? aspect : 4 / 3;
        const at = (width) => FigureLayouts.raw(preset, width, gapX, gapY, a);
        let width = Math.max(1, box.w);
        let cells = at(width);
        const height = (list) => Math.max(...list.map((c) => c.y + c.h));
        if (box.maxH > 0 && height(cells) > box.maxH + 1e-6) {
            const h1 = height(at(width));
            const h2 = height(at(width / 2));
            const slope = (h1 - h2) / (width / 2);
            const fitted = slope > 0 ? width - (h1 - box.maxH) / slope : width;
            width = Math.max(1, Math.min(width, fitted));
            cells = at(width);
        }
        const spanW = Math.max(...cells.map((c) => c.x + c.w));
        const left = box.x + (box.w - spanW) / 2;
        return cells.map((c) => ({ x_mm: left + c.x, y_mm: box.y + c.y, w_mm: c.w, h_mm: c.h }));
    }

    /** One preset at one width, origin at 0,0. */
    static raw(preset, width, gapX, gapY, a) {
        const out = [];
        if (preset.kind === "grid") {
            const cw = (width - gapX * (preset.cols - 1)) / preset.cols;
            const ch = cw / a;
            for (let i = 0; i < preset.cols * preset.rows; i++) {
                const col = i % preset.cols;
                const row = Math.floor(i / preset.cols);
                out.push({ x: col * (cw + gapX), y: row * (ch + gapY), w: cw, h: ch });
            }
            return out;
        }
        const k = preset.count;
        if (preset.kind === "top") {
            const th = width / a;
            out.push({ x: 0, y: 0, w: width, h: th });
            const cw = (width - gapX * (k - 1)) / k;
            for (let i = 0; i < k; i++) {
                out.push({ x: i * (cw + gapX), y: th + gapY, w: cw, h: cw / a });
            }
            return out;
        }
        // "left": one large cell and k stacked beside it, the stack exactly as
        // tall as the large cell and every cell the same shape. Solved from
        //   width = a*H + gapX + a*(H - (k-1)*gapY)/k
        const height = (width - gapX + (k - 1) * gapY * a / k) / (a * (1 + 1 / k));
        const lw = a * height;
        const rw = width - gapX - lw;
        const rh = (height - (k - 1) * gapY) / k;
        out.push({ x: 0, y: 0, w: lw, h: height });
        for (let i = 0; i < k; i++) {
            out.push({ x: lw + gapX, y: i * (rh + gapY), w: rw, h: rh });
        }
        return out;
    }

    /**
     * Where a panel of shape `aspect` goes inside `cell`.
     *
     * "fit": the largest box of the panel's own proportions that the cell
     * holds, centred -- the picture is never cut. "fill": the cell itself; the
     * caller reframes the panel's field to match (FigureSchema.aspectViewport),
     * which is a change to what the panel SHOWS and is reported as such.
     */
    static place(cell, aspect, fit) {
        if (fit === "fill" || !(aspect > 0)) return { ...cell };
        const cellAspect = cell.w_mm / cell.h_mm;
        let w = cell.w_mm;
        let h = cell.h_mm;
        if (aspect > cellAspect) h = w / aspect;
        else w = h * aspect;
        return {
            x_mm: cell.x_mm + (cell.w_mm - w) / 2,
            y_mm: cell.y_mm + (cell.h_mm - h) / 2,
            w_mm: w, h_mm: h,
        };
    }

    /** A preset drawn as a small picture of its cells, for the pickers. Inline
     *  SVG rather than an icon font, which has no glyph for "large left panel
     *  and two stacked beside it". */
    static diagram(preset, width = 44, height = 32) {
        const cells = FigureLayouts.cells(preset, { x: 0, y: 0, w: width, maxH: height },
                                          2.5, 2.5, 4 / 3);
        const top = (height - Math.max(...cells.map((c) => c.y_mm + c.h_mm))) / 2;
        const rects = cells.map((c) =>
            `<rect x="${c.x_mm.toFixed(2)}" y="${(c.y_mm + top).toFixed(2)}" `
            + `width="${c.w_mm.toFixed(2)}" height="${c.h_mm.toFixed(2)}" rx="1"/>`).join("");
        return `<svg class="fb-layout-diagram" viewBox="0 0 ${width} ${height}" `
            + `width="${width}" height="${height}" aria-hidden="true">${rects}</svg>`;
    }
}
