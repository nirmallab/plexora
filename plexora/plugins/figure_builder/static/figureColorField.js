/**
 * The one colour control on this canvas: a well, ten presets, then the OS picker.
 *
 * Every colour on a figure used to be a bare `<input type="color">` -- caption
 * colour, shape fill, shape stroke, a line's colour, a legacy rect's fill. Five
 * wells, one gesture, and that gesture was "open the operating system's colour
 * dialog and find this colour again by eye". A figure is a document whose
 * colours REPEAT: the arrow that points at the tumour is the same black as the
 * scale bar, and the box round the inset is the same red as the one on the
 * facing panel. A dialog that starts from wherever it was last left is the
 * wrong tool for choosing a colour that already exists in the figure.
 *
 * So this is the channel list's picker, in the figure: a small palette of
 * colours worth one click, with the dialog kept behind "Custom" for the times
 * an exact value is wanted. Same shape of control, same classes -- the popover
 * is literally `.color-swatch-popover` from viewer.css, which workspace.html
 * already links for Quick Edit's channel rows. A user who has coloured a
 * channel has already learned this.
 *
 * ONE popover for the whole page, held statically, rather than an instance per
 * well. That is not economy -- it is what makes the control survive the panels
 * it sits in. `FigureShapePanel`, `FigureTextPanel` and `FigureContextBar` all
 * redraw by replacing `innerHTML`, and they redraw on every document change --
 * which includes the change this popover just made. An object that owned DOM
 * inside a panel would be torn out from under itself mid-drag by the first
 * colour it applied; `ColorSwatchPicker`, which does own its mount, would be
 * left calling `querySelectorAll` on a popover its own `destroy()` had nulled.
 * Here the panels emit nothing but a button, the popover lives in the portal,
 * and a redraw underneath it is something it never has to hear about.
 *
 * The well is markup, not an object, for the same reason: it is a string in the
 * middle of a template literal, and the panels' existing delegated click
 * handlers route it. Nothing has to be constructed, and nothing has to be
 * destroyed when a panel empties itself.
 */
class FigureColorField {

    /**
     * The palette. Black and white and a grey first, because most marks on a
     * publication figure are one of the three, and then eight hues that stay
     * apart from each other in print and in the colourblind simulations
     * reviewers run. They are the matplotlib category colours rather than the
     * channel list's screen palette: these are inks on a white page, not
     * fluorophores on a black one, and the channel greens and cyans that glow
     * over an image are the first things to disappear against paper.
     */
    static get PRESETS() {
        return [
            { label: "Black", hex: "#000000" },
            { label: "White", hex: "#ffffff" },
            { label: "Grey", hex: "#808080" },
            { label: "Red", hex: "#d62728" },
            { label: "Orange", hex: "#ff7f0e" },
            { label: "Yellow", hex: "#ffd60a" },
            { label: "Green", hex: "#2ca02c" },
            { label: "Teal", hex: "#17becf" },
            { label: "Blue", hex: "#1f77b4" },
            { label: "Purple", hex: "#9467bd" },
        ];
    }

    /**
     * One well, as markup.
     *
     * `field` is both the name of the style property being set and the key the
     * open state is remembered under, so a panel that redraws while the popover
     * is up gets its button back looking pressed.
     */
    static swatch({ field, value, label, disabled, id, block }) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const hex = FigureColorField.hex(value);
        const open = FigureColorField.isOpenFor(field);
        return `<button type="button"
                        class="fb-color-well${block ? " fb-color-well-block" : ""}${open ? " is-open" : ""}"
                        ${id ? `id="${escape(id)}"` : ""}
                        data-swatch="${escape(field)}" data-value="${escape(hex)}"
                        ${disabled ? "disabled" : ""}
                        aria-haspopup="true" aria-expanded="${open ? "true" : "false"}"
                        title="${escape(label)}" aria-label="${escape(label)}"
                        style="--fb-well-color:${escape(hex)}"></button>`;
    }

    /** A colour the markup and the `<input type="color">` will both accept.
     *  Anything else -- an empty fill, a name, a partly typed code -- becomes
     *  black rather than being passed through, because an invalid value silently
     *  resets a colour input to #000000 anyway and doing it here means the well
     *  and the dialog agree about what is showing. */
    static hex(value) {
        const text = String(value === undefined || value === null ? "" : value).trim().toLowerCase();
        return /^#[0-9a-f]{6}$/.test(text) ? text : "#000000";
    }

    /** Whether the popover is up for this field, which is what draws the well
     *  pressed across the redraw its own change caused. */
    static isOpenFor(field) {
        return Boolean(FigureColorField.state && FigureColorField.state.field === field);
    }

    /** Whether a node is inside the popover -- for the other dismiss handlers on
     *  this page, which would otherwise read a click on a swatch as a click
     *  somewhere else and shut themselves. */
    static contains(node) {
        const popover = FigureColorField.state?.popover;
        return Boolean(popover && node && popover.contains(node));
    }

    /**
     * Open the palette against a well, or shut it if that well already has it.
     *
     * `onPick` is called with a hex code for every choice, including each step
     * of a drag inside the OS dialog -- the same live-preview contract the bare
     * colour inputs had, so a caller that was committing on `input` keeps
     * behaving as it did.
     */
    static open(button, { value, onPick }) {
        const field = button.dataset.swatch;
        if (FigureColorField.isOpenFor(field)) {
            FigureColorField.close();
            return;
        }
        FigureColorField.close();

        const popover = document.createElement("div");
        popover.className = "color-swatch-popover fb-color-popover";
        popover.dataset.field = field;
        FigureColorField.state = {
            field: field, popover: popover,
            value: FigureColorField.hex(value === undefined ? button.dataset.value : value),
            onPick: onPick || (() => {}),
        };
        popover.innerHTML = FigureColorField.markup(FigureColorField.state.value);
        popover.addEventListener("click", (event) => FigureColorField.clicked(event));
        popover.addEventListener("input", (event) => FigureColorField.custom(event));

        FigureColorField.attach(popover);
        FigureColorField.place(popover, button);
        button.classList.add("is-open");
        button.setAttribute("aria-expanded", "true");
        requestAnimationFrame(() => popover.classList.add("is-open"));
        FigureColorField.listen();
    }

    static markup(value) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const chips = FigureColorField.PRESETS.map((preset) =>
            `<button type="button" class="color-swatch-option${
                preset.hex === value ? " is-selected" : ""}"
                     data-pick="${escape(preset.hex)}" title="${escape(preset.label)}"
                     aria-label="${escape(preset.label)}"
                     style="--swatch-color:${escape(preset.hex)}"></button>`).join("");
        return `<div class="color-swatch-grid">${chips}</div>
            <label class="color-swatch-custom-row">
                <span>Custom</span>
                <input type="color" data-custom="1" value="${escape(value)}"
                       aria-label="Custom color">
            </label>`;
    }

    /** Through the portal rather than onto `<body>`: the workspace has a
     *  full-screen mode, and the Fullscreen API paints an opaque backdrop over
     *  everything outside the element being shown -- see popoverPortal.js. The
     *  fallback is for the pages that load this plugin without core's views. */
    static attach(popover) {
        if (typeof PopoverPortal === "undefined") document.body.appendChild(popover);
        else PopoverPortal.attach(popover);
    }

    static detach(popover) {
        if (typeof PopoverPortal === "undefined") popover.remove();
        else PopoverPortal.detach(popover);
    }

    /** Under the well, and flipped above it near the bottom of the window. The
     *  popover is `position: fixed`, so these are viewport coordinates and no
     *  scroll offset comes into it. */
    static place(popover, button) {
        const box = button.getBoundingClientRect();
        // Measured before the opening transform lands: a client rect would
        // report the scaled box rather than the one it settles at.
        const width = popover.offsetWidth;
        const height = popover.offsetHeight;
        let top = box.bottom + 6;
        if (top + height > window.innerHeight - 8) {
            top = Math.max(8, box.top - height - 6);
        }
        popover.style.left = Math.round(Math.max(8,
            Math.min(box.left, window.innerWidth - width - 8))) + "px";
        popover.style.top = Math.round(top) + "px";
    }

    static clicked(event) {
        const chip = event.target.closest("[data-pick]");
        if (!chip) return;
        FigureColorField.pick(chip.dataset.pick);
        FigureColorField.close();
    }

    /** The OS dialog, which reports every colour the pointer passes over. Kept
     *  open, so the drag can be watched on the figure itself. */
    static custom(event) {
        if (!event.target.dataset?.custom) return;
        FigureColorField.pick(event.target.value);
    }

    static pick(value) {
        const state = FigureColorField.state;
        if (!state) return;
        const hex = FigureColorField.hex(value);
        state.value = hex;
        state.popover.querySelectorAll("[data-pick]").forEach((chip) => {
            chip.classList.toggle("is-selected", chip.dataset.pick === hex);
        });
        // The well itself, and not only through whatever redraw `onPick`
        // causes. The sidebar panels rebuild their markup on every document
        // change and would repaint it anyway; the context bar's popover does
        // not, and its well would go on showing the colour it started with.
        FigureColorField.wells(state.field).forEach((well) => {
            well.dataset.value = hex;
            well.style.setProperty("--fb-well-color", hex);
        });
        state.onPick(hex);
    }

    static close() {
        const state = FigureColorField.state;
        FigureColorField.state = null;
        FigureColorField.deafen();
        if (!state) return;
        FigureColorField.detach(state.popover);
        FigureColorField.wells(state.field).forEach((well) => {
            well.classList.remove("is-open");
            well.setAttribute("aria-expanded", "false");
        });
    }

    /** Every well on the page for a field, rather than one remembered element:
     *  the panel underneath may have redrawn since the palette opened, in which
     *  case the button it was anchored to is not the one on screen any more. */
    static wells(field) {
        return Array.from(
            document.querySelectorAll(`[data-swatch="${CSS.escape(field)}"]`));
    }

    /**
     * Dismissal.
     *
     * Bound a turn late, on purpose. `open()` is always called from a click on
     * the well, and that click is still on its way up to the document -- a
     * listener attached now would receive it and shut the popover in the same
     * gesture that asked for it.
     */
    static listen() {
        const state = FigureColorField.state;
        FigureColorField._closeKey = (event) => {
            if (event.key === "Escape") FigureColorField.close();
        };
        FigureColorField._closeScroll = (event) => {
            if (FigureColorField.contains(event.target)) return;
            FigureColorField.close();
        };
        document.addEventListener("keydown", FigureColorField._closeKey);
        window.addEventListener("scroll", FigureColorField._closeScroll, true);
        window.addEventListener("resize", FigureColorField._closeScroll);
        window.setTimeout(() => {
            if (FigureColorField.state !== state) return;
            FigureColorField._closeClick = (event) => {
                if (FigureColorField.contains(event.target)) return;
                FigureColorField.close();
            };
            document.addEventListener("click", FigureColorField._closeClick);
        }, 0);
    }

    static deafen() {
        if (FigureColorField._closeKey) {
            document.removeEventListener("keydown", FigureColorField._closeKey);
            window.removeEventListener("scroll", FigureColorField._closeScroll, true);
            window.removeEventListener("resize", FigureColorField._closeScroll);
            FigureColorField._closeKey = null;
            FigureColorField._closeScroll = null;
        }
        if (FigureColorField._closeClick) {
            document.removeEventListener("click", FigureColorField._closeClick);
            FigureColorField._closeClick = null;
        }
    }
}

//: The open popover, or null. Static because there is one for the page -- see
//: the note at the top about panels that redraw underneath it.
FigureColorField.state = null;
FigureColorField._closeKey = null;
FigureColorField._closeScroll = null;
FigureColorField._closeClick = null;
