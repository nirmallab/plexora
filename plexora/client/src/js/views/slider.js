/**
 * slider.js - the one slider in Plexora.
 *
 * ONE CONTROL, EVERY PANEL. Before this file there were nineteen sliders built
 * five different ways: Jinja markup with an `<output>`, `createElement` with a
 * span, an innerHTML string with nothing at all, two stacked range inputs, and
 * d3-simple-slider's SVG. Two of the fifteen native ones set `accent-color`;
 * the other thirteen rendered browser-default grey, which is to say the app
 * had no slider design, it had thirteen of the platform's. Everything here is
 * the design: a thin muted rail, an accent fill up to the thumb, a solid round
 * thumb, and a translucent disc that grows in behind it on hover and grows
 * again while dragging.
 *
 * THE HALO IS AN AFFORDANCE AND NOT A VALUE. It is drawn as `box-shadow` on
 * the thumb pseudo-element, so it costs no node, no layout and no paint of
 * anything but the thumb, and it can never be mistaken for a wider selection.
 * It is neutral white rather than tinted, because the same control is cyan in
 * a channel panel and orange in a gate and the disc has to read the same over
 * both.
 *
 * A NATIVE `<input type="range">` UNDERNEATH, reduced by CSS to its thumb.
 * That is what buys arrow keys, Home/End, a tab stop and a screen-reader
 * announcement for nothing, on every engine, forever. The rail and the fill
 * are sibling divs driven by two unitless custom properties, which is why a
 * one-handle and a two-handle slider are the same picture, the same CSS, and
 * very nearly the same code: `--plx-lo` and `--plx-hi` are the only things
 * that move, and in single mode the first of them is written once and never
 * again.
 *
 * TWO EVENTS, TWO COSTS, and this file never coalesces them. `onInput` fires
 * per tick of a drag and is where a caller repaints something cheap; `onChange`
 * fires once on release and is where a caller commits, saves, or pushes an
 * undo entry. Every consumer already had that split -- a 500ms debounced
 * PATCH, a rAF-coalesced tile repaint, one undo entry per drag -- and keeping
 * the two callbacks separate is what let all of them keep their throttling
 * exactly as it was. A tick here is one `Number()`, a few floats, one
 * `style.setProperty`, one field write and one callback. No measurement, no
 * `getBoundingClientRect`, no layout read of any kind.
 *
 * EVERY SLIDER GETS A TYPEABLE NUMBER, because a slider alone cannot express
 * "0.5 exactly" and half the values in this app are read off a paper. The
 * field is a real `<input type="number">` -- mobile numeric keyboard, native
 * bounds for assistive tech -- with its spinner arrows removed globally in
 * main.css, so `format` must return something `type=number` accepts: a plain
 * float string, no thousands separator and no unit. The unit lives in its own
 * span beside the box.
 *
 * AND `fieldMax` LETS THAT BOX GO PAST THE END OF THE TRACK. Where a slider's
 * useful travel is narrower than its legal range, the track keeps the travel
 * and the box takes the rest: the thumb pins at `max`, the value is whatever
 * was typed, and `aria-valuetext` carries it because a pinned `aria-valuenow`
 * cannot. Dragging afterwards is a new number off the track, as it should be
 * -- touching the rail means taking the rail's answer.
 *
 * LOG SLIDERS STORE THE VALUE AND DERIVE THE POSITION, never the other way
 * round. A channel window runs 1..65535 and its handle holds an integer
 * position on a 1000-step grid; if the value were read back off that grid,
 * a panel that displayed 1234 would save 1231.7, and the number the user saw
 * and the number on disk would differ. `set()` and a typed field keep the
 * exact number and move only the thumb.
 */
class PlexoraSlider {

    /** Thumb diameter in px. The CSS declares the same number as `--plx-thumb`
     *  on `.plx-slider`; this copy exists because the rail and the fill are
     *  inset by half of it and the two must not drift. */
    static THUMB = 12;

    /** Positions a log slider's handle can take. 1000 is finer than the
     *  ~300px the control is ever drawn at, so the grid is invisible. */
    static LOG_STEPS = 1000;

    /**
     * How many decimals a step implies: 0.01 -> 2, 1 -> 0, 0.5 -> 1.
     *
     * Used for the field's text and to round the dust off a snapped value --
     * `0 + 12 * 0.01` is 0.12000000000000001 in binary floating point, and
     * that is the number a panel would otherwise persist.
     */
    static decimalsFor(step) {
        const size = Math.abs(Number(step));
        if (!Number.isFinite(size) || size <= 0) return 2;
        if (Number.isInteger(size)) return 0;
        const [mantissa, exponent] = size.toExponential().split("e");
        const digits = (mantissa.split(".")[1] || "").length;
        return Math.min(8, Math.max(0, digits - Number(exponent)));
    }

    /** A number as `type="number"` will take it back: no separators, no unit. */
    static format(value, decimals = 2) {
        if (!Number.isFinite(value)) return "";
        return value.toFixed(Math.min(8, Math.max(0, decimals)));
    }

    /** Onto the step grid, measured from `min` the way a range input measures. */
    static snap(value, min, max, step) {
        const inside = Math.min(max, Math.max(min, value));
        const size = Number(step);
        if (!Number.isFinite(size) || size <= 0) return inside;
        const snapped = min + Math.round((inside - min) / size) * size;
        const tidy = Number(snapped.toFixed(PlexoraSlider.decimalsFor(size)));
        return Math.min(max, Math.max(min, tidy));
    }

    /**
     * The number box, on its own.
     *
     * Separate from the slider because two controls want the box without the
     * track: the gradient range writes its two ends under the colour bar (put
     * inline, they would shorten the very bar the handles are read against),
     * and the transcript bin row pairs a four-rung ladder with a box that
     * accepts any micron count. Both get the app's one number box by calling
     * this rather than by growing an option here for their layout.
     *
     * COMMIT IS BLUR OR ENTER, not every keystroke: "25" is typed one
     * character at a time and the first of them is 2, which is a different
     * bin size and, in the panels behind these boxes, a tile request. What
     * each keystroke does get is a preview -- the handle moves, nothing
     * commits -- and Escape puts back the number that was there on focus.
     *
     * ENTER ALSO GIVES THE FOCUS BACK. Beside a slider the box is drawn as
     * plain text until it is focused (main.css), and focus is the entire
     * difference between a number that reads as text and one that reads as
     * an input: without the blur the field stays drawn until the user finds
     * somewhere else to click. Enter commits first -- `change` fires on it --
     * and the blur finds nothing left to commit. Only Enter: Escape puts the
     * entry back and leaves the field open, for somebody who pressed it to
     * start the entry again.
     */
    static numberField(options = {}) {
        const {
            id = "", min = null, max = null, step = null,
            decimals = 2, unit = "", ariaLabel = "", width = null,
            disabled = false, value = null,
            format = (v) => PlexoraSlider.format(v, decimals),
            parse = (text) => Number(text),
            constrain = (v) => v,
            onInput = null, onCommit = null,
        } = options;

        const group = document.createElement("div");
        group.className = "plx-number-group";
        const input = document.createElement("input");
        input.type = "number";
        input.className = "plx-number";
        input.setAttribute("inputmode", "decimal");
        if (id) input.id = id;
        if (ariaLabel) input.setAttribute("aria-label", ariaLabel);
        if (width) group.style.setProperty("--plx-number-width", `${width}px`);
        const unitNode = document.createElement("span");
        unitNode.className = "plx-number-unit";
        group.append(input, unitNode);

        const api = {
            el: group, input, unit: unitNode,
            committed: Number.isFinite(value) ? value : 0,
            entry: 0, previewed: false,
        };

        api.get = () => api.committed;
        api.set = (next) => {
            if (!Number.isFinite(next)) return;
            api.committed = next;
            api.previewed = false;
            input.value = format(next);
        };
        api.setBounds = (bounds = {}) => {
            if (bounds.min !== undefined) input.min = String(bounds.min);
            if (bounds.max !== undefined) input.max = String(bounds.max);
            if (bounds.step !== undefined) input.step = String(bounds.step);
        };
        api.setUnit = (text) => {
            unitNode.textContent = text || "";
            unitNode.hidden = !text;
        };
        api.setDisabled = (flag) => { input.disabled = Boolean(flag); };

        api.setBounds({
            min: min === null ? "" : min,
            max: max === null ? "" : max,
            // `any` and not the slider's step: the box is where an exact
            // number is typed, and a box that rejected 0.37 for being off a
            // 0.5 grid would be a box that argues with what was typed. The
            // snap happens on commit, in `constrain`, where it can be seen.
            step: step === null ? "any" : step,
        });
        api.setUnit(unit);
        api.setDisabled(disabled);
        if (Number.isFinite(value)) api.set(value);

        // `Number("")` is 0, and 0 is a legal opacity, a legal Q-score and a
        // legal threshold -- so a box cleared with the intent of typing into
        // it would otherwise commit zero on the way past. Blank is not a
        // number here, whatever `parse` would make of it.
        const read = () => {
            const text = String(input.value ?? "").trim();
            return text === "" ? NaN : parse(text);
        };

        const commit = () => {
            const parsed = read();
            if (!Number.isFinite(parsed)) {
                // Cleared, or half-typed into nonsense. Put back what was
                // there rather than committing a zero nobody asked for.
                api.set(api.committed);
                if (api.previewed) onInput?.(api.committed);
                api.previewed = false;
                return;
            }
            const next = constrain(parsed);
            const changed = next !== api.committed;
            api.set(next);
            if (changed || api.previewed) onInput?.(next);
            if (changed) onCommit?.(next);
        };

        input.addEventListener("focus", () => { api.entry = api.committed; });
        input.addEventListener("input", () => {
            const parsed = read();
            if (!Number.isFinite(parsed)) return;
            api.previewed = true;
            onInput?.(constrain(parsed));
        });
        input.addEventListener("change", commit);
        input.addEventListener("keydown", (event) => {
            if (event?.key === "Escape") {
                const back = api.entry;
                const moved = api.previewed;
                api.set(back);
                if (moved) onInput?.(back);
            } else if (event?.key === "Enter") {
                input.blur?.();
            }
        });
        api.commit = commit;
        return api;
    }

    /**
     * @param mount  an element to append into, an `<input type="range">` to
     *               adopt in place, or null (the caller appends `.el` itself).
     *
     * ADOPTION IS THE COMMON CASE. Eleven of the sliders this replaces are
     * staged in a template or an innerHTML string, with an id a golden file
     * records, a `<label for>` pointing at it and, in two cases, a test
     * matching its attributes. Wrapping the element that is already there
     * keeps every one of those true, and keeps the markup readable as markup.
     */
    constructor(mount, options = {}) {
        const adopted = mount && mount.tagName === "INPUT" && mount.type === "range"
            ? mount : null;
        const opts = options || {};
        this.handlers = { onInput: opts.onInput || null, onChange: opts.onChange || null };
        this.mode = opts.mode === "range" ? "range" : "single";
        this.scale = opts.scale === "log" ? "log" : "linear";
        this.steps = Number(opts.steps) > 0 ? Number(opts.steps) : PlexoraSlider.LOG_STEPS;
        this.ends = this.mode === "range" ? ["low", "high"] : ["high"];

        const staged = (key) => {
            if (!adopted) return undefined;
            const held = adopted[key];
            return held === undefined || held === null || held === "" ? undefined : held;
        };
        const pick = (given, key, fallback) => {
            const chosen = given !== undefined && given !== null ? given : staged(key);
            const number = Number(chosen);
            return Number.isFinite(number) ? number : fallback;
        };
        this.min = pick(opts.min, "min", 0);
        this.max = pick(opts.max, "max", 1);
        // A BOX THAT GOES HIGHER THAN THE RAIL. `fieldMax` is where typing
        // stops; `max` stays where the track ends. The two differ wherever a
        // slider's useful travel and its legal range are not the same number
        // -- a transcript point size is worth dragging between 1 and 20 and a
        // track that ran to 100 would spend four fifths of its length on
        // sizes nobody wants, but the one figure that needs a 40-pixel dot
        // still has to be able to ask for it. Above `max` the thumb sits
        // pinned at the end of its travel, which is the track saying what it
        // can say; the value is the typed one, and `aria-valuetext` spells it
        // out because `aria-valuenow` can only report the pinned thumb.
        this.fieldMax = Number.isFinite(Number(opts.fieldMax))
            ? Number(opts.fieldMax) : null;
        this.ceiling = Math.max(this.max, this.fieldMax ?? this.max);
        const rawStep = opts.step !== undefined && opts.step !== null
            ? opts.step : (staged("step") ?? 1);
        this.step = rawStep === "any" ? "any" : (Number(rawStep) > 0 ? Number(rawStep) : 1);
        this.minGap = Number(opts.minGap) > 0 ? Number(opts.minGap) : 0;
        this.fixedDecimals = opts.decimals !== undefined && opts.decimals !== null;
        this.decimals = this.fixedDecimals ? Number(opts.decimals) : this.impliedDecimals();
        this.unit = opts.unit || "";
        this.formatValue = opts.format || ((v) => PlexoraSlider.format(v, this.decimals));
        this.parseValue = opts.parse || ((text) => Number(text));
        // A position on a log grid and a percentage are both lies to a screen
        // reader reading `aria-valuenow` off the input. Where the two differ,
        // the real number is spelled out instead.
        this.needsValueText = this.scale === "log" || Boolean(this.unit)
            || this.ceiling > this.max;
        this.wanted = Boolean(opts.disabled);
        this.boundsInvalid = !(this.max > this.min);
        this.top = null;
        this.typing = false;

        const seed = Array.isArray(opts.value) ? opts.value : null;
        const low = seed ? seed[0] : opts.low;
        const high = seed ? seed[1] : (this.mode === "range" ? opts.high : opts.value);
        this.values = {
            low: this.mode === "range"
                ? this.toValue(low === undefined || low === null ? this.min : low)
                : this.min,
            high: this.toValue(high === undefined || high === null
                ? (this.mode === "range" ? this.max : pick(undefined, "value", this.min))
                : high),
        };
        this.settle();

        this.build(adopted, opts);
        if (mount && !adopted && mount.appendChild) mount.appendChild(this.el);
    }

    /* ----------------------------------------------------------------- DOM */

    build(adopted, opts) {
        const root = document.createElement("div");
        let className = "plx-slider";
        if (this.mode === "range") className += " is-range";
        if (opts.labelAbove) className += " has-label-above";
        if (opts.className) className += ` ${opts.className}`;
        root.className = className;
        this.el = root;
        this.nodes = { root, inputs: {}, fields: {}, units: {} };

        const track = document.createElement("div");
        track.className = "plx-slider-track";
        const rail = document.createElement("div");
        rail.className = "plx-slider-rail";
        const fill = document.createElement("div");
        fill.className = "plx-slider-fill";
        track.append(rail, fill);
        this.nodes.track = track;
        this.nodes.rail = rail;
        this.nodes.fill = fill;

        const ids = opts.ids || {};
        const fieldIds = opts.fieldIds || {};
        const labels = this.ariaLabels(opts);
        for (const which of this.ends) {
            const id = (this.mode === "range" ? ids[which] : (opts.id || ids.high)) || "";
            const input = this.buildInput(which, adopted, id, labels[which]);
            this.nodes.inputs[which] = input;
            if (input !== adopted) track.appendChild(input);
        }

        const wantFields = this.mode === "range"
            ? opts.fields !== false : opts.field !== false;
        if (wantFields) {
            for (const which of this.ends) {
                const field = PlexoraSlider.numberField({
                    id: (this.mode === "range" ? fieldIds[which] : (opts.fieldId || fieldIds.high)) || "",
                    min: this.min, max: this.ceiling,
                    decimals: this.decimals,
                    unit: this.unit,
                    width: opts.fieldWidth || null,
                    ariaLabel: labels[which],
                    value: this.values[which],
                    format: (v) => this.formatValue(v),
                    parse: (text) => this.parseValue(text),
                    constrain: (v) => this.constrainEnd(which, v),
                    onInput: (v) => this.fromField(which, v, false),
                    onCommit: (v) => this.fromField(which, v, true),
                });
                this.nodes.fields[which] = field;
                this.nodes.units[which] = field.unit;
            }
        }

        // `[ lower ]  ----o====o----  [ upper ]`: the low field leads, because
        // a range read left to right is read in the order its ends are drawn.
        //
        // Unless the caller hands us somewhere else to put them. Two boxes and
        // their gaps are about a hundred pixels, which a 640px figure panel can
        // spare and the ~300px viewer sidebar cannot -- there the track would be
        // left too short to aim with. Those callers pass `fieldsSlot`: a row of
        // their own, above the slider, usually the one that already holds an
        // Auto button. The boxes are still ours -- same listeners, same clamp,
        // same commit -- they are just parented elsewhere, which is why
        // `destroy()` takes them out by hand.
        const slot = opts.fieldsSlot || null;
        this.nodes.fieldsSlot = slot;
        if (this.nodes.fields.low) (slot || root).appendChild(this.nodes.fields.low.el);
        root.appendChild(track);
        if (this.nodes.fields.high) (slot || root).appendChild(this.nodes.fields.high.el);

        if (opts.label) {
            const labelNode = this.buildLabel(opts);
            root.insertBefore(labelNode, root.firstChild || null);
        }
        // On the slot as well as the root when the boxes moved out: a custom
        // property inherits down the tree it is set on, and the boxes are no
        // longer in this one.
        for (const node of slot ? [root, slot] : [root]) {
            if (opts.accent) node.style.setProperty("--plx-slider-accent", opts.accent);
            if (opts.fieldWidth) {
                node.style.setProperty("--plx-number-width", `${opts.fieldWidth}px`);
            }
        }

        // Remembered so `destroy()` can undo the adoption. Adoption MOVES a
        // node the page already owned; destroying the root would take that
        // node with it and the markup would be gone for good.
        this.adopted = adopted || null;
        if (adopted?.parentNode) adopted.parentNode.insertBefore(root, adopted);
        if (adopted) track.appendChild(adopted);

        this.applyDisabled();
        this.paint();
        this.updateTop();
        for (const which of this.ends) this.syncAria(which);
    }

    buildInput(which, adopted, id, ariaLabel) {
        const reuse = adopted && (this.mode === "single" || which === "low");
        const input = reuse ? adopted : document.createElement("input");
        input.type = "range";
        let className = "plx-range plx-slider-input";
        if (this.mode === "range") className += ` is-${which}`;
        if (reuse && adopted.className) className = `${adopted.className} ${className}`;
        input.className = className;
        if (id) input.id = id;
        if (ariaLabel) input.setAttribute("aria-label", ariaLabel);
        this.writeBounds(input);
        input.value = String(this.toInput(this.values[which]));
        input.addEventListener("input", () => this.fromInput(which));
        input.addEventListener("change", () => this.released(which));
        input.addEventListener("pointerdown", () => this.grabbed(which));
        input.addEventListener("pointerup", () => this.dropped(which));
        input.addEventListener("pointercancel", () => this.dropped(which));
        input.addEventListener("keydown", () => this.keyed(which));
        input.addEventListener("blur", () => this.blurred(which));
        return input;
    }

    buildLabel(opts) {
        if (this.mode === "single") {
            const node = document.createElement("label");
            node.className = "plx-slider-label";
            node.textContent = opts.label;
            const id = this.nodes.inputs.high.id;
            if (id) node.htmlFor = id;
            return node;
        }
        // Two inputs cannot share one `for`, so a range says what it is with
        // a group name instead and each end keeps its own aria-label.
        const node = document.createElement("span");
        node.className = "plx-slider-label";
        node.textContent = opts.label;
        this.el.setAttribute("role", "group");
        this.el.setAttribute("aria-label", opts.label);
        return node;
    }

    ariaLabels(opts) {
        if (Array.isArray(opts.ariaLabels)) {
            return { low: opts.ariaLabels[0], high: opts.ariaLabels[1] };
        }
        if (opts.ariaLabels) return opts.ariaLabels;
        if (this.mode === "single") {
            return { high: opts.ariaLabel || opts.label || "Value" };
        }
        const stem = opts.ariaLabel || opts.label || "";
        return {
            low: stem ? `${stem} lower end` : "Lower end",
            high: stem ? `${stem} upper end` : "Upper end",
        };
    }

    writeBounds(input) {
        if (this.scale === "log") {
            input.min = "0";
            input.max = String(this.steps);
            input.step = "1";
            return;
        }
        input.min = String(this.min);
        input.max = String(this.max);
        input.step = String(this.step);
    }

    /* --------------------------------------------------------- value space */

    impliedDecimals() {
        if (this.scale === "log" || this.step === "any") return 2;
        return PlexoraSlider.decimalsFor(this.step);
    }

    toValue(candidate) {
        const number = Number(candidate);
        if (!Number.isFinite(number)) return this.min;
        if (this.scale === "log" || this.step === "any") {
            return Math.min(this.ceiling, Math.max(this.min, number));
        }
        return PlexoraSlider.snap(number, this.min, this.ceiling, this.step);
    }

    /** Value -> what the underlying input holds. Identity, except on a log
     *  scale, where the input holds a position on the 0..steps grid. */
    toInput(value) {
        if (this.scale !== "log") return value;
        const ratio = Math.log(this.max / this.min);
        if (!(ratio > 0) || !(this.min > 0)) return 0;
        const position = Math.round(this.steps * Math.log(value / this.min) / ratio);
        return Math.min(this.steps, Math.max(0, position));
    }

    /** What the input holds -> a value. */
    fromInputSpace(held) {
        if (this.scale !== "log") return held;
        const ratio = this.max / this.min;
        if (!(ratio > 0) || !(this.min > 0)) return this.min;
        return this.min * Math.pow(ratio, held / this.steps);
    }

    /** 0..1 along the track. Derived through the input's own grid on a log
     *  scale, so the fill's edge sits under the thumb's centre and not a
     *  fraction of a pixel off it. */
    fraction(value) {
        if (this.scale === "log") return this.toInput(value) / this.steps;
        const span = this.max - this.min;
        if (!(span > 0)) return 0;
        return Math.min(1, Math.max(0, (value - this.min) / span));
    }

    /** One end, clamped, snapped and held clear of the other. */
    constrainEnd(which, candidate) {
        let value = this.toValue(candidate);
        if (this.mode !== "range") return value;
        if (which === "low") value = Math.min(value, this.values.high - this.minGap);
        else value = Math.max(value, this.values.low + this.minGap);
        return Math.min(this.ceiling, Math.max(this.min, value));
    }

    /** Both ends into a legal pair, after a bounds change or a paired `set`. */
    settle() {
        this.values.high = this.toValue(this.values.high);
        if (this.mode !== "range") {
            this.values.low = this.min;
            return;
        }
        this.values.low = this.toValue(this.values.low);
        if (this.values.low > this.values.high - this.minGap) {
            this.values.low = Math.max(this.min, this.values.high - this.minGap);
            if (this.values.low > this.values.high) this.values.high = this.values.low;
        }
    }

    /* ------------------------------------------------------------ painting */

    /** A fraction as `calc()` will take it. Fixed notation and not `String`,
     *  because a position near an end stringifies as "1e-7" and a number in
     *  scientific notation inside `calc()` is not something every engine
     *  parses -- and six decimals is sub-pixel at any width this is drawn at. */
    static css(fraction) {
        return fraction.toFixed(6);
    }

    paint() {
        this.el.style.setProperty("--plx-lo", PlexoraSlider.css(this.fraction(this.values.low)));
        this.el.style.setProperty("--plx-hi", PlexoraSlider.css(this.fraction(this.values.high)));
    }

    /** The per-tick paint: one custom property, because only one end moved. */
    paintEnd(which) {
        this.el.style.setProperty(
            which === "low" ? "--plx-lo" : "--plx-hi",
            PlexoraSlider.css(this.fraction(this.values[which])));
    }

    syncInput(which) {
        const input = this.nodes.inputs[which];
        // PINNED AT THE END OF THE TRAVEL for a value above it -- see
        // `fieldMax`. The input would clamp the write itself; writing the
        // clamped number is what keeps this a comparison rather than a
        // rewrite on every tick for as long as the box holds 40 of a 20.
        const held = String(Math.min(this.toInput(this.values[which]),
                                     this.toInput(this.max)));
        if (input.value !== held) input.value = held;
    }

    syncField(which) {
        if (this.typing) return;
        this.nodes.fields[which]?.set(this.values[which]);
    }

    syncAria(which) {
        if (!this.needsValueText) return;
        const text = this.formatValue(this.values[which]);
        this.nodes.inputs[which].setAttribute(
            "aria-valuetext", this.unit ? `${text} ${this.unit}` : text);
    }

    /**
     * Which of the two stacked inputs takes the clicks where they overlap.
     *
     * The thumbs only ever meet at one end of the other's travel, so the one
     * that can still move is the one to raise: at the right-hand end that is
     * the low handle, which would otherwise be buried under a high handle
     * with nowhere left to go.
     */
    updateTop() {
        if (this.mode !== "range") return;
        const lo = this.fraction(this.values.low);
        const hi = this.fraction(this.values.high);
        const top = (hi - lo < 1e-6 && lo > 0.5) ? "low" : "high";
        if (this.top === top) return;
        this.top = top;
        this.nodes.inputs.low.classList.toggle("is-top", top === "low");
        this.nodes.inputs.high.classList.toggle("is-top", top === "high");
    }

    applyDisabled() {
        const off = this.wanted || this.boundsInvalid;
        this.el.classList.toggle("is-disabled", off);
        for (const which of this.ends) {
            this.nodes.inputs[which].disabled = off;
            this.nodes.fields[which]?.setDisabled(off);
        }
    }

    /* -------------------------------------------------------------- events */

    emit(name, which) {
        this.handlers[name]?.(this.get(), this.mode === "range" ? which : undefined);
    }

    /** A drag tick, or an arrow key. */
    fromInput(which) {
        const held = Number(this.nodes.inputs[which].value);
        const next = this.constrainEnd(which, this.fromInputSpace(held));
        this.values[which] = next;
        // Written back only when the constraint moved it -- one end pushing
        // into the other, or a value off the step grid. On an ordinary tick
        // the input already holds what it should and this is a comparison.
        this.syncInput(which);
        this.paintEnd(which);
        this.syncField(which);
        this.syncAria(which);
        this.updateTop();
        this.emit("onInput", which);
    }

    /** Release, or the commit half of an arrow key. */
    released(which) {
        this.nodes.inputs[which].classList.remove("is-dragging");
        this.emit("onChange", which);
    }

    /** A typed number: preview on every keystroke, commit on blur or Enter. */
    fromField(which, value, commit) {
        this.typing = true;
        this.values[which] = value;
        this.syncInput(which);
        this.paintEnd(which);
        this.syncAria(which);
        this.updateTop();
        this.typing = false;
        this.emit("onInput", which);
        if (commit) this.emit("onChange", which);
    }

    grabbed(which) {
        const input = this.nodes.inputs[which];
        input.classList.add("is-dragging");
        // Chromium matches `:focus-visible` on a range input after a plain
        // mouse click, which is where the ring-shaped box around the track
        // came from. Pointer users get the halo; the ring is for the keyboard.
        input.classList.add("is-pointer-focus");
    }

    dropped(which) {
        this.nodes.inputs[which].classList.remove("is-dragging");
    }

    /** Not on pointerup: the thumb still has focus after a click, and putting
     *  the keyboard ring back the instant the button came up would redraw the
     *  very rectangle this control exists to be rid of. */
    blurred(which) {
        const input = this.nodes.inputs[which];
        input.classList.remove("is-dragging");
        input.classList.remove("is-pointer-focus");
    }

    keyed(which) {
        this.nodes.inputs[which].classList.remove("is-pointer-focus");
    }

    /* ----------------------------------------------------------------- API */

    get() {
        return this.mode === "range"
            ? [this.values.low, this.values.high] : this.values.high;
    }

    /**
     * Set from the outside.
     *
     * `{ silent: true }` paints without telling anybody, which is what a
     * readout refresh wants. Without it both callbacks fire exactly once,
     * which is what an "Auto" button wants: the same commit a release makes.
     */
    set(value, options = {}) {
        if (this.mode === "range") {
            const pair = Array.isArray(value) ? value : [value, this.values.high];
            this.values.low = Number(pair[0]);
            this.values.high = Number(pair[1]);
        } else {
            this.values.high = Number(value);
        }
        this.settle();
        for (const which of this.ends) {
            this.syncInput(which);
            this.nodes.fields[which]?.set(this.values[which]);
            this.syncAria(which);
        }
        this.paint();
        this.updateTop();
        if (options.silent) return;
        this.emit("onInput");
        this.emit("onChange");
    }

    /**
     * New extent, step or gap, without an event.
     *
     * A channel's domain changes when HD mode is toggled and a gate's when
     * the column changes; both are the app telling the slider what it is
     * measuring, not the user setting a value, so nothing is emitted. An
     * empty extent (a constant column) disables the control rather than
     * drawing a slider whose every position means the same thing.
     */
    setBounds(bounds = {}) {
        if (bounds.min !== undefined) this.min = Number(bounds.min);
        if (bounds.max !== undefined) this.max = Number(bounds.max);
        if (bounds.fieldMax !== undefined) {
            this.fieldMax = Number.isFinite(Number(bounds.fieldMax))
                ? Number(bounds.fieldMax) : null;
        }
        this.ceiling = Math.max(this.max, this.fieldMax ?? this.max);
        this.needsValueText = this.scale === "log" || Boolean(this.unit)
            || this.ceiling > this.max;
        if (bounds.step !== undefined) {
            this.step = bounds.step === "any" ? "any" : Number(bounds.step);
            if (!this.fixedDecimals) this.decimals = this.impliedDecimals();
        }
        if (bounds.minGap !== undefined) this.minGap = Number(bounds.minGap) || 0;
        this.boundsInvalid = !(this.max > this.min);
        this.settle();
        for (const which of this.ends) {
            this.writeBounds(this.nodes.inputs[which]);
            this.syncInput(which);
            this.nodes.fields[which]?.setBounds({ min: this.min, max: this.ceiling });
            this.nodes.fields[which]?.set(this.values[which]);
            this.syncAria(which);
        }
        this.paint();
        this.updateTop();
        this.applyDisabled();
    }

    setDisabled(flag) {
        this.wanted = Boolean(flag);
        this.applyDisabled();
    }

    setUnit(text) {
        this.unit = text || "";
        this.needsValueText = this.scale === "log" || Boolean(this.unit)
            || this.ceiling > this.max;
        for (const which of this.ends) {
            this.nodes.fields[which]?.setUnit(this.unit);
            this.syncAria(which);
        }
    }

    setAccent(css) {
        if (css) this.el.style.setProperty("--plx-slider-accent", css);
        else this.el.style.removeProperty("--plx-slider-accent");
    }

    /** Every listener is on a node inside `el`, so dropping it drops them. */
    destroy() {
        this.handlers = { onInput: null, onChange: null };
        // The boxes are not always inside the root -- see `fieldsSlot` -- and
        // removing the root would leave them behind, still listening, in
        // somebody else's row.
        for (const which of this.ends) this.nodes?.fields?.[which]?.el?.remove?.();
        // GIVE BACK WHAT WAS ADOPTED, in the place the root is standing in.
        // An adopted `<input type="range">` is the page's, not ours: it came
        // from a template, it carries the id a `<label for>` and a golden
        // file name, and it is what the panel looks for the next time it
        // binds. Dropping the root with it still inside is how a panel ends
        // up as a row of labels with no controls and no way back -- the next
        // `bind()` finds nothing and returns early, so the loss is permanent
        // and silent. Restoring it makes destroy the exact inverse of
        // construction, which is what every consumer that re-renders assumes.
        if (this.adopted && this.el?.parentNode) {
            this.el.parentNode.insertBefore(this.adopted, this.el);
        }
        this.el?.remove?.();
    }
}

// Both, and for one reason: in a browser `window` IS the global object, so the
// bare identifier the callers use resolves either way. In a node vm -- which is
// how the probes run this -- `window` is an ordinary object inside the sandbox,
// and only the second assignment makes the name reachable.
if (typeof window !== "undefined") window.PlexoraSlider = PlexoraSlider;
if (typeof globalThis !== "undefined") globalThis.PlexoraSlider = PlexoraSlider;
