/**
 * The lines the picker offers, as style overlays.
 *
 * There is one line OBJECT and there is not meant to be a second one. A dashed
 * line is a line with `line_style: "dashed"`; an arrow is a line with an
 * `end_head`; a double arrow has two. Nothing here is a type, a subclass, or a
 * different renderer -- each variant is a handful of style keys laid over the
 * canvas's ordinary drawing defaults, which is why changing an arrow into a
 * plain line afterwards is a property edit rather than a delete and redraw.
 *
 * The icons ARE the definitions: `icon()` asks `FigureStrokeGeometry` for the
 * same dash pattern and the same head the canvas will draw. A separate icon set
 * would be a second drawing of every variant, and the first time one of them is
 * adjusted the picker starts lying about what it inserts. They are also inline
 * SVG rather than Font Awesome spans on purpose -- FontAwesome replaces
 * `<span class="fas">` once at boot, so a span injected into a card opened later
 * never becomes anything and draws nothing at all.
 */
class FigureLineDefs {

    /**
     * The variants, built on first use and kept.
     *
     * Lazy rather than computed at load, and that is not an optimisation: every
     * file in this plugin is a plain script whose load order is deliberately
     * not significant (`test_the_order_of_the_declared_scripts_does_not_matter`),
     * and reading another class at load time would make this the one file that
     * has to come second.
     */
    static get VARIANTS() {
        if (!FigureLineDefs.variantCache) FigureLineDefs.variantCache = FigureLineDefs.build();
        return FigureLineDefs.variantCache;
    }

    /** One variant, or null. */
    static byId(id) { return FigureLineDefs.VARIANTS[id] || null; }

    /**
     * The card's cells, in reading order.
     *
     * No "reverse arrow". A line drawn right-to-left already IS one -- the
     * geometry keeps its direction in the SIGNS of w/h -- and for a line already
     * on the page the panel's Start head is the control. A cell for it would be
     * a third way to say the same thing.
     */
    static get GRID() { return ["line", "dashed", "dotted", "arrow", "double"]; }

    static build() {
        const defs = {};
        const add = (id, label, style) => { defs[id] = { id, label, style }; };
        add("line", "Line", { line_style: "solid" });
        add("dashed", "Dashed line", { line_style: "dashed" });
        add("dotted", "Dotted line", { line_style: "dotted" });
        // "open" rather than "filled": this is the arrow every figure already
        // drawn with this plugin has, and the tool that used to be called Arrow
        // has to keep placing it.
        add("arrow", "Arrow", { line_style: "solid", end_head: "open" });
        add("double", "Double arrow",
            { line_style: "solid", start_head: "open", end_head: "open" });
        return defs;
    }

    /**
     * A variant's icon: the shaft it draws, with the heads it draws.
     *
     * Drawn at a head size that suits a 24-pixel box rather than at the one the
     * tool inserts -- the auto rule is derived from the pen, and a 3pt head in
     * an icon is a smudge. Everything else, the dash pattern included, comes
     * from the geometry the canvas uses.
     */
    static icon(id) {
        const variant = FigureLineDefs.byId(id);
        if (!variant) return "";
        const style = variant.style;
        const p1 = [4, 12];
        const p2 = [20, 12];
        const width = 1.8;
        const size = 6;
        const heads = [
            FigureStrokeGeometry.headGeometry(style.start_head || "none", size, width),
            FigureStrokeGeometry.headGeometry(style.end_head || "none", size, width),
        ];
        const [a, b] = FigureStrokeGeometry.trimmedShaft(
            p1, p2, heads[0].trim, heads[1].trim);
        const dash = FigureStrokeGeometry.dashPattern(style.line_style, 1);
        const parts = [`<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"`
            + (dash ? ` stroke-dasharray="${dash[0]} ${dash[1]}"` : "") + "/>"];
        for (const [tip, other, geom] of [[p1, p2, heads[0]], [p2, p1, heads[1]]]) {
            const placed = FigureStrokeGeometry.placeHead(tip, other, geom);
            for (const [from, to] of placed.lines) {
                parts.push(`<line x1="${from[0]}" y1="${from[1]}"`
                    + ` x2="${to[0]}" y2="${to[1]}"/>`);
            }
            if (placed.polygon) {
                parts.push(`<polygon points="${placed.polygon.map(
                    ([x, y]) => `${x},${y}`).join(" ")}" fill="currentColor"/>`);
            }
        }
        return `<svg class="fb-line-icon" viewBox="0 0 24 24" aria-hidden="true"`
            + ` focusable="false" fill="none" stroke="currentColor"`
            + ` stroke-width="${width}" stroke-linecap="round">${parts.join("")}</svg>`;
    }

    /**
     * A head style's icon, for the panel's Start/End rows.
     *
     * A stub of shaft with the head on its right, so the five cells read as
     * five ends of the same line rather than as five unrelated glyphs. "none" is
     * the stub alone, which is what it means.
     */
    static headIcon(headStyle) {
        const tip = [19, 12];
        const other = [5, 12];
        const width = 1.8;
        const geom = FigureStrokeGeometry.headGeometry(headStyle, 7, width);
        const [a, b] = FigureStrokeGeometry.trimmedShaft(other, tip, 0, geom.trim);
        const placed = FigureStrokeGeometry.placeHead(tip, other, geom);
        const parts = [`<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}"/>`];
        for (const [from, to] of placed.lines) {
            parts.push(`<line x1="${from[0]}" y1="${from[1]}"`
                + ` x2="${to[0]}" y2="${to[1]}"/>`);
        }
        if (placed.polygon) {
            parts.push(`<polygon points="${placed.polygon.map(
                ([x, y]) => `${x},${y}`).join(" ")}" fill="currentColor"/>`);
        }
        return `<svg class="fb-line-icon" viewBox="0 0 24 24" aria-hidden="true"`
            + ` focusable="false" fill="none" stroke="currentColor"`
            + ` stroke-width="${width}" stroke-linecap="round">${parts.join("")}</svg>`;
    }

    /** A line style's icon, for the panel's Style row: the dash, and nothing
     *  else. Heads are a different row and would only be noise here. */
    static styleIcon(lineStyle) {
        const dash = FigureStrokeGeometry.dashPattern(lineStyle, 1);
        return `<svg class="fb-line-icon" viewBox="0 0 24 24" aria-hidden="true"`
            + ` focusable="false" fill="none" stroke="currentColor"`
            + ` stroke-width="1.8" stroke-linecap="round">`
            + `<line x1="4" y1="12" x2="20" y2="12"`
            + (dash ? ` stroke-dasharray="${dash[0]} ${dash[1]}"` : "") + "/></svg>";
    }
}
