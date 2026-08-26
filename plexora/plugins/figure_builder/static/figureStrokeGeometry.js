/**
 * A stroke's ink: dashes, arrowheads, tapers and fades.
 *
 * The browser's half of a rule written twice. `server/strokegeom.py` holds the
 * other half and `server/schema.py normalize_annotation` the validation, and
 * `plexora/plugins/figure_builder/tests/test_figure_builder_lines.py` re-runs
 * one case table -- written in `tests/js/figure_stroke_probe.mjs` -- through
 * both. They have to agree exactly: the canvas draws from what this file
 * produces and the PDF from what Python produces, so a disagreement shows the
 * user one arrow and prints another, and they find out in the export. Same
 * arrangement as `FigureShapeGeometry` and `FigureRichText`.
 *
 * Everything here is PURE -- no DOM, no measurement, no canvas. Units are the
 * caller's: pass pixels and get pixels, pass millimetres and get millimetres.
 * The one exception is `headSize`, which is the pt-to-pt rule for how big a head
 * should be before anyone converts it, and it is shared precisely so that the
 * canvas cannot size a head differently from the exporter.
 *
 * The model, in one paragraph. A line is a start point and a signed offset, and
 * every other thing about it is a flat style key: `line_style` says whether the
 * shaft is solid, dashed or dotted; `start_head` and `end_head` name what is
 * drawn at each end out of a small vocabulary; `head_size_pt` sizes both, with
 * zero meaning the old automatic rule; `edge` picks one of standard, a taper or
 * a fade. An arrow is a line whose `end_head` is not "none" -- there is no
 * separate arrow renderer, and the stored type `"arrow"` survives only because
 * old documents are full of it.
 */
class FigureStrokeGeometry {

    /** Mirrors `schema.LINE_STYLES`. */
    static get LINE_STYLES() { return ["solid", "dashed", "dotted"]; }

    /** Mirrors `schema.HEAD_STYLES`. */
    static get HEAD_STYLES() { return ["none", "open", "filled", "bar", "diamond"]; }

    /** Mirrors `schema.LINE_EDGES`. */
    static get LINE_EDGES() {
        return ["standard", "taper_start", "taper_end", "taper_both",
                "fade_start", "fade_end", "fade_both"];
    }

    /** Mirrors `strokegeom.DASH_FACTORS`: multiples of the effective stroke
     *  width, so a dashed 4pt rule does not read as a solid one. A zero-length
     *  dash under a round cap is what a dot IS, in SVG and in a PDF alike. */
    static get DASH_FACTORS() {
        return { dashed: [4, 2], dotted: [0, 3] };
    }

    /** Mirrors `strokegeom.MIN_DASH_WIDTH_PT`. */
    static get MIN_DASH_WIDTH_PT() { return 0.75; }

    /** Mirrors `schema.MAX_HEAD_SIZE_PT`. The panel clamps to it so a typo is
     *  corrected where the user can see it, rather than silently on the way
     *  back from the server. */
    static get MAX_HEAD_SIZE_PT() { return 72; }

    /** Mirrors `strokegeom.FADE_STEPS`. The browser never needs it -- SVG has
     *  gradients -- but the probe checks Python's plan against this one. */
    static get FADE_STEPS() { return 24; }

    /** Mirrors `strokegeom.MAX_DASHES`. */
    static get MAX_DASHES() { return 10000; }

    /** Mirrors `strokegeom.OPEN_HEAD_DEGREES`. */
    static get OPEN_HEAD_DEGREES() { return 20; }

    /** Mirrors `strokegeom.HEAD_HALF_WIDTH`. */
    static get HEAD_HALF_WIDTH() { return 0.35; }

    /** Mirrors `strokegeom.HEAD_TRIM`. */
    static get HEAD_TRIM() { return { filled: 0.85, diamond: 0.9 }; }

    /** Mirrors `strokegeom.TAPER_THIN`. */
    static get TAPER_THIN() { return 0.05; }

    /** Mirrors `strokegeom.TAPER_EDGES` and `strokegeom.FADE_EDGES`. */
    static get TAPER_EDGES() { return ["taper_start", "taper_end", "taper_both"]; }

    static get FADE_EDGES() { return ["fade_start", "fade_end", "fade_both"]; }

    static isTaper(edge) { return FigureStrokeGeometry.TAPER_EDGES.includes(edge); }

    static isFade(edge) { return FigureStrokeGeometry.FADE_EDGES.includes(edge); }

    // -- the shaft ---------------------------------------------------------

    /**
     * The dash array for a line style, in the caller's units, or null.
     *
     * Derived from the enum and never taken from a document verbatim: the PDF
     * writer refuses a negative entry or a pattern summing to zero, and an
     * exception raised inside it takes the whole export down.
     */
    static dashPattern(lineStyle, widthPt) {
        const factors = FigureStrokeGeometry.DASH_FACTORS[lineStyle];
        if (!factors) return null;
        const unit = Math.max(Number(widthPt) || 0, FigureStrokeGeometry.MIN_DASH_WIDTH_PT);
        return [factors[0] * unit, factors[1] * unit];
    }

    /**
     * How long a head is, in points, given what the user asked for.
     *
     * Zero means auto, and auto is `max(3, 4 * width)` -- the rule the old
     * `arrowHeadPx`/`export._arrow_head` pair used. Every arrow drawn before
     * head size existed stores zero, so that branch is what makes those
     * documents open looking the way they were saved.
     *
     * A stored size is honoured exactly bar one floor: a head shorter than the
     * pen drawing it is a blob, not a head. That floor is the ONLY coupling
     * between head size and stroke width, which is the whole point of the
     * control.
     */
    static headSize(headSizePt, widthPt) {
        const width = Math.max(Number(widthPt) || 0, 0);
        const asked = Number(headSizePt) || 0;
        if (asked <= 0) return Math.max(3, 4 * width);
        return Math.max(asked, 1.5 * width);
    }

    // -- the heads ---------------------------------------------------------

    /**
     * One head, in its own frame: tip at the origin, +x back down the shaft.
     *
     * `{ lines, polygon, trim, extent }`. `lines` are stroked at the shaft's
     * width and `polygon` is filled, which is what makes "open" and "bar" read
     * as pen strokes and "filled" and "diamond" as solid ink. `trim` is how far
     * back the shaft must stop so a round cap does not poke out past a solid
     * head; it is zero for the open styles, which have nothing to hide behind.
     * `extent` is how far the ink reaches from the tip, half the pen included --
     * what the canvas pads its element by.
     */
    static headGeometry(headStyle, size, width) {
        const s = Math.max(Number(size) || 0, 0);
        const w = Math.max(Number(width) || 0, 0);
        let lines = [];
        let polygon = null;
        let trim = 0;
        if (headStyle === "open") {
            const angle = FigureStrokeGeometry.OPEN_HEAD_DEGREES * Math.PI / 180;
            const across = s * Math.sin(angle);
            const along = s * Math.cos(angle);
            lines = [[[0, 0], [along, across]], [[0, 0], [along, -across]]];
        } else if (headStyle === "filled") {
            const half = FigureStrokeGeometry.HEAD_HALF_WIDTH * s;
            polygon = [[0, 0], [s, half], [s, -half]];
            trim = FigureStrokeGeometry.HEAD_TRIM.filled * s;
        } else if (headStyle === "bar") {
            lines = [[[0, -0.5 * s], [0, 0.5 * s]]];
        } else if (headStyle === "diamond") {
            const half = FigureStrokeGeometry.HEAD_HALF_WIDTH * s;
            polygon = [[0, 0], [0.5 * s, half], [s, 0], [0.5 * s, -half]];
            trim = FigureStrokeGeometry.HEAD_TRIM.diamond * s;
        } else {
            return { lines: [], polygon: null, trim: 0, extent: 0 };
        }
        let reach = 0;
        const points = lines.reduce((all, line) => all.concat(line), []).concat(polygon || []);
        for (const [x, y] of points) reach = Math.max(reach, Math.hypot(x, y));
        return { lines, polygon, trim, extent: reach + (lines.length ? w / 2 : 0) };
    }

    /**
     * A head from `headGeometry`, put on the page.
     *
     * `tip` is the end it points out of, `other` the far end of the line. The
     * head's own +x runs from the tip back toward `other`, so the head at
     * either end of a line is one table read through a different frame.
     */
    static placeHead(tip, other, geom) {
        const [ux, uy] = FigureStrokeGeometry.direction(tip, other);
        const world = ([x, y]) => [tip[0] + x * ux - y * uy, tip[1] + x * uy + y * ux];
        return {
            lines: geom.lines.map(([a, b]) => [world(a), world(b)]),
            polygon: geom.polygon ? geom.polygon.map(world) : null,
        };
    }

    /** `distance` along the line from `from` toward `toward`. */
    static trimPoint(from, toward, distance) {
        const [ux, uy] = FigureStrokeGeometry.direction(from, toward);
        const d = Number(distance) || 0;
        return [from[0] + ux * d, from[1] + uy * d];
    }

    /**
     * Where the shaft starts and stops once both heads have taken their bite.
     *
     * Degenerate cases collapse rather than invert: two heads wanting more room
     * than the line is long would otherwise give a shaft running backwards,
     * which draws, and which looks like a bite out of the middle -- exactly
     * while the user is shortening the line and watching it.
     */
    static trimmedShaft(p1, p2, trim1, trim2) {
        const length = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
        const a = Math.max(Number(trim1) || 0, 0);
        const b = Math.max(Number(trim2) || 0, 0);
        if (length <= 0) return [[p1[0], p1[1]], [p1[0], p1[1]]];
        const total = a + b;
        if (total >= length) {
            const point = FigureStrokeGeometry.trimPoint(
                p1, p2, total > 0 ? length * (a / total) : 0);
            return [point, [point[0], point[1]]];
        }
        return [FigureStrokeGeometry.trimPoint(p1, p2, a),
                FigureStrokeGeometry.trimPoint(p2, p1, b)];
    }

    // -- the edge treatments -----------------------------------------------

    /**
     * A tapered shaft as a closed polygon, or [] when `edge` is not a taper.
     *
     * A taper is not a stroke -- its width varies along its length and no
     * renderer here has a variable-width pen -- so it becomes filled ink, which
     * every backend already draws. `width` is the FULL width at the fat end,
     * the same thing `line_width_pt` means everywhere else.
     */
    static taperOutline(p1, p2, width, edge, trim1, trim2) {
        if (!FigureStrokeGeometry.isTaper(edge)) return [];
        const [a, b] = FigureStrokeGeometry.trimmedShaft(p1, p2, trim1, trim2);
        // The axis comes from the untrimmed line: a shaft trimmed away to
        // nothing has no direction of its own, and the taper is still a shape.
        const [ux, uy] = FigureStrokeGeometry.direction(p1, p2);
        const nx = -uy;
        const ny = ux;
        const w = Math.max(Number(width) || 0, 0);
        const full = w / 2;
        const thin = FigureStrokeGeometry.TAPER_THIN * w;
        const side = (point, half) => [point[0] + nx * half, point[1] + ny * half];
        if (edge === "taper_both") {
            const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
            return [side(a, thin), side(mid, full), side(b, thin),
                    side(b, -thin), side(mid, -full), side(a, -thin)];
        }
        const halfA = edge === "taper_start" ? thin : full;
        const halfB = edge === "taper_start" ? full : thin;
        return [side(a, halfA), side(b, halfB), side(b, -halfB), side(a, -halfA)];
    }

    /**
     * The opacity multiplier a fraction `t` along the shaft, 0 at the start.
     *
     * Linear, and over the TRIMMED shaft rather than the whole line: fading
     * over the full length leaves a ghost stub sticking out behind a head that
     * is drawn at full opacity.
     */
    static fadeAlpha(t, edge) {
        const u = Math.min(1, Math.max(0, Number(t) || 0));
        if (edge === "fade_start") return u;
        if (edge === "fade_end") return 1 - u;
        if (edge === "fade_both") return 1 - Math.abs(2 * u - 1);
        return 1;
    }

    /**
     * The shaft as `[a, b, alpha, isDot]` pieces to draw.
     *
     * What both EXPORTERS consume, and what neither could do natively: a PDF
     * stroke has no gradient and PIL has neither gradients nor dashes. The
     * browser ignores this -- it has `stroke-dasharray` and `<linearGradient>`
     * and should use them -- and this twin exists so the probe can prove the two
     * languages agree about what those exporters draw.
     */
    static shaftRenderPlan(p1, p2, dash, fade, steps) {
        const length = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
        if (length <= 0) return [];
        const [ux, uy] = FigureStrokeGeometry.direction(p1, p2);
        const at = (d) => [p1[0] + ux * d, p1[1] + uy * d];
        const faded = FigureStrokeGeometry.isFade(fade);
        const count = Math.max(1, Math.trunc(
            steps === undefined ? FigureStrokeGeometry.FADE_STEPS : steps));
        const plan = [];
        for (const [start, end, isDot] of FigureStrokeGeometry.dashIntervals(length, dash)) {
            if (faded && !(dash && dash.length)) {
                const span = (end - start) / count;
                for (let index = 0; index < count; index += 1) {
                    const a = start + span * index;
                    const b = a + span;
                    plan.push([at(a), at(b),
                               FigureStrokeGeometry.fadeAlpha((a + b) / 2 / length, fade), false]);
                }
                continue;
            }
            const alpha = faded
                ? FigureStrokeGeometry.fadeAlpha((start + end) / 2 / length, fade)
                : 1;
            plan.push([at(start), at(end), alpha, isDot]);
        }
        return plan;
    }

    /** `[start, end, isDot]` for every piece of ink a dash array puts down. */
    static dashIntervals(length, dash) {
        if (!dash || !dash.length) return [[0, length, false]];
        const on = Number(dash[0]) || 0;
        const off = Number(dash[1]) || 0;
        if (on + off <= 0) return [[0, length, false]];
        const isDot = on <= 0;
        const out = [];
        let position = 0;
        while (position <= length && out.length < FigureStrokeGeometry.MAX_DASHES) {
            const end = Math.min(position + on, length);
            // A zero-length piece is ink only when it is meant to be -- a dot.
            // The last dash of a dashed line can land exactly on the end, and
            // drawing it would put a round blob out past the final gap.
            if (isDot || end > position) out.push([position, end, isDot]);
            position += on + off;
        }
        return out;
    }

    /** The unit vector from `a` to `b`; +x when there is no distance between
     *  them, so a line of no length still has a head pointing somewhere. */
    static direction(a, b) {
        const dx = b[0] - a[0];
        const dy = b[1] - a[1];
        const length = Math.hypot(dx, dy);
        if (!(length > 0)) return [1, 0];
        return [dx / length, dy / length];
    }
}
