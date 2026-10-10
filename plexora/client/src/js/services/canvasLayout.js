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
 * The plugin face is `ctx.view.dock(...)` / `ctx.view.reserve(...)`
 * (main.js). Classic script, `window.PlexoraCanvasLayout`.
 */
(function (global) {
    "use strict";

    const EDGES = ["top", "right", "bottom", "left"];
    const CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right",
                     "top-center", "bottom-center"];
    const DEFAULT_ORDER = 50;
    const GLIDE_MS = 180;

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
            safe.append(left, centre, right);
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
            this.announce();
        }

        // -- docks -------------------------------------------------------

        /**
         * Put `element` in a dock. `corner`: top-left, top-right, bottom-left,
         * bottom-right, top-center, bottom-center. Lower `order` sits nearer
         * the corner. Returns a function that takes it out again (back into
         * the wrapper, where an element with its own position keeps working).
         */
        place(element, { corner = "top-right", order = DEFAULT_ORDER, id = null } = {}) {
            if (!element) return () => {};
            const dock = this.docks[CORNERS.includes(corner) ? corner : "top-right"];
            this.remember();
            element.dataset.canvasDock = dock.dataset.corner;
            element.dataset.canvasOrder = String(order);
            if (id) element.dataset.canvasId = String(id);
            element.classList.add("plx-docked");
            const after = [...dock.children].find((child) =>
                Number(child.dataset.canvasOrder ?? DEFAULT_ORDER) > order && child !== element);
            dock.insertBefore(element, after || null);
            this.glide();
            this.announce();
            return () => {
                if (element.parentElement !== dock) return;
                this.remember();
                element.classList.remove("plx-docked");
                delete element.dataset.canvasDock;
                this.wrapper.appendChild(element);
                this.glide();
                this.announce();
            };
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
                this.changes = new MutationObserver(() => this.glide());
                for (const dock of Object.values(this.docks)) {
                    this.changes.observe(dock, { childList: true });
                }
            }
            if (typeof ResizeObserver === "function") {
                this.sizes = new ResizeObserver(() => {
                    this.glide();
                    this.announce();
                });
                for (const dock of Object.values(this.docks)) this.sizes.observe(dock);
                this.sizes.observe(this.safe);
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
            for (const dock of Object.values(this.docks)) {
                for (const child of dock.children) {
                    const now = child.getBoundingClientRect();
                    const was = this.rects.get(child);
                    this.rects.set(child, now);
                    if (still || !was || !(now.width || now.height)
                            || typeof child.animate !== "function") continue;
                    const dx = was.left - now.left;
                    const dy = was.top - now.top;
                    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
                    // Not from where it was when it had no size (just shown).
                    if (!(was.width || was.height)) continue;
                    child.animate([{ translate: `${dx}px ${dy}px` }, { translate: "0px 0px" }],
                                  { duration: GLIDE_MS, easing: "cubic-bezier(0.2, 0, 0, 1)" });
                }
            }
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
        CORNERS, EDGES, CanvasLayout,
        get: layout,
        place: (element, options) => layout()?.place(element, options) || null,
        reserve: (id, options) => layout()?.reserve(id, options) || (() => {}),
        release: (id) => layout()?.release(id),
        insets: () => layout()?.insets() || { top: 0, right: 0, bottom: 0, left: 0 },
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraCanvasLayout;
    }
})(typeof window !== "undefined" ? window : globalThis);
