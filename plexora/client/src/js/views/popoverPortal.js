/**
 * PopoverPortal - the one place that decides where a floating popup lives.
 *
 * A popup that must escape its own row's stacking context gets appended to a
 * "portal" ancestor rather than to the element that opens it. <body> is the
 * obvious choice and the wrong one: the viewer's full-screen button fullscreens
 * #bodyDiv (see ImageViewer's "pre-full-page" handler), and the Fullscreen API
 * paints an opaque ::backdrop over everything that is not the fullscreen
 * element or a descendant of it. A menu parked on <body> is then a sibling of
 * the fullscreen element -- still laid out, still "open", still receiving
 * clicks in the abstract, but drawn underneath the backdrop where no z-index
 * can reach it. That is what "the channel dropdown does nothing in fullscreen"
 * is: it opened where nobody could see it.
 *
 * So the portal target is whatever is currently fullscreen, falling back to
 * <body>, and every portaled element is moved when that changes -- entering and
 * leaving fullscreen both, or leaving fullscreen would strand the menus inside
 * a #bodyDiv they no longer need to be in.
 */
const PopoverPortal = (() => {
    /** Every element currently entrusted to the portal, in DOM-move order. */
    const portaled = new Set();
    let listening = false;

    /** Where portaled elements belong right now. */
    function root() {
        // webkit prefix for Safari, which still ships only the prefixed
        // property on the versions we see in the wild.
        return document.fullscreenElement
            || document.webkitFullscreenElement
            || document.body;
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
