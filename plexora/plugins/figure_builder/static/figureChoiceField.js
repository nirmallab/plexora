/**
 * One small button, a list or a keypad behind it: the sidebar's compact picker.
 *
 * ## Why the controls it replaces had to shrink
 *
 * The image sidebar's rows now carry five or six controls each -- a length, its
 * unit, where it sits, what colour it is, and whether it is on -- because that
 * IS one decision and splitting it down a column made the user scroll through
 * their own scale bar. Two of those controls could not be built from a native
 * element at that size:
 *
 *   * the NINE ANCHORS were a 78px keypad three rows tall, rendered inline
 *     wherever a corner was chosen -- five of them down one column was most of
 *     the panel's height spent on a choice that is made once and left alone;
 *
 *   * a UNIT has to read as "µm" when it is closed and as "Micrometres (µm)"
 *     when it is open, and a `<select>` shows the selected option's own text in
 *     both states. Naming them "µm" makes an unlabelled list of symbols; naming
 *     them in full makes a 140px control in a 308px row.
 *
 * So both are a button that states the current answer in as few characters as
 * the answer has, and a popover that states it in full.
 *
 * ## One class, two layouts
 *
 * `list` is a column of options; `grid` is the nine anchors as a picture of the
 * panel, which is the whole reason the anchors were ever a keypad -- nine words
 * in a list have to be read and then mapped onto the image, and a 3x3 grid does
 * not have to be read at all. The arrow keys walk the grid without wrapping
 * onto the row above: it is a picture of the panel, and "left of the top-left
 * corner" is not the bottom-right one.
 *
 * ## One popover for the page, held statically
 *
 * Exactly as `FigureColorField` does, and for exactly the same reason: the
 * sidebars redraw by replacing innerHTML, they redraw on every document change,
 * and that includes the change this popover just made. An object owning DOM
 * inside a panel would be torn out from under itself by the first choice it
 * applied. Here the panel emits a button, the popover lives in the portal, and
 * a redraw underneath it is something it never has to hear about.
 */
class FigureChoiceField {

    /** Arrows rather than words: the cell says which corner without being read. */
    static get ANCHOR_GLYPHS() {
        return {
            top_left: "↖", top_center: "↑", top_right: "↗",
            middle_left: "←", center: "•", middle_right: "→",
            bottom_left: "↙", bottom_center: "↓", bottom_right: "↘",
        };
    }

    static anchorName(anchor) {
        return String(anchor || "").replace(/_/g, " ")
            .replace(/^./, (c) => c.toUpperCase());
    }

    /** The nine anchors as options. `short` is what the button shows. */
    static anchorOptions() {
        return FigureSchema.PANEL_ANCHORS.map((anchor) => ({
            value: anchor,
            short: FigureChoiceField.ANCHOR_GLYPHS[anchor],
            name: FigureChoiceField.anchorName(anchor),
        }));
    }

    /** The option a value names, or the first -- a stored value the schema does
     *  not know is still a value, and it must not leave the button blank. */
    static option(options, value) {
        const list = options || [];
        return list.find((entry) => entry.value === value) || list[0] || null;
    }

    /**
     * The button, as markup.
     *
     * `field` is both what is being chosen and the key the open state is
     * remembered under, so a panel that redraws while the popover is up gets its
     * button back looking pressed.
     *
     * ## Which of these draws a chevron
     *
     * The LIST does and the GRID does not. A chevron says "there is more behind
     * this than you can see", which is true of "µm" -- the closed unit button
     * shows one symbol out of five and nothing else about it says so. It is not
     * true of "↘": the arrow is already a picture of a thing being chosen, the
     * button is 34px, and at that size the chevron beside the arrow read as a
     * second arrow pointing somewhere else. Five of them down a panel is ten
     * arrows for five answers.
     *
     * `variant` is how the same control wears a different skin without becoming
     * a second class. "suffix" is the scale bar's unit, drawn INSIDE the length
     * field where "mm" and "pt" are printed on the fields either side of it --
     * the row it has to fit in already carries four controls and a fifth box for
     * "µm" is the one that would not go in. What it does is unchanged, which is
     * the point: one popover, one open-state key, one list of the units.
     */
    static button({ field, value, options, label, layout, disabled, id,
                    variant }) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const grid = layout === "grid";
        const chosen = FigureChoiceField.option(options, value);
        const open = FigureChoiceField.isOpenFor(field);
        const name = chosen ? `${label || "Choose"}: ${chosen.name}` : (label || "Choose");
        const classes = ["fb-choice-well"];
        if (grid) classes.push("fb-choice-grid");
        if (variant) classes.push(`fb-choice-${variant}`);
        if (open) classes.push("is-open");
        return `<button type="button"
                        class="${classes.join(" ")}"
                        ${id ? `id="${escape(id)}"` : ""}
                        data-choice="${escape(field)}"
                        data-value="${escape(chosen ? chosen.value : "")}"
                        ${disabled ? "disabled" : ""}
                        aria-haspopup="true" aria-expanded="${open ? "true" : "false"}"
                        title="${escape(name)}" aria-label="${escape(name)}">
            ${chosen ? `<span class="fb-choice-text" aria-hidden="true"
                >${escape(chosen.short)}</span>` : ""}
            ${grid ? "" : `<span class="fas fa-chevron-down fb-choice-caret"
                                 aria-hidden="true"></span>`}
        </button>`;
    }

    static isOpenFor(field) {
        return Boolean(FigureChoiceField.state && FigureChoiceField.state.field === field);
    }

    /** Whether a node is inside the popover -- for the other dismiss handlers on
     *  this page, which would otherwise read a click in it as a click somewhere
     *  else and shut themselves. */
    static contains(node) {
        const popover = FigureChoiceField.state?.popover;
        return Boolean(popover && node && popover.contains(node));
    }

    /**
     * Open against a button, or shut it if that button already has it.
     *
     * `keepFocus` is for a trigger that is a TEXT FIELD -- the Labels row,
     * where the same box is both "type a label" and "or take one from the
     * image". Taking the keyboard off it on open would mean the click that
     * asked for the list also stopped the user typing, which is the one thing
     * that box must never do.
     */
    static open(button, { value, options, layout, onPick, keepFocus }) {
        const field = button.dataset.choice;
        if (FigureChoiceField.isOpenFor(field)) {
            FigureChoiceField.close();
            return;
        }
        FigureChoiceField.close();

        const popover = document.createElement("div");
        popover.className = "color-swatch-popover fb-choice-popover";
        popover.dataset.field = field;
        FigureChoiceField.state = {
            field: field, popover: popover, options: options || [],
            layout: layout === "grid" ? "grid" : "list",
            value: value === undefined ? button.dataset.value : value,
            onPick: onPick || (() => {}),
        };
        popover.innerHTML = FigureChoiceField.markup(FigureChoiceField.state);
        popover.addEventListener("click", (event) => FigureChoiceField.clicked(event));
        popover.addEventListener("keydown", (event) => FigureChoiceField.keyDown(event));

        FigureChoiceField.attach(popover);
        FigureChoiceField.place(popover, button);
        button.classList.add("is-open");
        button.setAttribute("aria-expanded", "true");
        requestAnimationFrame(() => {
            popover.classList.add("is-open");
            if (!keepFocus) popover.querySelector(".is-on")?.focus();
        });
        FigureChoiceField.listen();
    }

    /** Put the keyboard on the list's first option. The way IN for a popover
     *  opened with `keepFocus`: the field keeps the keyboard, so ArrowDown is
     *  the only route to the list, exactly as it is for any other combobox. */
    static first() {
        FigureChoiceField.state?.popover
            ?.querySelector("[data-choice-value]")?.focus();
    }

    /**
     * Nine cells as a RADIOGROUP, a column as a LISTBOX.
     *
     * Not nine independent toggles: `aria-pressed` on each of them says "this
     * button is pressed in" nine times over, with nothing tying them together or
     * saying that exactly one is the answer. With `role="radio"` a screen reader
     * announces "top left, 1 of 9".
     */
    static markup(state) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        if (state.layout === "grid") {
            const cells = state.options.map((entry) => {
                const on = entry.value === state.value;
                return `<button type="button" class="fb-anchor-cell${on ? " is-on" : ""}"
                        data-choice-value="${escape(entry.value)}" role="radio"
                        aria-checked="${on ? "true" : "false"}"
                        tabindex="${on ? "0" : "-1"}"
                        title="${escape(entry.name)}" aria-label="${escape(entry.name)}"
                    >${escape(entry.short)}</button>`;
            }).join("");
            const chosen = FigureChoiceField.option(state.options, state.value);
            return `<div class="fb-anchor-grid" role="radiogroup"
                         aria-label="Location">${cells}</div>
                <p class="fb-choice-name">${escape(chosen ? chosen.name : "")}</p>`;
        }
        const rows = state.options.map((entry) => {
            const on = entry.value === state.value;
            return `<button type="button" class="fb-choice-option${on ? " is-on" : ""}"
                    data-choice-value="${escape(entry.value)}" role="option"
                    aria-selected="${on ? "true" : "false"}"
                >${escape(entry.name)}</button>`;
        }).join("");
        return `<div class="fb-choice-list" role="listbox">${rows}</div>`;
    }

    /** Through the portal rather than onto `<body>`: the workspace has a
     *  full-screen mode, and the Fullscreen API paints an opaque backdrop over
     *  everything outside the element being shown -- see popoverPortal.js. */
    static attach(popover) {
        if (typeof PopoverPortal === "undefined") document.body.appendChild(popover);
        else PopoverPortal.attach(popover);
    }

    static detach(popover) {
        if (typeof PopoverPortal === "undefined") popover.remove();
        else PopoverPortal.detach(popover);
    }

    /** Under the button, flipped above it near the bottom of the window. The
     *  popover is `position: fixed`, so these are viewport coordinates and no
     *  scroll offset comes into it. */
    static place(popover, button) {
        const box = button.getBoundingClientRect();
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
        const cell = event.target.closest("[data-choice-value]");
        if (!cell || cell.disabled) return;
        FigureChoiceField.pick(cell.dataset.choiceValue);
        FigureChoiceField.close();
    }

    /**
     * The arrow keys.
     *
     * Moving and choosing are the same act in the grid, which is what a
     * radiogroup does natively. Left and right do not wrap onto the row above or
     * below: the grid is a picture of the panel.
     */
    static keyDown(event) {
        const state = FigureChoiceField.state;
        const cell = event.target.closest?.("[data-choice-value]");
        if (!state || !cell) return;
        const grid = state.layout === "grid";
        const step = grid
            ? { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -3, ArrowDown: 3 }[event.key]
            : { ArrowUp: -1, ArrowDown: 1 }[event.key];
        if (step === undefined) return;
        const values = state.options.map((entry) => entry.value);
        const from = values.indexOf(cell.dataset.choiceValue);
        const to = from + step;
        if (from < 0 || to < 0 || to >= values.length) return;
        if (grid && Math.abs(step) === 1
                && Math.floor(to / 3) !== Math.floor(from / 3)) return;
        event.preventDefault();
        if (grid) {
            FigureChoiceField.pick(values[to]);
            state.popover.querySelector(".is-on")?.focus();
            return;
        }
        // A list only MOVES on the arrows; Enter or a click is what chooses,
        // because walking past four units must not restyle four bars on the way.
        state.popover.querySelectorAll("[data-choice-value]")[to]?.focus();
    }

    static pick(value) {
        const state = FigureChoiceField.state;
        if (!state) return;
        state.value = value;
        const chosen = FigureChoiceField.option(state.options, value);
        state.popover.querySelectorAll("[data-choice-value]").forEach((cell) => {
            const on = cell.dataset.choiceValue === value;
            cell.classList.toggle("is-on", on);
            if (cell.getAttribute("role") === "radio") {
                cell.setAttribute("aria-checked", on ? "true" : "false");
                cell.tabIndex = on ? 0 : -1;
            } else {
                cell.setAttribute("aria-selected", on ? "true" : "false");
            }
        });
        const name = state.popover.querySelector(".fb-choice-name");
        if (name && chosen) name.textContent = chosen.name;
        // The button itself, and not only through whatever redraw `onPick`
        // causes: the sidebars repaint it anyway, the floating bar does not.
        FigureChoiceField.fields(state.field).forEach((button) => {
            button.dataset.value = value;
            const text = button.querySelector(".fb-choice-text");
            if (text && chosen) text.textContent = chosen.short;
        });
        state.onPick(value);
    }

    static close() {
        const state = FigureChoiceField.state;
        FigureChoiceField.state = null;
        FigureChoiceField.deafen();
        if (!state) return;
        FigureChoiceField.detach(state.popover);
        FigureChoiceField.fields(state.field).forEach((button) => {
            button.classList.remove("is-open");
            button.setAttribute("aria-expanded", "false");
        });
    }

    /** Every button on the page for a field, rather than one remembered element:
     *  the panel underneath may have redrawn since the popover opened, in which
     *  case the button it was anchored to is not the one on screen any more. */
    static fields(field) {
        return Array.from(
            document.querySelectorAll(`[data-choice="${CSS.escape(field)}"]`));
    }

    /**
     * Dismissal.
     *
     * Bound a turn late, on purpose. `open()` is always called from a click on
     * the button, and that click is still on its way up to the document -- a
     * listener attached now would receive it and shut the popover in the same
     * gesture that asked for it.
     */
    static listen() {
        const state = FigureChoiceField.state;
        FigureChoiceField._closeKey = (event) => {
            if (event.key === "Escape") FigureChoiceField.close();
        };
        FigureChoiceField._closeScroll = (event) => {
            if (FigureChoiceField.contains(event.target)) return;
            FigureChoiceField.close();
        };
        document.addEventListener("keydown", FigureChoiceField._closeKey);
        window.addEventListener("scroll", FigureChoiceField._closeScroll, true);
        window.addEventListener("resize", FigureChoiceField._closeScroll);
        window.setTimeout(() => {
            if (FigureChoiceField.state !== state) return;
            FigureChoiceField._closeClick = (event) => {
                if (FigureChoiceField.contains(event.target)) return;
                FigureChoiceField.close();
            };
            document.addEventListener("click", FigureChoiceField._closeClick);
        }, 0);
    }

    static deafen() {
        if (FigureChoiceField._closeKey) {
            document.removeEventListener("keydown", FigureChoiceField._closeKey);
            window.removeEventListener("scroll", FigureChoiceField._closeScroll, true);
            window.removeEventListener("resize", FigureChoiceField._closeScroll);
            FigureChoiceField._closeKey = null;
            FigureChoiceField._closeScroll = null;
        }
        if (FigureChoiceField._closeClick) {
            document.removeEventListener("click", FigureChoiceField._closeClick);
            FigureChoiceField._closeClick = null;
        }
    }
}

//: The open popover, or null. Static because there is one for the page -- see
//: the note at the top about panels that redraw underneath it.
FigureChoiceField.state = null;
FigureChoiceField._closeKey = null;
FigureChoiceField._closeScroll = null;
FigureChoiceField._closeClick = null;
