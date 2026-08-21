/**
 * cellExplorerColors.js - palettes, and the lookup table core draws from.
 *
 * `buildLUT` is the whole point of the plugin's client half. Everything the
 * user can change without touching the data -- palette, category colour,
 * category visibility, continuous range -- is a rebuild of this table and a
 * redraw, with no request and no geometry rebuilt. The values themselves are
 * fetched once per column and cached, so those interactions cost microseconds
 * rather than a round trip.
 *
 * Two shapes, because a dense table over cell ids is the fast lookup and cell
 * ids are not guaranteed to be dense. Below the threshold it is a flat
 * Uint8Array indexed by id; above it, a Map. Core reads either (see
 * ImageViewer.setCellColorLUT).
 *
 * Alpha 0 is the single "do not draw this cell" channel. Hidden category, no
 * value, a value that is NaN, a cell the table has no row for -- all of them
 * arrive as alpha 0, so the renderer needs exactly one test.
 */
class CellExplorerColors {

    /**
     * Categorical palette. Paul Tol's qualitative sets, which are built to stay
     * distinguishable under the common colour-vision deficiencies -- the usual
     * default (a rainbow, or matplotlib's tab20) puts a red and a green next to
     * each other and asks the user to tell two cell populations apart by them.
     *
     * Ordered so the first handful are maximally separated: most variables have
     * fewer than eight categories, and those are the ones that must not be
     * confusable. Past 24 the palette cycles, which is honest -- there is no
     * set of 60 colours anybody can tell apart, and the legend's search and
     * isolate controls are the real answer at that size.
     */
    static CATEGORICAL = [
        "#4477aa", "#ee6677", "#228833", "#ccbb44", "#66ccee", "#aa3377",
        "#ee7733", "#0077bb", "#009988", "#cc3311", "#33bbee", "#ee3377",
        "#88ccee", "#44aa99", "#117733", "#999933", "#ddcc77", "#cc6677",
        "#882255", "#aa4499", "#6699cc", "#994455", "#997700", "#66aa55",
    ];

    /**
     * The quick picks in a category's colour popover, in the shape core's
     * ColorSwatchPicker takes.
     *
     * Drawn from CATEGORICAL rather than invented, so the ten one-click choices
     * are the same ten colours the legend already assigned by itself: swapping
     * two categories over is picking one of the colours on screen, not matching
     * a new one to them by eye. The neutral last is UNASSIGNED, which is the
     * one deliberate "this means nothing" colour in the set.
     */
    static SWATCH_PRESETS = [
        { label: "Blue", hex: "#4477aa" },
        { label: "Red", hex: "#ee6677" },
        { label: "Green", hex: "#228833" },
        { label: "Yellow", hex: "#ccbb44" },
        { label: "Cyan", hex: "#66ccee" },
        { label: "Purple", hex: "#aa3377" },
        { label: "Orange", hex: "#ee7733" },
        { label: "Teal", hex: "#009988" },
        { label: "Magenta", hex: "#ee3377" },
        { label: "Neutral grey", hex: "#5b6675" },
    ];

    /** Missing values. Neutral and desaturated, so "we do not know" never reads
     *  as a category with a finding attached to it. */
    static UNASSIGNED = "#5b6675";

    /** The label the panel and the LUT both use for missing values. */
    static UNASSIGNED_LABEL = "Unassigned";

    /**
     * Continuous ramps, as anchor colours interpolated to 256 stops at use.
     *
     * Four, deliberately: one perceptually uniform default, one warm, one
     * colour-vision-safe, one diverging, plus a custom two-colour option. Forty
     * matplotlib colormaps is a menu, not a choice, and most of them are
     * perceptually non-uniform in ways that invent structure in the data.
     */
    static RAMPS = {
        viridis: ["#440154", "#472d7b", "#3b528b", "#2c728e", "#21918c",
                  "#28ae80", "#5ec962", "#addc30", "#fde725"],
        magma: ["#000004", "#1c1044", "#4f127b", "#812581", "#b5367a",
                "#e55964", "#fb8761", "#fec287", "#fcfdbf"],
        cividis: ["#00224e", "#123570", "#3b496c", "#575d6d", "#707173",
                  "#8a8678", "#a59c74", "#c3b369", "#fee838"],
        coolwarm: ["#3b4cc0", "#6788ee", "#9abbff", "#c9d7f0", "#edd1c2",
                   "#f7a889", "#e26952", "#b40426"],
    };

    static PALETTE_LABELS = {
        viridis: "Viridis",
        magma: "Magma",
        cividis: "Cividis (colour-vision safe)",
        coolwarm: "Cool-warm (diverging)",
        custom: "Custom",
    };

    /** Fallback ends for the custom ramp, before the user picks anything. */
    static CUSTOM_LOW = "#1b2a4a";
    static CUSTOM_HIGH = "#f7c948";

    /**
     * Above this, a dense table stops being worth allocating: 8M ids is 32 MB
     * of Uint8Array, and a table whose ids run that high is sparse by
     * definition (they are usually source label values, not row numbers).
     */
    static DENSE_MAX_ID = 8_000_000;

    static RAMP_STOPS = 256;

    // -- colour utilities ---------------------------------------------------

    static parseHex(hex) {
        const text = String(hex || "").trim();
        if (!/^#[0-9a-f]{6}$/i.test(text)) return null;
        return [
            parseInt(text.slice(1, 3), 16),
            parseInt(text.slice(3, 5), 16),
            parseInt(text.slice(5, 7), 16),
        ];
    }

    static toHex([r, g, b]) {
        const part = (v) => Math.max(0, Math.min(255, Math.round(v)))
            .toString(16).padStart(2, "0");
        return `#${part(r)}${part(g)}${part(b)}`;
    }

    /**
     * The colour a category gets when nobody has chosen one.
     *
     * From its POSITION in the legend, not a hash of its name. Two things
     * follow, and both are what a user expects: the same project opened twice
     * gives the same colours, and `cluster = "1"` and `grade = "1"` are not
     * forced to share one just because they share a label.
     */
    static defaultCategoryColor(index) {
        return CellExplorerColors.CATEGORICAL[index % CellExplorerColors.CATEGORICAL.length];
    }

    /**
     * 256 RGB stops for a palette, built by interpolating its anchors.
     *
     * Anchors rather than 256 literal entries per ramp: four ramps at 256
     * colours each is 3 kB of source that nobody can read or check, and the
     * interpolation is exact at every anchor.
     */
    static ramp(palette, custom) {
        const anchors = palette === "custom"
            ? [custom?.low || CellExplorerColors.CUSTOM_LOW,
               custom?.high || CellExplorerColors.CUSTOM_HIGH]
            : (CellExplorerColors.RAMPS[palette] || CellExplorerColors.RAMPS.viridis);

        const points = anchors
            .map((hex) => CellExplorerColors.parseHex(hex))
            .filter(Boolean);
        if (points.length === 0) points.push([0, 0, 0], [255, 255, 255]);
        if (points.length === 1) points.push(points[0]);

        const stops = CellExplorerColors.RAMP_STOPS;
        const out = new Uint8Array(stops * 3);
        const span = points.length - 1;
        for (let i = 0; i < stops; i += 1) {
            const position = (i / (stops - 1)) * span;
            const lower = Math.min(Math.floor(position), span - 1);
            const t = position - lower;
            const a = points[lower];
            const b = points[lower + 1];
            out[i * 3] = a[0] + (b[0] - a[0]) * t;
            out[i * 3 + 1] = a[1] + (b[1] - a[1]) * t;
            out[i * 3 + 2] = a[2] + (b[2] - a[2]) * t;
        }
        return out;
    }

    /** One ramp stop as a hex string, for the panel's own swatches. */
    static rampStop(palette, custom, fraction) {
        const ramp = CellExplorerColors.ramp(palette, custom);
        const index = Math.max(0, Math.min(CellExplorerColors.RAMP_STOPS - 1,
            Math.round(fraction * (CellExplorerColors.RAMP_STOPS - 1))));
        return CellExplorerColors.toHex([
            ramp[index * 3], ramp[index * 3 + 1], ramp[index * 3 + 2]]);
    }

    // -- the lookup table ---------------------------------------------------

    /**
     * Build the cell-id -> colour table core draws from.
     *
     * @param spec
     *   ids        Uint32Array, one per cell, from the values response
     *   kind       "categorical" | "continuous"
     *   codes      Uint16Array parallel to ids (categorical)
     *   values     Float32Array parallel to ids (continuous)
     *   categories [{value}] in the server's order -- codes index into this
     *   colors     { category: "#rrggbb" } overrides
     *   hidden     Set of category labels to leave undrawn
     *   domain     [low, high] for continuous
     *   palette    ramp name, or "custom"
     *   custom     { low, high } hex ends for "custom"
     *   showMissing whether cells with no value are drawn at all
     *   blank      draw nothing: a table of the right shape, transparent
     * @returns { colors, maxId } | { map } | null
     */
    static buildLUT(spec) {
        const ids = spec?.ids;
        if (!ids || !ids.length) return null;

        let maxId = 0;
        for (let i = 0; i < ids.length; i += 1) {
            if (ids[i] > maxId) maxId = ids[i];
        }

        const dense = maxId <= CellExplorerColors.DENSE_MAX_ID;
        const table = dense ? new Uint8Array(4 * (maxId + 1)) : null;
        const map = dense ? null : new Map();
        const write = dense
            ? (id, r, g, b, a) => {
                const offset = id * 4;
                table[offset] = r;
                table[offset + 1] = g;
                table[offset + 2] = b;
                table[offset + 3] = a;
            }
            // Alpha 0 is simply an absent entry in the sparse form: core treats
            // "no entry" and "alpha 0" identically, so storing hidden cells
            // would be bytes spent to say nothing.
            : (id, r, g, b, a) => { if (a) map.set(id, [r, g, b, a]); };

        // Nothing written is the whole of "drawn but hidden": a zeroed dense
        // table is alpha 0 for every id, and an empty sparse map says the same.
        // A table rather than a null LUT, because null means "this plugin is
        // not colouring cells" -- core would then draw the layer in its own
        // default white, which is not what hiding an overlay looks like.
        if (!spec.blank) {
            if (spec.kind === "continuous") {
                CellExplorerColors._fillContinuous(spec, ids, write);
            } else {
                CellExplorerColors._fillCategorical(spec, ids, write);
            }
        }
        return dense ? { colors: table, maxId } : { map };
    }

    static _fillCategorical(spec, ids, write) {
        const categories = spec.categories || [];
        const hidden = spec.hidden || new Set();
        const overrides = spec.colors || {};

        // One RGBA per code, resolved before the loop over cells. Doing the
        // override lookup and the hex parse per cell instead would be a string
        // operation per cell per redraw.
        const swatches = categories.map((entry, index) => {
            const label = entry.value;
            if (hidden.has(label)) return [0, 0, 0, 0];
            const rgb = CellExplorerColors.parseHex(overrides[label])
                || CellExplorerColors.parseHex(
                    CellExplorerColors.defaultCategoryColor(index));
            return [rgb[0], rgb[1], rgb[2], 255];
        });

        const missingHidden = spec.showMissing === false
            || hidden.has(CellExplorerColors.UNASSIGNED_LABEL);
        const missingRgb = CellExplorerColors.parseHex(
            overrides[CellExplorerColors.UNASSIGNED_LABEL]
            || CellExplorerColors.UNASSIGNED);
        const missing = missingHidden
            ? [0, 0, 0, 0] : [missingRgb[0], missingRgb[1], missingRgb[2], 255];

        const codes = spec.codes;
        for (let i = 0; i < ids.length; i += 1) {
            const code = codes[i];
            // Anything not in the dictionary -- the missing sentinel, or a code
            // from a stale payload -- is Unassigned rather than a guess at a
            // nearby category.
            const swatch = code < swatches.length ? swatches[code] : missing;
            write(ids[i], swatch[0], swatch[1], swatch[2], swatch[3]);
        }
    }

    static _fillContinuous(spec, ids, write) {
        const ramp = CellExplorerColors.ramp(spec.palette, spec.custom);
        const stops = CellExplorerColors.RAMP_STOPS;
        const values = spec.values;
        let [low, high] = spec.domain || [0, 1];
        if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) {
            // A constant column, or a range that stopped making sense. Every
            // cell takes the same stop rather than dividing by zero -- the
            // panel says so in words, which is more use than a uniform picture
            // the user has to work out for themselves.
            const flat = Math.floor(stops * 0.75) * 3;
            for (let i = 0; i < ids.length; i += 1) {
                if (!Number.isFinite(values[i])) continue;
                write(ids[i], ramp[flat], ramp[flat + 1], ramp[flat + 2], 255);
            }
            return;
        }

        const scale = (stops - 1) / (high - low);
        for (let i = 0; i < ids.length; i += 1) {
            const value = values[i];
            // NaN is how the server says "no value", including the infinities
            // it folded in. Left undrawn rather than clamped to an end, which
            // would put a handful of cells at the extreme colour and say
            // nothing about why.
            if (!Number.isFinite(value)) continue;
            // Clipping is display-only. The stored value is untouched; a cell
            // past the top of the range simply takes the top colour.
            let index = Math.round((value - low) * scale);
            if (index < 0) index = 0;
            else if (index >= stops) index = stops - 1;
            const offset = index * 3;
            write(ids[i], ramp[offset], ramp[offset + 1], ramp[offset + 2], 255);
        }
    }
}
