/**
 * popoverMenu.js - a small action menu floated under a button.
 *
 * `PlexoraMenu.open(anchor, items)` for an overflow menu (the Image card's
 * `•••`, layerManager.js). One menu at a time; a click on an item closes the
 * menu and then runs it; the next click anywhere else, Escape, a scroll
 * outside it or a resize dismisses it -- and so does a second click on the
 * button that opened it.
 *
 * THE SECOND CLICK IS CAUGHT HERE, not left to the document listener: an
 * anchor inside a card header stops its click from propagating (or the header
 * would fold), so the document never hears it and the menu used to reopen on
 * top of itself. The ROI tree had the same bug and the same fix.
 *
 * Modelled on RoiTree.popup/menu (plugins/roi/static/roiTree.js), which
 * stays where it is: ROI's and Transcripts' menus may move onto this later.
 *
 * THROUGH PopoverPortal, not <body>: a menu appended to <body> opens under the
 * fullscreen backdrop and cannot be seen (tests/test_popover_portal.py). The
 * portal re-parents; positioning is here, fixed to the viewport because the
 * sidebar scrolls and an absolutely placed menu would scroll off its anchor.
 *
 * Items: `{ label, onSelect?, disabled?, className?, checked?, title? }`, or
 * `{ separator: true }`, or a row of glyph actions under one label:
 *   `{ label, actions: [{ icon, title, onSelect?, disabled? }] }`
 * -- "Channel names  [copy] [paste]" -- for a menu whose items come in pairs
 * over the same object, where four sentences would say the noun twice each.
 * Every action carries its `title` as tooltip and accessible name. Labels
 * and titles are text, never HTML. `checked` (a boolean) makes an item one
 * of a set, a radio row with a check beside the current one -- the Visium HD
 * heatmap's Mean / Sum / Max / Min.
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

    /** The same close-then-run every item does. Closed first, so whatever it
     *  opens (a file dialog, a toast) is not dismissed by the menu going. */
    function runner(button, onSelect) {
        return () => {
            if (button.disabled) return;
            close();
            onSelect?.();
        };
    }

    function buildRow(item) {
        const row = document.createElement("div");
        row.className = "plx-menu-row";
        row.setAttribute("role", "group");
        const label = String(item.label || "");
        row.setAttribute("aria-label", label);
        const text = document.createElement("span");
        text.className = "plx-menu-row-label";
        text.textContent = label;
        row.appendChild(text);
        const actions = document.createElement("div");
        actions.className = "plx-menu-row-actions";
        for (const action of item.actions) {
            if (!action) continue;
            const button = document.createElement("button");
            button.type = "button";
            button.className = "plx-menu-action";
            button.setAttribute("role", "menuitem");
            const title = String(action.title || "");
            button.title = title;
            button.setAttribute("aria-label", title);
            const glyph = document.createElement("span");
            glyph.className = String(action.icon || "");
            glyph.setAttribute("aria-hidden", "true");
            button.appendChild(glyph);
            button.disabled = Boolean(action.disabled);
            button.addEventListener("click", runner(button, action.onSelect));
            actions.appendChild(button);
        }
        row.appendChild(actions);
        return row;
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
            if (Array.isArray(item.actions)) {
                menu.appendChild(buildRow(item));
                continue;
            }
            const button = document.createElement("button");
            button.type = "button";
            button.className = ("plx-menu-item " + (item.className || "")).trim();
            if (typeof item.checked === "boolean") {
                // One of a set, like a radio: the menu says which is current
                // with a check in a gutter every row of the set keeps, so the
                // labels line up whichever one is ticked.
                button.classList.add("is-checkable");
                button.classList.toggle("is-checked", item.checked);
                button.setAttribute("role", "menuitemradio");
                button.setAttribute("aria-checked", String(item.checked));
            } else {
                button.setAttribute("role", "menuitem");
            }
            button.textContent = String(item.label || "");
            if (item.title) button.title = String(item.title);
            button.disabled = Boolean(item.disabled);
            button.addEventListener("click", runner(button, item.onSelect));
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
        // The button that opened the menu, pressed again: a toggle. See the
        // header for why the document listener cannot be the one to see it.
        if (current && current.anchor === anchor) {
            close();
            return close;
        }
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
            anchor,
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
        (el.querySelector?.(".plx-menu-item.is-checked:not([disabled])")
            || el.querySelector?.(".plx-menu-item:not([disabled])")
            || el.querySelector?.(".plx-menu-action:not([disabled])"))?.focus?.();
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
