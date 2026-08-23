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

    constructor(options) {
        this.state = options.state;
        this.api = options.api;
        this.figureId = options.figureId;
        this.onEditPanel = options.onEditPanel || (() => {});
        this.onSelectionChange = options.onSelectionChange || (() => {});

        this.pageEl = options.pageEl;
        this.surfaceEl = options.surfaceEl;
        this.guideEl = options.guideEl || null;

        this.scale = 96 / 25.4;
        this.pageId = null;
        this.selection = new Set();
        //: {kind, panels:[{panel_id, start}], origin, handle} while a gesture is
        //: in flight. Null the rest of the time, which is what every handler
        //: below tests instead of a set of booleans.
        this.gesture = null;
    }

    // -- units -----------------------------------------------------------

    toPx(mm) { return mm * this.scale; }

    toMm(px) { return px / this.scale; }

    get page() {
        return FigureSchema.pageById(this.state.document, this.pageId);
    }

    // -- lifecycle -------------------------------------------------------

    setup() {
        this.surfaceEl.addEventListener("pointerdown", (event) => this.pointerDown(event));
        this.surfaceEl.addEventListener("dblclick", (event) => this.doubleClick(event));
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
        this.setScale(Math.min(
            (viewportEl.clientWidth - margin) / page.size_mm.w,
            (viewportEl.clientHeight - margin) / page.size_mm.h));
    }

    render() {
        const page = this.page;
        if (!page) return;

        this.pageEl.style.width = this.toPx(page.size_mm.w) + "px";
        this.pageEl.style.height = this.toPx(page.size_mm.h) + "px";
        this.pageEl.style.background = page.background;

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

    handlesMarkup() {
        return ["nw", "ne", "se", "sw", "n", "e", "s", "w"].map((handle) =>
            `<span class="fb-handle fb-handle-${handle}" data-handle="${handle}"></span>`).join("");
    }

    annotationMarkup(annotation) {
        const geometry = annotation.geometry;
        const style = [
            `left:${this.toPx(geometry.x_mm)}px`,
            `top:${this.toPx(geometry.y_mm)}px`,
            `width:${this.toPx(geometry.w_mm)}px`,
            `height:${this.toPx(geometry.h_mm)}px`,
            `z-index:${1000 + annotation.z}`,
            `color:${annotation.style.color}`,
        ].join(";");
        const selected = this.selection.has(annotation.annotation_id) ? " is-selected" : "";
        if (annotation.type === "text") {
            return `<div class="fb-annotation fb-annotation-text${selected}" style="${style};
                        font-size:${annotation.style.font_size_pt * this.scale / 2.8346}px"
                        data-annotation-id="${FigureSchema.escapeHtml(annotation.annotation_id)}"
                    >${FigureSchema.escapeHtml(annotation.text)}</div>`;
        }
        return `<div class="fb-annotation fb-annotation-${annotation.type}${selected}"
                     style="${style};border-color:${annotation.style.color}"
                     data-annotation-id="${FigureSchema.escapeHtml(annotation.annotation_id)}"></div>`;
    }

    // -- selection -------------------------------------------------------

    select(ids, additive) {
        if (!additive) this.selection.clear();
        for (const id of ids) {
            if (additive && this.selection.has(id)) this.selection.delete(id);
            else this.selection.add(id);
        }
        this.render();
        this.onSelectionChange(Array.from(this.selection));
    }

    selectedPanels() {
        return Array.from(this.selection)
            .map((id) => this.state.panel(id))
            .filter(Boolean);
    }

    // -- gestures --------------------------------------------------------

    surfacePoint(event) {
        const rect = this.surfaceEl.getBoundingClientRect();
        return { x: this.toMm(event.clientX - rect.left), y: this.toMm(event.clientY - rect.top) };
    }

    pointerDown(event) {
        if (event.button !== 0) return;
        const handle = event.target.closest?.(".fb-handle");
        const panelEl = event.target.closest?.(".fb-panel");
        const annotationEl = event.target.closest?.(".fb-annotation");

        if (handle && panelEl) {
            event.preventDefault();
            this.beginGesture("resize", event, { handle: handle.dataset.handle });
            return;
        }
        if (panelEl || annotationEl) {
            const id = panelEl ? panelEl.dataset.panelId : annotationEl.dataset.annotationId;
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

    beginGesture(kind, event, extra) {
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
    }

    pointerUp() {
        const gesture = this.gesture;
        this.gesture = null;
        if (!gesture) return;
        this.clearGuides();

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
            const element = this.elementFor(item);
            if (!element) continue;
            element.style.left = this.toPx(item.start.x_mm + snapped.dx) + "px";
            element.style.top = this.toPx(item.start.y_mm + snapped.dy) + "px";
        }
        this.gesture.delta = snapped;
    }

    previewResize(dx, dy, keepAspect) {
        const handle = this.gesture.handle;
        for (const item of this.gesture.items) {
            const element = this.elementFor(item);
            if (!element) continue;
            const box = this.resizedBox(item.start, handle, dx, dy, keepAspect);
            element.style.left = this.toPx(box.x_mm) + "px";
            element.style.top = this.toPx(box.y_mm) + "px";
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
     */
    resizedBox(start, handle, dx, dy, freeAspect) {
        let { x_mm: x, y_mm: y, w_mm: w, h_mm: h } = start;
        const corner = handle.length === 2;

        if (corner && !freeAspect) {
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

        const smallest = FigureCanvas.MIN_SIZE_MM;
        if (w < smallest) { if (handle.includes("w")) x -= smallest - w; w = smallest; }
        if (h < smallest) { if (handle.includes("n")) y -= smallest - h; h = smallest; }
        return { ...start, x_mm: x, y_mm: y, w_mm: w, h_mm: h };
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
        const hits = FigureSchema.panelsOnPage(this.state.document, this.pageId)
            .filter((panel) => {
                const place = panel.placement;
                return place.x_mm < box.x + box.w && place.x_mm + place.w_mm > box.x
                    && place.y_mm < box.y + box.h && place.y_mm + place.h_mm > box.y;
            })
            .map((panel) => panel.panel_id);
        this.select(hits, false);
    }

    commitGesture(gesture) {
        const moves = [];
        const annotationOps = [];
        for (const item of gesture.items) {
            const element = this.elementFor(item);
            if (!element) continue;
            const box = {
                x_mm: this.toMm(parseFloat(element.style.left)),
                y_mm: this.toMm(parseFloat(element.style.top)),
                w_mm: this.toMm(parseFloat(element.style.width) || this.toPx(item.start.w_mm)),
                h_mm: this.toMm(parseFloat(element.style.height) || this.toPx(item.start.h_mm)),
            };
            if (item.kind === "panel") {
                moves.push({ panel_id: item.id, placement: { ...item.start, ...box } });
                if (gesture.kind === "resize") {
                    // A linked row shares a box. Only on a resize: sharing a
                    // POSITION would mean dragging one panel dragged them all
                    // onto each other, and the row could never be a row.
                    moves.push(...this._linkedSizeMoves(this.state.panel(item.id), box));
                }
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
        const moving = this.gesture.items.filter((item) => item.kind === "panel");
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

    snapTargets() {
        const page = this.page;
        const moving = new Set(this.gesture.items.map((item) => item.id));
        const x = [0, page.size_mm.w / 2, page.size_mm.w,
                   page.margins_mm.left, page.size_mm.w - page.margins_mm.right];
        const y = [0, page.size_mm.h / 2, page.size_mm.h,
                   page.margins_mm.top, page.size_mm.h - page.margins_mm.bottom];
        for (const panel of FigureSchema.panelsOnPage(this.state.document, this.pageId)) {
            if (moving.has(panel.panel_id)) continue;
            const place = panel.placement;
            x.push(place.x_mm, place.x_mm + place.w_mm / 2, place.x_mm + place.w_mm);
            y.push(place.y_mm, place.y_mm + place.h_mm / 2, place.y_mm + place.h_mm);
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
        const typing = document.activeElement
            && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
        if (typing || !this.selection.size) return;

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

    nudge(dx, dy) {
        const moves = this.selectedPanels()
            .filter((panel) => panel.placement)
            .map((panel) => ({
                panel_id: panel.panel_id,
                placement: { ...panel.placement,
                             x_mm: panel.placement.x_mm + dx,
                             y_mm: panel.placement.y_mm + dy },
            }));
        if (!moves.length) return;
        this.state.commit([{ op: "move_panels", moves: moves }], (draft) => {
            for (const move of moves) draft.panels[move.panel_id].placement = move.placement;
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

    // -- layout commands -------------------------------------------------

    /**
     * Align, distribute or equalise the selection.
     *
     * All of them compile to one `move_panels`, so each is one undo step -- the
     * same rule the drag follows, for the same reason.
     */
    arrange(command) {
        const panels = this.selectedPanels().filter((panel) => panel.placement);
        if (panels.length < 2) return;
        const boxes = panels.map((panel) => ({ ...panel.placement }));

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

        this.commitBoxes(panels, boxes);
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

    commitBoxes(panels, boxes) {
        const moves = panels.map((panel, index) => ({
            panel_id: panel.panel_id, placement: boxes[index],
        }));
        this.state.commit([{ op: "move_panels", moves: moves }], (draft) => {
            for (const move of moves) draft.panels[move.panel_id].placement = move.placement;
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
     * A panel dragged in from the tray.
     *
     * Sized from the region it shows rather than to a fixed box, so a wide
     * field arrives wide -- landing every panel as a square and making the user
     * fix the aspect ratio afterwards is squashed tissue waiting to be exported.
     */
    dropFromTray(event) {
        const panelId = event.dataTransfer?.getData("text/x-plexora-panel");
        if (!panelId) return;
        event.preventDefault();
        const panel = this.state.panel(panelId);
        const page = this.page;
        if (!panel || !page) return;

        const point = this.surfacePoint(event);
        const aspect = panel.scene.viewport.h / panel.scene.viewport.w || 1;
        const width = Math.min(60, page.size_mm.w / 3);
        const placement = {
            page_id: this.pageId,
            x_mm: Math.max(0, point.x - width / 2),
            y_mm: Math.max(0, point.y - (width * aspect) / 2),
            w_mm: width,
            h_mm: width * aspect,
            z: this.nextZ(),
        };
        this.state.commit(
            [{ op: "move_panels", moves: [{ panel_id: panelId, placement: placement }] }],
            (draft) => { draft.panels[panelId].placement = placement; });
    }

    nextZ() {
        const panels = FigureSchema.panelsOnPage(this.state.document, this.pageId);
        return panels.reduce((top, panel) => Math.max(top, panel.placement.z), 0) + 1;
    }

    doubleClick(event) {
        const panelEl = event.target.closest?.(".fb-panel");
        if (!panelEl) return;
        // Double-click is "reopen the view this came from", which is a
        // different intent from selecting it for layout -- and an expensive
        // one, which is why it is not what a single click does.
        this.onEditPanel(panelEl.dataset.panelId);
    }
}
