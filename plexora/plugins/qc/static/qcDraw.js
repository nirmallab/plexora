/**
 * qcDraw.js - drawing a QC region by hand, without leaving the QC panel.
 *
 * The ROI tool's Freehand, restated at the size QC needs: press and drag round
 * an artifact, release to keep it. The region is stored through the ROI
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
        this.onStroke = null;       // (points) => void, a finished stroke
        this.onModeChange = null;   // () => void
        this._handlers = [];
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
            document.addEventListener("keydown", this._onKeyDown);
            document.addEventListener("keyup", this._onKeyUp);
            window.addEventListener("blur", this._onBlur);
        }
        this.drawing = true;
        this.applyCursor();
        this.onModeChange?.();
        return true;
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
        if (!this.drawing || this.spaceHeld) return;
        const point = this.toImage(event.position);
        if (!point) return;
        this.stroke = [this.clamp(point)];
        this.showStroke();
    }

    dragging(event) {
        if (!this.stroke) return;
        event.preventDefaultAction = true;
        const point = this.toImage(event.position);
        if (!point) return;
        this.stroke.push(this.clamp(point));
        this.showStroke();
    }

    dragEnd(event) {
        if (!this.stroke) return;
        event.preventDefaultAction = true;   // no flick-momentum pan
        const raw = this.stroke;
        this.cancelStroke();
        const scale = this.imagePerScreen();
        const points = QcFreehand.simplify(QcFreehand.dedupe(raw, scale * 0.75),
                                           scale * QcFreehand.SIMPLIFY_PX);
        if (points.length < 3) return;
        if (Math.abs(QcFreehand.area(points)) < QcFreehand.MIN_AREA_PX * scale * scale) return;
        this.onStroke?.(points.map(([x, y]) => [Math.round(x * 100) / 100,
                                                Math.round(y * 100) / 100]));
    }

    showStroke() {
        this.overlay.draft = this.stroke ? { points: this.stroke, color: this.color } : null;
        this.overlay.schedule();
    }

    cancelStroke() {
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
            this.pan();
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
 * Magic select in the QC panel (key E, or the wand on the draw bar): click an
 * artifact and its outline appears as a QC region in the category in hand.
 * A floating bar on the image switches what a click does -- Add (include; a
 * drag pans), Remove (take away; Shift-click in Add too), Box (drag round a
 * large or textured object) and Scribble (a line drawn over the object, sent
 * as points along it; Shift for a line over what to leave out) -- and all
 * four refine the same outline. A click
 * inside the selected QC region refines that region, starting from its own
 * outline; × on the bar puts magic select away. The ROI tool's magic select, at QC's size: the
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
        //: How a click prompts: the floating bar's Add / Remove / Box / Scribble.
        this.mode = "box";
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
        this.onClose = null;              // () => void, the bar's ×
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

    /** The floating bar on the image: Add / Remove / Box / Scribble, and × to close. */
    showBar() {
        const bars = window.PlexoraMagicBar;
        if (!bars) return;
        this.bar = bars.show({
            owner: this, mode: this.mode,
            onMode: (mode) => this.setMode(mode),
            onClose: () => (this.onClose ? this.onClose() : this.stop()),
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
            const points = window.PlexoraSegment?.strokePoints?.(stroke.points, {
                spacing: QcMagic.STROKE_SPACING * this.pen.imagePerScreen() }) || [];
            if (points.length) this.prompt({ stroke: points, shift: stroke.remove });
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

    static padded(bbox, fraction) {
        const pad = Math.max(bbox.width, bbox.height) * fraction;
        return { x: bbox.x - pad, y: bbox.y - pad, width: bbox.width + 2 * pad,
                 height: bbox.height + 2 * pad };
    }

    prompt({ point = null, box = null, stroke = null, shift = false } = {}) {
        const segment = window.PlexoraSegment;
        if (!segment || this.busy) return;
        // A scribble is decided by where it starts (inside the selected region
        // it reshapes it); its points go in as one step, undone together.
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
        });
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
                this.session.session.addMany(prompts, 1);
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
                else this.session.session.addMany(prompts, plan.kind === "grow" ? 1 : 0);
                break;
            case "refine":
                // No seed from a loose outline here (it is drawn back as
                // itself); Remove keeps one, to take away from what is there.
                this.session = { session: new segment.Session({
                    roiId: selected.id, bbox: selected.bbox,
                    box: box || QcMagic.padded(selected.bbox, 0.10) }), snapshot };
                this.session.session.addMany(prompts, 1);
                break;
            default:
                return;
        }
        this.showPrompts();
        this.run(this.session);
    }

    showPrompts() {
        this.overlay.prompts = this.session ? this.session.session.points.slice() : null;
        this.overlay.promptColor = this.color;
        this.overlay.schedule();
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
