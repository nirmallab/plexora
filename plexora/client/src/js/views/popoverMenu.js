/**
 * popoverMenu.js - a small action menu floated under a button.
 *
 * `PlexoraMenu.open(anchor, items)` for an overflow menu (the Image card's
 * `•••`, layerManager.js). One menu at a time; a click on an item closes the
 * menu and then runs it; the next click anywhere else, Escape, a scroll
 * outside it or a resize dismisses it.
 *
 * Modelled on RoiTree.popup/menu (plugins/roi/static/roiTree.js), which
 * stays where it is: ROI's and Transcripts' menus may move onto this later.
 *
 * THROUGH PopoverPortal, not <body>: a menu appended to <body> opens under the
 * fullscreen backdrop and cannot be seen (tests/test_popover_portal.py). The
 * portal re-parents; positioning is here, fixed to the viewport because the
 * sidebar scrolls and an absolutely placed menu would scroll off its anchor.
 *
 * Items: `{ label, onSelect?, disabled?, className? }`, or `{ separator: true }`.
 * Labels are text, never HTML.
 */
window.PlexoraMenu = (function () {
    "use strict";

    let current = null;

    function portal(el) {
        if (typeof PopoverPortal !== "undefined") PopoverPortal.attach(el);
        else document.documentElement.appendChild(el);
    }

    function unportal(el) {
        if (typeof PopoverPortal !== "undefined") PopoverPortal.detach(el);
        else el.remove();
    }

    function build(items) {
        const menu = document.createElement("div");
        menu.className = "plx-menu";
        menu.setAttribute("role", "menu");
        // A click inside is the menu's own business; the document-level
        // dismissal must not see it.
        menu.addEventListener("click", (event) => event.stopPropagation());
        for (const item of items || []) {
            if (!item) continue;
            if (item.separator) {
                const line = document.createElement("div");
                line.className = "plx-menu-separator";
                line.setAttribute("role", "separator");
                menu.appendChild(line);
                continue;
            }
            const button = document.createElement("button");
            button.type = "button";
            button.className = ("plx-menu-item " + (item.className || "")).trim();
            button.setAttribute("role", "menuitem");
            button.textContent = String(item.label || "");
            button.disabled = Boolean(item.disabled);
            button.addEventListener("click", () => {
                if (button.disabled) return;
                close();
                item.onSelect?.();
            });
            menu.appendChild(button);
        }
        return menu;
    }

    function place(anchor, el, align) {
        const box = anchor.getBoundingClientRect();
        el.style.position = "fixed";
        el.style.top = `${box.bottom + 4}px`;
        const width = el.offsetWidth || 200;
        const left = align === "left" ? box.left : box.right - width;
        el.style.left = `${Math.max(8, Math.min(left, window.innerWidth - width - 8))}px`;
        // Above when there is no room below and there is room above.
        const height = el.offsetHeight || 0;
        if (box.bottom + 4 + height > window.innerHeight - 8 && box.top - 4 - height > 8) {
            el.style.top = `${box.top - 4 - height}px`;
        }
    }

    /** Float `items` under `anchor`. Returns the function that closes it. */
    function open(anchor, items, { align = "right" } = {}) {
        close();
        const el = build(items);
        portal(el);
        place(anchor, el, align);
        anchor.setAttribute("aria-expanded", "true");

        const onKey = (event) => {
            // Capture, and stopped: Escape here shuts the menu and nothing else
            // (a tool's own Escape deselects).
            if (event.key !== "Escape") return;
            event.stopPropagation();
            close();
        };
        const onScroll = (event) => { if (!el.contains(event.target)) close(); };
        const onDocumentClick = () => close();
        // Next tick, or the click that opened this would shut it.
        const timer = window.setTimeout(
            () => document.addEventListener("click", onDocumentClick), 0);
        document.addEventListener("keydown", onKey, true);
        document.addEventListener("scroll", onScroll, true);
        window.addEventListener("resize", onDocumentClick);

        current = {
            el,
            teardown() {
                window.clearTimeout(timer);
                document.removeEventListener("click", onDocumentClick);
                document.removeEventListener("keydown", onKey, true);
                document.removeEventListener("scroll", onScroll, true);
                window.removeEventListener("resize", onDocumentClick);
                anchor.setAttribute("aria-expanded", "false");
                unportal(el);
            },
        };
        el.querySelector?.(".plx-menu-item:not([disabled])")?.focus?.();
        return close;
    }

    function close() {
        if (!current) return;
        const menu = current;
        current = null;
        menu.teardown();
    }

    return { open, close, isOpen: () => Boolean(current) };
})();
