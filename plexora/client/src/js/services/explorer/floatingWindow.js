/**
 * floatingWindow.js -- a modal <dialog> that behaves like a desktop window:
 * dragged by its title bar, resized from any edge or corner, maximized,
 * rolled up to its title bar, and put back where it was left for the rest of
 * the browser session.
 *
 * Still a modal dialog, on purpose. `showModal()` is what stacks it above the
 * import dialog that opened it (the top layer clears every z-index on the
 * page) and what keeps keyboard focus inside it. The dialog element itself is
 * the window: fixed position, an inline rect, no UA margin and no UA size cap.
 *
 * Three things the CSS must never put on the dialog or its chrome: transform,
 * filter and contain. Each makes the element the containing block of every
 * `position: fixed` descendant, and the menus PopoverPortal hosts inside an
 * open modal are exactly that -- they would open offset by the window's own
 * position. tests/test_file_explorer.py greps explorer.css for all three.
 *
 * Nothing here knows about files. fileExplorer.js fills `body` and `footer`.
 */
window.PlexoraFloatingWindow = (function () {
    "use strict";

    const T = () => window.PlexoraExplorerTypes;

    //: The title bar's height, which is also the whole window when rolled up.
    const TITLE_H = 36;
    //: Pointer travel before a press on the title bar becomes a drag, so a
    //: click with a slightly unsteady hand is still a click.
    const DRAG_THRESHOLD = 4;
    //: How much of the title bar a drag must leave on screen: enough to grab.
    const KEEP_VISIBLE = 80;
    const EDGES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
    const INTERACTIVE = "button, input, textarea, select, a[href], [contenteditable]";

    function storage() {
        try {
            return window.sessionStorage || null;
        } catch (error) {
            return null;
        }
    }

    function viewport() {
        return {
            width: window.innerWidth || 1440,
            height: window.innerHeight || 900,
        };
    }

    function reducedMotion() {
        try {
            return Boolean(window.matchMedia
                && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
        } catch (error) {
            return false;
        }
    }

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    /**
     * @function create - build a window. Returns its parts and controls;
     *   nothing is on screen until `open()`.
     * @param id - names the remembered rect (sessionStorage).
     * @param onLayout - called whenever the window's size changes, so content
     *   that measures itself (a virtualized list) can measure again.
     */
    function create({ id = "window", title = "", subtitle = "", icon = "fas fa-folder-open",
                      width = 1000, height = 650, minWidth = 640, minHeight = 420,
                      margin = 12, className = "", onLayout = null } = {}) {
        const { el, button, glyph, setIcon } = T();
        const key = `plexora:explorer-window:${id}`;

        const dialog = el("dialog", `plx-explorer-window ${className}`.trim());
        const chrome = el("div", "plx-explorer-chrome");
        const titleBar = el("div", "plx-explorer-titlebar");
        const titleId = `${id}-title-${Math.random().toString(36).slice(2, 8)}`;
        const heading = el("h2", "plx-explorer-title", title);
        heading.setAttribute("id", titleId);
        dialog.setAttribute("aria-labelledby", titleId);
        const sub = el("span", "plx-explorer-subtitle", subtitle);
        const text = el("div", "plx-explorer-titletext");
        text.append(heading, sub);

        const controls = el("div", "plx-explorer-wincontrols");
        const rollButton = button("plx-explorer-winbutton", "", "Minimize");
        rollButton.setAttribute("data-win", "rollup");
        rollButton.setAttribute("aria-pressed", "false");
        rollButton.append(glyph("far fa-window-minimize"));
        const maxButton = button("plx-explorer-winbutton", "", "Maximize");
        maxButton.setAttribute("data-win", "maximize");
        const maxGlyph = glyph("far fa-window-maximize");
        maxButton.append(maxGlyph);
        const closeButton = button("plx-explorer-winbutton is-close", "", "Close");
        closeButton.setAttribute("data-win", "close");
        closeButton.append(glyph("fas fa-xmark"));
        controls.append(rollButton, maxButton, closeButton);

        titleBar.append(glyph(icon, "plx-explorer-titleicon"), text, controls);

        const body = el("div", "plx-explorer-window-body");
        const footer = el("div", "plx-explorer-window-footer");
        chrome.append(titleBar, body, footer);
        dialog.append(chrome);

        const handles = EDGES.map((edge) => {
            const handle = el("div", `plx-explorer-resize is-${edge}`);
            handle.setAttribute("data-edge", edge);
            handle.setAttribute("aria-hidden", "true");
            dialog.append(handle);
            return handle;
        });

        const state = {
            rect: null,
            restoreRect: null,
            maximized: false,
            rolled: false,
            opener: null,
            drag: null,
            open: false,
            layoutQueued: false,
        };
        const layoutListeners = [];
        if (typeof onLayout === "function") layoutListeners.push(onLayout);
        const rollListeners = [];

        // -- the rect ----------------------------------------------------------

        function defaultRect() {
            const view = viewport();
            const w = Math.min(width, view.width - 2 * margin);
            const h = Math.min(height, view.height - 2 * margin);
            return {
                left: Math.round((view.width - w) / 2),
                top: Math.round(Math.max(margin, (view.height - h) / 2)),
                width: w,
                height: h,
            };
        }

        /**
         * Keep a rect usable. `loose` is for a drag in progress: the window
         * may hang off an edge as long as enough title bar stays to grab.
         * Otherwise the whole window fits, and a viewport narrower than the
         * minimum gets the viewport (minus the margin) rather than overflow.
         */
        function clampRect(rect, { loose = false } = {}) {
            const view = viewport();
            const maxW = Math.max(1, view.width - 2 * margin);
            const maxH = Math.max(1, view.height - 2 * margin);
            const w = clamp(Math.round(rect.width), Math.min(minWidth, maxW), maxW);
            const h = clamp(Math.round(rect.height), Math.min(minHeight, maxH), maxH);
            let left;
            let top;
            if (loose) {
                left = clamp(Math.round(rect.left), KEEP_VISIBLE - w, view.width - KEEP_VISIBLE);
                top = clamp(Math.round(rect.top), 0, view.height - TITLE_H);
            } else {
                left = clamp(Math.round(rect.left), margin, Math.max(margin, view.width - w - margin));
                top = clamp(Math.round(rect.top), margin, Math.max(margin, view.height - h - margin));
            }
            return { left, top, width: w, height: h };
        }

        function maximizedRect() {
            const view = viewport();
            return {
                left: margin,
                top: margin,
                width: Math.max(1, view.width - 2 * margin),
                height: Math.max(1, view.height - 2 * margin),
            };
        }

        function paint() {
            const rect = state.rect;
            if (!rect) return;
            dialog.style.left = `${rect.left}px`;
            dialog.style.top = `${rect.top}px`;
            dialog.style.width = `${rect.width}px`;
            dialog.style.height = state.rolled ? `${TITLE_H + 2}px` : `${rect.height}px`;
        }

        function setRect(rect, { loose = false } = {}) {
            state.rect = clampRect(rect, { loose });
            paint();
            queueLayout();
        }

        function rect() {
            return state.rect ? { ...state.rect } : null;
        }

        function queueLayout() {
            if (state.layoutQueued) return;
            state.layoutQueued = true;
            const run = () => {
                state.layoutQueued = false;
                layoutListeners.forEach((listener) => {
                    try { listener(); } catch (error) { console.error(error); }
                });
            };
            if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(run);
            else setTimeout(run, 0);
        }

        function glide(from) {
            if (!from || reducedMotion() || typeof dialog.animate !== "function") return;
            const to = state.rect;
            try {
                dialog.animate([
                    { left: `${from.left}px`, top: `${from.top}px`,
                      width: `${from.width}px`, height: `${from.height}px` },
                    { left: `${to.left}px`, top: `${to.top}px`,
                      width: `${to.width}px`, height: `${to.height}px` },
                ], { duration: 180, easing: "cubic-bezier(0.2, 0, 0, 1)" });
            } catch (error) {
                // Cosmetic. The rect is already where it belongs.
            }
        }

        // -- remembering it ----------------------------------------------------

        function save() {
            const store = storage();
            if (!store || !state.rect) return;
            const kept = state.maximized && state.restoreRect ? state.restoreRect : state.rect;
            try {
                store.setItem(key, JSON.stringify({ ...kept, maximized: state.maximized }));
            } catch (error) {
                // Private windows and full quotas: the window opens centred.
            }
        }

        function load() {
            const store = storage();
            if (!store) return null;
            try {
                const saved = JSON.parse(store.getItem(key) || "null");
                if (!saved || typeof saved !== "object") return null;
                const numbers = ["left", "top", "width", "height"]
                    .every((name) => Number.isFinite(saved[name]));
                return numbers ? saved : null;
            } catch (error) {
                return null;
            }
        }

        // -- maximize, restore, roll up ---------------------------------------

        function paintStates() {
            dialog.classList.toggle("is-maximized", state.maximized);
            dialog.classList.toggle("is-rolled", state.rolled);
            const label = state.maximized ? "Restore" : "Maximize";
            maxButton.setAttribute("title", label);
            maxButton.setAttribute("aria-label", label);
            setIcon(maxGlyph, state.maximized ? "far fa-window-restore" : "far fa-window-maximize");
            const roll = state.rolled ? "Restore window" : "Minimize";
            rollButton.setAttribute("title", roll);
            rollButton.setAttribute("aria-label", roll);
            rollButton.setAttribute("aria-pressed", state.rolled ? "true" : "false");
            if (state.rolled) titleBar.setAttribute("tabindex", "0");
            else titleBar.removeAttribute("tabindex");
        }

        function maximize() {
            if (state.maximized) return;
            const from = rect();
            state.restoreRect = rect();
            state.maximized = true;
            state.rect = maximizedRect();
            paint();
            paintStates();
            glide(from);
            save();
            queueLayout();
        }

        function restore() {
            if (!state.maximized) return;
            const from = rect();
            state.maximized = false;
            state.rect = clampRect(state.restoreRect || defaultRect());
            state.restoreRect = null;
            paint();
            paintStates();
            glide(from);
            save();
            queueLayout();
        }

        function toggleMaximize() {
            if (state.maximized) restore();
            else maximize();
        }

        function rollUp() {
            if (state.rolled) return;
            state.rolled = true;
            paint();
            paintStates();
            if (rollButton.focus) rollButton.focus();
            rollListeners.forEach((listener) => listener(true));
        }

        function unroll() {
            if (!state.rolled) return;
            state.rolled = false;
            paint();
            paintStates();
            queueLayout();
            rollListeners.forEach((listener) => listener(false));
        }

        function toggleRolled() {
            if (state.rolled) unroll();
            else rollUp();
        }

        // -- dragging the title bar -------------------------------------------

        function interactive(target) {
            return Boolean(target && target.closest && target.closest(INTERACTIVE));
        }

        function rootClass(name, on) {
            const root = document.documentElement;
            if (root && root.classList) root.classList.toggle(name, on);
        }

        function endDrag(commit) {
            const drag = state.drag;
            if (!drag) return;
            state.drag = null;
            try {
                if (drag.capture && drag.capture.releasePointerCapture) {
                    drag.capture.releasePointerCapture(drag.pointerId);
                }
            } catch (error) {
                // Already released by the browser.
            }
            document.removeEventListener("keydown", drag.onKey, true);
            rootClass(drag.rootClass, false);
            if (!commit) {
                state.rect = drag.start;
                paint();
                queueLayout();
                return;
            }
            if (drag.moved) {
                save();
                // The click that ends a drag is not a click on the title bar
                // (which, rolled up, would unroll it). A flag rather than a
                // stopPropagation: both listeners sit on the same element.
                if (!drag.edge) {
                    state.justDragged = true;
                    setTimeout(() => { state.justDragged = false; }, 0);
                }
            }
        }

        function beginDrag(event, { capture, edge }) {
            if (state.drag) return;
            const onKey = (keyEvent) => {
                if (keyEvent.key !== "Escape") return;
                // Before the dialog hears it: Esc mid-drag puts the window
                // back, it does not close it.
                keyEvent.preventDefault();
                keyEvent.stopPropagation();
                endDrag(false);
            };
            state.drag = {
                edge,
                capture,
                pointerId: event.pointerId,
                x: event.clientX,
                y: event.clientY,
                start: rect(),
                moved: false,
                onKey,
                rootClass: edge ? `plx-explorer-resizing-${edge}` : "plx-explorer-dragging",
            };
            try {
                if (capture.setPointerCapture) capture.setPointerCapture(event.pointerId);
            } catch (error) {
                // A synthetic pointer: the moves still arrive on the element.
            }
            document.addEventListener("keydown", onKey, true);
            if (edge) rootClass(state.drag.rootClass, true);
        }

        function moveDrag(event) {
            const drag = state.drag;
            if (!drag || event.pointerId !== drag.pointerId) return;
            const dx = event.clientX - drag.x;
            const dy = event.clientY - drag.y;
            if (!drag.moved) {
                if (!drag.edge && Math.abs(dx) < DRAG_THRESHOLD && Math.abs(dy) < DRAG_THRESHOLD) return;
                drag.moved = true;
                if (!drag.edge) rootClass(drag.rootClass, true);
            }
            if (drag.edge) {
                state.rect = resized(drag.start, drag.edge, dx, dy);
                paint();
                queueLayout();
                return;
            }
            setRect({ ...drag.start, left: drag.start.left + dx, top: drag.start.top + dy },
                    { loose: true });
        }

        /** One edge's arithmetic, from the rect the press started on. */
        function resized(start, edge, dx, dy) {
            const view = viewport();
            const minW = Math.min(minWidth, view.width - 2 * margin);
            const minH = Math.min(minHeight, view.height - 2 * margin);
            let { left, top, width: w, height: h } = start;
            if (edge.indexOf("e") >= 0) {
                w = clamp(start.width + dx, minW, view.width - margin - start.left);
            }
            if (edge.indexOf("w") >= 0) {
                const right = start.left + start.width;
                left = clamp(start.left + dx, margin, right - minW);
                w = right - left;
            }
            if (edge.indexOf("s") >= 0) {
                h = clamp(start.height + dy, minH, view.height - margin - start.top);
            }
            if (edge.indexOf("n") >= 0) {
                const bottom = start.top + start.height;
                top = clamp(start.top + dy, margin, bottom - minH);
                h = bottom - top;
            }
            return { left: Math.round(left), top: Math.round(top),
                     width: Math.round(w), height: Math.round(h) };
        }

        titleBar.addEventListener("pointerdown", (event) => {
            if (event.button !== undefined && event.button !== 0) return;
            if (interactive(event.target) || state.maximized) return;
            beginDrag(event, { capture: titleBar, edge: null });
        });
        titleBar.addEventListener("pointermove", moveDrag);
        titleBar.addEventListener("pointerup", (event) => {
            if (state.drag && !state.drag.edge) endDrag(true);
        });
        titleBar.addEventListener("pointercancel", () => endDrag(false));
        titleBar.addEventListener("dblclick", (event) => {
            if (interactive(event.target) || state.rolled || state.justDragged) return;
            toggleMaximize();
        });
        titleBar.addEventListener("click", (event) => {
            if (!state.rolled || state.justDragged || interactive(event.target)) return;
            unroll();
        });
        titleBar.addEventListener("keydown", (event) => {
            if (!state.rolled || event.target !== titleBar) return;
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                event.stopPropagation();
                unroll();
            }
        });

        handles.forEach((handle) => {
            const edge = handle.getAttribute("data-edge");
            handle.addEventListener("pointerdown", (event) => {
                if (event.button !== undefined && event.button !== 0) return;
                if (state.maximized || state.rolled) return;
                if (event.preventDefault) event.preventDefault();
                beginDrag(event, { capture: handle, edge });
            });
            handle.addEventListener("pointermove", moveDrag);
            handle.addEventListener("pointerup", () => {
                if (state.drag && state.drag.edge) endDrag(true);
            });
            handle.addEventListener("pointercancel", () => endDrag(false));
        });

        rollButton.addEventListener("click", () => toggleRolled());
        maxButton.addEventListener("click", () => toggleMaximize());
        closeButton.addEventListener("click", () => close());

        // -- the viewport ----------------------------------------------------

        function onViewport() {
            if (!state.open || !state.rect) return;
            if (state.maximized) state.rect = maximizedRect();
            else state.rect = clampRect(state.rect);
            paint();
            queueLayout();
        }

        // -- open and close --------------------------------------------------

        function open() {
            const saved = load();
            state.opener = document.activeElement || null;
            if (saved) {
                state.rect = clampRect(saved);
                if (saved.maximized) {
                    state.restoreRect = rect();
                    state.maximized = true;
                    state.rect = maximizedRect();
                }
            } else {
                state.rect = clampRect(defaultRect());
            }
            paint();
            paintStates();
            if (!dialog.parentNode) document.body.appendChild(dialog);
            state.open = true;
            window.addEventListener("resize", onViewport);
            dialog.showModal();
            queueLayout();
        }

        function close() {
            if (dialog.open) dialog.close();
        }

        dialog.addEventListener("close", () => {
            state.open = false;
            endDrag(false);
            window.removeEventListener("resize", onViewport);
            const opener = state.opener;
            state.opener = null;
            // Back to whatever opened this, when it is still on the page. A
            // window that closes into nowhere strands focus at the top of the
            // document, which inside another modal is outside it.
            if (opener && opener.focus && opener.isConnected !== false) {
                try { opener.focus(); } catch (error) { /* gone */ }
            }
        });

        function dispose() {
            window.removeEventListener("resize", onViewport);
            if (dialog.remove) dialog.remove();
        }

        return {
            dialog,
            chrome,
            titleBar,
            body,
            footer,
            setTitle(value) { heading.textContent = value || ""; },
            setSubtitle(value) { sub.textContent = value || ""; },
            open,
            close,
            maximize,
            restore,
            toggleMaximize,
            isMaximized: () => state.maximized,
            rollUp,
            unroll,
            toggleRolled,
            isRolled: () => state.rolled,
            isDragging: () => Boolean(state.drag),
            rect,
            setRect(next) { setRect(next); save(); },
            onLayout(listener) { layoutListeners.push(listener); },
            onRoll(listener) { rollListeners.push(listener); },
            dispose,
        };
    }

    return { create };
})();
