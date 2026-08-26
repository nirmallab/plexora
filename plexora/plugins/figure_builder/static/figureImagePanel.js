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
    }

    setup() {
        if (!this.root) return;
        this.root.addEventListener("input", (event) => this.changed(event));
        this.root.addEventListener("change", (event) => this.changed(event));
        this.root.addEventListener("click", (event) => this.clicked(event));
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
            ${single ? this.titleSection(single) : ""}
            ${this.scaleBarSection(panels)}
            ${this.colorBarSection(panels)}
            ${this.labelsSection(panels, single)}
            ${this.legendSection(panels)}
            ${this.renderingSection(panels)}`;

        this.restore(focus);
    }

    // -- shared controls -----------------------------------------------------

    /**
     * The nine anchors as a keypad.
     *
     * A grid rather than a dropdown because the choice IS a position: nine
     * words in a list have to be read and mapped onto the panel, where nine
     * cells in the shape of the panel do not. `group` names what is being
     * placed, and the click handler routes on it.
     */
    anchorGrid(group, current) {
        const cells = FigureSchema.PANEL_ANCHORS.map((anchor) => {
            const name = FigureImagePanel.anchorName(anchor);
            return `<button type="button" class="fb-anchor-cell${
                anchor === current ? " is-on" : ""}" data-anchor="${anchor}"
                    title="${FigureSchema.escapeHtml(name)}"
                    aria-label="${FigureSchema.escapeHtml(name)}"
                    aria-pressed="${anchor === current ? "true" : "false"}"
                >${FigureImagePanel.ANCHOR_GLYPHS[anchor]}</button>`;
        }).join("");
        return `<div class="fb-anchor-grid" data-anchors="${group}"
                     role="group">${cells}</div>`;
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
     * The two ways back to the image, and the split.
     *
     * All three are about ONE panel and none of them has a sensible reading for
     * several: Quick Edit edits a view, the viewer opens a view, and a split
     * replaces a panel with one per channel. A button that silently acted on
     * whichever panel was selected first is worse than no button.
     */
    actionsSection(single) {
        if (!single) return "";
        const source = this.state.source(single.source_id);
        const editable = Boolean(source && source.kind === "plexora_project"
                                 && source.datasource);
        const channels = (single.scene.channels || []).length;
        return `
            <section class="fb-side-section">
                <div class="fb-side-actions">
                    <button type="button" class="fb-button" data-act="quick_edit"
                            ${editable ? "" : "disabled"}>
                        <span class="fas fa-sliders" aria-hidden="true"></span>
                        Quick Edit
                    </button>
                    <button type="button" class="fb-button" data-act="viewer"
                            ${editable ? "" : "disabled"}>
                        <span class="fas fa-arrow-up-right-from-square" aria-hidden="true"></span>
                        Open in Main Viewer
                    </button>
                </div>
                ${editable ? "" : `<p class="fb-side-note">This panel came from an
                    image the figure no longer references, so it cannot be
                    reopened.</p>`}
                ${channels > 1 ? `
                <div class="fb-side-actions">
                    <button type="button" class="fb-button" data-act="split_with_composite">
                        Composite + channels</button>
                    <button type="button" class="fb-button" data-act="split_channels_only">
                        Channels only</button>
                </div>
                <p class="fb-side-note">One panel per channel, sharing this panel's exact
                    crop and window &mdash; linked, so resizing one resizes the row. This
                    one shows ${channels}.</p>` : ""}
            </section>`;
    }

    titleSection(panel) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const style = this.state.document.settings.label_style;
        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Title and label</h3>
                ${this.field("Title", "fb_image_title", `
                    <input id="fb_image_title" class="fb-input" type="text"
                           data-field="title" maxlength="200" placeholder="No title"
                           value="${escape(panel.title || "")}">`)}
                ${this.field("Label", "fb_image_label", `
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
                ${this.field("Numbering", "fb_image_label_style", `
                    <select id="fb_image_label_style" class="fb-select"
                            data-field="label_style">
                        <option value="A" ${style === "A" ? "selected" : ""}>A, B, C</option>
                        <option value="a" ${style === "a" ? "selected" : ""}>a, b, c</option>
                        <option value="A1" ${style === "A1" ? "selected" : ""}>A1, A2, A3</option>
                    </select>`)}
                <p class="fb-side-note">Numbering is the whole figure's, and follows
                    reading order &mdash; left to right, top to bottom.</p>
            </section>`;
    }

    /**
     * Scale bars, for one panel or for a whole selection.
     *
     * The length is EITHER automatic or an explicit number of microns, and the
     * difference matters across several panels: automatic gives each image a
     * round number that fits it, which for a row of different magnifications is
     * several different bars; an explicit length is the same physical distance
     * everywhere, which is what makes two panels comparable by eye. The panel
     * says which is which rather than leaving it to be discovered.
     */
    scaleBarSection(panels) {
        const uncalibrated = panels.filter(
            (panel) => !FigureSchema.physicalWidthUm(
                this.state.source(panel.source_id), panel.scene.viewport));
        const bar = panels[0].scalebar;
        const shown = panels.every((panel) => panel.scalebar.visible);
        const target = bar.target_um;
        const same = panels.every((panel) => panel.scalebar.target_um === target);
        const per = FigureSchema.SCALEBAR_UNITS[bar.unit];
        const typed = target && same
            ? String(Number((target / (per ? per.um : 1)).toFixed(4))) : "";

        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Scale bar
                    <span class="fb-side-aside">${this.pixelSizeNote(panels)}</span></h3>
                <label class="fb-check">
                    <input type="checkbox" data-field="scalebar" ${shown ? "checked" : ""}>
                    Show a scale bar${panels.length > 1 ? ` on all ${panels.length}` : ""}
                </label>
                ${this.field("Length", "fb_image_bar_len", `
                    <input id="fb_image_bar_len" class="fb-input fb-input-tiny"
                           type="number" min="0" step="any" data-field="scalebar_length"
                           placeholder="Auto" value="${FigureSchema.escapeHtml(typed)}">
                    <select class="fb-select fb-select-tiny" data-field="scalebar_unit"
                            aria-label="Unit">
                        <option value="auto"${bar.unit === "auto" ? " selected" : ""}
                        >Auto</option>
                        ${Object.entries(FigureSchema.SCALEBAR_UNITS).map(([key, entry]) =>
                            `<option value="${key}"${bar.unit === key ? " selected" : ""}
                            >${entry.text}</option>`).join("")}
                    </select>`)}
                <p class="fb-side-note">Leave the length empty for a round number that
                    fits each image. ${panels.length > 1
                        ? "A set length is the same physical distance on every panel, "
                          + "which is what makes two of them comparable by eye."
                        : "The unit is how the caption is written; “Auto” "
                          + "prints microns below a millimetre and millimetres above."}</p>
                ${this.field("Position", "", this.anchorGrid("scalebar", bar.position))}
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
                    file stated.</p>` : ""}
            </section>`;
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

        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Color bar</h3>
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
                ${this.field("Position", "", this.anchorGrid("colorbar", bar.position))}
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
                    itself is drawn.</p>
            </section>`;
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

        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Labels</h3>
                ${this.field("Add", "fb_image_new_label", `
                    <input id="fb_image_new_label" class="fb-input" type="text"
                           data-field="new_label_text" maxlength="200"
                           placeholder="Label" value="${escape(draft.text || "")}">
                    ${this.sizeSelect("new_label_size", draft.size_pt ?? null)}
                    ${FigureColorField.swatch({
                        field: "new_label_color", value: draft.color || "#ffffff",
                        label: "New label color" })}
                    <button type="button" class="fb-button fb-button-primary"
                            data-act="add_label">Add</button>`)}
                ${this.field("Place at", "",
                    this.anchorGrid("new_label", draft.position || "top_left"))}
                ${single ? "" : `<p class="fb-side-note">Added to all
                    ${panels.length} selected images.</p>`}
                ${rows.length ? `
                <div class="fb-label-list">
                    ${rows.map((entry, index) => this.labelRow(entry, index, rows.length)).join("")}
                </div>` : `<p class="fb-side-note">${single
                    ? "Nothing on this image yet."
                    : "Select one image to edit the labels already on it."}</p>`}
            </section>`;
    }

    /** One existing caption: everything about it, plus where it sits in the
     *  stack. Reordering is up/down rather than a drag because the order only
     *  decides which of two captions in the same corner is on top. */
    labelRow(entry, index, total) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const id = escape(entry.label_id);
        return `<div class="fb-label-row" data-label-id="${id}">
            <input class="fb-input" type="text" maxlength="200"
                   data-field="label_text" data-label-id="${id}"
                   aria-label="Label text" value="${escape(entry.text)}">
            ${this.sizeSelect("label_size", entry.size_pt ?? null, "",
                              `data-label-id="${id}"`)}
            <select class="fb-select fb-select-tiny" data-field="label_position"
                    data-label-id="${id}" aria-label="Position">
                ${FigureSchema.PANEL_ANCHORS.map((anchor) =>
                    `<option value="${anchor}"${entry.position === anchor ? " selected" : ""}
                    >${escape(FigureImagePanel.anchorName(anchor))}</option>`).join("")}
            </select>
            ${FigureColorField.swatch({
                field: `label_color:${entry.label_id}`, value: entry.color,
                label: "Label color" })}
            <button type="button" class="fb-icon-button" data-act="label_up:${id}"
                    title="Move up" ${index === 0 ? "disabled" : ""}>
                <span class="fas fa-arrow-up" aria-hidden="true"></span></button>
            <button type="button" class="fb-icon-button" data-act="label_down:${id}"
                    title="Move down" ${index === total - 1 ? "disabled" : ""}>
                <span class="fas fa-arrow-down" aria-hidden="true"></span></button>
            <button type="button" class="fb-icon-button fb-icon-button-danger"
                    data-act="label_delete:${id}" title="Delete">
                <span class="fas fa-xmark" aria-hidden="true"></span></button>
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

        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Legend</h3>
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
                </div>` : ""}
            </section>`;
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
        const single = this.single;
        const armed = Boolean(this.handlers.hasRenderClipboard?.());
        return `
            <section class="fb-side-section">
                <h3 class="fb-side-subheading">Rendering</h3>
                <div class="fb-side-actions">
                    <button type="button" class="fb-button" data-act="copy_rendering"
                            ${single ? "" : "disabled"}>
                        <span class="fas fa-eye-dropper" aria-hidden="true"></span>
                        Copy
                    </button>
                    <button type="button" class="fb-button" data-act="apply_rendering"
                            ${armed ? "" : "disabled"}>
                        <span class="fas fa-fill-drip" aria-hidden="true"></span>
                        Apply to ${panels.length === 1 ? "this" : `these ${panels.length}`}
                    </button>
                </div>
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
        const where = active.id ? `#${active.id}`
            : active.dataset && active.dataset.field
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
        const ids = panels.map((panel) => panel.panel_id);
        // Row actions carry their label's id after a colon, so that one
        // delegated handler serves a list whose length changes.
        const [act, argument] = String(button.dataset.act).split(":");

        ({
            quick_edit: () => single && this.handlers.onQuickEdit?.(single.panel_id),
            viewer: () => single && this.handlers.onEditPanel?.(single.panel_id),
            split_with_composite: () => this.handlers.onSplit?.("with_composite"),
            split_channels_only: () => this.handlers.onSplit?.("channels_only"),
            pixel_size: () => this.applyPixelSize(panels),
            legend_keep: () => this.applyLegend(panels, { channels: true }),
            legend_share: () => this.shareLegendColours(panels),
            copy_rendering: () => single && this.handlers.onCopyRendering?.(single.panel_id),
            apply_rendering: () => this.handlers.onApplyRendering?.(ids),
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

        // Numbering is the FIGURE's, not a panel's: every label on the page is
        // drawn from it, so it goes to the document rather than to whichever
        // panel happened to be selected.
        if (field === "label_style") {
            this.handlers.onSettingsChange?.({ label_style: input.value });
            return;
        }

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
        const change = field === "label_text" ? { text: input.value }
            : field === "label_position" ? { position: input.value }
                : field === "label_size" ? { size_pt: input.value ? Number(input.value) : null }
                    : null;
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
