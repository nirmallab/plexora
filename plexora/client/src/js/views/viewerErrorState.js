/**
 * viewerErrorState.js - the canvas saying why there is nothing on it.
 *
 * An image that cannot be read has always been a blank rectangle. The server
 * is loud about it -- the load raises and every tile comes back 500 -- but
 * nothing carried that to the screen: the spinner stopped, the sidebar drew,
 * and the picture was simply absent, with the reason in a terminal the user is
 * usually not looking at.
 *
 * THREE CAUSES, THREE SENTENCES. A file that has moved, a file this process
 * may not read, and a file whose bytes are not an image are three different
 * problems with three different fixes, and "could not load" covers all of them
 * while helping with none. The server classifies (data_model.image_status) and
 * this says which.
 *
 * WHAT IT IS NOT. Not the node-unreachable case: a layer on a machine that is
 * asleep already has a banner, and that banner exists to offer a button that
 * connects the machine. This is for the cases with no such button -- where the
 * answer is to repoint the sample at its file, or to go and look at another
 * one -- so it is a statement, and it sits on the canvas where the picture
 * should be rather than in a strip at the top of the page.
 *
 * THE REST OF THE PAGE STAYS LIVE. Only the card takes pointer events, so the
 * navbar, the status chip and above all the dataset's Prev/Next controls go on
 * working -- the most likely next thing the user wants is the next sample, and
 * a modal over the whole viewer would be one more thing to dismiss first.
 */
window.PlexoraViewerError = (function () {
    "use strict";

    //: What each classification means, in one sentence somebody can act on.
    //: The server's vocabulary; anything not in here is not shown at all,
    //: because a card that says "unknown" is a blank canvas with extra steps.
    const SENTENCES = {
        missing: "The image file is not where this sample says it is. "
            + "It may have been moved, renamed or deleted.",
        inaccessible: "The image file is there, but Plexora is not allowed to "
            + "read it. Check the file's permissions, or whether the drive it "
            + "is on is still mounted.",
        corrupt: "The image file is there and readable, but it could not be "
            + "opened as an image. It may be truncated, still being written, "
            + "or in a format Plexora does not read.",
    };

    let root = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    function backHref() {
        // Back to the folder this sample came from when it has one, which is
        // where the other samples are -- and to the full list when it does
        // not. The dataset nav already had to work out which folder that is.
        const id = window.PlexoraDatasetNav && window.PlexoraDatasetNav.datasetId
            ? window.PlexoraDatasetNav.datasetId() : null;
        const base = plexoraUrl("open_project");
        return id ? base + "?dataset=" + encodeURIComponent(id) : base;
    }

    /**
     * Put the card up.
     *
     * @param status one of the keys of SENTENCES. Anything else -- including
     *   "ok" and the node-unreachable "unavailable", which has its own banner
     *   -- shows nothing, so a caller can hand over whatever the server said
     *   without having to filter it first.
     * @param detail the server's own line about the failure, shown small. It
     *   is the thing that distinguishes two samples failing the same way.
     * @param src the path the sample records, so the message names the file
     *   somebody has to go and find.
     */
    function show(report) {
        const status = report && report.status;
        const sentence = SENTENCES[status];
        if (!sentence) return false;
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper) return false;
        hide();

        root = el("div", "viewer-error-state");
        root.setAttribute("role", "alert");

        const card = el("div", "viewer-error-card");

        const icon = el("span", "fas fa-triangle-exclamation viewer-error-icon");
        icon.setAttribute("aria-hidden", "true");
        card.appendChild(icon);

        card.appendChild(el("h2", "viewer-error-title", "This image could not be loaded"));
        card.appendChild(el("p", "viewer-error-sentence", sentence));

        if (report.src) {
            const path = el("p", "viewer-error-path", report.src);
            path.title = report.src;
            card.appendChild(path);
        }
        if (report.detail) {
            card.appendChild(el("p", "viewer-error-detail", report.detail));
        }

        const actions = el("div", "viewer-error-actions");
        const sample = (window.flaskVariables && window.flaskVariables.datasource) || "";
        if (sample) {
            // The edit page, where the image field is -- this sample exists and
            // has its coordinate system, so pointing it at the file again is an
            // edit rather than a new import. A plain <a> so the app shell picks
            // it up like any other internal link.
            const edit = el("a", "viewer-error-action", "Repoint this sample");
            edit.href = plexoraUrl("edit_config/" + encodeURIComponent(sample));
            actions.appendChild(edit);
        }
        const back = el("a", "viewer-error-action secondary", "Back to samples");
        back.href = backHref();
        actions.appendChild(back);
        card.appendChild(actions);

        root.appendChild(card);
        wrapper.appendChild(root);
        return true;
    }

    function hide() {
        if (root && root.parentNode) root.parentNode.removeChild(root);
        root = null;
    }

    /** Whether the canvas is currently explaining itself. Read by carryOver's
     *  flush(), which stays quiet about a channel it could not restore while
     *  something larger is already on screen. */
    function isShowing() {
        return Boolean(root);
    }

    return { show, hide, isShowing, SENTENCES };
})();
