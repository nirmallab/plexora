/**
 * canvasLayout.js - one layout for everything that floats over the image.
 *
 * WHY. Every control over the canvas used to place itself: `right: 12px`
 * here, `bottom: 48px` there, a `.has-sections` rule nudging the legend, a
 * plugin measuring core's legend to stop above it. Two pieces that did not
 * know about each other landed on the same pixels -- Figure Builder's dock
 * hung down over the depth strip, neither one aware of the other -- and the
 * only lever left was z-index, which decides who is hidden, not where things
 * go. This replaces positions with a layout that cannot overlap.
 *
 * TWO KINDS OF SPACE.
 *
 *   BANDS run the length of an edge and are taken out of the canvas: the
 *   depth strip's column on the right, the XZ section along the bottom, the
 *   YZ section beside the strip. `reserve(id, {edge, size, order})`; bands on
 *   one edge sit side by side, the lowest `order` outermost. Each band's
 *   distance from its edge is published as `--plx-band-<id>`, and the totals
 *   as `--plx-inset-top/right/bottom/left`, on #openseadragon_wrapper.
 *
 *   DOCKS hold small things in what is left (the SAFE AREA). Four corner
 *   docks and two centre ones; in each, items stack in `order` with a gap,
 *   and a column's top and bottom docks share its height, the top one giving
 *   way first (its items scroll or shrink, never cover the one below).
 *   `place(element, {corner, order})`, or declaratively: an element appended
 *   to the wrapper with `data-canvas-dock="top-right"` (and optionally
 *   `data-canvas-order`) is picked up and docked, which is all a plugin needs
 *   to do -- nothing in core names it.
 *
 * A dock never changes the viewer: it moves controls, not the image, so a
 * panel opening leaves the camera, the zoom and the plane where they were.
 * Items that move because something arrived or grew glide there (a short
 * FLIP of their `translate`); under reduced motion they jump.
 *
 * Each dock's size is published (`--plx-dock-<corner>-w/-h`), and with it
 * the bottom LANE (`--plx-lane-left/right/bottom/top`, px from the wrapper's
 * edges) for a panel too wide for any corner -- the chat: between the bottom
 * corners while there is room, above them when there is not.
 *
 * WHEN A COLUMN RUNS OUT OF ROOM (a short window, a tall section pane) the
 * top dock shrinks first and its panels scroll. Past that, items that say
 * they can fold up (`place(el, {collapse})`, or `data-canvas-collapsible`
 * and a `plexora:canvas-collapse` listener) are folded, the last placed
 * first, until the column fits; they open again, in reverse, when the room
 * comes back. Only a change of the room itself does this -- a panel the user
 * opens in a tight spot is never folded back under their hand.
 *
 * MOVABLE CONTROLS. `movable(element, {id, handle})` lets the user drag a
 * control out of its place (a dock, or a line of the caption) to anywhere in
 * the safe area, by its handle only, so a slider or a field inside it works
 * as before. While moved it lives in the FREE LAYER, a box the size of the
 * safe area's content, at FRACTIONS of the room it can travel in (0 at the
 * left/top, 1 at the right/bottom): a control left in the bottom-right
 * corner stays in that corner when the canvas narrows, and one in the middle
 * stays in the middle, and none is ever pushed past an edge -- the CSS turns
 * the fractions into pixels on every layout, so a resize needs no script.
 * A small x beside it glides it home; dropped next to home, it goes home.
 * The fractions are kept per tab (sessionStorage, by `id`), so the next
 * sample of a dataset -- a full page load -- opens with the control where
 * the user left it.
 *
 * The plugin face is `ctx.view.dock(...)` / `ctx.view.reserve(...)` /
 * `ctx.view.movable(...)` (main.js). Classic script, `window.PlexoraCanvasLayout`.
 */
(function (global) {
    "use strict";

    const EDGES = ["top", "right", "bottom", "left"];
    const CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right",
                     "top-center", "bottom-center"];
    const DEFAULT_ORDER = 50;
    //: The narrowest the bottom LANE may be between the bottom corners
    //: before it moves up over them (px): a wide panel such as the chat.
    const LANE_MIN = 320;
    //: The safe area's padding and the gap between docked things (viewer.css).
    const EDGE = 12;
    const GLIDE_MS = 180;
    //: A moved control going home travels further than a docked one
    //: stepping aside, so it takes a little longer over it.
    const HOME_MS = 260;
    const EASE = "cubic-bezier(0.2, 0, 0, 1)";
    //: How far a press travels before it is a drag (px), as pointerDrag.js:
    //: a click on a handle stays a click.
    const MOVE_THRESHOLD = 4;
    //: Dropped this close to home (px), a moved control goes home.
    const HOME_SNAP = 12;
    //: sessionStorage, + the movable's id: where the user left it, this tab.
    const POSITION_KEY = "plexora:canvas-position:";
    //: Inside a handle, what a press means for itself rather than a drag.
    const INTERACTIVE = "button, input, textarea, select, a[href], [contenteditable], [role='slider'], .plx-slider";

    const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

    function el(tag, className) {
        const node = document.createElement(tag);
        node.className = className;
        return node;
    }

    function reducedMotion() {
        if (global.PlexoraViewerPrefs?.smoothMotion) return !global.PlexoraViewerPrefs.smoothMotion();
        try {
            return Boolean(global.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
        } catch (error) {
            return false;
        }
    }

    class CanvasLayout {
        constructor(wrapper) {
            this.wrapper = wrapper;
            this.bands = new Map();          // id -> {edge, size, order}
            this.docks = {};
            this.rects = new WeakMap();      // docked element -> last screen rect
            this.folders = new WeakMap();    // docked element -> collapse(bool)
            this.folded = [];                // folded by the layout, latest last
            this.build();
            this.apply();
            this.watch();
        }

        build() {
            const safe = el("div", "plx-canvas-safe");
            safe.setAttribute("aria-hidden", "false");
            const left = el("div", "plx-canvas-column is-left");
            const centre = el("div", "plx-canvas-column is-center");
            const right = el("div", "plx-canvas-column is-right");
            const make = (column, corner) => {
                const dock = el("div", `plx-canvas-dock is-${corner}`);
                dock.dataset.corner = corner;
                column.appendChild(dock);
                this.docks[corner] = dock;
            };
            make(left, "top-left");
            make(left, "bottom-left");
            make(centre, "top-center");
            make(centre, "bottom-center");
            make(right, "top-right");
            make(right, "bottom-right");
            // Last, so a control the user moved over a docked one is drawn
            // over it: it is where they just put it.
            const free = el("div", "plx-canvas-free");
            safe.append(left, centre, right, free);
            this.columns = [left, centre, right];
            this.free = free;
            this.safe = safe;
            this.wrapper.appendChild(safe);
        }

        // -- bands -------------------------------------------------------

        /** Take a band `size` px deep along `edge` out of the canvas; a size
         *  of 0 (or `release`) gives it back. */
        reserve(id, { edge, size = 0, order = DEFAULT_ORDER } = {}) {
            const key = String(id);
            if (!EDGES.includes(edge)) throw new Error(`canvasLayout: no edge "${edge}"`);
            const value = Math.max(0, Math.round(Number(size) || 0));
            const held = this.bands.get(key);
            if (held && held.edge === edge && held.size === value && held.order === order) {
                return () => this.release(key);
            }
            this.bands.set(key, { edge, size: value, order });
            this.apply();
            return () => this.release(key);
        }

        release(id) {
            if (this.bands.delete(String(id))) this.apply();
        }

        /** {top, right, bottom, left}: what the bands take from each edge. */
        insets() {
            const out = { top: 0, right: 0, bottom: 0, left: 0 };
            for (const band of this.bands.values()) out[band.edge] += band.size;
            return out;
        }

        apply() {
            const style = this.wrapper.style;
            const offsets = { top: 0, right: 0, bottom: 0, left: 0 };
            const ordered = [...this.bands.entries()]
                .sort((a, b) => a[1].order - b[1].order || a[0].localeCompare(b[0]));
            for (const [id, band] of ordered) {
                style.setProperty(`--plx-band-${id}`, `${offsets[band.edge]}px`);
                offsets[band.edge] += band.size;
            }
            for (const edge of EDGES) style.setProperty(`--plx-inset-${edge}`, `${offsets[edge]}px`);
            this.measure();
            this.announce();
        }

        // -- docks -------------------------------------------------------

        /**
         * Put `element` in a dock. `corner`: top-left, top-right, bottom-left,
         * bottom-right, top-center, bottom-center. Lower `order` sits nearer
         * the corner. Returns a function that takes it out again (back into
         * the wrapper, where an element with its own position keeps working).
         */
        place(element, { corner = "top-right", order = DEFAULT_ORDER, id = null,
                         collapse = null } = {}) {
            if (!element) return () => {};
            const dock = this.docks[CORNERS.includes(corner) ? corner : "top-right"];
            this.remember();
            element.dataset.canvasDock = dock.dataset.corner;
            element.dataset.canvasOrder = String(order);
            if (id) element.dataset.canvasId = String(id);
            if (typeof collapse === "function") this.folders.set(element, collapse);
            element.classList.add("plx-docked");
            const after = [...dock.children].find((child) =>
                Number(child.dataset.canvasOrder ?? DEFAULT_ORDER) > order && child !== element);
            dock.insertBefore(element, after || null);
            this.glide();
            this.announce();
            return () => {
                if (element.parentElement !== dock) return;
                this.remember();
                if (this.folded.includes(element)) this.fold(element, false);
                element.classList.remove("plx-docked");
                delete element.dataset.canvasDock;
                this.wrapper.appendChild(element);
                this.glide();
                this.announce();
            };
        }

        /** Let the user move `element` anywhere in the safe area by its
         *  `handle`, and put it back (MOVABLE CONTROLS, above). `element`
         *  must already be in its home. Returns its Movable. */
        movable(element, options = {}) {
            return element ? new Movable(this, element, options) : null;
        }

        /** Dock whatever arrives in the wrapper asking to be docked. */
        adopt(node) {
            if (!(node instanceof global.HTMLElement) || node.parentElement !== this.wrapper) return;
            const corner = node.dataset.canvasDock;
            if (!corner) return;
            this.place(node, { corner, order: Number(node.dataset.canvasOrder ?? DEFAULT_ORDER) });
        }

        watch() {
            if (typeof MutationObserver === "function") {
                this.arrivals = new MutationObserver((records) => {
                    for (const record of records) {
                        for (const node of record.addedNodes) this.adopt(node);
                    }
                });
                this.arrivals.observe(this.wrapper, { childList: true });
                // What is already there (a plugin may have mounted first).
                for (const node of [...this.wrapper.children]) this.adopt(node);
                // A docked item growing or shrinking moves its neighbours.
                this.changes = new MutationObserver(() => {
                    this.glide();
                    this.measure();
                });
                for (const dock of Object.values(this.docks)) {
                    this.changes.observe(dock, { childList: true });
                }
            }
            if (typeof ResizeObserver === "function") {
                this.sizes = new ResizeObserver((entries) => {
                    this.glide();
                    this.measure();
                    this.announce();
                    // The room changed (not just something in it): fit again.
                    if (entries.some((entry) => entry.target === this.safe)) this.scheduleFit();
                });
                for (const dock of Object.values(this.docks)) this.sizes.observe(dock);
                this.sizes.observe(this.safe);
            }
        }

        // -- folding when a column runs out of room -------------------------

        /** Can `element` fold up, and is it open now? */
        foldable(element) {
            return this.folders.has(element) || element.dataset?.canvasCollapsible != null;
        }

        /** Fold `element` (or open it again), by its own rule. */
        fold(element, collapsed) {
            const at = this.folded.indexOf(element);
            if (collapsed && at < 0) this.folded.push(element);
            if (!collapsed && at >= 0) this.folded.splice(at, 1);
            element.classList.toggle("plx-canvas-collapsed", collapsed);
            const rule = this.folders.get(element);
            try {
                if (rule) rule(collapsed);
                else element.dispatchEvent?.(new CustomEvent("plexora:canvas-collapse",
                                                             { detail: { collapsed } }));
            } catch (error) {
                console.warn("canvasLayout: a control could not fold", error);
            }
        }

        /** True when what a column holds, at the height each piece would
         *  take if nothing squeezed it, is taller than the column. Measured
         *  with the squeezing switched off for one synchronous layout
         *  (`plx-measuring`, viewer.css), so a panel that scrolls inside an
         *  inner list is seen as squeezed too. */
        crowded(column) {
            if (!column) return false;
            column.classList.add("plx-measuring");
            const wants = column.scrollHeight;
            column.classList.remove("plx-measuring");
            return wants > column.clientHeight + 1;
        }

        scheduleFit() {
            if (this._fitQueued) return;
            this._fitQueued = true;
            const run = () => {
                this._fitQueued = false;
                this.fit();
            };
            if (typeof global.requestAnimationFrame === "function") global.requestAnimationFrame(run);
            else setTimeout(run, 0);
        }

        /**
         * Fold what can fold, the last placed first, until every column fits;
         * then open what the layout folded, latest first, while it still fits.
         * Measuring after each step forces a layout, which is the point: it
         * happens inside one task, so nothing half-folded is ever painted.
         */
        fit() {
            for (const column of this.columns) {
                const items = () => [...column.children].flatMap((dock) => [...dock.children]);
                while (this.crowded(column)) {
                    const open = items().filter((child) => this.foldable(child)
                        && !this.folded.includes(child));
                    if (!open.length) break;
                    open.sort((a, b) => Number(b.dataset.canvasOrder ?? DEFAULT_ORDER)
                                      - Number(a.dataset.canvasOrder ?? DEFAULT_ORDER));
                    this.fold(open[0], true);
                }
                for (const child of [...this.folded].reverse()) {
                    if (!items().includes(child)) continue;
                    this.fold(child, false);
                    if (this.crowded(column)) {
                        this.fold(child, true);
                        break;
                    }
                }
            }
        }

        // -- moving without jumping -----------------------------------------

        /** Note where every docked item is, so a change can glide from there. */
        remember() {
            for (const dock of Object.values(this.docks)) {
                for (const child of dock.children) {
                    this.rects.set(child, child.getBoundingClientRect());
                }
            }
        }

        /** Where an item in this corner is held: the edge (or the middle) its
         *  dock stacks from on each axis. An item that only grows or shrinks
         *  keeps this point, so it does not glide; the neighbours it pushes
         *  move theirs, so they do. Measured from the top-left, a bottom-left
         *  item shrinking (the mini-map closing) read as a move, and slid down
         *  from where its top had been. */
        static anchor(corner, rect) {
            const [vertical, horizontal] = corner.split("-");
            const x = horizontal === "left" ? rect.left
                : horizontal === "right" ? rect.left + rect.width
                    : rect.left + rect.width / 2;
            const y = vertical === "bottom" ? rect.top + rect.height : rect.top;
            return { x, y };
        }

        /** Each docked item that moved since it was last seen slides from its
         *  old place to its new one. */
        glide() {
            // The safe area itself moved or resized (a band changed): that
            // glides by its own CSS transition, and items sliding on top of
            // it would trail behind. Only a change inside a dock glides here.
            const area = this.safe.getBoundingClientRect();
            const before = this.safeRect;
            this.safeRect = area;
            const areaMoved = before && (Math.abs(area.left - before.left) > 0.5
                || Math.abs(area.top - before.top) > 0.5
                || Math.abs(area.width - before.width) > 0.5
                || Math.abs(area.height - before.height) > 0.5);
            const still = reducedMotion() || areaMoved
                || this.wrapper.matches?.("[class*='is-resizing']");
            for (const [corner, dock] of Object.entries(this.docks)) {
                for (const child of dock.children) {
                    const now = child.getBoundingClientRect();
                    const was = this.rects.get(child);
                    this.rects.set(child, now);
                    if (still || !was || !(now.width || now.height)
                            || typeof child.animate !== "function") continue;
                    const from = CanvasLayout.anchor(corner, was);
                    const to = CanvasLayout.anchor(corner, now);
                    const dx = from.x - to.x;
                    const dy = from.y - to.y;
                    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
                    // Not from where it was when it had no size (just shown).
                    if (!(was.width || was.height)) continue;
                    child.animate([{ translate: `${dx}px ${dy}px` }, { translate: "0px 0px" }],
                                  { duration: GLIDE_MS, easing: "cubic-bezier(0.2, 0, 0, 1)" });
                }
            }
        }

        /** Publish how much each corner holds (`--plx-dock-<corner>-w/-h`
         *  on the wrapper): a panel too big for a corner (the chat, in the
         *  middle) sizes itself between them. An empty dock is 0. */
        measure() {
            const style = this.wrapper.style;
            const size = {};
            for (const [corner, dock] of Object.entries(this.docks)) {
                const shown = [...dock.children].some((child) => !child.hidden
                    && (child.offsetWidth || child.getBoundingClientRect().width));
                const rect = shown ? dock.getBoundingClientRect() : { width: 0, height: 0 };
                size[corner] = { w: Math.round(rect.width), h: Math.round(rect.height) };
                style.setProperty(`--plx-dock-${corner}-w`, `${size[corner].w}px`);
                style.setProperty(`--plx-dock-${corner}-h`, `${size[corner].h}px`);
            }
            // The LANE: where a panel wider than any corner goes along the
            // bottom. Between the bottom corners while that is wide enough;
            // otherwise across the whole safe area, above the taller corner.
            const inset = this.insets();
            const width = this.safe.getBoundingClientRect().width;
            const left = size["bottom-left"];
            const right = size["bottom-right"];
            // What sits in the middle of the bottom (the bookmarks tray): the
            // lane starts above it.
            const middle = size["bottom-center"] || { w: 0, h: 0 };
            const over = (h) => (h ? h + EDGE : 0);
            const beside = (part) => (part.w ? part.w + EDGE : 0);
            const between = width - 2 * EDGE - beside(left) - beside(right);
            const lane = between >= LANE_MIN
                ? { left: inset.left + EDGE + beside(left), right: inset.right + EDGE + beside(right),
                    bottom: inset.bottom + EDGE + over(middle.h) }
                : { left: inset.left + EDGE, right: inset.right + EDGE,
                    bottom: inset.bottom + EDGE + over(Math.max(left.h, right.h, middle.h)) };
            for (const key of ["left", "right", "bottom"]) {
                style.setProperty(`--plx-lane-${key}`, `${Math.round(lane[key])}px`);
            }
            // And what the top corners hold, whichever is taller, so the lane
            // knows how high it may reach.
            const top = Math.max(size["top-left"].h, size["top-right"].h, size["top-center"].h);
            style.setProperty("--plx-lane-top", `${Math.round(inset.top + EDGE + (top ? top + EDGE : 0))}px`);
        }

        announce() {
            clearTimeout(this._announce);
            this._announce = setTimeout(() => {
                try {
                    global.dispatchEvent(new CustomEvent("plexora:canvas-layout",
                        { detail: { insets: this.insets() } }));
                } catch (error) {
                    // No CustomEvent in a bare runtime.
                }
            }, 0);
        }
    }

    /**
     * One control the user may move (MOVABLE CONTROLS, above).
     *
     * HOME is where the element was when it was made movable -- a dock, or a
     * line of the caption -- held by a comment node, which takes no room: the
     * slot closes while the control is away and opens again when it comes
     * back, and what sat under it moves up meanwhile (the dock's glide).
     *
     * `at` is null at home, otherwise {x, y}, fractions of the room in the
     * free layer. A drag is pointer events on the WINDOW, not pointer
     * capture: the element changes parent when it is lifted, and a capture
     * ends when its element leaves the document.
     */
    class Movable {
        constructor(layout, element, { id = null, handle = null,
                                       back = "Move back to the corner" } = {}) {
            this.layout = layout;
            this.element = element;
            this.handle = handle || element;
            this.key = id ? POSITION_KEY + String(id) : null;
            this.at = null;
            this.press = null;
            this.homing = null;
            this.disposed = false;
            const doc = element.ownerDocument || global.document;
            this.home = doc.createComment(` movable${id ? ` ${id}` : ""} `);
            element.parentNode?.insertBefore(this.home, element);
            element.classList.add("plx-movable");
            this.handle.classList.add("plx-movable-handle");
            // The x that puts it back, at the end of the handle's line: beside
            // what the hand moved it by, shown only while it is away.
            const reset = doc.createElement("button");
            reset.type = "button";
            reset.className = "plx-movable-reset";
            reset.hidden = true;
            reset.title = back;
            reset.setAttribute("aria-label", back);
            const icon = doc.createElement("span");
            icon.className = "fas fa-xmark";
            icon.setAttribute("aria-hidden", "true");
            reset.appendChild(icon);
            this.handle.appendChild(reset);
            this.reset = reset;
            this.onDown = this.onDown.bind(this);
            this.onMove = this.onMove.bind(this);
            this.onUp = this.onUp.bind(this);
            this.onCancel = this.onCancel.bind(this);
            this.onKey = this.onKey.bind(this);
            this.onReset = (event) => {
                event.preventDefault();
                event.stopPropagation();
                this.goHome(true);
            };
            this.handle.addEventListener("pointerdown", this.onDown);
            reset.addEventListener("click", this.onReset);
            // Where the user left it, in this tab: before the first paint, so
            // the next sample opens with it there rather than gliding over.
            const saved = Movable.read(this.key);
            if (saved) this.moveTo(saved);
        }

        /** True while it is somewhere other than home. */
        get moved() {
            return Boolean(this.at);
        }

        // -- where it is ---------------------------------------------------

        /** Fractions of the room for a top-left `left, top` px into `area`,
         *  for a box of `size`; pinned inside, and 0 on an axis with no room. */
        static fractions({ left, top }, area, size) {
            const roomX = area.width - size.width;
            const roomY = area.height - size.height;
            return {
                x: roomX > 0 ? clamp(left / roomX, 0, 1) : 0,
                y: roomY > 0 ? clamp(top / roomY, 0, 1) : 0,
            };
        }

        /** The inverse, px into `area` -- what viewer.css computes from the
         *  `--plx-x/--plx-y` it is given. */
        static offset(at, area, size) {
            return {
                left: at.x * Math.max(0, area.width - size.width),
                top: at.y * Math.max(0, area.height - size.height),
            };
        }

        /** Put it at `at` in the free layer, at once. */
        moveTo(at) {
            if (this.disposed || !at) return;
            if (!this.at) this.lift();
            this.place(at);
        }

        place(at) {
            this.at = { x: clamp(Number(at.x) || 0, 0, 1), y: clamp(Number(at.y) || 0, 0, 1) };
            this.element.style.setProperty("--plx-x", String(this.at.x));
            this.element.style.setProperty("--plx-y", String(this.at.y));
            this.reset.hidden = false;
        }

        /** Out of home into the free layer. Whatever was gliding it (a dock's
         *  FLIP, the way home) would fight the position, so it stops. */
        lift() {
            this.stopHoming();
            for (const animation of this.element.getAnimations?.() || []) animation.cancel();
            this.layout.free.appendChild(this.element);
            this.element.classList.add("plx-floating");
        }

        stopHoming() {
            this.homing?.cancel?.();
            this.homing = null;
        }

        /** Back where it came from; `animate` glides it there from here. */
        goHome(animate = false) {
            const parent = this.home.parentNode;
            if (!this.at || !parent) return;
            const from = this.element.getBoundingClientRect();
            this.stopHoming();
            parent.insertBefore(this.element, this.home.nextSibling);
            this.element.classList.remove("plx-floating");
            this.element.style.removeProperty("--plx-x");
            this.element.style.removeProperty("--plx-y");
            this.at = null;
            this.reset.hidden = true;
            this.forget();
            if (!animate || reducedMotion() || typeof this.element.animate !== "function"
                    || !(from.width || from.height)) return;
            const to = this.element.getBoundingClientRect();
            const dx = from.left - to.left;
            const dy = from.top - to.top;
            if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
            this.homing = this.element.animate(
                [{ translate: `${dx}px ${dy}px` }, { translate: "0px 0px" }],
                { duration: HOME_MS, easing: EASE });
        }

        /** Where it would be at home, measured by putting it there and back
         *  inside one task: a layout is forced, nothing is painted. */
        homeRect() {
            const parent = this.home.parentNode;
            if (!parent) return null;
            if (!this.at) return this.element.getBoundingClientRect();
            parent.insertBefore(this.element, this.home.nextSibling);
            this.element.classList.remove("plx-floating");
            const rect = this.element.getBoundingClientRect();
            this.layout.free.appendChild(this.element);
            this.element.classList.add("plx-floating");
            return rect;
        }

        // -- the drag --------------------------------------------------------

        onDown(event) {
            if (this.disposed || this.press || event.button !== 0 || this.element.hidden) return;
            if (event.target?.closest?.(INTERACTIVE)) return;
            this.press = { x: event.clientX, y: event.clientY, pointer: event.pointerId,
                           dragging: false };
            global.addEventListener("pointermove", this.onMove, true);
            global.addEventListener("pointerup", this.onUp, true);
            global.addEventListener("pointercancel", this.onCancel, true);
            global.addEventListener("keydown", this.onKey, true);
            // No text selection, no focus moving, and not a press on the image.
            event.preventDefault();
            event.stopPropagation();
        }

        ours(event) {
            return this.press && (this.press.pointer == null || event.pointerId == null
                                  || event.pointerId === this.press.pointer);
        }

        onMove(event) {
            if (!this.ours(event)) return;
            const press = this.press;
            const dx = event.clientX - press.x;
            const dy = event.clientY - press.y;
            if (!press.dragging) {
                if (Math.hypot(dx, dy) < MOVE_THRESHOLD) return;
                this.begin();
            }
            event.preventDefault();
            const area = this.layout.free.getBoundingClientRect();
            const size = this.element.getBoundingClientRect();
            this.place(Movable.fractions({ left: press.left + dx, top: press.top + dy }, area, size));
        }

        begin() {
            const press = this.press;
            press.dragging = true;
            press.from = this.at;
            const rect = this.element.getBoundingClientRect();
            const area = this.layout.free.getBoundingClientRect();
            press.left = rect.left - area.left;
            press.top = rect.top - area.top;
            if (!this.at) this.lift();
            this.element.classList.add("is-moving");
            global.document?.documentElement?.classList.add("plx-moving-control");
        }

        onUp(event) {
            if (!this.ours(event)) return;
            const press = this.press;
            this.end();
            if (!press.dragging) return;
            // The click a drag's pointerup is followed by is not a click; it
            // comes in the same task, so the swallow goes on the next one.
            const swallow = (click) => {
                click.preventDefault();
                click.stopPropagation();
            };
            global.addEventListener("click", swallow, { capture: true, once: true });
            global.setTimeout(() => global.removeEventListener("click", swallow, true), 0);
            // Hidden under the hand (its panel closed mid-drag), it measures
            // as nothing at the origin, as its home does: it stays put.
            const now = this.element.getBoundingClientRect();
            const home = now.width || now.height ? this.homeRect() : null;
            if (home && Math.hypot(now.left - home.left, now.top - home.top) <= HOME_SNAP) {
                this.goHome(true);
            } else {
                this.save();
            }
        }

        /** The drag undone: back where it was when it was picked up. */
        undo(press) {
            if (!press?.dragging) return;
            if (press.from) this.place(press.from);
            else this.goHome(true);
        }

        onCancel(event) {
            if (!this.ours(event)) return;
            const press = this.press;
            this.end();
            this.undo(press);
        }

        onKey(event) {
            if (event.key !== "Escape" || !this.press?.dragging) return;
            event.preventDefault();
            event.stopPropagation();
            const press = this.press;
            this.end();
            this.undo(press);
        }

        end() {
            global.removeEventListener("pointermove", this.onMove, true);
            global.removeEventListener("pointerup", this.onUp, true);
            global.removeEventListener("pointercancel", this.onCancel, true);
            global.removeEventListener("keydown", this.onKey, true);
            this.element.classList.remove("is-moving");
            global.document?.documentElement?.classList.remove("plx-moving-control");
            this.press = null;
        }

        // -- remembered, this tab ----------------------------------------------

        static storage() {
            // sessionStorage throws rather than returning null with site
            // data blocked; a control that cannot remember still moves.
            try {
                return global.sessionStorage || null;
            } catch (error) {
                return null;
            }
        }

        static read(key) {
            if (!key) return null;
            try {
                const value = JSON.parse(Movable.storage()?.getItem(key) || "null");
                const fraction = (n) => typeof n === "number" && n >= 0 && n <= 1;
                return value && fraction(value.x) && fraction(value.y) ? { x: value.x, y: value.y } : null;
            } catch (error) {
                return null;
            }
        }

        save() {
            if (!this.key || !this.at) return;
            const round = (n) => Math.round(n * 1e4) / 1e4;
            try {
                Movable.storage()?.setItem(this.key,
                    JSON.stringify({ x: round(this.at.x), y: round(this.at.y) }));
            } catch (error) {
                // Full or blocked: it stays where it is for this page.
            }
        }

        forget() {
            if (!this.key) return;
            try {
                Movable.storage()?.removeItem(this.key);
            } catch (error) {
                // As save().
            }
        }

        /** Stop listening and give the element back to its home, as it was.
         *  Where the user left it is kept for the next one made with this id. */
        dispose() {
            if (this.disposed) return;
            if (this.press) this.end();
            this.stopHoming();
            this.disposed = true;
            this.handle.removeEventListener("pointerdown", this.onDown);
            this.reset.removeEventListener("click", this.onReset);
            this.reset.remove();
            this.handle.classList.remove("plx-movable-handle");
            this.element.classList.remove("plx-movable", "plx-floating");
            this.element.style.removeProperty("--plx-x");
            this.element.style.removeProperty("--plx-y");
            if (this.at) {
                if (this.home.parentNode) this.home.parentNode.insertBefore(this.element, this.home.nextSibling);
                else this.element.remove();
            }
            this.at = null;
            this.home.remove();
        }
    }

    let instance = null;

    /** The layout of #openseadragon_wrapper, built on first use; null on a
     *  page with no viewer. */
    function layout() {
        if (instance && instance.wrapper.isConnected) return instance;
        const wrapper = global.document?.getElementById?.("openseadragon_wrapper");
        instance = wrapper ? new CanvasLayout(wrapper) : null;
        return instance;
    }

    global.PlexoraCanvasLayout = {
        CORNERS, EDGES, CanvasLayout, Movable,
        get: layout,
        place: (element, options) => layout()?.place(element, options) || null,
        movable: (element, options) => layout()?.movable(element, options) || null,
        fit: () => layout()?.fit(),
        reserve: (id, options) => layout()?.reserve(id, options) || (() => {}),
        release: (id) => layout()?.release(id),
        insets: () => layout()?.insets() || { top: 0, right: 0, bottom: 0, left: 0 },
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraCanvasLayout;
    }
})(typeof window !== "undefined" ? window : globalThis);
