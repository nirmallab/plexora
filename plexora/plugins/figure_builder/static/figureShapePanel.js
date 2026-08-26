/**
 * The shape sidebar: fill, stroke, width, opacity -- and the point tools while
 * Edit Points is open.
 *
 * CONTEXTUAL, in the same strip as the text panel and by the same argument: it
 * appears while a shape is selected and goes away again, so the canvas keeps the
 * width of the window for everything else. `FigureWorkspace.SIDEBARS` names the
 * three, and `contextSidebar` settles which of them has the strip -- a panel
 * that showed and hid itself is how two of them ended up stacked.
 *
 * It carries shape STYLING and nothing else, which is the same line the text
 * panel draws. Arrange, align, duplicate and delete apply to every object kind
 * and belong to the floating bar; entering Edit Points is a command and belongs
 * there too. What lands here is what only a shape has.
 *
 * One consequence worth stating: the bar's Stroke and Fill popovers no longer
 * apply to shapes. Two controls for one number, in two places, disagreeing
 * about which is authoritative, is the failure the text panel already avoided
 * by moving formatting off the bar. So `figureActions.js` narrows them, and the
 * legacy `rect`/`ellipse` annotations -- which have no panel and are no longer
 * creatable -- keep them.
 *
 * "None" is not a separate stored value in either row. A fill of none is the
 * empty-string fill the schema already has, and a stroke of none is a width of
 * zero, which was already legal. The panel remembers what was switched off so
 * switching it back on returns the colour rather than a default -- but that
 * memory lives HERE, in the session, not in the document. A document that
 * stored "the fill it would have if it had one" would be storing a colour
 * nothing draws, and every renderer would have to know to ignore it.
 */
class FigureShapePanel {

    constructor({ root, canvas, state, onStyle, onClose }) {
        this.root = root;
        this.canvas = canvas;
        this.state = state;
        this.onStyle = onStyle || (() => {});
        this.onClose = onClose || (() => {});
        this.annotationId = null;
        //: Shut by hand for THIS shape. Cleared when the selection moves, so
        //: closing the panel is "not for this one" rather than "never again".
        this.dismissed = false;
        //: What the toggles put back. Session-only, deliberately -- see above.
        this.lastFill = "#ffffff";
        this.lastWidth = FigureShapePanel.DEFAULT_WIDTH_PT;
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

    get annotation() {
        return this.annotationId
            ? this.state.document.annotations[this.annotationId] : null;
    }

    /** The point editor, but only while it has THIS shape open. */
    get points() {
        const editor = this.canvas?.pointEditor;
        return editor && editor.active && editor.annotationId === this.annotationId
            ? editor : null;
    }

    // -- what it shows -------------------------------------------------------

    update(ids) {
        if (!this.root) return;
        const single = ids.length === 1
            ? this.state.document.annotations[ids[0]] : null;
        const annotation = single && single.type === "shape" ? single : null;
        if (annotation && annotation.annotation_id !== this.annotationId) {
            this.dismissed = false;
        }
        if (!annotation) {
            this.annotationId = null;
            this.root.innerHTML = "";
            // The palette outlives the panel -- it is in the portal, not in
            // here -- so emptying the panel has to take it down. Clicking
            // elsewhere already would; changing the selection with the keyboard
            // would have left it up, pointed at a shape nobody is looking at.
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

    /** Bring it back after it was shut -- what entering Edit Points does, since
     *  that is the moment the point tools are wanted. */
    reveal() {
        this.dismissed = false;
    }

    render() {
        const annotation = this.annotation;
        if (!annotation) return;
        const style = annotation.style;
        const shape = annotation.shape || { closed: true };
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const focus = this.focused();

        const hasFill = Boolean(style.fill);
        const hasStroke = style.line_width_pt > 0;
        const opacity = Math.round(
            (style.opacity === undefined ? 1 : style.opacity) * 100);

        this.root.innerHTML = `
            <header class="fb-side-heading">
                <span class="fb-side-title">Shape</span>
                <button type="button" class="fb-icon-button" data-close="1"
                        title="Close">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                </button>
            </header>

            ${this.field("Fill", "fb_shape_fill", `
                <input type="checkbox" id="fb_shape_fill_on"
                       data-toggle="fill" ${hasFill ? "checked" : ""}
                       aria-label="Fill this shape">
                ${FigureColorField.swatch({
                    field: "fill", id: "fb_shape_fill", label: "Fill color",
                    value: style.fill || this.lastFill, disabled: !hasFill })}
                <input class="fb-input fb-input-hex" id="fb_shape_fill_hex" type="text"
                       data-hex="fill" spellcheck="false" maxlength="7"
                       value="${escape(style.fill || "")}" placeholder="None"
                       ${hasFill ? "" : "disabled"}
                       aria-label="Fill color, as a hex code">`)}
            ${shape.closed ? "" : `<p class="fb-side-note">A fill is drawn only
                when the path is closed. This one keeps its colour for when it
                is.</p>`}

            ${this.field("Stroke", "fb_shape_stroke", `
                <input type="checkbox" id="fb_shape_stroke_on"
                       data-toggle="stroke" ${hasStroke ? "checked" : ""}
                       aria-label="Outline this shape">
                ${FigureColorField.swatch({
                    field: "color", id: "fb_shape_stroke", label: "Stroke color",
                    value: style.color || "#000000", disabled: !hasStroke })}
                <input class="fb-input fb-input-hex" id="fb_shape_stroke_hex" type="text"
                       data-hex="color" spellcheck="false" maxlength="7"
                       value="${escape(style.color || "")}" placeholder="None"
                       ${hasStroke ? "" : "disabled"}
                       aria-label="Stroke color, as a hex code">`)}

            ${this.field("Stroke width", "fb_shape_width",
                this.widthStepper(style.line_width_pt, hasStroke))}

            ${this.field("Opacity", "fb_shape_opacity", `
                <input class="fb-range" id="fb_shape_opacity" type="range"
                       min="0" max="100" step="1" value="${opacity}"
                       data-opacity="1" aria-label="Opacity, as a percentage">
                <span class="fb-range-value" data-opacity-readout="1">${opacity}%</span>`)}

            ${this.pointsSection()}`;

        this.restore(focus);
    }

    /**
     * The point tools, when Edit Points has this shape open.
     *
     * In the panel rather than replacing the floating bar. The bar is driven by
     * `figureActions.js` and is about what applies to a SELECTION; these are
     * about what applies to the nodes selected inside one object, which is a
     * different question with a different answer for every press. Putting them
     * here also means the styling rows stay reachable while the points are
     * being edited, which is when the fill usually turns out to be wrong.
     */
    pointsSection() {
        const points = this.points;
        if (!points) return "";
        const type = points.selectedType;
        return `<div class="fb-side-group">
            <div class="fb-side-group-title">Points</div>
            ${this.field("Node type", null, `<div class="fb-segmented" role="group"
                     aria-label="Node type">
                ${this.pointButton("corner", "Corner", type === "corner", !points.selectedCount)}
                ${this.pointButton("smooth", "Smooth", type === "smooth", !points.selectedCount)}
            </div>`)}
            ${this.field("Selection", null, `
                <button type="button" class="fb-button" data-point="delete"
                        ${points.canDelete ? "" : "disabled"}>Delete point</button>`)}
            ${this.field("Path", null, `
                <button type="button" class="fb-button" data-point="closed"
                        ${points.canToggleClosed ? "" : "disabled"}
                >${points.closed ? "Open path" : "Close path"}</button>`)}
            <p class="fb-side-note">Click a segment to add a point. Drag a node to
               move it, or its levers to bend the curve either side.</p>
            <div class="fb-side-actions">
                <button type="button" class="fb-button fb-button-primary"
                        data-point="done">Done</button>
            </div>
        </div>`;
    }

    pointButton(value, label, on, disabled) {
        return `<button type="button" class="fb-segmented-button${on ? " is-on" : ""}"
                        data-point="${value}" ${disabled ? "disabled" : ""}
                        aria-pressed="${on ? "true" : "false"}"
                >${FigureSchema.escapeHtml(label)}</button>`;
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
     * Minus, the number, plus -- the text panel's stepper, in quarter points.
     *
     * A TEXT input and not a number one, which is not a detail: `focused()`
     * keeps the caret across the panel's redraw by reading `selectionStart`,
     * and that property is null on a number input. Typing "20" put 02 in the
     * field. `inputmode="decimal"` keeps the numeric keypad on a touch device
     * and Up/Down are handled in `keyDown`.
     */
    widthStepper(value, enabled) {
        return `<div class="fb-stepper">
            <button type="button" class="fb-stepper-button" data-step="-1"
                    ${enabled ? "" : "disabled"} aria-label="Thinner">
                <span class="fas fa-minus" aria-hidden="true"></span></button>
            <input class="fb-stepper-value" id="fb_shape_width" type="text"
                   inputmode="decimal" spellcheck="false" data-width="1"
                   value="${value}" ${enabled ? "" : "disabled"}
                   aria-label="Stroke width, in points">
            <button type="button" class="fb-stepper-button" data-step="1"
                    ${enabled ? "" : "disabled"} aria-label="Thicker">
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
        if (step) {
            this.step(Number(step.dataset.step));
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
            return;
        }
        const point = event.target.closest?.("[data-point]");
        if (point && !point.disabled) this.point(point.dataset.point);
    }

    point(action) {
        const points = this.points;
        if (!points) return;
        if (action === "delete") points.deleteSelected();
        else if (action === "closed") points.toggleClosed();
        else if (action === "done") points.exit();
        else points.setType(action);
    }

    keyDown(event) {
        if (!event.target.dataset?.width) return;
        const by = { ArrowUp: 1, ArrowDown: -1 }[event.key];
        if (!by) return;
        event.preventDefault();
        this.step(event.shiftKey ? by * 4 : by);
    }

    /** A quarter point thicker or thinner, from whatever the field reads. */
    step(by) {
        const input = this.root.querySelector("#fb_shape_width");
        const from = parseFloat(input && input.value);
        const base = Number.isFinite(from) ? from : FigureShapePanel.DEFAULT_WIDTH_PT;
        this.setWidth(base + by * FigureShapePanel.WIDTH_STEP_PT);
    }

    changed(event) {
        const target = event.target;
        const toggle = target.dataset.toggle;
        if (toggle) {
            this.toggle(toggle, target.checked);
            return;
        }
        if (target.dataset.hex) {
            // Only a complete code applies. This fires on every keystroke, and
            // "#f" is not a colour -- committing the partial ones would put six
            // entries in the undo history for one decision and repaint the
            // shape black on the way past.
            const value = target.value.trim().toLowerCase();
            if (/^#[0-9a-f]{6}$/.test(value)) this.applyStyle({ [target.dataset.hex]: value });
            return;
        }
        if (target.dataset.width) {
            // On `change` -- the field being left, or Enter -- and never on
            // `input`. A width is typed a digit at a time and every prefix of
            // it is a valid number, so committing keystrokes sets the outline
            // to 2pt on the way to 20 and leaves two entries in the undo
            // history for one decision.
            if (event.type !== "change") return;
            const value = parseFloat(target.value);
            // Nothing to commit -- redraw so the field goes back to reading
            // what the shape actually is, rather than what was typed over it.
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
     * Fill or stroke, on or off.
     *
     * Off remembers what it was so on can put it back. Without that, turning a
     * fill off to look at what is underneath and turning it on again costs the
     * colour -- which is the whole reason anyone reaches for the toggle rather
     * than for undo.
     */
    toggle(which, on) {
        const style = this.annotation?.style;
        if (!style) return;
        if (which === "fill") {
            if (!on && style.fill) this.lastFill = style.fill;
            this.applyStyle({ fill: on ? (this.lastFill || "#ffffff") : "" });
            return;
        }
        if (!on && style.line_width_pt > 0) this.lastWidth = style.line_width_pt;
        this.applyStyle({ line_width_pt: on
            ? (this.lastWidth || FigureShapePanel.DEFAULT_WIDTH_PT) : 0 });
    }

    setWidth(value) {
        const width = Math.min(20, Math.max(0, Math.round(value * 100) / 100));
        if (width > 0) this.lastWidth = width;
        this.applyStyle({ line_width_pt: width });
    }

    applyStyle(patch) {
        if (!this.annotationId) return;
        this.onStyle(this.annotationId, { style: patch });
    }
}
