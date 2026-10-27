/**
 * qcDraw.js - drawing a QC region by hand, without leaving the QC panel.
 *
 * The ROI tool's Freehand, Polygon and Rectangle, restated at the size QC
 * needs (`shape`, picked on the drawing bar on the image): Freehand,
 * press and drag round an artifact and release; Rectangle, drag a box;
 * Polygon, click its corners and finish with a double-click, Enter or a click
 * on the first one (Backspace takes the last back, a drag still pans). The region is stored through the ROI
 * plugin's own `create_roi` (the QC route `/regions/draw`), so it is an ROI
 * like any other and the ROI panel, when opened, shows and edits it -- but
 * opening that panel is never needed to make one.
 *
 * Two modes, and only the user switches between them: DRAW (a drag draws)
 * and PAN (a drag moves the image, the viewer's own navigation). Holding
 * Space while drawing pans for as long as it is held; Escape goes to PAN.
 * The wheel always zooms. As in roiTools.js, the viewer is overruled per
 * gesture (`preventDefaultAction`) rather than switched off wholesale, and
 * the gesture is decided at press: Space pressed mid-stroke does not turn the
 * stroke into a pan.
 *
 * The stroke in progress is drawn by QC's region overlay (`draft`), in the
 * category's colour, in image pixels like everything else there.
 */
class QcFreehand {

    constructor(ctx, overlay) {
        this.ctx = ctx;
        this.overlay = overlay;
        this.viewer = ctx.viewer?.viewer || null;
        this.drawing = false;       // DRAW mode (true) or PAN mode (false)
        this.spaceHeld = false;
        this.stroke = null;         // [[x, y]] while a stroke is being drawn
        this.color = "#fbbf24";
        this.shape = "freehand";    // freehand | polygon | rectangle
        this.hover = null;          // the pointer, a polygon's rubber band
        this.onStroke = null;       // (points, shape) => void, a finished shape
        this.onModeChange = null;   // () => void
        this._handlers = [];
        this._onMove = (event) => this.move(event);
        this._onKeyDown = (event) => this.keyDown(event);
        this._onKeyUp = (event) => { if (event.key === " ") this.releaseSpace(); };
        this._onBlur = () => this.releaseSpace();
    }

    //: Screen pixels a stroke may wobble by before it is a vertex -- the ROI
    //: tool's freehand tolerance.
    static get SIMPLIFY_PX() { return 1.5; }
    //: A stroke enclosing less than this many square screen pixels is a stray
    //: press, not a region.
    static get MIN_AREA_PX() { return 36; }
    //: Screen pixels from a polygon's first corner that a click closes it.
    static get CLOSE_PX() { return 8; }
    static get SHAPES() { return ["freehand", "polygon", "rectangle"]; }

    /** Draw mode on, in `color`. */
    start(color) {
        if (color) this.color = color;
        if (!this.viewer) return false;
        if (!this._handlers.length) {
            const on = (name, fn) => {
                this.viewer.addHandler(name, fn);
                this._handlers.push([name, fn]);
            };
            on("canvas-press", (e) => this.press(e));
            on("canvas-drag", (e) => this.dragging(e));
            on("canvas-drag-end", (e) => this.dragEnd(e));
            on("canvas-click", (e) => this.click(e));
            on("canvas-double-click", (e) => this.doubleClick(e));
            this.viewer.canvas?.addEventListener("pointermove", this._onMove);
            document.addEventListener("keydown", this._onKeyDown);
            document.addEventListener("keyup", this._onKeyUp);
            window.addEventListener("blur", this._onBlur);
        }
        this.drawing = true;
        this.applyCursor();
        this.onModeChange?.();
        return true;
    }

    /** Which shape a gesture draws; drawing goes on if it was off. */
    setShape(shape) {
        if (!QcFreehand.SHAPES.includes(shape)) return;
        this.cancelStroke();
        this.shape = shape;
        this.start();
    }

    /** Pan mode: the viewer's own navigation, with the listeners still up
     *  so Draw is one click (or key) away. */
    pan() {
        this.cancelStroke();
        this.drawing = false;
        this.applyCursor();
        this.onModeChange?.();
    }

    /** Everything off: no listeners, no cursor, nothing half drawn. */
    stop() {
        this.cancelStroke();
        this.drawing = false;
        for (const [name, fn] of this._handlers) this.viewer?.removeHandler(name, fn);
        this._handlers = [];
        this.viewer?.canvas?.removeEventListener("pointermove", this._onMove);
        document.removeEventListener("keydown", this._onKeyDown);
        document.removeEventListener("keyup", this._onKeyUp);
        window.removeEventListener("blur", this._onBlur);
        this.spaceHeld = false;
        this.setCursor("");
        this.onModeChange?.();
    }

    get active() {
        return this._handlers.length > 0;
    }

    get polygon() {
        return this.shape === "polygon";
    }

    // -- the viewer ---------------------------------------------------------------

    /** Screen position -> full-resolution image pixel (roiTools.toImage). */
    toImage(position) {
        const item = this.viewer?.world?.getItemAt(0);
        if (!item) return null;
        if (item.source && typeof item.source.getImagePixel === "function") {
            const [x, y] = item.source.getImagePixel(item, position);
            return [x, y];
        }
        const viewportPoint = window.PlexoraViewTransform
            ? window.PlexoraViewTransform.pointFromPixel(this.viewer, position)
            : this.viewer.viewport.pointFromPixel(position);
        const imagePoint = item.viewportToImageCoordinates(viewportPoint);
        const scale = 2 ** (this.ctx.config?.extraZoomLevels || 0);
        return [imagePoint.x / scale, imagePoint.y / scale];
    }

    /** Image pixels per screen pixel at the current zoom. */
    imagePerScreen() {
        try {
            const item = this.viewer.world.getItemAt(0);
            const zoom = item.viewportToImageZoom(this.viewer.viewport.getZoom(true));
            return zoom > 0 ? 1 / zoom : 1;
        } catch (error) {
            return 1;
        }
    }

    clamp([x, y]) {
        const w = Number(this.ctx.config?.width) || Infinity;
        const h = Number(this.ctx.config?.height) || Infinity;
        return [Math.min(Math.max(x, 0), w), Math.min(Math.max(y, 0), h)];
    }

    setCursor(cursor) {
        if (this.viewer?.canvas) this.viewer.canvas.style.cursor = cursor;
    }

    /** A crosshair while a drag would draw; the canvas's own grab otherwise. */
    applyCursor() {
        this.setCursor(this.drawing && !this.spaceHeld ? "crosshair" : "");
    }

    // -- the pointer ----------------------------------------------------------------

    press(event) {
        // A polygon is built from clicks; a press there may be a pan.
        if (!this.drawing || this.spaceHeld || this.polygon) return;
        const point = this.toImage(event.position);
        if (!point) return;
        this.stroke = [this.clamp(point)];
        this.showStroke();
    }

    dragging(event) {
        if (!this.stroke || this.polygon) return;
        event.preventDefaultAction = true;
        const point = this.toImage(event.position);
        if (!point) return;
        if (this.shape === "rectangle") this.stroke[1] = this.clamp(point);
        else this.stroke.push(this.clamp(point));
        this.showStroke();
    }

    dragEnd(event) {
        if (!this.stroke || this.polygon) return;
        event.preventDefaultAction = true;   // no flick-momentum pan
        const raw = this.shape === "rectangle" ? QcFreehand.box(this.stroke) : this.stroke;
        this.cancelStroke();
        const scale = this.imagePerScreen();
        const points = this.shape === "rectangle" ? raw
            : QcFreehand.simplify(QcFreehand.dedupe(raw, scale * 0.75),
                                  scale * QcFreehand.SIMPLIFY_PX);
        this.finish(points);
    }

    /** A click places a polygon's next corner; on its first one, closes it. */
    click(event) {
        if (!this.polygon) {
            // A press with no drag after it leaves nothing half drawn.
            if (event.quick && this.stroke) this.cancelStroke();
            return;
        }
        if (!this.drawing || this.spaceHeld || !event.quick) return;
        event.preventDefaultAction = true;   // a click here is a corner, not a zoom
        const point = this.toImage(event.position);
        if (!point) return;
        const corner = this.clamp(point);
        const corners = this.stroke || [];
        const near = this.imagePerScreen() * QcFreehand.CLOSE_PX;
        if (corners.length >= 3 && Math.hypot(corner[0] - corners[0][0],
                                              corner[1] - corners[0][1]) <= near) {
            this.finishPolygon();
            return;
        }
        const last = corners[corners.length - 1];
        if (!last || Math.hypot(corner[0] - last[0], corner[1] - last[1]) > near / 4) {
            this.stroke = [...corners, corner];
        }
        this.showStroke();
    }

    doubleClick(event) {
        if (!this.drawing || !this.polygon) return;
        event.preventDefaultAction = true;
        if (this.stroke) this.finishPolygon();
    }

    /** The pointer, for the edge from a polygon's last corner to it. */
    move(event) {
        if (!this.polygon || !this.stroke || !this.viewer?.canvas || !window.OpenSeadragon) return;
        const rect = this.viewer.canvas.getBoundingClientRect();
        const point = this.toImage(new window.OpenSeadragon.Point(event.clientX - rect.left,
                                                                  event.clientY - rect.top));
        this.hover = point ? this.clamp(point) : null;
        this.showStroke();
    }

    finishPolygon() {
        const corners = this.stroke || [];
        this.cancelStroke();
        this.finish(QcFreehand.dedupe(corners, this.imagePerScreen() * 0.75));
    }

    /** A finished ring, kept unless it is a stray press. */
    finish(points) {
        if (points.length < 3) return;
        const scale = this.imagePerScreen();
        if (Math.abs(QcFreehand.area(points)) < QcFreehand.MIN_AREA_PX * scale * scale) return;
        this.onStroke?.(points.map(([x, y]) => [Math.round(x * 100) / 100,
                                                Math.round(y * 100) / 100]), this.shape);
    }

    /** The drag's two corners as the box's four. */
    static box([[x0, y0], [x1, y1] = [x0, y0]]) {
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
    }

    showStroke() {
        let points = this.stroke;
        if (points && this.shape === "rectangle") {
            const box = QcFreehand.box(points);
            points = [...box, box[0]];
        } else if (points && this.polygon && this.hover) {
            points = [...points, this.hover];
        }
        this.overlay.draft = points && points.length ? { points, color: this.color } : null;
        this.overlay.schedule();
    }

    cancelStroke() {
        this.hover = null;
        if (!this.stroke && !this.overlay.draft) return;
        this.stroke = null;
        this.overlay.draft = null;
        this.overlay.schedule();
    }

    // -- the keyboard ---------------------------------------------------------------

    static typing() {
        const active = document.activeElement;
        if (!active) return false;
        return ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName) || active.isContentEditable;
    }

    keyDown(event) {
        if (QcFreehand.typing() || event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === " " && this.drawing) {
            event.preventDefault();   // Space scrolls the page otherwise
            if (!this.spaceHeld) {
                this.spaceHeld = true;
                this.applyCursor();
            }
        } else if (event.key === "Escape" && this.drawing) {
            event.preventDefault();
            // A polygon half made goes first; a second Escape pans.
            if (this.polygon && this.stroke) this.cancelStroke();
            else this.pan();
        } else if (event.key === "Enter" && this.polygon && this.stroke) {
            event.preventDefault();
            this.finishPolygon();
        } else if (event.key === "Backspace" && this.polygon && this.stroke) {
            event.preventDefault();
            this.stroke = this.stroke.slice(0, -1);
            if (!this.stroke.length) this.cancelStroke();
            else this.showStroke();
        }
    }

    releaseSpace() {
        if (!this.spaceHeld) return;
        this.spaceHeld = false;
        this.applyCursor();
    }

    // -- geometry -------------------------------------------------------------------

    static dedupe(points, tolerance) {
        const out = [];
        for (const point of points) {
            const last = out[out.length - 1];
            if (!last || Math.hypot(point[0] - last[0], point[1] - last[1]) > tolerance) {
                out.push(point);
            }
        }
        return out;
    }

    /** Ramer-Douglas-Peucker, iterative. */
    static simplify(points, epsilon) {
        if (points.length < 4) return points.slice();
        const keep = new Uint8Array(points.length);
        keep[0] = keep[points.length - 1] = 1;
        const stack = [[0, points.length - 1]];
        while (stack.length) {
            const [first, last] = stack.pop();
            const [ax, ay] = points[first];
            const [bx, by] = points[last];
            const dx = bx - ax;
            const dy = by - ay;
            const length = Math.hypot(dx, dy);
            let far = -1;
            let farthest = epsilon;
            for (let i = first + 1; i < last; i++) {
                const [px, py] = points[i];
                // A closed stroke ends where it began: from a segment of no
                // length, distance is to the point itself, or every point
                // measures zero and the loop collapses to its two ends.
                const distance = length < 1e-9 ? Math.hypot(px - ax, py - ay)
                    : Math.abs(dy * px - dx * py + bx * ay - by * ax) / length;
                if (distance > farthest) {
                    farthest = distance;
                    far = i;
                }
            }
            if (far >= 0) {
                keep[far] = 1;
                stack.push([first, far], [far, last]);
            }
        }
        return points.filter((_, i) => keep[i]);
    }

    static area(points) {
        let sum = 0;
        for (let i = 0; i < points.length; i++) {
            const [x0, y0] = points[i];
            const [x1, y1] = points[(i + 1) % points.length];
            sum += x0 * y1 - x1 * y0;
        }
        return sum / 2;
    }
}

window.QcFreehand = QcFreehand;


/**
 * Magic select in the QC panel (key E, or the wand on the drawing bar): click
 * an artifact and its outline appears as a QC region in the category in hand.
 * Its options open out of the wand on the bar (core's services/drawToolbar.js,
 * through `bars`) and switch what a click does -- Add (include; a
 * drag pans), Remove (take away; Shift-click in Add too), Box (drag round a
 * large or textured object) and Scribble (a line drawn over the object, sent
 * as points along it, or a loop round it, sent as its box; Shift for a line
 * over what to leave out). Clicks build up the outline being made; a box or a
 * scribble outlines a whole object by itself, so each starts its own. Begun
 * inside the selected QC region, any of them redraws that region instead.
 * The ROI tool's magic select, at QC's size: the
 * same planner and prompt bookkeeping (services/segmentService.js), QC's own
 * routes to store the result (`/regions/draw` with a geometry, and
 * `/regions/reshape`), through callbacks the controller supplies.
 *
 * Coordinates come from the freehand pen it is handed (one `toImage` for the
 * panel). It yields while the ROI tool is on screen, so two tools never
 * outline one click.
 */
class QcMagic {

    constructor(ctx, overlay, pen) {
        this.ctx = ctx;
        this.overlay = overlay;
        this.pen = pen;
        this.viewer = ctx.viewer?.viewer || null;
        this.active = false;
        this.busy = false;
        this.spaceHeld = false;
        this.color = "#fbbf24";
        this.session = null;     // {session: PlexoraSegment.Session, snapshot}
        //: How a click prompts: the bar's Add / Remove / Box / Scribble.
        this.mode = "box";
        //: Where the options are shown (PlexoraDrawBar.magicBars), set by the
        //: controller; `bar` is this tool's handle on them while it is on.
        this.bars = null;
        this.bar = null;
        this.dragOrigin = null;
        this.box = null;
        this.stroke = null;      // a scribble being drawn: {points, remove}
        // Callbacks (the controller's).
        this.selected = () => null;       // {id, bbox: {x,y,width,height}, locked} | null
        this.canCreate = () => false;
        this.onCommit = null;             // async ({roiId, geometry}) => roiId | null
        this.onMessage = null;            // (text) => void
        this.onBusy = null;               // (busy) => void
        this.onModeChange = null;         // () => void
        this.onEscape = null;             // () => void, Esc with nothing to cancel
        this._handlers = [];
        this._onKeyDown = (event) => this.keyDown(event);
        this._onKeyUp = (event) => { if (event.key === " ") this.releaseSpace(); };
        this._onBlur = () => this.releaseSpace();
    }

    start(color) {
        if (color) this.color = color;
        if (!this.viewer) return false;
        if (!this._handlers.length) {
            const on = (name, fn) => {
                this.viewer.addHandler(name, fn);
                this._handlers.push([name, fn]);
            };
            on("canvas-press", (e) => this.press(e));
            on("canvas-drag", (e) => this.dragging(e));
            on("canvas-drag-end", (e) => this.dragEnd(e));
            on("canvas-click", (e) => this.click(e));
            on("canvas-double-click", (e) => { if (this.live()) e.preventDefaultAction = true; });
            document.addEventListener("keydown", this._onKeyDown);
            document.addEventListener("keyup", this._onKeyUp);
            window.addEventListener("blur", this._onBlur);
        }
        const already = this.active;
        this.active = true;
        if (!already) this.mode = "box";   // the ROI tool's default too
        this.showBar();
        // Arming it starts the one-time setup when the model is not there yet.
        window.PlexoraSegment?.ensureReady();
        this.applyCursor();
        this.onModeChange?.();
        return true;
    }

    stop() {
        this.endSession();
        this.cancelBox();
        this.bar?.hide();
        this.bar = null;
        this.active = false;
        for (const [name, fn] of this._handlers) this.viewer?.removeHandler(name, fn);
        this._handlers = [];
        document.removeEventListener("keydown", this._onKeyDown);
        document.removeEventListener("keyup", this._onKeyUp);
        window.removeEventListener("blur", this._onBlur);
        this.spaceHeld = false;
        this.pen.setCursor("");
        this.onModeChange?.();
    }

    /** Add / Remove / Box / Scribble, opened out of the wand on the bar. */
    showBar() {
        if (!this.bars) return;
        this.bar = this.bars.show({
            owner: this, mode: this.mode,
            onMode: (mode) => this.setMode(mode),
        });
    }

    /** A mode change keeps the outline being made. */
    setMode(mode) {
        if (!["add", "remove", "box", "scribble"].includes(mode)) return;
        this.mode = mode;
        this.bar?.setMode(mode);
        this.applyCursor();
    }

    /** On, and not yielding to the ROI tool. */
    live() {
        return this.active && !window.PlexoraToolLoader?.isToolVisible?.("roi");
    }

    applyCursor() {
        if (!this.active) return;
        this.pen.setCursor(this.spaceHeld ? "" : (this.busy ? "progress" : "crosshair"));
    }

    // -- the pointer ----------------------------------------------------------------

    press(event) {
        if (!this.live() || this.spaceHeld) return;
        this.box = null;
        this.stroke = null;
        // Box and Scribble take drags; in Add and Remove a drag pans the image.
        if (this.mode !== "box" && this.mode !== "scribble") {
            this.dragOrigin = null;
            return;
        }
        const point = this.pen.toImage(event.position);
        this.dragOrigin = point ? this.pen.clamp(point) : null;
        if (this.dragOrigin && this.mode === "scribble") {
            this.stroke = { points: [this.dragOrigin],
                            remove: Boolean(event.originalEvent?.shiftKey) };
        }
    }

    dragging(event) {
        if (!this.live() || this.spaceHeld || !this.dragOrigin) return;
        event.preventDefaultAction = true;
        const point = this.pen.toImage(event.position);
        if (!point) return;
        if (this.stroke) {
            this.stroke.points.push(this.pen.clamp(point));
            this.overlay.draft = { points: this.stroke.points, color: this.color,
                                   scribble: this.stroke.remove ? "remove" : "add" };
            this.overlay.schedule();
            return;
        }
        const [x0, y0] = this.dragOrigin;
        const [x1, y1] = this.pen.clamp(point);
        this.box = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]];
        this.overlay.draft = { points: this.box, color: this.color };
        this.overlay.schedule();
    }

    dragEnd(event) {
        if (!this.live() || !this.dragOrigin) return;
        event.preventDefaultAction = true;
        const corners = this.box;
        const stroke = this.stroke;
        this.dragOrigin = null;
        this.cancelBox();
        if (stroke) {
            // A line over the object is points along it; a loop round it is its box.
            const prompt = window.PlexoraSegment?.strokePrompt?.(stroke.points, {
                spacing: QcMagic.STROKE_SPACING * this.pen.imagePerScreen(),
                minDepth: QcMagic.LOOP_DEPTH * this.pen.imagePerScreen(),
                remove: stroke.remove });
            if (prompt) this.prompt({ ...prompt, shift: stroke.remove });
            return;
        }
        if (!corners) return;
        const xs = corners.map((p) => p[0]);
        const ys = corners.map((p) => p[1]);
        const box = { x: Math.min(...xs), y: Math.min(...ys),
                      width: Math.max(...xs) - Math.min(...xs),
                      height: Math.max(...ys) - Math.min(...ys) };
        const least = 6 * this.pen.imagePerScreen();
        if (box.width < least || box.height < least) return;
        this.prompt({ box });
    }

    click(event) {
        if (!this.live() || this.spaceHeld) return;
        event.preventDefaultAction = true;
        if (event.quick === false) return;   // the end of a drag: a box, or a pan
        this.dragOrigin = null;
        if (this.mode === "box") return;
        const point = this.pen.toImage(event.position);
        if (!point) return;
        this.prompt({ point: { x: point[0], y: point[1] },
                      shift: this.mode === "remove" || Boolean(event.originalEvent?.shiftKey) });
    }

    cancelBox() {
        this.box = null;
        this.stroke = null;
        if (this.overlay.draft) {
            this.overlay.draft = null;
            this.overlay.schedule();
        }
    }

    // -- prompts ----------------------------------------------------------------------

    /** Screen pixels between the points a scribble becomes. */
    static get STROKE_SPACING() { return 24; }

    /** Screen pixels deep (its radius, if round) a loop must be to have been
     *  drawn round something rather than dabbed on it. */
    static get LOOP_DEPTH() { return 8; }

    static padded(bbox, fraction) {
        const pad = Math.max(bbox.width, bbox.height) * fraction;
        return { x: bbox.x - pad, y: bbox.y - pad, width: bbox.width + 2 * pad,
                 height: bbox.height + 2 * pad };
    }

    prompt({ point = null, box = null, stroke = null, shift = false } = {}) {
        const segment = window.PlexoraSegment;
        if (!segment || this.busy) return;
        // A scribble is decided by where it starts (inside the selected region
        // it reshapes it, elsewhere it is a new one, never more of the last);
        // its points go in as one step, undone together.
        if (stroke && !stroke.length) return;
        const prompts = stroke || (point ? [point] : []);
        const where = prompts[0] || { x: box.x + box.width / 2, y: box.y + box.height / 2 };
        const snapshot = window.PlexoraViewSnapshot?.capture({ sample: this.ctx.datasource });
        if (this.session && this.session.snapshot && snapshot
                && !window.PlexoraViewSnapshot.sameView(this.session.snapshot, snapshot)) {
            this.session = null;
        }
        const selected = this.selected();
        const plan = segment.plan({
            point: where, shift, selected,
            session: this.session ? { roiId: this.session.session.roiId,
                                      bbox: this.session.session.bbox } : null,
            canCreate: this.canCreate(),
            standalone: Boolean(box || stroke),
        });
        const marks = { stroke: Boolean(stroke) };
        switch (plan.kind) {
            case "locked":
                return this.onMessage?.("That region is locked. Unlock it in the ROI panel to reshape it.");
            case "needCategory":
                return this.onMessage?.("Pick what you are marking first (the + above).");
            case "needSelection":
                return this.onMessage?.("Remove takes an area out of an outline: make one first, "
                    + "or select a region and click inside it");
            case "new":
                this.session = { session: new segment.Session({ box }), snapshot };
                this.session.session.addMany(prompts, 1, marks);
                break;
            case "grow":
            case "carve":
                if (!this.session || this.session.session.roiId !== plan.roiId) {
                    if (!selected || selected.id !== plan.roiId) return;
                    this.session = { session: new segment.Session({
                        roiId: selected.id, bbox: selected.bbox,
                        box: QcMagic.padded(selected.bbox, 0.05),
                        seed: selected.geometry || null }), snapshot };
                }
                if (box) this.session.session.box = box;
                else this.session.session.addMany(prompts, plan.kind === "grow" ? 1 : 0, marks);
                break;
            case "refine":
                // No seed from a loose outline here (it is drawn back as
                // itself); Remove keeps one, to take away from what is there.
                this.session = { session: new segment.Session({
                    roiId: selected.id, bbox: selected.bbox,
                    box: box || QcMagic.padded(selected.bbox, 0.10) }), snapshot };
                this.session.session.addMany(prompts, 1, marks);
                break;
            default:
                return;
        }
        this.showPrompts(prompts.length > 0);
        this.run(this.session);
    }

    /** The outline's dots; `pending` while the latest prompt is on its way. */
    showPrompts(pending = false) {
        const points = this.session ? this.session.session.shown(pending) : [];
        this.overlay.prompts = points.length ? points : null;
        this.overlay.promptColor = this.color;
        this.overlay.schedule();
    }

    /** The outline being made lost its region (deleted from the list, by an
     *  agent, from the ROI panel): its dots go with it, and the next prompt
     *  starts a new outline. `exists(roiId)` says what is still there. */
    forgetGone(exists) {
        const id = this.session?.session.roiId;
        if (id && !exists(id)) this.endSession();
    }

    setBusy(busy) {
        this.busy = busy;
        this.applyCursor();
        this.bar?.setBusy(busy);
        this.onBusy?.(busy);
    }

    async run(current) {
        const segment = window.PlexoraSegment;
        const session = current.session;
        this.setBusy(true);
        let result;
        try {
            result = await segment.point({
                datasource: this.ctx.datasource, points: session.points, box: session.box,
                snapshot: current.snapshot,
                simplifyPx: QcFreehand.SIMPLIFY_PX * this.pen.imagePerScreen() / 2,
                usePrevious: session.refining,
                maskGeometry: session.seed,
                retry: () => {
                    if (this.session !== current || !this.active) return false;
                    const now = window.PlexoraViewSnapshot?.capture({ sample: this.ctx.datasource });
                    if (now && current.snapshot
                            && !window.PlexoraViewSnapshot.sameView(current.snapshot, now)) return false;
                    this.run(current);
                    return true;
                },
            });
        } finally {
            this.setBusy(false);
        }
        if (this.session !== current) return;
        const drop = () => {
            session.undo();
            if (!session.points.length && !session.roiId) this.endSession();
            else this.showPrompts();
        };
        if (!result || result.ok === false) {
            if (result && result.kind === "setup") return;
            if (result && result.kind === "busy") this.onMessage?.("One moment -- still outlining the last click");
            else if (result && result.message) this.onMessage?.(result.message);
            return drop();
        }
        const flags = result.flags || {};
        if (flags.too_large) {
            this.onMessage?.("That covers most of the screen -- zoom in for a tighter outline");
            return drop();
        }
        if (!result.geometry || flags.empty) {
            this.onMessage?.("Nothing found there. Click nearer its middle, or drag a box round it");
            return drop();
        }
        this.setBusy(true);
        let roiId = null;
        try {
            roiId = await this.onCommit?.({ roiId: session.roiId, geometry: result.geometry });
        } finally {
            this.setBusy(false);
        }
        if (this.session !== current) return;
        if (!roiId) return drop();
        session.roiId = roiId;
        session.absorb(result);
        if (flags.touches_edge) {
            this.onMessage?.("Part of it runs off the screen -- zoom out and click again for all of it");
        }
        this.showPrompts();
    }

    endSession() {
        const had = Boolean(this.session);
        this.session = null;
        if (this.overlay.prompts) {
            this.overlay.prompts = null;
            this.overlay.schedule();
        }
        return had;
    }

    // -- the keyboard ---------------------------------------------------------------

    keyDown(event) {
        if (!this.live() || QcFreehand.typing() || event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === " ") {
            event.preventDefault();
            if (!this.spaceHeld) {
                this.spaceHeld = true;
                this.applyCursor();
            }
        } else if (event.key === "Escape") {
            event.preventDefault();
            if (!this.endSession()) this.onEscape?.();
        }
    }

    releaseSpace() {
        if (!this.spaceHeld) return;
        this.spaceHeld = false;
        this.applyCursor();
    }
}

window.QcMagic = QcMagic;
