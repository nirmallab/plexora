/**
 * The text sidebar: font, size, colour, spacing, alignment, decoration.
 *
 * CONTEXTUAL, and that is what distinguishes it from the fixed properties
 * column this workspace deliberately removed. It appears while a text object is
 * selected or being typed into and goes away again, so the canvas keeps the
 * full width of the window for everything else anyone does here. The reason the
 * old column went is in `workspace_body.html`: three hundred permanent pixels
 * to show controls that are relevant some of the time.
 *
 * It carries text FORMATTING and nothing else. Arrange, align, group,
 * duplicate, lock and delete apply to every object kind, so they belong to the
 * bar that floats over the selection -- one shared component driven by what the
 * selection can do, rather than a list rebuilt per type.
 *
 * GEOMETRY is on the same side of that line, and it took a second pass to hold
 * it: this panel grew a "Box" group with a Rotation field in it, and the bar's
 * Transform popover has rotation, width and height for every object kind. Two
 * controls for one number, one of which only exists while the object is a
 * caption. Rotation has gone from here. What is left in "Box" is `autofit`,
 * which is not geometry at all -- it says the TEXT decides the height, so it
 * belongs with the text.
 *
 * Which formatting a control changes depends on what is selected, which is the
 * behaviour people expect from every text editor:
 *
 *   * text highlighted inside the editor -> just that range;
 *   * the caret sitting in the editor, or the box merely selected -> the whole
 *     object.
 *
 * A property whose value differs across the selection shows as blank rather
 * than as one of the values it is not.
 */
class FigureTextPanel {

    constructor({ root, canvas, state, editor, onStyle, onClose }) {
        this.root = root;
        this.canvas = canvas;
        this.state = state;
        this.editor = editor;
        this.onStyle = onStyle || (() => {});
        this.onClose = onClose || (() => {});
        this.annotationId = null;
        //: Shut by hand for THIS object. Cleared when the selection moves, so
        //: closing the panel is "not for this caption" rather than "never
        //: again" -- a contextual panel that stayed shut until some other
        //: switch was found would be a feature with no way back on.
        this.dismissed = false;
    }

    setup() {
        if (!this.root) return;
        this.root.addEventListener("input", (event) => this.changed(event));
        this.root.addEventListener("change", (event) => this.changed(event));
        this.root.addEventListener("click", (event) => this.clicked(event));
        this.root.addEventListener("keydown", (event) => this.keyDown(event));
    }

    get annotation() {
        return this.annotationId
            ? this.state.document.annotations[this.annotationId] : null;
    }

    // -- what it shows -------------------------------------------------------

    /**
     * Draw the panel for a selection, or empty it when that is not one text
     * object. Called on every selection change and after every render.
     *
     * It does NOT decide whether it is on screen. Whoever owns the strip beside
     * the rail does, because only one panel can be in it at a time: this one
     * says whether it has anything to show, through `wants`, and the workspace
     * settles it against the tray. Two panels each hiding and showing
     * themselves is how they ended up stacked on top of each other.
     */
    update(ids) {
        if (!this.root) return;
        const single = ids.length === 1
            ? this.state.document.annotations[ids[0]] : null;
        const annotation = single && single.type === "text" ? single : null;
        if (annotation && annotation.annotation_id !== this.annotationId) {
            this.dismissed = false;
        }
        if (!annotation) {
            this.annotationId = null;
            this.root.innerHTML = "";
            // The palette is in the portal, not in this panel, so emptying the
            // panel is not enough to take it off the screen.
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

    /** Bring it back after it was shut -- what double-clicking a caption does,
     *  since typing into one is the moment its formatting is wanted. */
    reveal() {
        this.dismissed = false;
    }

    /**
     * What the controls should read.
     *
     * Taken from the highlighted range when there is one, so the panel is
     * describing what a change would actually affect. `null` for a property the
     * range disagrees about -- which is what makes a mixed selection show a
     * blank box instead of quietly claiming one of the two values.
     */
    current() {
        const annotation = this.annotation;
        const style = annotation.style;
        // What the WHOLE object reads as, when there is no highlighted range to
        // describe instead. The four decoration flags used to be hard-coded
        // false here, which made every one of them a one-way switch: the click
        // handler reads `aria-pressed` to decide whether it is setting the mark
        // or clearing it, and a button that is never pressed can only ever set.
        // Bold went on and would not come off.
        const rich = FigureRichText.normalize(annotation.text || "", annotation.rich);
        const box = FigureRichText.formatOfRange(
            rich, 0, FigureRichText.plainText(rich).length, style) || {
            family: style.font_family, size_pt: style.font_size_pt,
            color: style.color, bold: false, italic: false,
            underline: false, strike: false,
        };
        if (!this.editor?.active || this.editor.annotationId !== this.annotationId) {
            return box;
        }
        const at = this.editor.offsets();
        if (!at || at.start === at.end) return box;
        return FigureRichText.formatOfRange(
            this.editor.rich, at.start, at.end, style) || box;
    }

    /**
     * One property per ROW: its name ranged left, its control ranged right.
     *
     * The panel was built as three titled groups -- FONT, PARAGRAPH, BOX --
     * with the controls stacked underneath each title and only some of them
     * labelled. Two of the eight properties said what they were, and the rest
     * were left to their icons: a number field with nothing beside it, a colour
     * well with nothing beside it, and three segmented tracks of arrows that a
     * reader has to decode before they can tell horizontal alignment from
     * vertical. The group titles were the wrong thing to spend the width on --
     * "FONT" over a font menu says nothing that the menu does not.
     *
     * Every row now names itself, and every control starts at the same left
     * edge, so the panel is scanned down one column of names rather than read.
     * That is what the reference design does and it is the whole of why it is
     * legible at this width.
     */
    render() {
        const annotation = this.annotation;
        const style = annotation.style;
        const now = this.current();
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const focus = this.focused();

        const families = FigureRichText.FAMILIES.map((name) =>
            `<option value="${name}"${now.family === name ? " selected" : ""}
             >${escape(FigureTextPanel.FAMILY_LABELS[name] || name)}</option>`).join("");

        this.root.innerHTML = `
            <header class="fb-side-heading">
                <span class="fb-side-title">Text</span>
                <button type="button" class="fb-icon-button" data-close="1"
                        title="Close">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                </button>
            </header>

            ${this.field("Font", "fb_text_font",
                `<select class="fb-input" id="fb_text_font" data-field="family"
                         aria-label="Font family">${families}</select>`)}

            ${this.field("Size", "fb_text_size", this.stepper(now.size_pt))}

            ${this.field("Color", "fb_text_color", `
                ${FigureColorField.swatch({
                    field: "color", id: "fb_text_color", label: "Text color",
                    value: now.color || "#000000" })}
                <input class="fb-input fb-input-hex" id="fb_text_hex" type="text"
                       data-field="color_hex" spellcheck="false" maxlength="7"
                       value="${escape(now.color || "")}" placeholder="Mixed"
                       aria-label="Color, as a hex code">`)}

            ${this.field("Line spacing", "fb_text_leading",
                this.leadingSelect(style.line_height || FigureRichText.LINE_HEIGHT))}

            ${this.field("Horizontal align", null, this.segmented("Horizontal alignment",
                [["left", "Align left"], ["center", "Center"],
                 ["right", "Align right"], ["justify", "Justify"]].map(([value, title]) =>
                    this.styleButton("align", value, `align-${value}`,
                                     title, style.align === value))))}

            ${this.field("Vertical align", null, this.segmented("Vertical alignment",
                [["top", "arrow-up", "Align to the top"],
                 ["middle", "arrows-up-down", "Center vertically"],
                 ["bottom", "arrow-down", "Align to the bottom"]].map(
                    ([value, icon, title]) => this.styleButton(
                        "valign", value, icon, title, style.valign === value))))}

            ${this.field("Decoration", null, this.segmented("Decoration", [
                // "(⌘B)" and not "Bold  ⌘B": a shortcut inside a tooltip is
                // parenthesised, the same way FigureContextBar.button writes
                // one. Two spaces and a glyph is how a MENU ROW carries it, and
                // a menu row draws the key in its own right-hand column.
                this.toggleButton("bold", "bold", "Bold (⌘B)", now.bold),
                this.toggleButton("italic", "italic", "Italic (⌘I)", now.italic),
                this.toggleButton("underline", "underline", "Underline (⌘U)", now.underline),
                this.toggleButton("strike", "strikethrough", "Strikethrough", now.strike),
            ]))}

            ${this.field("Auto height", "fb_text_autofit",
                `<input type="checkbox" id="fb_text_autofit" data-style="autofit"
                        ${style.autofit ? "checked" : ""}
                        aria-label="Fit the box height to the text">`)}`;

        this.restore(focus);
    }

    /** One row: the property's name, then its control. `forId` is the control
     *  the name belongs to, and is null for the segmented tracks -- a <label>
     *  can only point at one element, and a track is four buttons that carry
     *  their own. */
    field(name, forId, control) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const label = forId
            ? `<label class="fb-field-name" for="${forId}">${escape(name)}</label>`
            : `<span class="fb-field-name">${escape(name)}</span>`;
        return `<div class="fb-field">${label}
            <div class="fb-field-control">${control}</div></div>`;
    }

    /**
     * Minus, the number, plus.
     *
     * A bare number field is a control you have to select and retype to change
     * by one point, and its spin buttons are two arrows four pixels tall that
     * only appear on hover. Type size is the property on this panel most likely
     * to be nudged rather than set, so it gets the buttons the reference design
     * gives it. The field stays editable -- 7.5 is a real answer and no stepper
     * gets to it.
     *
     * A TEXT input and not a number one, which is not a detail: typing "20" put
     * 02 in the field. `focused()` keeps the caret across the panel's redraw by
     * reading `selectionStart`, and that property is null on a number input --
     * the HTML spec makes it null for every type that does not have a "selection
     * API", number among them. So the caret was restored to offset 0 after the
     * first digit and the second went in front of it. The platform's own
     * spinners were already suppressed in CSS, so `type="number"` was buying
     * nothing but that bug; `inputmode="decimal"` keeps the numeric keypad on a
     * touch device, and Up/Down are handled in `keyDown`.
     */
    stepper(value) {
        return `<div class="fb-stepper">
            <button type="button" class="fb-stepper-button" data-step="-1"
                    aria-label="Smaller">
                <span class="fas fa-minus" aria-hidden="true"></span></button>
            <input class="fb-stepper-value" id="fb_text_size" type="text"
                   inputmode="decimal" spellcheck="false" data-field="size_pt"
                   value="${value === null ? "" : value}" placeholder="Mixed"
                   aria-label="Type size, in points">
            <button type="button" class="fb-stepper-button" data-step="1"
                    aria-label="Larger">
                <span class="fas fa-plus" aria-hidden="true"></span></button>
        </div>`;
    }

    /** The usual leadings, plus whatever this caption is actually set to. The
     *  extra option is what keeps a menu from being a value editor that can
     *  silently round: a figure set to 1.35 by an earlier build, or through the
     *  REST surface, still shows 1.35 and still selects it. */
    leadingSelect(current) {
        const values = FigureTextPanel.LEADINGS.slice();
        if (!values.includes(current)) values.push(current);
        values.sort((a, b) => a - b);
        return `<select class="fb-input" id="fb_text_leading" data-style="line_height"
                        aria-label="Line spacing">${values.map((value) =>
            `<option value="${value}"${value === current ? " selected" : ""}
             >${value.toFixed(2).replace(/\.?0+$/, "")}</option>`).join("")}</select>`;
    }

    static get LEADINGS() { return [1, 1.15, 1.2, 1.5, 2]; }

    /**
     * Which control had the keyboard, and where its caret was.
     *
     * The panel redraws on every document change, and a document change is what
     * every control on it causes -- so typing a hex code rebuilt the field
     * under the caret at the second character. Only the id and the caret are
     * kept, because the markup is rebuilt from the model either way; this is
     * about not losing the user's place in it.
     */
    focused() {
        const active = document.activeElement;
        if (!active || !this.root.contains(active) || !active.id) return null;
        let caret = null;
        // Guarded because `selectionStart` is null -- or throws outright -- on
        // any input type without a selection API, and this reads whichever one
        // happens to have the keyboard.
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

    /** A row of buttons that read as one control. Four loose icon buttons and a
     *  segmented track hold the same things; only the second one says that
     *  exactly one of them is the current answer. */
    segmented(label, buttons) {
        return `<div class="fb-segmented" role="group"
                     aria-label="${FigureSchema.escapeHtml(label)}"
                >${buttons.join("")}</div>`;
    }

    toggleButton(mark, icon, title, on) {
        return `<button type="button" class="fb-segmented-button${on ? " is-on" : ""}"
                        data-mark="${mark}" title="${FigureSchema.escapeHtml(title)}"
                        aria-label="${FigureSchema.escapeHtml(title)}"
                        aria-pressed="${on ? "true" : "false"}">
            <span class="fas fa-${icon}" aria-hidden="true"></span></button>`;
    }

    styleButton(field, value, icon, title, on) {
        return `<button type="button" class="fb-segmented-button${on ? " is-on" : ""}"
                        data-style-set="${field}" data-value="${value}"
                        title="${FigureSchema.escapeHtml(title)}"
                        aria-label="${FigureSchema.escapeHtml(title)}"
                        aria-pressed="${on ? "true" : "false"}">
            <span class="fas fa-${icon}" aria-hidden="true"></span></button>`;
    }

    static get FAMILY_LABELS() {
        return {
            "Helvetica": "Helvetica / Arial",
            "Times-Roman": "Times",
            "Courier": "Courier",
        };
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
            // `applyRun`, not `applyStyle`: a colour is a run property here, so
            // it lands on the selected characters when there are any and on the
            // whole caption when there are not -- which is what the hex field
            // beside this well already does.
            FigureColorField.open(well, {
                value: well.dataset.value,
                onPick: (hex) => this.applyRun({ color: hex }),
            });
            return;
        }
        const mark = event.target.closest?.("[data-mark]");
        if (mark) {
            this.applyRun({ [mark.dataset.mark]:
                mark.getAttribute("aria-pressed") === "true" ? null : true });
            return;
        }
        const set = event.target.closest?.("[data-style-set]");
        if (set) this.applyStyle({ [set.dataset.styleSet]: set.dataset.value });
    }

    /**
     * Up and down step the size field.
     *
     * Which is the one thing `type="number"` gave for free and is worth putting
     * back by hand -- a size is adjusted against what it looks like on the page,
     * so holding a key and watching the caption grow is how it is actually set.
     * Shift takes ten at a time, the same multiplier the arrow keys use to nudge
     * an object on the canvas.
     */
    keyDown(event) {
        if (event.target.dataset?.field !== "size_pt") return;
        const by = { ArrowUp: 1, ArrowDown: -1 }[event.key];
        if (!by) return;
        event.preventDefault();
        this.step(event.shiftKey ? by * 10 : by);
    }

    /** One point bigger or smaller, from whatever the field currently reads.
     *  From the FIELD and not from the annotation, because the field is what
     *  shows blank for a selection whose sizes disagree -- and stepping "mixed"
     *  has to start somewhere the user can see. */
    step(by) {
        const input = this.root.querySelector("#fb_text_size");
        const from = parseFloat(input && input.value);
        const base = Number.isFinite(from) ? from : FigureRichText.DEFAULT_SIZE_PT;
        this.applyRun({ size_pt: Math.min(200, Math.max(1, base + by)) });
    }

    changed(event) {
        const target = event.target;
        if (target.dataset.field === "color_hex") {
            // Only a complete code applies. This fires on every keystroke, and
            // "#f" is not a colour -- committing the partial ones would put six
            // entries in the undo history for one colour and repaint the
            // caption black on the way past.
            const value = target.value.trim().toLowerCase();
            if (/^#[0-9a-f]{6}$/.test(value)) this.applyRun({ color: value });
            return;
        }
        if (target.dataset.field === "size_pt") {
            // On `change` -- the field being left, or Enter -- and never on
            // `input`. A size is typed a digit at a time and every prefix of it
            // is a valid number, so committing keystrokes set the caption to
            // 2 pt on the way to 20: a visible flash of tiny type, and two
            // entries in the undo history for one decision. The hex field above
            // has the same shape of guard, and can be stricter because it knows
            // what a finished value looks like.
            if (event.type !== "change") return;
            const value = parseFloat(target.value);
            // Not a number, so there is nothing to commit -- redraw the row so
            // the field goes back to reading what the caption actually is,
            // rather than being left showing what was typed over it.
            if (!isFinite(value)) return this.render();
            this.applyRun({ size_pt: Math.min(200, Math.max(1, value)) });
            return;
        }
        if (target.dataset.field) {
            this.applyRun({ [target.dataset.field]: target.value });
            return;
        }
        if (target.dataset.style) {
            const key = target.dataset.style;
            this.applyStyle({ [key]: key === "autofit"
                ? target.checked : parseFloat(target.value) });
        }
    }

    /**
     * A property that lives on a RUN -- font, size, colour, bold and friends.
     *
     * Routed through the open editor when there is one, because only the editor
     * knows what is highlighted. With no editor open the whole object is the
     * target, and the change goes onto the box's own style so that runs which
     * never overrode it follow along.
     */
    applyRun(patch) {
        const annotation = this.annotation;
        if (!annotation) return;
        if (this.editor?.active && this.editor.annotationId === this.annotationId) {
            this.editor.applyFormat(patch);
            this.render();
            return;
        }
        const style = {};
        if ("family" in patch) style.font_family = patch.family;
        if ("size_pt" in patch) style.font_size_pt = patch.size_pt;
        if ("color" in patch) style.color = patch.color;
        const marks = ["bold", "italic", "underline", "strike"]
            .filter((mark) => mark in patch);
        if (marks.length) {
            // A box-level mark is written onto every run instead of onto the
            // box, so that a run which set it explicitly and one which merely
            // inherited it cannot end up disagreeing after the next edit.
            //
            // The length comes from the NORMALISED rich text, not from
            // `annotation.rich`: a text box saved by an older build has only a
            // flat `text`, `plainText(undefined)` is "", and the range would be
            // [0, 0) -- so bolding a caption that had never been formatted
            // before did nothing at all.
            const rich = FigureRichText.normalize(
                annotation.text || "", annotation.rich);
            const whole = FigureRichText.applyToRange(
                rich, 0, FigureRichText.plainText(rich).length,
                Object.fromEntries(marks.map((mark) => [mark, patch[mark]])));
            this.onStyle(this.annotationId, { rich: whole });
        }
        if (Object.keys(style).length) this.onStyle(this.annotationId, { style: style });
    }

    /** A property that lives on the BOX -- alignment, spacing, autofit. */
    applyStyle(patch) {
        if (!this.annotationId) return;
        this.onStyle(this.annotationId, { style: patch });
    }
}
