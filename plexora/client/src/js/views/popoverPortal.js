/**
 * PopoverPortal - the one place that decides where a floating popup lives.
 *
 * A popup that must escape its own row's stacking context gets appended to a
 * "portal" ancestor rather than to the element that opens it. <body> is not
 * unconditionally the right host: the Fullscreen API paints an opaque
 * ::backdrop over everything that is not the fullscreen element or a
 * descendant of it, so when something SMALLER than the document goes
 * fullscreen, a menu parked on <body> becomes a sibling of it -- still laid
 * out, still "open", still receiving clicks in the abstract, but drawn
 * underneath the backdrop where no z-index can reach it. That is what "the
 * channel dropdown does nothing in fullscreen" was: it opened where nobody
 * could see it.
 *
 * The viewer's own full-screen button no longer creates that situation -- it
 * fullscreens the document element, which contains <body> and therefore
 * contains the menus (see ImageViewer's "pre-full-page" handler, and the
 * navbar it was hiding). This is kept because the guarantee is worth having
 * for any element that fullscreens a subtree, and because it costs one
 * containment check.
 *
 * So the portal target is <body> whenever <body> is inside the fullscreen
 * element or nothing is fullscreen at all, and the fullscreen element itself
 * otherwise; every portaled element is moved when that changes -- entering and
 * leaving fullscreen both, or leaving fullscreen would strand the menus inside
 * an element they no longer need to be in.
 */
const PopoverPortal = (() => {
    /** Every element currently entrusted to the portal, in DOM-move order. */
    const portaled = new Set();
    let listening = false;

    /** Where portaled elements belong right now. */
    function root() {
        // webkit prefix for Safari, which still ships only the prefixed
        // property on the versions we see in the wild.
        const full = document.fullscreenElement
            || document.webkitFullscreenElement;
        // A fullscreen element that CONTAINS <body> -- the document element,
        // which is what the viewer's button asks for -- leaves nothing under
        // the backdrop, so <body> stays the host and the markup stays where
        // the rest of the app expects to find it.
        if (!full || full.contains(document.body)) return document.body;
        return full;
    }

    /**
     * Move everything to the current portal target.
     *
     * Popups position themselves with viewport coordinates (position: fixed +
     * inline left/top), so a move does not invalidate a position. It does blur
     * whatever is focused inside a moved subtree, and the fullscreen transition
     * fires a resize besides -- both of which the popups already treat as
     * "close" -- so an open menu shuts on the toggle rather than being carried
     * across, which is the honest outcome anyway.
     */
    function relocate() {
        const target = root();
        portaled.forEach((el) => {
            if (el.parentNode !== target) {
                target.appendChild(el);
            }
        });
    }

    function listen() {
        if (listening) return;
        listening = true;
        document.addEventListener("fullscreenchange", relocate);
        document.addEventListener("webkitfullscreenchange", relocate);
    }

    return {
        /** Adopt `el` into the portal and keep it there across fullscreen. */
        attach(el) {
            portaled.add(el);
            listen();
            root().appendChild(el);
            return el;
        },
        /** Release `el` and take it off the page. Safe to call twice. */
        detach(el) {
            if (!el) return;
            portaled.delete(el);
            el.remove();
        },
        root,
    };
})();
