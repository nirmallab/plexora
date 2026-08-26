/**
 * Drawing a custom shape: polygon, curved shape, freehand, open path.
 *
 * These four are the reason the shape tool is not just a picker. They are
 * MODES rather than gestures, and that is what separates them from every other
 * drawing tool on this canvas: a rectangle is one press, one drag, one release,
 * so `FigureCanvas.beginGesture` describes it exactly. A polygon is an
 * unbounded number of presses with no drag at all, and there is no gesture
 * object for the whole of it -- the canvas's armed-tool branch hands the press
 * here and returns before `beginGesture`, so `pointerUp` and `commitGesture`
 * never see any of this and cannot interfere with it.
 *
 * All four end in the same place: a node list, a box that tightly fits it, and
 * ONE `add_annotation`. A freehand stroke is simplified first, because the
 * points are the thing the user will later have to drag past to reach the one
 * they meant -- three hundred of them is a shape that cannot be edited, and the
 * fidelity nobody asked for is bought with the editability everybody does.
 *
 * The rubber band goes into the canvas's guide layer, never onto the surface:
 * `render()` replaces the surface wholesale on every document change, and the
 * shape being drawn is not in the document yet.
 */
class FigureShapeDrawing {

    constructor({ canvas }) {
        this.canvas = canvas;
        //: {mode, points[], hover, dragging, lastPress} while drawing. Null the
        //: rest of the time, which is what `active` and every handler test.
        this.state = null;
    }

    /** How near the first point a press has to land to close the path, and how
     *  near the start a freehand stroke has to end to close itself. In screen
     *  pixels, so the target is the same size at every zoom. */
    static get CLOSE_PX() { return 10; }

    /** Freehand: the shortest move that records a point. Sampling every
     *  pointermove records the pointer's jitter as geometry, and a stroke drawn
     *  slowly would carry ten times the points of the same stroke drawn fast. */
    static get SAMPLE_PX() { return 2; }

    /** Freehand: how far the simplified line may sit from the sampled one. */
    static get SIMPLIFY_PX() { return 1.5; }

    get active() { return Boolean(this.state); }

    // -- the pointer ---------------------------------------------------------

    /**
     * A press while a drawing mode is armed. Always consumes it.
     *
     * The three ways to finish a click-placed path are all decided here: a
     * press on the first point, a second press in the same place (this file's
     * own double-press check -- `FigureCanvas.secondPress` keys on an object
     * id, and there is no object yet), and Enter, which arrives in `keyDown`.
     */
    pointerDown(event, mode) {
        const point = this.placed(event);
        if (!this.state || this.state.mode !== mode) {
            this.state = { mode: mode, points: [point], hover: point,
                           dragging: mode === "freehand", lastPress: null };
            this.mark(event);
            this.preview();
            return true;
        }

        const state = this.state;
        const reach = this.canvas.toMm(FigureShapeDrawing.CLOSE_PX);
        const first = state.points[0];
        if (state.points.length >= 3 && mode !== "path"
            && Math.hypot(point.x - first.x, point.y - first.y) <= reach) {
            this.finish();
            return true;
        }
        if (this.isSecondPress(event)) {
            this.finish();
            return true;
        }
        state.points.push(point);
        state.hover = point;
        this.mark(event);
        this.preview();
        return true;
    }

    /** This file's own double-press check. `FigureCanvas.secondPress` cannot be
     *  reused: it identifies the press by the OBJECT under it, and the whole
     *  point of this mode is that there is not one yet. */
    isSecondPress(event) {
        const previous = this.state.lastPress;
        return Boolean(previous
            && event.timeStamp - previous.at < FigureCanvas.DOUBLE_PRESS_MS
            && Math.abs(event.clientX - previous.x) <= FigureCanvas.DOUBLE_PRESS_PX
            && Math.abs(event.clientY - previous.y) <= FigureCanvas.DOUBLE_PRESS_PX);
    }

    mark(event) {
        this.state.lastPress = { at: event.timeStamp, x: event.clientX, y: event.clientY };
    }

    /** Where a press puts a point. Shift holds the new edge to 45 degrees off
     *  the last one, which is how a bracket or an arrowhead gets drawn square
     *  without anybody measuring. Freehand ignores it -- the whole tool is the
     *  path the hand took. */
    placed(event) {
        const point = this.canvas.surfacePoint(event);
        if (!event.shiftKey || !this.state || this.state.dragging) return point;
        const from = this.state.points[this.state.points.length - 1];
        const delta = FigureShapeGeometry.constrainDelta(point.x - from.x, point.y - from.y);
        return { x: from.x + delta.x, y: from.y + delta.y };
    }

    pointerMove(event) {
        if (!this.state) return false;
        const point = this.placed(event);
        this.state.hover = point;
        if (this.state.dragging) {
            const last = this.state.points[this.state.points.length - 1];
            const step = this.canvas.toMm(FigureShapeDrawing.SAMPLE_PX);
            if (Math.hypot(point.x - last.x, point.y - last.y) >= step) {
                this.state.points.push(point);
            }
        }
        this.preview();
        return true;
    }

    pointerUp() {
        if (!this.state) return false;
        // Only freehand ends on a release. The click-placed modes are still
        // waiting for their next press, and consuming their pointerup would be
        // harmless but would hide that difference.
        if (!this.state.dragging) return true;
        this.finish();
        return true;
    }

    keyDown(event) {
        if (!this.state) return false;
        if (event.key === "Enter") {
            event.preventDefault();
            this.finish();
            return true;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            this.abandon();
            return true;
        }
        return false;
    }

    /**
     * Escape: throw away what has been drawn AND put the rail back.
     *
     * One press, not two. Escape means "I am not doing this" everywhere else on
     * this page, and a first press that only cleared the rubber band would
     * leave the mode armed and the rail lit with nothing to show for it -- so
     * the next click would start another polygon nobody asked for.
     */
    abandon() {
        this.cancel();
        this.canvas.setTool(null);
        this.canvas.onToolFinished();
    }

    /** Idempotent, and called from `FigureCanvas.setTool` on every tool change:
     *  a half-drawn polygon belongs to the tool that started it, so switching
     *  away has to end it rather than leave a rubber band chasing the pointer. */
    cancel() {
        if (!this.state) return;
        this.state = null;
        this.canvas.clearGuides();
    }

    // -- what it produces ----------------------------------------------------

    /**
     * Turn what has been drawn into one shape annotation, or cancel silently.
     *
     * Silently, because a two-click polygon is not an error the user needs told
     * about -- it is a gesture that did not become anything, and the modal that
     * said so would appear on every stray double-click.
     */
    finish() {
        const state = this.state;
        if (!state) return;
        this.state = null;
        this.canvas.clearGuides();

        const geometry = this.buildGeometry(state);
        this.canvas.setTool(null);
        this.canvas.onToolFinished();
        if (!geometry || !this.canvas.pageId) return;

        const annotation = {
            annotation_id: FigureSchema.newAnnotationId(),
            type: "shape",
            page_id: this.canvas.pageId,
            geometry: geometry.geometry,
            text: "",
            shape: { preset: "custom", closed: geometry.closed, nodes: geometry.nodes },
            style: this.canvas.drawStyle(),
            z: this.canvas.nextAnnotationZ(),
        };
        this.canvas.state.commit(
            [{ op: "add_annotation", annotation: annotation }],
            (draft) => { draft.annotations[annotation.annotation_id] = annotation; });
        this.canvas.select([annotation.annotation_id], false);
    }

    /** Points to nodes to a box, or null when there is not enough to be a
     *  shape. A closed path needs three points to enclose anything and an open
     *  one needs two to go anywhere. */
    buildGeometry(state) {
        let points = state.points;
        let closed = state.mode !== "path";

        if (state.mode === "freehand") {
            points = FigureShapeGeometry.rdp(
                points, this.canvas.toMm(FigureShapeDrawing.SIMPLIFY_PX));
            // A stroke that came back to where it started meant to be closed.
            // Deciding it here rather than asking is the difference between a
            // drawing tool and a form.
            const first = points[0];
            const last = points[points.length - 1];
            closed = points.length >= 4 && Math.hypot(last.x - first.x, last.y - first.y)
                <= this.canvas.toMm(FigureShapeDrawing.CLOSE_PX);
            if (closed) points = points.slice(0, -1);
        }
        if (points.length < (closed ? 3 : 2)) return null;

        // Curved modes go through Catmull-Rom, which produces the same node and
        // handle structure the point editor works in -- so what is drawn IS
        // what Edit Points opens, with no conversion between them.
        const smooth = state.mode === "curve" || state.mode === "freehand";
        const nodes = smooth
            ? FigureShapeGeometry.catmullRom(points, closed)
            : points.map((point) => ({ x: point.x, y: point.y, type: "corner",
                                       in: null, out: null }));

        // The points are in absolute page millimetres and `renormalize` works
        // in the box's local frame -- which, for a box at the origin with no
        // size and no rotation, is the same frame. So this both computes the
        // tight box and normalises the nodes into it, using the arithmetic the
        // point editor uses for the same job later.
        const placed = FigureShapeGeometry.renormalize(nodes, closed,
            { x_mm: 0, y_mm: 0, w_mm: 0, h_mm: 0, rotation: 0 });
        return { geometry: placed.geometry, nodes: placed.nodes, closed: closed };
    }

    // -- the rubber band -----------------------------------------------------

    preview() {
        const canvas = this.canvas;
        const state = this.state;
        if (!canvas.guideEl) return;
        if (!state) return canvas.clearGuides();

        const px = (point) => ({ x: canvas.toPx(point.x), y: canvas.toPx(point.y) });
        const placed = state.points.map(px);
        // The segment to the pointer is part of the preview for the click-
        // placed modes: it is what makes "where would the next edge go" legible
        // before the press that decides it.
        const trail = state.dragging || !state.hover
            ? placed : placed.concat([px(state.hover)]);
        const nodes = state.mode === "curve"
            ? FigureShapeGeometry.catmullRom(trail, false)
            : trail.map((point) => ({ x: point.x, y: point.y, type: "corner",
                                      in: null, out: null }));
        const d = FigureShapeGeometry.pathD(nodes, false);

        const reach = FigureShapeDrawing.CLOSE_PX;
        const closable = state.points.length >= 3 && state.mode !== "path"
            && state.hover && !state.dragging
            && Math.hypot(canvas.toPx(state.hover.x) - placed[0].x,
                          canvas.toPx(state.hover.y) - placed[0].y) <= reach;

        const dots = state.dragging ? "" : placed.map((point, index) =>
            `<circle class="fb-draw-node${index === 0 && closable ? " is-target" : ""}"
                     cx="${point.x}" cy="${point.y}" r="${index === 0 ? 5 : 3.5}"/>`).join("");

        canvas.guideEl.innerHTML =
            `<svg class="fb-draw-guide" width="1" height="1" overflow="visible">
                ${d ? `<path class="fb-draw-line" d="${d}"/>` : ""}${dots}
            </svg>`;
    }
}
