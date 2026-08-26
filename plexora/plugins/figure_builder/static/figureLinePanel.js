/**
 * The line sidebar: stroke, width, dash, both heads, head size, edge, opacity.
 *
 * CONTEXTUAL, in the same strip as the text and shape panels and by the same
 * argument: it appears while a line is selected and goes away again, so the
 * canvas keeps the width of the window for everything else.
 * `FigureWorkspace.SIDEBARS` names them and `contextSidebar` settles which one
 * has the strip -- a panel that showed and hid itself is how two of them ended
 * up stacked.
 *
 * It carries line STYLING and nothing else, the same line the other two panels
 * draw. Arrange, align, duplicate and delete apply to every object kind and
 * belong to the floating bar.
 *
 * One consequence worth stating: the bar's Stroke and Colour popovers no longer
 * apply to lines. Two controls for one number, in two places, disagreeing about
 * which is authoritative, is the failure the text panel already avoided by
 * moving formatting off the bar. So `figureActions.js` narrows them, and the
 * legacy `rect`/`ellipse` annotations -- which have no panel and are no longer
 * creatable -- keep them.
 *
 * Both heads are here and neither is privileged. The card's five cells are a
 * starting point, not a taxonomy: an arrow is a line whose end carries a head,
 * and taking the head off again is one press here rather than a delete and a
 * redraw. Which is also why there is no "reverse arrow" anywhere -- Start head
 * and End head are the controls, and they always were.
 *
 * "Auto" head size is the stored zero, not a separate flag. Zero means "size it
 * from the pen", which is what every arrow drawn before this control existed
 * stores by construction -- see `strokegeom.head_size`.
 */
class FigureLinePanel {

    constructor({ root, canvas, state, onStyle, onClose }) {
        this.root = root;
        this.canvas = canvas;
        this.state = state;
        this.onStyle = onStyle || (() => {});
        this.onClose = onClose || (() => {});
        this.annotationId = null;
        //: Shut by hand for THIS line. Cleared when the selection moves, so
        //: closing the panel is "not for this one" rather than "never again".
        this.dismissed = false;
        //: What the stroke toggle puts back. Session-only, for the reason the
        //: shape panel's is: a document storing "the width it would have if it
        //: had one" is storing a number nothing draws.
        this.lastWidth = FigureLinePanel.DEFAULT_WIDTH_PT;
    }

    setup() {
        if (!this.root) return;
        this.root.addEventListener("input", (event) => this.changed(event));
        this.root.addEventListener("change", (event) => this.changed(event));
        this.root.addEventListener("click", (event) => this.clicked(event));
        this.root.addEventListener("keydown", (event) => this.keyDown(event));
    }

    static get DEFAULT_WIDTH_PT() { return 0.75; }

    /** How much a press of the stepper moves the stroke width. Quarter points,
     *  because journal line weights are quoted in them and a whole point is a
     *  visible jump at the widths anyone actually uses. */
    static get WIDTH_STEP_PT() { return 0.25; }

    /** And a whole point for head size, which is measured in tens rather than
     *  in fractions -- a quarter-point step would need forty presses to cross
     *  the range anyone uses. */
    static get HEAD_STEP_PT() { return 1; }

    /** The Edge control's seven values, as one list. Tapering and fading are
     *  ALTERNATIVES rather than things to combine, which is why this is a
     *  select and not two checkboxes: a shaft is a constant-width stroke, a
     *  ribbon that narrows, or a stroke whose opacity ramps. */
    static get EDGES() {
        return [
            ["standard", "Standard"],
            ["taper_start", "Taper start"],
            ["taper_end", "Taper end"],
            ["taper_both", "Taper both ends"],
            ["fade_start", "Fade start"],
            ["fade_end", "Fade end"],
            ["fade_both", "Fade both ends"],
        ];
    }

    get annotation() {
        return this.annotationId
            ? this.state.document.annotations[this.annotationId] : null;
    }

    // -- what it shows -------------------------------------------------------

    update(ids) {
        if (!this.root) return;
        const single = ids.length === 1
            ? this.state.document.annotations[ids[0]] : null;
        // `arrow` as well as `line`: the stored type is superseded, not gone,
        // and every arrow already on a page has to be editable with the same
        // controls that made it.
        const annotation = single && FigureCanvas.isStrokeType(single.type)
            ? single : null;
        if (annotation && annotation.annotation_id !== this.annotationId) {
            this.dismissed = false;
        }
        if (!annotation) {
            this.annotationId = null;
            this.root.innerHTML = "";
            // The palette outlives the panel -- it is in the portal, not in
            // here -- so emptying the panel has to take it down. Clicking
            // elsewhere already would; changing the selection with the keyboard
            // would have left it up, pointed at a line nobody is looking at.
            FigureColorField.close();
            return;
        }
        this.annotationId = annotation.annotation_id;
        this.render();
    }

    /** Whether there is anything here worth the strip. */
    get wants() {
        return Boolean(this.annotationId) && !this.dismissed;
    }

    /** Bring it back after it was shut. */
    reveal() {
        this.dismissed = false;
    }

    render() {
        const annotation = this.annotation;
        if (!annotation) return;
        const style = annotation.style;
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const focus = this.focused();

        const hasStroke = style.line_width_pt > 0;
        const opacity = Math.round(
            (style.opacity === undefined ? 1 : style.opacity) * 100);
        const headSize = style.head_size_pt || 0;

        this.root.innerHTML = `
            <header class="fb-side-heading">
                <span class="fb-side-title">Line</span>
                <button type="button" class="fb-icon-button" data-close="1"
                        title="Close">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                </button>
            </header>

            ${this.field("Stroke", "fb_line_stroke", `
                <input type="checkbox" id="fb_line_stroke_on"
                       data-toggle="stroke" ${hasStroke ? "checked" : ""}
                       aria-label="Draw this line">
                ${FigureColorField.swatch({
                    field: "color", id: "fb_line_stroke", label: "Line color",
                    value: style.color || "#000000", disabled: !hasStroke })}
                <input class="fb-input fb-input-hex" id="fb_line_stroke_hex" type="text"
                       data-hex="color" spellcheck="false" maxlength="7"
                       value="${escape(style.color || "")}" placeholder="None"
                       ${hasStroke ? "" : "disabled"}
                       aria-label="Line color, as a hex code">`)}

            ${this.field("Width", "fb_line_width",
                this.stepper("fb_line_width", "width", style.line_width_pt,
                             hasStroke, "Line width, in points"))}

            ${this.field("Style", null, this.segmented("Line style",
                FigureStrokeGeometry.LINE_STYLES.map((id) => ({
                    value: id, group: "line_style", on: style.line_style === id,
                    icon: FigureLineDefs.styleIcon(id), label: FigureLinePanel.title(id),
                }))))}

            ${this.field("Start head", null, this.headRow("start_head", style.start_head))}
            ${this.field("End head", null, this.headRow("end_head", style.end_head))}

            ${this.field("Head size", "fb_line_head",
                this.stepper("fb_line_head", "head", headSize || "", true,
                             "Head size, in points, or blank for automatic",
                             "Auto"))}

            ${this.field("Edge", "fb_line_edge", `
                <select class="fb-select" id="fb_line_edge" data-edge="1"
                        aria-label="Edge treatment">
                    ${FigureLinePanel.EDGES.map(([value, label]) =>
                        `<option value="${value}"${style.edge === value ? " selected" : ""}
                         >${escape(label)}</option>`).join("")}
                </select>`)}
            ${FigureStrokeGeometry.isTaper(style.edge) && style.line_style !== "solid"
                ? `<p class="fb-side-note">A tapered line is drawn as solid ink,
                   so it has no dashes. Its dash setting is kept for when the
                   edge goes back to standard.</p>` : ""}

            ${this.field("Opacity", "fb_line_opacity", `
                <input class="fb-range" id="fb_line_opacity" type="range"
                       min="0" max="100" step="1" value="${opacity}"
                       data-opacity="1" aria-label="Opacity, as a percentage">
                <span class="fb-range-value" data-opacity-readout="1">${opacity}%</span>`)}`;

        this.restore(focus);
    }

    /** One head row: five ends of the same line, drawn from the same geometry
     *  the canvas draws. A separate icon set would be a second drawing of every
     *  head, and the first time one was adjusted the panel would start lying. */
    headRow(group, current) {
        return this.segmented(group === "start_head" ? "Start head" : "End head",
            FigureStrokeGeometry.HEAD_STYLES.map((id) => ({
                value: id, group: group, on: current === id,
                // The start row's icons are mirrored, so each cell shows the end
                // of the line it actually controls.
                icon: FigureLineDefs.headIcon(id),
                mirror: group === "start_head",
                label: FigureLinePanel.title(id),
            })));
    }

    segmented(label, cells) {
        return `<div class="fb-segmented fb-segmented-icons" role="group"
                     aria-label="${FigureSchema.escapeHtml(label)}">
            ${cells.map((cell) => `<button type="button"
                    class="fb-segmented-button${cell.on ? " is-on" : ""}${
                        cell.mirror ? " is-mirrored" : ""}"
                    data-pick="${cell.group}" data-value="${cell.value}"
                    aria-pressed="${cell.on ? "true" : "false"}"
                    title="${FigureSchema.escapeHtml(cell.label)}"
                    aria-label="${FigureSchema.escapeHtml(cell.label)}"
                >${cell.icon}</button>`).join("")}
        </div>`;
    }

    /** "fade_both" -> "Fade both". Enum ids are the vocabulary and the labels
     *  are derived from them, so a new value cannot arrive without a name. */
    static title(id) {
        const words = String(id).replace(/_/g, " ");
        return words.charAt(0).toUpperCase() + words.slice(1);
    }

    /** One row: the property's name, then its control. */
    field(name, forId, control) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const label = forId
            ? `<label class="fb-field-name" for="${forId}">${escape(name)}</label>`
            : `<span class="fb-field-name">${escape(name)}</span>`;
        return `<div class="fb-field">${label}
            <div class="fb-field-control">${control}</div></div>`;
    }

    /**
     * Minus, the number, plus -- the shape panel's stepper.
     *
     * A TEXT input and not a number one, which is not a detail: `focused()`
     * keeps the caret across the panel's redraw by reading `selectionStart`,
     * and that property is null on a number input. Typing "20" put 02 in the
     * field. `inputmode="decimal"` keeps the numeric keypad on a touch device
     * and Up/Down are handled in `keyDown`.
     */
    stepper(id, kind, value, enabled, label, placeholder) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        return `<div class="fb-stepper">
            <button type="button" class="fb-stepper-button" data-step="-1"
                    data-step-kind="${kind}" ${enabled ? "" : "disabled"}
                    aria-label="Smaller">
                <span class="fas fa-minus" aria-hidden="true"></span></button>
            <input class="fb-stepper-value" id="${id}" type="text"
                   inputmode="decimal" spellcheck="false" data-${kind}="1"
                   value="${escape(String(value))}" ${enabled ? "" : "disabled"}
                   ${placeholder ? `placeholder="${escape(placeholder)}"` : ""}
                   aria-label="${escape(label)}">
            <button type="button" class="fb-stepper-button" data-step="1"
                    data-step-kind="${kind}" ${enabled ? "" : "disabled"}
                    aria-label="Larger">
                <span class="fas fa-plus" aria-hidden="true"></span></button>
        </div>`;
    }

    /** Which control had the keyboard, and where its caret was. The panel
     *  redraws on every document change and every control here causes one, so
     *  without this typing a hex code rebuilds the field under the caret at the
     *  second character. */
    focused() {
        const active = document.activeElement;
        if (!active || !this.root.contains(active) || !active.id) return null;
        let caret = null;
        try {
            if (Number.isInteger(active.selectionStart)) {
                caret = [active.selectionStart, active.selectionEnd];
            }
        } catch (error) { caret = null; }
        return { id: active.id, caret: caret };
    }

    restore(focus) {
        if (!focus) return;
        const again = this.root.querySelector(`#${focus.id}`);
        if (!again) return;
        again.focus();
        if (focus.caret) again.setSelectionRange(focus.caret[0], focus.caret[1]);
    }

    // -- acting --------------------------------------------------------------

    clicked(event) {
        if (event.target.closest?.("[data-close]")) {
            this.dismissed = true;
            this.onClose();
            return;
        }
        const step = event.target.closest?.("[data-step]");
        if (step && !step.disabled) {
            this.step(step.dataset.stepKind, Number(step.dataset.step));
            return;
        }
        const pick = event.target.closest?.("[data-pick]");
        if (pick && !pick.disabled) {
            this.applyStyle({ [pick.dataset.pick]: pick.dataset.value });
            return;
        }
        const well = event.target.closest?.("[data-swatch]");
        if (well && !well.disabled) {
            // The property name, taken now rather than read off the button
            // later: applying a colour redraws this panel, so by the time the
            // second colour of a drag arrives this element is detached.
            const field = well.dataset.swatch;
            FigureColorField.open(well, {
                value: well.dataset.value,
                onPick: (hex) => this.applyStyle({ [field]: hex }),
            });
        }
    }

    keyDown(event) {
        const kind = event.target.dataset?.width ? "width"
            : (event.target.dataset?.head ? "head" : null);
        if (!kind) return;
        const by = { ArrowUp: 1, ArrowDown: -1 }[event.key];
        if (!by) return;
        event.preventDefault();
        this.step(kind, event.shiftKey ? by * 4 : by);
    }

    step(kind, by) {
        if (kind === "head") {
            const input = this.root.querySelector("#fb_line_head");
            const from = parseFloat(input && input.value);
            const base = Number.isFinite(from) ? from : 0;
            // Stepping down off the bottom lands on Auto rather than on a
            // one-point head, which is the setting anyone stepping down is
            // heading for.
            this.setHeadSize(base + by * FigureLinePanel.HEAD_STEP_PT);
            return;
        }
        const input = this.root.querySelector("#fb_line_width");
        const from = parseFloat(input && input.value);
        const base = Number.isFinite(from) ? from : FigureLinePanel.DEFAULT_WIDTH_PT;
        this.setWidth(base + by * FigureLinePanel.WIDTH_STEP_PT);
    }

    changed(event) {
        const target = event.target;
        if (target.dataset.toggle) {
            this.toggle(target.checked);
            return;
        }
        if (target.dataset.hex) {
            // Only a complete code applies. This fires on every keystroke, and
            // "#f" is not a colour -- committing the partial ones would put six
            // entries in the undo history for one decision and repaint the line
            // black on the way past.
            const value = target.value.trim().toLowerCase();
            if (/^#[0-9a-f]{6}$/.test(value)) this.applyStyle({ [target.dataset.hex]: value });
            return;
        }
        if (target.dataset.edge) {
            this.applyStyle({ edge: target.value });
            return;
        }
        if (target.dataset.width || target.dataset.head) {
            // On `change` -- the field being left, or Enter -- and never on
            // `input`. A width is typed a digit at a time and every prefix of
            // it is a valid number, so committing keystrokes sets the line to
            // 2pt on the way to 20 and leaves two entries in the undo history
            // for one decision.
            if (event.type !== "change") return;
            const text = target.value.trim();
            if (target.dataset.head) {
                // Blank, or the word, is Auto -- which is the stored zero. A
                // separate "automatic" checkbox would be a second way to say a
                // value the schema already has a value for.
                if (!text || text.toLowerCase() === "auto") return this.setHeadSize(0);
                const size = parseFloat(text);
                return isFinite(size) ? this.setHeadSize(size) : this.render();
            }
            const value = parseFloat(text);
            // Nothing to commit -- redraw so the field goes back to reading
            // what the line actually is, rather than what was typed over it.
            if (!isFinite(value)) return this.render();
            this.setWidth(value);
            return;
        }
        if (target.dataset.opacity) {
            const readout = this.root.querySelector("[data-opacity-readout]");
            if (readout) readout.textContent = `${target.value}%`;
            // The readout follows the thumb; the DOCUMENT waits for the release.
            // A range fires `input` per pixel of travel, and one commit per
            // pixel is a hundred entries in the undo history and a hundred
            // queued writes for one drag.
            if (event.type !== "change") return;
            this.applyStyle({ opacity: Math.min(1, Math.max(0, Number(target.value) / 100)) });
        }
    }

    /**
     * The stroke, on or off.
     *
     * Off remembers what it was so on can put it back. Without that, turning a
     * line off to look at what is underneath and turning it on again costs the
     * width -- which is the whole reason anyone reaches for the toggle rather
     * than for undo. A stroke of none is a width of zero, which was already
     * legal; there is no second key for it.
     */
    toggle(on) {
        const style = this.annotation?.style;
        if (!style) return;
        if (!on && style.line_width_pt > 0) this.lastWidth = style.line_width_pt;
        this.applyStyle({ line_width_pt: on
            ? (this.lastWidth || FigureLinePanel.DEFAULT_WIDTH_PT) : 0 });
    }

    setWidth(value) {
        const width = Math.min(20, Math.max(0, Math.round(value * 100) / 100));
        if (width > 0) this.lastWidth = width;
        this.applyStyle({ line_width_pt: width });
    }

    setHeadSize(value) {
        const size = Math.min(FigureStrokeGeometry.MAX_HEAD_SIZE_PT,
                              Math.max(0, Math.round(value * 100) / 100));
        this.applyStyle({ head_size_pt: size });
    }

    applyStyle(patch) {
        if (!this.annotationId) return;
        this.onStyle(this.annotationId, { style: patch });
    }
}
