/**
 * viewerErrorState.js - the canvas saying why there is nothing on it.
 *
 * An image that cannot be read has always been a blank rectangle. The server
 * is loud about it -- the load raises and every tile comes back 500 -- but
 * nothing carried that to the screen: the spinner stopped, the sidebar drew,
 * and the picture was simply absent, with the reason in a terminal the user is
 * usually not looking at.
 *
 * FOUR CAUSES, FOUR SENTENCES. A file that has moved, a file this process may
 * not read, a file whose bytes are not an image, and a web address whose host
 * cannot be reached are different problems with different fixes, and "could
 * not load" covers all of them while helping with none. The server classifies
 * (data_model.image_status) and this says which.
 *
 * WHAT IT IS NOT. Not the node-unreachable case: a layer on a machine that is
 * asleep already has a banner, and that banner exists to offer a button that
 * connects the machine. This is for the cases with no such button -- where the
 * answer is to repoint the sample at its file, or to go and look at another
 * one -- so it is a statement, and it sits on the canvas where the picture
 * should be rather than in a strip at the top of the page.
 *
 * `offline` is the one status here that is not "repoint or leave": a web
 * address that cannot be reached right now needs neither, only for the host
 * to come back, so its card offers Retry instead of Repoint. Its OTHER shape
 * -- the image opened fine, from cache, while the host is unreachable -- is
 * not this card at all; it is `paintOfflineBanner`'s slim, non-blocking strip,
 * because nothing there is broken enough to stop the canvas taking clicks.
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
        offline: "This image is read from a web address that cannot be "
            + "reached right now. Parts already fetched still draw; the rest "
            + "will load when the connection is back.",
    };

    let root = null;
    //: The slim, non-blocking notice for the OTHER offline case -- see
    //: paintOfflineBanner.
    let banner = null;

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
     * @param report `{status, detail, src, ...}` from `/image_status`. Anything
     *   whose `status` is not a key of SENTENCES -- including "ok" and the
     *   node-unreachable "unavailable", which has its own banner -- shows
     *   nothing, so a caller can hand over whatever the server said without
     *   having to filter it first.
     * @param options `onRetry`, a function to call when the offline card's
     *   Retry button is pressed -- the caller's to supply, since re-probing
     *   `/image_status` is main.js's job, not this module's.
     */
    function show(report, options) {
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
        if (status === "offline") {
            // Nothing here is a file this sample points at wrongly -- only a
            // host that is not answering right now -- so there is no Repoint
            // action, and never a pin/"keep offline" button even when
            // `report.pinnable` says the store is not pinned yet: pinning
            // fetches the whole store, which needs exactly the host this card
            // exists because it cannot reach.
            const retry = el("button", "viewer-error-action", "Retry");
            retry.type = "button";
            retry.addEventListener("click", () => {
                if (options && typeof options.onRetry === "function") options.onRetry();
            });
            actions.appendChild(retry);
        } else {
            const sample = (window.flaskVariables && window.flaskVariables.datasource) || "";
            if (sample) {
                // The edit page, where the image field is -- this sample exists
                // and has its coordinate system, so pointing it at the file
                // again is an edit rather than a new import. A plain <a> so the
                // app shell picks it up like any other internal link.
                const edit = el("a", "viewer-error-action", "Repoint this sample");
                edit.href = plexoraUrl("edit_config/" + encodeURIComponent(sample));
                actions.appendChild(edit);
            }
        }
        const back = el("a", "viewer-error-action secondary", "Back to samples");
        back.href = backHref();
        actions.appendChild(back);
        card.appendChild(actions);

        root.appendChild(card);
        wrapper.appendChild(root);
        // A blocking card already says the image cannot be read; the slim
        // banner underneath it would be saying the same host is unreachable a
        // second, quieter way.
        paintOfflineBanner(null);
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

    /**
     * The slim, non-blocking notice for the offline case that is NOT a
     * failure: the image opened -- cached parts draw -- and only the host
     * behind it is unreachable right now.
     *
     * Idempotent and safe to call with every `/image_status` answer, whatever
     * its status: `report` is null to clear it, and anything other than
     * `{status: "ok", offline: true}` clears it too, which is what makes "a
     * later probe says offline: false" make it disappear -- the caller does
     * not have to remember to call `hide` separately.
     *
     * Never drawn under the blocking card (see `show`): a project that
     * cannot open at all has already said so, louder.
     */
    function paintOfflineBanner(report) {
        const wanted = Boolean(report && report.status === "ok" && report.offline)
            && !isShowing();
        if (!wanted) {
            if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
            banner = null;
            return;
        }
        if (banner) return;   // already up, saying the same thing
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper) return;
        banner = el("div", "viewer-offline-banner");
        banner.setAttribute("role", "status");
        const icon = el("span", "fas fa-cloud viewer-offline-banner-icon");
        icon.setAttribute("aria-hidden", "true");
        banner.appendChild(icon);
        banner.appendChild(el("span", null,
            "Offline — showing the parts of this image already on this computer."));
        wrapper.appendChild(banner);
    }

    return { show, hide, isShowing, paintOfflineBanner, SENTENCES };
})();
