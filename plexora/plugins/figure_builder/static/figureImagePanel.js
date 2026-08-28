/**
 * The image sidebar: everything a placed PANEL has that is not its position.
 *
 * Quick Edit, the round trip to the viewer, the split, the panel's letter, the
 * scale bar, the colour bar, the captions on the image, and copying one panel's
 * rendering onto others.
 *
 * ## Why it is a panel and not six popovers
 *
 * These were six buttons on the floating bar, each opening a popover of its
 * own. That bar sits ON the artwork, so every one of them covered the thing it
 * was about; only one could be open at a time, so setting a caption and then a
 * scale bar was two round trips through a bar that had moved in between; and
 * none of them could be left open while the user looked at the result. Text,
 * shapes and lines each moved into this strip for the same reasons, and a
 * panel's properties are the last set that had not.
 *
 * CONTEXTUAL, in the same strip as those three. `FigureWorkspace.SIDEBARS`
 * names them and `contextSidebar` settles which one has it -- a panel that
 * showed and hid itself is how two of them ended up stacked. It comes LAST in
 * that order: a selection of a caption and a panel is more usefully described
 * by the caption's panel, which has controls that only apply to one object,
 * than by this one, which mostly does not.
 *
 * ## Several panels at once is the normal case
 *
 * A figure is a row of crops of one slide. So EVERY property here applies to
 * the whole selection in ONE commit -- see `applyToPanels` -- and only the four
 * things that cannot mean anything for several panels at once (Quick Edit, the
 * viewer, the split, the panel's own letter) show when one is selected.
 *
 * The captions used to be in the second group: adding worked across a
 * selection, but the LIST of existing ones was single-panel only, on the
 * argument that six panels' captions interleaved with nothing saying which row
 * belonged to which image is a list nobody could edit. That is true of a list
 * that interleaves them and false of one that MERGES them: `labelRows` folds
 * the selection's captions into one row per distinct text, so "DNA_2" written
 * on four panels is one row, and renaming it to "DNA" renames it on all four.
 * Which is the thing anybody was going to do.
 *
 * ## Rows rather than sections
 *
 * A scale bar is one decision -- how long, in what unit, which corner, what
 * colour, on or off -- and it used to be spread over two sections with eight
 * other fields between them, so the commonest job here was a scroll. The two
 * controls that could not shrink to fit a row are behind `FigureChoiceField`:
 * the nine anchors, which were a keypad three rows tall, and the unit, which
 * has to read as "µm" closed and "Micrometres (µm)" open.
 */
class FigureImagePanel {

    constructor({ root, canvas, state, handlers, onClose }) {
        this.root = root;
        this.canvas = canvas;
        this.state = state;
        this.handlers = handlers || {};
        this.onClose = onClose || (() => {});
        //: The panel ids currently described, in selection order.
        this.ids = [];
        //: Shut by hand for THIS selection. Cleared when the selection moves,
        //: so closing it is "not for these" rather than "never again".
        this.dismissed = false;
        //: The caption typed but not yet added, with the size, colour and
        //: corner chosen for it, and which PRESET is armed. Held on the panel
        //: rather than read out of the DOM at Add time, because the panel
        //: redraws on every document change and a half-typed label would not
        //: survive one.
        this.draft = { text: "", preset: "", position: "top_left",
                       color: "#ffffff", size_pt: null };
        //: Which folds are open. Held here for the same reason `draft` is: the
        //: panel redraws on every document change, and a section that closed
        //: itself every time a number in it was typed would be unusable.
        this.openSections = new Set();
        //: Whether the `?` beside Copy and Apply has been pressed. Held here
        //: for the same reason the folds are: pressing it is a change to the
        //: panel, not to the figure, and nothing redraws it but this panel.
        this.helpOpen = false;
        //: Where the keyboard should land on the NEXT redraw, when that is not
        //: where it is now. Only a reorder sets it -- see `moveRowTo`.
        this.pendingFocus = null;
        //: The SortableJS instance over the caption list, rebuilt on every
        //: render because the list is rebuilt on every render. See bindSorting.
        this.sorter = null;
    }

    setup() {
        if (!this.root) return;
        this.root.addEventListener("input", (event) => this.changed(event));
        this.root.addEventListener("change", (event) => this.changed(event));
        this.root.addEventListener("click", (event) => this.clicked(event));
        this.root.addEventListener("focusin", (event) => this.focusedIn(event));
        this.root.addEventListener("keydown", (event) => this.keyDown(event));
    }

    /**
     * A field showing a chosen preset selects itself when it is focused.
     *
     * The Labels field is both "which preset" and "what to type", so while a
     * preset is armed it holds that preset's NAME. Without this, typing over
     * "Channels" edits the word -- the first keystroke gives you "Channelsx"
     * as a literal caption, which is nobody's intention. Selected, the first
     * keystroke replaces it, which is what a combobox does everywhere else.
     */
    focusedIn(event) {
        const input = event.target;
        if (!this.preset) return;
        if (input?.dataset?.field !== "new_label_text") return;
        try { input.select(); } catch (error) { /* not a text field */ }
    }

    /**
     * ArrowDown, from the Labels field into the list behind it.
     *
     * That popover opens with `keepFocus` so the field can still be typed in,
     * which leaves the list unreachable from the keyboard unless something
     * offers the way in. This is the way in, and it is the one every other
     * combobox uses.
     */
    keyDown(event) {
        // The reorder handle. Dragging is a pointer gesture, so the arrows are
        // what keep the list reorderable at all without one -- and they are
        // what a held key walks the row with, which `pendingFocus` follows.
        const grip = event.target?.dataset?.grip;
        if (grip !== undefined) {
            const step = { ArrowUp: -1, ArrowDown: 1 }[event.key];
            if (step === undefined) return;
            event.preventDefault();
            this.moveRowTo(Number(grip), Number(grip) + step);
            return;
        }
        if (event.key !== "ArrowDown") return;
        if (event.target?.dataset?.choice !== "new_label_preset") return;
        if (!FigureChoiceField.isOpenFor("new_label_preset")) return;
        event.preventDefault();
        FigureChoiceField.first();
    }

    // -- dragging a caption row ----------------------------------------------

    /**
     * Reordering runs on SortableJS, which core already uses for exactly this.
     *
     * It was hand-rolled HTML5 drag-and-drop -- `draggable` on the handle,
     * `dragover`/`drop` on the rows -- and it did not reorder anything. That
     * API is the wrong tool here whatever the immediate cause was: it needs a
     * `dragstart` that survives every ancestor's pointer handling (this panel
     * floats over a canvas that binds `pointerdown` for panning, marquee and
     * tool arming), a `dragover` that calls `preventDefault` on every frame or
     * the drop is silently refused, and it competes with the workspace's own
     * file-drop listener. `toolLoader.ensureSortable` reorders the tool cards
     * from a `.grip` with six lines and no such conditions, and `Sortable` is a
     * global on this page: `vendor.js` puts it there and `base.html` loads that
     * bundle, which the figure workspace extends.
     *
     * Rebound on every render, because this panel replaces its own innerHTML on
     * every document change and the instance's element goes with it. Destroying
     * first is what keeps that from leaving one behind per keystroke.
     *
     * The keyboard route does not go through this at all -- see `keyDown`. If
     * the bundle is ever absent, the arrows still reorder.
     */
    bindSorting() {
        this.sorter?.destroy();
        this.sorter = null;
        const list = this.root.querySelector?.(".fb-label-list");
        if (!list || typeof window === "undefined"
            || typeof window.Sortable !== "function") return;
        this.sorter = new window.Sortable(list, {
            handle: ".fb-grip",
            draggable: ".fb-label-row",
            animation: 150,
            chosenClass: "is-dragging",
            ghostClass: "is-drop-target",
            // Sortable has already moved the DOM; this writes the same move to
            // the document, and the redraw that follows rebuilds the list from
            // it. `oldIndex`/`newIndex` are the row's places in the merged list,
            // which is exactly what `moveRowTo` addresses by.
            onEnd: (event) => {
                if (event.oldIndex === event.newIndex) return;
                this.moveRowTo(event.oldIndex, event.newIndex);
            },
        });
    }

    /**
     * What the Labels row can add besides a word somebody typed.
     *
     * Both are things a user would otherwise type once per panel and get subtly
     * wrong: the image's name, and one caption per channel on show. "Channels"
     * is the reason captions in the same corner had to start stacking -- it
     * adds three or four at once, and three names on top of each other is a
     * smudge rather than a legend.
     *
     * There is no "Text you type" row any more, and there is no picker beside
     * the field either. The FIELD is the picker: clicking it offers these two,
     * and typing in it is how they are let go of again -- which is what that
     * row said, spelled as an option somebody had to find and choose.
     */
    static get PRESETS() {
        return [
            { value: "image_name", short: "", name: "Image name" },
            { value: "channels", short: "", name: "Channels" },
        ];
    }

    /** The armed preset's entry, or null for "whatever is typed". Looked up
     *  rather than taken from `FigureChoiceField.option`, whose job is to never
     *  leave a button blank -- here "none of them" is a real answer. */
    get preset() {
        const armed = (this.draft || {}).preset;
        return FigureImagePanel.PRESETS.find(
            (entry) => entry.value === armed) || null;
    }

    /** The scale-bar units offered, as choice-field options: the symbol on the
     *  button, the full name in the list. */
    static unitOptions() {
        return FigureSchema.SCALEBAR_UNIT_CHOICES.map((entry) => ({
            value: entry.key, short: entry.symbol, name: entry.name,
        }));
    }

    // -- what it shows -------------------------------------------------------

    get panels() {
        return this.ids.map((id) => this.state.panel(id)).filter(Boolean);
    }

    get single() {
        return this.ids.length === 1 ? this.state.panel(this.ids[0]) : null;
    }

    update(ids) {
        if (!this.root) return;
        const placed = (ids || []).filter((id) => {
            const panel = this.state.panel(id);
            return Boolean(panel && panel.placement);
        });
        if (placed.join(",") !== this.ids.join(",")) this.dismissed = false;
        this.ids = placed;
        if (!placed.length) {
            this.root.innerHTML = "";
            return;
        }
        this.render();
    }

    /** Whether there is anything here worth the strip. */
    get wants() {
        return this.ids.length > 0 && !this.dismissed;
    }

    /** Bring it back after it was shut. */
    reveal() {
        this.dismissed = false;
    }

    /**
     * The panel, top to bottom.
     *
     * Four named things and one fold. The fold is the panel's own A/B/C, which
     * is set once when the figure is laid out and then left alone; the four
     * that are not folded are the four somebody opened this panel to change.
     *
     * It was seven sections and about sixty controls, then seven folds. Both
     * were answers to the same problem -- a three-hundred-pixel column and
     * fourteen hundred pixels of controls -- and neither was the right one. The
     * right one was that a scale bar is FIVE controls and one row, not eleven
     * controls and two sections.
     */
    render() {
        const panels = this.panels;
        if (!panels.length) return;
        const focus = this.focused();
        const single = this.single;

        this.root.innerHTML = `
            <header class="fb-side-heading">
                <span class="fb-side-title">${panels.length === 1 ? "Image"
                    : `${panels.length} images`}</span>
                <button type="button" class="fb-icon-button" data-close="1"
                        title="Close">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                </button>
            </header>
            ${this.actionsSection(single)}
            ${this.scalebarSection(panels)}
            ${this.colorbarSection(panels)}
            ${this.labelsSection(panels)}
            ${single ? this.panelLabelSection(single) : ""}`;

        this.restore(focus);
        this.bindSorting();
    }

    /**
     * One collapsible group.
     *
     * `body` is a THUNK, not a string: a collapsed section costs nothing to
     * render. The summary beside the title is not decoration either -- a fold
     * whose face says only "Panel label" makes the user open it to find out
     * whether there is one.
     */
    fold(id, title, summary, body) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const open = this.openSections.has(id);
        return `<section class="fb-side-section">
            <button type="button" class="fb-side-disclosure" data-fold="${id}"
                    aria-expanded="${open ? "true" : "false"}">
                <span class="fas ${open ? "fa-chevron-down" : "fa-chevron-right"}
                             fb-fold-caret" aria-hidden="true"></span>
                <span class="fb-fold-title">${escape(title)}</span>
                <span class="fb-fold-summary">${escape(summary)}</span>
            </button>
            ${open ? body() : ""}
        </section>`;
    }

    /**
     * Everything done TO the picture, in two rows at the top of the panel.
     *
     * Two ways back to the image on the first -- Quick Edit edits a view, the
     * viewer opens a view -- and three things done to the panel on the second:
     * split it into a composite and its channels, copy how it is rendered,
     * paste that onto others. It was two blocks at opposite ends of the panel,
     * with the scale bar and the captions between them, which put the two
     * halves of "make these eight panels match" as far apart as the column
     * allows.
     *
     * The first row is single-panel only and says so by being empty: Quick Edit
     * edits one view and a split replaces one panel, so their `applies` are
     * what leaves the row out, not this method. `apply_rendering` is the
     * opposite -- a multiple selection is the case it exists for -- so the
     * second row is drawn at every selection size.
     *
     * The paragraph under it is a `?` now. It was four permanent lines of prose
     * explaining two buttons, at the bottom of a panel whose every other row is
     * controls; the sentence is worth having and worth being asked for.
     */
    actionsSection(single) {
        const context = this.registryContext();
        const editable = single ? FigureActions.reopenable(single, context) : true;
        return `
            <section class="fb-side-section">
                ${this.actionRow("actions", context, { tight: true })}
                ${editable ? "" : `<p class="fb-side-note">This panel came from an
                    image the figure no longer references, so it cannot be
                    reopened.</p>`}
                ${this.actionRow(["split", "rendering"], context, {
                    tight: true,
                    after: this.iconButton("rendering_help", "fa-question",
                                           this.renderingNote()) })}
                ${this.helpOpen
                    ? `<p class="fb-side-note">${this.renderingNote()}</p>` : ""}
            </section>`;
    }

    /** What the three verbs on that row actually do. The tooltip on the `?` and,
     *  once it is pressed, the paragraph -- one sentence, written once, rather
     *  than a tooltip and a note that drift apart.
     *
     *  Composite is described here rather than in its own tooltip because it is
     *  the one whose NAME is the least self-explanatory of the three: "Copy"
     *  and "Apply" say what they do and only leave open what they carry. */
    renderingNote() {
        const split = "Composite replaces this panel with one per visible "
            + "channel, keeping the composite at the head of the row and this "
            + "panel's exact crop on all of them. ";
        return split + (this.handlers.hasRenderClipboard?.()
            ? "Copy and Apply move channel colors and contrast only; across two "
              + "images, channels are matched by name."
            : "Copy and Apply move this panel's channel colors and contrast onto "
              + "other panels.");
    }

    /**
     * The figure's own A/B/C for this panel.
     *
     * "Panel label", not "Label", and a fold of its own: this and the free
     * captions on the image were two different things called Label and Labels,
     * stacked in one column, with only a docstring saying which was which.
     *
     * Numbering is not here. It is the whole figure's -- see
     * FigureWorkspace.openNumbering -- and a document-wide setting reachable
     * only by selecting an image looked like a property of that image.
     */
    panelLabelSection(panel) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const summary = !panel.label.visible ? "Hidden"
            : panel.label.auto ? "Automatic" : (panel.label.text || "Blank");
        return this.fold("panel_label", "Panel label", summary, () => `
            ${this.field("Letter", "fb_image_label", `
                <input id="fb_image_label" class="fb-input fb-input-tiny" type="text"
                       data-field="label" maxlength="8" placeholder="auto"
                       value="${escape(panel.label.auto ? "" : panel.label.text)}">
                <label class="fb-check"
                       title="Renumber when the panels are rearranged">
                    <input type="checkbox" data-field="label_auto"
                           ${panel.label.auto ? "checked" : ""}> Auto
                </label>`)}
            <label class="fb-check">
                <input type="checkbox" data-field="label_visible"
                       ${panel.label.visible ? "checked" : ""}> Show the label
            </label>
            <p class="fb-side-note">Automatic letters follow reading order &mdash; left
                to right, top to bottom. The scheme they are drawn in is the whole
                figure's, under the page menu.</p>`);
    }

    // -- the scale bar -------------------------------------------------------

    /**
     * The bar, in three rows: what it is calibrated against, what it says, and
     * how it is drawn.
     *
     * The pixel size is a field rather than a note that appeared only when an
     * image had none -- which meant the one case it could not help with was the
     * one that matters most: a calibration that is present and WRONG. Every
     * scale bar in the figure is derived from it, and a bar drawn from a wrong
     * pixel size looks exactly like one that is right.
     *
     * An image that recorded none still gets a bar, measured in image pixels.
     * That is a true statement about the picture -- and the alternative, which
     * is what this did before, was that switching the bar on, typing a length
     * and pressing everything in the row all did nothing and said nothing.
     */
    scalebarSection(panels) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const bar = panels[0].scalebar;
        const pixel = this.pixelSize(panels);
        const length = this.lengthField(panels);
        //: Stated by the panel rather than by the stored unit: an uncalibrated
        //: image reads "px" whatever it has on file, because that is the bar it
        //: is actually going to get.
        const unit = this.unitOf(panels[0]);
        const uncalibrated = panels.some((panel) => this.pixelBar(panel))
            && bar.unit !== "px";

        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Scalebar</h3>
                <div class="fb-row fb-row-stacked">
                    ${this.stack("Pixel size", "fb_image_mpp",
                        this.suffixed("µm", `<input id="fb_image_mpp"
                               class="fb-input fb-input-tiny" type="text"
                               inputmode="decimal"
                               placeholder="${escape(pixel.placeholder)}"
                               value="${escape(pixel.value)}">`,
                            "fb-input-unit-mpp"))}
                    ${this.iconButton("pixel_size", "fa-rotate-right", "Update",
                                      "Update the pixel size and remeasure the bar")}
                </div>
                ${uncalibrated ? `<p class="fb-side-note">No pixel size recorded, so
                    the bar is measured in image pixels. Type one and press Update to
                    measure it in ${escape(FigureImagePanel.unitName(bar.unit))}
                    instead.</p>` : ""}
                <div class="fb-row fb-row-stacked">
                    ${this.stack("Length", "fb_image_bar_len",
                        `<span class="fb-input-unit fb-input-unit-wide">
                            <input id="fb_image_bar_len"
                                   class="fb-input fb-input-tiny" type="text"
                                   inputmode="decimal" data-field="scalebar_length"
                                   placeholder="${escape(length.placeholder)}"
                                   value="${escape(length.value)}">
                            ${FigureChoiceField.button({
                                field: "scalebar_unit", value: unit,
                                options: FigureImagePanel.unitOptions(),
                                label: "Unit", variant: "suffix" })}
                        </span>`)}
                    ${FigureChoiceField.button({
                        field: "scalebar_position", value: bar.position, layout: "grid",
                        options: FigureChoiceField.anchorOptions(),
                        label: "Scalebar location" })}
                    ${FigureColorField.swatch({
                        field: "scalebar_color", value: bar.color,
                        label: "Scalebar color" })}
                    ${this.eyeToggle({
                        on: panels.every((panel) => panel.scalebar.visible),
                        act: "scalebar_visible", name: "scale bar" })}
                </div>
                <div class="fb-row fb-row-stacked">
                    ${this.stack("Height", "fb_image_bar_thick",
                        this.mmInput("scalebar_thickness", bar.thickness_mm,
                                     "fb_image_bar_thick", "Scalebar height"))}
                    ${this.stack("Margin", "",
                        this.mmInput("scalebar_margin", bar.margin_mm, "",
                                     "Scalebar margin"))}
                    ${this.stack("Size", "",
                        this.ptInput("scalebar_label_size", bar.label_size_pt,
                                     "Scalebar label size"))}
                    ${this.eyeToggle({
                        on: panels.every((panel) => panel.scalebar.label),
                        act: "scalebar_label", name: "length beside the bar" })}
                </div>
            </section>`;
    }

    /**
     * Whether a panel's bar is measured in image pixels rather than in microns.
     *
     * Two ways to land here and both draw the same bar: the panel asked for
     * "px", or there is no calibration and a physical bar cannot honestly be
     * drawn at all. The second is the one that was missing -- an uncalibrated
     * image got no bar and no explanation, so every control in this section
     * appeared to be broken. `compose.scale_bar` and
     * `FigureCanvas.scaleBarLength` decide it the same way, and they have to:
     * this is what the Length field's number MEANS.
     */
    pixelBar(panel) {
        if (panel.scalebar.unit === "px") return true;
        const source = this.state.source(panel.source_id);
        const size = source && source.pixel_size;
        return !(size && size.value > 0);
    }

    /**
     * What the images say they are calibrated at, for the field to show.
     *
     * A selection whose images disagree says so instead of picking one -- two
     * magnifications in one row is ordinary, and a single number would describe
     * neither. Typing over "Mixed" and pressing Update sets them all, which is
     * the one thing anybody wants from that state.
     */
    pixelSize(panels) {
        const seen = new Set();
        for (const panel of panels) {
            const source = this.state.source(panel.source_id);
            const size = source && source.pixel_size;
            seen.add(size && size.value > 0 ? Number(size.value.toFixed(6)) : null);
        }
        if (seen.size > 1) return { value: "", placeholder: "Mixed" };
        const only = Array.from(seen)[0];
        if (!only) return { value: "", placeholder: "NA" };
        return { value: String(only), placeholder: "NA" };
    }

    /** The unit actually in force. "auto" is still accepted by the schema -- no
     *  figure made before units existed is restyled by reopening it -- but it
     *  reads as a mode rather than as a unit, so the button says what "auto"
     *  actually prints. An uncalibrated panel says "px" whatever it has stored,
     *  because that is the bar it is going to get. */
    unitOf(panel) {
        if (this.pixelBar(panel)) return "px";
        const unit = panel.scalebar.unit;
        return unit && unit !== "auto" ? unit : "um";
    }

    /** A stored unit's full name, for the sentence that explains what supplying
     *  a pixel size would switch the bar to. */
    static unitName(unit) {
        const entry = FigureChoiceField.option(
            FigureImagePanel.unitOptions(), unit && unit !== "auto" ? unit : "um");
        return entry ? entry.short : "µm";
    }

    /**
     * The length box: what is typed in it, and what it says when it is empty.
     *
     * Empty means "a round number that fits", which is a different answer per
     * panel and is why it is stored as null rather than as whatever fits this
     * one. The placeholder is that answer for the first panel, so the field
     * still shows the number the bar is actually going to be.
     */
    lengthField(panels) {
        const bar = panels[0].scalebar;
        const px = this.pixelBar(panels[0]);
        const key = px ? "target_px" : "target_um";
        const target = bar[key] || null;
        const same = panels.every((panel) => (panel.scalebar[key] || null) === target
            && this.pixelBar(panel) === px
            && panel.scalebar.unit === bar.unit);
        if (!same) return { value: "", placeholder: "Mixed" };
        const per = px ? 1 : FigureImagePanel.unitUm(this.unitOf(panels[0]));
        if (target) {
            return { value: String(Number((target / per).toFixed(4))),
                     placeholder: "Auto" };
        }
        const viewport = panels[0].scene.viewport;
        const span = px ? (viewport && viewport.w)
            : FigureSchema.physicalWidthUm(this.state.source(panels[0].source_id),
                                           viewport);
        const automatic = span ? FigureSchema.scaleBarLength(span) : null;
        return { value: "",
                 placeholder: automatic
                     ? String(Number((automatic / per).toFixed(4))) : "Auto" };
    }

    // -- the colour bar ------------------------------------------------------

    /**
     * An intensity scale for the rendered channels.
     *
     * Off by default and never turned on automatically: a colour bar is a claim
     * that the intensities are quantitative, and most panels are not making it.
     * When it is on, the ticks are the channel's own display window in raw
     * units -- the numbers the contrast was set against -- and there is one bar
     * per channel because each has its own window.
     */
    colorbarSection(panels) {
        const bar = panels[0].colorbar;

        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Colorbar</h3>
                <div class="fb-row fb-row-stacked">
                    ${this.stack("Thickness", "fb_image_cb_thick",
                        this.mmInput("colorbar_thickness", bar.thickness_mm,
                                     "fb_image_cb_thick", "Colorbar thickness"))}
                    ${this.stack("Gap", "",
                        this.mmInput("colorbar_gap", bar.gap_mm, "", "Colorbar gap"))}
                </div>
                <div class="fb-row fb-row-stacked">
                    ${this.stack("Ticks", "fb_image_cb_ticks",
                        `<select id="fb_image_cb_ticks" class="fb-select fb-select-tiny"
                                 data-field="colorbar_ticks">
                            ${[0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map((count) =>
                                `<option value="${count}"${bar.ticks === count ? " selected" : ""}
                                >${count === 0 ? "None" : count}</option>`).join("")}
                        </select>`)}
                    ${this.stack("Tick", "",
                        this.mmInput("colorbar_tick_length", bar.tick_length_mm, "",
                                     "Tick length"))}
                    ${this.stack("Margin", "",
                        this.mmInput("colorbar_margin", bar.margin_mm, "",
                                     "Colorbar margin"))}
                </div>
                <div class="fb-row fb-row-stacked">
                    ${this.stack("Color", "", FigureColorField.swatch({
                        field: "colorbar_tick_color", value: bar.tick_color,
                        label: "Tick and label color" }))}
                    ${this.stack("Size", "",
                        this.ptInput("colorbar_label_size", bar.label_size_pt,
                                     "Tick label size"))}
                    ${this.stack("Place", "", FigureChoiceField.button({
                        field: "colorbar_position", value: bar.position, layout: "grid",
                        options: FigureChoiceField.anchorOptions(),
                        label: "Colorbar location" }))}
                    ${this.eyeToggle({
                        on: panels.every((panel) => panel.colorbar.visible),
                        act: "colorbar_visible", name: "colour bar" })}
                </div>
            </section>`;
    }

    // -- the captions on the image -------------------------------------------

    /**
     * Free captions drawn on the image.
     *
     * A different thing from the panel's LABEL, which is the figure's own A/B/C
     * and is one per panel by definition, and from a text annotation, which
     * sits on the page and stays behind when the panel is moved. These belong
     * to the image: "Tumor", "40x", a channel's name.
     *
     * They are also what the panel's TITLE and its channel LEGEND used to be.
     * Both are gone: a title was a caption that could only sit under the panel,
     * a legend was a caption per channel that could only sit top-left in white,
     * and the presets behind the text box add exactly that legend as captions
     * the user can then move, recolour and rename.
     *
     * ## The text box IS the preset picker
     *
     * It was a box and a chevron beside it, and the chevron's list opened with
     * "Text you type" -- an option whose only job was to undo the other two.
     * Three controls for one answer. Now the box is a combobox: clicking it
     * offers the two labels the image can supply, typing in it says "neither,
     * this instead", and an armed preset shows its own name where the typing
     * would be. Nothing has to be un-chosen, because typing is the un-choosing.
     */
    labelsSection(panels) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const draft = this.draft || {};
        const rows = this.labelRows();
        const preset = this.preset;
        const open = FigureChoiceField.isOpenFor("new_label_preset");
        const what = "Type a label, or click for one the image can supply";

        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Add Labels</h3>
                <div class="fb-row">
                    <input id="fb_image_new_label" type="text" maxlength="200"
                           class="fb-input fb-input-label${
                               preset ? " is-preset" : ""}"
                           data-field="new_label_text"
                           data-choice="new_label_preset"
                           data-value="${escape(draft.preset || "")}"
                           role="combobox" aria-autocomplete="list"
                           aria-haspopup="listbox"
                           aria-expanded="${open ? "true" : "false"}"
                           autocomplete="off" placeholder="Label"
                           title="${what}" aria-label="${what}"
                           value="${escape(preset ? preset.name
                                                  : (draft.text || ""))}">
                    ${this.ptInput("new_label_size", draft.size_pt ?? null,
                                   "New label size")}
                    ${FigureChoiceField.button({
                        field: "new_label_position", value: draft.position || "top_left",
                        layout: "grid", options: FigureChoiceField.anchorOptions(),
                        label: "Where a new label lands" })}
                    ${FigureColorField.swatch({
                        field: "new_label_color", value: draft.color || "#ffffff",
                        label: "New label color" })}
                    ${this.iconButton("add_label", "fa-plus", "Add",
                                      "Add this label to the selected images",
                                      "fb-icon-button-primary")}
                </div>
                ${rows.length ? `
                <h3 class="fb-side-subheading fb-side-subheading-minor">Edit Labels</h3>
                <div class="fb-label-list">
                    ${rows.map((row, index) =>
                        this.labelRow(row, index, panels.length,
                                      rows.length)).join("")}
                </div>` : ""}
            </section>`;
    }

    /**
     * The selection's captions, MERGED into one row per distinct text.
     *
     * Six panels' captions interleaved in one list, with nothing saying which
     * row belongs to which image, is a list nobody could edit -- which is why
     * this list used to appear for a single panel only. Merged, it is the list
     * anybody wanted: "DNA_2" on four panels is one row, and renaming it
     * renames all four.
     *
     * A row carries the (panel, label) pairs it stands for, and every edit
     * recomputes them from the CURRENT document rather than re-matching by text
     * at apply time. Text is what merges rows; it is never what addresses them,
     * because the first keystroke of a rename would stop matching.
     */
    labelRows() {
        const rows = [];
        const byText = new Map();
        for (const panel of this.panels) {
            for (const entry of panel.labels || []) {
                const key = String(entry.text || "").trim();
                let row = byText.get(key);
                if (!row) {
                    row = { key: key, text: entry.text,
                            size_pt: entry.size_pt ?? null,
                            position: entry.position, color: entry.color,
                            refs: [], panels: new Set() };
                    byText.set(key, row);
                    rows.push(row);
                }
                row.refs.push({ panel_id: panel.panel_id, label_id: entry.label_id });
                row.panels.add(panel.panel_id);
            }
        }
        return rows;
    }

    /**
     * One row of the merged list.
     *
     * Addressed by INDEX, not by text and not by label id: a row can stand for
     * four labels on four panels, and the text it is keyed on changes under the
     * user's fingers as they rename it.
     *
     * ## Why the reorder is a grip and not two arrows
     *
     * It was one cycling button, then two arrows with the ends disabled. Both
     * were the same mistake in different sizes: a press renumbers the row out
     * from under the pointer, so what the second press does depends on what
     * took the vacated slot, and moving a row three places is three presses
     * that each need re-reading. Dragging says where the row is going in one
     * gesture, and it is the only arrangement whose control does not have to
     * be re-aimed between presses. It also gives the row back two columns,
     * which is most of what the panel needed to stop being 380px wide.
     *
     * The handle is a real `<button>`, and the arrow keys still move the row
     * while it has the keyboard -- dragging is a mouse gesture and cannot be
     * the only way to do this. `fa-grip-vertical` is what core's own tool
     * cards use for exactly this gesture (toolLoader.js), so it is both proven
     * to exist and already the tree's word for "drag me".
     */
    labelRow(row, index, selected, total) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const shared = selected > 1 && row.panels.size > 1;
        const named = `Reorder ${row.text || "the blank label"}`;
        return `<div class="fb-label-row" data-row="${index}">
            <button type="button" class="fb-icon-button fb-grip"
                    data-grip="${index}"
                    title="Drag to reorder"
                    aria-label="${escape(named)} \u2014 drag it, or use the arrow keys">
                <span class="fas fa-grip-vertical" aria-hidden="true"></span></button>
            <span class="fb-input-unit fb-input-unit-label${
                    shared ? " is-shared" : ""}">
                <input class="fb-input" type="text" maxlength="200"
                       data-field="label_text" data-row="${index}"
                       aria-label="Label text" value="${escape(row.text)}">
                ${shared ? `<span class="fb-unit-suffix fb-label-count"
                        title="On ${row.panels.size} of the selected images"
                    >&times;${row.panels.size}</span>` : ""}
            </span>
            ${this.ptInput("label_size", row.size_pt, "Label size",
                           `data-row="${index}"`)}
            ${FigureChoiceField.button({
                field: `label_position:${index}`, value: row.position, layout: "grid",
                options: FigureChoiceField.anchorOptions(), label: "Label location" })}
            ${FigureColorField.swatch({
                field: `label_color:${index}`, value: row.color, label: "Label color" })}
            <button type="button" class="fb-icon-button fb-icon-button-danger"
                    data-act="label_delete:${index}" title="Delete"
                    aria-label="Delete label">
                <span class="fas fa-xmark" aria-hidden="true"></span></button>
        </div>`;
    }

    // -- shared controls -----------------------------------------------------

    /**
     * Everything the registry says this selection can be done to, described
     * once and asked for by section.
     *
     * This panel used to hand-build its action buttons AND hand-write the
     * predicate behind them -- a re-typed copy of "can this panel be reopened"
     * that had already drifted from the registry's. See
     * `FigureActions.reopenable`; the drift was a live bug, not a tidiness
     * problem.
     */
    registryContext() {
        const ids = this.ids.slice();
        return {
            ids: ids,
            sel: FigureSelection.describe(ids, this.state, this.canvas),
            canvas: this.canvas,
            state: this.state,
            handlers: this.handlers,
        };
    }

    /** One row of registry-projected buttons. Empty when the section has none,
     *  so a caller can put `${...}` straight into its markup. `tight` keeps the
     *  row on one line -- Quick Edit and the viewer are two halves of "go back
     *  to the image" and belong beside each other. `after` is a member of the
     *  row that is not an action: the `?`, which has to be INSIDE the flex row
     *  to sit at the end of it rather than under it.
     *
     *  These rows do NOT set the panel's width -- see `contain: inline-size` on
     *  `.fb-side-actions`. They take the width the control rows settled and
     *  divide it, which is why the buttons can carry their icons: an icon is
     *  ~20px, and with the buttons as equal columns that was 60px of panel for
     *  a picture of a table beside the word "Composite". Sized to their words
     *  instead, all five fit inside the caption row's width with the icons on.
     */
    actionRow(section, context, extra) {
        const actions = FigureActions.forSidebar(section, context.sel, context);
        if (!actions.length) return "";
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const buttons = actions.map((action) => `
            <button type="button" class="fb-button${
                    extra && extra.primary === action.id ? " fb-button-primary" : ""}"
                    data-act="${action.id}" title="${escape(action.label)}"
                    ${action.isEnabled ? "" : "disabled"}>
                ${action.icon
                    ? `<span class="fas fa-${action.icon}" aria-hidden="true"></span>` : ""}
                ${escape(action.word)}
            </button>`).join("");
        // The buttons are a GRID inside the row, not the row itself: equal
        // columns are a grid's native answer and a flex row's approximate one
        // -- `flex: 1 1 0` divides the row evenly but asks the row for the SUM
        // of the labels, so the longest one gets an even share of a width that
        // was never enough for it and quietly ellipsises. The `?` stays outside
        // the grid, because it is the row's footnote and not a fourth equal.
        return `<div class="fb-side-actions${
            extra && extra.tight ? " fb-side-actions-tight" : ""}">
            <div class="fb-side-actions-equal">${buttons}</div>${
            (extra && extra.after) || ""}</div>`;
    }

    /**
     * A millimetre box. Every distance on a page is in millimetres here, so a
     * bar 0.8 thick is 0.8 whatever the export DPI turns out to be -- a
     * thickness in pixels would mean a different bar at every DPI.
     *
     * `type="text"` with a decimal keypad rather than `type="number"`: a number
     * input reports `selectionStart` as null, and this panel rebuilds itself on
     * every keystroke, so `focused()` could not put the caret back and typing
     * "1.25" landed as "5".
     */
    mmInput(field, value, id, label) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        return this.suffixed("mm",
            `<input class="fb-input fb-input-tiny" type="text" inputmode="decimal"
                    data-field="${field}" ${id ? `id="${id}"` : ""}
                    ${label ? `aria-label="${escape(label)}"` : ""}
                    value="${Number(value).toFixed(2).replace(/\.?0+$/, "")}">`);
    }

    /**
     * A number box with its unit printed INSIDE it, against the right edge.
     *
     * The suffix used to be a sibling of the box, and it cost as much of the
     * row as the box did: three of them and a row could no longer also hold the
     * controls it was about. It takes no pointer events, so clicking "mm" puts
     * the caret in the field the way clicking the field does.
     *
     * `extra` widens ONE of these -- the pixel size, which holds a decimal
     * where every other suffixed box holds a round number. Sized by a class
     * rather than inline so the arithmetic stays beside the padding it has to
     * agree with; see .fb-input-unit-mpp.
     */
    suffixed(unit, control, extra) {
        return `<span class="fb-input-unit${extra ? ` ${extra}` : ""}"
            >${control}<span class="fb-unit-suffix"
            aria-hidden="true">${unit}</span></span>`;
    }

    /**
     * A points box, empty when the size is the figure's own.
     *
     * A dropdown of sizes was the wrong control for the same reason "Hide" was
     * the wrong button: the widest option ("Figure", then "Fig") set the width,
     * and a row of three of them had no room left. Empty-means-inherited needs
     * no option at all, and the placeholder shows the number it will inherit --
     * the same idiom as the Length field, whose placeholder is the round length
     * it will pick if nothing is typed.
     *
     * Narrower than the millimetre boxes it sits beside, via
     * .fb-input-unit-pt: a size is two digits and "pt" is half the width of
     * "mm", so the shared 58px reserved room for neither.
     */
    ptInput(field, value, label, extra) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        return this.suffixed("pt",
            `<input class="fb-input fb-input-tiny" type="text" inputmode="decimal"
                    data-field="${field}" aria-label="${escape(label)}"
                    placeholder="${this.figureSize()}" ${extra || ""}
                    title="Text size in points &mdash; empty follows the figure's own"
                    value="${value === null || value === undefined ? "" : value}">`,
            "fb-input-unit-pt");
    }

    /** The figure's body text size, which is what a caption with none of its own
     *  is drawn at -- see FigureCanvas.scaleBarMarkup, which resolves it the
     *  same way. Defaulted here rather than left blank because this is only a
     *  placeholder: a document still in flight must not show an empty one and
     *  then silently gain a number under the caret. */
    figureSize() {
        const style = this.state.document?.settings?.style;
        const size = Number(style?.font_size_pt);
        return size > 0 ? size : 8;
    }

    /** A name ABOVE its control rather than beside it. Beside, a name costs as
     *  much of the row as the control does, and three pairs would not fit; above,
     *  it costs height the row was already tall enough for, and which box each
     *  name belongs to stops being a question. */
    stack(name, forId, control) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const label = forId
            ? `<label class="fb-stack-name" for="${forId}">${escape(name)}</label>`
            : `<span class="fb-stack-name">${escape(name)}</span>`;
        return `<div class="fb-stack">${label}${control}</div>`;
    }

    /** A bare glyph with its word in the tooltip. The tooltip is the button's
     *  NAME -- "Update", "Add" -- rather than a sentence about it, because the
     *  note above the row tells the user to press Update and there has to be
     *  something on the panel that answers to that.
     *
     *  `extra` is for the one of these that is not a setting but the row's
     *  point: Add, which keeps the filled accent the word had. A symbol was
     *  what the row needed -- "Add" was the only word left in it and it set the
     *  width of the column -- but a symbol that also stopped being the loud
     *  thing on the line would be a row with no visible verb in it at all. */
    iconButton(act, icon, name, does, extra) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        return `<button type="button" class="fb-icon-button${extra ? ` ${extra}` : ""}"
                        data-act="${act}"
                        title="${escape(name)}" aria-label="${escape(does || name)}">
            <span class="fas ${icon}" aria-hidden="true"></span></button>`;
    }

    /**
     * Show or hide, as an eye rather than as a word.
     *
     * "Hide" was the widest thing in a row of five controls, and it had to say
     * what pressing it DID rather than what the state was -- a button reading
     * "Show" beside a bar that was showing. The eye says the state, which is
     * what the rest of the row is describing anyway, and the tooltip says the
     * action.
     *
     * Takes the answer rather than the panels: three of these now, and one of
     * them is not a `visible` flag at all -- the scale bar's caption, which is
     * its own eye because a bar with no number is a figure people publish and a
     * number with no bar is not.
     */
    eyeToggle({ on, act, name }) {
        const does = `${on ? "Hide" : "Show"} the ${name}`;
        return `<button type="button" class="fb-icon-button fb-eye${
                    on ? " is-on" : ""}" data-act="${act}"
                    aria-pressed="${on ? "true" : "false"}"
                    title="${does}" aria-label="${does}">
            <span class="fas ${on ? "fa-eye" : "fa-eye-slash"}"
                  aria-hidden="true"></span></button>`;
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

    /** Which control had the keyboard, and where its caret was. The panel
     *  redraws on every document change and every control here causes one, so
     *  without this typing a caption rebuilds the field under the caret at the
     *  second character. */
    focused() {
        // A move names where the keyboard should END UP rather than where it
        // is, because the row it was on has just been renumbered out from under
        // it. Consumed once: the next redraw is an ordinary one again.
        if (this.pendingFocus) {
            const where = this.pendingFocus;
            this.pendingFocus = null;
            return { where: where, caret: null };
        }
        const active = document.activeElement;
        if (!active || !this.root.contains(active)) return null;
        // An id where there is one; otherwise the field name and, for a row in
        // the label list, which ROW -- the rows are generated and have no ids of
        // their own. A fold's own button is found by its id, so opening one
        // leaves the keyboard on the thing that was pressed.
        const data = active.dataset || {};
        const row = data.row === undefined || data.row === null
            ? "" : `[data-row="${data.row}"]`;
        const where = active.id ? `#${active.id}`
            : data.fold ? `[data-fold="${data.fold}"]`
            : data.choice ? `[data-choice="${data.choice}"]`
            : data.swatch ? `[data-swatch="${data.swatch}"]`
            : data.grip ? `[data-grip="${data.grip}"]`
            : data.act ? `[data-act="${data.act}"]`
            : data.field ? `[data-field="${data.field}"]${row}`
            : null;
        if (!where) return null;
        let caret = null;
        try {
            if (Number.isInteger(active.selectionStart)) {
                caret = [active.selectionStart, active.selectionEnd];
            }
        } catch (error) { caret = null; }
        return { where: where, caret: caret };
    }

    restore(focus) {
        if (!focus) return;
        const again = this.root.querySelector(focus.where);
        if (!again) return;
        again.focus();
        if (focus.caret) {
            try {
                again.setSelectionRange(focus.caret[0], focus.caret[1]);
            } catch (error) { /* a select has no caret to put back */ }
        }
    }

    // -- acting --------------------------------------------------------------

    clicked(event) {
        if (event.target.closest?.("[data-close]")) {
            this.dismissed = true;
            this.onClose();
            return;
        }
        const fold = event.target.closest?.("[data-fold]");
        if (fold) {
            const id = fold.dataset.fold;
            if (this.openSections.has(id)) this.openSections.delete(id);
            else this.openSections.add(id);
            this.render();
            return;
        }
        const choice = event.target.closest?.("[data-choice]");
        if (choice && !choice.disabled) {
            // Read now rather than off the button later: applying a choice
            // redraws this panel, so the element the popover was opened against
            // is detached by the time it reports.
            const field = choice.dataset.choice;
            const spec = FigureImagePanel.choiceSpec(field);
            FigureChoiceField.open(choice, {
                value: choice.dataset.value,
                options: spec.options,
                layout: spec.layout,
                // The Labels field is a text box wearing this: it has to keep
                // the keyboard, or the click that asked for the list is the
                // click that stopped the user typing into it.
                keepFocus: choice.tagName === "INPUT",
                onPick: (value) => this.choicePicked(field, value),
            });
            return;
        }
        const well = event.target.closest?.("[data-swatch]");
        if (well && !well.disabled) {
            // Same reason: by the second colour of a drag inside the OS dialog
            // this element is detached and its dataset is gone with it.
            const field = well.dataset.swatch;
            FigureColorField.open(well, {
                value: well.dataset.value,
                onPick: (hex) => this.colourPicked(field, hex),
            });
            return;
        }
        const button = event.target.closest?.("[data-act]");
        if (!button || button.disabled) return;
        const panels = this.panels;
        // Row actions carry their row's index after a colon, so that one
        // delegated handler serves a list whose length changes.
        const [act, argument] = String(button.dataset.act).split(":");

        // A registry action runs the registry's `run`, which is the same
        // function the right-click menu and the floating bar call. Two copies
        // of "what does Quick Edit do" is how the two surfaces disagreed about
        // when it could be done at all.
        const action = FigureActions.byId(act);
        if (action && action.run && action.surface.includes("sidebar")) {
            action.run(this.registryContext());
            return;
        }

        ({
            pixel_size: () => this.applyPixelSize(panels),
            scalebar_visible: () => this.toggleBar(panels, "scalebar"),
            colorbar_visible: () => this.toggleBar(panels, "colorbar"),
            scalebar_label: () => this.toggleCaption(panels),
            add_label: () => this.addLabel(panels),
            rendering_help: () => {
                this.helpOpen = !this.helpOpen;
                this.render();
            },
            label_delete: () => this.deleteRow(Number(argument)),
        }[act] || (() => {}))();
    }

    /** Which popover a field wants, and what goes in it. Static and pure: one
     *  table answers "what is this control" both for the click that opens it
     *  and for the markup that draws its button. */
    static choiceSpec(field) {
        const name = String(field).split(":")[0];
        if (name === "scalebar_unit") {
            return { layout: "list", options: FigureImagePanel.unitOptions() };
        }
        if (name === "new_label_preset") {
            return { layout: "list", options: FigureImagePanel.PRESETS };
        }
        return { layout: "grid", options: FigureChoiceField.anchorOptions() };
    }

    /** A choice popover reported. `field` may carry a row index. */
    choicePicked(field, value) {
        const panels = this.panels;
        const [name, argument] = String(field).split(":");
        if (name === "scalebar_position") {
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar, position: value } }));
        } else if (name === "colorbar_position") {
            this.applyToPanels(panels, (panel) => ({
                colorbar: { ...panel.colorbar, position: value } }));
        } else if (name === "scalebar_unit") {
            // Both targets survive: the physical length and the pixel length
            // are different facts about the bar, and switching how it is
            // written must not throw either away.
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar, unit: value } }));
        } else if (name === "new_label_position") {
            this.draft = { ...(this.draft || {}), position: value };
            this.render();
        } else if (name === "new_label_preset") {
            this.draft = { ...(this.draft || {}), preset: value };
            this.render();
        } else if (name === "label_position") {
            this.editRow(Number(argument), { position: value });
        }
    }

    /** A colour well was picked from. `field` may carry a row index. */
    colourPicked(field, hex) {
        const panels = this.panels;
        const [name, argument] = String(field).split(":");
        if (name === "scalebar_color") {
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar, color: hex } }));
        } else if (name === "colorbar_tick_color") {
            this.applyToPanels(panels, (panel) => ({
                colorbar: { ...panel.colorbar, tick_color: hex } }));
        } else if (name === "new_label_color") {
            this.draft = { ...(this.draft || {}), color: hex };
            this.render();
        } else if (name === "label_color") {
            this.editRow(Number(argument), { color: hex });
        }
    }

    changed(event) {
        const input = event.target;
        const field = input.dataset && input.dataset.field;
        if (!field) return;

        // Numbering used to be handled here, with a note explaining that it is
        // the FIGURE's rather than this panel's. It is now WHERE it is the
        // figure's: FigureWorkspace.openNumbering, off the page menu.

        const panels = this.panels;
        if (!panels.length) return;

        if (field.startsWith("new_label_")) {
            const armed = Boolean(this.preset);
            this.draft = { ...(this.draft || {}),
                           ...FigureImagePanel.draftChange(field, input) };
            // One redraw at the moment typing lets a preset go, so the field
            // stops reading as a chosen one and the list stops standing open
            // over it. Every other keystroke takes the cheap path: the panel
            // redraws on every document change, and a half-typed caption must
            // not be one of them.
            if (armed && !this.preset) {
                FigureChoiceField.close();
                this.render();
            }
            return;         // no redraw: the caret is in the field being typed in
        }
        if (field === "label_text" || field === "label_size") {
            this.rowFieldChanged(field, input);
            return;
        }

        const scalebar = FigureImagePanel.SCALEBAR_FIELDS[field];
        if (scalebar) {
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar,
                            ...scalebar(input, panel, this.unitOf(panel)) } }));
            return;
        }
        const colorbar = FigureImagePanel.COLORBAR_FIELDS[field];
        if (colorbar) {
            this.applyToPanels(panels, (panel) => ({
                colorbar: { ...panel.colorbar, ...colorbar(input, panel) } }));
            return;
        }

        const single = this.single;
        if (!single) return;
        const changes = {};
        if (field === "label") {
            // Typing a letter makes it the user's; it stops renumbering when the
            // page is rearranged, which is the whole difference between the two.
            changes.label = { ...single.label, text: input.value, auto: false };
            const auto = this.root.querySelector('[data-field="label_auto"]');
            if (auto) auto.checked = false;
        } else if (field === "label_auto") {
            changes.label = { ...single.label, auto: input.checked };
        } else if (field === "label_visible") {
            changes.label = { ...single.label, visible: input.checked };
        } else return;

        this.handlers.onPanelChange?.(single.panel_id, changes);
    }

    /**
     * Which scale-bar setting each control writes, and how it reads.
     *
     * A table rather than a chain of ifs because every entry is the same shape
     * -- one control, one key -- and the one that is not (the length, which is
     * expressed in whatever unit is chosen, and which of two fields it lands in
     * depends on that unit) is the only one worth reading closely.
     */
    static get SCALEBAR_FIELDS() {
        return {
            scalebar_length: (input, panel, unit) => {
                // Empty is "pick a round number that fits", which is a
                // different answer per panel and is why it is stored as null
                // rather than as whatever number happens to fit this one.
                //
                // `unit` is the EFFECTIVE one, not `panel.scalebar.unit`: an
                // uncalibrated panel draws a pixel bar whatever it has stored,
                // and a number typed into a field reading "px" that landed in
                // `target_um` would be silently discarded until the day
                // somebody supplied a pixel size.
                const typed = parseFloat(input.value);
                const value = Number.isFinite(typed) && typed > 0 ? typed : null;
                if (unit === "px") return { target_px: value };
                return { target_um: value === null ? null
                    : value * FigureImagePanel.unitUm(unit) };
            },
            scalebar_thickness: (input) => ({
                thickness_mm: parseFloat(input.value) > 0
                    ? parseFloat(input.value) : 0.05 }),
            scalebar_margin: (input) => ({
                margin_mm: Math.max(0, parseFloat(input.value) || 0) }),
            scalebar_label_size: (input) => ({
                label_size_pt: input.value ? Number(input.value) : null }),
        };
    }

    static get COLORBAR_FIELDS() {
        return {
            colorbar_thickness: (input) => ({
                thickness_mm: parseFloat(input.value) > 0
                    ? parseFloat(input.value) : 0.1 }),
            colorbar_gap: (input) => ({
                gap_mm: Math.max(0, parseFloat(input.value) || 0) }),
            colorbar_margin: (input) => ({
                margin_mm: Math.max(0, parseFloat(input.value) || 0) }),
            colorbar_ticks: (input) => ({ ticks: parseInt(input.value, 10) || 0 }),
            colorbar_tick_length: (input) => ({
                tick_length_mm: Math.max(0, parseFloat(input.value) || 0) }),
            colorbar_label_size: (input) => ({
                label_size_pt: input.value ? Number(input.value) : null }),
        };
    }

    static unitUm(unit) {
        const entry = FigureSchema.SCALEBAR_UNITS[unit];
        return entry ? entry.um : 1;
    }

    /** What one control writes into the not-yet-added label.
     *
     *  Typing DISARMS the preset, and that is the whole of "Text you type":
     *  the option existed to say "neither of those, mine", and saying it by
     *  typing needs no option. Only the text field does this -- the size, the
     *  colour and the corner are settings OF the label whichever way its words
     *  are arrived at. */
    static draftChange(field, input) {
        if (field === "new_label_text") return { text: input.value, preset: "" };
        if (field === "new_label_size") {
            return { size_pt: input.value ? Number(input.value) : null };
        }
        return {};
    }

    /** Turn a bar on or off across the selection. The button says what pressing
     *  it does rather than what the state is -- "Hide" on a bar that is showing
     *  -- because that is the question a button answers. */
    toggleBar(panels, which) {
        const shown = panels.every((panel) => panel[which].visible);
        this.applyToPanels(panels, (panel) => ({
            [which]: { ...panel[which], visible: !shown } }));
    }

    /** The number printed beside the bar, on or off. A second eye rather than a
     *  checkbox: it is the same kind of answer the bar's own eye gives, and the
     *  two sit in the same column at the end of their rows, which is what says
     *  that the caption is a part of the bar rather than a setting of its own.
     *  `scalebar.label` is what it writes -- the bar keeps its `visible`. */
    toggleCaption(panels) {
        const shown = panels.every((panel) => panel.scalebar.label);
        this.applyToPanels(panels, (panel) => ({
            scalebar: { ...panel.scalebar, label: !shown } }));
    }

    // -- the labels on the selection -----------------------------------------

    /**
     * Put the typed caption, or the chosen preset, on every selected panel.
     *
     * The draft is kept rather than cleared, because the next thing a user does
     * after adding "Tumor" to one panel is add "Stroma" to the next -- and the
     * size, colour and corner they chose are almost always the same. Only the
     * text is emptied, and a preset is not: it produces different words per
     * panel and there is nothing to clear.
     */
    addLabel(panels) {
        const draft = this.draft || {};
        const updates = [];
        for (const panel of panels) {
            const added = this.presetEntries(panel, draft);
            if (!added.length) continue;
            updates.push({ panel_id: panel.panel_id,
                           changes: { labels: [...(panel.labels || []), ...added] } });
        }
        if (!updates.length) return;
        this.handlers.onPanelsChange?.(updates);
        if (!draft.preset) this.draft = { ...draft, text: "" };
    }

    /**
     * What one panel gets when Add is pressed.
     *
     * The presets are per-PANEL by construction, which is the whole reason they
     * are not just text put in the box for the user: "Channels" on a row of
     * four single-channel panels writes four different words, each in its own
     * colour, in one gesture and one undo step.
     */
    presetEntries(panel, draft) {
        const make = (text, colour) => ({
            // One id per panel: the same word on six images is six labels, each
            // editable where it sits.
            label_id: FigureSchema.newLabelId(),
            text: text,
            position: draft.position || "top_left",
            color: colour || draft.color || "#ffffff",
            size_pt: draft.size_pt ?? null,
            bold: false, italic: false,
        });
        if (draft.preset === "channels") {
            return (panel.scene.channels || [])
                .filter((channel) => channel.visible !== false)
                .map((channel) => make(channel.fullname_at_capture || channel.key,
                                       FigureSchema.channelHex(channel.color)));
        }
        if (draft.preset === "image_name") {
            const source = this.state.source(panel.source_id);
            const name = source && (source.display_name || source.datasource);
            return name ? [make(name)] : [];
        }
        const text = String(draft.text || "").trim();
        return text ? [make(text)] : [];
    }

    rowFieldChanged(field, input) {
        const index = Number(input.dataset.row);
        const change = field === "label_text" ? { text: input.value }
            : { size_pt: input.value ? Number(input.value) : null };
        this.editRow(index, change);
    }

    /**
     * Which (panel, label) pairs a row of the merged list stands for, RIGHT NOW.
     *
     * Recomputed on every event rather than stamped into the markup: a rename
     * changes the text the row is keyed on, and a row addressed by the text it
     * used to have would apply the second keystroke to nothing.
     */
    rowTargets(index) {
        const row = this.labelRows()[index];
        if (!row) return null;
        const byPanel = new Map();
        for (const ref of row.refs) {
            if (!byPanel.has(ref.panel_id)) byPanel.set(ref.panel_id, new Set());
            byPanel.get(ref.panel_id).add(ref.label_id);
        }
        return byPanel;
    }

    /** One property of one merged row, on every panel carrying it, as ONE undo
     *  step. */
    editRow(index, change) {
        const targets = this.rowTargets(index);
        if (!targets) return;
        this.commitLabels(targets, (labels, mine) => labels.map((entry) =>
            (mine.has(entry.label_id) ? { ...entry, ...change } : entry)));
    }

    deleteRow(index) {
        const targets = this.rowTargets(index);
        if (!targets) return;
        this.commitLabels(targets, (labels, mine) =>
            labels.filter((entry) => !mine.has(entry.label_id)));
    }

    /**
     * Put a row where another one is.
     *
     * The general move, because a drag is a general move: dropping row 0 onto
     * row 4 is one gesture, and the two arrows it replaces would have been
     * four presses that each had to be re-aimed. Dropping DOWN lands after the
     * row dropped on, dropping UP lands before it -- which is what a drop
     * marker drawn on that edge is promising.
     *
     * The merged list is derived, so this cannot just splice an array: each
     * panel holds its own captions and the row stands for one entry in each of
     * them. So the move is performed once per panel, against that panel's own
     * list, and all of it is ONE commit -- one thing the user did, one Ctrl+Z.
     *
     * The FIRST matching entry in each panel is the one that moves. A row can
     * only stand for two captions on ONE panel when both carry the same text,
     * in which case they are already indistinguishable to a reader; moving both
     * would be two moves for one gesture.
     *
     * `pendingFocus` is what makes a HELD arrow key work: the list redraws with
     * the row in its new place, and without it the keyboard stays on the handle
     * at the old index -- which now belongs to whichever row took the vacated
     * slot, so the key walks two rows past each other instead of walking one
     * of them anywhere. See `focused`, which this overrides for one redraw.
     */
    moveRowTo(from, to) {
        const rows = this.labelRows();
        if (from === to || !rows[from] || !rows[to]) return;
        const moving = this.rowTargets(from);
        const landing = this.rowTargets(to);
        if (!moving || !landing) return;
        const down = to > from;
        this.pendingFocus = `[data-grip="${to}"]`;

        const updates = [];
        for (const panel of this.panels) {
            const mine = moving.get(panel.panel_id);
            if (!mine) continue;
            const out = (panel.labels || []).slice();
            const at = out.findIndex((entry) => mine.has(entry.label_id));
            if (at < 0) continue;
            const [entry] = out.splice(at, 1);
            // Where the row being dropped ON sits in THIS panel, measured after
            // the removal so the index is the one to splice against. A panel
            // carrying the dragged caption but not the one it was dropped on is
            // possible in a merged selection, and there is no position on that
            // panel that answers the gesture -- so it goes to the end it was
            // heading for, which is the half of the answer that is defined.
            const theirs = landing.get(panel.panel_id);
            const met = theirs
                ? out.findIndex((other) => theirs.has(other.label_id)) : -1;
            const into = met < 0 ? (down ? out.length : 0)
                : (down ? met + 1 : met);
            out.splice(into, 0, entry);
            updates.push({ panel_id: panel.panel_id, changes: { labels: out } });
        }
        if (updates.length) this.handlers.onPanelsChange?.(updates);
    }

    /** Rewrite the label list of every panel a row touches, in one commit. */
    commitLabels(targets, edit) {
        const updates = [];
        for (const panel of this.panels) {
            const mine = targets.get(panel.panel_id);
            if (!mine) continue;
            updates.push({ panel_id: panel.panel_id,
                           changes: { labels: edit(panel.labels || [], mine) } });
        }
        if (updates.length) this.handlers.onPanelsChange?.(updates);
    }

    /** One change, across the selection, as ONE undo step. */
    applyToPanels(panels, changesFor) {
        this.handlers.onPanelsChange?.(
            panels.map((panel) => ({ panel_id: panel.panel_id,
                                     changes: changesFor(panel) })));
    }

    /**
     * Record the pixel size in the box against every selected image.
     *
     * Written to the SOURCE rather than to the panel, because it is a fact
     * about the image and every panel of it is entitled to the same answer.
     *
     * It overwrites a calibration that is already there, which it used not to:
     * it only ever wrote to sources that had none. That made the field useless
     * for the case that matters most -- a pixel size the file states, and
     * states wrongly -- and every bar in the figure is derived from it. The
     * panel ids ride along so that a bar currently measured in pixels can
     * switch to microns in the same commit.
     */
    applyPixelSize(panels) {
        const input = this.root.querySelector("#fb_image_mpp");
        const value = parseFloat(input && input.value);
        if (!Number.isFinite(value) || value <= 0) return;
        const sources = new Set();
        for (const panel of panels) {
            if (panel.source_id) sources.add(panel.source_id);
        }
        this.handlers.onSetPixelSize?.(Array.from(sources), value,
                                       panels.map((panel) => panel.panel_id));
    }
}
