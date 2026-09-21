/**
 * cellExplorerContinuous.js - the numeric controls: ramp, range, palette.
 *
 * One line: the colour bar, the palette button, Auto, and the eye. The range
 * is set on the bar rather than beside it -- two handles over the data's own
 * extent -- which is the arrangement that makes the picture literally true.
 *
 * THE BAR ITSELF IS CORE'S, `client/src/js/views/gradientRange.js`. It used to
 * live here, and moved out when the transcript density map needed the same
 * palettes and the same two handles: the argument for one implementation is
 * the same one that put the ramps in one place, and it is stronger for an
 * interaction than for a list of colours. What is left in this file is the
 * three things core cannot know -- that the extent is a column's stats, that
 * a constant column has nothing to set, and that a numeric column needs an
 * eye of its own.
 *
 * Clipping is display-only. A value past either end takes the end colour;
 * nothing in the table is touched, and Auto always gets back to the robust
 * percentiles the server computed.
 *
 * The eye at the end of the row is the legend's per-category eye, for a kind
 * of column that has no rows to put one on. Without it a numeric column had no
 * way to get the colours off the tissue at all except dragging opacity to
 * zero, which loses whatever opacity was set to get back to.
 */
class CellExplorerContinuous {

    /** How finely the handles divide the data's extent. Core's number. */
    static get STEPS() { return PlexoraGradientRange.STEPS; }

    constructor(container, handlers) {
        this.container = container;
        this.handlers = handlers;
        //: One instance for the life of the panel, so the palette disclosure
        //: stays open across the re-render that choosing a palette causes.
        this.range = new PlexoraGradientRange(null, {
            onRange: (low, high) => this.handlers.onRange?.(low, high),
            onPalette: (palette) => this.handlers.onPalette?.(palette),
            onCustomColor: (end, hex) => this.handlers.onCustomColor?.(end, hex),
        });
        //: The last thing rendered, so anything that redraws this panel
        //: without new state can do it without asking the controller.
        this._last = null;
    }

    /**
     * @param descriptor the column's server descriptor
     * @param entry      { palette, custom, range } for this column
     * @param domain     [low, high] currently drawn between
     * @param auto       whether that domain is the automatic one
     */
    render(descriptor, entry, domain, auto) {
        const container = this.container;
        if (!container) return;
        this._last = { descriptor, entry, domain, auto };
        container.textContent = "";
        if (!descriptor) return;

        const stats = descriptor.stats || {};
        if (!Number.isFinite(stats.min)) {
            container.appendChild(CellExplorerContinuous.note(
                `No valid values found for "${descriptor.name}".`));
            return;
        }

        const host = document.createElement("div");
        container.appendChild(host);
        this.range.container = host;
        // A zero-width scale has nothing to spread across, so neither the
        // handles nor the palette chooser have anything to change -- but the
        // eye is exactly as reasonable there as anywhere else, and the bar is
        // still worth looking at.
        this.range.render({
            min: stats.min,
            max: stats.max,
            low: domain[0],
            high: domain[1],
            palette: entry.palette,
            custom: entry.custom,
            auto: stats.constant ? null : Boolean(auto),
            hidden: Boolean(entry.hidden),
            choosable: !stats.constant,
            format: CellExplorerContinuous.format,
            extras: [this.buildVisibility(entry)],
        });

        if (stats.constant) {
            container.appendChild(CellExplorerContinuous.note(
                `Every cell has the same value: ${CellExplorerContinuous.format(stats.min)}.`));
            return;
        }
        if (descriptor.n_missing > 0) {
            container.appendChild(CellExplorerContinuous.note(
                `${descriptor.n_missing.toLocaleString()} cells have no value and are not drawn.`));
        }
    }

    /** Redraw from what was last handed in. */
    refresh() {
        if (!this._last) return;
        const { descriptor, entry, domain, auto } = this._last;
        this.render(descriptor, entry, domain, auto);
    }

    /**
     * Draw this column's overlay, or take it off.
     *
     * The same eye the legend puts on every category row, for a kind of column
     * that has no rows -- so a numeric column had no way at all to get the
     * colours off the tissue short of dragging opacity to zero and back, which
     * loses whatever opacity was set. Nothing about the column is forgotten
     * while it is off; the lookup table is simply built transparent.
     */
    buildVisibility(entry) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "cex-visibility cex-ramp-visibility";
        button.setAttribute("aria-pressed", entry.hidden ? "false" : "true");
        button.title = entry.hidden ? "Show these colours" : "Hide these colours";
        button.setAttribute("aria-label", button.title);
        const icon = document.createElement("span");
        // The icon carries the state as well as the dimming does, so it is
        // never conveyed by contrast alone.
        icon.className = entry.hidden ? "fas fa-eye-slash" : "fas fa-eye";
        button.appendChild(icon);
        button.addEventListener("click", () => {
            this.handlers.onHidden?.(!entry.hidden);
        });
        return button;
    }

    static note(text) {
        const paragraph = document.createElement("p");
        paragraph.className = "cex-hint";
        paragraph.textContent = text;
        return paragraph;
    }

    /** Readable in a narrow sidebar without losing what the number is. */
    static format(value) { return PlexoraGradientRange.format(value); }
}
