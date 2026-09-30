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
