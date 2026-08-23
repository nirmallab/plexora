/**
 * FigureCaptureBoxes - where the captures came from, still on the image.
 *
 * A capture used to leave no mark. The shutter fired, a thumbnail appeared in
 * the strip, and the region it came from was gone -- so building a row of
 * comparable panels meant remembering four places on a slide by eye. These are
 * those four places, drawn as thin outlines that stay on the image for the whole
 * session.
 *
 * ## Held in IMAGE pixels, which is the opposite of the viewfinder
 *
 * figureCaptureTool's frame is held in SCREEN pixels on purpose: it is a
 * viewfinder, and panning the image under it is how you aim it. A capture box is
 * the other thing entirely -- it is a record of a region OF THE IMAGE, so it has
 * to travel with the image, growing as you zoom in and sliding as you pan. The
 * two live in the same corner of the same screen and mean opposite things, and
 * getting either one backwards produces something that looks fine until the
 * moment it matters.
 *
 * Both are reprojected through the SAME tool instance rather than through a
 * second copy of the arithmetic, so a frame re-aimed at a box (which is what
 * selecting a capture does) lands exactly on it. Two copies would agree until
 * the day one of them was fixed.
 *
 * ## The interior is not a target
 *
 * A box can cover most of the viewer. If the whole rectangle were clickable, a
 * session's captures would gradually cover the image in dead zones where panning
 * and every other tool's drag stopped working. So the element itself is
 * `pointer-events: none` and only four thin bands along its edges -- plus the
 * label, once it is showing -- take clicks. Everything inside a capture box
 * behaves exactly like the image, because it is the image.
 *
 * ## Selecting one does not restore what it looked like
 *
 * Centring on a box puts the viewer back over that FIELD and touches nothing
 * about the rendering: not a channel, not a window, not an overlay. That is
 * deliberate and it is the feature -- go back to a region, change the channels,
 * and capture it again to get a second panel of the same field under a different
 * rendering, in pixel-level concordance with the first. Restoring the captured
 * scene here would make that impossible to do by accident and hard to do on
 * purpose. Reopening a panel's full scene is a different action, lives on the
 * canvas, and says so (see the sidebar controller's editPanel).
 */
class FigureCaptureBoxes {

    /** How thick the clickable band along each edge is, in CSS pixels. Wider
     *  than the 1px outline it straddles, because a 1px hit target is a target
     *  nobody hits. */
    static get EDGE() { return 9; }

    /**
     * How much viewer a region gets when you go back to it, as a multiple of
     * the region itself. Two, so the capture fills about half the window.
     *
     * Filling the window edge to edge answers "what was in this capture" and
     * nothing else: where it sits, what surrounds it, and whether the next one
     * should be a little to the left are all off screen, and arriving that
     * close is a lurch out of whatever the user was looking at. Half leaves a
     * margin of context on every side, and it leaves the capture's own outline
     * visible -- which is what tells the user they have arrived somewhere they
     * have already been.
     */
    static get CONTEXT() { return 2; }

    /**
     * How long the viewer has to be QUIET before it counts as having arrived,
     * and the longest the arrival will wait for a viewer that never is.
     *
     * `animation-finish` is not the end of the movement. OpenSeadragon raises
     * it when the springs reach their target and then pulls the viewport back
     * inside its constraints, which is a second, smaller movement afterwards.
     * A capture near an edge of the slide provokes it, and so does any capture
     * whose context rectangle does not share the viewer's aspect ratio -- which
     * is most of them. See centerOn().
     */
    static get SETTLE_MS() { return 120; }
    static get SETTLE_LIMIT_MS() { return 900; }

    /** The viewer events that mean the picture is still moving. `resize` counts:
     *  the canvas opening beside the viewer moves the region on screen just as
     *  surely as a pan does. */
    static get MOTION_EVENTS() { return ["animation", "animation-finish", "resize"]; }

    /**
     * The region, grown about its own centre.
     *
     * Pure and separate so the framing can be checked without a viewer. About
     * the CENTRE rather than the top-left, or going back to a capture would put
     * it in the corner of the window instead of the middle of it.
     */
    static contextRect(rect, grow) {
        const factor = grow || FigureCaptureBoxes.CONTEXT;
        const w = rect.w * factor;
        const h = rect.h * factor;
        return {
            x: rect.x + (rect.w - w) / 2,
            y: rect.y + (rect.h - h) / 2,
            w: w,
            h: h,
        };
    }

    /**
     * Where a captured region lands on screen, or null when it lands nowhere.
     *
     * Pure, and separate from the drawing, because "is this box on screen at
     * all" is the question asked most often -- once per box per animation frame
     * -- and the answer for a user zoomed into one corner of a slide is usually
     * no. Boxes that are nowhere are not drawn at all rather than drawn far
     * outside the viewer: #openseadragon_wrapper does not clip, so an undrawn
     * box is the only kind that cannot end up over the sidebar.
     */
    static placement(screenRect, bounds) {
        if (!screenRect || !bounds || !(bounds.width > 0) || !(bounds.height > 0)) return null;
        const width = Math.max(1, screenRect.width);
        const height = Math.max(1, screenRect.height);
        const left = screenRect.x;
        const top = screenRect.y;
        if (left + width <= 0 || top + height <= 0) return null;
        if (left >= bounds.width || top >= bounds.height) return null;
        return {
            left: Math.round(left),
            top: Math.round(top),
            width: Math.round(width),
            height: Math.round(height),
        };
    }

    /**
     * @param {object} ctx the plugin context.
     * @param {object} options tool (the FigureCaptureTool whose coordinate
     *        frame these share) and onSelect(captureId).
     */
    constructor(ctx, options) {
        this.ctx = ctx;
        this.viewer = ctx.viewer?.viewer || null;
        this.tool = (options && options.tool) || null;
        this.onSelect = (options && options.onSelect) || (() => {});

        //: [{id, rect}] with rect in FULL-RESOLUTION image pixels.
        this.boxes = [];
        this.selected = null;
        this.root = null;
        this._nodes = new Map();

        this._onViewport = () => this.paint();
        this._handlers = [];
    }

    get mounted() {
        return Boolean(this.root);
    }

    // -- lifecycle -------------------------------------------------------

    /** A sibling of #openseadragon inside #openseadragon_wrapper -- miniMap's
     *  placement, and the same reason: OSD binds its mouse tracker to
     *  #openseadragon, so a click on an edge band here never reaches the viewer
     *  underneath and nothing needs stopPropagation. */
    mount() {
        const host = document.getElementById("openseadragon_wrapper");
        if (this.root || !host || !this.viewer) return false;

        const root = document.createElement("div");
        root.className = "fb-boxes";
        // Delegated, because the nodes are rebuilt whenever the capture list
        // changes and per-node handlers would be rebound on every shutter.
        // Events from the edge bands still bubble here: pointer-events on an
        // ancestor decides whether IT is hit, not whether its children's
        // events pass through it.
        root.addEventListener("click", (event) => {
            const id = event.target.closest?.("[data-capture-id]")?.dataset.captureId;
            if (id) this.onSelect(id);
        });
        root.addEventListener("pointerover", (event) => this.hover(event, true));
        root.addEventListener("pointerout", (event) => this.hover(event, false));
        host.appendChild(root);
        this.root = root;

        const on = (name, fn) => {
            this.viewer.addHandler(name, fn);
            this._handlers.push([name, fn]);
        };
        // The trio miniMap.js uses: every frame of a pan or zoom, the frame it
        // settles on, and a viewer that changed size -- which is what opening
        // the figure canvas beside it does.
        on("animation", this._onViewport);
        on("animation-finish", this._onViewport);
        on("resize", this._onViewport);
        on("open", this._onViewport);
        window.addEventListener("resize", this._onViewport);

        this.paint();
        return true;
    }

    unmount() {
        for (const [name, fn] of this._handlers) this.viewer?.removeHandler?.(name, fn);
        this._handlers = [];
        window.removeEventListener("resize", this._onViewport);
        this.root?.remove();
        this.root = null;
        this._nodes = new Map();
    }

    destroy() {
        this.unmount();
        this.boxes = [];
        this.selected = null;
    }

    hover(event, on) {
        const box = event.target.closest?.(".fb-box");
        if (!box) return;
        // pointerout fires when the pointer moves between two edge bands of the
        // SAME box, which happens at every corner -- and toggling the highlight
        // off and straight back on is a flicker on a 120ms transition. Where
        // the pointer went is relatedTarget.
        if (!on && box.contains?.(event.relatedTarget)) return;
        box.classList.toggle("is-hover", on);
    }

    // -- what there is to draw -------------------------------------------

    /** @param {Array} boxes [{id, rect}], rect in full-resolution image px. */
    setBoxes(boxes) {
        this.boxes = (boxes || []).filter((box) => box && box.id && box.rect);
        this.paint();
    }

    setSelected(id) {
        this.selected = id || null;
        this.paint();
    }

    // -- drawing ---------------------------------------------------------

    node(id) {
        let node = this._nodes.get(id);
        if (node && node.isConnected !== false) return node;
        node = document.createElement("div");
        node.className = "fb-box";
        node.dataset.captureId = id;
        node.innerHTML = ["n", "e", "s", "w"].map((side) =>
            `<span class="fb-box-edge fb-box-${side}"></span>`).join("")
            + '<span class="fb-box-tag"></span>';
        this.root.appendChild(node);
        this._nodes.set(id, node);
        return node;
    }

    /**
     * Put every box where the image is now.
     *
     * Called once per animation frame during a pan, so it does the cheap thing:
     * the nodes are reused and only their geometry is written. A session has
     * tens of captures, not thousands.
     */
    paint() {
        if (!this.root || !this.tool) return;
        const bounds = this.tool.bounds();
        const offset = this.tool.offset();

        // The container is the viewer's own drawing area, and it clips: a box
        // whose left edge is on screen and whose right edge is a mile off it
        // would otherwise be a div a mile wide, laid over whatever is beside
        // the viewer.
        this.root.style.left = offset.x + "px";
        this.root.style.top = offset.y + "px";
        this.root.style.width = bounds.width + "px";
        this.root.style.height = bounds.height + "px";

        const live = new Set();
        this.boxes.forEach((box, index) => {
            const placement = FigureCaptureBoxes.placement(
                this.tool.toScreenRect(box.rect), bounds);
            if (!placement) return;
            live.add(box.id);
            const node = this.node(box.id);
            node.style.left = placement.left + "px";
            node.style.top = placement.top + "px";
            node.style.width = placement.width + "px";
            node.style.height = placement.height + "px";
            node.classList.toggle("is-selected", box.id === this.selected);
            const tag = node.querySelector(".fb-box-tag");
            // Newest first in the strip, so the numbers read the same way in
            // both places -- a box labelled 1 and a thumbnail labelled 1 that
            // are different captures is worse than no numbers at all.
            if (tag) tag.textContent = String(index + 1);
        });

        // Anything not placed this pass is off screen or gone from the strip.
        for (const [id, node] of Array.from(this._nodes.entries())) {
            if (live.has(id)) continue;
            node.remove();
            this._nodes.delete(id);
        }
    }

    // -- going back to one -----------------------------------------------

    /**
     * Put the viewer back over a captured region.
     *
     * Over it with room around it -- CONTEXT times its own size -- rather than
     * filling the window with it. The rendering is not touched; see the class
     * comment.
     *
     * `done` is called once the viewer has STOPPED, which is not the same as
     * once it says it has finished. The caller uses it to land the viewfinder
     * on this exact region and to lock the shutter onto it, and that lock is
     * what the selection is: taking it a few pixels off the region means the
     * next movement -- OpenSeadragon's own constraint correction, arriving
     * after `animation-finish` -- breaks it immediately, and the capture the
     * user just clicked goes unselected before they can see it was selected.
     *
     * So this waits for quiet rather than for an event: every `animation`
     * restarts a short timer, and the arrival is the timer surviving. The first
     * wait is started here rather than by an event, because a fitBounds onto
     * where the viewer already is raises no animation at all; and a viewer that
     * never goes quiet gives up at SETTLE_LIMIT_MS rather than stranding the
     * selection forever.
     */
    centerOn(rect, done) {
        const item = this.viewer?.world?.getItemAt?.(0);
        const viewport = this.viewer?.viewport;
        if (!item || !viewport || !rect) {
            done?.();
            return false;
        }
        const scale = 2 ** (this.ctx.config?.extraZoomLevels || 0);
        const framed = FigureCaptureBoxes.contextRect(rect);
        viewport.fitBounds(item.imageToViewportRectangle(new OpenSeadragon.Rect(
            framed.x * scale, framed.y * scale, framed.w * scale, framed.h * scale)));

        if (done) this.whenStill(done);
        return true;
    }

    /** Call `done` once the viewer has been still for SETTLE_MS. */
    whenStill(done) {
        let settled = false;
        let quiet = null;
        let limit = null;

        const settle = () => {
            if (settled) return;
            settled = true;
            clearTimeout(quiet);
            clearTimeout(limit);
            for (const name of FigureCaptureBoxes.MOTION_EVENTS) {
                this.viewer.removeHandler?.(name, restart);
            }
            done();
        };
        const restart = () => {
            if (settled) return;
            clearTimeout(quiet);
            quiet = setTimeout(settle, FigureCaptureBoxes.SETTLE_MS);
        };

        for (const name of FigureCaptureBoxes.MOTION_EVENTS) {
            this.viewer.addHandler?.(name, restart);
        }
        restart();
        limit = setTimeout(settle, FigureCaptureBoxes.SETTLE_LIMIT_MS);
    }
}
