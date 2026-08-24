/**
 * FigureCanvas - laying panels out on a page.
 *
 * Everything positional is in MILLIMETRES, converted to CSS pixels by exactly
 * one number (`scale`) at the moment of drawing. Nothing is ever stored in
 * pixels. That is what makes zooming free, what makes the same figure lay out
 * identically on a different monitor, and what makes "4 mm gutter" mean 4 mm in
 * the exported PDF rather than 4 mm on whichever screen it was nudged into
 * place on.
 *
 * Panels are cached preview rasters -- <img> elements, one HTTP request each.
 * Never live viewers: a figure with a hundred panels would otherwise be a
 * hundred WebGL contexts and a hundred tile queues, and the browser stops
 * responding long before the figure is finished. Editing a panel's SCENE hands
 * off to the one live viewer (see onEditPanel).
 *
 * ## Drags are provisional until they are released
 *
 * A pointer move writes inline styles and nothing else. One `move_panels`
 * operation is committed on release, which is what makes a drag of five
 * selected panels one undo step, and what keeps a save out of the pointer path
 * -- a request per mousemove would be hundreds of writes for one gesture whose
 * intermediate positions nobody wants.
 *
 * ## Snapping
 *
 * Candidate lines come from the page (edges, margins, centre) and from every
 * other panel (its three horizontal and three vertical lines). The threshold is
 * in SCREEN pixels, converted to mm per gesture: a fixed mm threshold is
 * unusably sticky zoomed in and useless zoomed out.
 */
class FigureCanvas {

    /** How close, in SCREEN pixels, snaps. */
    static get SNAP_PIXELS() { return 6; }

    /** Nudge distance in mm, and the coarse one with Shift. */
    static get NUDGE_MM() { return 0.5; }
    static get NUDGE_COARSE_MM() { return 5; }

    /** Smallest panel, in mm. Below this the handles overlap and it cannot be
     *  grabbed to make it bigger again. */
    static get MIN_SIZE_MM() { return 5; }

    /** The floor for a TEXT box, which is not the same problem.
     *
     *  A single 8 pt line is 8 x 1.2 x 25.4/72 = 3.39mm tall, so the 5mm floor
     *  would inflate every one-line caption by nearly half and make `autofit`
     *  fight the clamp on every commit. */
    static get MIN_TEXT_MM() { return 1; }

    /** How far a duplicate or a paste lands from the original. Enough to see
     *  that there are two of something, small enough to still be next to it. */
    static get PASTE_OFFSET_MM() { return 4; }

    /** What `page.background` says instead of a colour. Matches
     *  `server/schema.TRANSPARENT`. */
    static get TRANSPARENT() { return "transparent"; }

    constructor(options) {
        this.state = options.state;
        this.api = options.api;
        this.figureId = options.figureId;
        this.onEditPanel = options.onEditPanel || (() => {});
        this.onSelectionChange = options.onSelectionChange || (() => {});
        //: Told when a drag starts and stops, so chrome that floats over the
        //: page can get out of the way of it. Not an event bus: there is one
        //: listener and it is the workspace that built this.
        this.onGesture = options.onGesture || (() => {});
        //: A drawing tool has placed something and the rail should stand down.
        this.onToolFinished = options.onToolFinished || (() => {});
        //: Open the in-place editor on a text annotation. The editor lives in
        //: the overlay layer, which this does not own -- render() would destroy
        //: it mid-keystroke.
        this.onEditText = options.onEditText || (() => {});

        this.pageEl = options.pageEl;
        this.surfaceEl = options.surfaceEl;
        this.guideEl = options.guideEl || null;

        this.scale = 96 / 25.4;
        this.pageId = null;
        this.selection = new Set();
        //: {kind, items:[{kind, id, start}], origin, handle} while a gesture is
        //: in flight. Null the rest of the time, which is what every handler
        //: below tests instead of a set of booleans.
        this.gesture = null;
        //: The armed drawing tool -- "text", "rect", "ellipse", "line",
        //: "arrow" -- or null for select. One-shot: see setTool.
        this.tool = null;
        //: What a drag may snap onto, from the View menu. Held here rather than
        //: read from a preference store on every pointer move.
        this.snapping = { guides: true, grid: false, gridMm: 5 };
    }

    // -- units -----------------------------------------------------------

    toPx(mm) { return mm * this.scale; }

    toMm(px) { return px / this.scale; }

    /**
     * The page being edited, or null when there is not one yet.
     *
     * Null is a real answer here rather than a defensive shrug: the canvas is
     * constructed and wired before `state.load()` has returned, and the View
     * menu draws the margins from this during that window. Every caller already
     * tests it -- `render`, `zoomToFit`, `drawMargins` -- so the one place that
     * has to know a document can be absent is this getter.
     */
    get page() {
        if (!this.state.document) return null;
        return FigureSchema.pageById(this.state.document, this.pageId);
    }

    // -- lifecycle -------------------------------------------------------

    setup() {
        this.surfaceEl.addEventListener("pointerdown", (event) => this.pointerDown(event));
        // On the window rather than on the surface: a fast drag leaves the
        // element behind, and a move handler bound to the panel stops firing
        // the moment the pointer outruns it.
        this._onMove = (event) => this.pointerMove(event);
        this._onUp = (event) => this.pointerUp(event);
        window.addEventListener("pointermove", this._onMove);
        window.addEventListener("pointerup", this._onUp);
        this._onKey = (event) => this.keyDown(event);
        window.addEventListener("keydown", this._onKey);

        this.surfaceEl.addEventListener("dragover", (event) => {
            if (event.dataTransfer?.types.includes("text/x-plexora-panel")) event.preventDefault();
        });
        this.surfaceEl.addEventListener("drop", (event) => this.dropFromTray(event));
    }

    destroy() {
        window.removeEventListener("pointermove", this._onMove);
        window.removeEventListener("pointerup", this._onUp);
        window.removeEventListener("keydown", this._onKey);
    }

    // -- rendering -------------------------------------------------------

    setPage(pageId) {
        this.pageId = pageId;
        this.render();
    }

    setScale(scale) {
        this.scale = Math.max(0.4, Math.min(20, scale));
        this.render();
    }

    zoomToFit(viewportEl) {
        const page = this.page;
        if (!page || !viewportEl) return;
        const margin = 48;
        // clientWidth INCLUDES the padding, and the scroll surface's left
        // padding is what holds the sheet clear of the floating rail and tray
        // -- two hundred and eighty pixels of it with the tray open. Fitting to
        // the padding box put the page that far off the right edge of the
        // window. Guarded because the layout probes drive this class with stub
        // elements that have no owner document.
        const view = viewportEl.ownerDocument?.defaultView;
        const box = view?.getComputedStyle ? view.getComputedStyle(viewportEl) : null;
        const pad = (name) => (box ? parseFloat(box[name]) || 0 : 0);
        const width = viewportEl.clientWidth - pad("paddingLeft") - pad("paddingRight");
        const height = viewportEl.clientHeight - pad("paddingTop") - pad("paddingBottom");
        this.setScale(Math.min(
            (width - margin) / page.size_mm.w,
            (height - margin) / page.size_mm.h));
    }

    render() {
        const page = this.page;
        if (!page) return;

        this.pageEl.style.width = this.toPx(page.size_mm.w) + "px";
        this.pageEl.style.height = this.toPx(page.size_mm.h) + "px";
        // A transparent page is drawn as the conventional checkerboard, from a
        // class rather than an inline colour: "transparent" as a CSS background
        // would show the dark app surface behind it, which reads as a black
        // page rather than as no page.
        const clear = page.background === FigureCanvas.TRANSPARENT;
        this.pageEl.classList.toggle("is-transparent", clear);
        this.pageEl.style.background = clear ? "" : page.background;

        const panels = FigureSchema.panelsOnPage(this.state.document, this.pageId);
        const labelStyle = this.state.document.settings.label_style;
        const annotations = Object.values(this.state.document.annotations)
            .filter((annotation) => annotation.page_id === this.pageId);

        this.surfaceEl.innerHTML =
            panels.map((panel, index) => this.panelMarkup(panel, index, labelStyle)).join("")
            + annotations.map((annotation) => this.annotationMarkup(annotation)).join("");
        this.clearGuides();
    }

    panelMarkup(panel, index, labelStyle) {
        const place = panel.placement;
        const selected = this.selection.has(panel.panel_id);
        const label = panel.label.auto ? FigureSchema.labelFor(index, labelStyle) : panel.label.text;
        const source = this.state.source(panel.source_id);
        const status = this.state.sourceStatus[panel.source_id]?.status || "ok";

        return `<div class="fb-panel${selected ? " is-selected" : ""}"
                     data-panel-id="${FigureSchema.escapeHtml(panel.panel_id)}"
                     style="left:${this.toPx(place.x_mm)}px;top:${this.toPx(place.y_mm)}px;
                            width:${this.toPx(place.w_mm)}px;height:${this.toPx(place.h_mm)}px;
                            z-index:${place.z}">
            <img class="fb-panel-image" draggable="false"
                 src="${this.panelImageUrl(panel, source)}"
                 alt="" onerror="this.classList.add('fb-panel-image-missing')">
            ${this.legendMarkup(panel)}
            ${this.scaleBarMarkup(panel, source, place)}
            ${panel.label.visible && label
                ? `<span class="fb-panel-label">${FigureSchema.escapeHtml(label)}</span>` : ""}
            ${panel.title ? `<span class="fb-panel-title">${FigureSchema.escapeHtml(panel.title)}</span>` : ""}
            ${status !== "ok"
                ? `<span class="fb-panel-badge fb-panel-badge-${status}"
                         title="This panel's source has ${status === "missing" ? "gone" : "changed"}">
                       <span class="fas fa-triangle-exclamation"></span></span>` : ""}
            ${selected ? this.handlesMarkup() : ""}
        </div>`;
    }

    /**
     * A scale bar, or nothing at all.
     *
     * Nothing, specifically, when the source has no physical calibration --
     * never a bar drawn from an assumed pixel size, which is wrong and looks
     * exactly like one that is right.
     */
    scaleBarMarkup(panel, source, place) {
        if (!panel.scalebar.visible) return "";
        const span = FigureSchema.physicalWidthUm(source, panel.scene.viewport);
        if (!span) return "";
        const length = panel.scalebar.target_um || FigureSchema.scaleBarLength(span);
        const fraction = length / span;
        if (!(fraction > 0) || fraction > 1) return "";
        return `<span class="fb-panel-scalebar" style="width:${(fraction * 100).toFixed(2)}%">
            <span class="fb-panel-scalebar-label">${FigureSchema.escapeHtml(
                FigureSchema.formatMicrons(length))}</span>
        </span>`;
    }

    /**
     * Where a panel's picture comes from on screen.
     *
     * An imported asset is served straight from the figure's own directory:
     * there is nothing to preview because the file IS the panel, and rendering
     * a preview of it would be storing a worse copy of something already here.
     * Everything else is the cached capture raster.
     */
    panelImageUrl(panel, source) {
        if (source && source.kind === "imported_asset" && source.asset_id) {
            return this.api.assetUrl(this.figureId, source.asset_id);
        }
        return this.api.previewUrl(this.figureId, panel.panel_id, panel.render_revision);
    }

    /**
     * The panel's legend, drawn from what was recorded at capture time.
     *
     * Never from the live plugins. A legend regenerated from a palette that has
     * since changed is a legend that disagrees with the panel above it -- and
     * on a figure whose plugin is not even installed there would be nothing to
     * regenerate it from. Each plugin computes its rows once, at capture, and
     * they travel with the panel; see the capture bridge.
     */
    legendMarkup(panel) {
        const rows = [];
        if (panel.legend.channels) {
            for (const channel of panel.scene.channels || []) {
                const color = `rgb(${channel.color.r},${channel.color.g},${channel.color.b})`;
                rows.push(this.legendRow(color, channel.fullname_at_capture || channel.key));
            }
        }
        if (panel.legend.plugins) {
            for (const contribution of Object.values(panel.scene.plugins || {})) {
                for (const entry of contribution.legend || []) {
                    if (entry.kind === "continuous") {
                        rows.push(this.legendRamp(entry));
                    } else {
                        rows.push(this.legendRow(entry.color, entry.label));
                    }
                }
            }
        }
        if (!rows.length) return "";
        return `<div class="fb-panel-legend">${rows.join("")}</div>`;
    }

    legendRow(color, label) {
        return `<span class="fb-legend-row">
            <span class="fb-legend-swatch" style="background:${FigureSchema.escapeHtml(color)}"></span>
            <span>${FigureSchema.escapeHtml(label)}</span>
        </span>`;
    }

    legendRamp(entry) {
        const stops = (entry.ramp || []).map((color) => FigureSchema.escapeHtml(color)).join(",");
        const [low, high] = entry.domain || [0, 1];
        return `<span class="fb-legend-row">
            <span class="fb-legend-ramp" style="background:linear-gradient(to right,${stops})"></span>
            <span>${FigureSchema.escapeHtml(this.formatNumber(low))}&ndash;${FigureSchema.escapeHtml(this.formatNumber(high))}</span>
        </span>`;
    }

    formatNumber(value) {
        if (!Number.isFinite(value)) return "";
        const magnitude = Math.abs(value);
        if (magnitude >= 1000 || (magnitude > 0 && magnitude < 0.01)) return value.toExponential(1);
        return String(Math.round(value * 100) / 100);
    }

    handlesMarkup(rotatable) {
        const handles = ["nw", "ne", "se", "sw", "n", "e", "s", "w"].map((handle) =>
            `<span class="fb-handle fb-handle-${handle}" data-handle="${handle}"></span>`).join("");
        // The rotate handle stands OFF the top edge rather than sitting on it:
        // on the outline it would land under the `n` handle, and at a caption's
        // size those are the same few pixels.
        return rotatable
            ? handles + '<span class="fb-handle fb-handle-rotate" data-handle="rotate"></span>'
            : handles;
    }

    /** Points per millimetre. Annotation stroke and font sizes are stored in
     *  points because that is what the PDF exporter draws in. */
    static get PT_PER_MM() { return 2.8346; }

    annotationMarkup(annotation) {
        if (annotation.type === "line" || annotation.type === "arrow") {
            return this.strokeMarkup(annotation);
        }
        const geometry = annotation.geometry;
        const selected = this.selection.has(annotation.annotation_id) ? " is-selected" : "";
        // A rotation turns the whole box about its own centre, AFTER the layout
        // inside it is settled -- the same rule the PDF and raster writers use.
        // CSS carries hit-testing through a transform, so `closest()` keeps
        // working and only `resizedBox` has to know the angle.
        const rotation = geometry.rotation
            ? `transform:rotate(${geometry.rotation}deg);transform-origin:center;` : "";
        const style = [
            `left:${this.toPx(geometry.x_mm)}px`,
            `top:${this.toPx(geometry.y_mm)}px`,
            `width:${this.toPx(geometry.w_mm)}px`,
            `height:${this.toPx(geometry.h_mm)}px`,
            `z-index:${1000 + annotation.z}`,
            `color:${annotation.style.color}`,
        ].join(";");

        if (annotation.type === "text") {
            return `<div class="fb-annotation fb-annotation-text${selected}"
                        style="${style};${rotation}"
                        data-annotation-id="${FigureSchema.escapeHtml(annotation.annotation_id)}"
                    >${this.textMarkup(annotation)}${
                        selected ? this.handlesMarkup(true) : ""}</div>`;
        }
        const fill = annotation.style.fill
            ? `background:${annotation.style.fill};` : "";
        return `<div class="fb-annotation fb-annotation-${annotation.type}${selected}"
                     style="${style};${rotation}${fill}border-color:${annotation.style.color};
                            border-width:${Math.max(1,
                                annotation.style.line_width_pt * this.scale / FigureCanvas.PT_PER_MM)}px"
                     data-annotation-id="${FigureSchema.escapeHtml(annotation.annotation_id)}"
                >${selected ? this.handlesMarkup(true) : ""}</div>`;
    }

    // -- text ------------------------------------------------------------

    /**
     * Every line of a text annotation, positioned. Pure arithmetic, no DOM.
     *
     * The JavaScript half of `compose._text_layout`, and
     * `test_the_canvas_and_the_exporter_put_the_baseline_in_the_same_place`
     * asserts the two agree to a nanometre. If they drift, the caption sits in
     * one place on screen and another in the PDF -- and nothing says so until
     * somebody opens the export.
     *
     * Where the lines BREAK is not decided here. Only a browser can measure a
     * string, so the browser breaks them once (`FigureRichText.rewrap`) and the
     * break is stored; this only stacks lines that already exist.
     *
     * Baselines come back in millimetres from the PAGE top, matching the
     * exporter. `textMarkup` subtracts the box's own origin.
     */
    static textLayout(annotation) {
        const style = annotation.style;
        const geometry = annotation.geometry;
        // Falls back to normalising the flat string, because the draft this
        // draws from is OPTIMISTIC: `state.commit` applies the change locally
        // and the server's normaliser -- which is what puts `rich` on an
        // annotation -- has not run yet. Without this a text box drawn a moment
        // ago renders as nothing at all until the page is reloaded.
        const rich = annotation.rich && annotation.rich.lines
            ? annotation.rich
            : FigureRichText.normalize(annotation.text || "", null);
        const lines = rich.lines;

        const measured = lines.map((line) => {
            const metrics = FigureRichText.lineMetrics(line.runs, style);
            return { runs: line.runs, hard: line.hard !== false, ...metrics };
        });
        const block = measured.reduce((total, line) => total + line.lead, 0);

        let top = geometry.y_mm;
        if (style.valign === "middle") top += (geometry.h_mm - block) / 2;
        else if (style.valign === "bottom") top += geometry.h_mm - block;

        let cursor = top;
        const out = measured.map((line, index) => {
            // The line sits centred in its own box, half the leading above and
            // half below, so a line mixing an 8 pt caption with a 6 pt
            // superscript lands where a reader expects rather than riding the
            // bottom of the box.
            const halfLead = (line.lead - (line.ascent + line.descent)) / 2;
            const next = measured[index + 1];
            const entry = {
                baseline_mm: cursor + halfLead + line.ascent,
                lead_mm: line.lead,
                runs: line.runs,
                last_of_paragraph: !next || next.hard,
            };
            cursor += line.lead;
            return entry;
        });
        return { block_h_mm: block, lines: out };
    }

    /**
     * A text annotation's words, as SVG inside its box.
     *
     * SVG rather than a styled <div> because `<text y>` places the BASELINE
     * exactly at y. A div's baseline comes from whatever font file the browser
     * actually resolved -- Arial where the PDF holds Helvetica -- and those
     * differ by about 0.09 em, which at 8 pt is a quarter of a millimetre of
     * disagreement that no test could reach. Putting the baseline in the markup
     * takes the browser's guess out of the loop entirely.
     *
     * Each run is its own <text> at a computed pen position, which is the same
     * walk both exporters do, so a line of mixed styling cannot be spaced one
     * way here and another way there. The precedent is `strokeMarkup`, which
     * already draws lines and arrows as inline SVG.
     */
    textMarkup(annotation) {
        const layout = FigureCanvas.textLayout(annotation);
        const style = annotation.style;
        const boxWidth = this.toPx(annotation.geometry.w_mm);
        const parts = [];

        for (const line of layout.lines) {
            if (!line.runs.length) continue;
            const runs = line.runs.map((run) => FigureRichText.resolveRun(run, style));
            const widths = runs.map((run) => this.measureRun(run));
            let lineWidth = widths.reduce((total, width) => total + width, 0);
            // Trailing whitespace is excluded from the width so a wrapped line
            // centres on its words. The space the break landed on is kept in
            // the run -- that is what makes the break reversible -- and would
            // otherwise pull every centred line slightly left.
            const last = runs[runs.length - 1];
            if (last.text !== last.text.replace(/\s+$/, "")) {
                lineWidth -= widths[widths.length - 1]
                    - this.measureRun({ ...last, text: last.text.replace(/\s+$/, "") });
            }

            let pen = 0;
            if (style.align === "center") pen = (boxWidth - lineWidth) / 2;
            else if (style.align === "right") pen = boxWidth - lineWidth;
            let extra = 0;
            if (style.align === "justify" && !line.last_of_paragraph) {
                const gaps = runs.reduce(
                    (total, run) => total + (run.text.split(" ").length - 1), 0);
                if (gaps && boxWidth > lineWidth) extra = (boxWidth - lineWidth) / gaps;
            }

            const baseline = this.toPx(line.baseline_mm - annotation.geometry.y_mm);
            for (let index = 0; index < runs.length; index += 1) {
                const run = runs[index];
                const spread = extra * (run.text.split(" ").length - 1);
                const width = widths[index] + spread;
                parts.push(this.runMarkup(run, pen, baseline, extra));
                parts.push(this.decorationMarkup(run, pen, baseline, width));
                pen += width;
            }
        }
        // overflow visible: a box narrower than a word shows the word running
        // past its edge rather than clipping it, which is the honest picture --
        // the export will do the same.
        return `<svg class="fb-text-svg" width="100%" height="100%" overflow="visible"
                     aria-hidden="true">${parts.join("")}</svg>`;
    }

    runMarkup(run, x, baseline, wordSpacing) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        return `<text x="${x}" y="${baseline}" xml:space="preserve"
                      font-family='${escape(FigureRichText.cssStack(run.family))}'
                      font-size="${this.fontPx(run.size_pt)}"
                      ${run.bold ? 'font-weight="bold"' : ""}
                      ${run.italic ? 'font-style="italic"' : ""}
                      ${wordSpacing ? `word-spacing="${wordSpacing}"` : ""}
                      fill="${escape(run.color)}">${escape(run.text)}</text>`;
    }

    /**
     * Underline and strike, as drawn rules.
     *
     * Not `text-decoration`: SVG takes the underline's position from the font
     * file the browser resolved, and the PDF has no underline of its own at all
     * -- it is always a drawn rule. Both sides use the same fraction of the em
     * instead, so the mark lands in the same place in both.
     */
    decorationMarkup(run, x, baseline, width) {
        const em = this.fontPx(run.size_pt);
        const thickness = Math.max(1, em * FigureRichText.UNDERLINE_THICKNESS_EM);
        const rules = [];
        if (run.underline) rules.push(baseline + em * FigureRichText.UNDERLINE_OFFSET_EM);
        if (run.strike) rules.push(baseline - em * FigureRichText.STRIKE_OFFSET_EM);
        return rules.map((y) =>
            `<rect x="${x}" y="${y}" width="${Math.max(0, width)}" height="${thickness}"
                   fill="${FigureSchema.escapeHtml(run.color)}"></rect>`).join("");
    }

    /** A run's type size in CSS pixels at the current zoom. */
    fontPx(sizePt) {
        return sizePt * FigureRichText.MM_PER_PT * this.scale;
    }

    /**
     * How wide a run is, in CSS pixels.
     *
     * The one genuinely browser-bound piece of text layout, which is why
     * `FigureRichText.rewrap` takes a measuring function rather than calling
     * this: node has no text engine, and a probe that could not measure could
     * not test the breaking algorithm at all.
     *
     * Returns 0 without a real canvas, which is the probe's case -- a fake
     * measurement would be worse than none, because it would look like layout.
     */
    measureRun(run) {
        if (!this._measureCtx) {
            const canvas = document.createElement("canvas");
            this._measureCtx = canvas.getContext ? canvas.getContext("2d") : null;
        }
        if (!this._measureCtx) return 0;
        this._measureCtx.font = `${run.italic ? "italic " : ""}${run.bold ? "bold " : ""}`
            + `${this.fontPx(run.size_pt)}px ${FigureRichText.cssStack(run.family)}`;
        return this._measureCtx.measureText(run.text).width;
    }

    /** Re-break a text annotation to its own box width, in millimetres. */
    rewrapAnnotation(annotation) {
        return FigureRichText.rewrap(
            annotation.rich, annotation.geometry.w_mm, annotation.style,
            (text, run) => this.toMm(this.measureRun({ ...run, text: text })));
    }

    /**
     * A line or an arrow, as real SVG.
     *
     * These used to render as empty bordered boxes -- the schema, the
     * operations and the PDF exporter all understood them and the canvas drew a
     * rectangle, so an arrow looked like a rectangle right up until it was
     * exported.
     *
     * Two things make this fiddlier than the other four types:
     *
     * **The geometry is a vector, not a box.** `w_mm` and `h_mm` are legally
     * negative -- they are the offset from the start point to the end point --
     * and a div cannot have a negative width. The element is therefore the
     * NORMALISED bounds, padded, with the line drawn inside it in local
     * coordinates.
     *
     * **A diagonal's bounding box is mostly empty.** A long arrow across a page
     * has a bounding box covering a quarter of it, and a box that took clicks
     * would be a place where selecting panels underneath quietly stopped
     * working. So the container takes no pointer events and a fat transparent
     * line under the visible one does, which is the standard trick and the only
     * one that puts the hit area on the ink.
     */
    strokeMarkup(annotation) {
        const geometry = annotation.geometry;
        const selected = this.selection.has(annotation.annotation_id);
        const width = this.toPx(geometry.w_mm);
        const height = this.toPx(geometry.h_mm);
        const stroke = Math.max(1,
            annotation.style.line_width_pt * this.scale / FigureCanvas.PT_PER_MM);
        const head = this.arrowHeadPx(annotation.style.line_width_pt);
        const pad = head + stroke + 2;

        const x1 = pad + (width < 0 ? -width : 0);
        const y1 = pad + (height < 0 ? -height : 0);
        const x2 = pad + (width < 0 ? 0 : width);
        const y2 = pad + (height < 0 ? 0 : height);

        const parts = [
            `<line class="fb-stroke-hit" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`,
            `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"`
            + ` stroke="${FigureSchema.escapeHtml(annotation.style.color)}"`
            + ` stroke-width="${stroke}" stroke-linecap="round"/>`,
        ];
        if (annotation.type === "arrow") {
            for (const [hx, hy] of FigureCanvas.arrowHeadPoints(x1, y1, x2, y2, head)) {
                parts.push(`<line x1="${x2}" y1="${y2}" x2="${hx}" y2="${hy}"`
                    + ` stroke="${FigureSchema.escapeHtml(annotation.style.color)}"`
                    + ` stroke-width="${stroke}" stroke-linecap="round"/>`);
            }
        }

        const boxWidth = Math.abs(width) + pad * 2;
        const boxHeight = Math.abs(height) + pad * 2;
        const style = [
            `left:${this.toPx(geometry.x_mm + Math.min(0, geometry.w_mm)) - pad}px`,
            `top:${this.toPx(geometry.y_mm + Math.min(0, geometry.h_mm)) - pad}px`,
            `width:${boxWidth}px`,
            `height:${boxHeight}px`,
            `z-index:${1000 + annotation.z}`,
        ].join(";");

        return `<div class="fb-annotation fb-annotation-stroke${selected ? " is-selected" : ""}"
                     style="${style}"
                     data-annotation-id="${FigureSchema.escapeHtml(annotation.annotation_id)}">
            <svg width="${boxWidth}" height="${boxHeight}">${parts.join("")}</svg>
            ${selected
                ? `<span class="fb-handle fb-handle-point" data-handle="p1"
                          style="left:${x1 - 5}px;top:${y1 - 5}px"></span>
                   <span class="fb-handle fb-handle-point" data-handle="p2"
                          style="left:${x2 - 5}px;top:${y2 - 5}px"></span>` : ""}
        </div>`;
    }

    /**
     * The arrowhead's length, in screen pixels.
     *
     * Deliberately the same rule as `export._arrow_head`, which uses
     * `max(3, line_width * 4)` POINTS. An arrowhead sized by some other rule
     * here would make every arrow look different in the PDF from the way it
     * looked while it was being placed -- and the PDF is the deliverable.
     */
    arrowHeadPx(lineWidthPt) {
        return Math.max(3, lineWidthPt * 4) * this.scale / FigureCanvas.PT_PER_MM;
    }

    /** The two barb endpoints, spread 160 degrees from the shaft -- the angle
     *  `export._arrow_head` uses. Pure, so the parity can be checked. */
    static arrowHeadPoints(x1, y1, x2, y2, size) {
        const angle = Math.atan2(y2 - y1, x2 - x1);
        return [-1, 1].map((direction) => {
            const spread = angle + direction * (160 * Math.PI / 180);
            return [x2 + size * Math.cos(spread), y2 + size * Math.sin(spread)];
        });
    }

    // -- selection -------------------------------------------------------

    /**
     * Set or extend the selection.
     *
     * Every id is expanded to its whole visual group on the way in, so a group
     * cannot be half-selected from anywhere -- not from a click, not from a
     * marquee, not from a context menu. Half a group would drag apart under
     * the pointer, which is the one thing grouping exists to prevent.
     */
    select(ids, additive) {
        if (!additive) this.selection.clear();
        for (const id of this.expandToGroups(ids)) {
            if (additive && this.selection.has(id)) this.selection.delete(id);
            else this.selection.add(id);
        }
        this.render();
        this.onSelectionChange(Array.from(this.selection));
    }

    /** The visual group holding this panel or annotation, or null. */
    groupFor(id) {
        const groups = this.state.document.groups || {};
        for (const group of Object.values(groups)) {
            if (group.member_ids.includes(id)) return group;
        }
        return null;
    }

    expandToGroups(ids) {
        const out = [];
        const seen = new Set();
        for (const id of ids) {
            const members = this.groupFor(id)?.member_ids || [id];
            for (const member of members) {
                if (seen.has(member)) continue;
                seen.add(member);
                out.push(member);
            }
        }
        return out;
    }

    selectedPanels() {
        return Array.from(this.selection)
            .map((id) => this.state.panel(id))
            .filter(Boolean);
    }

    selectedAnnotations() {
        return Array.from(this.selection)
            .map((id) => this.state.document.annotations[id])
            .filter(Boolean);
    }

    selectAllOnPage() {
        const panels = FigureSchema.panelsOnPage(this.state.document, this.pageId)
            .map((panel) => panel.panel_id);
        const annotations = Object.values(this.state.document.annotations)
            .filter((annotation) => annotation.page_id === this.pageId)
            .map((annotation) => annotation.annotation_id);
        this.select(panels.concat(annotations), false);
    }

    // -- gestures --------------------------------------------------------

    surfacePoint(event) {
        const rect = this.surfaceEl.getBoundingClientRect();
        return { x: this.toMm(event.clientX - rect.left), y: this.toMm(event.clientY - rect.top) };
    }

    pointerDown(event) {
        if (event.button !== 0) return;

        // A drawing tool takes the whole surface: while one is armed, a press
        // starts a shape rather than selecting whatever is under it. Tools are
        // one-shot, so this state lasts exactly one gesture.
        if (this.tool && this.tool !== "select") {
            event.preventDefault();
            this.select([], false);
            this.beginGesture("draw", event, {});
            return;
        }

        const handle = event.target.closest?.(".fb-handle");
        const panelEl = event.target.closest?.(".fb-panel");
        const annotationEl = event.target.closest?.(".fb-annotation");

        // The second press comes first, ahead of the handle, and ahead of the
        // move: a one-line caption at 14 pt is about twenty pixels tall, so its
        // handles cover most of it, and "wherever in the object you pressed
        // twice" is the only rule that opens it every time.
        const id = panelEl ? panelEl.dataset.panelId
            : (annotationEl ? annotationEl.dataset.annotationId : null);
        const opens = annotationEl?.classList.contains("fb-annotation-text")
            ? () => this.onEditText(id)
            : (panelEl ? () => this.onEditPanel(id) : null);
        if (this.secondPress(event, id) && opens) {
            event.preventDefault();
            this._press = null;
            opens();
            return;
        }

        if (handle && (panelEl || annotationEl)) {
            event.preventDefault();
            const kind = handle.dataset.handle === "rotate" ? "rotate" : "resize";
            this.beginGesture(kind, event, { handle: handle.dataset.handle });
            return;
        }
        if (panelEl || annotationEl) {
            if (!this.selection.has(id)) this.select([id], event.shiftKey);
            else if (event.shiftKey) { this.select([id], true); return; }
            event.preventDefault();
            this.beginGesture("move", event, {});
            return;
        }
        // Empty page: a marquee, or a click that clears the selection.
        this.select([], false);
        this.beginGesture("marquee", event, {});
    }

    /**
     * Is this press the second half of a double-click on the same object?
     *
     * Asked here rather than answered by a `dblclick` listener, and that is the
     * whole of the fix. Three separate things were breaking that listener, all
     * of them still true of this file:
     *
     *   * the first press SELECTS, selecting re-renders, and `render()` rewrites
     *     the surface -- so the two clicks have different targets and the event
     *     is retargeted to the surface, where `closest` finds nothing;
     *   * the recovery for that was `elementFromPoint`, which answers with the
     *     topmost element at the point -- and the floating bar the first click
     *     just opened is in the overlay ABOVE the page, so whenever it lands
     *     over the object the handler bailed;
     *   * `pointerdown` is default-prevented on every press that lands on an
     *     object (see below), which suppresses the compatibility mouse events a
     *     double-click is derived from.
     *
     * A press knows what it hit, before any of that: `event.target` at
     * `pointerdown` is the element that was pressed, the surface has not been
     * rewritten yet, and nothing has been drawn over it. What it does not know
     * is that a press happened here a moment ago, which is the one thing kept
     * here.
     *
     * The identity is the OBJECT, not the element -- the element the second
     * press lands on is a different one from the first, freshly rendered, and
     * comparing elements is exactly what does not work.
     */
    secondPress(event, id) {
        const previous = this._press;
        this._press = id
            ? { id: id, at: event.timeStamp, x: event.clientX, y: event.clientY }
            : null;
        return Boolean(id && previous && previous.id === id
            && event.timeStamp - previous.at < FigureCanvas.DOUBLE_PRESS_MS
            && Math.abs(event.clientX - previous.x) <= FigureCanvas.DOUBLE_PRESS_PX
            && Math.abs(event.clientY - previous.y) <= FigureCanvas.DOUBLE_PRESS_PX);
    }

    /** The platform's own double-click interval is not readable from a page, so
     *  this is the usual default. The distance is what keeps a drag that
     *  happened to end where it started from reading as a double-click. */
    static get DOUBLE_PRESS_MS() { return 400; }
    static get DOUBLE_PRESS_PX() { return 4; }

    beginGesture(kind, event, extra) {
        this.onGesture(true);
        const origin = this.surfacePoint(event);
        this.gesture = {
            kind: kind,
            origin: origin,
            current: origin,
            moved: false,
            handle: extra.handle || null,
            // The starting geometry of everything being moved, captured once:
            // reading it back off the DOM each frame would compound rounding
            // and make a long drag drift.
            items: this.gestureItems(),
        };
    }

    gestureItems() {
        const items = [];
        for (const id of this.selection) {
            const panel = this.state.panel(id);
            if (panel && panel.placement) {
                items.push({ kind: "panel", id: id, start: { ...panel.placement } });
                continue;
            }
            const annotation = this.state.document.annotations[id];
            if (annotation) {
                items.push({ kind: "annotation", id: id, start: { ...annotation.geometry } });
            }
        }
        return items;
    }

    pointerMove(event) {
        if (!this.gesture) return;
        this.gesture.current = this.surfacePoint(event);
        const dx = this.gesture.current.x - this.gesture.origin.x;
        const dy = this.gesture.current.y - this.gesture.origin.y;
        if (Math.abs(dx) > 0.2 || Math.abs(dy) > 0.2) this.gesture.moved = true;

        if (this.gesture.kind === "move") this.previewMove(dx, dy, event.shiftKey);
        else if (this.gesture.kind === "resize") this.previewResize(dx, dy, event.shiftKey);
        else if (this.gesture.kind === "marquee") this.previewMarquee();
        else if (this.gesture.kind === "draw") this.previewDraw(event.shiftKey);
        else if (this.gesture.kind === "rotate") this.previewRotate(event.shiftKey);
    }

    pointerUp() {
        const gesture = this.gesture;
        this.gesture = null;
        if (!gesture) return;
        this.onGesture(false);
        this.clearGuides();
        // A press that turned into a drag cannot be the first half of a double
        // click, however quickly the next one follows it.
        if (gesture.moved) this._press = null;

        if (gesture.kind === "draw") {
            this.finishDraw(gesture);
            return;
        }
        if (gesture.kind === "marquee") {
            this.finishMarquee(gesture);
            return;
        }
        if (!gesture.moved) {
            this.render();
            return;
        }
        this.commitGesture(gesture);
    }

    /**
     * Write the provisional positions straight onto the elements.
     *
     * Inline styles rather than a re-render: a re-render per pointer move
     * rebuilds every <img> in the page, which makes the browser re-decode the
     * previews and turns a smooth drag into a slideshow.
     */
    previewMove(dx, dy, disableSnap) {
        const snapped = disableSnap ? { dx, dy } : this.snapMove(dx, dy);
        for (const item of this.gesture.items) {
            this.previewBox(item, {
                ...item.start,
                x_mm: item.start.x_mm + snapped.dx,
                y_mm: item.start.y_mm + snapped.dy,
            });
        }
        this.gesture.delta = snapped;
    }

    /** Angles a rotation snaps to with Shift held, in degrees. */
    static get ROTATE_STEP() { return 15; }

    /**
     * Turn the selection to follow the pointer.
     *
     * The angle is measured from the box's CENTRE to the pointer, not from the
     * drag's start -- so grabbing the handle and swinging round puts the top of
     * the box under the pointer, which is the thing every drawing tool does and
     * the only reading that survives dragging past 180 degrees.
     */
    previewRotate(snap) {
        for (const item of this.gesture.items) {
            if (item.kind === "panel") continue;
            const centre = { x: item.start.x_mm + item.start.w_mm / 2,
                             y: item.start.y_mm + item.start.h_mm / 2 };
            const point = this.gesture.current;
            // +90 because the handle stands ABOVE the box: with the pointer
            // straight up from the centre the box is at rest, not at -90.
            let degrees = Math.atan2(point.y - centre.y, point.x - centre.x)
                          * 180 / Math.PI + 90;
            if (snap) {
                degrees = Math.round(degrees / FigureCanvas.ROTATE_STEP)
                          * FigureCanvas.ROTATE_STEP;
            }
            degrees = Math.round(((degrees % 360) + 360) % 360 * 10) / 10;
            item.rotation = degrees;
            const element = this.elementFor(item);
            if (element) {
                element.style.transform = `rotate(${degrees}deg)`;
                element.style.transformOrigin = "center";
            }
        }
    }

    previewResize(dx, dy, keepAspect) {
        const handle = this.gesture.handle;
        for (const item of this.gesture.items) {
            this.previewBox(item, this.resizedBox(
                item.start, handle, dx, dy, keepAspect, this.annotationFor(item)));
        }
    }

    /** Whether this resize is the user taking a text box's height into their
     *  own hands -- any handle that changes the height, on a box that was
     *  following its contents. */
    clearsAutofit(item, gesture) {
        const annotation = this.annotationFor(item);
        return Boolean(annotation) && annotation.type === "text"
            && annotation.style.autofit
            && /[ns]/.test(gesture.handle || "");
    }

    /** The annotation an in-flight gesture item refers to, or null. */
    annotationFor(item) {
        return item.kind === "panel"
            ? null : (this.state.document.annotations[item.id] || null);
    }

    /**
     * Show one item at a provisional box, and remember it.
     *
     * The box is stored on the gesture item as well as written to the DOM, so
     * the commit reads a number rather than parsing a style back -- which
     * matters for lines, whose element is not their geometry (see
     * strokeMarkup).
     *
     * Ordinary items get inline styles rather than a re-render: re-rendering
     * per pointer move rebuilds every <img> on the page and turns a smooth drag
     * into a slideshow. A line has no <img> and cannot be expressed as four
     * style properties, so it is the one thing redrawn each frame.
     */
    previewBox(item, box) {
        item.box = box;
        const element = this.elementFor(item);
        if (!element) return;
        if (element.classList.contains("fb-annotation-stroke")) {
            const annotation = this.state.document.annotations[item.id];
            if (!annotation) return;
            element.outerHTML = this.strokeMarkup({ ...annotation, geometry: box });
            return;
        }
        element.style.left = this.toPx(box.x_mm) + "px";
        element.style.top = this.toPx(box.y_mm) + "px";
        if (this.gesture.kind === "resize") {
            element.style.width = this.toPx(box.w_mm) + "px";
            element.style.height = this.toPx(box.h_mm) + "px";
        }
    }

    /**
     * The box a resize produces.
     *
     * Corner handles keep the aspect ratio by DEFAULT and free it with Shift,
     * which is the opposite of most drawing tools and the right way round here:
     * a panel's aspect ratio is the shape of the region it shows, and changing
     * it silently squashes the tissue. Edge handles are single-axis by
     * definition and ignore the modifier.
     *
     * `p1` and `p2` are a line's two ENDS, and they are a different kind of
     * handle: the geometry of a line is a start point and an offset, so w and h
     * are legally negative and the minimum-size clamp below must not apply --
     * a line is allowed to be a hair thick, and clamping it to 5mm would stop
     * anyone drawing a horizontal one.
     */
    resizedBox(start, handle, dx, dy, freeAspect, annotation) {
        let { x_mm: x, y_mm: y, w_mm: w, h_mm: h } = start;
        const isText = Boolean(annotation) && annotation.type === "text";
        const rotation = (annotation && annotation.geometry.rotation) || 0;

        // A rotated box resizes along ITS OWN axes, so the pointer's movement is
        // turned back through the angle before any of the arithmetic below sees
        // it. Without this, dragging the corner of a box rotated 45 degrees
        // moves it diagonally instead of widening it.
        if (rotation) {
            const turned = FigureCanvas.turn(dx, dy, -rotation);
            dx = turned.x;
            dy = turned.y;
        }

        if (handle === "p1") {
            return { ...start, x_mm: x + dx, y_mm: y + dy, w_mm: w - dx, h_mm: h - dy };
        }
        if (handle === "p2") {
            return { ...start, w_mm: w + dx, h_mm: h + dy };
        }

        // Text inverts the modifier. The aspect lock exists because a panel's
        // shape is the shape of the region it shows and squashing it is a
        // scientific error -- a text box has no such invariant, and locking it
        // would mean widening a caption also made it taller, which `autofit`
        // then immediately undoes.
        const corner = handle.length === 2;
        if (corner && (isText ? freeAspect : !freeAspect)) {
            // Drive both axes from whichever the pointer moved further along,
            // so the shape follows the gesture rather than snapping between
            // two interpretations of it.
            const aspect = start.w_mm / start.h_mm;
            const signX = handle.includes("w") ? -1 : 1;
            const signY = handle.includes("n") ? -1 : 1;
            const byWidth = signX * dx;
            const byHeight = signY * dy * aspect;
            const grow = Math.abs(byWidth) >= Math.abs(byHeight) ? byWidth : byHeight;
            dx = signX * grow;
            dy = signY * (grow / aspect);
        }

        if (handle.includes("w")) { x = start.x_mm + dx; w = start.w_mm - dx; }
        if (handle.includes("e")) { w = start.w_mm + dx; }
        if (handle.includes("n")) { y = start.y_mm + dy; h = start.h_mm - dy; }
        if (handle.includes("s")) { h = start.h_mm + dy; }

        const smallest = isText ? FigureCanvas.MIN_TEXT_MM : FigureCanvas.MIN_SIZE_MM;
        if (w < smallest) { if (handle.includes("w")) x -= smallest - w; w = smallest; }
        if (h < smallest) { if (handle.includes("n")) y -= smallest - h; h = smallest; }
        if (!rotation) return { ...start, x_mm: x, y_mm: y, w_mm: w, h_mm: h };

        // The box is drawn from its top-left and then turned about its own
        // CENTRE, so a resize that moves the centre also swings the corner the
        // user is not touching. Put the centre where the rotated geometry says
        // it belongs, and the anchored corner stays under the pointer's
        // opposite number instead of sliding away as the box grows.
        const shift = FigureCanvas.turn(
            (x - start.x_mm) + (w - start.w_mm) / 2,
            (y - start.y_mm) + (h - start.h_mm) / 2, rotation);
        return { ...start,
                 x_mm: start.x_mm + (start.w_mm - w) / 2 + shift.x,
                 y_mm: start.y_mm + (start.h_mm - h) / 2 + shift.y,
                 w_mm: w, h_mm: h };
    }

    /** A vector turned clockwise by `degrees`, in page coordinates (y down). */
    static turn(x, y, degrees) {
        const radians = degrees * Math.PI / 180;
        const cos = Math.cos(radians);
        const sin = Math.sin(radians);
        return { x: x * cos - y * sin, y: x * sin + y * cos };
    }

    previewMarquee() {
        const { origin, current } = this.gesture;
        this.showMarquee({
            x: Math.min(origin.x, current.x), y: Math.min(origin.y, current.y),
            w: Math.abs(current.x - origin.x), h: Math.abs(current.y - origin.y),
        });
    }

    finishMarquee(gesture) {
        this.showMarquee(null);
        if (!gesture.moved) return;
        const box = {
            x: Math.min(gesture.origin.x, gesture.current.x),
            y: Math.min(gesture.origin.y, gesture.current.y),
            w: Math.abs(gesture.current.x - gesture.origin.x),
            h: Math.abs(gesture.current.y - gesture.origin.y),
        };
        // Intersecting rather than fully-enclosed: on a page where panels butt
        // up against each other, "fully enclosed" means a marquee has to be
        // drawn outside the page to catch the edge ones.
        // Intersecting rather than fully-enclosed: on a page where panels butt
        // up against each other, "fully enclosed" means a marquee has to be
        // drawn outside the page to catch the edge ones.
        const hits = FigureSchema.panelsOnPage(this.state.document, this.pageId)
            .filter((panel) => {
                const place = panel.placement;
                return place.x_mm < box.x + box.w && place.x_mm + place.w_mm > box.x
                    && place.y_mm < box.y + box.h && place.y_mm + place.h_mm > box.y;
            })
            .map((panel) => panel.panel_id);
        // Annotations are caught too. They were not, and a marquee that swept
        // over an arrow and left it behind made grouping an image with its
        // label impossible to do by dragging.
        const annotations = Object.values(this.state.document.annotations)
            .filter((annotation) => {
                if (annotation.page_id !== this.pageId) return false;
                const g = annotation.geometry;
                const left = g.x_mm + Math.min(0, g.w_mm);
                const top = g.y_mm + Math.min(0, g.h_mm);
                return left < box.x + box.w && left + Math.abs(g.w_mm) > box.x
                    && top < box.y + box.h && top + Math.abs(g.h_mm) > box.y;
            })
            .map((annotation) => annotation.annotation_id);
        this.select(hits.concat(annotations), false);
    }

    // -- drawing -----------------------------------------------------------

    /**
     * Arm a drawing tool, or go back to selecting.
     *
     * One-shot: `finishDraw` puts it back to "select" the moment something has
     * been placed. A mode that persisted would be a canvas where the next click
     * on a panel drew a rectangle on top of it, and the only clue would be a
     * pressed button in the rail 200 pixels away.
     */
    setTool(name) {
        this.tool = name && name !== "select" ? name : null;
        this.surfaceEl.classList.toggle("is-drawing", Boolean(this.tool));
    }

    /** Default size for a shape placed with a click rather than a drag, in mm. */
    static get DRAW_DEFAULT_MM() { return { w: 30, h: 18 }; }

    previewDraw(constrain) {
        const type = this.tool;
        const box = this.drawBox(constrain, this.gesture, type);
        if (!this.guideEl) return;
        // Into the guides layer rather than the surface: the surface is what
        // render() replaces, and the provisional shape is not part of the
        // document until the pointer comes up.
        if (type === "line" || type === "arrow") {
            this.guideEl.innerHTML = this.strokeMarkup({
                annotation_id: "__draft", type: type, z: 999,
                geometry: { ...box, rotation: 0 },
                style: this.drawStyle(),
            });
        } else {
            this.guideEl.innerHTML = `<span class="fb-draft fb-draft-${type}"
                style="left:${this.toPx(box.x_mm)}px;top:${this.toPx(box.y_mm)}px;
                       width:${this.toPx(box.w_mm)}px;height:${this.toPx(box.h_mm)}px"></span>`;
        }
    }

    /**
     * The geometry a draw gesture describes.
     *
     * Rectangles and ellipses are normalised -- dragging up and left gives a
     * box, not a negative one. Lines and arrows are NOT: their w/h is the
     * offset to the far end, and normalising it would point every arrow down
     * and to the right whichever way it was drawn.
     *
     * Both the gesture and the tool are ARGUMENTS rather than `this.gesture`
     * and `this.tool`, because the one caller that matters has already cleared
     * both: `pointerUp` nulls the gesture before it hands it on, and
     * `finishDraw` releases the tool before it asks for the geometry. Reading
     * the fields here threw on every shape drawn, and would have pointed every
     * arrow down-and-right if it had not.
     */
    drawBox(constrain, gesture, type) {
        const { origin, current } = gesture;
        let dx = current.x - origin.x;
        let dy = current.y - origin.y;
        if (constrain) {
            // Shift gives a square, or an axis-aligned line -- the two things
            // the modifier means in every drawing tool anyone has used.
            if (type === "line" || type === "arrow") {
                if (Math.abs(dx) >= Math.abs(dy)) dy = 0;
                else dx = 0;
            } else {
                const size = Math.max(Math.abs(dx), Math.abs(dy));
                dx = Math.sign(dx || 1) * size;
                dy = Math.sign(dy || 1) * size;
            }
        }
        if (type === "line" || type === "arrow") {
            return { x_mm: origin.x, y_mm: origin.y, w_mm: dx, h_mm: dy };
        }
        return {
            x_mm: Math.min(origin.x, origin.x + dx),
            y_mm: Math.min(origin.y, origin.y + dy),
            w_mm: Math.abs(dx),
            h_mm: Math.abs(dy),
        };
    }

    /**
     * The style a newly drawn annotation starts with.
     *
     * The FAMILY and the colour come from the document's own defaults, so a
     * figure set in Times does not place a Helvetica caption. The SIZE does
     * not, and that is the correction: this asked for `label_size_pt`, which is
     * the size of the letter "A" in the corner of a panel. A caption inherited
     * it and came out at 10 pt -- typeset for the inside of an image, while
     * sitting beside one. The document's `font_size_pt` would have been no
     * better: it is 8 pt, and it is the legend and scale-bar type, small for
     * the same reason.
     *
     * So a text box starts at a reading size of its own, and whoever wants
     * something else changes it in the sidebar -- which is where the number now
     * is, in a stepper, rather than nowhere.
     */
    drawStyle() {
        const style = this.state.document.settings.style;
        return {
            color: style.text_color || "#000000",
            fill: "",
            line_width_pt: 0.75,
            font_size_pt: FigureRichText.DEFAULT_SIZE_PT,
            font_family: FigureRichText.family(style.font_family),
            align: "left",
            valign: "top",
            autofit: true,
        };
    }

    /**
     * Commit whatever was drawn.
     *
     * A click with no drag still places something, at a default size: a tool
     * that silently did nothing unless the pointer travelled far enough is a
     * tool people press twice and then give up on.
     */
    finishDraw(gesture) {
        const type = this.tool;
        this.setTool(null);
        this.onToolFinished();
        this.clearGuides();
        if (!type || !this.pageId) return;

        let box = this.drawBox(false, gesture, type);
        if (!gesture.moved) {
            const size = FigureCanvas.DRAW_DEFAULT_MM;
            box = (type === "line" || type === "arrow")
                ? { x_mm: gesture.origin.x, y_mm: gesture.origin.y, w_mm: size.w, h_mm: 0 }
                : { x_mm: gesture.origin.x, y_mm: gesture.origin.y,
                    w_mm: size.w,
                    // One line at the default size, computed rather than the
                    // 8 mm literal this used to be: that was two lines of 8 pt
                    // type, and at 14 pt it is not quite one -- so the box a
                    // click placed was shorter than the text it was about to
                    // hold. `autofit` grows it as soon as there are words in
                    // it; this only has to be right for the empty one.
                    h_mm: type === "text"
                        ? FigureRichText.DEFAULT_SIZE_PT * FigureRichText.MM_PER_PT
                          * FigureRichText.LINE_HEIGHT
                        : size.h };
        }

        const annotation = {
            annotation_id: FigureSchema.newAnnotationId(),
            type: type,
            page_id: this.pageId,
            geometry: { ...box, rotation: 0 },
            text: "",
            // Written here rather than left for the server so that the local
            // draft is already in the shape the canvas draws from.
            ...(type === "text" ? { rich: FigureRichText.normalize("", null) } : {}),
            style: this.drawStyle(),
            z: this.nextAnnotationZ(),
        };
        this.state.commit(
            [{ op: "add_annotation", annotation: annotation }],
            (draft) => { draft.annotations[annotation.annotation_id] = annotation; });
        this.select([annotation.annotation_id], false);
        // A text box placed with nothing in it is invisible, so the editor opens
        // on it straight away -- placing text and typing it are one action.
        if (type === "text") this.onEditText(annotation.annotation_id);
    }

    nextAnnotationZ() {
        return Object.values(this.state.document.annotations)
            .filter((annotation) => annotation.page_id === this.pageId)
            .reduce((top, annotation) => Math.max(top, annotation.z), 0) + 1;
    }

    commitGesture(gesture) {
        const moves = [];
        const annotationOps = [];
        for (const item of gesture.items) {
            // The provisional box the preview computed, if there was one. Read
            // in preference to the DOM because a line's element is its
            // NORMALISED bounds plus padding -- measuring that back would turn
            // every arrow into a rectangle a few pixels bigger than itself.
            let box = item.box;
            if (!box) {
                const element = this.elementFor(item);
                if (!element) continue;
                box = {
                    x_mm: this.toMm(parseFloat(element.style.left)),
                    y_mm: this.toMm(parseFloat(element.style.top)),
                    w_mm: this.toMm(parseFloat(element.style.width) || this.toPx(item.start.w_mm)),
                    h_mm: this.toMm(parseFloat(element.style.height) || this.toPx(item.start.h_mm)),
                };
            }
            if (item.kind === "panel") {
                moves.push({ panel_id: item.id, placement: { ...item.start, ...box } });
                if (gesture.kind === "resize") {
                    // A linked row shares a box. Only on a resize: sharing a
                    // POSITION would mean dragging one panel dragged them all
                    // onto each other, and the row could never be a row.
                    moves.push(...this._linkedSizeMoves(this.state.panel(item.id), box));
                }
            } else if (gesture.kind === "resize" && this.clearsAutofit(item, gesture)) {
                // Dragging the top or bottom edge of a text box is the user
                // saying how tall it should be, so it stops following its
                // contents. The gesture IS the opt-out -- a checkbox they had
                // to find first would make the drag do nothing.
                annotationOps.push({
                    op: "update_annotation", annotation_id: item.id,
                    changes: { geometry: box, style: { autofit: false } },
                });
            } else if (gesture.kind === "rotate") {
                // Only the angle: a rotation must not also write back the box,
                // or a preview transform read off the DOM would be committed as
                // a resize.
                annotationOps.push({
                    op: "update_annotation", annotation_id: item.id,
                    changes: { geometry: { rotation: item.rotation || 0 } },
                });
            } else {
                annotationOps.push({
                    op: "update_annotation", annotation_id: item.id, changes: { geometry: box },
                });
            }
        }
        const operations = moves.length ? [{ op: "move_panels", moves: moves }] : [];
        operations.push(...annotationOps);
        if (!operations.length) return;

        // One commit for the whole gesture: dragging five selected panels is
        // one thing the user did and must be one thing they can undo.
        this.state.commit(operations, (draft) => {
            for (const move of moves) {
                draft.panels[move.panel_id].placement = move.placement;
            }
            for (const op of annotationOps) {
                Object.assign(draft.annotations[op.annotation_id].geometry, op.changes.geometry);
            }
        });
    }

    elementFor(item) {
        const selector = item.kind === "panel"
            ? `.fb-panel[data-panel-id="${item.id}"]`
            : `.fb-annotation[data-annotation-id="${item.id}"]`;
        return this.surfaceEl.querySelector(selector);
    }

    // -- snapping --------------------------------------------------------

    /**
     * Nudge a move onto a nearby edge, centre or margin.
     *
     * The threshold is in screen pixels: a fixed millimetre threshold is
     * unusably sticky zoomed in and does nothing at all zoomed out.
     */
    snapMove(dx, dy) {
        const tolerance = this.toMm(FigureCanvas.SNAP_PIXELS);
        const targets = this.snapTargets();
        // Annotations both snap and are snapped to. They were neither, so a
        // caption could not be lined up with the panel above it by dragging --
        // which is the one alignment on a figure page that always matters.
        const moving = this.gesture.items;
        if (!moving.length) return { dx, dy };

        let bestX = { distance: tolerance, delta: dx, line: null };
        let bestY = { distance: tolerance, delta: dy, line: null };

        for (const item of moving) {
            const edgesX = [item.start.x_mm + dx,
                            item.start.x_mm + dx + item.start.w_mm / 2,
                            item.start.x_mm + dx + item.start.w_mm];
            const edgesY = [item.start.y_mm + dy,
                            item.start.y_mm + dy + item.start.h_mm / 2,
                            item.start.y_mm + dy + item.start.h_mm];
            for (const edge of edgesX) {
                for (const line of targets.x) {
                    const distance = Math.abs(edge - line);
                    if (distance < bestX.distance) {
                        bestX = { distance: distance, delta: dx + (line - edge), line: line };
                    }
                }
            }
            for (const edge of edgesY) {
                for (const line of targets.y) {
                    const distance = Math.abs(edge - line);
                    if (distance < bestY.distance) {
                        bestY = { distance: distance, delta: dy + (line - edge), line: line };
                    }
                }
            }
        }
        this.showGuides(bestX.line, bestY.line);
        return { dx: bestX.delta, dy: bestY.delta };
    }

    /**
     * Every line a move may snap onto.
     *
     * The page's own lines and every other panel's three are always here --
     * that is what "smart guides" means, and turning it off in the View menu
     * empties this rather than changing the signature, because `snapMove` is
     * pinned by a probe and the point of it is the arithmetic, not the source
     * of the candidates.
     */
    snapTargets() {
        const page = this.page;
        const moving = new Set(this.gesture.items.map((item) => item.id));
        const x = [];
        const y = [];

        if (this.snapping.guides) {
            x.push(0, page.size_mm.w / 2, page.size_mm.w,
                   page.margins_mm.left, page.size_mm.w - page.margins_mm.right);
            y.push(0, page.size_mm.h / 2, page.size_mm.h,
                   page.margins_mm.top, page.size_mm.h - page.margins_mm.bottom);
            for (const panel of FigureSchema.panelsOnPage(this.state.document, this.pageId)) {
                if (moving.has(panel.panel_id)) continue;
                const place = panel.placement;
                x.push(place.x_mm, place.x_mm + place.w_mm / 2, place.x_mm + place.w_mm);
                y.push(place.y_mm, place.y_mm + place.h_mm / 2, place.y_mm + place.h_mm);
            }
            for (const annotation of Object.values(this.state.document.annotations)) {
                if (annotation.page_id !== this.pageId) continue;
                if (moving.has(annotation.annotation_id)) continue;
                // Normalised, because a line's w/h are legally negative: they
                // are an offset from its start point, not a box.
                const g = annotation.geometry;
                const left = g.x_mm + Math.min(0, g.w_mm);
                const top = g.y_mm + Math.min(0, g.h_mm);
                const width = Math.abs(g.w_mm);
                const height = Math.abs(g.h_mm);
                x.push(left, left + width / 2, left + width);
                y.push(top, top + height / 2, top + height);
            }
        }
        if (this.snapping.grid && this.snapping.gridMm > 0) {
            const step = this.snapping.gridMm;
            for (let value = 0; value <= page.size_mm.w + 1e-6; value += step) x.push(value);
            for (let value = 0; value <= page.size_mm.h + 1e-6; value += step) y.push(value);
        }
        return { x: x, y: y };
    }

    showGuides(lineX, lineY) {
        if (!this.guideEl) return;
        const parts = [];
        if (lineX !== null && lineX !== undefined) {
            parts.push(`<span class="fb-guide fb-guide-v" style="left:${this.toPx(lineX)}px"></span>`);
        }
        if (lineY !== null && lineY !== undefined) {
            parts.push(`<span class="fb-guide fb-guide-h" style="top:${this.toPx(lineY)}px"></span>`);
        }
        this.guideEl.innerHTML = parts.join("");
    }

    showMarquee(box) {
        if (!this.guideEl) return;
        this.guideEl.innerHTML = box
            ? `<span class="fb-marquee" style="left:${this.toPx(box.x)}px;top:${this.toPx(box.y)}px;
                   width:${this.toPx(box.w)}px;height:${this.toPx(box.h)}px"></span>`
            : "";
    }

    clearGuides() {
        if (this.guideEl) this.guideEl.innerHTML = "";
    }

    // -- keyboard --------------------------------------------------------

    keyDown(event) {
        // A <dialog> traps focus, not keystrokes: with the delete confirmation
        // up and its Cancel button focused, pressing Delete again arrived here
        // and asked the same question a second time. The tag guard below does
        // not catch it, because a BUTTON is not one of the tags it names.
        if (FigureConfirm.modalOpen) return;
        // `isContentEditable` is checked too: the text editor is a DIV, not a
        // TEXTAREA, so the tag list alone would let every canvas shortcut fire
        // while somebody is typing a caption.
        const active = document.activeElement;
        const typing = active
            && (["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)
                || active.isContentEditable);
        if (typing) return;

        // The standard chords, bound here because this is what owns the
        // selection they act on. Undo and redo are the workspace's -- they are
        // the document's, not the page's.
        if (event.metaKey || event.ctrlKey) {
            // Z-order, keyed off `event.code` rather than `event.key`: with
            // Shift held, the bracket keys report "{" and "}" on a US layout
            // and something else again on most others, so the physical key is
            // the only stable name for them.
            const bracket = { BracketRight: "up", BracketLeft: "down" }[event.code];
            if (bracket && this.selection.size) {
                event.preventDefault();
                this.reorderZ(bracket === "up"
                    ? (event.shiftKey ? "front" : "forward")
                    : (event.shiftKey ? "back" : "backward"));
                return;
            }
            const chord = {
                c: () => this.copySelection(),
                v: () => this.paste(null),
                d: () => this.duplicateSelection(),
                a: () => this.selectAllOnPage(),
                g: () => (event.shiftKey ? this.ungroupSelection() : this.groupSelection()),
            }[event.key.toLowerCase()];
            if (!chord) return;
            // Cmd+A on an empty page is still "select everything", so the
            // empty-selection guard comes after the chords that do not need one.
            if (event.key.toLowerCase() !== "a" && event.key.toLowerCase() !== "v"
                    && !this.selection.size) {
                return;
            }
            event.preventDefault();
            chord();
            return;
        }
        if (!this.selection.size) return;

        if (event.key === "Delete" || event.key === "Backspace") {
            event.preventDefault();
            this.removeSelection();
            return;
        }
        const step = event.shiftKey ? FigureCanvas.NUDGE_COARSE_MM : FigureCanvas.NUDGE_MM;
        const deltas = {
            ArrowLeft: [-step, 0], ArrowRight: [step, 0],
            ArrowUp: [0, -step], ArrowDown: [0, step],
        }[event.key];
        if (!deltas) return;
        event.preventDefault();
        this.nudge(deltas[0], deltas[1]);
    }

    /**
     * Move the selection by a step.
     *
     * Annotations move too. They did not, and the filter to panels made the
     * arrow keys silently inert on a caption -- the commonest thing on a page
     * to want a half-millimetre nudge on, and the one object that had no way to
     * get one.
     */
    nudge(dx, dy) {
        const moves = this.selectedPanels()
            .filter((panel) => panel.placement)
            .map((panel) => ({
                panel_id: panel.panel_id,
                placement: { ...panel.placement,
                             x_mm: panel.placement.x_mm + dx,
                             y_mm: panel.placement.y_mm + dy },
            }));
        const shifts = this.selectedAnnotations().map((annotation) => ({
            op: "update_annotation", annotation_id: annotation.annotation_id,
            changes: { geometry: { x_mm: annotation.geometry.x_mm + dx,
                                   y_mm: annotation.geometry.y_mm + dy } },
        }));
        if (!moves.length && !shifts.length) return;
        // One batch, so a nudge of an image and its caption is one undo step
        // rather than two.
        const ops = moves.length ? [{ op: "move_panels", moves: moves }] : [];
        this.state.commit(ops.concat(shifts), (draft) => {
            for (const move of moves) draft.panels[move.panel_id].placement = move.placement;
            for (const shift of shifts) {
                Object.assign(draft.annotations[shift.annotation_id].geometry,
                              shift.changes.geometry);
            }
        });
    }

    /**
     * Delete what is selected.
     *
     * A panel goes back to the TRAY rather than being destroyed: the captured
     * scene may be the only record of a view somebody spent an hour finding,
     * and Delete on a layout is a statement about the layout. Removing it for
     * good is done from the tray, where the thing being destroyed is what is
     * under the pointer.
     */
    removeSelection() {
        const panels = this.selectedPanels().filter((panel) => panel.placement);
        const annotationIds = Array.from(this.selection)
            .filter((id) => this.state.document.annotations[id]);

        const operations = [];
        if (panels.length) {
            operations.push({
                op: "move_panels",
                moves: panels.map((panel) => ({ panel_id: panel.panel_id, placement: null })),
            });
        }
        if (annotationIds.length) {
            operations.push({ op: "remove_annotations", annotation_ids: annotationIds });
        }
        if (!operations.length) return;

        this.selection.clear();
        this.state.commit(operations, (draft) => {
            for (const panel of panels) draft.panels[panel.panel_id].placement = null;
            for (const id of annotationIds) delete draft.annotations[id];
        });
        this.onSelectionChange([]);
    }
    // -- duplicating, copying, grouping, stacking --------------------------

    /**
     * Copy the selection, offset a little, in ONE commit.
     *
     * The scene is deep-copied and the render revision is carried across
     * unchanged, which is what makes the copy show a picture immediately: its
     * preview is the original's bytes, uploaded under the new panel's id. A
     * copy whose preview had to be re-rendered would arrive as an empty frame
     * and stay that way until the user reopened it in the viewer.
     */
    duplicateSelection() {
        const made = this.copiesOf(this.selectedPanels(), this.selectedAnnotations(),
                                  FigureCanvas.PASTE_OFFSET_MM, FigureCanvas.PASTE_OFFSET_MM);
        this.commitCopies(made);
    }

    copySelection() {
        FigureClipboard.put(this.selectedPanels(), this.selectedAnnotations());
    }

    /** Paste at a point, or offset from where the originals were. */
    paste(point) {
        const held = FigureClipboard.take();
        if (!held.panels.length && !held.annotations.length) return;

        let dx = FigureCanvas.PASTE_OFFSET_MM;
        let dy = FigureCanvas.PASTE_OFFSET_MM;
        if (point) {
            const boxes = held.panels.map((panel) => panel.placement)
                .concat(held.annotations.map((annotation) => annotation.geometry))
                .filter(Boolean);
            if (boxes.length) {
                dx = point.x - Math.min(...boxes.map((box) => box.x_mm));
                dy = point.y - Math.min(...boxes.map((box) => box.y_mm));
            }
        }
        this.commitCopies(this.copiesOf(held.panels, held.annotations, dx, dy));
    }

    /**
     * New panels and annotations, offset, ready to commit.
     *
     * `pageId` is where the copies land, defaulting to the page being looked
     * at; page duplication passes the new page. A panel with no placement (one
     * sitting in the tray) keeps none -- duplicating a tray panel gives another
     * tray panel, not one that jumps onto whatever page happened to be open.
     */
    copiesOf(panels, annotations, dx, dy, pageId) {
        const page = pageId || this.pageId;
        const madePanels = panels.map((panel) => {
            const copy = JSON.parse(JSON.stringify(panel));
            copy.panel_id = FigureSchema.newPanelId();
            // Never a member of the original's link group: a duplicate is a new
            // panel that happens to look the same, and joining the row would
            // make resizing the original resize it too.
            copy.link_group = null;
            copy.derived_from = { panel_id: panel.panel_id, operation: "duplicate", layer: "" };
            if (copy.placement) {
                copy.placement = { ...copy.placement, page_id: page,
                                   x_mm: copy.placement.x_mm + dx,
                                   y_mm: copy.placement.y_mm + dy };
            }
            return { copy: copy, from: panel };
        });
        const madeAnnotations = annotations.map((annotation) => {
            const copy = JSON.parse(JSON.stringify(annotation));
            copy.annotation_id = FigureSchema.newAnnotationId();
            copy.page_id = page;
            copy.geometry = { ...copy.geometry,
                              x_mm: copy.geometry.x_mm + dx, y_mm: copy.geometry.y_mm + dy };
            return copy;
        });
        return { panels: madePanels, annotations: madeAnnotations };
    }

    /**
     * Store a set of copies and give each one the original's preview.
     *
     * `options.select` is false when the copies are not on the page being
     * looked at -- duplicating a tray panel, or a whole page -- because
     * selecting something the user cannot see puts a context bar on screen
     * pointing at nothing. `options.operations` lets a caller put the copies
     * inside a larger batch, which is how page duplication stays one undo step.
     */
    commitCopies(made, options) {
        const flags = options || {};
        if (!made.panels.length && !made.annotations.length) return Promise.resolve(false);
        let z = this.nextZ();
        for (const entry of made.panels) {
            if (entry.copy.placement && flags.select !== false) entry.copy.placement.z = z++;
        }
        const operations = (flags.operations || []).slice();
        operations.push(...made.panels.map((entry) => ({ op: "add_panel", panel: entry.copy })));
        operations.push(...made.annotations.map((a) => ({ op: "add_annotation", annotation: a })));

        const stored = this.state.commit(operations, (draft) => {
            if (typeof flags.mutate === "function") flags.mutate(draft);
            for (const entry of made.panels) draft.panels[entry.copy.panel_id] = entry.copy;
            for (const a of made.annotations) draft.annotations[a.annotation_id] = a;
        }).then((ok) => {
            if (ok) this.copyPreviews(made.panels);
            return ok;
        });

        if (flags.select !== false) {
            this.select(made.panels.map((entry) => entry.copy.panel_id)
                .concat(made.annotations.map((a) => a.annotation_id)), false);
        }
        return stored;
    }

    /**
     * Give each copy the original's preview bytes.
     *
     * After the document commit, not before: a preview uploaded for a panel the
     * server has not accepted yet is a file with no owner. Failures are silent
     * on purpose -- the copy is already real, and an empty frame is a smaller
     * problem than an error about a raster.
     */
    async copyPreviews(entries) {
        for (const entry of entries) {
            const source = this.state.source(entry.from.source_id);
            // An imported asset is drawn straight from the figure's own
            // directory, so the copy already has its picture.
            if (source && source.kind === "imported_asset") continue;
            try {
                const response = await fetch(this.api.previewUrl(
                    this.figureId, entry.from.panel_id, entry.from.render_revision));
                if (!response.ok) continue;
                const blob = await response.blob();
                await this.api.putPreview(this.figureId, entry.copy.panel_id,
                                          entry.copy.render_revision, blob, {});
            } catch (error) {
                /* see the docstring */
            }
        }
        this.render();
    }

    groupSelection() {
        const ids = Array.from(this.selection);
        if (ids.length < 2) return;
        // Already grouped members are dissolved into the new group rather than
        // refused: selecting a group plus one more thing and pressing Cmd+G
        // plainly means "all of these".
        const dissolve = new Set();
        for (const id of ids) {
            const group = this.groupFor(id);
            if (group) dissolve.add(group.group_id);
        }
        const groupId = FigureSchema.newGroupId();
        const group = { group_id: groupId, member_ids: ids };
        const operations = Array.from(dissolve)
            .map((id) => ({ op: "ungroup_items", group_id: id }));
        operations.push({ op: "group_items", group: group });

        this.state.commit(operations, (draft) => {
            for (const id of dissolve) delete draft.groups[id];
            draft.groups[groupId] = group;
        });
    }

    ungroupSelection() {
        const ids = new Set();
        for (const id of this.selection) {
            const group = this.groupFor(id);
            if (group) ids.add(group.group_id);
        }
        if (!ids.size) return;
        this.state.commit(
            Array.from(ids).map((id) => ({ op: "ungroup_items", group_id: id })),
            (draft) => { for (const id of ids) delete draft.groups[id]; });
    }

    /**
     * Move the selection through the z-order.
     *
     * Four commands, not two. "To the front" and "one place forward" are
     * different intents, and a stack of overlapping panels is exactly where the
     * second is the only one that gets you where you meant to go -- with only
     * the two absolute commands, putting a panel between two others meant
     * fronting it and then fronting each of the ones that had to end up over it.
     *
     * Written as absolute z values rather than as +1/-1, because "bring to
     * front" past a panel already at the front has to be a no-op rather than a
     * number that climbs for ever.
     *
     * Panels and annotations are reordered in separate stacks. Annotations draw
     * at `1000 + z` and so are always above every panel; "bring to front" on a
     * caption means the front of the captions, and always did.
     */
    reorderZ(command) {
        const annotations = this.selectedAnnotations();
        if (annotations.length) this.reorderAnnotationZ(command, annotations);
        const selected = this.selectedPanels().filter((panel) => panel.placement);
        if (!selected.length) return;

        const chosen = new Set(selected.map((panel) => panel.panel_id));
        // Sorted by z here, not taken in `panelsOnPage`'s order: that one sorts
        // into READING order for numbering (A B C / D E F), and renumbering z
        // from it would rewrite the stacking of every panel the user did not
        // touch. One press of "Bring to front" would reshuffle the whole page.
        const ordered = FigureCanvas.reordered(
            FigureSchema.panelsOnPage(this.state.document, this.pageId)
                .slice().sort((a, b) => a.placement.z - b.placement.z),
            chosen, command, (panel) => panel.panel_id);
        if (!ordered) return;

        const moves = ordered.map((panel, index) => ({
            panel_id: panel.panel_id,
            placement: { ...panel.placement, z: index },
        }));
        this.state.commit([{ op: "move_panels", moves: moves }], (draft) => {
            for (const move of moves) draft.panels[move.panel_id].placement = move.placement;
        });
    }

    /**
     * Reorder annotations among themselves.
     *
     * They can be ordered against EACH OTHER at all only since the offset above
     * was separated from the stack below it -- before that, "Bring to front" on
     * a text box was a menu item that did nothing, in two different menus.
     */
    reorderAnnotationZ(command, selected) {
        const chosen = new Set(selected.map((annotation) => annotation.annotation_id));
        const ordered = FigureCanvas.reordered(
            Object.values(this.state.document.annotations)
                .filter((annotation) => annotation.page_id === this.pageId)
                .sort((a, b) => a.z - b.z),
            chosen, command, (annotation) => annotation.annotation_id);
        if (!ordered) return;

        const ops = ordered.map((annotation, index) => ({
            op: "update_annotation", annotation_id: annotation.annotation_id,
            changes: { z: index },
        }));
        this.state.commit(ops, (draft) => {
            ops.forEach((op) => { draft.annotations[op.annotation_id].z = op.changes.z; });
        });
    }

    /**
     * `ordered` with the chosen members moved as `command` asks, or null when
     * there is nothing for the command to do.
     *
     * `ordered` runs back to front. Pure and static, which is the point: this
     * is the only part of z-ordering that is arithmetic rather than DOM, and it
     * is the part that is easy to get subtly wrong.
     *
     * A multiple selection moves as a BLOCK. Stepping each member on its own
     * reverses a pair of adjacent objects on the second press -- the classic
     * bug in every "send backward" written as a loop over the selection -- and
     * it also lets a selection come apart across a gap it should have jumped.
     */
    static reordered(ordered, chosen, command, idOf) {
        const picked = ordered.filter((item) => chosen.has(idOf(item)));
        const rest = ordered.filter((item) => !chosen.has(idOf(item)));
        if (!picked.length || !rest.length) return null;
        if (command === "front") return rest.concat(picked);
        if (command === "back") return picked.concat(rest);

        const forward = command === "forward";
        // The edge of the block on the side it is moving towards, gaps in the
        // selection included -- so a split selection jumps whatever sits
        // between its two halves rather than closing up around it.
        const edge = forward
            ? ordered.length - 1 - [...ordered].reverse()
                .findIndex((item) => chosen.has(idOf(item)))
            : ordered.findIndex((item) => chosen.has(idOf(item)));
        const neighbour = ordered[edge + (forward ? 1 : -1)];
        if (!neighbour) return null;

        const out = rest.slice();
        out.splice(out.indexOf(neighbour) + (forward ? 1 : 0), 0, ...picked);
        return out;
    }

    // -- layout commands -------------------------------------------------

    /**
     * Align, distribute or equalise the selection.
     *
     * Everything compiles to ONE commit, so each command is one undo step --
     * the same rule the drag follows, for the same reason.
     *
     * ANNOTATIONS TOO. This read `selectedPanels()` and nothing else, so
     * selecting two captions and pressing Align left ran the whole arithmetic
     * over an empty list and returned without touching either of them -- a menu
     * with six live rows in it, none of which did anything. It is the same bug
     * `reorderZ` and `nudge` had, in the last place it was left.
     *
     * A caption's rectangle is its `geometry` and a panel's is its `placement`;
     * they hold the same four numbers under the same names, which is what makes
     * one list of boxes possible at all. What differs is the fifth key -- a
     * panel carries `z` and an annotation carries `rotation` -- so each box is
     * spread from its own object and handed back to it whole.
     *
     * Lines and arrows are left out, by `arrangeItems` rather than here. See
     * FigureSelection.describe: their `w_mm`/`h_mm` are a vector, and "same
     * width" on one would reverse its direction rather than resize it.
     */
    arrange(command) {
        const items = this.arrangeItems();
        if (items.length < 2) return;
        const boxes = items.map((item) => ({ ...item.box }));

        const left = Math.min(...boxes.map((b) => b.x_mm));
        const right = Math.max(...boxes.map((b) => b.x_mm + b.w_mm));
        const top = Math.min(...boxes.map((b) => b.y_mm));
        const bottom = Math.max(...boxes.map((b) => b.y_mm + b.h_mm));

        if (command === "left") boxes.forEach((b) => { b.x_mm = left; });
        else if (command === "right") boxes.forEach((b) => { b.x_mm = right - b.w_mm; });
        else if (command === "center") boxes.forEach((b) => { b.x_mm = (left + right - b.w_mm) / 2; });
        else if (command === "top") boxes.forEach((b) => { b.y_mm = top; });
        else if (command === "bottom") boxes.forEach((b) => { b.y_mm = bottom - b.h_mm; });
        else if (command === "middle") boxes.forEach((b) => { b.y_mm = (top + bottom - b.h_mm) / 2; });
        else if (command === "same_width") boxes.forEach((b) => { b.w_mm = boxes[0].w_mm; });
        else if (command === "same_height") boxes.forEach((b) => { b.h_mm = boxes[0].h_mm; });
        else if (command === "same_size") {
            boxes.forEach((b) => { b.w_mm = boxes[0].w_mm; b.h_mm = boxes[0].h_mm; });
        } else if (command === "distribute_h") this.distribute(boxes, "x_mm", "w_mm", left, right);
        else if (command === "distribute_v") this.distribute(boxes, "y_mm", "h_mm", top, bottom);
        else if (command === "row") this.pack(boxes, "row");
        else if (command === "column") this.pack(boxes, "column");
        else if (command === "grid") this.pack(boxes, "grid");
        else return;

        this.commitBoxes(items, boxes);
    }

    /**
     * The selection as one list of rectangles, whatever kind each object is.
     *
     * In selection order rather than in page order, because `same_width` and
     * friends take their answer from `boxes[0]` -- so "the first one I picked"
     * is the one the others match, which is the only reading of it a user can
     * predict.
     */
    arrangeItems() {
        const items = [];
        for (const id of this.selection) {
            const panel = this.state.panel(id);
            if (panel && panel.placement) {
                items.push({ kind: "panel", id: id, box: { ...panel.placement } });
                continue;
            }
            const annotation = this.state.document.annotations[id];
            if (annotation && !["line", "arrow"].includes(annotation.type)) {
                items.push({ kind: "annotation", id: id,
                             box: { ...annotation.geometry } });
            }
        }
        return items;
    }

    /** Equal GAPS, not equal centres. Equal centres leaves visibly uneven space
     *  the moment the panels are not all the same size, which for a figure of
     *  mixed crops is most of the time. */
    distribute(boxes, axis, size, low, high) {
        const ordered = boxes.slice().sort((a, b) => a[axis] - b[axis]);
        const total = ordered.reduce((sum, box) => sum + box[size], 0);
        const gap = (high - low - total) / (ordered.length - 1);
        let cursor = low;
        for (const box of ordered) {
            box[axis] = cursor;
            cursor += box[size] + gap;
        }
    }

    /** Row, column or smart grid, inside the bounding box of the selection and
     *  using the document's gutter. The result is ordinary geometry the user can
     *  then drag -- an arrangement, not a layout mode that has to be maintained. */
    pack(boxes, shape) {
        const gutter = this.state.document.settings.style.gutter_mm;
        const left = Math.min(...boxes.map((b) => b.x_mm));
        const top = Math.min(...boxes.map((b) => b.y_mm));
        const columns = shape === "row" ? boxes.length
            : shape === "column" ? 1
            : Math.max(1, Math.round(Math.sqrt(boxes.length)));

        const width = Math.max(...boxes.map((b) => b.w_mm));
        const height = Math.max(...boxes.map((b) => b.h_mm));
        boxes.forEach((box, index) => {
            box.x_mm = left + (index % columns) * (width + gutter);
            box.y_mm = top + Math.floor(index / columns) * (height + gutter);
            box.w_mm = width;
            box.h_mm = height;
        });
    }

    /**
     * Put the arranged boxes back, panels and annotations together, as ONE
     * commit.
     *
     * One commit and not two: aligning a caption to a panel is a single thing
     * the user did, and two commits would be two presses of Ctrl+Z to undo it,
     * with the figure sitting in a half-aligned state in between. The operation
     * vocabulary already batches panels; the annotations ride along in the same
     * list, which is what `splitComposite` does with panels and links.
     */
    commitBoxes(items, boxes) {
        const moves = [];
        const operations = [];
        items.forEach((item, index) => {
            if (item.kind === "panel") {
                moves.push({ panel_id: item.id, placement: boxes[index] });
                return;
            }
            operations.push({ op: "update_annotation", annotation_id: item.id,
                              changes: { geometry: boxes[index] } });
        });
        if (moves.length) operations.unshift({ op: "move_panels", moves: moves });
        if (!operations.length) return;

        this.state.commit(operations, (draft) => {
            for (const move of moves) draft.panels[move.panel_id].placement = move.placement;
            for (const operation of operations) {
                if (operation.op !== "update_annotation") continue;
                draft.annotations[operation.annotation_id].geometry =
                    operation.changes.geometry;
            }
        });
    }
    // -- split composite -------------------------------------------------

    /**
     * Turn one composite panel into a row of single-channel panels.
     *
     * The move this whole plugin is worth building for. Making the same figure
     * by hand is: find the field again, turn off every channel but one,
     * screenshot, repeat, then line five images up and hope they are the same
     * crop. Here the crop is not hoped for -- every derived panel carries the
     * SAME viewport, because it is copied rather than re-found.
     *
     * `mode` is "with_composite" (the original stays, first) or "channels_only".
     *
     * Everything arrives in ONE commit: N panels, their layout, and the link
     * between them. That is what makes a five-channel split one Ctrl+Z rather
     * than five, and it is why the operation vocabulary has batch forms at all.
     */
    splitComposite(panelId, mode) {
        const panel = this.state.panel(panelId);
        if (!panel || !panel.placement) return null;
        const channels = (panel.scene.channels || []).filter((c) => c.visible !== false);
        if (channels.length < 2) return null;

        const gutter = this.state.document.settings.style.gutter_mm;
        const place = panel.placement;
        const page = this.page;
        const keepComposite = mode !== "channels_only";

        const derived = channels.map((channel) => ({
            panel_id: FigureSchema.newPanelId(),
            source_id: panel.source_id,
            scene: {
                ...JSON.parse(JSON.stringify(panel.scene)),
                // One channel each. The windows, the colours and the region stay
                // the composite's -- a split that re-auto-levelled each channel
                // would produce a row nobody could compare.
                channels: [JSON.parse(JSON.stringify(channel))],
                captured_at: new Date().toISOString(),
            },
            placement: null,
            // Named from the channel, because that is what the panel now shows
            // and typing five titles is the tax this feature exists to remove.
            title: channel.fullname_at_capture || channel.key,
            label: { text: "", auto: true, visible: panel.label.visible },
            scalebar: { visible: false, target_um: panel.scalebar.target_um },
            legend: { channels: false, plugins: false },
            render_revision: 1,
            derived_from: { panel_id: panelId, operation: "split_channel",
                            layer: channel.key },
        }));

        const row = keepComposite
            ? [panelId, ...derived.map((entry) => entry.panel_id)]
            : derived.map((entry) => entry.panel_id);
        const placements = this._rowPlacements(row, place, gutter, page);
        const groupId = FigureSchema.newGroupId();

        const operations = derived.map((entry) => ({ op: "add_panel", panel: entry }));
        operations.push({
            op: "move_panels",
            moves: row.map((id) => ({ panel_id: id, placement: placements[id] })),
        });
        if (!keepComposite) {
            // Removing the original is part of the same action, so it rides in
            // the same batch -- and therefore in the same undo step.
            operations.push({ op: "remove_panels", panel_ids: [panelId] });
        }
        operations.push({
            op: "link_panels",
            group: {
                group_id: groupId, panel_ids: row,
                // The crop and the box, not the channels: a split row shares a
                // field of view and emphatically does not share what is drawn
                // in it, which is the entire point of it.
                sync: ["viewport", "size"],
            },
        });

        this.state.commit(operations, (draft) => {
            for (const entry of derived) draft.panels[entry.panel_id] = entry;
            for (const id of row) {
                if (draft.panels[id]) draft.panels[id].placement = placements[id];
            }
            if (!keepComposite) delete draft.panels[panelId];
            draft.link_groups[groupId] = {
                group_id: groupId, panel_ids: row.slice(), sync: ["viewport", "size"],
            };
            for (const id of row) {
                if (draft.panels[id]) draft.panels[id].link_group = groupId;
            }
        });
        this.select(derived.map((entry) => entry.panel_id), false);
        return groupId;
    }

    /**
     * Lay a set of panels out in a row from where the original sat, wrapping
     * onto further rows when the page runs out.
     *
     * Wrapping rather than shrinking: a row of nine channels squeezed to an A4
     * width is nine panels too small to read, and the user can always drag them
     * afterwards. The result is ordinary geometry, not a layout mode that has
     * to be maintained.
     */
    _rowPlacements(ids, origin, gutter, page) {
        const available = page ? page.size_mm.w - origin.x_mm : Infinity;
        const perRow = Math.max(1, Math.floor((available + gutter) / (origin.w_mm + gutter)));
        const out = {};
        ids.forEach((id, index) => {
            out[id] = {
                ...origin,
                x_mm: origin.x_mm + (index % perRow) * (origin.w_mm + gutter),
                y_mm: origin.y_mm + Math.floor(index / perRow) * (origin.h_mm + gutter),
                z: origin.z + index,
            };
        });
        return out;
    }

    /**
     * The extra moves a resize owes to everyone linked to this panel.
     *
     * Only on a RESIZE, never on a move. Sharing a size is what keeps a split
     * row comparable; sharing a POSITION would mean the row could never be a
     * row, because dragging one panel would drag them all onto each other.
     */
    _linkedSizeMoves(panel, box) {
        const groupId = panel.link_group;
        const group = groupId && this.state.document.link_groups[groupId];
        if (!group || !group.sync.includes("size")) return [];
        return group.panel_ids
            .filter((id) => id !== panel.panel_id)
            .map((id) => this.state.panel(id))
            .filter((other) => other && other.placement)
            .map((other) => ({
                panel_id: other.panel_id,
                placement: { ...other.placement, w_mm: box.w_mm, h_mm: box.h_mm },
            }));
    }



    // -- the tray --------------------------------------------------------

    /**
     * Panels dragged in from the tray -- one, or a whole multiple selection.
     *
     * Sized from the region each one shows rather than to a fixed box, so a
     * wide field arrives wide: landing every panel as a square and making the
     * user fix the aspect ratio afterwards is squashed tissue waiting to be
     * exported.
     *
     * Several at once are laid out from the drop point rather than dropped on
     * top of each other. A pile at one coordinate looks like ONE panel, and the
     * others are found only by dragging the top one off -- which is a way to
     * lose work that looks exactly like a bug.
     */
    dropFromTray(event) {
        const panelIds = this.readTrayPayload(event.dataTransfer);
        if (!panelIds.length) return;
        event.preventDefault();
        const page = this.page;
        if (!page) return;

        const point = this.surfacePoint(event);
        const sizes = this.traySizes(panelIds, page);
        // The first panel lands centred under the pointer, which is where the
        // user aimed; the rest flow from there.
        const origin = {
            x_mm: Math.max(page.margins_mm.left, point.x - (sizes[0]?.w_mm || 0) / 2),
            y_mm: Math.max(page.margins_mm.top, point.y - (sizes[0]?.h_mm || 0) / 2),
        };
        this.placePanels(panelIds, sizes, origin);
    }

    /** The dragged panel ids. A JSON array is what the tray writes now; the
     *  bare id is still read, because a drag started before a reload would
     *  otherwise arrive as an unexplained no-op. */
    readTrayPayload(transfer) {
        const raw = transfer?.getData("text/x-plexora-panel");
        if (!raw) return [];
        try {
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
        } catch (error) {
            return [raw];
        }
    }

    /** The size each tray panel wants, from the shape of the region it shows. */
    traySizes(panelIds, page) {
        const width = Math.min(60, page.size_mm.w / 3);
        return panelIds.map((panelId) => {
            const panel = this.state.panel(panelId);
            const viewport = panel?.scene?.viewport;
            const aspect = (viewport && viewport.w) ? viewport.h / viewport.w : 1;
            return { w_mm: width, h_mm: width * (aspect || 1) };
        });
    }

    /**
     * Put a batch of tray panels onto the page, in ONE commit.
     *
     * One commit because placing four panels is one thing the user did: four
     * would be four undo steps and four saves for a single drag.
     */
    placePanels(panelIds, sizes, origin) {
        const page = this.page;
        if (!page || !panelIds.length) return;
        const boxes = FigureCanvas.freePlacements(
            sizes, page, this.occupiedBoxes(),
            this.state.document.settings.style.gutter_mm, origin);

        let z = this.nextZ();
        const moves = panelIds.map((panelId, index) => ({
            panel_id: panelId,
            placement: { page_id: this.pageId, ...boxes[index], z: z++ },
        })).filter((move) => this.state.panel(move.panel_id));
        if (!moves.length) return;

        this.state.commit([{ op: "move_panels", moves: moves }], (draft) => {
            for (const move of moves) draft.panels[move.panel_id].placement = move.placement;
        });
        this.select(moves.map((move) => move.panel_id), false);
    }

    /** Everything already standing on this page, as plain boxes. */
    occupiedBoxes() {
        return FigureSchema.panelsOnPage(this.state.document, this.pageId)
            .map((panel) => ({ ...panel.placement }));
    }

    static overlaps(a, b) {
        return a.x_mm < b.x_mm + b.w_mm && a.x_mm + a.w_mm > b.x_mm
            && a.y_mm < b.y_mm + b.h_mm && a.y_mm + a.h_mm > b.y_mm;
    }

    /**
     * Where a batch of new boxes should go on a page that already has things
     * on it.
     *
     * A suggestion, not a template: the result is ordinary geometry the user
     * drags immediately afterwards. What it guarantees is only the thing that
     * cannot be recovered from by dragging -- that nothing lands exactly on top
     * of something else, which hides it.
     *
     * Left to right from the origin, wrapping down a row when the right margin
     * is reached and stepping past anything already in the way. When the page
     * genuinely has no room left the remainder cascades from the origin, offset
     * by a gutter each: still visible, still obviously new, and honest about
     * the page being full.
     *
     * Pure and static so the arithmetic can be checked without a browser.
     */
    static freePlacements(sizes, page, occupied, gutter, origin) {
        const left = origin ? origin.x_mm : page.margins_mm.left;
        const top = origin ? origin.y_mm : page.margins_mm.top;
        const right = page.size_mm.w - page.margins_mm.right;
        const floor = page.size_mm.h - page.margins_mm.bottom;
        const taken = (occupied || []).map((box) => ({ ...box }));

        const out = [];
        let x = left;
        let y = top;
        let rowHeight = 0;

        for (const size of sizes) {
            let box = null;
            // Bounded rather than "until it fits": a page crowded with narrow
            // panels can otherwise step forward a fraction of a millimetre at a
            // time, and a layout helper must not be able to hang the tab.
            for (let attempt = 0; attempt < 400 && !box; attempt += 1) {
                if (x + size.w_mm > right + 0.001 && x > left) {
                    x = left;
                    y += (rowHeight || size.h_mm) + gutter;
                    rowHeight = 0;
                }
                if (y + size.h_mm > floor + 0.001 && y > top) break;
                const candidate = { x_mm: x, y_mm: y, w_mm: size.w_mm, h_mm: size.h_mm };
                const clash = taken.find((other) => FigureCanvas.overlaps(candidate, other));
                if (!clash) box = candidate;
                else x = clash.x_mm + clash.w_mm + gutter;
            }
            if (!box) {
                const step = gutter * (out.length + 1);
                box = { x_mm: left + step, y_mm: top + step, w_mm: size.w_mm, h_mm: size.h_mm };
            }
            out.push(box);
            taken.push(box);
            rowHeight = Math.max(rowHeight, box.h_mm);
            x = box.x_mm + box.w_mm + gutter;
            y = box.y_mm;
        }
        return out;
    }

    nextZ() {
        const panels = FigureSchema.panelsOnPage(this.state.document, this.pageId);
        return panels.reduce((top, panel) => Math.max(top, panel.placement.z), 0) + 1;
    }

}
