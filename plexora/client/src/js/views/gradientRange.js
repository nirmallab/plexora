/**
 * gradientRange.js - the colour bar with two handles, and the palettes behind it.
 *
 * ONE CONTROL, TWO PLUGINS. This started life inside Cell Explorer, as the
 * numeric column's ramp. Transcripts then wanted the same thing for its
 * density map -- the same palettes, the same two handles over the bar, the
 * same disclosure for choosing -- and the choice was to copy three hundred
 * lines into a second plugin or to move them here. A copy would have been the
 * second place in Plexora where a viridis ramp is defined and the second
 * arrangement of the same control for a user to learn.
 *
 * WHY THE RANGE IS SET ON THE BAR. Two handles over the colours they select,
 * rather than a pair of number fields beside them. The bar is painted as the
 * mapping across the whole extent -- flat low colour up to the bottom handle,
 * the ramp between them, flat high colour above the top one -- which is
 * literally what the picture does with a value past either end. The control
 * and the legend are the same object and so they cannot disagree.
 *
 * THE HANDLES ARE ONE `PlexoraSlider` in range mode, overlaid on the bar with
 * its rail and fill turned off -- the bar IS the rail here. Everything a
 * handle does is core's: the two ends held a step apart, the halo, the
 * keyboard ring, and the trick that keeps both ends reachable by mouse when
 * one input covers the other. What stays local is the shape, because a 14px
 * disc would cover the very colour the handle points at, and the two numbers,
 * because put inline they would shorten the bar they are read against.
 *
 * TWO EVENTS, TWO COSTS. `onInput` fires per pixel of drag and only repaints
 * this bar. `onChange` fires once on release and is the only one that reaches
 * the caller. What is behind these handles is a lookup table over every cell,
 * or a tile request per screenful; neither is a thing to do sixty times a
 * second, and re-rendering on `input` would also pull the panel out from under
 * the handle being dragged.
 *
 * Nothing here knows what the numbers mean. The caller gives an extent, a
 * window inside it and a formatter, and gets back the window on release --
 * which is how the same bar serves a column of cell areas in square microns
 * and a density in molecules per bin.
 */
class PlexoraColorRamps {

    /**
     * Continuous ramps, as anchor colours interpolated to 256 stops at use.
     *
     * Four, deliberately: one perceptually uniform default, one warm, one
     * colour-vision-safe, one diverging, plus a custom two-colour option.
     * Forty matplotlib colormaps is a menu, not a choice, and most of them
     * are perceptually non-uniform in ways that invent structure in the data.
     *
     * THE SERVER HAS THESE TOO, in `plexora/server/utils/colormaps.py`, to the
     * digit -- it is the one that draws a density tile, while this side draws
     * the swatch that says what was asked for. Two copies of a palette drift,
     * so `tests/js/transcript_points_probe.mjs` reads both files and fails
     * when they disagree.
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

    static STOPS = 256;

    static parseHex(hex) {
        const text = String(hex || "").trim();
        if (!/^#[0-9a-f]{6}$/i.test(text)) return null;
        return [
            parseInt(text.slice(1, 3), 16),
            parseInt(text.slice(3, 5), 16),
            parseInt(text.slice(5, 7), 16),
        ];
    }

    static toHex(rgb) {
        return "#" + rgb.map(
            (v) => Math.max(0, Math.min(255, Math.round(v)))
                .toString(16).padStart(2, "0")).join("");
    }

    /** The anchors a palette name stands for, custom included. */
    static anchors(palette, custom) {
        if (palette === "custom") {
            return [custom?.low || PlexoraColorRamps.CUSTOM_LOW,
                    custom?.high || PlexoraColorRamps.CUSTOM_HIGH];
        }
        return PlexoraColorRamps.RAMPS[palette] || PlexoraColorRamps.RAMPS.viridis;
    }

    /**
     * `stops` x 3 bytes of RGB, built by interpolating the anchors.
     *
     * Anchors rather than 256 literal entries per ramp: four ramps at 256
     * colours each is 3 kB of source that nobody can read or check, and the
     * interpolation is exact at every anchor.
     */
    static ramp(palette, custom, stops = PlexoraColorRamps.STOPS) {
        const points = PlexoraColorRamps.anchors(palette, custom)
            .map((hex) => PlexoraColorRamps.parseHex(hex))
            .filter(Boolean);
        if (points.length === 0) points.push([0, 0, 0], [255, 255, 255]);
        if (points.length === 1) points.push(points[0]);

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

    /** One ramp stop as a hex string, for a panel's own swatches. */
    static rampStop(palette, custom, fraction) {
        const stops = PlexoraColorRamps.STOPS;
        const ramp = PlexoraColorRamps.ramp(palette, custom, stops);
        const index = Math.max(0, Math.min(stops - 1,
            Math.round(fraction * (stops - 1))));
        return PlexoraColorRamps.toHex([
            ramp[index * 3], ramp[index * 3 + 1], ramp[index * 3 + 2]]);
    }

    /** The plain ramp, end to end -- a swatch, with no window in it. */
    static gradientCss(palette, custom) {
        const stops = [0, 0.25, 0.5, 0.75, 1].map(
            (fraction) => PlexoraColorRamps.rampStop(palette, custom, fraction));
        return `linear-gradient(to right, ${stops.join(", ")})`;
    }
}


class PlexoraGradientRange {

    /** How finely the handles divide the extent. */
    static STEPS = 1000;

    /**
     * @param container the element to draw into; emptied on every render
     * @param handlers  { onRange(low, high), onPalette(name),
     *                    onCustomColor(end, hex) }
     */
    constructor(container, handlers = {}) {
        this.container = container;
        this.handlers = handlers;
        this.nodes = null;
        //: Whether the palette chooser is open. On the instance, not in the
        //: DOM, because choosing a palette re-renders the panel around this
        //: -- so anything held in the markup would slam shut on the click.
        this.paletteOpen = false;
        //: The last spec rendered, so opening or closing the palettes can
        //: redraw without asking the caller for state it already handed us.
        this._last = null;
        //: True only while a keystroke is going into one of the two number
        //: boxes, so that repainting the bar does not rewrite the box being
        //: typed into out from under the next key.
        this.typing = false;
    }

    /**
     * @param spec
     *   min, max    the extent the handles move over
     *   low, high   the window currently drawn between
     *   palette     a name from `palettes`
     *   custom      { low, high } hex ends, when "custom" is offered
     *   palettes    names to offer, in order; defaults to all of them
     *   labels      name -> label, for the titles
     *   auto        true/false to show an Auto button in that state; null for
     *               no Auto button at all
     *   hidden      dim the bar (the caller's own eye is off)
     *   choosable   false for an extent with nothing to set on it
     *   format      value -> string, for the two numbers underneath
     *   decimals    how many the two typeable ends show; null (the default)
     *               takes them from the handles' step. An extent in counts
     *               with a ceiling of 56.4375 steps by 0.0564375, and seven
     *               decimals on a count is noise.
     *   extras      nodes to put in the row after the palette button
     *   swatch      name -> a CSS background, for an entry in the list that
     *               is not one of these ramps. The transcript density map
     *               offers "a colour per gene" beside the four, which is a
     *               different question about the same picture and has no
     *               ramp to draw itself with.
     *   caption     what the two numbers are counts OF, set BETWEEN them.
     *               A unit belongs on the same line as the numbers it
     *               qualifies, and a line of its own for two words is a
     *               line this control cannot spare.
     */
    render(spec) {
        const container = this.container;
        if (!container) return;
        this._last = spec;
        this.nodes?.slider?.destroy();
        container.textContent = "";
        this.nodes = null;
        if (!spec) return;

        const {
            min = 0, max = 1, low = min, high = max,
            palette = "viridis", custom = null,
            palettes = Object.keys(PlexoraColorRamps.PALETTE_LABELS),
            labels = PlexoraColorRamps.PALETTE_LABELS,
            auto = null, hidden = false, choosable = true,
            format = PlexoraGradientRange.format, extras = [],
            swatch = null, caption = "", decimals = null,
        } = spec;

        const wrapper = document.createElement("div");
        wrapper.className = hidden
            ? "gradient-range is-dimmed" : "gradient-range";

        const row = document.createElement("div");
        row.className = "gradient-range-row";

        const track = document.createElement("div");
        track.className = "gradient-range-track";
        const bar = document.createElement("div");
        bar.className = "gradient-range-bar";
        track.appendChild(bar);
        row.appendChild(track);

        // A window that can be set gets two typeable numbers; one that cannot
        // gets two pieces of text. The editable pair is built with the shared
        // number box rather than inline in the slider, because inline fields
        // would take a hundred pixels out of the very bar the handles are
        // read against -- and the bar is the legend as much as the control.
        const usable = choosable && max > min && typeof PlexoraSlider !== "undefined";
        const scale = document.createElement("div");
        scale.className = "gradient-range-scale";
        const lowLabel = usable ? null : document.createElement("span");
        const highLabel = usable ? null : document.createElement("span");

        this.nodes = {
            bar, lowLabel, highLabel, palette, custom, format, swatch, decimals,
            min, max, low, high, slider: null, lowField: null, highField: null,
        };

        if (usable) {
            // One step of clearance between the ends, and not zero: a window
            // with no width has no ramp to draw and divides by zero on the
            // way to whatever is reading it.
            const step = (max - min) / PlexoraGradientRange.STEPS;
            this.nodes.slider = new PlexoraSlider(null, {
                mode: "range", min, max, step, low, high, minGap: step,
                fields: false, className: "gradient-range-slider",
                ariaLabels: ["Range minimum", "Range maximum"],
                // `input` per pixel of drag repaints this bar and nothing
                // else. What is behind these handles is a lookup table over
                // every cell or a tile request per screenful, and neither is
                // a thing to do sixty times a second.
                onInput: ([lo, hi]) => {
                    this.nodes.low = lo;
                    this.nodes.high = hi;
                    this.paint();
                },
                onChange: ([lo, hi]) => this.handlers.onRange?.(lo, hi),
            });
            track.appendChild(this.nodes.slider.el);
            // Read back what the slider actually holds: it snaps onto the step
            // grid and clamps to the ends, so taking the numbers from it
            // rather than from the spec keeps the readout equal to what
            // releasing would commit.
            [this.nodes.low, this.nodes.high] = this.nodes.slider.get();
            this.nodes.lowField = this.buildScaleField("low", step);
            this.nodes.highField = this.buildScaleField("high", step);
            row.appendChild(this.buildPaletteButton(palette, labels));
            if (auto !== null) row.appendChild(this.buildAuto(auto));
        }

        const ends = usable
            ? [this.nodes.lowField.el, this.nodes.highField.el]
            : [lowLabel, highLabel];
        if (caption) {
            const middle = document.createElement("span");
            middle.className = "gradient-range-caption";
            middle.textContent = caption;
            scale.append(ends[0], middle, ends[1]);
        } else {
            scale.append(ends[0], ends[1]);
        }
        for (const extra of extras) if (extra) row.appendChild(extra);

        this.paint();
        wrapper.append(row, scale);
        container.appendChild(wrapper);
        if (this.paletteOpen && choosable) {
            container.appendChild(
                this.buildPalettes(palette, custom, palettes, labels, swatch));
        }
    }

    /** Redraw from what was last handed in. Used by the palette disclosure,
     *  which changes what this shows and nothing about the caller's state. */
    refresh() {
        if (this._last) this.render(this._last);
    }

    /**
     * One end of the window, as a number you can type into.
     *
     * NOT the caller's `format`. That one is for reading -- it gives
     * "1,245,322" for an area and "4.2e-6" for a probability, and an
     * `<input type="number">` will take back neither. The box shows as many
     * decimals as the step has and no separators; the caption beside it, and
     * every other place these numbers are printed, still use the caller's.
     */
    buildScaleField(which, step) {
        const { min, max, decimals } = this.nodes;
        return PlexoraSlider.numberField({
            value: this.nodes[which],
            min, max,
            decimals: Number.isInteger(decimals) ? decimals
                : (step >= 1 ? 0 : PlexoraSlider.decimalsFor(step)),
            ariaLabel: which === "low" ? "Range minimum" : "Range maximum",
            constrain: (value) => {
                const held = Math.min(max, Math.max(min, value));
                return which === "low"
                    ? Math.min(held, this.nodes.high - step)
                    : Math.max(held, this.nodes.low + step);
            },
            onInput: (value) => this.typed(which, value, false),
            onCommit: (value) => this.typed(which, value, true),
        });
    }

    /**
     * A typed end: preview while the keys are going in, commit on blur or
     * Enter. The commit runs through the slider un-silenced, so the bar is
     * repainted and the caller told by exactly the path a release takes.
     */
    typed(which, value, commit) {
        const { slider } = this.nodes;
        if (!slider) return;
        const [low, high] = slider.get();
        const pair = which === "low" ? [value, high] : [low, value];
        if (commit) {
            slider.set(pair);
            return;
        }
        this.typing = true;
        slider.set(pair, { silent: true });
        [this.nodes.low, this.nodes.high] = slider.get();
        this.paint();
        this.typing = false;
    }

    /** Repaint the bar and its numbers from the handles' current positions. */
    paint() {
        const { bar, lowLabel, highLabel, lowField, highField, palette, custom,
                swatch, min, max, low, high, format } = this.nodes;
        const own = swatch?.(palette);
        // An entry with no ramp of its own still gets the window: the two
        // handles say which counts are drawn, which is a question that has
        // an answer whether the colour means "how much" or "which gene".
        bar.style.background = own || PlexoraGradientRange.gradient(
            palette, custom, low, high, min, max);
        if (lowField) {
            // Not while somebody is typing into one of them, or the second
            // keystroke lands in a box this just rewrote.
            if (!this.typing) {
                lowField.set(low);
                highField.set(high);
            }
            return;
        }
        lowLabel.textContent = format(low);
        highLabel.textContent = format(high);
    }

    /**
     * The bar's paint: what the picture does to every value in the extent.
     *
     * Sampled from the same ramp the caller draws with, and stopped flat
     * outside the chosen window, because that is what clipping is.
     */
    static gradient(palette, custom, low, high, min, max) {
        const span = max - min;
        if (!(span > 0)) return PlexoraColorRamps.gradientCss(palette, custom);
        const at = (value) =>
            Math.min(100, Math.max(0, ((value - min) / span) * 100));
        const stops = [0, 0.25, 0.5, 0.75, 1].map((fraction) =>
            `${PlexoraColorRamps.rampStop(palette, custom, fraction)}`
            + ` ${at(low + fraction * (high - low)).toFixed(2)}%`);
        const first = PlexoraColorRamps.rampStop(palette, custom, 0);
        const last = PlexoraColorRamps.rampStop(palette, custom, 1);
        return `linear-gradient(to right, ${first} 0%, ${stops.join(", ")}, ${last} 100%)`;
    }

    /**
     * Auto, lit rather than disabled when the window IS the automatic one:
     * that is the state it reports, and it is also how the row says which of
     * the two it is in without spending a caption on "(auto)".
     *
     * The same 20px glyph the channel window and the gate threshold end their
     * line with, and not a boxed word: it is one more thing on a narrow row,
     * and the wand already means "set this for me" two panels up.
     */
    buildAuto(auto) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = auto
            ? "slider-auto-button gradient-auto is-active"
            : "slider-auto-button gradient-auto";
        const icon = document.createElement("span");
        icon.className = "fas fa-wand-magic-sparkles";
        icon.setAttribute("aria-hidden", "true");
        button.appendChild(icon);
        button.setAttribute("aria-pressed", auto ? "true" : "false");
        const tip = auto ? "Drawn over the whole range"
                         : "Go back to the automatic range";
        button.title = tip;
        button.setAttribute("aria-label", tip);
        button.addEventListener("click", () => this.handlers.onRange?.(null, null));
        return button;
    }

    /**
     * The disclosure that opens the palette list, sitting against the ramp it
     * changes. Named as well as drawn: the current palette is in the title, so
     * what this button would change is answerable without opening it.
     */
    buildPaletteButton(palette, labels) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = this.paletteOpen
            ? "gradient-palette-button is-open" : "gradient-palette-button";
        button.setAttribute("aria-expanded", this.paletteOpen ? "true" : "false");
        const name = labels[palette] || palette;
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

    buildPalettes(palette, custom, palettes, labels, swatch = null) {
        // A disclosure panel, not a permanent row: it is here because the
        // button next to the ramp is open. No heading -- the button that
        // opened it is the heading.
        const section = document.createElement("div");
        section.className = "gradient-palettes";

        const row = document.createElement("div");
        row.className = "gradient-palette-row";
        row.setAttribute("role", "radiogroup");
        row.setAttribute("aria-label", "Palette");

        palettes.forEach((name) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = name === palette
                ? "gradient-palette is-active" : "gradient-palette";
            button.setAttribute("role", "radio");
            button.setAttribute("aria-checked", name === palette ? "true" : "false");
            // Named as well as shown: a swatch alone is not reachable by
            // anyone who cannot distinguish the ramps, which is the group
            // these palettes exist for.
            const label = labels[name] || name;
            button.title = label;
            button.setAttribute("aria-label", label);
            button.style.background = swatch?.(name)
                || PlexoraColorRamps.gradientCss(name, custom);
            button.addEventListener("click", () => this.handlers.onPalette?.(name));
            row.appendChild(button);
        });
        section.appendChild(row);

        if (palette === "custom") section.appendChild(this.buildCustomEnds(custom));
        return section;
    }

    buildCustomEnds(custom) {
        const row = document.createElement("div");
        row.className = "gradient-custom-row";
        [["low", "Low"], ["high", "High"]].forEach(([end, label]) => {
            const wrapper = document.createElement("label");
            wrapper.className = "gradient-custom-end";
            const input = document.createElement("input");
            input.type = "color";
            input.value = custom?.[end] || (end === "low"
                ? PlexoraColorRamps.CUSTOM_LOW : PlexoraColorRamps.CUSTOM_HIGH);
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

    /**
     * Readable in a narrow sidebar without losing what the number is.
     *
     * Both ends of the magnitude range turn up in real metadata -- a
     * probability of 0.0000042 and an area of 1,245,322 -- and the default
     * `toString` gives "4.2e-6" for one and an unreadable run of digits for
     * the other.
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

if (typeof window !== "undefined") {
    window.PlexoraColorRamps = PlexoraColorRamps;
    window.PlexoraGradientRange = PlexoraGradientRange;
}
