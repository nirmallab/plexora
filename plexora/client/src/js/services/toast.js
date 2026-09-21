/**
 * toast.js - the bottom-right notice: something happened, and you do not have
 * to do anything about it.
 *
 * Core had no such thing, and the gap showed. `PlexoraStatus` is the navbar
 * chip and says exactly three things (live, busy, failed) about the app as a
 * whole. `PlexoraResourceStatus`'s banner is a strip at the top of the page
 * that exists to OFFER A FIX -- a button that connects the machine holding the
 * missing layer -- which is why it sits where it cannot be missed.
 * `PlexoraConfirm` is a dialog, and its own header says why a statement with
 * nothing to decide should not be one.
 *
 * What was left over is the case this file is for: a thing that already
 * happened, that the user may want to know about, and that nothing is waiting
 * on. Walking to the next sample in a dataset and finding that two of the five
 * channels are not in it is the first of them -- the panel looks different
 * from the one just left, and the difference wants a sentence rather than a
 * dialog.
 *
 * So: bottom right, muted, dismissible, and gone on its own after twenty
 * seconds. It never blocks, never takes focus, and never asks anything.
 *
 * ONE AT A TIME, on purpose. Several notices about one action is the failure
 * this is supposed to prevent -- callers collect what they have to say and say
 * it once (services/carryOver.js's flush() is the worked example). A second
 * show() replaces the first rather than stacking under it.
 *
 * HOVER PAUSES THE CLOCK. A notice that disappears while it is being read is
 * worse than no notice, and twenty seconds is not long for a list of six
 * channel names somebody is checking against what they expected.
 */
window.PlexoraToast = (function () {
    "use strict";

    //: Long enough to read a short list and act on none of it. The notice is
    //: never the only record of what happened -- the panel it describes is
    //: right there -- so this is a glance, not a deadline.
    const DEFAULT_TIMEOUT_MS = 20000;

    const HOST_ID = "plexora_toast_host";

    //: The notice on screen, or null. One at a time; see the header.
    let live = null;

    function host() {
        let element = document.getElementById(HOST_ID);
        if (element) return element;
        if (!document.body) return null;
        element = document.createElement("div");
        element.id = HOST_ID;
        element.className = "plx-toast-host";
        // A live region that is always present and empty when it has nothing
        // to say, rather than one created at the moment it gains text: a
        // region that appears and fills in the same tick is the shape screen
        // readers miss. Same idiom as the quick-view status line.
        element.setAttribute("role", "status");
        element.setAttribute("aria-live", "polite");
        document.body.appendChild(element);
        return element;
    }

    function dismiss(entry) {
        if (!entry || entry.gone) return;
        entry.gone = true;
        window.clearTimeout(entry.timer);
        if (live === entry) live = null;
        const node = entry.node;
        if (!node) return;
        node.classList.add("is-leaving");
        // Removed after the fade rather than with it, so the notice does not
        // vanish mid-transition. The timeout is the CSS duration; a browser
        // with reduced motion has that token at 0ms and this lands next tick.
        window.setTimeout(() => {
            if (node.parentNode) node.parentNode.removeChild(node);
        }, 200);
    }

    function arm(entry, timeout) {
        window.clearTimeout(entry.timer);
        if (!(timeout > 0)) return;
        entry.timer = window.setTimeout(() => dismiss(entry), timeout);
    }

    /**
     * Show one notice.
     *
     * @param title the one line that has to be read. Required.
     * @param note an optional sentence under it -- what the list below is, or
     *   the whole of the message when there is no list.
     * @param lines an optional list of short items (channel names, panels).
     * @param timeout ms before it goes by itself; 0 to leave it until
     *   dismissed. Defaults to twenty seconds.
     * @returns {dismiss} so a caller that knows the notice is stale -- the
     *   thing it described has been undone -- can take it back.
     */
    function show(options) {
        const settings = options || {};
        if (!settings.title) return null;
        const mount = host();
        if (!mount) return null;

        // One at a time: whatever is up is about something the user has since
        // moved on from.
        if (live) dismiss(live);

        const node = document.createElement("div");
        node.className = "plx-toast";

        const head = document.createElement("div");
        head.className = "plx-toast-head";
        const title = document.createElement("span");
        title.className = "plx-toast-title";
        title.textContent = settings.title;
        head.appendChild(title);

        const close = document.createElement("button");
        close.type = "button";
        close.className = "plx-toast-dismiss";
        close.title = "Dismiss";
        close.setAttribute("aria-label", "Dismiss");
        // The glyph is markup rather than a Font Awesome span: this file is
        // loaded on every page and the notice must draw the same with the icon
        // bundle still in flight.
        close.textContent = "×";
        head.appendChild(close);
        node.appendChild(head);

        if (settings.note) {
            const note = document.createElement("p");
            note.className = "plx-toast-note";
            note.textContent = settings.note;
            node.appendChild(note);
        }

        const items = Array.isArray(settings.lines) ? settings.lines : [];
        if (items.length) {
            const list = document.createElement("ul");
            list.className = "plx-toast-lines";
            items.forEach((line) => {
                const row = document.createElement("li");
                row.textContent = line;
                list.appendChild(row);
            });
            node.appendChild(list);
        }

        const entry = { node, timer: null, gone: false };
        close.addEventListener("click", () => dismiss(entry));

        const timeout = settings.timeout === undefined
            ? DEFAULT_TIMEOUT_MS : Number(settings.timeout);
        // Reading is not idleness. The clock stops while the pointer is over
        // the notice and starts again, in full, when it leaves -- a list being
        // checked against what the user expected takes longer than the glance
        // this is timed for.
        node.addEventListener("mouseenter", () => window.clearTimeout(entry.timer));
        node.addEventListener("mouseleave", () => arm(entry, timeout));
        // Same for the keyboard: a notice whose dismiss button has focus is
        // one somebody is about to press.
        node.addEventListener("focusin", () => window.clearTimeout(entry.timer));
        node.addEventListener("focusout", () => arm(entry, timeout));

        mount.appendChild(node);
        live = entry;
        arm(entry, timeout);
        return { dismiss: () => dismiss(entry), node };
    }

    /** Take back whatever is showing. Nothing if nothing is. */
    function clear() {
        if (live) dismiss(live);
    }

    return { show, clear, DEFAULT_TIMEOUT_MS, HOST_ID };
})();
