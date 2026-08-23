/**
 * FigureCaptureTool - the viewfinder, and turning what is inside it into a panel.
 *
 * The gesture used to be "drag a rectangle, get a panel". That is one action per
 * panel, which is right for one capture and wrong for the thing people actually
 * do: take the same field from four places in a slide. Each drag was a
 * hand-drawn rectangle, so the four panels were four slightly different sizes,
 * and nothing on screen said so until they were side by side in a figure.
 *
 * So the rectangle is now a persistent VIEWFINDER. It is defined once, stays put
 * while capture mode is on, and every shot is taken through it:
 *
 *   drag on the image   redraw the viewfinder
 *   drag inside it      move it
 *   drag a corner       resize it
 *   Shift + drag        square, in IMAGE pixels -- so a set of panels really is
 *                       the same physical field, which a square in screen pixels
 *                       on a non-square-pixel image would not be
 *   Space + drag        pans, whatever is armed
 *   wheel               always zooms, never intercepted
 *   S / the shutter     takes the shot, and ends capture mode
 *   Esc                 cancels a drag, or leaves capture mode
 *
 * The box is held in SCREEN pixels, not image pixels, and that is the whole
 * point of it: pan the image underneath and the frame stays where it is, so the
 * four fields come out the same size. It is converted to image pixels at the
 * moment the shutter fires, which is the only moment the answer matters, and
 * what gets stored is still full-resolution image coordinates.
 *
 * ## Except while it is pinned
 *
 * Going back to a capture locks the frame onto that capture's region (lockOn),
 * and while it is locked the shutter takes THAT region -- the stored numbers,
 * not a fresh reading off the screen. Without the lock, "go back, change the
 * channels, capture it again" gives two panels that are a pixel or two apart
 * and nothing on screen says so.
 *
 * A locked frame FOLLOWS its region, because the lock is on the region and the
 * frame is only how the region is shown: a frame sitting anywhere else would be
 * the tool lying about what the next shot will be. It lets go when the user
 * aims somewhere else by hand, or when the region can no longer be shown as a
 * frame at all -- panned off, zoomed past, shrunk to a speck. That last rule is
 * also what keeps a following frame inside the viewer, so "one frame, four
 * places" survives: the moment the frame stops being locked to anything, it is
 * a screen-anchored viewfinder again.
 *
 * Precedence is settled per event (`event.preventDefaultAction = true`) rather
 * than with `viewer.setMouseNavEnabled(false)`, for the reason roiTools.js
 * documents: the same drag redraws or pans depending on what is held, and a
 * viewer-wide switch cannot express that without eventually being left in the
 * wrong position.
 *
 * ## Where the viewfinder lives
 *
 * As a sibling of #openseadragon inside #openseadragon_wrapper, which is where
 * miniMap.js puts the overview lens and for the same two reasons. OSD binds its
 * mouse tracker to #openseadragon, so a drag on the frame never reaches the
 * viewer and no stopPropagation is needed; and a plain <div> is not a canvas
 * inside the viewer, so the frame can never be baked into the preview the way
 * the old marquee could -- that hazard, and the synchronous clear() that
 * answered it, are both gone.
 *
 * ## The preview
 *
 * What lands on the canvas is a crop of the canvases the viewer has ALREADY
 * composited -- the tile drawer plus every overlay, in stacking order. That is
 * what makes the preview WYSIWYG for free: whatever a plugin drew over the
 * image is in it, without Figure Builder knowing that plugin exists, and no
 * second rendering path can disagree with the first. Viewer chrome is excluded
 * because it is not a canvas inside the viewer.
 *
 * The preview is Tier 0/1 only. Publication export re-reads the source pixels
 * at the requested DPI from the scene snapshot; this raster exists so the panel
 * appears instantly, so reopening a figure is fast, and so a panel whose source
 * has gone still shows something.
 */
class FigureCaptureTool {

    /** Smallest capture worth keeping, in SCREEN pixels on a side. Below this
     *  it is a click that slipped, not a region anybody chose. */
    static get MIN_SCREEN_SIZE() { return 12; }

    /**
     * The one key that fires the shutter.
     *
     * Enter was the obvious choice and the wrong one. Enter activates whatever
     * button has focus, and by the time anybody presses it the focus is on the
     * last thing they clicked -- the mode toggle, a thumbnail, "Figure Canvas".
     * So it fired the shutter AND that button, or that button swallowed it and
     * the shutter never fired, depending on which handler ran first and where
     * the user had last put their hand. A bare letter is only ever the document
     * handler's, which is why the dock's mode toggle is one too.
     */
    static get SHOOT_KEY() { return "s"; }

    /**
     * Is this keystroke the shutter?
     *
     * The same two guards the dock's own bare letter needs, for the same
     * reasons: no modifier chord (Cmd-S is save and always will be), and
     * nothing while the user is typing -- otherwise naming a figure "cross
     * sections" takes three photographs.
     */
    static isShootKey(event) {
        if (!event || event.metaKey || event.ctrlKey || event.altKey) return false;
        if (typeof event.key !== "string") return false;
        return event.key.toLowerCase() === FigureCaptureTool.SHOOT_KEY
            && !FigureCaptureTool.isTyping();
    }

    /** Longest edge of a preview raster. A preview is not the master, and a
     *  hundred of them at full resolution is a document nobody can open. */
    static get MAX_PREVIEW_EDGE() { return 1400; }

    /** The frame capture mode opens with, as a fraction of the shorter side of
     *  the viewer, and its height as a fraction of its width. A frame that is
     *  already there is what makes the mode legible in the first second: the
     *  user sees what will be taken and adjusts it, rather than reading a hint
     *  about a gesture they have not made yet. */
    static get DEFAULT_FRACTION() { return 0.46; }
    static get DEFAULT_RATIO() { return 0.75; }

    /**
     * A frame in the middle of a viewer of this size.
     *
     * Pure and static so the arithmetic can be checked without a browser --
     * see tests/js/figure_capture_probe.mjs.
     */
    static defaultBox(bounds) {
        const width = Math.max(0, (bounds && bounds.width) || 0);
        const height = Math.max(0, (bounds && bounds.height) || 0);
        const w = Math.round(Math.min(width, height) * FigureCaptureTool.DEFAULT_FRACTION);
        const h = Math.round(w * FigureCaptureTool.DEFAULT_RATIO);
        return FigureCaptureTool.clampBox({
            x: Math.round((width - w) / 2),
            y: Math.round((height - h) / 2),
            width: w,
            height: h,
        }, bounds);
    }

    /**
     * Keep a frame inside the viewer, and no smaller than a frame can be.
     *
     * Fully inside rather than merely overlapping: the shutter and the corner
     * handles live on the frame, and a frame half off the edge is one the user
     * can see and cannot reach.
     */
    static clampBox(box, bounds) {
        const limitW = Math.max(FigureCaptureTool.MIN_SCREEN_SIZE, (bounds && bounds.width) || 0);
        const limitH = Math.max(FigureCaptureTool.MIN_SCREEN_SIZE, (bounds && bounds.height) || 0);
        const width = Math.min(Math.max(FigureCaptureTool.MIN_SCREEN_SIZE, box.width), limitW);
        const height = Math.min(Math.max(FigureCaptureTool.MIN_SCREEN_SIZE, box.height), limitH);
        return {
            x: Math.round(Math.min(Math.max(0, box.x), limitW - width)),
            y: Math.round(Math.min(Math.max(0, box.y), limitH - height)),
            width: Math.round(width),
            height: Math.round(height),
        };
    }

    /** The frame moved by a screen delta, still inside the viewer. */
    static moveBox(box, dx, dy, bounds) {
        return FigureCaptureTool.clampBox(
            { x: box.x + dx, y: box.y + dy, width: box.width, height: box.height }, bounds);
    }

    /**
     * The frame with one corner dragged.
     *
     * The opposite corner is the anchor and does not move, which is what makes
     * a resize feel like a resize; dragging a corner past it stops at the
     * minimum size rather than inverting the frame.
     */
    static resizeBox(box, corner, dx, dy, bounds) {
        const west = corner === "nw" || corner === "sw";
        const north = corner === "nw" || corner === "ne";
        const right = box.x + box.width;
        const bottom = box.y + box.height;

        let x = west ? box.x + dx : box.x;
        let y = north ? box.y + dy : box.y;
        let width = west ? right - x : box.width + dx;
        let height = north ? bottom - y : box.height + dy;

        if (width < FigureCaptureTool.MIN_SCREEN_SIZE) {
            width = FigureCaptureTool.MIN_SCREEN_SIZE;
            if (west) x = right - width;
        }
        if (height < FigureCaptureTool.MIN_SCREEN_SIZE) {
            height = FigureCaptureTool.MIN_SCREEN_SIZE;
            if (north) y = bottom - height;
        }
        return FigureCaptureTool.clampBox({ x: x, y: y, width: width, height: height }, bounds);
    }

    constructor(ctx, options) {
        this.ctx = ctx;
        this.viewer = ctx.viewer?.viewer || null;
        this.onCapture = (options && options.onCapture) || (() => {});
        this.onStateChange = (options && options.onStateChange) || (() => {});
        //: The frame let go of the capture it was locked to. The selection in
        //: the strip and the highlight on the image are the same state as the
        //: lock, so they go with it.
        this.onUnpin = (options && options.onUnpin) || (() => {});

        //: This plugin's name, for the one question the tool asks core: whether
        //: it is the selected tool. See canRedraw().
        this.toolName = (options && options.toolName) || "figure_builder";

        this.armed = false;
        this.dragging = false;
        this.spaceHeld = false;
        this.anchor = null;
        //: The viewfinder, in CSS pixels relative to the viewer's canvas -- the
        //: same origin toImage() and grabPreview() work in, so no conversion
        //: sits between what is drawn and what is taken. Kept across a
        //: disarm/arm so "same frame, four places" survives leaving the mode.
        this.box = null;
        //: The frame while a redraw drag is in flight, in IMAGE pixels. Kept in
        //: image space until the drag ends rather than converted at the end, so
        //: a pan or a zoom mid-drag cannot move the region under the pointer.
        this.drawing = null;

        //: The region the frame is LOCKED to, in full-resolution image pixels,
        //: or null when the frame is aimed freely. A pin makes the shutter take
        //: THIS region rather than whatever the frame happens to be over --
        //: see pinTo().
        this.pinned = null;
        this.pinLabel = "";
        this._pinWatch = null;


        this.element = null;
        this._pointer = null;
        this._handlers = [];
        this._onKeyDown = (event) => this.keyDown(event);
        this._onKeyUp = (event) => this.keyUp(event);
        // Space released outside the window never reaches keyup, so the class
        // it added has to come off here too -- otherwise the frame stays
        // unclickable and nothing on screen explains why.
        this._onBlur = () => {
            this.spaceHeld = false;
            this.element?.classList.remove("is-transparent");
        };
        //: The viewer changes size when the canvas opens beside it, and a frame
        //: that was centred is then hanging off the right-hand edge.
        this._onResize = () => this.reflow();
    }

    get active() {
        return this.armed;
    }

    /** The frame the next shot will be taken through, or null. */
    get viewfinder() {
        return this.armed ? this.box : null;
    }

    // -- lifecycle -------------------------------------------------------

    arm() {
        if (this.armed || !this.viewer) return;
        this.armed = true;

        const on = (name, fn) => {
            this.viewer.addHandler(name, fn);
            this._handlers.push([name, fn]);
        };
        on("canvas-press", (event) => this.press(event));
        on("canvas-drag", (event) => this.drag(event));
        on("canvas-drag-end", (event) => this.dragEnd(event));

        document.addEventListener("keydown", this._onKeyDown);
        document.addEventListener("keyup", this._onKeyUp);
        window.addEventListener("blur", this._onBlur);
        window.addEventListener("resize", this._onResize);

        if (!this.box) this.box = FigureCaptureTool.defaultBox(this.bounds());
        this.attachFrame();
        this.paint();
        // A pin set while the mode was off can be stale by now -- nothing stops
        // the user panning away from it with the frame gone. Ask before showing
        // a lock that is not one.
        this.checkPin();
        this.setCursor("crosshair");
        this.onStateChange(true);
    }

    /**
     * Stop capturing.
     *
     * Symmetrical with arm() and called on every switch away, because the
     * listeners are on the VIEWER and the DOCUMENT -- neither of which goes
     * away when this panel is hidden. Left attached, a drag meant for another
     * tool would move a viewfinder the user cannot see.
     *
     * The frame's geometry is kept: leaving capture mode to adjust a channel
     * and coming back should not cost the user the frame they set up.
     */
    disarm() {
        if (!this.armed) return;
        this.armed = false;
        this.dragging = false;
        this.drawing = null;
        this.anchor = null;

        for (const [name, fn] of this._handlers) this.viewer.removeHandler(name, fn);
        this._handlers = [];
        document.removeEventListener("keydown", this._onKeyDown);
        document.removeEventListener("keyup", this._onKeyUp);
        window.removeEventListener("blur", this._onBlur);
        window.removeEventListener("resize", this._onResize);

        this.detachFrame();
        this.setCursor("");
        this.onStateChange(false);
    }

    destroy() {
        this.disarm();
        this.unpin(true);
        this.box = null;
    }

    setCursor(cursor) {
        if (this.viewer?.canvas) this.viewer.canvas.style.cursor = cursor;
    }

    // -- the viewfinder --------------------------------------------------

    /** Where plugin-owned chrome hangs: a sibling of the OSD element, never a
     *  child of it. See the class comment. */
    host() {
        return document.getElementById("openseadragon_wrapper");
    }

    /** The viewer's own drawing area, in CSS pixels. */
    bounds() {
        const rect = this.viewer?.canvas?.getBoundingClientRect?.();
        return rect ? { width: rect.width, height: rect.height } : { width: 0, height: 0 };
    }

    /** The canvas's top-left corner within the host, so a box in canvas
     *  coordinates can be drawn by a sibling of the canvas. */
    offset() {
        const host = this.host();
        const canvas = this.viewer?.canvas;
        if (!host || !canvas?.getBoundingClientRect) return { x: 0, y: 0 };
        const outer = host.getBoundingClientRect();
        const inner = canvas.getBoundingClientRect();
        return { x: inner.left - outer.left, y: inner.top - outer.top };
    }

    attachFrame() {
        const host = this.host();
        if (this.element || !host) return;

        const frame = document.createElement("div");
        frame.className = "fb-viewfinder";
        frame.innerHTML =
            '<div class="fb-viewfinder-caption"></div>'
            + ["nw", "ne", "se", "sw"].map((corner) =>
                `<span class="fb-vf-handle fb-vf-${corner}" data-corner="${corner}"></span>`).join("")
            // Icon and key, no word. The button sits inside the frame, over the
            // tissue being judged, and "Capture" written there is one more thing
            // between the user and the picture -- while the key beside the icon
            // is the only place the shortcut is written down at the moment
            // anybody wants it. What the button DOES is on its title and its
            // accessible name, which cost no pixels.
            + '<button type="button" class="fb-shutter" data-role="shutter">'
            + '<span class="fas fa-camera" aria-hidden="true"></span>'
            + '<kbd class="fb-shutter-key">'
            + FigureCaptureTool.SHOOT_KEY.toUpperCase() + '</kbd>'
            + '<span class="fb-visually-hidden" data-role="shutterName">Capture</span>'
            + '</button>';

        frame.addEventListener("pointerdown", (event) => this.framePointerDown(event));
        frame.addEventListener("pointermove", (event) => this.framePointerMove(event));
        frame.addEventListener("pointerup", (event) => this.framePointerUp(event));
        frame.addEventListener("pointercancel", (event) => this.framePointerUp(event));
        frame.addEventListener("click", (event) => {
            if (event.target.closest('[data-role="shutter"]')) this.shoot();
        });

        host.appendChild(frame);
        this.element = frame;
    }

    detachFrame() {
        this.element?.remove();
        this.element = null;
        this._pointer = null;
    }

    /** Put the frame where the numbers say it is. */
    paint() {
        if (!this.element) return;
        const box = this.drawing ? this.toScreenRect(this.drawing) : this.box;
        if (!box) return;
        const offset = this.offset();
        this.element.style.left = (box.x + offset.x) + "px";
        this.element.style.top = (box.y + offset.y) + "px";
        this.element.style.width = box.width + "px";
        this.element.style.height = box.height + "px";
        this.element.classList.toggle("is-drawing", Boolean(this.drawing));

        // A pinned frame is about to take THAT region again rather than
        // whatever the frame is over. It says so in colour, and in the caption
        // naming the capture -- not in the button, which carries no words at
        // all now. The title and the accessible name carry the difference for
        // the two readers who need it spelled out.
        const pinned = Boolean(this.pinned) && !this.drawing;
        this.element.classList.toggle("is-pinned", pinned);
        const shutter = this.element.querySelector(".fb-shutter");
        if (shutter) {
            const what = pinned ? "Capture this region again" : "Capture";
            shutter.title = what + " (" + FigureCaptureTool.SHOOT_KEY.toUpperCase() + ")";
            const name = shutter.querySelector('[data-role="shutterName"]');
            if (name) name.textContent = what;
        }

        const caption = this.element.querySelector(".fb-viewfinder-caption");
        if (caption) {
            const rect = this.imageRectFor(box);
            const size = rect ? Math.round(rect.w) + " × " + Math.round(rect.h) + " px" : "";
            caption.textContent = pinned && this.pinLabel
                ? this.pinLabel + " · " + size
                : size;
        }
    }

    /** Re-clamp to a viewer that changed size, and redraw. */
    reflow() {
        if (!this.armed || !this.box) return;
        this.box = FigureCaptureTool.clampBox(this.box, this.bounds());
        this.paint();
    }

    setBox(box) {
        this.unpin();
        this.box = FigureCaptureTool.clampBox(box, this.bounds());
        this.paint();
    }

    /**
     * Put the frame exactly over a region of the IMAGE.
     *
     * The one place the two coordinate systems are deliberately joined. The
     * frame is normally screen-anchored -- pan the image under it and it stays
     * put -- but when the user goes back to a capture they took earlier, "the
     * same field of view" has to mean the same image pixels, not a rectangle of
     * the same size somewhere near them. Landing the frame on the region is
     * what makes a second capture of it pixel-for-pixel concordant with the
     * first, which is the whole reason to go back.
     *
     * Called after the viewer has finished moving, never during: mid-flight the
     * region is somewhere it is only passing through.
     */
    aimAt(rect) {
        const screenRect = rect ? this.toScreenRect(rect) : null;
        if (!screenRect) return false;
        this.box = FigureCaptureTool.clampBox(screenRect, this.bounds());
        this.paint();
        return true;
    }

    // -- pinning the frame to a capture ----------------------------------

    /**
     * How far the frame may sit from the region it is pinned to, in screen
     * pixels, before the pin counts as broken. One, because the frame is
     * clamped to whole pixels and the projection is not: half a pixel of
     * rounding is not the user going somewhere else.
     */
    static get PIN_SLACK() { return 1; }

    /** The viewer events that mean "the picture moved" -- miniMap.js's trio. */
    static get VIEWPORT_EVENTS() { return ["animation", "animation-finish", "resize"]; }

    /** Is this frame ON this region, rather than merely near it? */
    static onTarget(box, screenRect) {
        if (!box || !screenRect) return false;
        const slack = FigureCaptureTool.PIN_SLACK;
        return Math.abs(box.x - screenRect.x) <= slack
            && Math.abs(box.y - screenRect.y) <= slack
            && Math.abs(box.width - screenRect.width) <= slack
            && Math.abs(box.height - screenRect.height) <= slack;
    }

    /**
     * Lock the frame onto a capture's region.
     *
     * Selecting a capture and then pressing the shutter has to give back the
     * SAME region rather than a fresh one near it -- that is what makes a
     * second panel of a field comparable with the first. So while the frame is
     * pinned the shutter reads the pin, and the frame stops being whatever
     * rectangle the user happened to leave lying about.
     *
     * The pin holds through every rendering change -- channel, window, colour,
     * overlay -- which is the entire reason to go back to a region. It also
     * holds while the viewer MOVES: the lock is on a region, and the frame is
     * only how that region is shown, so the frame follows it. It lets go when
     * the region can no longer be shown as a frame at all -- panned off the
     * edge, zoomed past, or shrunk to a speck -- which is also what stops a
     * following frame from drifting outside the viewer and breaking "one frame,
     * four places". See checkPin().
     *
     * Locks what the frame is ALREADY on, and never moves it. aimAt() is how
     * the frame gets there; keeping the two apart is what lets a capture that
     * has just been taken lock for free -- the frame is on it already -- while
     * a frame the user set up is never quietly resized to the region that was
     * actually saved when it hung over the edge of the slide.
     *
     * So it is refused whenever the frame is not on the region: because the
     * region is off screen, larger than the viewer, or projects smaller than a
     * frame can be -- all of which leave clampBox holding the frame somewhere
     * the region is not, and a lock pointing at the wrong tissue is worse than
     * no lock at all. Returns whether it took.
     */
    pinTo(rect, label) {
        this.unpin(true);
        if (!rect || !this.box) return false;
        if (!FigureCaptureTool.onTarget(this.box, this.toScreenRect(rect))) return false;

        this.pinned = { ...rect };
        this.pinLabel = label || "";
        this.watchPin();
        this.paint();
        return true;
    }

    /**
     * Let go of the region.
     *
     * `silent` is for the one caller that is on its way to pinning somewhere
     * else: the viewer moves to get there, and an unpin that announced itself
     * would clear the very selection being made.
     */
    unpin(silent) {
        if (!this.pinned) return;
        this.pinned = null;
        this.pinLabel = "";
        this.stopWatchingPin();
        this.paint();
        if (!silent) this.onUnpin();
    }

    /**
     * Put the frame on a region and lock the shutter to it, in one step.
     *
     * Going back to a capture wants both, and wants them atomically. Done as
     * aimAt-then-pinTo by the caller, the pin was judged against a projection
     * read a moment after the aim -- and OpenSeadragon is still settling for a
     * while after it says it has finished moving, so the two readings differed
     * by a few pixels, the lock was refused, and the capture the user had just
     * clicked came back unselected. One reading, used for both.
     *
     * Refused when the region cannot be shown as a frame: off screen, larger
     * than the viewer, or smaller than a frame can be. clampBox moving the
     * rectangle is the test for all three.
     */
    lockOn(rect, label) {
        const screenRect = rect ? this.toScreenRect(rect) : null;
        if (!screenRect) return false;
        const box = FigureCaptureTool.clampBox(screenRect, this.bounds());
        if (!FigureCaptureTool.onTarget(box, screenRect)) return false;

        this.unpin(true);
        this.box = box;
        this.pinned = { ...rect };
        this.pinLabel = label || "";
        this.watchPin();
        this.paint();
        return true;
    }

    /**
     * The viewer moved. Keep the frame on the region, or let the region go.
     *
     * Following rather than releasing, because the lock is on a REGION: while a
     * capture is selected the shutter takes that region, and a frame sitting
     * anywhere else is the tool lying about what the next shot will be. It also
     * makes the lock survive the movement the user did not ask for and cannot
     * see -- OpenSeadragon's own settling after a flight, a nudge, a resize --
     * which is what made "click a capture and it comes back unselected" happen
     * at all.
     *
     * Releasing when the region can no longer be framed is what keeps the frame
     * inside the viewer: #openseadragon_wrapper does not clip, so a frame that
     * chased the tissue without this would end up drawn over the sidebar. The
     * frame then stays exactly where it last sat, unlocked, and "one frame, four
     * places" is back.
     */
    checkPin() {
        if (!this.pinned || this.dragging) return;
        const screenRect = this.toScreenRect(this.pinned);
        if (FigureCaptureTool.onTarget(this.box, screenRect)) return;
        if (!screenRect) {
            this.unpin();
            return;
        }
        const box = FigureCaptureTool.clampBox(screenRect, this.bounds());
        if (!FigureCaptureTool.onTarget(box, screenRect)) {
            this.unpin();
            return;
        }
        this.box = box;
        this.paint();
    }

    /** Watched only while something is pinned, and hung on the viewer rather
     *  than on arm/disarm: a pin survives leaving capture mode, so that coming
     *  back to it finds the frame still on the region. */
    watchPin() {
        if (this._pinWatch || !this.viewer) return;
        const check = () => this.checkPin();
        this._pinWatch = check;
        for (const name of FigureCaptureTool.VIEWPORT_EVENTS) {
            this.viewer.addHandler(name, check);
        }
    }

    stopWatchingPin() {
        if (!this._pinWatch || !this.viewer) return;
        for (const name of FigureCaptureTool.VIEWPORT_EVENTS) {
            this.viewer.removeHandler(name, this._pinWatch);
        }
        this._pinWatch = null;
    }

    // -- moving and resizing the frame -----------------------------------

    framePointerDown(event) {
        if (!this.armed || this.spaceHeld || !this.box) return;
        if (event.target.closest('[data-role="shutter"]')) return;
        const corner = event.target.closest(".fb-vf-handle")?.dataset.corner || null;
        this._pointer = {
            id: event.pointerId,
            corner: corner,
            startX: event.clientX,
            startY: event.clientY,
            box: { ...this.box },
        };
        this.element.setPointerCapture?.(event.pointerId);
        event.preventDefault();
    }

    framePointerMove(event) {
        const drag = this._pointer;
        if (!drag || drag.id !== event.pointerId) return;
        const dx = event.clientX - drag.startX;
        const dy = event.clientY - drag.startY;
        // Jitter under a finger is not a gesture, and unpinning on it would
        // lose the lock to a press the user does not think they made.
        if (!dx && !dy) return;
        // Moving or resizing the frame by hand is aiming it somewhere else.
        this.unpin();
        const bounds = this.bounds();
        this.box = drag.corner
            ? FigureCaptureTool.resizeBox(drag.box, drag.corner, dx, dy, bounds)
            : FigureCaptureTool.moveBox(drag.box, dx, dy, bounds);
        this.paint();
    }

    framePointerUp(event) {
        if (!this._pointer || this._pointer.id !== event.pointerId) return;
        this.element?.releasePointerCapture?.(event.pointerId);
        this._pointer = null;
    }

    // -- coordinates -----------------------------------------------------

    /** An OpenSeadragon point, which viewport.pointFromPixel needs -- it does
     *  arithmetic through the point's own methods, so a bare {x, y} throws. */
    point(x, y) {
        if (typeof OpenSeadragon !== "undefined" && OpenSeadragon.Point) {
            return new OpenSeadragon.Point(x, y);
        }
        return { x: x, y: y };
    }

    /**
     * Screen position -> full-resolution image pixel.
     *
     * Through the tile source's own getImagePixel where it exists (viewerManager
     * installs it on every channel), which already accounts for extraZoomLevels.
     * The longhand is the same arithmetic for a source that predates it.
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

    /** Full-resolution image rectangle -> the pixels it occupies on screen. */
    toScreenRect(rect) {
        const item = this.viewer?.world?.getItemAt(0);
        if (!item) return null;
        const scale = 2 ** (this.ctx.config?.extraZoomLevels || 0);
        const viewportRect = item.imageToViewportRectangle(new OpenSeadragon.Rect(
            rect.x * scale, rect.y * scale, rect.w * scale, rect.h * scale));
        const topLeft = this.viewer.viewport.pixelFromPoint(viewportRect.getTopLeft(), true);
        const bottomRight = this.viewer.viewport.pixelFromPoint(viewportRect.getBottomRight(), true);
        return {
            x: topLeft.x,
            y: topLeft.y,
            width: Math.abs(bottomRight.x - topLeft.x),
            height: Math.abs(bottomRight.y - topLeft.y),
        };
    }

    /**
     * The frame -> the region of the image inside it, trimmed to the image.
     *
     * Both corners are converted rather than one corner plus a scaled size:
     * the second is only right while the viewer's mapping is a plain scale,
     * and it would fail silently the day it is not.
     */
    imageRectFor(box) {
        if (!box) return null;
        const topLeft = this.toImage(this.point(box.x, box.y));
        const bottomRight = this.toImage(this.point(box.x + box.width, box.y + box.height));
        if (!topLeft || !bottomRight) return null;
        return this.clamp(this.rectBetween(topLeft, bottomRight, false));
    }

    /** Keep a captured region inside the image it was drawn on. */
    clamp(rect) {
        const width = this.ctx.config?.width || 0;
        const height = this.ctx.config?.height || 0;
        if (!width || !height) return rect;
        const x = Math.max(0, Math.min(rect.x, width));
        const y = Math.max(0, Math.min(rect.y, height));
        return {
            x: x,
            y: y,
            w: Math.max(1, Math.min(rect.w, width - x)),
            h: Math.max(1, Math.min(rect.h, height - y)),
        };
    }

    // -- redrawing the frame on the image --------------------------------

    /**
     * May a drag on bare image redraw the frame right now?
     *
     * Only while Figure Builder is the tool core's shared controls point at.
     * The dock outlives being switched away from -- it is on the image and the
     * session's captures are in it -- so capture mode can be armed while ROI's
     * pen is the selected tool, and one drag would then both draw a region and
     * redraw this frame. Moving and resizing the frame by its own handles work
     * either way: those never reach the viewer at all.
     *
     * Asked of the loader rather than of the other plugins: "am I the selected
     * tool" is a question core already answers, and this file has no business
     * knowing which other tools exist.
     */
    canRedraw() {
        const selected = window.PlexoraToolLoader?.activeTool?.();
        return !selected || selected === this.toolName;
    }

    press(event) {
        if (!this.armed || this.spaceHeld || !this.canRedraw()) return;
        const origin = this.toImage(event.position);
        if (!origin) return;
        event.preventDefaultAction = true;
        this.dragging = true;
        this.anchor = origin;
        this.drawing = { x: origin[0], y: origin[1], w: 0, h: 0 };
        this.paint();
    }

    drag(event) {
        if (!this.armed) return;
        if (this.spaceHeld || !this.dragging) return;   // Space pans, always.
        // A redraw in flight IS the user choosing a new region, and it is the
        // one gesture allowed to take the frame off a capture it was locked to.
        // Here rather than in press(), so a click that never moved does not
        // quietly cost them the lock.
        this.unpin();
        event.preventDefaultAction = true;
        const current = this.toImage(event.position);
        if (!current) return;
        this.drawing = this.rectBetween(this.anchor, current, event.shift);
        this.paint();
    }

    dragEnd(event) {
        if (!this.armed || !this.dragging) return;
        event.preventDefaultAction = true;
        this.dragging = false;

        const drawn = this.drawing ? this.toScreenRect(this.clamp(this.drawing)) : null;
        this.drawing = null;
        const smallest = FigureCaptureTool.MIN_SCREEN_SIZE;
        if (drawn && drawn.width >= smallest && drawn.height >= smallest) {
            this.box = FigureCaptureTool.clampBox(drawn, this.bounds());
        }
        // Anything smaller is a click that slipped, not a frame anybody drew.
        // The previous frame stands rather than collapsing to a dot -- which
        // would cost the user the frame they had set up in exchange for a
        // gesture they did not mean to make.
        this.paint();
    }

    /**
     * The rectangle between two image points, squared off if Shift is held.
     *
     * Squared in IMAGE pixels rather than screen pixels, so a row of square
     * panels really does show the same physical field. On an image whose pixels
     * are not square on screen the two differ, and the screen answer is the one
     * that produces a figure whose panels quietly disagree about scale.
     */
    rectBetween(anchor, current, square) {
        let dx = current[0] - anchor[0];
        let dy = current[1] - anchor[1];
        if (square) {
            const side = Math.max(Math.abs(dx), Math.abs(dy));
            dx = Math.sign(dx || 1) * side;
            dy = Math.sign(dy || 1) * side;
        }
        return {
            x: Math.min(anchor[0], anchor[0] + dx),
            y: Math.min(anchor[1], anchor[1] + dy),
            w: Math.abs(dx),
            h: Math.abs(dy),
        };
    }

    keyDown(event) {
        if (!this.armed) return;
        if (event.key === "Escape") {
            // A drag first, the mode second: Esc during a redraw should put the
            // frame back, not throw the user out of capture mode as well.
            if (this.dragging || this.drawing) {
                this.dragging = false;
                this.drawing = null;
                this.paint();
                return;
            }
            this.disarm();
            return;
        }
        if (FigureCaptureTool.isShootKey(event)) {
            event.preventDefault();
            this.shoot();
            return;
        }
        if (event.code === "Space" && !this.spaceHeld) {
            // Never mid-drag: pressing Space halfway through a redraw must not
            // turn that redraw into a pan.
            if (this.dragging) return;
            this.spaceHeld = true;
            this.setCursor("grab");
            // The frame would otherwise swallow the pan that starts on top of
            // it, which is most of the area anybody would start a pan in.
            this.element?.classList.add("is-transparent");
        }
    }

    keyUp(event) {
        if (event.code === "Space") {
            this.spaceHeld = false;
            this.element?.classList.remove("is-transparent");
            if (this.armed) this.setCursor("crosshair");
        }
    }

    /** Is the user typing? Shared with the dock, which has the same question to
     *  ask about its own single-letter shortcut. */
    static isTyping() {
        const active = typeof document !== "undefined" ? document.activeElement : null;
        return Boolean(active && (["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)
            || active.isContentEditable));
    }

    // -- taking the shot -------------------------------------------------

    /**
     * Capture what is inside the viewfinder.
     *
     * The image rectangle is trimmed to the image and the screen rectangle is
     * then recomputed FROM IT, rather than the frame being cropped one way and
     * the pixels another. A frame hanging over the edge of the slide otherwise
     * produces a preview with a band of background in it and a scene that says
     * there is none -- the two disagreeing about what the panel is.
     *
     * A PINNED frame shoots the region it is pinned to and does not ask the
     * screen at all. Reading it back off the frame would round-trip the region
     * through screen pixels and integer clamping, so the second capture of a
     * field would land a pixel or two from the first -- and "go back, change
     * the channels, capture it again" would be true to the eye and false in the
     * file. Taken from the pin, the two panels' viewports are the same numbers.
     *
     * ## One shot per arming
     *
     * The mode ends here. Capture mode changes what a drag on the image does,
     * so leaving it on after the shot meant the user went back to adjusting the
     * picture -- which is the whole reason to take a second capture -- with the
     * gesture for that still redrawing a viewfinder. Ending it makes the mode as
     * long as the thing it is for, and pressing C again is one keystroke.
     *
     * The frame's geometry and its pin both survive (disarm keeps them), so
     * arming again brings the frame back on the same region, still locked: the
     * capture-adjust-capture loop costs one keystroke a lap and still lands on
     * the same pixels.
     */
    shoot() {
        if (!this.armed || !this.box) return null;
        const rect = this.pinned ? this.clamp(this.pinned) : this.imageRectFor(this.box);
        if (!rect) return null;

        const screenRect = this.toScreenRect(rect);
        const smallest = FigureCaptureTool.MIN_SCREEN_SIZE;
        if (!screenRect || screenRect.width < smallest || screenRect.height < smallest) {
            return null;
        }
        const preview = this.previewBlob(screenRect);
        this.onCapture(rect, screenRect, preview);
        // After the capture is recorded, so the pin onCapture sets is put on a
        // frame that still exists. What tells the user it worked is the frame
        // going and the thumbnail arriving in its place -- which is why the
        // shutter blink this used to draw on the frame is gone rather than
        // being drawn on an element about to be removed.
        this.disarm();
        return rect;
    }

    /**
     * Crop what the viewer has already drawn.
     *
     * Every canvas inside the viewer's own element, in DOM order: the tile
     * drawer first, then each overlay. Their backing stores can be at different
     * scales -- an overlay is sized in CSS pixels, the drawer in device pixels
     * -- so each one's crop is computed from its own bounding box rather than
     * from a shared devicePixelRatio, which is what makes this correct on a
     * mixed-DPI setup and on a browser mid-zoom.
     */
    grabPreview(screenRect) {
        const container = this.viewer?.canvas;
        if (!container) return null;
        const containerRect = container.getBoundingClientRect();

        const cssWidth = Math.max(1, screenRect.width);
        const cssHeight = Math.max(1, screenRect.height);
        const ratio = window.devicePixelRatio || 1;
        const scale = Math.min(ratio, FigureCaptureTool.MAX_PREVIEW_EDGE
            / Math.max(cssWidth, cssHeight));

        const out = document.createElement("canvas");
        out.width = Math.max(1, Math.round(cssWidth * scale));
        out.height = Math.max(1, Math.round(cssHeight * scale));
        const context = out.getContext("2d");
        // Black, not transparent: the viewer composites channels additively over
        // black, and a preview with an alpha hole in it would look like a hole
        // rather than like the dark tissue it is.
        context.fillStyle = "#000000";
        context.fillRect(0, 0, out.width, out.height);

        for (const element of container.querySelectorAll("canvas")) {
            const rect = element.getBoundingClientRect();
            if (!element.width || !element.height || !rect.width || !rect.height) continue;
            const scaleX = element.width / rect.width;
            const scaleY = element.height / rect.height;
            const sx = (screenRect.x - (rect.left - containerRect.left)) * scaleX;
            const sy = (screenRect.y - (rect.top - containerRect.top)) * scaleY;
            try {
                // drawImage clips a source rectangle that runs off the edge and
                // scales the destination to match, so a capture that overlaps
                // the edge of an overlay needs no special case.
                context.drawImage(element, sx, sy, cssWidth * scaleX, cssHeight * scaleY,
                    0, 0, out.width, out.height);
            } catch (error) {
                // A canvas that cannot be read must not cost the user their
                // capture: the scene snapshot is the master, and a panel with a
                // partial preview still re-renders correctly at export.
                console.error("figure_builder: a layer could not be previewed", error);
            }
        }
        return out;
    }

    /** The preview as a WebP blob, or null. */
    previewBlob(screenRect) {
        const canvas = this.grabPreview(screenRect);
        if (!canvas) return Promise.resolve(null);
        return new Promise((resolve) => {
            // WebP over PNG: a 600x600 crop of tissue is roughly a tenth the
            // size, and these are stored in the figure's own database and sent
            // over the wire on every reopen.
            canvas.toBlob((blob) => resolve(blob ? { blob: blob, width: canvas.width, height: canvas.height } : null),
                "image/webp", 0.9);
        });
    }
}
