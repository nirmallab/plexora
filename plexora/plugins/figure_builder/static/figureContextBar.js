/**
 * FigureContextBar - the controls for whatever is selected, beside it.
 *
 * Figure Builder used to carry a 280px properties column down the right-hand
 * side of the page. It was permanent and it was empty most of the time: the
 * controls in it act on a selection, and the usual state of a figure being
 * arranged is that nothing is selected. So it has gone, and this took its
 * place -- a small bar that appears above what it acts on and nowhere else.
 *
 * ## Why it lives outside the canvas
 *
 * `FigureCanvas.render()` replaces the whole of #fb_page_surface on every
 * document change, which is most of what happens here. Anything mounted inside
 * it is destroyed mid-interaction: a popover open over a panel would vanish the
 * moment the checkbox in it was ticked. So the bar is mounted in
 * #fb_overlay_layer, a sibling the renderer never touches, and it is told where
 * the selection is rather than living in it.
 *
 * ## Three levels, and only the first is visible
 *
 * Level 1 is the tool rail and the topbar (always). Level 2 is this bar, which
 * exists only while something is selected. Level 3 is inside `More`, inside the
 * popovers and on the right-click, which is where everything that is rarely
 * wanted goes. The rule for deciding is not how useful an action is -- it is
 * how often the answer to "do I want this right now?" is yes.
 */
class FigureContextBar {

    /** Gap in CSS pixels between the selection and the bar. */
    static get OFFSET() { return 10; }

    /** How close to the top of the workspace the bar may sit before it flips
     *  underneath the selection instead. */
    static get FLIP_MARGIN() { return 48; }

    constructor(options) {
        this.overlayEl = options.overlayEl;
        this.canvas = options.canvas;
        this.state = options.state;
        this.handlers = options.handlers || {};

        this.el = null;
        this.popover = null;
        //: The ids the bar is currently drawn for. Kept so a re-render that
        //: changes nothing about the selection does not close an open popover.
        this.ids = [];
        this.suppressed = false;
        //: How far the user has dragged the bar from where it would otherwise
        //: sit -- see `dragStart`. Remembered per browser, because the reason
        //: anyone moves it is that their windows are the shape they are, and
        //: making the same correction at the start of every session is the
        //: definition of a setting that should have been kept.
        this.offset = FigureContextBar.storedOffset();
    }

    /** Where the browser last left the bar. */
    static get STORAGE_KEY() { return "fb.contextBar.offset"; }

    static storedOffset() {
        // Storage throws outright in a few privacy modes, and a value written
        // by an older build is not something to trust into a layout: anything
        // that is not two finite numbers is no offset at all.
        try {
            const saved = JSON.parse(
                window.localStorage.getItem(FigureContextBar.STORAGE_KEY) || "null");
            if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
                return { x: saved.x, y: saved.y };
            }
        } catch (error) { /* no storage, or nothing worth reading */ }
        return { x: 0, y: 0 };
    }

    rememberOffset() {
        try {
            if (this.moved) {
                window.localStorage.setItem(FigureContextBar.STORAGE_KEY,
                                            JSON.stringify(this.offset));
            } else {
                window.localStorage.removeItem(FigureContextBar.STORAGE_KEY);
            }
        } catch (error) { /* not being able to remember is not a failure */ }
    }

    setup() {
        if (!this.overlayEl) return;
        this.el = document.createElement("div");
        this.el.className = "fb-context-bar";
        this.el.id = "fb_context_bar";
        this.el.hidden = true;
        this.overlayEl.appendChild(this.el);
        this.el.addEventListener("click", (event) => this.clicked(event));
        this.el.addEventListener("change", (event) => this.changed(event));
        this.el.addEventListener("pointerdown", (event) => this.barPointerDown(event));


        // A click anywhere else closes an open popover. On the document rather
        // than on the overlay: the click that dismisses it is usually on the
        // canvas, which is not a descendant of this.
        this._onDocDown = (event) => {
            if (!this.popover) return;
            if (this.popover.contains(event.target)) return;
            if (this.el.contains(event.target)) return;
            // The colour palette this popover opened is in the portal rather
            // than inside it, so by this test a click on a swatch is a click
            // somewhere else -- and the popover holding the well that opened it
            // would shut while the user was choosing.
            if (FigureColorField.contains(event.target)) return;
            this.closePopover();
        };
        document.addEventListener("pointerdown", this._onDocDown, true);
    }

    destroy() {
        document.removeEventListener("pointerdown", this._onDocDown, true);
        this.closePopover();
        this.el?.remove();
        this.el = null;
    }

    // -- visibility ------------------------------------------------------

    /**
     * Out of the way while a drag is in flight.
     *
     * A bar that followed the panel would be a control panel moving under the
     * pointer, and one that stayed put would be pointing at where the panel
     * used to be. Neither is worth having for the second a drag lasts.
     */
    suppress(active) {
        this.suppressed = Boolean(active);
        if (this.el) this.el.hidden = this.suppressed || !this.ids.length;
        if (this.suppressed) this.closePopover();
        else this.position();
    }

    /** Redraw for this selection. Called on every selection change AND after
     *  every canvas render, because the elements it measures are new each time. */
    update(ids) {
        if (!this.el) return;
        const same = ids.length === this.ids.length
            && ids.every((id, index) => id === this.ids[index]);
        this.ids = ids.slice();

        if (!ids.length) {
            this.el.hidden = true;
            this.closePopover();
            return;
        }
        if (!same) this.closePopover();
        this.el.innerHTML = this.markup(ids);
        this.el.hidden = this.suppressed;
        this.position();
        // The Transform popover shows the selection's width and height, and
        // this runs after every canvas render -- so dragging a corner moves the
        // numbers instead of leaving them describing where the box used to be.
        this.refreshPopover();
    }

    /**
     * Put the bar above the selection, or below it near the top of the page.
     *
     * Measured from the live elements rather than computed from millimetres:
     * the bar has to sit beside what the user can SEE, and what they can see is
     * the scrolled, zoomed rendering of those millimetres.
     */
    position() {
        if (!this.el || this.el.hidden || !this.ids.length) return;
        const box = this.selectionBox();
        if (!box) {
            this.el.hidden = true;
            return;
        }
        const host = this.overlayEl.getBoundingClientRect();
        const size = this.el.getBoundingClientRect();

        // `this.offset` is whatever the user dragged the bar by, and it is
        // added BEFORE the clamp -- so a bar pushed off the edge comes back to
        // the edge rather than off the screen, and the drag stops where the
        // window does.
        let left = box.left + box.width / 2 - host.left - size.width / 2
            + this.offset.x;
        left = Math.max(8, Math.min(left, host.width - size.width - 8));

        let top = box.top - host.top - size.height - FigureContextBar.OFFSET;
        const below = box.bottom - host.top + FigureContextBar.OFFSET;
        if (top < 8) top = Math.min(below, host.height - size.height - 8);
        top = Math.max(8, Math.min(top + this.offset.y, host.height - size.height - 8));

        this.el.style.left = Math.round(left) + "px";
        this.el.style.top = Math.round(top) + "px";
        this.positionPopover();
    }
    /** The union of the selected elements, in client coordinates. */
    selectionBox() {
        let box = null;
        for (const id of this.ids) {
            const element = this.canvas.surfaceEl.querySelector(
                `[data-panel-id="${id}"], [data-annotation-id="${id}"]`);
            if (!element) continue;
            const rect = element.getBoundingClientRect();
            box = box ? {
                left: Math.min(box.left, rect.left),
                top: Math.min(box.top, rect.top),
                right: Math.max(box.right, rect.right),
                bottom: Math.max(box.bottom, rect.bottom),
            } : { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
        }
        if (!box) return null;
        box.width = box.right - box.left;
        box.height = box.bottom - box.top;
        return box;
    }

    // -- what the bar shows ----------------------------------------------

    /**
     * The buttons for this selection, from the registry.
     *
     * This used to be three hand-built lists -- one for a panel, one for an
     * annotation with a branch inside it for text, one for a multiple selection
     * -- and the right-click menu was a fourth. They drifted, which is what
     * `figureActions.js` exists to stop: what an action is, when it appears and
     * when it can run are declared once and both surfaces read them.
     */
    markup(ids) {
        const context = this.context(ids);
        const actions = FigureActions.forSurface("bar", context.sel, context);
        if (!actions.length) return "";

        // The handle the bar is picked up by. First, and drawn as a grip rather
        // than as another button, so "this whole strip moves" is said by the
        // one part of it that is not a control.
        const parts = [`<span class="fb-context-grip" aria-hidden="true"></span>`];
        let group = null;
        for (const action of actions) {
            // A divider at every change of GROUP. It used to be one divider, at
            // the single flip from type-specific to generic -- so a selection of
            // several images, which is the ordinary state of composing a figure,
            // got eight identical tiles in a row with no boundary anywhere in
            // them. Drawn from the registry rather than typed into each list by
            // hand, so the bar and the right-click menu cluster the same way.
            if (group !== null && action.group !== group) parts.push(this.divider());
            group = action.group;
            parts.push(this.button(action.id, action.icon, action.label, {
                menu: Boolean(action.popover),
                short: action.short,
                shortcut: action.shortcut,
                danger: Boolean(action.danger),
                disabled: !action.isEnabled,
            }));
        }
        parts.push(this.divider());
        // The overflow is the one button with no word under it. A vertical
        // ellipsis is already the universal name for "the rest of this menu",
        // and captioning it "More" would put the least specific label on the
        // bar at the same size as the ones that say what they do.
        parts.push(this.button("more", "ellipsis-vertical", "More",
                               { menu: true, wordless: true }));
        return parts.join("");
    }

    /** Everything an action's predicates and `run` need. */
    context(ids) {
        return {
            ids: ids,
            sel: FigureSelection.describe(ids, this.state, this.canvas),
            canvas: this.canvas,
            state: this.state,
            handlers: this.handlers,
        };
    }
    /**
     * One button: icon ABOVE word.
     *
     * Icon-only was smaller, and it meant every control on this bar had to be
     * hovered to find out what it was -- on a bar whose contents CHANGE with
     * the selection, so the row learnt one week is not the row seen the next.
     * A tooltip is not a label; it is a label you have to ask for.
     *
     * Stacked rather than side by side, which is the shape every object bar
     * that carries words uses. Beside the icon, a word doubles the width of its
     * button, and eleven of those is a bar wider than the page it floats over --
     * it wrapped to two lines on a panel selection. Above it, the word costs
     * fourteen pixels of HEIGHT once for the whole strip, and the button stays
     * about as wide as its widest word. Both labels then start at the same
     * baseline, so the row reads as a row.
     *
     * The word is the registry's `short`, which is why that field exists: the
     * full label is a menu row's sentence and gets truncated to nothing here.
     * `title` and `aria-label` keep the full one -- plus the shortcut, which is
     * the one place on the bar there is room to name it -- so the tooltip and
     * the screen reader still say "Title, label and numbering" where the button
     * says "Titles".
     */
    button(act, icon, title, options) {
        const flags = options || {};
        const word = flags.short || title;
        const name = flags.shortcut ? `${title} (${flags.shortcut})` : title;
        return `<button type="button" class="fb-context-button${
                            flags.danger ? " is-danger" : ""}${
                            flags.wordless ? " is-wordless" : ""}"
                        data-act="${act}" title="${FigureSchema.escapeHtml(name)}"
                        aria-label="${FigureSchema.escapeHtml(name)}"
                        ${flags.disabled ? "disabled" : ""}
                        ${flags.menu ? 'aria-haspopup="true"' : ""}>
            <span class="fas fa-${icon} fb-context-icon" aria-hidden="true"></span>${
            flags.wordless ? ""
                : `<span class="fb-context-label">${FigureSchema.escapeHtml(word)}</span>`}
        </button>`;
    }


    divider() {
        return '<span class="fb-context-divider"></span>';
    }

    // -- acting ------------------------------------------------------------

    clicked(event) {
        const button = event.target.closest("[data-act]");
        if (!button || button.disabled) return;
        const act = button.dataset.act;

        // Anything the registry can run outright, runs. Only the actions whose
        // detail needs a form fall through to a popover.
        const action = FigureActions.byId(act);
        if (action && action.run) {
            this.closePopover();
            action.run(this.context(this.ids.slice()));
            return;
        }
        // Everything else opens a menu of its own, and a second click on the
        // same button closes it -- the button is the toggle, so there is never
        // a menu on screen whose opener does not look pressed.
        if (this.popover && this.popover.dataset.act === act) {
            this.closePopover();
            return;
        }
        this.openPopover(button, act);
    }

    changed(event) {
        const field = event.target.dataset?.field;
        if (!field) return;
        // The width waits for `change` -- the field being left, or Return --
        // and never fires on `input`. It is typed a digit at a time and every
        // prefix of it is a valid number, so committing keystrokes draws the
        // line at 2 pt on the way to 20 and leaves two entries in the undo
        // history for one decision. `FigureShapePanel.changed` guards the same
        // property the same way.
        if (field === "line_width_pt" && event.type !== "change") return;
        this.fieldChanged(field, event.target);
    }

    // -- popovers ----------------------------------------------------------

    openPopover(button, act) {
        this.closePopover();
        const body = this.popoverMarkup(act);
        if (!body) return;

        this.popover = document.createElement("div");
        this.popover.className = "fb-context-popover";
        this.popover.dataset.act = act;
        this.popover.innerHTML = body;
        this.overlayEl.appendChild(this.popover);
        this._anchor = button;

        this.popover.addEventListener("click", (event) => this.popoverClicked(event));
        this.popover.addEventListener("input", (event) => this.changed(event));
        this.popover.addEventListener("change", (event) => this.changed(event));
        this.popover.addEventListener(
            "pointerdown", (event) => this.popoverPointerDown(event));
        // Escape, scoped to the popover itself. The workspace's own Escape
        // stands down while a field has focus -- correctly, since Escape in a
        // text box means "undo what I am typing" -- and every popover here
        // auto-focuses its first field. So the one dismissal a user reaches for
        // was the one that did nothing, in exactly the state it was needed.
        this.popover.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            event.stopPropagation();
            const anchor = this._anchor;
            this.closePopover();
            anchor?.focus();
        });
        button.classList.add("is-open");
        this.positionPopover();
        // Not for the symbol palette. Focusing a tile takes the caret out of
        // the text editor, and the caret is the place the symbol is going to be
        // inserted at -- so the one popover that must not grab focus is the one
        // whose whole purpose is to act on what has focus.
        //
        // A field before a button, rather than whichever comes first in the
        // markup: the stroke popover leads with the stepper's minus, and a
        // focused minus turns the Return that closes a dialog everywhere else
        // into a line one step thinner.
        if (act !== "symbol") {
            (this.popover.querySelector("input:not([type=button]), select")
                || this.popover.querySelector("button"))?.focus();
        }
    }

    closePopover() {
        this.popover?.remove();
        this.popover = null;
        this._anchor?.classList.remove("is-open");
        this._anchor = null;
        // A palette opened from a well inside it has nothing else holding it up.
        FigureColorField.close();
    }

    positionPopover() {
        if (!this.popover || !this._anchor) return;
        const host = this.overlayEl.getBoundingClientRect();
        const anchor = this._anchor.getBoundingClientRect();
        // See FigureContextMenu.open: the popover animates in with a scale, and
        // a client rect would measure the transformed box rather than the one
        // it is about to settle at.
        const size = { width: this.popover.offsetWidth,
                       height: this.popover.offsetHeight };

        let left = anchor.left - host.left + anchor.width / 2 - size.width / 2;
        left = Math.max(8, Math.min(left, host.width - size.width - 8));
        let top = anchor.bottom - host.top + 6;
        // Above the bar when there is no room under it, which happens as soon
        // as the selection is near the bottom of a tall page.
        if (top + size.height > host.height - 8) {
            top = Math.max(8, anchor.top - host.top - size.height - 6);
        }
        this.popover.style.left = Math.round(left) + "px";
        this.popover.style.top = Math.round(top) + "px";
    }

    popoverMarkup(act) {
        const panel = this.ids.length === 1 ? this.state.panel(this.ids[0]) : null;
        const annotation = this.ids.length === 1
            ? this.state.document.annotations[this.ids[0]] : null;
        // A PANEL's own properties -- title, scale bar, legend, split, pixel
        // size -- are the image sidebar's, the way a caption's are the text
        // panel's. Nothing here builds them any more; see figureImagePanel.js.
        if (act === "colour" && annotation) return this.colourPopover(annotation);
        if (act === "stroke" && annotation) return this.strokePopover(annotation);
        if (act === "more") return this.morePopover(panel);
        if (act === "transform") return this.transformPopover();
        if (act === "symbol") return this.symbolPopover();
        if (act === "group:arrange") return this.arrangePopover();
        return "";
    }

    /**
     * Everything spatial, in one place, under headings.
     *
     * Align, Distribute, Match size, Layout and Order were five tiles on the
     * bar, and their words are near-synonyms of each other: "which of these
     * five puts things in a row?" was a question the bar asked every time it
     * appeared. On the ordinary selection here -- several images -- they were
     * five of eight identical tiles, in a strip wider than the sheet they float
     * over.
     *
     * Not a menu of five submenus, and not a scrolling list of eighteen rows
     * either: five short sections, flowed into two columns, which is a popover
     * about the height of a menu and readable in one glance. The sections carry
     * their own names, so nothing is behind a word that has to be guessed at,
     * and each is greyed by the same predicate its button used to be greyed by.
     *
     * The order is by how often the answer is yes -- Align first, Order last --
     * rather than by the order they happened to be declared in.
     */
    arrangePopover() {
        const context = this.context(this.ids.slice());
        const members = new Map(FigureActions.forGroup("arrange", context.sel, context)
            .map((action) => [action.id, action]));

        // `data-arrange` is the contract FigureCanvas already answers to, so
        // these rows add vocabulary rather than a path. Order is the exception:
        // its four are registry actions with their own `run`, so they go out as
        // `data-more` -- and they are taken from FigureActions.ARRANGE, so this
        // popover and the right-click menu cannot list different commands.
        const section = (id, title, rows) => {
            const member = members.get(id);
            if (!member) return "";
            return `<div class="fb-arrange-section">
                <div class="fb-arrange-title">${title}</div>
                ${rows.map(([command, label, icon]) =>
                    this.menuItem(command, label, {
                        icon: icon, attribute: "data-arrange",
                        disabled: !member.isEnabled })).join("")}
            </div>`;
        };
        const order = members.get("arrange");

        return `<div class="fb-arrange-grid">
            ${section("align", "Align", [
                ["left", "Align left", "align-left"],
                ["center", "Align centers", "align-center"],
                ["right", "Align right", "align-right"],
                ["top", "Align top", "arrow-up"],
                ["middle", "Align middles", "arrows-up-down"],
                ["bottom", "Align bottom", "arrow-down"],
            ])}
            ${section("distribute", "Distribute", [
                // Evenly spaced bars, which is what the command produces, rather
                // than a pair of arrows -- the arrows are already the BUTTON
                // that opened this, and repeating them here says nothing about
                // which of the two axes each row is.
                ["distribute_h", "Equal gaps across", "grip-lines-vertical"],
                ["distribute_v", "Equal gaps down", "grip-lines"],
            ])}
            ${section("resize", "Match size", [
                ["same_width", "Same width", "left-right"],
                ["same_height", "Same height", "up-down"],
                ["same_size", "Same size", "expand"],
            ])}
            ${section("layout", "Layout", [
                ["row", "Row", "table-columns"],
                ["column", "Column", "bars"],
                ["grid", "Grid", "table-cells"],
            ])}
            ${order ? `<div class="fb-arrange-section">
                <div class="fb-arrange-title">Order</div>
                ${FigureActions.ARRANGE.map((id) => FigureActions.byId(id))
                    .map((action) => this.menuItem(action.id, action.label, {
                        icon: action.icon, shortcut: action.shortcut,
                        disabled: !order.isEnabled })).join("")}
            </div>` : ""}
        </div>`;
    }

    /**
     * The characters a caption needs and a keyboard does not have.
     *
     * A grid rather than a search field: the useful set for a figure is about
     * thirty characters, and thirty tiles are quicker to read than any box is
     * to type into.
     *
     * Restricted to WinAnsi, which is the encoding the PDF core fonts are drawn
     * with. Greek and the maths signs are left out for a reason worth writing
     * down, because it is not the obvious one: reportlab does not refuse them.
     * It silently SUBSTITUTES -- an alpha comes out of Helvetica-with-
     * WinAnsiEncoding set in Symbol, and a dingbat in ZapfDingbats, both with
     * that face's metrics rather than the document's. So the character is set
     * in a typeface the caption did not choose, at a width the browser did not
     * measure, and the line it is on can break in one place on screen and
     * another in the PDF. Everything in the list below is drawn by the font the
     * caption actually names.
     *
     * Nothing stops anybody PASTING an alpha; this is about what the tool
     * proposes, and about not proposing the one character in the caption that
     * will not match.
     */
    symbolPopover() {
        return `<div class="fb-symbol-grid">${FigureContextBar.SYMBOLS.map((glyph) =>
            `<button type="button" class="fb-symbol" data-symbol="${
                FigureSchema.escapeHtml(glyph)}" aria-label="${
                FigureSchema.escapeHtml(glyph)}">${
                FigureSchema.escapeHtml(glyph)}</button>`).join("")}</div>`;
    }

    static get SYMBOLS() {
        return ["µ", "°", "±", "×", "÷", "¹",
                "²", "³", "½", "¼", "¾", "Å",
                "–", "—", "·", "•", "…", "‰",
                "†", "‡", "§", "¶", "‘", "’",
                "“", "”", "«", "»", "™", "©"];
    }

    /* `typePopover` was here: a size field and an alignment select for a text
       box, opened by an action with id "type". No such action has existed since
       text formatting moved into the text sidebar, so nothing could open it --
       and while it sat here it was a second control for two properties the
       sidebar already owns, which is how a caption ends up 9pt in one place and
       9.5 in another. Deleted rather than rewired: the sidebar is the owner. */

    /**
     * Colour, as the shared palette.
     *
     * This popover used to be a bare `<input type="color">`, under a note
     * saying a palette of "nice" colours would invite picking one that nearly
     * matches something already in the figure. The opposite turned out to be
     * true: what a figure's colours have to match is the REST OF THE FIGURE,
     * and a dialog that opens on whatever was last chosen is the one tool that
     * cannot say what that was. `FigureColorField` offers the ten this canvas
     * draws with and keeps the dialog behind "Custom", so an exact value is
     * still one click further on and a repeat of a colour already used is one
     * click nearer.
     */
    colourPopover(annotation) {
        return `
            <label class="control-label" for="fb_ctx_colour">Color</label>
            ${FigureColorField.swatch({
                field: "color", id: "fb_ctx_colour", label: "Color", block: true,
                value: annotation.style.color })}`;
    }

    /**
     * Line width and fill, for the two legacy box shapes.
     *
     * That is all this covers now. Text, shapes and lines each have a sidebar
     * panel carrying the same properties, and two different controls for one
     * number is how a figure ends up with a 1 pt arrow beside a 0.75 pt outline
     * that were both meant to be "thin". What is left is `rect` and `ellipse`,
     * which predate the shape tool, are not creatable, and have no panel -- so
     * this popover is the only way in to either.
     *
     * The width is the shape panel's stepper rather than a number spinner, and
     * in the same quarter points.
     */
    strokePopover(annotation) {
        return `
            <label class="control-label" for="fb_ctx_width">Line width (pt)</label>
            <div class="fb-stepper">
                <button type="button" class="fb-stepper-button" data-step="-1"
                        aria-label="Thinner">
                    <span class="fas fa-minus" aria-hidden="true"></span></button>
                <input id="fb_ctx_width" class="fb-stepper-value" type="text"
                       inputmode="decimal" spellcheck="false" data-field="line_width_pt"
                       value="${annotation.style.line_width_pt}"
                       aria-label="Line width, in points">
                <button type="button" class="fb-stepper-button" data-step="1"
                        aria-label="Thicker">
                    <span class="fas fa-plus" aria-hidden="true"></span></button>
            </div>
            <label class="fb-check">
                <input type="checkbox" data-field="fill_on"
                       ${annotation.style.fill ? "checked" : ""}> Filled
            </label>
            ${FigureColorField.swatch({
                field: "fill", label: "Fill color", block: true,
                value: annotation.style.fill || "#ffffff" })}`;
    }

    /**
     * Level 3: everything worth having that is not worth a button.
     *
     * The two deletes are both here and are worded as what they do. "Remove
     * from page" unplaces a panel and it comes back in the tray; "Delete from
     * figure" destroys the captured scene. Naming them "Delete" and "Delete
     * permanently" would put the whole distinction in an adverb.
     */
    /** The overflow menu: the generic actions that did not fit on the bar,
     *  from the same registry the bar reads. */
    morePopover(panel) {
        const context = this.context(this.ids.slice());
        const rows = [];
        if (panel) {
            const source = this.state.source(panel.source_id);
            const status = this.state.sourceStatus[panel.source_id]?.status || "ok";
            if (status === "changed") {
                rows.push(this.menuItem("accept_source", "Accept the new image"));
            }
            if (source) {
                rows.push(`<div class="fb-menu-note">${FigureSchema.escapeHtml(
                    source.display_name || source.datasource || "no source")}</div>`);
            }
        }
        for (const action of FigureActions.forSurface("overflow", context.sel, context)) {
            rows.push(this.menuItem(action.id, action.label,
                                    { danger: action.danger,
                                      disabled: !action.isEnabled,
                                      icon: action.icon,
                                      shortcut: action.shortcut }));
        }
        // Only once the bar has actually been moved. A permanent "put it back"
        // is a row that says nothing for the whole of the time nobody has
        // picked the bar up, which is nearly all of it.
        if (this.moved) {
            rows.push(this.menuItem("reset_bar", "Move the toolbar back",
                                    { icon: "rotate-left" }));
        }
        return rows.join("");
    }
    /**
     * One row of a menu: icon, label, key -- in three columns, in that order.
     *
     * The icon is optional and is drawn in a fixed-width gutter, so a list
     * where only some rows have one still reads as a column of labels rather
     * than as a ragged left edge. The shortcut is pushed to the RIGHT edge and
     * greyed, which is the arrangement every platform menu uses and the reason
     * it works: the labels are what the eye scans down, and a key set in the
     * same colour immediately after a label of unpredictable length puts the
     * least important text in the middle of the scan.
     */
    menuItem(act, label, options) {
        const flags = options || {};
        // Which attribute carries the command is the caller's, because two
        // vocabularies come through this one row. `data-more` is a registry
        // action with its own `run`; `data-arrange` is a layout command that
        // FigureCanvas answers to directly. They looked like different kinds of
        // menu when only the first had icons -- and they are not: they are the
        // same row with a different dispatch behind it.
        const attribute = flags.attribute || "data-more";
        return `<button type="button" class="fb-menu-item${
                            flags.danger ? " is-danger" : ""}"
                        ${attribute}="${act}" ${flags.disabled ? "disabled" : ""}>
            <span class="fb-menu-icon" aria-hidden="true">${flags.icon
                ? `<span class="fas fa-${flags.icon}"></span>` : ""}</span>
            <span class="fb-menu-text">${FigureSchema.escapeHtml(label)}</span>${flags.shortcut
                ? `<span class="fb-menu-key" aria-hidden="true">${
                    FigureSchema.escapeHtml(flags.shortcut)}</span>` : ""}</button>`;
    }


    /**
     * A press inside the popover, before focus has had a chance to move.
     *
     * Inserting a symbol has to happen HERE and not on click: pressing a button
     * blurs the contenteditable first, blur is what commits and closes the text
     * editor, and by the time a click handler ran there would be no editor left
     * to ask where the caret was. Cancelling the default of the pointerdown is
     * the only thing that stops the focus moving at all -- so the palette can
     * be used several times in a row without the caret ever leaving the caption.
     */
    popoverPointerDown(event) {
        const symbol = event.target.closest("[data-symbol]");
        if (!symbol) return;
        event.preventDefault();
        this.handlers.onInsertSymbol?.(this.ids[0], symbol.dataset.symbol);
    }

    popoverClicked(event) {
        const well = event.target.closest("[data-swatch]");
        if (well && !well.disabled) {
            // The property name is taken now rather than read off the button
            // when a colour arrives: this popover is rebuilt whenever the
            // selection changes, so the element cannot be trusted to outlive
            // the palette it opened.
            const field = well.dataset.swatch;
            const id = this.ids.length === 1 ? this.ids[0] : null;
            FigureColorField.open(well, {
                value: well.dataset.value,
                onPick: (hex) => id && this.handlers.onAnnotationChange?.(
                    id, { style: { [field]: hex } }),
            });
            return;
        }
        const step = event.target.closest("[data-step]");
        if (step) {
            this.stepWidth(Number(step.dataset.step));
            return;
        }
        const arrange = event.target.closest("[data-arrange]");
        if (arrange) {
            this.handlers.onArrange?.(arrange.dataset.arrange);
            this.closePopover();
            return;
        }
        const more = event.target.closest("[data-more]");
        if (!more) return;
        const ids = this.ids.slice();
        const act = more.dataset.more;

        // The generic actions run from the registry, which is also what the
        // right-click menu runs -- so a row cannot mean one thing in one menu
        // and something else in the other.
        const action = FigureActions.byId(act);
        if (action && action.run) {
            action.run(this.context(ids));
            this.closePopover();
            return;
        }

        ({
            reset_bar: () => this.resetPosition(),
            accept_source: () => {
                const panel = this.state.panel(ids[0]);
                if (panel) this.handlers.onAcceptSource?.(panel.source_id);
            },
        }[act] || (() => {}))();
        this.closePopover();
    }

    /**
     * One field changed.
     *
     * Committed straight away rather than on a Done button: every one of these
     * is a single reversible property, autosave is immediate, and a popover
     * with an OK in it is a dialog pretending to be a control.
     *
     * The panel fields apply across the whole selection, which is the point of
     * the multi-panel bar -- doing a row of six one at a time is where a figure
     * picks up the inconsistencies it is then hard to spot.
     */
    fieldChanged(field, input) {
        // Transform first, and before the panel/annotation fork: it is the one
        // form on this bar that reads the same four numbers off either kind.
        if (field.startsWith("tf_")) {
            this.transformChanged(field.slice(3), input);
            return;
        }
        if (field === "units") {
            // The figure's unit, not this popover's. There is one answer to
            // "what is this page measured in" and the rulers already show it;
            // a second copy here is how the two end up disagreeing.
            this.handlers.onUnits?.(input.value);
            this.refreshPopover();
            return;
        }
        // What is left on this bar is annotation styling and the Transform
        // form. A placed panel's own fields -- title, label, numbering, scale
        // bar, legend -- are the image sidebar's now; see figureImagePanel.js.
        const annotation = this.ids.length === 1
            ? this.state.document.annotations[this.ids[0]] : null;
        if (annotation) this.annotationFieldChanged(field, input, annotation);
    }

    annotationFieldChanged(field, input, annotation) {
        const style = {};
        if (field === "line_width_pt") {
            const value = parseFloat(input.value);
            // Nothing to commit -- put the field back to what the object
            // actually is, rather than leaving it showing what was typed over
            // it. The bounds were the `min`/`max` of a number spinner until this
            // became a stepper over a text field, which has neither.
            if (!Number.isFinite(value)) {
                input.value = annotation.style.line_width_pt;
                return;
            }
            style[field] = FigureContextBar.width(value);
        } else if (field === "font_size_pt") {
            const value = parseFloat(input.value);
            if (!Number.isFinite(value)) return;
            style[field] = value;
        } else if (field === "align" || field === "color") {
            style[field] = input.value;
        } else if (field === "fill") {
            style.fill = input.value;
        } else if (field === "fill_on") {
            // An empty string is how the schema says "no fill"; the well keeps
            // its colour so unticking and re-ticking comes back to it rather
            // than to black.
            style.fill = input.checked
                ? (this.popover?.querySelector('[data-swatch="fill"]')?.dataset.value
                   || "#ffffff")
                : "";
        } else return;

        this.handlers.onAnnotationChange?.(annotation.annotation_id, { style: style });
    }

    /** The stepper, in the shape panel's quarter points -- journal line weights
     *  are quoted in them, and a whole point is a visible jump at the widths
     *  anyone actually uses. Read off the FIELD rather than the object, so
     *  stepping continues from a number that was typed but not yet committed. */
    stepWidth(by) {
        const annotation = this.ids.length === 1
            ? this.state.document.annotations[this.ids[0]] : null;
        if (!annotation) return;
        const input = this.popover?.querySelector('[data-field="line_width_pt"]');
        const from = parseFloat(input && input.value);
        const base = Number.isFinite(from) ? from : annotation.style.line_width_pt;
        const width = FigureContextBar.width(base + by * FigureShapePanel.WIDTH_STEP_PT);
        if (input) input.value = width;
        this.handlers.onAnnotationChange?.(
            annotation.annotation_id, { style: { line_width_pt: width } });
    }

    /** One reading of "a legal line width", shared by the stepper and the field.
     *  Same bounds and same rounding as `FigureShapePanel.setWidth`. */
    static width(value) {
        return Math.min(20, Math.max(0, Math.round(value * 100) / 100));
    }

    // -- transform ---------------------------------------------------------

    /**
     * Where the selected object is and how big it is, in the figure's unit.
     *
     * One reader for both kinds. A panel keeps its geometry in `placement` and
     * an annotation in `geometry`, which is a difference in where the numbers
     * live rather than in what they mean, and every caller past this point is
     * better off not knowing about it.
     *
     * `rotation` is null for a panel: panels are not rotated, and a field that
     * is always 0 and cannot be changed is worse than no field.
     */
    transformValues() {
        if (this.ids.length !== 1) return null;
        const id = this.ids[0];
        const panel = this.state.panel(id);
        const box = panel ? panel.placement
            : this.state.document.annotations[id]?.geometry;
        if (!box) return null;
        const per = FigureContextBar.UNITS[this.unit()].per;
        const show = (mm) => String(Math.round((mm / per) * 1000) / 1000);
        return {
            w: show(box.w_mm), h: show(box.h_mm),
            rotation: panel ? null : String(Math.round(box.rotation || 0)),
        };
    }

    /** The unit the figure is being measured in, asked of whoever owns it --
     *  the View menu -- rather than kept a second time here. */
    unit() {
        const name = this.handlers.units?.();
        return name in FigureContextBar.UNITS ? name : "mm";
    }

    /** The same table `FigureViewOptions.UNITS` holds, narrowed to the one
     *  field this needs. Duplicated rather than reached for, because the bar
     *  must render on a page where the View menu has not been built yet -- and
     *  it is pinned equal by the boot probe. */
    static get UNITS() {
        return { mm: { per: 1, label: "mm" }, in: { per: 25.4, label: "in" },
                 pt: { per: 25.4 / 72, label: "pt" } };
    }

    transformPopover() {
        const now = this.transformValues();
        if (!now) return "";
        const unit = this.unit();
        const units = Object.entries(FigureContextBar.UNITS).map(([name, spec]) =>
            `<option value="${name}"${name === unit ? " selected" : ""}>${spec.label}</option>`)
            .join("");
        const number = (field, label, value, step, suffix) => `
            <label class="fb-tf-row">
                <span class="control-label">${FigureSchema.escapeHtml(label)}</span>
                <span class="fb-input-suffixed">
                    <input class="fb-input fb-input-tiny" type="number" step="${step}"
                           data-field="tf_${field}" value="${value}">
                    <span class="fb-input-suffix" aria-hidden="true">${suffix}</span>
                </span>
            </label>`;

        return (now.rotation === null ? ""
                : number("rotation", "Rotation", now.rotation, "1", "&deg;"))
            + number("w", "Width", now.w, "0.1", FigureContextBar.UNITS[unit].label)
            + number("h", "Height", now.h, "0.1", FigureContextBar.UNITS[unit].label)
            + `<label class="fb-tf-row">
                   <span class="control-label">Units</span>
                   <select class="fb-input fb-input-tiny" data-field="units"
                   >${units}</select>
               </label>`;
    }

    transformChanged(field, input) {
        const value = parseFloat(input.value);
        if (!Number.isFinite(value) || this.ids.length !== 1) return;
        const id = this.ids[0];
        if (field === "rotation") {
            this.handlers.onAnnotationChange?.(
                id, { geometry: { rotation: ((value % 360) + 360) % 360 } });
            return;
        }
        const mm = Math.max(1, value * FigureContextBar.UNITS[this.unit()].per);
        const key = field === "w" ? "w_mm" : "h_mm";
        const panel = this.state.panel(id);
        if (panel) {
            this.handlers.onPanelChange?.(
                id, { placement: { ...panel.placement, [key]: mm } });
            return;
        }
        const annotation = this.state.document.annotations[id];
        if (key === "h_mm" && annotation?.type === "text") {
            // Typing a height says the same thing dragging the bottom edge
            // says: this box is this tall. `autofit` recomputes the height from
            // the text on the very next write, so without clearing it the
            // number would go in and come straight back out again.
            this.handlers.onAnnotationChange?.(
                id, { geometry: { h_mm: mm }, style: { autofit: false } });
            return;
        }
        this.handlers.onAnnotationChange?.(id, { geometry: { [key]: mm } });
    }

    /**
     * Put the current numbers back into an open Transform popover.
     *
     * Called after every canvas render, so dragging a corner moves the fields.
     * The focused input is skipped: rewriting the box somebody is typing into
     * is how "12" becomes "12.0" under the caret half-way through "12.5".
     */
    refreshPopover() {
        if (!this.popover || this.popover.dataset.act !== "transform") return;
        const now = this.transformValues();
        if (!now) return;
        for (const [field, value] of Object.entries(now)) {
            if (value === null) continue;
            const input = this.popover.querySelector(`[data-field="tf_${field}"]`);
            if (input && input !== document.activeElement) input.value = value;
        }
    }

    // -- moving the bar out of the way --------------------------------------
    /**
     * A press on the bar: either a button that must not steal the focus, or the
     * start of a drag.
     *
     * Pressing a button here keeps focus where it was. This bar acts on a
     * selection, and while a caption is being typed into, the focus IS that
     * selection: taking it away blurs the editor, and blur commits and closes
     * it -- so Insert Symbol would act on an object that stopped being edited
     * the instant the button went down. Cancelling the pointerdown's default
     * suppresses the focus change; the click still fires.
     */
    barPointerDown(event) {
        if (event.target.closest("button")) {
            event.preventDefault();
            return;
        }
        if (event.target.closest("input, select, label")) return;
        this.dragStart(event);
    }

    /**
     * Drag the bar itself.
     *
     * It sits over the page, and over a caption near the top of a panel it sits
     * over the thing being edited -- which is the one moment the controls are
     * most in the way. So it can be picked up by its grip and put somewhere
     * else, and the displacement is kept while the selection moves around
     * underneath it.
     *
     * An OFFSET rather than an absolute position, so the bar still follows the
     * selection: a bar parked at fixed coordinates stops being "the controls
     * for this object" and becomes a floating palette, which is the thing this
     * replaced. Kept between sessions, though -- an offset is not a way out of
     * one collision, it is where this user's windows leave room, and it was
     * being forgotten on every reload.
     */
    dragStart(event) {
        if (event.button !== 0) return;
        const from = { x: event.clientX, y: event.clientY };
        const start = { ...this.offset };
        this.closePopover();
        this.el.classList.add("is-dragging");

        const move = (moved) => {
            this.offset = { x: start.x + (moved.clientX - from.x),
                            y: start.y + (moved.clientY - from.y) };
            this.position();
        };
        const up = () => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
            this.el.classList.remove("is-dragging");
            this.rememberOffset();
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
        event.preventDefault();
    }

    get moved() {
        return this.offset.x !== 0 || this.offset.y !== 0;
    }

    resetPosition() {
        this.offset = { x: 0, y: 0 };
        this.rememberOffset();
        this.position();
    }
}
