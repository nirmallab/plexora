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
 *
 * A MODAL <dialog> OVERRIDES ALL OF THAT while it is up. `showModal()` promotes
 * the dialog to the TOP LAYER, which is painted above the whole of the ordinary
 * document -- there is no z-index on <body> that reaches it. A menu parked on
 * <body> while such a dialog is open is therefore drawn UNDERNEATH it: open,
 * positioned, receiving nothing, invisible. That was "the gene dropdown opens
 * behind the Create gene groups dialog". The cure is the same shape as the
 * fullscreen one -- host the popup inside the thing that is on top -- so the
 * dialog adopts them for as long as it is open, and the `close` listener below
 * hands them back.
 */
const PopoverPortal = (() => {
    /** Every element currently entrusted to the portal, in DOM-move order. */
    const portaled = new Set();
    let listening = false;

    /**
     * The open modal dialog a popup would otherwise be drawn behind, if there
     * is one.
     *
     * `:modal` and not `dialog[open]`, because only `showModal()` reaches the
     * top layer: a dialog opened with `show()` is an ordinary element and an
     * ordinary z-index still clears it. The last one in the document wins,
     * which is the right guess for the nesting this app does (a dialog opened
     * from a dialog is appended to <body> after it).
     */
    function modalOnTop() {
        const open = document.querySelectorAll("dialog[open]");
        for (let i = open.length - 1; i >= 0; i -= 1) {
            try {
                if (open[i].matches(":modal")) return open[i];
            } catch (error) {
                // `:modal` is younger than <dialog> itself. Where the selector
                // is not understood, treat an open dialog as modal: the cost of
                // guessing wrong is a menu hosted one element deeper than it
                // needed to be, which nothing can see.
                return open[i];
            }
        }
        return null;
    }

    /** Where portaled elements belong right now. */
    function root() {
        // Ahead of the fullscreen question and not folded into it: the top
        // layer is above the ::backdrop of a fullscreen element as well, so a
        // dialog is the answer whether or not one is fullscreen.
        const modal = modalOnTop();
        if (modal) return modal;
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
        // Capture, because `close` does not bubble: it fires at the <dialog>
        // and nowhere else, and a document-level listener only sees it on the
        // way down. Without this a menu adopted by a dialog stays inside it
        // after it shuts -- still on the page, still openable by keyboard,
        // inside an element the rest of the app has stopped drawing.
        document.addEventListener("close", relocate, true);
    }

    return {
        /** Adopt `el` into the portal and keep it there across fullscreen. */
        attach(el) {
            portaled.add(el);
            listen();
            root().appendChild(el);
            return el;
        },
        /**
         * Put `el` back where it belongs, if that has moved since it was
         * attached.
         *
         * The events above cover fullscreen and a dialog closing. Nothing
         * fires when a dialog OPENS -- there is no such event -- so a popup
         * asks on its way up instead, which is the one moment its host can
         * have changed without anybody being told.
         */
        reseat(el) {
            if (!el || !portaled.has(el)) return el;
            const target = root();
            // Guarded rather than unconditional: re-appending an element to
            // the parent it is already in still moves it, which blurs whatever
            // is focused inside -- and a menu with a search box in it focuses
            // that box immediately before this is called.
            if (el.parentNode !== target) target.appendChild(el);
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
