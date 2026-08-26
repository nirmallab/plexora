/**
 * The image sidebar: everything a placed PANEL has that is not its position.
 *
 * Quick Edit, the round trip to the viewer, the split, the title and the label,
 * the scale bar, the legend, and copying one panel's rendering onto others.
 *
 * ## Why it is a panel and not six popovers
 *
 * These were six buttons on the floating bar, each opening a popover of its
 * own. That bar sits ON the artwork, so every one of them covered the thing it
 * was about; only one could be open at a time, so setting a title and then a
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
 * A figure is a row of crops of one slide. So the scale bar, the legend and the
 * rendering apply to the whole selection in ONE commit -- see `applyToPanels`
 * -- and the sections that cannot mean anything for several panels at once
 * (Quick Edit, the viewer, the split, the title, the label) show only when one
 * is selected, rather than showing and quietly acting on whichever was first.
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
        //: corner chosen for it. Held on the panel rather than read out of the
        //: DOM at Add time, because the panel redraws on every document change
        //: and a half-typed label would not survive one.
        this.draft = { text: "", position: "top_left", color: "#ffffff", size_pt: null };
        //: Which folds are open. Held here for the same reason `draft` is: the
        //: panel redraws on every document change, and a section that closed
        //: itself every time a number in it was typed would be unusable. Not
        //: persisted -- what is worth opening depends on what is being done to
        //: this figure this afternoon, not on what was done last week.
        this.openSections = new Set();
    }

    setup() {
        if (!this.root) return;
        this.root.addEventListener("input", (event) => this.changed(event));
        this.root.addEventListener("change", (event) => this.changed(event));
        this.root.addEventListener("click", (event) => this.clicked(event));
        this.root.addEventListener("keydown", (event) => this.keyDown(event));
    }

    /** The scale-bar lengths offered, in microns. Round numbers a reader can
     *  hold in their head, which is the whole job of a scale bar. */
    static get LENGTHS() { return [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]; }

    /** Font sizes offered anywhere in this panel, in points. Journal body text
     *  is 7-9 pt and a figure's furniture sits under it; the big end is for a
     *  poster. */
    static get SIZES() { return [5, 6, 7, 8, 9, 10, 12, 14, 18, 24, 36]; }

    /** The nine anchors, as a 3x3 grid with something to put in each cell.
     *  Arrows rather than words: the control is the size of a keypad and the
     *  glyph says which corner without being read. */
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

    // -- what it shows -------------------------------------------------------

    get panels() {
        return this.ids.map((id) => this.state.panel(id)).filter(Boolean);
    }

    get single() {
        const panels = this.panels;
        return panels.length === 1 ? panels[0] : null;
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
     * It used to be seven sections rendered at once -- about sixty controls and
     * fourteen hundred pixels of them, in a three-hundred-pixel column that
     * scrolls. The rule this now follows is the one the floating bar has always
     * followed: rank by how often the answer to "do I want this right now?" is
     * yes. What is always wanted -- the title, whether there is a scale bar, how
     * long it is and which corner it sits in -- is at the top and never folded.
     * Everything else is a fold that states its own value on its face, so
     * nothing reads as missing, and whose body is not built at all until it is
     * opened.
     *
     * A fold that is holding something the user MUST see stops being a fold.
     * Two of them can: the legend when two panels colour a marker differently,
     * and the scale bar when an image recorded no pixel size. Both are cases
     * where a figure would otherwise be quietly wrong.
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
            ${this.essentialsSection(panels, single)}
            ${single ? this.panelLabelSection(single) : ""}
            ${this.scaleBarSection(panels)}
            ${this.colorBarSection(panels)}
            ${this.labelsSection(panels, single)}
            ${this.legendSection(panels)}
            ${this.renderingSection(panels)}`;

        this.restore(focus);
    }

    /**
     * One collapsible group.
     *
     * `body` is a THUNK, not a string, and that is the whole point: a collapsed
     * section costs nothing to render, so the panel's first paint is four
     * headings rather than fifty controls.
     *
     * The summary beside the title is not decoration either. A fold whose face
     * says only "Color bar" makes the user open it to find out whether there is
     * one; a fold that says "Color bar — off" has already answered the question
     * they were going to open it to ask. Every one of them states its current
     * value.
     *
     * `forced` turns the fold back into a plain section -- heading, aside, body,
     * open. There is nothing to press and nothing to miss.
     */
    fold(id, title, summary, body, forced) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        if (forced) {
            return `<section class="fb-side-section">
                <h3 class="fb-side-subheading">${escape(title)}
                    <span class="fb-side-aside">${escape(summary)}</span></h3>
                ${body()}
            </section>`;
        }
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
     * What is wanted nearly every time: the title, and the scale bar.
     *
     * No heading, because it is the top of the panel rather than one section of
     * it, and every row names itself. These four were spread across two sections
     * with eight other fields between them -- which made the commonest job here,
     * "put a 100 µm bar in the bottom left of these six crops", a scroll.
     */
    essentialsSection(panels, single) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const bar = panels[0].scalebar;
        const shown = panels.every((panel) => panel.scalebar.visible);
        const target = bar.target_um;
        const same = panels.every((panel) => panel.scalebar.target_um === target);
        const per = FigureSchema.SCALEBAR_UNITS[bar.unit];
        const typed = target && same
            ? String(Number((target / (per ? per.um : 1)).toFixed(4))) : "";

        return `
            <section class="fb-side-section">
                ${single ? this.field("Title", "fb_image_title", `
                    <input id="fb_image_title" class="fb-input" type="text"
                           data-field="title" maxlength="200" placeholder="No title"
                           value="${escape(single.title || "")}">`) : ""}
                <label class="fb-check">
                    <input type="checkbox" data-field="scalebar" ${shown ? "checked" : ""}>
                    Show a scale bar${panels.length > 1 ? ` on all ${panels.length}` : ""}
                </label>
                ${this.field("Length", "fb_image_bar_len", `
                    <input id="fb_image_bar_len" class="fb-input fb-input-tiny"
                           type="number" min="0" step="any" data-field="scalebar_length"
                           placeholder="Auto" value="${escape(typed)}">
                    <select class="fb-select fb-select-tiny" data-field="scalebar_unit"
                            aria-label="Unit">
                        <option value="auto"${bar.unit === "auto" ? " selected" : ""}
                        >Auto</option>
                        ${Object.entries(FigureSchema.SCALEBAR_UNITS).map(([key, entry]) =>
                            `<option value="${key}"${bar.unit === key ? " selected" : ""}
                            >${entry.text}</option>`).join("")}
                    </select>`)}
                ${this.field("Position", "",
                    this.anchorGrid("scalebar", bar.position, "Scale bar position"))}
                <p class="fb-side-note">Leave the length empty for a round number that
                    fits each image. ${panels.length > 1
                        ? "A set length is the same physical distance on every panel, "
                          + "which is what makes two of them comparable by eye."
                        : "The unit is how the caption is written; “Auto” "
                          + "prints microns below a millimetre and millimetres above."}</p>
            </section>`;
    }

    /**
     * The figure's own A/B/C for this panel.
     *
     * "Panel label", not "Label", and a fold of its own rather than a row under
     * the title: this, the panel's TITLE and the free captions on the image were
     * three different things called Title, Label and Labels, stacked in one
     * column, with only a docstring anywhere saying which was which.
     *
     * Numbering is not here any more. It is the whole figure's -- see
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

    // -- shared controls -----------------------------------------------------

    /**
     * The nine anchors as a keypad. One implementation, wherever a corner is
     * chosen: the scale bar, the colour bar, a new caption and every caption
     * already on the image.
     *
     * A grid rather than a dropdown because the choice IS a position: nine
     * words in a list have to be read and mapped onto the panel, where nine
     * cells in the shape of the panel do not. `group` names what is being
     * placed, and the click handler routes on it.
     *
     * A RADIOGROUP, not nine toggles. `aria-pressed` was what it carried, and
     * that says "this button is currently pressed in" nine times over, with
     * nothing tying the nine together or saying that exactly one of them is the
     * answer. With `role="radio"` a screen reader announces "top left, 1 of 9"
     * -- and the arrow keys work, through `keyDown`, which is the other half of
     * what a radiogroup promises. Exactly one cell is in the tab order, so the
     * keypad is one stop rather than nine.
     */
    anchorGrid(group, current, label) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const anchors = FigureSchema.PANEL_ANCHORS;
        // A stored value the schema does not know is still a value: rather than
        // leaving every cell out of the tab order, the first one takes it.
        const chosen = anchors.includes(current) ? current : anchors[0];
        const cells = anchors.map((anchor) => {
            const name = FigureImagePanel.anchorName(anchor);
            const on = anchor === chosen;
            return `<button type="button" class="fb-anchor-cell${on ? " is-on" : ""}"
                    data-anchor="${anchor}" role="radio"
                    aria-checked="${on ? "true" : "false"}"
                    tabindex="${on ? "0" : "-1"}"
                    title="${escape(name)}" aria-label="${escape(name)}"
                >${FigureImagePanel.ANCHOR_GLYPHS[anchor]}</button>`;
        }).join("");
        return `<div class="fb-anchor-grid" data-anchors="${group}" role="radiogroup"
                     aria-label="${escape(label || "Position")}">${cells}</div>`;
    }

    /**
     * The arrow keys, inside a keypad.
     *
     * Moving and choosing are the same act here, which is what a radiogroup
     * does natively and what these nine buttons could not do at all: the only
     * way to set a corner was to click it. Left and right do not wrap onto the
     * row above or below -- the grid is a picture of the panel, and "left of the
     * top-left corner" is not the bottom-right one.
     */
    keyDown(event) {
        const cell = event.target.closest?.("[data-anchor]");
        const pad = cell && cell.closest("[data-anchors]");
        if (!pad) return;
        const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -3, ArrowDown: 3 }[event.key];
        if (step === undefined) return;
        const anchors = FigureSchema.PANEL_ANCHORS;
        const from = anchors.indexOf(cell.dataset.anchor);
        const to = from + step;
        if (from < 0 || to < 0 || to >= anchors.length) return;
        if (Math.abs(step) === 1 && Math.floor(to / 3) !== Math.floor(from / 3)) return;
        event.preventDefault();
        this.anchorPicked(pad.dataset.anchors, anchors[to]);
    }

    /** A points dropdown, with "Figure" for the size that follows the document.
     *  Stored as null rather than as a copy of the number, so raising the
     *  figure's body size still moves everything that never asked for its own. */
    sizeSelect(field, value, id, extra) {
        // A size that is not one of the offered ones is added to the list
        // rather than dropped. Without this the browser falls back to the first
        // option, so a 11 pt caption would read as "Figure" and quietly become
        // it the next time anything else in the row was touched.
        const sizes = FigureImagePanel.SIZES.includes(value)
            || value === null || value === undefined
            ? FigureImagePanel.SIZES
            : [...FigureImagePanel.SIZES, value].sort((a, b) => a - b);
        const options = sizes.map((size) =>
            `<option value="${size}"${value === size ? " selected" : ""}>${size} pt</option>`
        ).join("");
        return `<select class="fb-select fb-select-tiny" data-field="${field}"
                        ${id ? `id="${id}"` : ""} ${extra || ""}>
            <option value=""${value === null || value === undefined ? " selected" : ""}
            >Figure</option>${options}</select>`;
    }

    /** A millimetre box. Every distance on a page is in millimetres here, so a
     *  bar 0.8 thick is 0.8 whatever the export DPI turns out to be. */
    mmInput(field, value, id, extra) {
        return `<input class="fb-input fb-input-tiny" type="number" min="0" step="0.1"
                       data-field="${field}" ${id ? `id="${id}"` : ""}
                       value="${Number(value).toFixed(2).replace(/\.?0+$/, "")}"
                       ${extra || ""}><span class="fb-unit-suffix">mm</span>`;
    }

    /**
     * Everything the registry says this selection can be done to, described
     * once and asked for by section.
     *
     * This panel used to hand-build its action buttons AND hand-write the
     * predicate behind them -- a re-typed copy of "can this panel be reopened"
     * that had already drifted from the registry's. See
     * `FigureActions.reopenable`; the drift was a live bug, not a tidiness
     * problem. The sidebar still owns its own headings, notes and row layout,
     * because those are about this panel; what exists and what can be pressed
     * comes from the one list every other surface reads.
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
     *  so a caller can put `${...}` straight into its markup. */
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
        return `<div class="fb-side-actions">${buttons}</div>`;
    }

    /**
     * The two ways back to the image, and the split.
     *
     * All of them are about ONE panel and none has a sensible reading for
     * several: Quick Edit edits a view, the viewer opens a view, and a split
     * replaces a panel with one per channel. A button that silently acted on
     * whichever panel was selected first is worse than no button -- which is
     * what each entry's `applies` now says, rather than this method.
     */
    actionsSection(single) {
        if (!single) return "";
        const context = this.registryContext();
        const editable = FigureActions.reopenable(single, context);
        const channels = (single.scene.channels || []).length;
        const split = this.actionRow("split", context);
        return `
            <section class="fb-side-section">
                ${this.actionRow("actions", context)}
                ${editable ? "" : `<p class="fb-side-note">This panel came from an
                    image the figure no longer references, so it cannot be
                    reopened.</p>`}
                ${split ? `${split}
                <p class="fb-side-note">One panel per channel, sharing this panel's exact
                    crop and window &mdash; linked, so resizing one resizes the row. This
                    one shows ${channels}.</p>` : ""}
            </section>`;
    }

    /**
     * How the bar itself is drawn, and what the image says it is calibrated at.
     *
     * The switch, the length and the corner are up in the essentials, because
     * they are what anyone wants; this is the rest -- thickness, colour, margin,
     * caption -- plus the pixel size every one of those numbers is derived from.
     *
     * It stops being a fold when an image recorded no pixel size, because that
     * is the answer to "I switched the scale bar on and nothing appeared", and
     * an answer behind a fold is an answer nobody finds.
     */
    scaleBarSection(panels) {
        const uncalibrated = panels.filter(
            (panel) => !FigureSchema.physicalWidthUm(
                this.state.source(panel.source_id), panel.scene.viewport));
        const bar = panels[0].scalebar;

        return this.fold("scalebar", "Bar and caption", this.pixelSizeNote(panels), () => `
            ${this.field("Bar", "fb_image_bar_thick", `
                ${this.mmInput("scalebar_thickness", bar.thickness_mm,
                               "fb_image_bar_thick")}
                ${FigureColorField.swatch({
                    field: "scalebar_color", value: bar.color, label: "Scale bar color" })}`)}
            ${this.field("Margin", "fb_image_bar_margin",
                this.mmInput("scalebar_margin", bar.margin_mm, "fb_image_bar_margin"))}
            ${this.field("Caption", "", `
                <label class="fb-check">
                    <input type="checkbox" data-field="scalebar_label"
                           ${bar.label ? "checked" : ""}> Show
                </label>
                ${this.sizeSelect("scalebar_label_size", bar.label_size_pt)}`)}
            ${uncalibrated.length ? `
            <p class="fb-side-note">${FigureSchema.escapeHtml(
                FigureSchema.countPhrase(uncalibrated.length, "image"))} here recorded
                no pixel size, so ${uncalibrated.length === 1 ? "it has" : "they have"}
                no bar.</p>
            ${this.field("µm per pixel", "fb_image_mpp", `
                <input id="fb_image_mpp" class="fb-input fb-input-tiny" type="number"
                       min="0" step="0.0001" placeholder="e.g. 0.325">
                <button type="button" class="fb-button" data-act="pixel_size">
                    Apply</button>`)}
            <p class="fb-side-note">Recorded against the image for the whole figure,
                and in the provenance as a value you supplied rather than one the
                file stated.</p>` : ""}`, uncalibrated.length > 0);
    }

    /**
     * What the images say they are calibrated at.
     *
     * Printed rather than left to be discovered, because it is the number every
     * scale bar in the figure is derived from and the one thing that makes a
     * bar wrong if it is wrong. A selection whose images disagree says so
     * instead of picking one -- two magnifications in one row is ordinary, and
     * a single number would describe neither.
     */
    pixelSizeNote(panels) {
        const sizes = new Set();
        for (const panel of panels) {
            const source = this.state.source(panel.source_id);
            const size = source && source.pixel_size;
            sizes.add(size && size.value > 0 ? Number(size.value.toFixed(4)) : null);
        }
        if (sizes.size > 1) return "Pixel sizes differ";
        const only = Array.from(sizes)[0];
        if (!only) return "No pixel size";
        return FigureSchema.escapeHtml(`${only} µm/px`);
    }

    /**
     * An intensity scale for the rendered channels.
     *
     * Off by default and never turned on automatically: a colour bar is a claim
     * that the intensities are quantitative, and most panels are not making it.
     * When it is on, the ticks are the channel's own display window in raw
     * units -- the numbers the contrast was set against.
     */
    colorBarSection(panels) {
        const bar = panels[0].colorbar;
        const shown = panels.every((panel) => panel.colorbar.visible);
        const channels = panels[0].scene.channels || [];
        const visible = channels.filter((channel) => channel.visible !== false).length;

        // Nineteen controls for a feature the paragraph inside it says most
        // panels should never switch on. Folded, the eleven ways of drawing a
        // ramp cost one line until somebody wants a ramp -- and that line says
        // whether there is one, which is the only thing anyone was scrolling
        // past this section to find out.
        return this.fold("colorbar", "Color bar",
            shown ? FigureSchema.countPhrase(visible, "ramp") : "Off", () => `
            <label class="fb-check">
                <input type="checkbox" data-field="colorbar" ${shown ? "checked" : ""}>
                Show an intensity scale
            </label>
            <p class="fb-side-note">One ramp per visible channel &mdash;
                ${FigureSchema.escapeHtml(FigureSchema.countPhrase(visible, "ramp"))}
                here. Each is labelled with that channel's own display window, in
                raw units, because each has its own.</p>
            ${this.field("Runs", "fb_image_cb_dir", `
                <select id="fb_image_cb_dir" class="fb-select"
                        data-field="colorbar_orientation">
                    <option value="horizontal"${bar.orientation === "horizontal"
                        ? " selected" : ""}>Across</option>
                    <option value="vertical"${bar.orientation === "vertical"
                        ? " selected" : ""}>Down</option>
                </select>`)}
            ${this.field("Position", "",
                this.anchorGrid("colorbar", bar.position, "Color bar position"))}
            ${this.field("Thickness", "fb_image_cb_thick",
                this.mmInput("colorbar_thickness", bar.thickness_mm, "fb_image_cb_thick"))}
            ${this.field("Gap", "fb_image_cb_gap",
                this.mmInput("colorbar_gap", bar.gap_mm, "fb_image_cb_gap"))}
            ${this.field("Margin", "fb_image_cb_margin",
                this.mmInput("colorbar_margin", bar.margin_mm, "fb_image_cb_margin"))}
            ${this.field("Ticks", "fb_image_cb_ticks", `
                <select id="fb_image_cb_ticks" class="fb-select fb-select-tiny"
                        data-field="colorbar_ticks">
                    ${[0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map((count) =>
                        `<option value="${count}"${bar.ticks === count ? " selected" : ""}
                        >${count === 0 ? "None" : count}</option>`).join("")}
                </select>
                ${FigureColorField.swatch({
                    field: "colorbar_tick_color", value: bar.tick_color,
                    label: "Tick and label color" })}
                ${this.sizeSelect("colorbar_label_size", bar.label_size_pt)}`)}
            ${this.field("Tick size", "fb_image_cb_tick_len", `
                ${this.mmInput("colorbar_tick_length", bar.tick_length_mm,
                               "fb_image_cb_tick_len")}
                <input class="fb-input fb-input-tiny" type="number" min="0" step="0.1"
                       data-field="colorbar_tick_width" aria-label="Tick thickness"
                       value="${bar.tick_width_pt}"><span class="fb-unit-suffix">pt</span>`)}
            <p class="fb-side-note">Length across the bar, then how thick the tick
                itself is drawn.</p>`);
    }

    /**
     * Free captions on the image.
     *
     * A different thing from the panel's LABEL, which is the figure's own A/B/C
     * and is one per panel by definition, and from a text annotation, which
     * sits on the page and stays behind when the panel is moved. These belong
     * to the image: "Tumor", "40x", an arrow's name.
     *
     * Adding works across a whole selection -- the same word on six panels is a
     * real thing to want -- but the list of existing ones is shown for a single
     * panel only. Six panels' captions interleaved in one list, with no way to
     * see which row belongs to which image, would be a list nobody could edit.
     */
    labelsSection(panels, single) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const draft = this.draft || {};
        const rows = single ? (single.labels || []) : [];
        const summary = single
            ? (rows.length ? FigureSchema.countPhrase(rows.length, "caption") : "None")
            : `Add to all ${panels.length}`;

        // "Captions", not "Labels". There were three things on this panel called
        // Title, Label and Labels -- the panel's own name, the figure's A/B/C,
        // and the words drawn on the image -- and only a docstring said which
        // was which. These are the ones on the image; the code has called them
        // captions all along.
        return this.fold("captions", "Captions", summary, () => `
            ${this.field("Add", "fb_image_new_label", `
                <input id="fb_image_new_label" class="fb-input" type="text"
                       data-field="new_label_text" maxlength="200"
                       placeholder="Caption" value="${escape(draft.text || "")}">
                ${this.sizeSelect("new_label_size", draft.size_pt ?? null)}
                ${FigureColorField.swatch({
                    field: "new_label_color", value: draft.color || "#ffffff",
                    label: "New caption color" })}
                <button type="button" class="fb-button fb-button-primary"
                        data-act="add_label">Add</button>`)}
            ${this.field("Place at", "",
                this.anchorGrid("new_label", draft.position || "top_left",
                                "Where a new caption lands"))}
            ${single ? "" : `<p class="fb-side-note">Added to all
                ${panels.length} selected images.</p>`}
            ${rows.length ? `
            <div class="fb-label-list">
                ${rows.map((entry, index) => this.labelRow(entry, index, rows.length)).join("")}
            </div>` : `<p class="fb-side-note">${single
                ? "Nothing on this image yet."
                : "Select one image to edit the captions already on it."}</p>`}`);
    }

    /**
     * One existing caption: everything about it, plus where it sits in the
     * stack. Reordering is up/down rather than a drag because the order only
     * decides which of two captions in the same corner is on top.
     *
     * Two lines, and the arithmetic is the reason. Six controls at their fixed
     * widths came to 308px in a 268px row, so the flexible one -- the text, the
     * whole point of the row -- was squeezed towards nothing: a caption was
     * edited through a box a few characters wide. The text and its three buttons
     * take the first line and the appearance takes the second.
     *
     * The corner is the same nine-cell keypad the Add row uses. It was a nine-row
     * dropdown here and a keypad three rows above, for the same choice, on the
     * same panel.
     */
    labelRow(entry, index, total) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const id = escape(entry.label_id);
        return `<div class="fb-label-row" data-label-id="${id}">
            <div class="fb-label-row-line">
                <input class="fb-input" type="text" maxlength="200"
                       data-field="label_text" data-label-id="${id}"
                       aria-label="Caption text" value="${escape(entry.text)}">
                <button type="button" class="fb-icon-button" data-act="label_up:${id}"
                        title="Move up" ${index === 0 ? "disabled" : ""}>
                    <span class="fas fa-arrow-up" aria-hidden="true"></span></button>
                <button type="button" class="fb-icon-button" data-act="label_down:${id}"
                        title="Move down" ${index === total - 1 ? "disabled" : ""}>
                    <span class="fas fa-arrow-down" aria-hidden="true"></span></button>
                <button type="button" class="fb-icon-button fb-icon-button-danger"
                        data-act="label_delete:${id}" title="Delete">
                    <span class="fas fa-xmark" aria-hidden="true"></span></button>
            </div>
            <div class="fb-label-row-line">
                ${this.anchorGrid(`caption:${entry.label_id}`, entry.position,
                                  `Corner for “${entry.text}”`)}
                ${this.sizeSelect("label_size", entry.size_pt ?? null, "",
                                  `data-label-id="${id}"`)}
                ${FigureColorField.swatch({
                    field: `label_color:${entry.label_id}`, value: entry.color,
                    label: "Caption color" })}
            </div>
        </div>`;
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
    legendSection(panels) {
        const channels = panels.every((panel) => panel.legend.channels);
        const clashes = FigureImagePanel.colourConflicts(panels);

        // A colour clash is the one thing on this panel that must never be
        // behind a fold. It is the difference between a figure and a figure that
        // misleads a reader, and a user who never opens this section is exactly
        // the user it has to reach -- so when there is one, the fold is not a
        // fold.
        return this.fold("legend", "Legend",
            clashes.length ? "Colors disagree"
                : channels ? "Channels named" : "Off", () => `
            <label class="fb-check">
                <input type="checkbox" data-field="legend_channels"
                       ${channels ? "checked" : ""}> Name the channels
            </label>
            <p class="fb-side-note">A swatch and a name per channel, drawn from what
                was recorded when the panel was captured.</p>
            ${clashes.length ? `
            <div class="fb-conflict">
                <strong>Selected panels use different colors for
                    ${FigureSchema.escapeHtml(
                        clashes.map((clash) => clash.name).join(", "))}.</strong>
                <p class="fb-side-note">One legend for both would say something the
                    panels do not.</p>
                <button type="button" class="fb-menu-item" data-act="legend_keep">
                    Keep them separate</button>
                <button type="button" class="fb-menu-item" data-act="legend_share">
                    Use one shared style</button>
            </div>` : ""}`, clashes.length > 0);
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

    /** Copy how one panel is rendered, and put it on others. Two buttons rather
     *  than one, because they happen at different moments: copy from the panel
     *  you got right, then select the rest. */
    renderingSection(panels) {
        const armed = Boolean(this.handlers.hasRenderClipboard?.());
        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Rendering</h3>
                ${this.actionRow("rendering", this.registryContext())}
                <p class="fb-side-note">${armed
                    ? "Channel colors and contrast only. Across two images, channels are "
                      + "matched by name."
                    : "Copies this panel's channel colors and contrast, to put on other "
                      + "panels."}</p>
            </section>`;
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
     *  without this typing a title rebuilds the field under the caret at the
     *  second character. */
    focused() {
        const active = document.activeElement;
        if (!active || !this.root.contains(active)) return null;
        // An id where there is one; otherwise the field name and, for a row in
        // the label list, which row -- the rows are generated and have no ids
        // of their own, and without this typing in one rebuilds the input under
        // the caret at its second character.
        //
        // A keypad cell is found by its PAD and its checked state rather than by
        // which cell it was: an arrow key both moves and chooses, so the cell
        // that had the keyboard is not the one that should have it after the
        // redraw. A fold's own button is found by its id, so opening one leaves
        // the keyboard on the thing that was pressed rather than at the top of
        // the panel.
        const where = active.id ? `#${active.id}`
            : active.dataset?.fold ? `[data-fold="${active.dataset.fold}"]`
            : active.dataset?.anchor
                ? `[data-anchors="${active.closest("[data-anchors]")
                    ?.dataset.anchors}"] [aria-checked="true"]`
            : active.dataset?.act ? `[data-act="${active.dataset.act}"]`
            : active.dataset?.field
                ? `[data-field="${active.dataset.field}"]${active.dataset.labelId
                    ? `[data-label-id="${active.dataset.labelId}"]` : ""}`
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
        const cell = event.target.closest?.("[data-anchor]");
        if (cell && !cell.disabled) {
            this.anchorPicked(cell.closest("[data-anchors]").dataset.anchors,
                              cell.dataset.anchor);
            return;
        }
        const well = event.target.closest?.("[data-swatch]");
        if (well && !well.disabled) {
            // Read now rather than off the button later: applying a colour
            // redraws this panel, so by the second colour of a drag this
            // element is detached and its dataset is gone with it.
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
        const single = this.single;
        // Row actions carry their label's id after a colon, so that one
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
            legend_keep: () => this.applyLegend(panels, { channels: true }),
            legend_share: () => this.shareLegendColours(panels),
            add_label: () => this.addLabel(panels),
            label_up: () => single && this.moveLabel(single, argument, -1),
            label_down: () => single && this.moveLabel(single, argument, 1),
            label_delete: () => single && this.editLabels(single,
                (labels) => labels.filter((entry) => entry.label_id !== argument)),
        }[act] || (() => {}))();
    }

    /** One of the nine-cell keypads was pressed. */
    anchorPicked(group, anchor) {
        const panels = this.panels;
        if (group === "new_label") {
            this.draft = { ...(this.draft || {}), position: anchor };
            this.render();
            return;
        }
        // A caption's own corner: the group carries which caption after a colon,
        // the same way its buttons carry it, so one keypad component serves a
        // list whose length changes.
        if (group.startsWith("caption:")) {
            const id = group.slice("caption:".length);
            if (this.single) {
                this.editLabels(this.single, (labels) => labels.map((entry) =>
                    (entry.label_id === id ? { ...entry, position: anchor } : entry)));
            }
            return;
        }
        if (group === "scalebar") {
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar, position: anchor } }));
        } else if (group === "colorbar") {
            this.applyToPanels(panels, (panel) => ({
                colorbar: { ...panel.colorbar, position: anchor } }));
        }
    }

    /** A colour well was picked from. `field` may carry a label id. */
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
        } else if (name === "label_color" && this.single) {
            this.editLabels(this.single, (labels) => labels.map((entry) =>
                (entry.label_id === argument ? { ...entry, color: hex } : entry)));
        }
    }

    changed(event) {
        const input = event.target;
        const field = input.dataset && input.dataset.field;
        if (!field) return;

        // Numbering used to be handled here, with a note explaining that it is
        // the FIGURE's rather than this panel's. It is now WHERE it is the
        // figure's: FigureWorkspace.openNumbering, off the page menu, beside
        // the other document settings. A row that had to explain it did not
        // belong to the thing it was inside was a row in the wrong place.

        const panels = this.panels;
        if (!panels.length) return;

        if (field.startsWith("new_label_")) {
            this.draft = { ...(this.draft || {}), ...FigureImagePanel.draftChange(field, input) };
            return;         // no redraw: the caret is in the field being typed in
        }
        if (field.startsWith("label_") && input.dataset.labelId) {
            this.labelFieldChanged(field, input);
            return;
        }
        if (field === "legend_channels") {
            this.applyToPanels(panels, (panel) => ({
                legend: { ...panel.legend, channels: input.checked } }));
            return;
        }

        const scalebar = FigureImagePanel.SCALEBAR_FIELDS[field];
        if (scalebar) {
            this.applyToPanels(panels, (panel) => ({
                scalebar: { ...panel.scalebar, ...scalebar(input, panel) } }));
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
        if (field === "title") changes.title = input.value;
        else if (field === "label") {
            // Typing a label makes it the user's; it stops renumbering when the
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
     * -- one control, one key -- and the two that are not (the length, which is
     * expressed in whatever unit is chosen, and the unit, which changes what
     * the length means without changing the length) are the only two worth
     * reading closely.
     */
    static get SCALEBAR_FIELDS() {
        return {
            scalebar: (input) => ({ visible: input.checked }),
            scalebar_length: (input, panel) => ({
                // Empty is "pick a round number that fits", which is a
                // different answer per panel and is why it is stored as null
                // rather than as whatever number happens to fit this one.
                target_um: input.value
                    ? parseFloat(input.value) * FigureImagePanel.unitUm(panel.scalebar.unit)
                    : null,
            }),
            scalebar_unit: (input, panel) => ({
                unit: input.value,
                // The physical length is unchanged: switching the unit changes
                // how the caption is WRITTEN, not how long the bar is. Storing
                // microns throughout is what makes that true.
                target_um: panel.scalebar.target_um,
            }),
            scalebar_thickness: (input) => ({ thickness_mm: Number(input.value) || 0.05 }),
            scalebar_margin: (input) => ({ margin_mm: Math.max(0, Number(input.value) || 0) }),
            scalebar_label: (input) => ({ label: input.checked }),
            scalebar_label_size: (input) => ({
                label_size_pt: input.value ? Number(input.value) : null }),
        };
    }

    static get COLORBAR_FIELDS() {
        return {
            colorbar: (input) => ({ visible: input.checked }),
            colorbar_orientation: (input) => ({ orientation: input.value }),
            colorbar_thickness: (input) => ({ thickness_mm: Number(input.value) || 0.1 }),
            colorbar_gap: (input) => ({ gap_mm: Math.max(0, Number(input.value) || 0) }),
            colorbar_margin: (input) => ({ margin_mm: Math.max(0, Number(input.value) || 0) }),
            colorbar_ticks: (input) => ({ ticks: parseInt(input.value, 10) || 0 }),
            colorbar_tick_width: (input) => ({
                tick_width_pt: Math.max(0, Number(input.value) || 0) }),
            colorbar_tick_length: (input) => ({
                tick_length_mm: Math.max(0, Number(input.value) || 0) }),
            colorbar_label_size: (input) => ({
                label_size_pt: input.value ? Number(input.value) : null }),
        };
    }

    static unitUm(unit) {
        const entry = FigureSchema.SCALEBAR_UNITS[unit];
        return entry ? entry.um : 1;
    }

    /** What one control writes into the not-yet-added label. */
    static draftChange(field, input) {
        if (field === "new_label_text") return { text: input.value };
        if (field === "new_label_size") {
            return { size_pt: input.value ? Number(input.value) : null };
        }
        return {};
    }

    // -- the labels on one image --------------------------------------------

    /**
     * Put the typed caption on every selected panel.
     *
     * The draft is kept rather than cleared, because the next thing a user does
     * after adding "Tumor" to one panel is add "Stroma" to the next -- and the
     * size, colour and corner they chose are almost always the same. Only the
     * text is emptied.
     */
    addLabel(panels) {
        const draft = this.draft || {};
        const text = String(draft.text || "").trim();
        if (!text) return;
        this.applyToPanels(panels, (panel) => ({
            labels: [...(panel.labels || []), {
                // One id per panel: the same caption on six images is six
                // labels, each editable where it sits.
                label_id: FigureSchema.newLabelId(),
                text: text,
                position: draft.position || "top_left",
                color: draft.color || "#ffffff",
                size_pt: draft.size_pt ?? null,
                bold: false, italic: false,
            }],
        }));
        this.draft = { ...draft, text: "" };
    }

    labelFieldChanged(field, input) {
        const single = this.single;
        if (!single) return;
        const id = input.dataset.labelId;
        // No `label_position` any more: a caption's corner is the keypad, and
        // the keypad reports through `anchorPicked` like every other one.
        const change = field === "label_text" ? { text: input.value }
            : field === "label_size"
                ? { size_pt: input.value ? Number(input.value) : null } : null;
        if (!change) return;
        this.editLabels(single, (labels) => labels.map((entry) =>
            (entry.label_id === id ? { ...entry, ...change } : entry)));
    }

    /** Swap a label with its neighbour. Order decides which of two captions in
     *  the same corner is drawn on top, and nothing else. */
    moveLabel(panel, id, by) {
        this.editLabels(panel, (labels) => {
            const from = labels.findIndex((entry) => entry.label_id === id);
            const to = from + by;
            if (from < 0 || to < 0 || to >= labels.length) return labels;
            const out = labels.slice();
            [out[from], out[to]] = [out[to], out[from]];
            return out;
        });
    }

    editLabels(panel, edit) {
        this.handlers.onPanelChange?.(panel.panel_id,
            { labels: edit(panel.labels || []) });
    }

    /** One change, across the selection, as ONE undo step. */
    applyToPanels(panels, changesFor) {
        this.handlers.onPanelsChange?.(
            panels.map((panel) => ({ panel_id: panel.panel_id, changes: changesFor(panel) })));
    }

    applyLegend(panels, which) {
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
    shareLegendColours(panels) {
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

    /**
     * Type in a pixel size for images that never recorded one.
     *
     * Written to the SOURCE rather than to the panel, because it is a fact
     * about the image and every panel of it is entitled to the same answer.
     */
    applyPixelSize(panels) {
        const input = this.root.querySelector("#fb_image_mpp");
        const value = parseFloat(input && input.value);
        if (!Number.isFinite(value) || value <= 0) return;
        const seen = new Map();
        for (const panel of panels) {
            const source = this.state.source(panel.source_id);
            if (source && !(source.pixel_size && source.pixel_size.value > 0)) {
                seen.set(source.source_id, source);
            }
        }
        this.handlers.onSetPixelSize?.(Array.from(seen.keys()), value);
    }
}
