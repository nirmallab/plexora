/**
 * datasetNav.js - Previous and Next, in the corner of the canvas.
 *
 * A dataset is a folder of samples that belong together -- a cohort, a slide
 * run, a time course. Looking at one of them almost always means looking at
 * the next one, and until now that meant leaving the viewer for the Samples
 * page and coming back, which threw away the channels, the colours, the open
 * tools and everything else that had been arranged.
 *
 * WHERE IT SITS, and why it is quiet. Top right of the image, muted until the
 * pointer is near it. The corner is the only one free (the sample's name is
 * top left, the scale bar and the lens are bottom left) but that is not the
 * reason: this is a way OUT of what is on screen, and a control that competes
 * with the picture for attention is a control in the way of the thing it
 * exists to let you leave.
 *
 * WHICH ORDER. `Dataset.projects`, the order the samples were added, which the
 * server has preserved from the beginning for this exact feature (see
 * server/models/datasets.py). Deliberately NOT the order the Samples page
 * shows: that sorts by "last opened" by default, and every open rewrites that
 * key -- so walking a cohort would reshuffle the walk as you went, and Next
 * twice could land you back where you started.
 *
 * WHY IT IS A FULL NAVIGATION. The server holds one loaded datasource, and the
 * viewer has no teardown path; appRouter.js declines cross-project links for
 * those reasons and this does not pretend otherwise. What makes the move feel
 * continuous is not keeping the page -- it is carrying the arrangement across
 * it (services/carryOver.js).
 *
 * IT DOES NOT DEPEND ON THE VIEWER HAVING BOOTED. A sample whose image is
 * missing gets an error card and no viewer at all, and the way out of that
 * sample is this control. So it mounts off its own data, from its own fetch,
 * and asks main.js for nothing.
 */
window.PlexoraDatasetNav = (function () {
    "use strict";

    //: Resolved once per page: which dataset this sample is in and who its
    //: neighbours are. Null until the fetch lands, and for a sample that is in
    //: no dataset at all -- which is most of them on a fresh install.
    let place = null;

    //: A navigation is in flight. A second click during it would capture the
    //: arrangement again, over a page that is already leaving.
    let leaving = false;

    let root = null;
    let previousButton = null;
    let nextButton = null;

    //: Whether the bare keys (PageUp/PageDown, B/N) are live. Off while the app shell
    //: has a routed page (Settings, Figures) over the viewer: the keys belong
    //: to the image, and that page has its own scrolling to do.
    let keysArmed = true;

    //: Which key walks which way. B and N sit side by side and read as Back
    //: and Next on the caps beside the chevrons. Chosen because nothing else
    //: binds them bare: the ROI tools take V/P/F/R, viewerControls T, Figure
    //: Builder C (and S while capture is armed), and OpenSeadragon pans on
    //: W/A/S/D once the canvas has focus.
    const KEY_DIRECTION = { PageUp: "previous", PageDown: "next", b: "previous", n: "next" };
    const KEY_CAP = { previous: "B", next: "N" };

    function datasource() {
        return (window.flaskVariables && window.flaskVariables.datasource) || "";
    }

    /**
     * Which samples this Plexora can actually open, as a Set.
     *
     * A dataset's member list is names, and a name can outlive what it names:
     * a shared root that is not mounted, a sample deleted from another tab.
     * Walking onto one of those is a 404, so they are skipped rather than
     * offered. The page's own list is the one that cannot be stale about this.
     */
    function known() {
        const names = (window.flaskVariables && window.flaskVariables.datasources) || [];
        return new Set(Array.isArray(names) ? names : []);
    }

    /** The dataset holding this sample, and where in it this sample sits. */
    async function resolve() {
        const here = datasource();
        if (!here) return null;
        const response = await fetch(plexoraUrl("datasets"), { credentials: "same-origin" });
        if (!response.ok) return null;
        const payload = await response.json();
        const datasets = (payload && payload.datasets) || [];
        const openable = known();
        for (const dataset of datasets) {
            const members = (dataset.projects || []).filter(
                (name) => name === here || openable.has(name));
            const index = members.indexOf(here);
            if (index === -1) continue;
            return {
                datasetId: dataset.id,
                datasetName: dataset.name,
                members,
                index,
                previous: index > 0 ? members[index - 1] : null,
                next: index < members.length - 1 ? members[index + 1] : null,
            };
        }
        return null;
    }

    /**
     * Leave for `target`, carrying what this page has arranged.
     *
     * The capture happens HERE rather than in an unload handler: an unload
     * handler runs after the browser has begun tearing the page down, when
     * reading a panel's state is a race it sometimes loses. This runs while
     * everything is still up, and the navigation is the next statement.
     */
    function go(target) {
        if (!target || leaving) return;
        leaving = true;
        try {
            window.PlexoraCarryOver && window.PlexoraCarryOver.stash(target);
        } catch (error) {
            console.error("datasetNav: could not carry the current state", error);
        }
        // The tool rides in the URL rather than only in the snapshot, because
        // the SERVER renders the active tool's panel (page_routes' ?tool=) and
        // a panel that is server-rendered is one the new page does not have to
        // fetch, inject and boot before it can be shown. The rest of the open
        // tools are reopened client-side by toolLoader.restore.
        let href = plexoraUrl(encodeURIComponent(target));
        const active = window.PlexoraToolLoader && window.PlexoraToolLoader.activeTool
            ? window.PlexoraToolLoader.activeTool() : "";
        if (active) href += "?tool=" + encodeURIComponent(active);
        // Through the router, which declines a cross-project link and hands it
        // to the browser -- the full navigation this needs. Going straight to
        // window.location would work today and would be the one call site that
        // stops working if that rule is ever relaxed.
        if (window.PlexoraRouter && window.PlexoraRouter.go) window.PlexoraRouter.go(href);
        else window.location.href = href;
    }

    /** Whether a keystroke is the user talking to the app rather than typing. */
    function isTyping() {
        const active = document.activeElement;
        if (!active) return false;
        const tag = active.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
            || active.isContentEditable;
    }

    function onKeyDown(event) {
        if (!keysArmed || !place) return;
        if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
        // Lower-cased when it is one character, so Caps Lock's "B" still walks.
        const raw = event.key || "";
        const direction = KEY_DIRECTION[raw.length === 1 ? raw.toLowerCase() : raw];
        if (!direction) return;
        if (isTyping()) return;
        // A dialog owns the window while it is up: <dialog> traps focus but not
        // keystrokes, so the guard above does not catch a key pressed with a
        // button focused. Same rule as the overlay key in viewerControls.
        if (document.querySelector("dialog[open]")) return;
        const target = direction === "previous" ? place.previous : place.next;
        if (!target) return;
        // Only once it is going to do something: an unhandled PageDown still
        // scrolls whatever has the scrollbar, which is what it is for.
        event.preventDefault();
        go(target);
    }

    function button(direction, label, target) {
        const element = document.createElement("button");
        element.type = "button";
        element.className = "dataset-nav-button";
        element.dataset.direction = direction;
        const icon = document.createElement("span");
        // Both names written out whole rather than assembled from a stem and a
        // direction. tests/test_icon_names.py greps the source for the icons a
        // page draws and checks each against what Font Awesome ships; a name
        // built at runtime reads to it as the stem alone, which is not an icon
        // -- so the check that exists to catch a misspelled icon would report
        // this one instead. (And the grep cannot tell code from comment, so
        // this note must not spell the stem out either.)
        icon.className = direction === "previous"
            ? "fas fa-chevron-left"
            : "fas fa-chevron-right";
        icon.setAttribute("aria-hidden", "true");
        // The key cap lives INSIDE the button, so it dims with it at the end
        // of a dataset and is part of what can be clicked. Outermost on each
        // side: B, then the left chevron; the right chevron, then N.
        const cap = document.createElement("kbd");
        cap.className = "dataset-nav-key";
        cap.setAttribute("aria-hidden", "true");
        cap.textContent = KEY_CAP[direction];
        if (direction === "previous") {
            element.appendChild(cap);
            element.appendChild(icon);
        } else {
            element.appendChild(icon);
            element.appendChild(cap);
        }
        // Disabled rather than hidden at the ends of a dataset. A control that
        // disappears on the last sample makes the row jump and leaves the user
        // wondering whether they lost the feature or reached the end; a greyed
        // one says which.
        element.disabled = !target;
        const name = direction === "previous" ? "Previous sample" : "Next sample";
        element.title = target
            ? name + ": " + target + " (" + KEY_CAP[direction] + ")"
            : "No " + label + " sample in this dataset";
        element.setAttribute("aria-label", element.title);
        element.addEventListener("click", () => go(target));
        return element;
    }

    function render() {
        if (!place) return;
        const wrapper = document.getElementById("openseadragon_wrapper");
        if (!wrapper) return;
        if (root && root.parentNode) root.parentNode.removeChild(root);

        root = document.createElement("nav");
        root.className = "dataset-nav";
        root.setAttribute("aria-label", "Dataset navigation");

        previousButton = button("previous", "previous", place.previous);
        root.appendChild(previousButton);

        const counter = document.createElement("span");
        counter.className = "dataset-nav-count";
        counter.textContent = (place.index + 1) + " / " + place.members.length;
        counter.title = place.datasetName
            ? "Sample " + (place.index + 1) + " of " + place.members.length
                + " in " + place.datasetName
            : "";
        root.appendChild(counter);

        nextButton = button("next", "next", place.next);
        root.appendChild(nextButton);

        wrapper.appendChild(root);
    }

    async function mount() {
        // Only in the viewer. Every other page either has no canvas to put
        // this on or is not about one sample.
        if (!document.getElementById("openseadragon_wrapper")) return;
        try {
            place = await resolve();
        } catch (error) {
            // A sample that is in no dataset and a /datasets that would not
            // answer are the same thing here: no controls. Nothing about this
            // feature is worth a message of its own.
            place = null;
        }
        if (!place) return;
        render();
        document.addEventListener("keydown", onKeyDown);
        window.addEventListener("plexora:viewer-hidden", () => { keysArmed = false; });
        window.addEventListener("plexora:viewer-shown", () => { keysArmed = true; });
    }

    if (typeof document !== "undefined") {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", mount);
        } else {
            mount();
        }
    }

    return {
        /** Which dataset this sample is in, for anything that needs a way back
         *  to it -- the image-error card's "Back to dataset" link. Null when
         *  this sample is in none, or before the lookup has landed. */
        datasetId: () => (place ? place.datasetId : null),
        datasetName: () => (place ? place.datasetName : null),
        /** The neighbours, for a probe and for anything that wants to offer
         *  the same move from somewhere else. */
        place: () => place,
        go,
        //: Test seams. The probe drives mount() against a stand-in document.
        _mount: mount,
        _resolve: resolve,
        _onKeyDown: onKeyDown,
    };
})();
