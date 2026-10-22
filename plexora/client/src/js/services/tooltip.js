/**
 * tooltip.js -- the fast tooltip, drawn where nothing can cut it off.
 *
 * The native `title` attribute has a browser-enforced ~1s hover delay with no
 * CSS way to shorten it, so a control where that delay matters (HD Mode, the
 * Local/Remote switch, the navbar's connection icon) opts in with
 * `data-tooltip="..."` instead. Everywhere else keeps plain `title`.
 *
 * It used to be two pseudo-elements on the control itself, which meant it was
 * clipped by every `overflow` box between the control and the window: HD Mode
 * sits in a scrolling sidebar, and its bubble was cut in half by the sidebar's
 * edge. No z-index reaches past an overflow clip. So the bubble is now ONE
 * element on <body>, `position: fixed`, placed from the control's rectangle and
 * kept inside the window -- flipped to the other side when its own side has no
 * room, slid along when it would run off an edge, the tail still pointing at
 * the control it explains.
 *
 * The attributes are unchanged:
 *   data-tooltip="..."               the text; read again whenever it changes
 *   data-tooltip-placement="bottom"  prefer below (anything at the top of the
 *                                    window, where "above" is off the page)
 *   data-tooltip-align="right"       prefer to hang left from the right edge
 *
 * It goes away on press, and does not come back while the control says it is
 * expanded (`aria-expanded="true"`): the pointer is still over the navbar globe
 * after the click that opened its panel, and a label hanging in front of the
 * panel it just introduced has nothing left to say.
 *
 * Costs nothing until hovered: a few delegated listeners, and the element is
 * not made until the first tooltip is shown.
 */
window.PlexoraTooltip = (function () {
    "use strict";

    // Clear space between the bubble and the control, and between the bubble
    // and the window's edge.
    const GAP = 10;
    const MARGIN = 8;
    // How close the tail may get to the bubble's rounded corner.
    const TAIL_INSET = 16;

    let host = null;
    let bubble = null;
    let tail = null;
    let anchor = null;
    let watcher = null;
    // Pressed: stay down until the pointer leaves the control.
    let pressed = null;

    /**
     * Where a bubble of `size` goes against `rect`, inside `view`.
     *
     * Pure, so the probe can drive it with numbers. Returns the bubble's
     * top-left, which side of the control it ended up on, and the tail's x
     * within the bubble.
     */
    function place(rect, size, view, prefer = {}) {
        const below = prefer.placement === "bottom";
        const roomAbove = rect.top - GAP - MARGIN;
        const roomBelow = view.height - rect.bottom - GAP - MARGIN;
        let side = below ? "bottom" : "top";
        // Flip only when the other side fits better; a bubble that fits
        // nowhere stays where it was asked for.
        if (side === "top" && size.height > roomAbove && roomBelow > roomAbove) side = "bottom";
        else if (side === "bottom" && size.height > roomBelow && roomAbove > roomBelow) side = "top";

        const top = side === "top" ? rect.top - GAP - size.height : rect.bottom + GAP;
        const centre = rect.left + rect.width / 2;
        let left = prefer.align === "right"
            ? rect.right + 4 - size.width
            : centre - size.width / 2;
        const maxLeft = view.width - MARGIN - size.width;
        left = Math.max(MARGIN, Math.min(left, maxLeft));

        let tailX = centre - left;
        tailX = Math.max(TAIL_INSET, Math.min(tailX, size.width - TAIL_INSET));
        return { left: Math.round(left), top: Math.round(top), side, tailX: Math.round(tailX) };
    }

    function ensureHost() {
        if (host) return;
        host = document.createElement("div");
        host.className = "plx-tooltip";
        host.setAttribute("aria-hidden", "true");
        // Two siblings and not a bubble with a tail inside it: an element with
        // a backdrop-filter is a backdrop root for its children, so a tail
        // nested in the bubble would blur nothing and read as a dark notch.
        bubble = document.createElement("div");
        bubble.className = "plx-tooltip-bubble";
        bubble.id = "plx-tooltip";
        bubble.setAttribute("role", "tooltip");
        tail = document.createElement("div");
        tail.className = "plx-tooltip-tail";
        host.append(bubble, tail);
        // Through the portal, not straight onto <body>: a control inside a
        // modal <dialog> is in the top layer, above anything <body> can hold,
        // and a fullscreened subtree hides <body> under its backdrop.
        if (typeof PopoverPortal !== "undefined") PopoverPortal.attach(host);
        else document.body.appendChild(host);
    }

    function prefersOf(el) {
        return {
            placement: el.getAttribute("data-tooltip-placement") || "top",
            align: el.getAttribute("data-tooltip-align") || "",
        };
    }

    function layout() {
        if (!anchor || !host) return;
        const rect = anchor.getBoundingClientRect();
        const size = { width: bubble.offsetWidth, height: bubble.offsetHeight };
        const view = { width: document.documentElement.clientWidth, height: window.innerHeight };
        const spot = place(rect, size, view, prefersOf(anchor));
        host.dataset.side = spot.side;
        bubble.style.left = `${spot.left}px`;
        bubble.style.top = `${spot.top}px`;
        tail.style.left = `${spot.left + spot.tailX}px`;
        tail.style.top = spot.side === "top" ? `${spot.top + size.height}px` : `${spot.top}px`;
    }

    function show(el) {
        const text = el.getAttribute("data-tooltip");
        if (!text || el.getAttribute("aria-expanded") === "true") { hide(); return; }
        ensureHost();
        // Nothing fires when a dialog opens, so ask where to be on the way up.
        if (typeof PopoverPortal !== "undefined") PopoverPortal.reseat(host);
        if (anchor !== el) {
            hide();
            anchor = el;
            el.setAttribute("aria-describedby", bubble.id);
            // The text can change under the pointer -- the globe relabels
            // itself as a connection comes up -- and so can aria-expanded.
            watcher = new MutationObserver(() => {
                if (anchor) show(anchor);
            });
            watcher.observe(el, { attributes: true,
                                  attributeFilter: ["data-tooltip", "aria-expanded"] });
        }
        bubble.textContent = text;
        // Measured unshown and at the origin so the width is the text's own
        // (max-content up to the cap), not whatever room was left where it
        // last stood.
        bubble.style.left = "0px";
        bubble.style.top = "0px";
        layout();
        host.classList.add("is-shown");
    }

    function hide() {
        if (watcher) { watcher.disconnect(); watcher = null; }
        if (anchor && bubble && anchor.getAttribute("aria-describedby") === bubble.id) {
            anchor.removeAttribute("aria-describedby");
        }
        anchor = null;
        if (host) host.classList.remove("is-shown");
    }

    function owner(target) {
        return target && target.closest ? target.closest("[data-tooltip]") : null;
    }

    function onOver(event) {
        const el = owner(event.target);
        if (pressed && el !== pressed) pressed = null;
        if (!el) { if (anchor && !anchor.matches(":focus-visible")) hide(); return; }
        if (el === pressed) return;
        if (el !== anchor) show(el);
    }

    function onOut(event) {
        if (!anchor) return;
        const to = owner(event.relatedTarget);
        if (to === anchor) return;
        if (pressed === anchor) pressed = null;
        if (!anchor.matches(":focus-visible")) hide();
    }

    function onDown(event) {
        const el = owner(event.target);
        if (el) pressed = el;
        hide();
    }

    function onFocusIn(event) {
        const el = owner(event.target);
        if (el && el.matches(":focus-visible")) show(el);
    }

    function onFocusOut(event) {
        if (anchor && owner(event.target) === anchor && !anchor.matches(":hover")) hide();
    }

    function onKey(event) {
        if (event.key === "Escape" && anchor) hide();
    }

    function start() {
        document.addEventListener("mouseover", onOver, true);
        document.addEventListener("mouseout", onOut, true);
        document.addEventListener("pointerdown", onDown, true);
        document.addEventListener("focusin", onFocusIn, true);
        document.addEventListener("focusout", onFocusOut, true);
        document.addEventListener("keydown", onKey, true);
        // A bubble placed in window coordinates is wrong the moment anything
        // under it scrolls; leaving is cheaper and less surprising than chasing.
        window.addEventListener("scroll", hide, true);
        window.addEventListener("resize", hide);
        window.addEventListener("blur", hide);
    }

    if (typeof document !== "undefined" && document.addEventListener) start();

    return { place, hide };
})();
