/**
 * cellExplorerContinuous.js - the numeric controls: ramp, range, palette.
 *
 * One line: the colour bar, the palette button, and Auto. The range is set on
 * the bar rather than beside it -- two handles over the data's own extent --
 * which is the arrangement that makes the picture literally true. The bar is
 * painted as the mapping across the whole extent: flat low colour up to the
 * bottom handle, the ramp between the handles, flat high colour above the top
 * one. That IS what the image does with a value past either end, so the control
 * and the legend are the same object and cannot disagree.
 *
 * This replaced a pair of typed number fields on a line of their own. They were
 * exact, which a slider is not, and they cost a line plus a caption plus the
 * whole "draft text is not committed state" apparatus -- a half-typed "0." is
 * not a number, so the text had to live in the input and the numbers in the
 * state and the two only met on Enter or blur. Two handles over a bar need none
 * of that, and what the numbers are is still readable underneath.
 *
 * Two events, two costs, the same split the opacity slider uses: `input` fires
 * per pixel of drag and only repaints this bar, while `change` fires once on
 * release and is the only one that recolours the image and saves. Recolouring
 * per pixel would be a lookup table per cell rebuilt tens of times a second,
 * and it would re-render this panel out from under the handle being dragged.
 *
 * Clipping is display-only. A value past either end takes the end colour;
 * nothing in the table is touched, and Auto always gets back to the robust
 * percentiles the server computed.
 *
 * The eye at the end of the row is the legend's per-category eye, for a kind of
 * column that has no rows to put one on. Without it a numeric column had no way
 * to get the colours off the tissue at all except dragging opacity to zero,
 * which loses whatever opacity was set to get back to.
 *
 * The palettes live behind a button beside the ramp rather than in a row of
 * their own. Choosing one is a thing people do once per column at most, and it
 * was taking permanent space in a sidebar that also has to hold a legend.
 */
class CellExplorerContinuous {

    /** How finely the handles divide the data's extent. */
    static STEPS = 1000;

    constructor(container, handlers) {
        this.container = container;
        this.handlers = handlers;
        this.nodes = null;
        //: Whether the palette chooser is open. On the instance, not in the
        //: DOM, because choosing a palette re-renders this whole panel -- so
        //: anything held in the markup would slam shut on the first click.
        this.paletteOpen = false;
        //: The last thing rendered, so opening or closing the palettes can
        //: redraw without asking the controller for state it already handed us.
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
        this.nodes = null;
        if (!descriptor) return;

        const stats = descriptor.stats || {};
        if (!Number.isFinite(stats.min)) {
            container.appendChild(CellExplorerContinuous.note(
                `No valid values found for "${descriptor.name}".`));
            return;
        }

        // A zero-width scale. Said in words, because a uniformly coloured image
        // is not something a user should have to deduce -- and with nothing to
        // spread across, neither the handles nor the palette chooser have
        // anything to change.
        if (stats.constant) {
            container.appendChild(this.buildRamp(entry, domain, stats, false, false));
            container.appendChild(CellExplorerContinuous.note(
                `Every cell has the same value: ${CellExplorerContinuous.format(stats.min)}.`));
            return;
        }

        container.appendChild(this.buildRamp(entry, domain, stats, true, auto));
        if (this.paletteOpen) container.appendChild(this.buildPalettes(entry));

        if (descriptor.n_missing > 0) {
            container.appendChild(CellExplorerContinuous.note(
                `${descriptor.n_missing.toLocaleString()} cells have no value and are not drawn.`));
        }
    }

    /** Redraw from what was last handed in. Used by the palette disclosure,
     *  which changes what this panel shows and nothing about the state. */
    refresh() {
        if (!this._last) return;
        const { descriptor, entry, domain, auto } = this._last;
        this.render(descriptor, entry, domain, auto);
    }

    /**
     * The colour bar, its two handles, the palette button and Auto -- one line,
     * with the numbers the handles are currently at underneath.
     *
     * @param choosable false for a constant column, where there is a bar to
     *   look at and nothing to set on it.
     */
    buildRamp(entry, [low, high], stats, choosable, auto) {
        const wrapper = document.createElement("div");
        // Dimmed while the overlay is off, so the panel says which of the two
        // states it is in without anybody having to look at the image.
        wrapper.className = entry.hidden
            ? "cex-ramp-wrapper is-hidden" : "cex-ramp-wrapper";

        const row = document.createElement("div");
        row.className = "cex-ramp-row";

        const track = document.createElement("div");
        track.className = "cex-ramp-track";

        const strip = document.createElement("div");
        strip.className = "cex-ramp";
        track.appendChild(strip);
        row.appendChild(track);

        const scale = document.createElement("div");
        scale.className = "cex-ramp-scale";
        const lowLabel = document.createElement("span");
        const highLabel = document.createElement("span");
        scale.append(lowLabel, highLabel);

        this.nodes = {
            strip, lowLabel, highLabel, entry, stats,
            low, high, minInput: null, maxInput: null,
        };

        if (choosable) {
            this.nodes.minInput = this.buildHandle("min", low, stats);
            this.nodes.maxInput = this.buildHandle("max", high, stats);
            // Read back what the inputs actually hold. A range input snaps its
            // value onto the step grid and clamps it to the ends, so taking
            // the numbers from the handles rather than from the domain is what
            // keeps the readout equal to what releasing would commit.
            this.nodes.low = Number(this.nodes.minInput.value);
            this.nodes.high = Number(this.nodes.maxInput.value);
            track.append(this.nodes.minInput, this.nodes.maxInput);
            row.appendChild(this.buildPaletteButton(entry));
            row.appendChild(this.buildAuto(auto));
        }
        // Offered for a constant column too: there is nothing to set on that
        // bar, and taking the overlay off to look at the tissue underneath is
        // exactly as reasonable there as anywhere else.
        row.appendChild(this.buildVisibility(entry));

        this.paint();
        wrapper.append(row, scale);
        return wrapper;
    }

    /**
     * One end of the range, as a real <input type="range">.
     *
     * Two of them stacked over the same track, which is what gives a
     * two-handled slider arrow keys, Home/End and a tab stop per end without
     * any of that being written here. The track ignores pointer events and the
     * thumbs take them back (see the stylesheet), or the upper input would
     * swallow every click meant for the lower one.
     */
    buildHandle(which, value, stats) {
        const input = document.createElement("input");
        input.type = "range";
        input.className = `cex-ramp-handle cex-ramp-handle-${which}`;
        input.min = String(stats.min);
        input.max = String(stats.max);
        input.step = String((stats.max - stats.min) / CellExplorerContinuous.STEPS || "any");
        input.value = String(value);
        input.setAttribute("aria-label", which === "min" ? "Range minimum" : "Range maximum");
        input.addEventListener("input", () => this.drag(which));
        // Release is the commit. Everything before it is this bar redrawing
        // itself, which costs nothing; this is the one that rebuilds the
        // lookup table, repaints the image and saves.
        input.addEventListener("change", () => {
            this.handlers.onRange?.(this.nodes.low, this.nodes.high);
        });
        return input;
    }

    /**
     * A handle moved. Keeps the two from crossing and repaints the bar.
     *
     * The ends are held one step apart rather than allowed to meet: a
     * zero-width domain has no ramp to draw and divides by zero on the way to
     * the lookup table.
     */
    drag(which) {
        const { minInput, maxInput, stats } = this.nodes;
        const step = (stats.max - stats.min) / CellExplorerContinuous.STEPS;
        let low = Number(minInput.value);
        let high = Number(maxInput.value);
        if (which === "min" && low > high - step) {
            low = Math.max(stats.min, high - step);
            minInput.value = String(low);
        } else if (which === "max" && high < low + step) {
            high = Math.min(stats.max, low + step);
            maxInput.value = String(high);
        }
        this.nodes.low = low;
        this.nodes.high = high;
        this.paint();
    }

    /** Repaint the bar and its numbers from the handles' current positions. */
    paint() {
        const { strip, lowLabel, highLabel, entry, stats, low, high } = this.nodes;
        strip.style.background = CellExplorerContinuous.gradient(entry, low, high, stats);
        lowLabel.textContent = CellExplorerContinuous.format(low);
        highLabel.textContent = CellExplorerContinuous.format(high);
    }

    /**
     * The bar's paint: what the image does to every value in the data's extent.
     *
     * Sampled from the same ramp the lookup table is built from, so the legend
     * cannot claim a mapping the image does not use -- and stopped flat outside
     * the chosen range, because that is what clipping is.
     */
    static gradient(entry, low, high, stats) {
        const span = stats.max - stats.min;
        // A column with no extent has no positions to place stops at. Drawn as
        // the plain ramp, which is the honest picture of a palette nothing is
        // being spread across.
        if (!(span > 0)) {
            return "linear-gradient(to right, " + [0, 0.25, 0.5, 0.75, 1].map(
                (fraction) => CellExplorerColors.rampStop(
                    entry.palette, entry.custom, fraction)).join(", ") + ")";
        }
        const at = (value) =>
            Math.min(100, Math.max(0, ((value - stats.min) / span) * 100));
        const stops = [0, 0.25, 0.5, 0.75, 1].map((fraction) =>
            `${CellExplorerColors.rampStop(entry.palette, entry.custom, fraction)}`
            + ` ${at(low + fraction * (high - low)).toFixed(2)}%`);
        const first = CellExplorerColors.rampStop(entry.palette, entry.custom, 0);
        const last = CellExplorerColors.rampStop(entry.palette, entry.custom, 1);
        return `linear-gradient(to right, ${first} 0%, ${stops.join(", ")}, ${last} 100%)`;
    }

    /**
     * Auto, lit rather than disabled when the range IS automatic: that is the
     * state it reports, and it is also how the row says which of the two it is
     * in without spending a caption on "(auto)".
     */
    buildAuto(auto) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = auto ? "cex-toggle is-active" : "cex-toggle";
        button.textContent = "Auto";
        button.setAttribute("aria-pressed", auto ? "true" : "false");
        button.title = auto
            ? "Drawn between the 1st and 99th percentiles"
            : "Go back to the automatic range";
        button.addEventListener("click", () => this.handlers.onRange?.(null, null));
        return button;
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

    /**
     * The disclosure that opens the palette list, sitting against the ramp it
     * changes. Named as well as drawn: the current palette is in the title, so
     * what this button would change is answerable without opening it.
     */
    buildPaletteButton(entry) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = this.paletteOpen
            ? "cex-palette-button is-open" : "cex-palette-button";
        button.setAttribute("aria-expanded", this.paletteOpen ? "true" : "false");
        const name = CellExplorerColors.PALETTE_LABELS[entry.palette] || entry.palette;
        button.title = `Palette: ${name}`;
        button.setAttribute("aria-label", `Choose a palette (currently ${name})`);
        const icon = document.createElement("span");
        icon.className = "fas fa-sliders";
        button.appendChild(icon);
        button.addEventListener("click", () => {
            this.paletteOpen = !this.paletteOpen;
            this.refresh();
        });
        return button;
    }

    buildPalettes(entry) {
        const section = document.createElement("div");
        // A disclosure panel, not a permanent row: it is here because the
        // button next to the ramp is open. No heading -- the button that opened
        // it is the heading.
        section.className = "cex-palettes";

        const row = document.createElement("div");
        row.className = "cex-palette-row";
        row.setAttribute("role", "radiogroup");
        row.setAttribute("aria-label", "Palette");

        Object.keys(CellExplorerColors.PALETTE_LABELS).forEach((palette) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = palette === entry.palette
                ? "cex-palette is-active" : "cex-palette";
            button.setAttribute("role", "radio");
            button.setAttribute("aria-checked", palette === entry.palette ? "true" : "false");
            // Named as well as shown: a swatch alone is not reachable by anyone
            // who cannot distinguish the ramps, which is the group these
            // palettes exist for.
            button.title = CellExplorerColors.PALETTE_LABELS[palette];
            button.setAttribute("aria-label", CellExplorerColors.PALETTE_LABELS[palette]);
            const stops = [0, 0.5, 1].map((fraction) =>
                CellExplorerColors.rampStop(palette, entry.custom, fraction));
            button.style.background = `linear-gradient(to right, ${stops.join(", ")})`;
            button.addEventListener("click", () => this.handlers.onPalette?.(palette));
            row.appendChild(button);
        });
        section.appendChild(row);

        if (entry.palette === "custom") {
            section.appendChild(this.buildCustomEnds(entry));
        }
        return section;
    }

    buildCustomEnds(entry) {
        const row = document.createElement("div");
        row.className = "cex-custom-row";
        [["low", "Low"], ["high", "High"]].forEach(([end, label]) => {
            const wrapper = document.createElement("label");
            wrapper.className = "cex-custom-end";
            const input = document.createElement("input");
            input.type = "color";
            input.value = entry.custom?.[end]
                || (end === "low" ? CellExplorerColors.CUSTOM_LOW : CellExplorerColors.CUSTOM_HIGH);
            input.addEventListener("input", (event) => {
                this.handlers.onCustomColor?.(end, event.target.value);
            });
            const text = document.createElement("span");
            text.textContent = label;
            wrapper.append(input, text);
            row.appendChild(wrapper);
        });
        return row;
    }

    static note(text) {
        const paragraph = document.createElement("p");
        paragraph.className = "cex-hint";
        paragraph.textContent = text;
        return paragraph;
    }

    /**
     * Readable in a narrow sidebar without losing what the number is.
     *
     * Both ends of the magnitude range turn up in real metadata -- a
     * probability of 0.0000042 and an area of 1,245,322 -- and the default
     * `toString` gives "4.2e-6" for one and an unreadable run of digits for the
     * other.
     */
    static format(value) {
        if (!Number.isFinite(value)) return "--";
        const magnitude = Math.abs(value);
        if (magnitude === 0) return "0";
        if (magnitude >= 100_000 || magnitude < 0.001) return value.toExponential(2);
        if (magnitude >= 1000) return Math.round(value).toLocaleString();
        if (magnitude >= 10) return value.toFixed(1);
        return value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
    }
}
