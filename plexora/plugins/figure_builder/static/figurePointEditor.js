/**
 * Edit Points: the nodes of one shape, and the bezier levers either side of
 * them.
 *
 * A mode, and mutually exclusive with the ordinary transform handles by design.
 * A node marker and a resize handle a few pixels apart, both live, is an
 * ambiguity nobody can aim through -- so while this is open the box's handles
 * are gone (`FigureCanvas.shapeFurniture` renders one or the other, never both)
 * and the shape is edited by its geometry rather than by its box.
 *
 * Three things here are less obvious than they look.
 *
 * **Entering converts a preset to a custom path, and that is one undoable
 * operation.** There is no state in between for anyone to be in, so there is no
 * decision to put in front of the user -- a dialog asking "convert this?" would
 * appear on every single use and its only honest answer would be yes. `preset`
 * becomes "custom" because the nodes no longer describe a pentagon, and Undo is
 * the way back.
 *
 * **The working copy is in LOCAL millimetres, and the box does not move while
 * it is being edited.** Nodes are stored normalised against the box, so
 * renormalising on every pointer move would move the box under the pointer on
 * every frame. Instead the drag runs in the box's own unrotated frame, the path
 * is allowed to hang outside the box for the length of the gesture, and the box
 * snaps back around the ink once -- on release, as one commit.
 *
 * **Every pointer delta is turned back through the rotation before it is
 * applied.** The local frame is tilted, so 2mm "right" on the screen is not 2mm
 * right in it. Getting this wrong is invisible on an upright shape and makes a
 * rotated one shear as it is dragged, which is why `resizedBox` does the same
 * thing and why the parity test uses 30 degrees.
 */
class FigurePointEditor {

    constructor({ canvas }) {
        this.canvas = canvas;
        this.annotationId = null;
        //: The nodes being edited, in the box's local unrotated millimetres.
        this.local = [];
        this.closed = true;
        this.selected = new Set();
        this.drag = null;
        //: Where a click on a segment would insert a node, while the pointer is
        //: over one. UI state, never stored.
        this.hover = null;
    }

    /** How near a segment the pointer counts as over it, in screen pixels.
     *  Matches the fat transparent hit path the markup lays over each edge. */
    static get SEGMENT_HIT_PX() { return 12; }

    get active() { return Boolean(this.annotationId); }

    get annotation() {
        return this.annotationId
            ? this.canvas.state.document.annotations[this.annotationId] : null;
    }

    // -- entering and leaving ------------------------------------------------

    enter(id) {
        const annotation = this.canvas.state.document.annotations[id];
        if (!annotation || annotation.type !== "shape" || !annotation.shape) return;
        if (this.annotationId && this.annotationId !== id) this.exit();

        this.annotationId = id;
        this.load();
        this.canvas.select([id], false);

        // The conversion, and the only thing that happens on entry. Committed
        // rather than deferred, so what the user is now editing is what the
        // document says it is -- and so Undo has something to go back to.
        if (annotation.shape.preset !== "custom") {
            this.commit({ shape: { preset: "custom", closed: this.closed,
                                   nodes: annotation.shape.nodes } });
        }
        this.canvas.render();
        this.canvas.onPointEditChange(id);
    }

    /**
     * Leave the mode.
     *
     * Nothing is ever pending: a drag commits on release and every discrete
     * action commits as it happens, so this only has to put the transform
     * handles back. That is deliberate -- an editor holding uncommitted
     * geometry would lose it to any of the dozen things that can end a mode.
     */
    exit() {
        if (!this.annotationId) return;
        this.annotationId = null;
        this.local = [];
        this.selected = new Set();
        this.drag = null;
        this.hover = null;
        this.canvas.render();
        this.canvas.onPointEditChange(null);
    }

    load() {
        this.syncLocal();
        this.selected = new Set();
    }

    /**
     * Take the working copy from the document, keeping the selection.
     *
     * Called on entry and again from `markup` whenever no drag is in flight.
     * That second call is what survives an UNDO: undo rewrites the shape with
     * nothing to tell this, and a working copy left holding the pre-undo nodes
     * would put the markers somewhere the path no longer is -- and the next
     * drag would then commit the undone geometry straight back.
     */
    syncLocal() {
        const annotation = this.annotation;
        if (!annotation || !annotation.shape) return;
        this.local = FigureShapeGeometry.localNodesMm(
            annotation.shape.nodes, annotation.geometry);
        this.closed = Boolean(annotation.shape.closed);
        const kept = new Set();
        for (const index of this.selected) {
            if (index < this.local.length) kept.add(index);
        }
        this.selected = kept;
    }

    // -- what the panel asks -------------------------------------------------

    get selectedCount() { return this.selected.size; }

    /** "corner", "smooth", or null when the selection disagrees -- which is
     *  what makes both buttons read as unpressed rather than one of them
     *  claiming a type half the nodes are not. */
    get selectedType() {
        let answer = null;
        for (const index of this.selected) {
            const type = this.local[index]?.type;
            if (answer === null) answer = type;
            else if (answer !== type) return null;
        }
        return answer;
    }

    get canDelete() {
        if (!this.selected.size) return false;
        return this.local.length - this.selected.size >= this.floor;
    }

    get canToggleClosed() {
        return this.closed || this.local.length >= 3;
    }

    /** A closed path needs three nodes to enclose anything; an open one needs
     *  two to go anywhere. Below that there is no shape left, so the operation
     *  simply does not happen -- no dialog, because a geometric constraint the
     *  user can see for themselves is not news. */
    get floor() { return this.closed ? 3 : 2; }

    // -- the pointer ---------------------------------------------------------

    /**
     * A press while this is open. Returns whether it was consumed.
     *
     * Priority: lever, node, segment, the shape's own body, then anything else.
     * Levers before nodes because a lever sits at the end of a short arm and is
     * routinely a few pixels from the node it belongs to, and the node is the
     * easier of the two to reach again.
     *
     * A press outside the shape returns FALSE after leaving the mode, so the
     * canvas goes on to handle it normally: clicking another object commits,
     * exits, and selects that object, rather than requiring Done first.
     */
    pointerDown(event) {
        if (!this.active) return false;
        const target = event.target;

        const lever = target.closest?.("[data-lever]");
        if (lever) {
            event.preventDefault();
            this.beginDrag(event, Number(lever.dataset.node), lever.dataset.lever);
            return true;
        }

        const node = target.closest?.("[data-node]");
        if (node) {
            event.preventDefault();
            const index = Number(node.dataset.node);
            if (event.shiftKey) {
                if (this.selected.has(index)) this.selected.delete(index);
                else this.selected.add(index);
            } else if (!this.selected.has(index)) {
                this.selected = new Set([index]);
            }
            this.beginDrag(event, index, null);
            return true;
        }

        const segment = target.closest?.("[data-segment]");
        if (segment) {
            event.preventDefault();
            this.insertOn(Number(segment.dataset.segment), this.pointIn(event));
            return true;
        }

        // Inside this shape but on none of its furniture: consumed and ignored,
        // so a stray press does not start dragging the whole object out of the
        // mode that is editing it.
        const own = target.closest?.(".fb-annotation-shape");
        if (own && own.dataset.annotationId === this.annotationId) {
            event.preventDefault();
            this.selected = new Set();
            this.redraw();
            this.canvas.onPointEditChange(this.annotationId);
            return true;
        }

        this.exit();
        return false;
    }

    beginDrag(event, index, lever) {
        const moving = lever || !this.selected.has(index)
            ? [index] : Array.from(this.selected);
        this.drag = {
            lever: lever,
            index: index,
            origin: this.canvas.surfacePoint(event),
            starts: moving.map((at) => ({ index: at, node: this.copy(this.local[at]) })),
        };
        this.redraw();
        this.canvas.onPointEditChange(this.annotationId);
    }

    pointerMove(event) {
        if (!this.active) return false;
        if (this.drag) {
            const at = this.canvas.surfacePoint(event);
            // Shift constrains in SCREEN space, not in the box's tilted frame:
            // it means "straight along the page", which is what the user is
            // looking at, and a shape rotated 30 degrees would otherwise snap
            // to axes that are nowhere on the screen.
            const delta = event.shiftKey
                ? FigureShapeGeometry.constrainDelta(
                    at.x - this.drag.origin.x, at.y - this.drag.origin.y)
                : { x: at.x - this.drag.origin.x, y: at.y - this.drag.origin.y };
            this.applyDrag(delta.x, delta.y);
            this.redraw();
            return true;
        }
        // Not consumed: with no drag in flight this is only watching for the
        // pointer crossing a segment, and swallowing the move would stop every
        // other hover on the page.
        this.trackHover(event);
        return false;
    }

    pointerUp() {
        if (!this.active || !this.drag) return false;
        this.drag = null;
        // One commit for the whole gesture, and the box snaps back around the
        // ink here rather than on every frame of it.
        this.commitLocal();
        return true;
    }

    keyDown(event) {
        if (!this.active) return false;
        if (event.key === "Escape") {
            event.preventDefault();
            this.exit();
            return true;
        }
        if (event.key === "Delete" || event.key === "Backspace") {
            event.preventDefault();
            // Consumed even when nothing is selected and even when the floor
            // refuses: while this mode is open, Delete means "this node", and
            // falling through to the canvas would delete the whole shape.
            this.deleteSelected();
            return true;
        }
        return false;
    }

    // -- editing -------------------------------------------------------------

    applyDrag(dx, dy) {
        const rotation = this.annotation?.geometry.rotation || 0;
        const turned = FigureShapeGeometry.turn(dx, dy, -rotation);
        const shift = (from) => (from
            ? { x: from.x + turned.x, y: from.y + turned.y } : null);

        if (this.drag.lever) {
            const node = this.local[this.drag.index];
            const start = this.drag.starts[0].node;
            const which = this.drag.lever;
            // The anchor stands in when there is no handle yet, so a lever
            // pulled off a bare node grows one from where the node is.
            node[which] = shift(start[which] || start);
            if (node.type === "smooth") this.mirror(node, which, start);
            return;
        }
        for (const entry of this.drag.starts) {
            const node = this.local[entry.index];
            const start = entry.node;
            node.x = start.x + turned.x;
            node.y = start.y + turned.y;
            // The handles travel with their anchor. They are absolute positions,
            // so leaving them behind would flatten the curve on every drag.
            node.in = shift(start.in);
            node.out = shift(start.out);
        }
    }

    /** Keep a smooth node's two levers opposite, each keeping its own LENGTH.
     *  Mirroring the length as well would make one side of a curve follow the
     *  other, which is a different control and not the one anybody expects. */
    mirror(node, which, start) {
        const other = which === "in" ? "out" : "in";
        const existing = start[other];
        if (!existing) return;
        const vx = node[which].x - node.x;
        const vy = node[which].y - node.y;
        const length = Math.hypot(vx, vy);
        if (!length) return;
        const keep = Math.hypot(existing.x - start.x, existing.y - start.y);
        node[other] = { x: node.x - vx / length * keep, y: node.y - vy / length * keep };
    }

    /**
     * Add a node where a segment was clicked.
     *
     * On a curve this is a De Casteljau subdivision rather than a point dropped
     * on the chord: the two halves of a split cubic ARE the original curve, so
     * the shape does not change at the moment the user asks to edit it.
     */
    insertOn(index, at) {
        const nodes = this.local;
        const start = nodes[index];
        const end = nodes[(index + 1) % nodes.length];
        if (!start || !end) return;
        const near = FigureShapeGeometry.nearestOnSegment(start, end, at);
        const split = FigureShapeGeometry.splitSegment(start, end, near.t);
        if (split.startOut) start.out = split.startOut;
        if (split.endIn) end.in = split.endIn;
        nodes.splice(index + 1, 0, split.node);
        this.selected = new Set([index + 1]);
        this.hover = null;
        this.commitLocal();
    }

    deleteSelected() {
        if (!this.canDelete) return;
        const keep = this.local.filter((node, index) => !this.selected.has(index));
        this.local = keep;
        this.selected = new Set();
        this.commitLocal();
    }

    setType(type) {
        if (!this.selected.size) return;
        for (const index of this.selected) {
            const node = this.local[index];
            if (!node) continue;
            node.type = type === "smooth" ? "smooth" : "corner";
            // Becoming smooth with no levers to be smooth ABOUT is a no-op the
            // user cannot see, so the conversion makes them -- along the line
            // between the two neighbours, which is the same tangent
            // `catmullRom` uses and therefore the curve they already expect.
            if (node.type === "smooth" && !node.in && !node.out) this.growLevers(index);
        }
        this.commitLocal();
    }

    growLevers(index) {
        const nodes = this.local;
        const count = nodes.length;
        const node = nodes[index];
        const first = !this.closed && index === 0;
        const last = !this.closed && index === count - 1;
        const previous = index === 0 ? nodes[count - 1] : nodes[index - 1];
        const next = index === count - 1 ? nodes[0] : nodes[index + 1];
        const tx = (next.x - previous.x) / 6;
        const ty = (next.y - previous.y) / 6;
        node.in = first ? null : { x: node.x - tx, y: node.y - ty };
        node.out = last ? null : { x: node.x + tx, y: node.y + ty };
    }

    /** Break the edge between the last node and the first, or put it back. The
     *  fill is not touched: `compose` and `shapeMarkup` both decline to draw one
     *  on an open path, so closing it again brings the colour back. */
    toggleClosed() {
        if (!this.canToggleClosed) return;
        this.closed = !this.closed;
        this.commitLocal();
    }

    // -- committing ----------------------------------------------------------

    /** The working copy, back into the document as ONE operation.
     *
     *  `geometry` and `shape` go together and always in the same op: the nodes
     *  are normalised against the box, so a write that changed one without the
     *  other would be a shape whose coordinates mean something different from
     *  what they meant a moment ago. */
    commitLocal() {
        const annotation = this.annotation;
        if (!annotation) return;
        const placed = FigureShapeGeometry.renormalize(
            this.local, this.closed, annotation.geometry);
        this.commit({
            geometry: placed.geometry,
            shape: { preset: "custom", closed: this.closed, nodes: placed.nodes },
        });
        // The box just moved, so the local frame did too. Reload rather than
        // keep editing against the frame that has gone.
        this.local = FigureShapeGeometry.localNodesMm(placed.nodes, placed.geometry);
        this.canvas.onPointEditChange(this.annotationId);
    }

    commit(changes) {
        const id = this.annotationId;
        this.canvas.state.commit(
            [{ op: "update_annotation", annotation_id: id, changes: changes }],
            (draft) => {
                const annotation = draft.annotations[id];
                if (!annotation) return;
                if (changes.geometry) Object.assign(annotation.geometry, changes.geometry);
                if (changes.shape) annotation.shape = changes.shape;
            });
    }

    // -- drawing -------------------------------------------------------------

    /** The pointer, in the box's local unrotated millimetres. */
    pointIn(event) {
        const annotation = this.annotation;
        const at = this.canvas.surfacePoint(event);
        const geometry = annotation.geometry;
        const centre = { x: geometry.x_mm + geometry.w_mm / 2,
                         y: geometry.y_mm + geometry.h_mm / 2 };
        const turned = FigureShapeGeometry.turn(
            at.x - centre.x, at.y - centre.y, -(geometry.rotation || 0));
        return { x: turned.x + geometry.w_mm / 2, y: turned.y + geometry.h_mm / 2 };
    }

    trackHover(event) {
        const segment = event.target.closest?.("[data-segment]");
        const previous = this.hover;
        if (!segment) {
            this.hover = null;
            if (previous) this.redraw();
            return;
        }
        const index = Number(segment.dataset.segment);
        const start = this.local[index];
        const end = this.local[(index + 1) % this.local.length];
        if (!start || !end) return;
        const near = FigureShapeGeometry.nearestOnSegment(start, end, this.pointIn(event));
        this.hover = { index: index, point: near.point };
        // Redrawn only when the dot would actually move on screen: the pointer
        // reports sub-pixel moves and this rewrites an element's markup.
        const moved = !previous || previous.index !== index
            || Math.round(this.canvas.toPx(previous.point.x))
               !== Math.round(this.canvas.toPx(near.point.x))
            || Math.round(this.canvas.toPx(previous.point.y))
               !== Math.round(this.canvas.toPx(near.point.y));
        if (moved) this.redraw();
    }

    /**
     * Rewrite just this shape's element.
     *
     * Not `canvas.render()`, which replaces the whole surface: that would
     * rebuild every panel image on every pointer move and turn a node drag into
     * a slideshow. Same reason, and same technique, as `previewMove`.
     */
    redraw() {
        const annotation = this.annotation;
        if (!annotation || !this.canvas.surfaceEl) return;
        const element = this.canvas.surfaceEl.querySelector(
            `[data-annotation-id="${this.annotationId}"]`);
        if (!element) return;
        element.outerHTML = this.canvas.annotationMarkup(this.draft(annotation));
    }

    /** The annotation as it currently looks, with the working nodes normalised
     *  against the box they are STILL in -- coordinates outside [0, 1] and all.
     *  The svg does not clip, so a node dragged outside its box draws where it
     *  was dragged, and the box catches up on release. */
    draft(annotation) {
        const geometry = annotation.geometry;
        const place = (point) => (point
            ? { x: point.x / geometry.w_mm, y: point.y / geometry.h_mm } : null);
        return { ...annotation, shape: {
            preset: "custom", closed: this.closed,
            nodes: this.local.map((node) => ({
                x: node.x / geometry.w_mm, y: node.y / geometry.h_mm, type: node.type,
                in: place(node.in), out: place(node.out),
            })),
        } };
    }

    /**
     * The node furniture, drawn over the shape instead of its resize handles.
     *
     * Two layers, because they are measured in different things. The segment
     * hit paths and the lever arms are geometry and go in an svg in the box's
     * own PIXEL space; the markers are chrome and are absolutely positioned
     * spans of a fixed size. That is what keeps a node ~9px on screen at every
     * zoom without any scaling arithmetic: the surface is laid out in pixels by
     * `toPx` and never CSS-scaled, so a fixed px size simply stays fixed.
     */
    markup(annotation) {
        // Positions come from the annotation being DRAWN, never from the
        // working copy. During a drag that argument is the draft, so the
        // markers follow the pointer; during an ordinary render it is the
        // document, so an undo moves them. Reading `this.local` instead would
        // leave the markers behind whenever anything changed the shape from
        // outside this file -- which undo does, on every press.
        if (!this.drag) this.syncLocal();
        const canvas = this.canvas;
        const geometry = annotation.geometry;
        const px = (value) => canvas.toPx(value);
        const place = (point) => ({ x: px(point.x * geometry.w_mm),
                                    y: px(point.y * geometry.h_mm) });
        const source = annotation.shape.nodes;
        const closed = Boolean(annotation.shape.closed);
        const nodes = source.map((node) => ({
            ...place(node), type: node.type,
            in: node.in ? place(node.in) : null,
            out: node.out ? place(node.out) : null,
        }));
        if (nodes.length < 2) return "";

        const edges = FigureShapeGeometry.edges(nodes, closed).map(([start, end], index) =>
            `<path class="fb-segment-hit" data-segment="${index}"
                   d="${FigureShapeGeometry.pathD([start, end], false)}"
                   stroke-width="${FigurePointEditor.SEGMENT_HIT_PX}"/>`).join("");

        const arms = [];
        const levers = [];
        for (const index of this.selected) {
            const node = nodes[index];
            if (!node) continue;
            for (const which of ["in", "out"]) {
                if (!node[which]) continue;
                arms.push(`<line class="fb-node-arm" x1="${node.x}" y1="${node.y}"
                                 x2="${node[which].x}" y2="${node[which].y}"/>`);
                levers.push(`<span class="fb-node-lever" data-node="${index}"
                                   data-lever="${which}"
                                   style="left:${node[which].x}px;top:${node[which].y}px"></span>`);
            }
        }

        const dot = this.hover
            ? `<span class="fb-node-insert"
                     style="left:${px(this.hover.point.x)}px;top:${px(this.hover.point.y)}px"></span>`
            : "";

        const markers = nodes.map((node, index) =>
            `<span class="fb-node${node.type === "smooth" ? " is-smooth" : ""}${
                this.selected.has(index) ? " is-picked" : ""}" data-node="${index}"
                   style="left:${node.x}px;top:${node.y}px"></span>`).join("");

        return `<svg class="fb-node-layer" width="${px(geometry.w_mm)}"
                     height="${px(geometry.h_mm)}" overflow="visible"
                >${edges}${arms.join("")}</svg>${markers}${levers.join("")}${dot}`;
    }

    copy(node) {
        return { x: node.x, y: node.y, type: node.type,
                 in: node.in ? { x: node.in.x, y: node.in.y } : null,
                 out: node.out ? { x: node.out.x, y: node.out.y } : null };
    }
}
