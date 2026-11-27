/**
 * canvasWindow.js - a window over the image the user can move, size, fold
 * and close.
 *
 * Docks (services/canvasLayout.js) place small controls so they cannot
 * overlap; a movable control can be dragged out of its dock. Neither fits a
 * panel the user works IN over the tissue -- an embedding they lasso while
 * watching the cells light up -- which wants a real size, set by the user,
 * and a place of its own. This is that window, as a core seam any plugin can
 * open:
 *
 *     const win = PlexoraCanvasWindow.open({
 *         id: "scimap-embedding",        // position and size remembered per tab
 *         title: node | "text",          // the head, beside the grip
 *         actions: [nodes],              // more of the head, before fold/close
 *         body: node,                    // the content
 *         collapsedLabel: () => "UMAP · 3,412 selected",
 *         width: 440, height: 420, minWidth: 300, minHeight: 240,
 *         onClose, onResize({width, height}), onCollapse(collapsed),
 *     });
 *     win.setCollapsedLabel(); win.collapse(true); win.maximize(true); win.close();
 *
 * Minimize folds it to a pill; Maximize (or a double-click on the head) fills
 * the room it can travel in and gives the old size back on the second press.
 * It is resized from any edge or corner.
 *
 * It lives in the canvas layout's FREE LAYER, the box over the safe area that
 * moved controls use, so it never sits under a band (the depth strip, a
 * section pane) and is pinned inside when the canvas narrows. Its place is
 * kept as FRACTIONS of the room it can travel in, as Movable does, so one
 * left in a corner stays in that corner; its size in px, clamped to the room.
 * Both are kept in this browser (localStorage, by `id`): the next image of a
 * dataset -- a full page load -- and the next time it is opened find the
 * window where the user left it.
 *
 * The look is the viewer's liquid glass (`.plx-view-back`, the bookmark
 * tray), in viewer.css under `.plx-float`.
 *
 * Classic script, `window.PlexoraCanvasWindow`.
 */
(function (global) {
    "use strict";

    const PLACE_KEY = "plexora:float:";
    const EDGE = 0;

    function clamp(v, lo, hi) {
        return Math.min(hi, Math.max(lo, v));
    }

    function el(tag, className, attrs) {
        const node = global.document.createElement(tag);
        if (className) node.className = className;
        for (const [k, v] of Object.entries(attrs || {})) {
            if (v !== null && v !== undefined) node.setAttribute(k, v);
        }
        return node;
    }

    function icon(name) {
        const span = el("span", `fas fa-${name}`, { "aria-hidden": "true" });
        return span;
    }

    function host() {
        const layout = global.PlexoraCanvasLayout?.get?.();
        return layout?.free || global.document.getElementById("openseadragon_wrapper") || global.document.body;
    }

    function read(id) {
        for (const store of ["localStorage", "sessionStorage"]) {
            try {
                const value = JSON.parse(global[store].getItem(PLACE_KEY + id) || "null");
                if (value) return value;
            } catch (error) { /* the next store */ }
        }
        return null;
    }

    function write(id, value) {
        try {
            global.localStorage.setItem(PLACE_KEY + id, JSON.stringify(value));
        } catch (error) { /* this page only */ }
    }

    const EDGES = ["n", "e", "s", "w", "ne", "nw", "se", "sw"];
    const MAX_INSET = 8;

    const open = new Map();

    class CanvasWindow {
        constructor(options) {
            this.options = Object.assign({
                width: 440, height: 420, minWidth: 280, minHeight: 220, at: { x: 1, y: 1 },
            }, options || {});
            this.id = String(this.options.id || "float");
            const saved = read(this.id) || {};
            this.at = saved.at || this.options.at;
            this.size = saved.size || { width: this.options.width, height: this.options.height };
            this.collapsed = Boolean(saved.collapsed);
            this.maximized = Boolean(saved.maximized);

            const root = el("section", "plx-float", { role: "dialog", "aria-label": this.options.label || "Floating window" });
            const head = el("div", "plx-float-head");
            const grip = el("span", "plx-float-grip", { "aria-hidden": "true" });
            grip.append(icon("grip-vertical"));
            const title = el("div", "plx-float-title");
            if (this.options.title instanceof global.Node) title.append(this.options.title);
            else title.textContent = this.options.title || "";
            const actions = el("div", "plx-float-actions");
            (this.options.actions || []).forEach((node) => node && actions.append(node));
            const fold = el("button", "plx-float-btn", { type: "button", "aria-label": "Minimize", title: "Minimize" });
            fold.append(icon("minus"));
            fold.addEventListener("click", () => this.collapse(true));
            const grow = el("button", "plx-float-btn", { type: "button", "aria-label": "Maximize", title: "Maximize" });
            grow.append(icon("expand"));
            grow.addEventListener("click", () => this.maximize(!this.maximized));
            this.growButton = grow;
            const close = el("button", "plx-float-btn", { type: "button", "aria-label": "Close", title: "Close" });
            close.append(icon("xmark"));
            close.addEventListener("click", () => this.close());
            head.append(grip, title, actions, fold, grow, close);
            head.addEventListener("dblclick", (event) => {
                if (event.target.closest("button, select, input, a, [data-no-drag]")) return;
                this.maximize(!this.maximized);
            });

            const body = el("div", "plx-float-body");
            if (this.options.body) body.append(this.options.body);
            const edges = EDGES.map((edge) => {
                const node = el("span", `plx-float-edge is-${edge}`, { "aria-hidden": "true" });
                node.dataset.edge = edge;
                return node;
            });
            const resize = el("span", "plx-float-resize", { "aria-hidden": "true", title: "Resize" });
            resize.dataset.edge = "se";

            // The folded form: a pill holding a short summary; a click opens it.
            const pill = el("button", "plx-float-pill", { type: "button", hidden: "" });
            pill.addEventListener("click", () => this.collapse(false));

            root.append(head, body, ...edges, resize);
            this.root = root;
            this.head = head;
            this.titleNode = title;
            this.body = body;
            this.pill = pill;

            this.parent = host();
            this.parent.append(root, pill);
            this.wireDrag(head, root);
            this.wireDrag(pill, pill, true);
            [...edges, resize].forEach((node) => this.wireResize(node));
            this.onRoom = () => this.layout();
            if (typeof ResizeObserver === "function") {
                this.room = new ResizeObserver(this.onRoom);
                this.room.observe(this.parent);
            }
            global.addEventListener("resize", this.onRoom);
            this.apply();
        }

        roomRect() {
            const r = this.parent.getBoundingClientRect();
            return { width: r.width, height: r.height, left: r.left, top: r.top };
        }

        /** Size clamped to the room and the minimums. */
        fitted() {
            const room = this.roomRect();
            const o = this.options;
            return {
                width: clamp(this.size.width, Math.min(o.minWidth, room.width), Math.max(o.minWidth, room.width)),
                height: clamp(this.size.height, Math.min(o.minHeight, room.height), Math.max(o.minHeight, room.height)),
            };
        }

        layout() {
            const room = this.roomRect();
            const node = this.collapsed ? this.pill : this.root;
            let width;
            let height;
            if (this.collapsed) {
                width = node.offsetWidth || 180;
                height = node.offsetHeight || 32;
            } else if (this.maximized) {
                width = Math.max(0, room.width - 2 * MAX_INSET);
                height = Math.max(0, room.height - 2 * MAX_INSET);
                node.style.width = width + "px";
                node.style.height = height + "px";
                node.style.left = MAX_INSET + "px";
                node.style.top = MAX_INSET + "px";
                return;
            } else {
                ({ width, height } = this.fitted());
                node.style.width = width + "px";
                node.style.height = height + "px";
            }
            const left = clamp(this.at.x, 0, 1) * Math.max(0, room.width - width - EDGE);
            const top = clamp(this.at.y, 0, 1) * Math.max(0, room.height - height - EDGE);
            node.style.left = left + "px";
            node.style.top = top + "px";
        }

        apply() {
            this.root.hidden = this.collapsed;
            this.pill.hidden = !this.collapsed;
            this.root.classList.toggle("is-maximized", this.maximized);
            if (this.growButton) {
                const label = this.maximized ? "Restore" : "Maximize";
                this.growButton.setAttribute("aria-label", label);
                this.growButton.title = label;
                // Font Awesome may have swapped the <span> for an <svg>: replace it whole.
                this.growButton.replaceChildren(icon(this.maximized ? "compress" : "expand"));
            }
            if (this.collapsed) this.setCollapsedLabel();
            this.layout();
        }

        save() {
            write(this.id, { at: this.at, size: this.size, collapsed: this.collapsed, maximized: this.maximized });
        }

        /** Fill the room, or give the size it had back; animated. */
        maximize(on) {
            const next = Boolean(on);
            if (next === this.maximized) return;
            this.maximized = next;
            this.root.classList.add("is-morphing");
            clearTimeout(this.morphTimer);
            this.morphTimer = setTimeout(() => {
                this.root.classList.remove("is-morphing");
                this.options.onResize?.(this.currentSize());
            }, 220);
            this.apply();
            this.save();
            this.options.onResize?.(this.currentSize());
        }

        currentSize() {
            return { width: this.root.offsetWidth, height: this.root.offsetHeight };
        }

        setCollapsedLabel(text) {
            const label = text !== undefined ? text
                : (typeof this.options.collapsedLabel === "function" ? this.options.collapsedLabel() : this.options.collapsedLabel);
            this.pill.textContent = "";
            const grip = el("span", "plx-float-grip", { "aria-hidden": "true" });
            grip.append(icon("grip-vertical"));
            const words = el("span", "plx-float-pill-text");
            words.textContent = label || "Open";
            const openWord = el("span", "plx-float-pill-open");
            openWord.textContent = "Open";
            this.pill.append(grip, words, openWord);
            if (this.collapsed) this.layout();
        }

        collapse(on) {
            const next = Boolean(on);
            if (next === this.collapsed) return;
            // The pill opens where the window's corner was, and the window
            // where the pill was left: one place, two sizes.
            this.collapsed = next;
            this.apply();
            this.save();
            this.options.onCollapse?.(next);
            if (!next) this.options.onResize?.(this.currentSize());
        }

        close() {
            if (this.closed) return;
            this.closed = true;
            this.room?.disconnect();
            global.removeEventListener("resize", this.onRoom);
            this.root.classList.add("is-leaving");
            const gone = () => { this.root.remove(); this.pill.remove(); };
            if (global.matchMedia?.("(prefers-reduced-motion: reduce)").matches) gone();
            else setTimeout(gone, 120);
            open.delete(this.id);
            this.options.onClose?.();
        }

        /** Drag by `handle`: pointer events on the window, so nothing inside
         *  the body (a lasso, a select) is affected. */
        wireDrag(handle, node, isPill) {
            let press = null;
            const move = (event) => {
                if (!press) return;
                const dx = event.clientX - press.x;
                const dy = event.clientY - press.y;
                if (!press.moved && Math.hypot(dx, dy) < 3) return;
                press.moved = true;
                const room = this.roomRect();
                const w = node.offsetWidth;
                const h = node.offsetHeight;
                const left = clamp(press.left + dx, 0, Math.max(0, room.width - w));
                const top = clamp(press.top + dy, 0, Math.max(0, room.height - h));
                node.style.left = left + "px";
                node.style.top = top + "px";
                this.at = {
                    x: room.width - w > 0 ? left / (room.width - w) : 0,
                    y: room.height - h > 0 ? top / (room.height - h) : 0,
                };
            };
            const up = () => {
                global.removeEventListener("pointermove", move);
                global.removeEventListener("pointerup", up);
                global.removeEventListener("pointercancel", up);
                node.classList.remove("is-dragging");
                if (press && press.moved) {
                    this.save();
                    if (isPill) this.suppressClick = true;
                }
                press = null;
            };
            handle.addEventListener("pointerdown", (event) => {
                if (event.button !== 0) return;
                if (!isPill && event.target.closest("button, select, input, a, [data-no-drag]")) return;
                if (!isPill && this.maximized) {
                    // Dragging a maximized window gives it its size back under the hand.
                    const fraction = (event.clientX - node.getBoundingClientRect().left) / Math.max(1, node.offsetWidth);
                    this.maximized = false;
                    this.apply();
                    const room = this.roomRect();
                    const left = clamp(event.clientX - room.left - fraction * node.offsetWidth, 0, Math.max(0, room.width - node.offsetWidth));
                    node.style.left = left + "px";
                    node.style.top = "0px";
                    this.save();
                }
                press = { x: event.clientX, y: event.clientY, left: node.offsetLeft, top: node.offsetTop, moved: false };
                node.classList.add("is-dragging");
                global.addEventListener("pointermove", move);
                global.addEventListener("pointerup", up);
                global.addEventListener("pointercancel", up);
            });
            if (isPill) {
                handle.addEventListener("click", (event) => {
                    if (this.suppressClick) {
                        this.suppressClick = false;
                        event.stopImmediatePropagation();
                        event.preventDefault();
                    }
                }, true);
            }
        }

        /** Resize from the edge or corner `handle.dataset.edge` names. */
        wireResize(handle) {
            const edge = handle.dataset.edge || "se";
            let press = null;
            let frame = 0;
            const move = (event) => {
                if (!press) return;
                const room = this.roomRect();
                const o = this.options;
                const dx = event.clientX - press.x;
                const dy = event.clientY - press.y;
                let { left, top, width, height } = press;
                if (edge.includes("e")) width = clamp(press.width + dx, o.minWidth, room.width - press.left);
                if (edge.includes("s")) height = clamp(press.height + dy, o.minHeight, room.height - press.top);
                if (edge.includes("w")) {
                    width = clamp(press.width - dx, o.minWidth, press.left + press.width);
                    left = press.left + press.width - width;
                }
                if (edge.includes("n")) {
                    height = clamp(press.height - dy, o.minHeight, press.top + press.height);
                    top = press.top + press.height - height;
                }
                Object.assign(this.root.style, { left: left + "px", top: top + "px", width: width + "px", height: height + "px" });
                this.size = { width, height };
                if (!frame) {
                    frame = global.requestAnimationFrame(() => {
                        frame = 0;
                        this.options.onResize?.(this.size);
                    });
                }
            };
            const up = () => {
                global.removeEventListener("pointermove", move);
                global.removeEventListener("pointerup", up);
                global.removeEventListener("pointercancel", up);
                this.root.classList.remove("is-resizing");
                if (press) {
                    // Keep the window where the hand left it.
                    const room = this.roomRect();
                    const w = this.root.offsetWidth;
                    const h = this.root.offsetHeight;
                    this.at = {
                        x: room.width - w > 0 ? this.root.offsetLeft / (room.width - w) : 0,
                        y: room.height - h > 0 ? this.root.offsetTop / (room.height - h) : 0,
                    };
                    this.save();
                }
                press = null;
            };
            handle.addEventListener("pointerdown", (event) => {
                if (event.button !== 0) return;
                event.preventDefault();
                event.stopPropagation();
                if (this.maximized) {
                    // A resize starts from the maximized box, which becomes the size.
                    this.maximized = false;
                    this.size = this.currentSize();
                    this.at = { x: 0.5, y: 0.5 };
                    this.apply();
                }
                press = { x: event.clientX, y: event.clientY, left: this.root.offsetLeft, top: this.root.offsetTop,
                          width: this.root.offsetWidth, height: this.root.offsetHeight };
                this.root.classList.add("is-resizing");
                global.addEventListener("pointermove", move);
                global.addEventListener("pointerup", up);
                global.addEventListener("pointercancel", up);
            });
        }
    }

    global.PlexoraCanvasWindow = {
        /** Open (or bring back) the window `options.id`. */
        open(options) {
            const id = String(options?.id || "float");
            const existing = open.get(id);
            if (existing && !existing.closed) {
                existing.collapse(false);
                return existing;
            }
            const win = new CanvasWindow(options);
            open.set(id, win);
            return win;
        },
        get(id) {
            const win = open.get(String(id));
            return win && !win.closed ? win : null;
        },
        CanvasWindow,
    };
    if (typeof module !== "undefined" && module.exports) module.exports = global.PlexoraCanvasWindow;
})(typeof window !== "undefined" ? window : globalThis);
