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
        //: sit. Not persisted -- see `dragStart`.
        this.offset = { x: 0, y: 0 };
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
        let generic = null;
        for (const action of actions) {
            // One divider where the type-specific actions give way to the ones
            // every object has. The gap is what says which controls belong
            // together, so it is drawn from the registry rather than typed into
            // each list by hand.
            if (generic !== null && Boolean(action.generic) !== generic) {
                parts.push(this.divider());
            }
            generic = Boolean(action.generic);
            parts.push(this.button(action.id, action.icon, action.label, {
                menu: Boolean(action.popover),
                short: action.short,
                shortcut: action.shortcut,
                pressed: action.isPressed,
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
        return `<button type="button" class="fb-context-button${flags.pressed ? " is-on" : ""}${
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
        if (field) this.fieldChanged(field, event.target);
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
        button.classList.add("is-open");
        this.positionPopover();
        // Not for the symbol palette. Focusing a tile takes the caret out of
        // the text editor, and the caret is the place the symbol is going to be
        // inserted at -- so the one popover that must not grab focus is the one
        // whose whole purpose is to act on what has focus.
        if (act !== "symbol") {
            this.popover.querySelector("input, select, button")?.focus();
        }
    }

    closePopover() {
        this.popover?.remove();
        this.popover = null;
        this._anchor?.classList.remove("is-open");
        this._anchor = null;
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
        const panels = this.selectedPanels();
        if (act === "titles" && panel) return this.titlesPopover(panel);
        if (act === "scalebar" && panels.length) return this.scalebarPopover(panels);
        if (act === "pixelsize" && panels.length) return this.pixelSizePopover(panels);
        if (act === "legend" && panels.length) return this.legendPopover(panels);
        if (act === "split" && panel) return this.splitPopover();
        if (act === "type" && annotation) return this.typePopover(annotation);
        if (act === "colour" && annotation) return this.colourPopover(annotation);
        if (act === "stroke" && annotation) return this.strokePopover(annotation);
        if (act === "more") return this.morePopover(panel);
        if (act === "align") return this.menu([
            ["left", "Align left", "align-left"],
            ["center", "Align centers", "align-center"],
            ["right", "Align right", "align-right"],
            ["top", "Align top", "arrow-up"],
            ["middle", "Align middles", "arrows-up-down"],
            ["bottom", "Align bottom", "arrow-down"],
        ]);
        if (act === "distribute") return this.menu([
            // Evenly spaced bars, which is what the command produces, rather
            // than a pair of arrows -- the arrows are already the BUTTON that
            // opened this, and repeating them here says nothing about which of
            // the two axes each row is.
            ["distribute_h", "Equal gaps across", "grip-lines-vertical"],
            ["distribute_v", "Equal gaps down", "grip-lines"],
        ]);
        if (act === "resize") return this.menu([
            ["same_width", "Same width", "left-right"],
            ["same_height", "Same height", "up-down"],
            ["same_size", "Same size", "expand"],
        ]);
        if (act === "layout") return this.menu([
            ["row", "Row", "table-columns"],
            ["column", "Column", "bars"],
            ["grid", "Grid", "table-cells"],
        ]);
        if (act === "transform") return this.transformPopover();
        if (act === "symbol") return this.symbolPopover();
        // Rows rather than `menu()`: these four are registry actions with their
        // own `run`, so they go out as `data-more` and are dispatched by the
        // same code the overflow and the right-click menu already use. Taken
        // from `FigureActions.ARRANGE` so this popover and the right-click menu
        // cannot end up offering different commands under the same word.
        if (act === "arrange") {
            return FigureActions.ARRANGE.map((id) => FigureActions.byId(id))
                .map((action) => this.menuItem(action.id, action.label,
                                               { icon: action.icon,
                                                 shortcut: action.shortcut })).join("");
        }
        return "";
    }

    /**
     * A list of arrange commands. `data-arrange` is the contract FigureCanvas
     * already answers to, so this menu adds vocabulary rather than a path.
     *
     * Drawn through `menuItem`, which is the only reason these rows have icons:
     * they were built here as bare labels while the Arrange popover next to them
     * -- the same size, the same shape, opened from the button alongside --
     * listed icon, label and key. So four menus on one toolbar looked like two
     * different kinds of menu, and the difference was which function had
     * happened to draw them rather than anything about the commands.
     */
    menu(entries) {
        return entries.map(([command, label, icon]) =>
            this.menuItem(command, label,
                          { icon: icon, attribute: "data-arrange" })).join("");
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

    titlesPopover(panel) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const style = this.state.document.settings.label_style;
        return `
            <label class="control-label" for="fb_ctx_title">Title</label>
            <input id="fb_ctx_title" class="fb-input" type="text" data-field="title"
                   value="${escape(panel.title || "")}" maxlength="200"
                   placeholder="No title">

            <label class="control-label" for="fb_ctx_label">Label</label>
            <div class="fb-figure-row">
                <input id="fb_ctx_label" class="fb-input fb-input-tiny" type="text"
                       data-field="label" maxlength="8" placeholder="auto"
                       value="${escape(panel.label.auto ? "" : panel.label.text)}">
                <label class="fb-check" title="Renumber when the panels are rearranged">
                    <input type="checkbox" data-field="label_auto"
                           ${panel.label.auto ? "checked" : ""}> Auto
                </label>
            </div>
            <label class="fb-check">
                <input type="checkbox" data-field="label_visible"
                       ${panel.label.visible ? "checked" : ""}> Show the label
            </label>

            <label class="control-label" for="fb_ctx_label_style">Numbering</label>
            <select id="fb_ctx_label_style" class="fb-select" data-field="label_style">
                <option value="A" ${style === "A" ? "selected" : ""}>A, B, C</option>
                <option value="a" ${style === "a" ? "selected" : ""}>a, b, c</option>
                <option value="A1" ${style === "A1" ? "selected" : ""}>A1, A2, A3</option>
            </select>
            <p class="fb-muted fb-popover-note">Numbering is the whole figure's, and follows
                reading order &mdash; left to right, top to bottom.</p>`;
    }

    /**
     * Scale bars, for one panel or for a whole selection.
     *
     * The length is EITHER automatic or an explicit number of microns, and the
     * difference matters across several panels: automatic gives each image a
     * round number that fits it, which for a row of different magnifications is
     * several different bars; an explicit length is the same physical distance
     * everywhere, which is what makes two panels comparable by eye. The popover
     * says which is which rather than leaving it to be discovered.
     */
    scalebarPopover(panels) {
        const uncalibrated = panels.filter(
            (panel) => !FigureSchema.physicalWidthUm(
                this.state.source(panel.source_id), panel.scene.viewport));
        const shown = panels.every((panel) => panel.scalebar.visible);
        const target = panels[0].scalebar.target_um;
        const same = panels.every((panel) => panel.scalebar.target_um === target);
        const lengths = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];

        return `
            <label class="fb-check">
                <input type="checkbox" data-field="scalebar" ${shown ? "checked" : ""}>
                Show a scale bar${panels.length > 1
                    ? " on all " + panels.length : ""}
            </label>

            <label class="control-label" for="fb_ctx_bar_len">Length</label>
            <select id="fb_ctx_bar_len" class="fb-select" data-field="scalebar_length">
                <option value="" ${same && !target ? "selected" : ""}>Automatic</option>
                ${lengths.map((value) =>
                    `<option value="${value}" ${same && target === value ? "selected" : ""}
                    >${FigureSchema.escapeHtml(FigureSchema.formatMicrons(value))}</option>`).join("")}
            </select>
            <p class="fb-muted fb-popover-note">${panels.length > 1
                ? "Automatic gives each image a round number that fits it; a set length is "
                  + "the same physical distance on every panel."
                : "Automatic picks a round number that fits this image."}</p>

            ${uncalibrated.length ? `
            <p class="fb-muted fb-popover-note">${FigureSchema.escapeHtml(
                FigureSchema.countPhrase(uncalibrated.length, "image"))} here recorded no
                pixel size, so ${uncalibrated.length === 1 ? "it has" : "they have"}
                no bar.</p>
            <button type="button" class="fb-menu-item" data-more="pixelsize">
                Set the pixel size…</button>` : ""}`;
    }

    /**
     * Type in a pixel size for images that never recorded one.
     *
     * Written to the SOURCE rather than to the panel, because it is a fact
     * about the image and every panel of it is entitled to the same answer --
     * and marked `manual`, which the provenance page prints. A number somebody
     * typed is not the same evidence as one the file stated, and a figure that
     * could not tell the difference would be a figure whose scale bars cannot
     * be checked.
     */
    pixelSizePopover(panels) {
        const sources = this.uncalibratedSources(panels);
        return `
            <label class="control-label" for="fb_ctx_mpp">Microns per pixel</label>
            <input id="fb_ctx_mpp" class="fb-input" type="number" min="0"
                   step="0.0001" placeholder="e.g. 0.325">
            <p class="fb-muted fb-popover-note">Applies to
                ${FigureSchema.escapeHtml(FigureSchema.countPhrase(sources.length, "image"))}
                across the whole figure, and is recorded in the provenance as a value you
                supplied rather than one the file stated.</p>
            <button type="button" class="fb-menu-item" data-more="apply_pixelsize">
                Apply</button>`;
    }

    /** The sources behind these panels that have no calibration. */
    uncalibratedSources(panels) {
        const seen = new Map();
        for (const panel of panels) {
            const source = this.state.source(panel.source_id);
            if (source && !(source.pixel_size && source.pixel_size.value > 0)) {
                seen.set(source.source_id, source);
            }
        }
        return Array.from(seen.values());
    }

    /**
     * Legends, and the conflict that must never be resolved silently.
     *
     * Two panels can show the same marker in different colours -- deliberately,
     * because they were captured in different sessions, or by accident. Turning
     * on a legend across both would produce two legends disagreeing about what
     * CD8 looks like, which is a figure that misleads a reader.
     *
     * So it is asked about. "Keep separate" is the safe answer and stays first;
     * "use one shared style" recolours the panels, which changes what the image
     * looks like and is therefore never the default. Nothing here alters a
     * scientific colour without being told to.
     */
    legendPopover(panels) {
        const channels = panels.every((panel) => panel.legend.channels);
        const plugins = panels.every((panel) => panel.legend.plugins);
        const clashes = FigureContextBar.colourConflicts(panels);

        return `
            <label class="fb-check">
                <input type="checkbox" data-field="legend_channels"
                       ${channels ? "checked" : ""}> Channels
            </label>
            <label class="fb-check">
                <input type="checkbox" data-field="legend_plugins"
                       ${plugins ? "checked" : ""}> Overlays
            </label>
            <p class="fb-muted fb-popover-note">Drawn from what was recorded when the panel
                was captured, never from the plugins that happen to be open now.</p>
            ${clashes.length ? `
            <div class="fb-conflict">
                <strong>Selected panels use different colors for
                    ${FigureSchema.escapeHtml(clashes.map((c) => c.name).join(", "))}.</strong>
                <p class="fb-muted fb-popover-note">One legend for both would say something
                    the panels do not.</p>
                <button type="button" class="fb-menu-item" data-more="legend_keep">
                    Keep them separate</button>
                <button type="button" class="fb-menu-item" data-more="legend_share">
                    Use one shared style</button>
            </div>` : ""}`;
    }

    /**
     * Markers that are drawn in more than one colour across a selection.
     *
     * Pure and static, so the rule can be checked without a browser. Compared
     * on the name the legend would PRINT rather than on the channel key: two
     * images can key the same marker differently, and a reader compares the
     * words.
     */
    static colourConflicts(panels) {
        const seen = new Map();
        for (const panel of panels) {
            for (const channel of panel.scene.channels || []) {
                if (channel.visible === false) continue;
                const name = channel.fullname_at_capture || channel.key;
                const colour = `${channel.color.r},${channel.color.g},${channel.color.b}`;
                if (!seen.has(name)) seen.set(name, new Set());
                seen.get(name).add(colour);
            }
        }
        return Array.from(seen.entries())
            .filter(([, colours]) => colours.size > 1)
            .map(([name, colours]) => ({ name: name, colours: Array.from(colours) }));
    }

    typePopover(annotation) {
        return `
            <label class="control-label" for="fb_ctx_size">Size (pt)</label>
            <input id="fb_ctx_size" class="fb-input fb-input-tiny" type="number"
                   min="4" max="96" step="0.5" data-field="font_size_pt"
                   value="${annotation.style.font_size_pt}">

            <label class="control-label" for="fb_ctx_align">Alignment</label>
            <select id="fb_ctx_align" class="fb-select" data-field="align">
                ${["left", "center", "right"].map((value) =>
                    `<option value="${value}" ${annotation.style.align === value ? "selected" : ""}
                    >${value}</option>`).join("")}
            </select>`;
    }

    /**
     * Colour, as the OS picker plus the figure's own text colour.
     *
     * No palette of "nice" colours. An annotation on a scientific figure is
     * usually meant to match something already in it, and offering a dozen
     * suggestions invites picking one that nearly does.
     */
    colourPopover(annotation) {
        return `
            <label class="control-label" for="fb_ctx_colour">Color</label>
            <input id="fb_ctx_colour" class="fb-input" type="color" data-field="color"
                   value="${FigureSchema.escapeHtml(annotation.style.color)}">`;
    }

    strokePopover(annotation) {
        const fills = annotation.type === "rect" || annotation.type === "ellipse";
        return `
            <label class="control-label" for="fb_ctx_width">Line width (pt)</label>
            <input id="fb_ctx_width" class="fb-input fb-input-tiny" type="number"
                   min="0" max="20" step="0.25" data-field="line_width_pt"
                   value="${annotation.style.line_width_pt}">
            ${fills ? `
            <label class="fb-check">
                <input type="checkbox" data-field="fill_on"
                       ${annotation.style.fill ? "checked" : ""}> Filled
            </label>
            <input class="fb-input" type="color" data-field="fill"
                   value="${FigureSchema.escapeHtml(annotation.style.fill || "#ffffff")}">` : ""}`;
    }

    splitPopover() {
        return `
            <p class="fb-muted fb-popover-note">One panel per channel, sharing this panel's
                exact crop and window &mdash; linked, so resizing one resizes the row.</p>
            <button type="button" class="fb-menu-item" data-split="with_composite">
                Composite + channels</button>
            <button type="button" class="fb-menu-item" data-split="channels_only">
                Channels only</button>`;
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
        const arrange = event.target.closest("[data-arrange]");
        if (arrange) {
            this.handlers.onArrange?.(arrange.dataset.arrange);
            this.closePopover();
            return;
        }
        const split = event.target.closest("[data-split]");
        if (split) {
            this.handlers.onSplit?.(split.dataset.split);
            this.closePopover();
            return;
        }
        const more = event.target.closest("[data-more]");
        if (!more) return;
        const ids = this.ids.slice();
        const act = more.dataset.more;

        // The two that stay open: one moves to a second popover, and the other
        // is a form that has not been filled in yet.
        if (act === "pixelsize") {
            this.openPopover(this._anchor, "pixelsize");
            return;
        }
        if (act === "apply_pixelsize") {
            this.applyPixelSize();
            return;
        }

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
            legend_keep: () => this.applyLegend({ channels: true }),
            legend_share: () => this.shareLegendColours(),
            reset_bar: () => this.resetPosition(),
            accept_source: () => {
                const panel = this.state.panel(ids[0]);
                if (panel) this.handlers.onAcceptSource?.(panel.source_id);
            },
        }[act] || (() => {}))();
        this.closePopover();
    }

    selectedPanels() {
        return this.ids.map((id) => this.state.panel(id)).filter(Boolean);
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
        const annotation = this.ids.length === 1
            ? this.state.document.annotations[this.ids[0]] : null;
        if (annotation) {
            this.annotationFieldChanged(field, input, annotation);
            return;
        }
        // Numbering is the figure's, not a panel's: every label on the page is
        // drawn from it, so it goes to the document rather than to whichever
        // panel happened to be selected.
        if (field === "label_style") {
            this.handlers.onSettingsChange?.({ label_style: input.value });
            return;
        }

        const panels = this.selectedPanels();
        if (!panels.length) return;
        const single = panels.length === 1 ? panels[0] : null;

        if (field === "scalebar") {
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar, visible: input.checked } }));
            return;
        }
        if (field === "scalebar_length") {
            const target = input.value ? parseFloat(input.value) : null;
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar, target_um: target } }));
            return;
        }
        if (field === "legend_channels" || field === "legend_plugins") {
            const key = field === "legend_channels" ? "channels" : "plugins";
            this.applyToPanels(panels, (panel) => ({
                legend: { ...panel.legend, [key]: input.checked } }));
            return;
        }

        if (!single) return;
        const changes = {};
        if (field === "title") changes.title = input.value;
        else if (field === "label") {
            // Typing a label makes it the user's; it stops renumbering when the
            // page is rearranged, which is the whole difference between the two.
            changes.label = { ...single.label, text: input.value, auto: false };
            const auto = this.popover?.querySelector('[data-field="label_auto"]');
            if (auto) auto.checked = false;
        } else if (field === "label_auto") {
            changes.label = { ...single.label, auto: input.checked };
        } else if (field === "label_visible") {
            changes.label = { ...single.label, visible: input.checked };
        } else return;

        this.handlers.onPanelChange?.(single.panel_id, changes);
    }

    /** One change, across the selection, as ONE undo step. */
    applyToPanels(panels, changesFor) {
        this.handlers.onPanelsChange?.(
            panels.map((panel) => ({ panel_id: panel.panel_id, changes: changesFor(panel) })));
    }

    applyLegend(which) {
        const panels = this.selectedPanels();
        this.applyToPanels(panels, (panel) => ({
            legend: { ...panel.legend, ...which } }));
    }

    /**
     * Make every selected panel draw a marker the same colour.
     *
     * The FIRST panel's colour wins, because it is the one at the top left and
     * the one the user was looking at when they asked. Every other panel is
     * recoloured and re-rendered, which is why this is a button and not a
     * default: it changes what the images look like.
     */
    shareLegendColours() {
        const panels = this.selectedPanels();
        const canonical = new Map();
        for (const panel of panels) {
            for (const channel of panel.scene.channels || []) {
                const name = channel.fullname_at_capture || channel.key;
                if (!canonical.has(name)) canonical.set(name, { ...channel.color });
            }
        }
        this.handlers.onShareLegendColours?.(
            panels.map((panel) => panel.panel_id), canonical);
    }

    applyPixelSize() {
        const input = this.popover?.querySelector("#fb_ctx_mpp");
        const value = parseFloat(input && input.value);
        if (!Number.isFinite(value) || value <= 0) return;
        const sources = this.uncalibratedSources(this.selectedPanels());
        this.handlers.onSetPixelSize?.(sources.map((source) => source.source_id), value);
        this.closePopover();
    }

    annotationFieldChanged(field, input, annotation) {
        const style = {};
        if (field === "font_size_pt" || field === "line_width_pt") {
            const value = parseFloat(input.value);
            if (!Number.isFinite(value)) return;
            style[field] = value;
        } else if (field === "align" || field === "color") {
            style[field] = input.value;
        } else if (field === "fill") {
            style.fill = input.value;
        } else if (field === "fill_on") {
            // An empty string is how the schema says "no fill"; the colour
            // input keeps its value so unticking and re-ticking comes back to
            // the same colour rather than to black.
            style.fill = input.checked
                ? (this.popover?.querySelector('[data-field="fill"]')?.value || "#ffffff")
                : "";
        } else return;

        this.handlers.onAnnotationChange?.(annotation.annotation_id, { style: style });
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
     * replaced. Not persisted either -- it is a way out of a collision on one
     * page, not a preference.
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
        this.position();
    }
}
