/**
 * Drag and drop inside the page, driven by pointer events.
 *
 * For the desktop app's window only. The shell takes file drops natively --
 * that is what hands it a dropped slide's PATH instead of its bytes -- and on
 * Windows that native handler swallows every HTML5 drag event on the page,
 * including drags that never leave it. The two hand-written drags in the app
 * (a project card onto a dataset folder, a tray panel onto the figure page)
 * attach this alongside their HTML5 code: a browser keeps the native drag it
 * always had, and the app window gets one built from pointerdown/move/up.
 *
 * Nothing moves until the pointer has travelled a few pixels, so a click on
 * a card or a tray item is still exactly a click.
 */
window.PlexoraPointerDrag = (function () {
    "use strict";

    const THRESHOLD = 4;

    /**
     * @param root - where drags can start (delegated; children may be
     *   re-rendered freely).
     * @param source - selector for a draggable element under `root`.
     * @param payload - `(sourceEl, event) => data | null`; null refuses.
     * @param label - `(data, sourceEl) => string` for the chip that follows
     *   the pointer.
     * @param targetAt - `(elementUnderPointer, data) => dropTargetEl | null`.
     * @param onOver - `(targetEl, isOver)`, for hover styling.
     * @param onDrop - `(targetEl, data, point)` where point has clientX/Y.
     * @returns a function that detaches everything.
     */
    function attach(root, { source, payload, label, targetAt, onOver, onDrop }) {
        if (!root) return () => {};
        let pending = null;
        let drag = null;

        function reset() {
            window.removeEventListener("pointermove", onMove, true);
            window.removeEventListener("pointerup", onUp, true);
            window.removeEventListener("pointercancel", cancel, true);
            window.removeEventListener("keydown", onKey, true);
            if (drag) {
                if (drag.target && onOver) onOver(drag.target, false);
                drag.ghost.remove();
                drag.sourceEl.classList.remove("is-dragging");
                document.documentElement.classList.remove("is-pointer-dragging");
            }
            pending = null;
            drag = null;
        }

        function cancel() {
            reset();
        }

        function onKey(event) {
            if (event.key !== "Escape" || !drag) return;
            event.preventDefault();
            event.stopPropagation();
            reset();
        }

        function begin(event) {
            const data = payload(pending.sourceEl, event);
            if (data == null) {
                reset();
                return;
            }
            const ghost = document.createElement("div");
            ghost.className = "plx-drag-ghost";
            ghost.textContent = label ? label(data, pending.sourceEl) : "";
            document.body.appendChild(ghost);
            pending.sourceEl.classList.add("is-dragging");
            document.documentElement.classList.add("is-pointer-dragging");
            drag = { data, ghost, sourceEl: pending.sourceEl, target: null };
        }

        function onMove(event) {
            if (!pending) return;
            if (!drag) {
                const dx = event.clientX - pending.x;
                const dy = event.clientY - pending.y;
                if (Math.hypot(dx, dy) < THRESHOLD) return;
                begin(event);
                if (!drag) return;
            }
            event.preventDefault();
            drag.ghost.style.left = `${event.clientX + 12}px`;
            drag.ghost.style.top = `${event.clientY + 12}px`;
            const under = document.elementFromPoint(event.clientX, event.clientY);
            const target = under ? targetAt(under, drag.data) : null;
            if (target !== drag.target) {
                if (drag.target && onOver) onOver(drag.target, false);
                if (target && onOver) onOver(target, true);
                drag.target = target;
            }
        }

        function onUp(event) {
            const finished = drag;
            const point = { clientX: event.clientX, clientY: event.clientY };
            reset();
            if (!finished) return;
            // The click that follows a drag's pointerup is not a click. It is
            // dispatched in the same task as the pointerup, so the swallow is
            // withdrawn on the next one: a drag that ends with no click must
            // not eat the user's next real one.
            const swallow = (click) => {
                click.preventDefault();
                click.stopPropagation();
            };
            window.addEventListener("click", swallow, { capture: true, once: true });
            window.setTimeout(() => window.removeEventListener("click", swallow, true), 0);
            if (finished.target) onDrop(finished.target, finished.data, point);
        }

        function onDown(event) {
            if (event.button !== 0 || pending) return;
            const sourceEl = event.target.closest?.(source);
            if (!sourceEl || !root.contains(sourceEl)) return;
            if (event.target.closest("input, textarea, select, [contenteditable]")) return;
            pending = { sourceEl, x: event.clientX, y: event.clientY };
            window.addEventListener("pointermove", onMove, true);
            window.addEventListener("pointerup", onUp, true);
            window.addEventListener("pointercancel", cancel, true);
            window.addEventListener("keydown", onKey, true);
        }

        // The HTML5 drag would start too, and cancel these pointer events the
        // moment it did. In the app's window it could never be dropped anyway.
        function onNativeDragStart(event) {
            if (event.target.closest?.(source)) event.preventDefault();
        }

        root.addEventListener("pointerdown", onDown);
        root.addEventListener("dragstart", onNativeDragStart, true);
        return () => {
            reset();
            root.removeEventListener("pointerdown", onDown);
            root.removeEventListener("dragstart", onNativeDragStart, true);
        };
    }

    return { attach, THRESHOLD };
})();
