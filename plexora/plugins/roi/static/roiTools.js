/**
 * roiTools.js - what the pointer and the keyboard do.
 *
 * The whole file turns on one variable, `this.state`, holding one of:
 *
 *     idle.select        drawing.polygon      editing.vertex
 *     panning.temporary  drawing.freehand     editing.move
 *                        drawing.rectangle
 *
 * Written as a state machine rather than the obvious set of booleans
 * (isDrawing, isDragging, isMoving...) because those combinations multiply and
 * most of them are meaningless. "Dragging while drawing while panning" has no
 * behaviour anyone intended, but a handler reading three flags has to have one.
 *
 * ## Sharing the pointer with the viewer
 *
 * OpenSeadragon wants the same events this does. The precedence is explicit
 * rather than left to whichever handler happens to run first:
 *
 *   wheel                 always zooms. Never intercepted, at any time -- a
 *                         drawing tool that breaks zoom on a 100,000px slide
 *                         is a drawing tool nobody can use.
 *   Space + drag          always pans, whatever tool is selected.
 *   drag on empty image   pans (Select tool) -- so navigation still works
 *                         without switching tools.
 *   drag on a shape       moves it.
 *   drag on a handle      moves that vertex.
 *   drag with a draw tool draws.
 *
 * Suppression is per event (`event.preventDefaultAction = true`) rather than
 * `viewer.setMouseNavEnabled(false)`, because the decision is per gesture: the
 * same drag pans or draws depending on what is under it and what is held, and a
 * viewer-wide switch cannot express that without being toggled from six places
 * and eventually being left in the wrong position.
 *
 * The gesture is decided at press and does not change mid-drag. Pressing Space
 * halfway through drawing a stroke does not turn that stroke into a pan.
 */
class RoiInteraction {

    constructor(ctx, store, renderer) {
        this.ctx = ctx;
        this.store = store;
        this.renderer = renderer;
        this.viewer = ctx.viewer?.viewer || null;

        this.tool = "select";
        this.state = "idle.select";
        this.armed = false;

        this.draftPoints = [];
        this.drag = null;           // {id, origin|vertex, before} once a drag is committed
        //: What a press landed on, before it is known whether this gesture is a
        //: click or a drag. Resolved by whichever of the two arrives next.
        this.pending = null;
        this.stateBeforeSpace = null;
        this.spaceHeld = false;

        //: What the pointer is over, plus the frame handle throttling the moves
        //: that decide it. Kept outside the state machine on purpose: hovering
        //: never edits anything, so giving it states of its own would multiply
        //: the combinations above for no behaviour anyone wanted.
        this.hoverId = null;
        this._hoverGeometry = null;
        this._hoverPosition = null;
        this._hoverFrame = 0;
        this._anchorFrame = 0;
        this._hoverTracker = null;
        this._unsubscribeStore = null;

        this._handlers = [];
        this._onKeyDown = (event) => this.keyDown(event);
        this._onKeyUp = (event) => this.keyUp(event);
        this._onBlur = () => this.releaseSpace();
    }

    //: How close, in SCREEN pixels, counts as "on" a vertex or an edge.
    //: Converted to image pixels per gesture, because a fixed image-pixel
    //: tolerance is unusably tight zoomed out and absurdly loose zoomed in.
    static get GRAB_RADIUS() { return 9; }

    //: Smallest shape worth keeping, in SCREEN pixels squared. A stray click
    //: sequence makes a region a few pixels across that is invisible, unhittable
    //: and permanent. Measured on screen rather than in image pixels because
    //: there is no such thing as a biologically-too-small region -- only one too
    //: small to have been drawn on purpose at this zoom.
    static get MIN_AREA() { return 24; }

    //: Freehand simplification tolerance, in screen pixels. Roughly "keep the
    //: shape within a pixel and a half of what was drawn, at the zoom it was
    //: drawn at".
    static get SIMPLIFY_EPSILON() { return 1.5; }

    // -- lifecycle -------------------------------------------------------

    /** Start listening. Called when the panel is shown, not when the plugin
     *  loads: a hidden panel whose shortcuts still fire is a tool acting on a
     *  window the user is not looking at. */
    arm() {
        if (this.armed || !this.viewer) return;
        this.armed = true;

        const on = (name, fn) => {
            this.viewer.addHandler(name, fn);
            this._handlers.push([name, fn]);
        };
        on("canvas-press", (e) => this.press(e));
        on("canvas-drag", (e) => this.dragging(e));
        on("canvas-drag-end", (e) => this.dragEnd(e));
        on("canvas-click", (e) => this.click(e));
        on("canvas-double-click", (e) => this.doubleClick(e));

        // Whether the tools are usable depends on the world having an image in
        // it, and that is not a fact about the annotations -- so no store change
        // ever announces it. A panel opened while the first tile is still on the
        // way would otherwise render its toolbar disabled and stay that way
        // until some unrelated edit happened to repaint it, which on a project
        // that already has categories is never.
        this._onWorldChange = () => {
            this.applyCursor();
            this.store.changed();
        };
        this.viewer.world?.addHandler("add-item", this._onWorldChange);
        this.viewer.world?.addHandler("remove-item", this._onWorldChange);

        // Hover is tracked apart from the canvas-* gestures above because OSD
        // does not report a plain move through them, and because a hover is an
        // observation rather than an edit -- it never touches the store.
        this._hoverTracker = new OpenSeadragon.MouseTracker({
            element: this.viewer.canvas,
            moveHandler: (event) => this.pointerMove(event),
            leaveHandler: () => this.clearHover(),
        });

        // A standing hover survives the picture moving under it, so the anchor
        // that went out with it has to be re-sent -- see reanchorHover.
        on("viewport-change", () => this.viewportMoved());

        // A hovered ROI can be deleted, hidden or reshaped from the panel while
        // the pointer sits perfectly still, and no pointer event will say so.
        this._unsubscribeStore = this.store.onChange?.(() => this.revalidateHover());

        document.addEventListener("keydown", this._onKeyDown);
        document.addEventListener("keyup", this._onKeyUp);
        window.addEventListener("blur", this._onBlur);

        this.renderer.setEnabled(true);
        this.applyCursor();
    }

    /** Stop listening and cancel anything half-drawn.
     *
     * Symmetrical with arm() and called on every switch away, because the
     * listeners above are on the VIEWER and the DOCUMENT -- neither of which
     * goes away when this plugin's panel is hidden. Left attached, ROI would go
     * on drawing over another tool's session and swallowing its keystrokes. */
    disarm() {
        if (!this.armed) return;
        this.armed = false;

        this.cancelDraft();
        for (const [name, fn] of this._handlers) this.viewer.removeHandler(name, fn);
        this._handlers = [];

        if (this._onWorldChange) {
            this.viewer.world?.removeHandler("add-item", this._onWorldChange);
            this.viewer.world?.removeHandler("remove-item", this._onWorldChange);
            this._onWorldChange = null;
        }

        document.removeEventListener("keydown", this._onKeyDown);
        document.removeEventListener("keyup", this._onKeyUp);
        window.removeEventListener("blur", this._onBlur);

        // clearHover() before the tracker goes, so anything listening for the
        // leave hears it -- a panel that closes with a summary card still
        // floating over the image has left litter nothing else will clean up.
        this.clearHover();
        this._hoverTracker?.destroy?.();
        this._hoverTracker = null;
        this._unsubscribeStore?.();
        this._unsubscribeStore = null;

        this.releaseSpace();
        this.state = "idle.select";
        this.renderer.setEnabled(false);
        this.setCursor("");
    }

    destroy() {
        this.disarm();
    }

    // -- coordinates -----------------------------------------------------

    /**
     * Screen position -> full-resolution image pixel.
     *
     * Through the tile source's own getImagePixel where it exists (viewerManager
     * installs it on every channel), which already accounts for extraZoomLevels
     * -- the pyramid's extra levels, currently 0, so the divide is by 1 today
     * and correct if that ever changes. The longhand below is the same
     * arithmetic for a source that predates it.
     */
    toImage(position) {
        const item = this.viewer?.world?.getItemAt(0);
        if (!item) return null;
        if (item.source && typeof item.source.getImagePixel === "function") {
            const [x, y] = item.source.getImagePixel(item, position);
            return [x, y];
        }
        const viewportPoint = this.viewer.viewport.pointFromPixel(position);
        const imagePoint = item.viewportToImageCoordinates(viewportPoint);
        const scale = 2 ** (this.ctx.config?.extraZoomLevels || 0);
        return [imagePoint.x / scale, imagePoint.y / scale];
    }

    /** Screen pixels -> image pixels at the current zoom, for tolerances. */
    imageDistance(screenPixels) {
        try {
            const item = this.viewer.world.getItemAt(0);
            const zoom = item.viewportToImageZoom(this.viewer.viewport.getZoom(true));
            return zoom > 0 ? screenPixels / zoom : screenPixels;
        } catch (error) {
            return screenPixels;
        }
    }

    /** Confine a drawn point to the image.
     *
     * Read off config every time rather than cached: `__plexora.refreshDataset`
     * mutates that object in place when the project's read spec changes, and a
     * cached copy would silently go stale. */
    clamp(point) {
        return RoiGeometry.clamp(point, this.ctx.config?.width, this.ctx.config?.height);
    }

    /** Can the pointer do anything on the image right now? */
    get ready() {
        if (!this.store.editable) return false;
        // No active channel means no tiled image, hence no coordinate frame to
        // draw in. Drawing is disabled rather than silently producing garbage.
        return Boolean(this.viewer?.world?.getItemCount());
    }

    /**
     * Can a NEW shape be made right now?
     *
     * Separate from `ready` because selecting, undoing and deleting must keep
     * working with no categories -- undoing the deletion of the last one is
     * exactly when a user needs them to. A project starts with none (the user
     * names their own), so the draw tools wait for one instead of filing the
     * shape under a label nobody chose.
     */
    get canDraw() {
        return this.ready && Boolean(this.store.activeCategory);
    }

    // -- tools -----------------------------------------------------------

    setTool(tool) {
        if (this.tool === tool) return;
        this.cancelDraft();
        this.tool = tool;
        this.state = tool === "select" ? "idle.select" : `drawing.${tool}`;
        this.applyCursor();
        this.store.changed();
        if (tool !== "select" && !this.store.activeCategory) this.needCategory();
    }

    /** Say why nothing happens, once, rather than letting clicks vanish. */
    needCategory() {
        this.notify("Add a category first -- every region belongs to one.");
    }

    setCursor(cursor) {
        if (this.viewer?.canvas) this.viewer.canvas.style.cursor = cursor;
    }

    applyCursor() {
        if (!this.armed) return;
        if (this.spaceHeld) return this.setCursor("grab");
        if (!this.ready) return this.setCursor("");
        // A crosshair over an image that cannot take a shape is a promise the
        // pointer does not keep.
        this.setCursor(this.tool !== "select" && this.canDraw ? "crosshair" : "default");
    }

    // -- pointer ---------------------------------------------------------

    press(event) {
        if (!this.ready) return;
        if (this.spaceHeld) {
            this.state = "panning.temporary";
            return;
        }

        const point = this.toImage(event.position);
        if (!point) return;

        if (this.tool === "select") {
            // Only RECORD what is under the pointer. Which gesture this is --
            // a click that selects, or a drag that moves -- is not known yet,
            // and committing to "move" here is what made a plain click leave
            // the machine in editing.move: `canvas-click` is guarded on
            // idle.select, so it never ran, and clicking a second shape or
            // clicking empty space to deselect both silently did nothing.
            // The gesture is decided on the first canvas-drag instead.
            this.drag = null;
            this.pending = { hit: this.hitTest(point), point };
            this.state = "idle.select";
            return;
        }

        if (!this.canDraw) {
            // Back to a state a drag cannot act on. setTool put the machine in
            // `drawing.<tool>` optimistically, and the rectangle case reads a
            // drag origin this refused press never set.
            this.state = "idle.select";
            return this.needCategory();
        }

        if (this.tool === "freehand") {
            this.state = "drawing.freehand";
            this.draftPoints = [this.clamp(point)];
            this.showDraft("freehand");
        } else if (this.tool === "rectangle") {
            this.state = "drawing.rectangle";
            this.drag = { origin: this.clamp(point) };
            this.draftPoints = [];
        }
    }

    dragging(event) {
        if (!this.armed) return;
        if (this.spaceHeld || this.state === "panning.temporary") return;  // viewer pans

        const point = this.toImage(event.position);
        if (!point) return;

        // First movement of a press that landed on something: NOW it is a drag,
        // so commit to which kind. A press that never moves stays idle.select
        // and is handled as a click.
        if (this.state === "idle.select" && this.pending) {
            const { hit, point: origin } = this.pending;
            this.pending = null;
            if (hit && hit.vertex) {
                this.state = "editing.vertex";
                this.drag = { id: hit.feature.id, vertex: hit.vertex, before: hit.feature.geometry };
            } else if (hit) {
                this.state = "editing.move";
                this.drag = { id: hit.feature.id, origin, before: hit.feature.geometry };
            }
            // Nothing under the pointer: left alone, so the viewer pans and
            // navigation keeps working without leaving the Select tool.
        }

        // Three of the cases below read a gesture the press set up, and the
        // gesture can be cancelled while the button is still down: Esc, a tool
        // shortcut and a re-arm all call cancelDraft, which nulls `drag` but
        // leaves `drawing.rectangle`/`editing.*` in place -- and the mouse goes
        // on sending canvas-drag the whole time. Without this the next movement
        // is a null dereference in a pointer handler.
        const needsGesture = this.state === "editing.vertex"
            || this.state === "editing.move"
            || this.state === "drawing.rectangle";
        if (needsGesture && !this.drag) return;

        switch (this.state) {
            case "editing.vertex": {
                event.preventDefaultAction = true;
                const feature = this.store.feature(this.drag.id);
                if (!feature) return;
                const rings = feature.geometry.coordinates.map((ring) => ring.slice());
                const points = RoiGeometry.openRing(rings[this.drag.vertex.ring]);
                points[this.drag.vertex.index] = this.clamp(point);
                rings[this.drag.vertex.ring] = RoiGeometry.closeRing(points);
                // Local only: the server hears about this once, at drag end.
                feature.geometry = { type: feature.geometry.type, coordinates: rings };
                this.renderer.invalidate(feature.id);
                this.renderer.schedule();
                return;
            }
            case "editing.move": {
                event.preventDefaultAction = true;
                const feature = this.store.feature(this.drag.id);
                if (!feature) return;
                feature.geometry = RoiGeometry.translate(
                    this.drag.before, point[0] - this.drag.origin[0], point[1] - this.drag.origin[1]);
                this.renderer.invalidate(feature.id);
                this.renderer.schedule();
                return;
            }
            case "drawing.freehand": {
                event.preventDefaultAction = true;
                this.draftPoints.push(this.clamp(point));
                this.showDraft("freehand");
                return;
            }
            case "drawing.rectangle": {
                event.preventDefaultAction = true;
                const [x0, y0] = this.drag.origin;
                const [x1, y1] = this.clamp(point);
                this.draftPoints = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
                this.showDraft("rectangle");
                return;
            }
            case "drawing.polygon":
                // A polygon is built from clicks; a drag while one is in
                // progress is the user repositioning the view.
                return;
            default:
                return;
        }
    }

    dragEnd(event) {
        if (!this.armed) return;

        switch (this.state) {
            case "editing.vertex":
            case "editing.move": {
                event.preventDefaultAction = true;   // no flick-momentum pan
                const feature = this.store.feature(this.drag?.id);
                const before = this.drag?.before;
                this.drag = null;
                this.state = "idle.select";
                if (!feature || !before) return;
                // One operation, and so one undo step, for a drag of any length.
                this.commitGeometry(feature, before, feature.geometry);
                return;
            }
            case "drawing.freehand": {
                event.preventDefaultAction = true;
                this.finishFreehand();
                return;
            }
            case "drawing.rectangle": {
                event.preventDefaultAction = true;
                const points = this.draftPoints.slice();
                this.drag = null;
                this.hideDraft();
                this.state = "drawing.rectangle";
                this.createFrom(points);
                return;
            }
            default:
                this.state = this.tool === "select" ? "idle.select" : `drawing.${this.tool}`;
        }
    }

    click(event) {
        if (!this.ready || this.spaceHeld) return;
        const point = this.toImage(event.position);
        if (!point) return;

        if (this.tool === "polygon") {
            if (!this.canDraw) return this.needCategory();
            event.preventDefaultAction = true;
            this.state = "drawing.polygon";
            this.draftPoints.push(this.clamp(point));
            this.showDraft("polygon");
            return;
        }

        if (this.tool === "select" && this.state === "idle.select") {
            // Suppressed so a click to select does not also zoom the viewer.
            // Double-click and the wheel still zoom, which is where the user
            // reaches for zoom anyway.
            event.preventDefaultAction = true;
            // Reuse what the press already found -- same point, same answer --
            // rather than hit-testing the whole image a second time.
            const hit = this.pending ? this.pending.hit : this.hitTest(point);
            this.pending = null;
            this.store.select(hit ? hit.feature.id : null);
            this.renderer.schedule();
        }
    }

    doubleClick(event) {
        if (this.tool === "polygon" && this.draftPoints.length) {
            event.preventDefaultAction = true;
            this.finishPolygon();
        }
    }

    // -- hit testing -----------------------------------------------------

    /** What is under this image-space point: a vertex of the selection first,
     *  then a shape. Hidden categories are excluded because they are excluded
     *  from `visibleFeatures` -- there is one list, so an invisible shape can
     *  never be clicked by accident. */
    hitTest(point) {
        const tolerance = this.imageDistance(RoiInteraction.GRAB_RADIUS);
        const selected = this.store.selected;

        if (selected && this.store.isVisible(selected) && !this.store.isLocked(selected)
            && RoiGeometry.isVertexEditable(selected.geometry)) {
            const vertex = RoiGeometry.nearestVertex(
                selected.geometry, point[0], point[1], tolerance);
            if (vertex) return { feature: selected, vertex };
        }

        const candidates = this.store.visibleFeatures();
        // Back to front, so the shape drawn on top is the one picked -- which is
        // what the user sees and therefore what they mean.
        for (let i = candidates.length - 1; i >= 0; i--) {
            const feature = candidates[i];
            if (RoiGeometry.containsPoint(feature.geometry, point[0], point[1])
                || RoiGeometry.distanceToBoundary(feature.geometry, point[0], point[1]) <= tolerance) {
                return { feature, vertex: null };
            }
        }
        return null;
    }

    // -- hover -------------------------------------------------------------

    /**
     * Which ROI the pointer is over, published for whoever wants to say
     * something about it.
     *
     * This plugin deliberately does not know what a composition summary is. It
     * owns geometry, so it answers "which region, and where is it on screen";
     * Cell Explorer's bridge answers "what is inside it". The two CustomEvents
     * below are the entire seam between them -- which is why the payload
     * carries geometry and an anchor rectangle rather than a live feature
     * reference, so neither side ends up reaching into the other's state.
     *
     * Tracked with a MouseTracker rather than another viewer handler because
     * OSD's canvas-* events have no plain move: canvas-drag is a gesture.
     */
    pointerMove(event) {
        if (!this.armed) return;
        // Mid-gesture there is no such thing as hovering. A committed drag, a
        // half-placed draft or a Space-pan all mean the pointer is busy, and
        // lighting up whatever it crosses would be noise laid over the gesture.
        if (this.drag || this.spaceHeld || this.draftPoints.length) {
            this.clearHover();
            return;
        }
        if (!event || !event.position) return;
        this._hoverPosition = event.position;
        // One hit test per frame at most: pointer moves arrive far faster than
        // anything downstream can redraw, and the answer cannot change more
        // often than the picture does.
        if (this._hoverFrame) return;
        this._hoverFrame = requestAnimationFrame(() => {
            this._hoverFrame = 0;
            this.resolveHover();
        });
    }

    resolveHover() {
        if (!this.armed || !this._hoverPosition) return;
        const point = this.toImage(this._hoverPosition);
        if (!point) return;
        const hit = this.hitTest(point);
        this.setHover(hit ? hit.feature : null);
    }

    /** Enter and leave, deduplicated. Moving around inside one ROI is not a
     *  succession of hovers, and re-announcing it every frame is exactly the
     *  cursor-chasing an anchored card is meant to avoid. */
    setHover(feature) {
        const id = feature ? feature.id : null;
        if (id === this.hoverId) return;
        if (this.hoverId) this.dispatchUnhover(this.hoverId);
        this.hoverId = id;
        // The renderer already knows how to emphasise a hovered shape: a second
        // stroke over the top, which leaves the saved colour and width alone.
        this.renderer.hoverId = id;
        this.renderer.schedule();
        if (feature) this.dispatchHover(feature);
    }

    clearHover() {
        if (this._hoverFrame) {
            cancelAnimationFrame(this._hoverFrame);
            this._hoverFrame = 0;
        }
        if (this._anchorFrame) {
            cancelAnimationFrame(this._anchorFrame);
            this._anchorFrame = 0;
        }
        this._hoverPosition = null;
        this.setHover(null);
    }

    /**
     * The picture moved while the pointer was inside an ROI.
     *
     * Anchors go out in client pixels, so a pan or a zoom leaves whoever is
     * showing something beside a region pointing at empty image. The obvious
     * answer -- have them close on the first viewport change -- is the wrong
     * one: the pointer has not moved, so no further enter is ever dispatched,
     * and the region has to be left and re-entered before anything can be seen
     * again. That reads as a hover the tool simply missed. Re-announcing the
     * hover with a fresh anchor keeps the two together instead.
     *
     * One dispatch per frame at most, because OSD reports a viewport change on
     * every frame of a spring and a hover cannot change more often than the
     * picture does.
     */
    viewportMoved() {
        if (!this.hoverId || this._anchorFrame) return;
        this._anchorFrame = requestAnimationFrame(() => {
            this._anchorFrame = 0;
            this.reanchorHover();
        });
    }

    reanchorHover() {
        if (!this.armed || !this.hoverId) return;
        const before = this.hoverId;
        // Zooming can slide a shape out from under a pointer that never moved,
        // so what is under it is asked again rather than assumed. setHover
        // announces the change itself, anchor included, when there is one.
        if (this._hoverPosition) {
            const point = this.toImage(this._hoverPosition);
            if (point) this.setHover(this.hitTest(point)?.feature || null);
        }
        if (!this.hoverId || this.hoverId !== before) return;
        const feature = this.store.feature(this.hoverId);
        if (feature) this.dispatchHover(feature);
    }

    /**
     * The store changed under a stationary pointer.
     *
     * Deleting, hiding or reshaping an ROI produces no pointer event, so
     * without this a summary card would go on describing a region that has left
     * the screen, or describe the old outline of one that just moved. Geometry
     * objects are replaced rather than mutated on every edit, so object
     * identity is all it takes to tell a reshape from a rename.
     */
    revalidateHover() {
        if (!this.hoverId) return;
        const feature = this.store.feature(this.hoverId);
        if (!feature || !this.store.isVisible(feature)) {
            const gone = this.hoverId;
            this.hoverId = null;
            this.renderer.hoverId = null;
            this.renderer.schedule();
            this.dispatchUnhover(gone);
            return;
        }
        if (feature.geometry !== this._hoverGeometry) this.dispatchHover(feature);
    }

    dispatchHover(feature) {
        this._hoverGeometry = feature.geometry;
        this.emit("plexora:roi-hover", {
            id: feature.id,
            name: feature.name || "",
            categoryId: feature.category_id === undefined ? null : feature.category_id,
            geometry: feature.geometry,
            anchorRect: this.screenRect(feature.geometry),
            viewportRect: this.canvasRect(),
        });
    }

    dispatchUnhover(id) {
        this._hoverGeometry = null;
        this.emit("plexora:roi-unhover", { id });
    }

    emit(name, detail) {
        try {
            window.dispatchEvent(new CustomEvent(name, { detail }));
        } catch (error) {
            console.error(`roiTools: could not dispatch ${name}`, error);
        }
    }

    /** The image canvas in client coordinates, so a listener can keep whatever
     *  it puts on screen inside the picture rather than inside the window. */
    canvasRect() {
        const box = this.viewer?.canvas?.getBoundingClientRect?.();
        if (!box) return null;
        return { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
    }

    /**
     * An ROI's bounding box in CLIENT pixels.
     *
     * Client rather than container coordinates because a listener has no
     * business knowing which element the viewer happens to be mounted in, and
     * because whatever it anchors there can then be positioned outside the
     * viewer's subtree, where no ancestor's overflow can clip it.
     *
     * Taken once, when the ROI is entered -- see setHover.
     */
    screenRect(geometry) {
        const item = this.viewer?.world?.getItemAt(0);
        const box = geometry ? RoiGeometry.bounds(geometry) : null;
        const canvas = this.canvasRect();
        if (!item || !box || !canvas) return null;
        const scale = 2 ** (this.ctx.config?.extraZoomLevels || 0);
        const rect = item.imageToViewportRectangle(
            box.minX * scale, box.minY * scale,
            (box.maxX - box.minX) * scale, (box.maxY - box.minY) * scale);
        const topLeft = this.viewer.viewport.pixelFromPoint(
            new OpenSeadragon.Point(rect.x, rect.y), true);
        const bottomRight = this.viewer.viewport.pixelFromPoint(
            new OpenSeadragon.Point(rect.x + rect.width, rect.y + rect.height), true);
        return {
            left: canvas.left + topLeft.x,
            top: canvas.top + topLeft.y,
            right: canvas.left + bottomRight.x,
            bottom: canvas.top + bottomRight.y,
        };
    }


    // -- draft -----------------------------------------------------------

    showDraft(tool) {
        this.renderer.draft = { points: this.draftPoints, tool };
        this.renderer.schedule();
    }

    hideDraft() {
        this.renderer.draft = null;
        this.renderer.schedule();
    }

    /** Throw away whatever is half-drawn.
     *
     * An incomplete shape is never stored. Switching tool, pressing Escape,
     * closing the panel or opening another one all land here, and all of them
     * discard rather than "helpfully" completing a polygon the user was still
     * placing points on. */
    cancelDraft() {
        const had = this.draftPoints.length > 0;
        this.draftPoints = [];
        this.drag = null;
        this.pending = null;
        this.hideDraft();
        if (this.state.startsWith("drawing.")) {
            this.state = this.tool === "select" ? "idle.select" : `drawing.${this.tool}`;
        }
        return had;
    }

    finishPolygon() {
        const points = this.draftPoints.slice();
        this.draftPoints = [];
        this.hideDraft();
        this.createFrom(points);
    }

    finishFreehand() {
        const raw = this.draftPoints.slice();
        this.draftPoints = [];
        this.hideDraft();
        this.state = "drawing.freehand";

        // Simplified once, here, and never again. A stroke arrives as one point
        // per pointer event -- thousands of them, mostly describing sub-pixel
        // wobble along a straight line. Re-simplifying on later edits would
        // erode the shape a little every time it was touched.
        const epsilon = this.imageDistance(RoiInteraction.SIMPLIFY_EPSILON);
        const deduped = RoiGeometry.dedupe(raw, epsilon / 2);
        this.createFrom(RoiGeometry.simplify(deduped, epsilon));
    }

    // -- creating and committing ------------------------------------------

    /** Turn a finished set of points into a stored region, or reject it. */
    createFrom(rawPoints) {
        const points = RoiGeometry.dedupe(rawPoints, 0);
        if (RoiGeometry.distinctCount(points) < 3) {
            // Two clicks and a double-click, or a stray press. Nothing was
            // drawn, so nothing is created -- and nothing is said, because a
            // dialog about a shape the user did not mean to draw is noise.
            this.renderer.schedule();
            return null;
        }

        const minArea = this.imageDistance(1) ** 2 * RoiInteraction.MIN_AREA;
        if (RoiGeometry.area(points) < minArea) {
            this.notify("That shape was too small to keep.");
            this.renderer.schedule();
            return null;
        }

        const category = this.store.activeCategory;
        if (!category) {
            // Belt and braces: the draw tools refuse to start without one, so
            // getting here means the category went away mid-draft.
            this.cancelDraft();
            this.needCategory();
            return null;
        }

        const geometry = RoiGeometry.polygonFrom(points);
        const categoryId = category.id;
        const feature = {
            id: RoiStore.newId("r"),
            category_id: categoryId,
            name: this.nextName(categoryId),
            locked: false,
            geometry,
            flags: { self_intersecting: RoiGeometry.selfIntersects(geometry.coordinates[0]) },
            source_roi_id: null,
        };

        const image = this.store.image;
        this.store.commit({
            label: "Draw ROI",
            redo: [{ op: "roi.create", image, feature }],
            undo: [{ op: "roi.delete", image, id: feature.id }],
        });
        this.store.select(feature.id);
        this.renderer.invalidate(feature.id);
        this.renderer.schedule();

        if (feature.flags.self_intersecting) {
            this.notify("That outline crosses itself. It has been kept exactly as drawn.");
        }
        return feature;
    }

    /** "Tumor 3" -- the category's name and the next free number in it. */
    nextName(categoryId) {
        const category = this.store.category(categoryId);
        const base = category ? category.label : "ROI";
        return `${base} ${this.store.countFor(categoryId) + 1}`;
    }

    commitGeometry(feature, before, after) {
        if (JSON.stringify(before) === JSON.stringify(after)) return;
        const image = this.store.image;
        const flags = {
            self_intersecting: RoiGeometry.isVertexEditable(after)
                ? RoiGeometry.selfIntersects(after.coordinates[0])
                : Boolean(feature.flags && feature.flags.self_intersecting),
        };
        // Applied locally already -- the shape followed the cursor. Put it back
        // first so commit()'s own applyLocal is not a no-op that leaves the undo
        // entry describing a change that never appeared to happen.
        feature.geometry = before;
        this.store.commit({
            label: "Edit ROI",
            redo: [{ op: "roi.update_geometry", image, id: feature.id, geometry: after, flags }],
            undo: [{
                op: "roi.update_geometry", image, id: feature.id, geometry: before,
                flags: feature.flags || { self_intersecting: false },
            }],
        });
        this.renderer.invalidate(feature.id);
        this.renderer.schedule();
    }

    deleteSelected() {
        const feature = this.store.selected;
        if (!feature) return false;
        if (this.store.isLocked(feature)) {
            this.notify("That ROI is locked.");
            return false;
        }
        const image = this.store.image;
        // No confirmation: deleting one region is a thing users do constantly,
        // and a dialog every time makes annotating miserable. Undo is the safety
        // net, and it is a better one -- it restores the shape rather than
        // asking about it in advance.
        this.store.commit({
            label: "Delete ROI",
            redo: [{ op: "roi.delete", image, id: feature.id }],
            undo: [{ op: "roi.create", image, feature: JSON.parse(JSON.stringify(feature)) }],
        });
        this.renderer.invalidate(feature.id);
        this.renderer.schedule();
        return true;
    }

    // -- keyboard --------------------------------------------------------

    /**
     * Whether a keystroke belongs to this tool.
     *
     * The two ways it does not: the user is typing (a category named "Var" must
     * not switch to Rectangle on the R), or this tool's panel is not the one on
     * screen. Neither guard existed anywhere in Plexora before -- ROI is the
     * first thing here to want document-level shortcuts -- so this is the
     * convention rather than a copy of one.
     */
    acceptsKeys() {
        if (!this.armed || !this.ready) return false;
        const active = document.activeElement;
        if (active) {
            const tag = active.tagName;
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return false;
            if (active.isContentEditable) return false;
        }
        const loader = window.PlexoraToolLoader;
        if (loader && typeof loader.activeTool === "function" && loader.activeTool() !== "roi") {
            return false;
        }
        return true;
    }

    keyDown(event) {
        if (!this.acceptsKeys()) return;

        const meta = event.ctrlKey || event.metaKey;
        if (meta && event.key.toLowerCase() === "z") {
            event.preventDefault();
            if (event.shiftKey) this.store.redo();
            else this.store.undo();
            this.renderer.invalidate();
            this.renderer.schedule();
            return;
        }
        if (meta && event.key.toLowerCase() === "y") {
            event.preventDefault();
            this.store.redo();
            this.renderer.invalidate();
            this.renderer.schedule();
            return;
        }
        if (meta) return;   // leave every other accelerator to the browser

        switch (event.key) {
            case " ":
                if (!this.spaceHeld) {
                    this.spaceHeld = true;
                    this.stateBeforeSpace = this.state;
                    this.applyCursor();
                }
                event.preventDefault();   // Space scrolls the page otherwise
                return;
            case "Escape":
                // Draft first, then selection: Escape means "back out of the
                // most immediate thing", and a half-drawn polygon is more
                // immediate than a selection made a minute ago.
                if (!this.cancelDraft()) this.store.select(null);
                this.renderer.schedule();
                return;
            case "Enter":
                if (this.tool === "polygon" && this.draftPoints.length) {
                    event.preventDefault();
                    this.finishPolygon();
                }
                return;
            case "Backspace":
                if (this.state === "drawing.polygon" && this.draftPoints.length) {
                    event.preventDefault();
                    this.draftPoints.pop();
                    this.showDraft("polygon");
                    return;
                }
                event.preventDefault();
                this.deleteSelected();
                return;
            case "Delete":
                event.preventDefault();
                this.deleteSelected();
                return;
            default:
                break;
        }

        const shortcut = { v: "select", p: "polygon", f: "freehand", r: "rectangle" };
        const tool = shortcut[event.key.toLowerCase()];
        if (tool) {
            event.preventDefault();
            this.setTool(tool);
        }
    }

    keyUp(event) {
        if (event.key === " ") this.releaseSpace();
    }

    /** Also called on window blur: alt-tabbing away while Space is down means
     *  the keyup lands in another window, and without this the viewer would be
     *  stuck in pan mode with no way to tell why. */
    releaseSpace() {
        if (!this.spaceHeld) return;
        this.spaceHeld = false;
        if (this.state === "panning.temporary") {
            this.state = this.stateBeforeSpace || "idle.select";
        }
        this.stateBeforeSpace = null;
        this.applyCursor();
    }

    notify(message) {
        if (this.onNotify) this.onNotify(message);
    }
}
