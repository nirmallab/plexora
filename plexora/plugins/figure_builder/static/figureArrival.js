/**
 * FigureArrival - open Figure Builder on the viewer the figure page sent the
 * user to.
 *
 * "Capture from a viewer" goes through core's project picker, which links to
 * an image with no `?tool=`, and a viewer opened without one opens no tool.
 * So the figure page leaves a note in sessionStorage (ARRIVAL_KEY) and this
 * viewer script, loaded on every viewer page, takes it: read once and cleared,
 * then the Figure Builder tool is opened through core's tool loader. Also on
 * `plexora:viewer-shown`, for a project picked that was already live, which
 * the router shows again without reloading the page.
 */
(function () {
    const ARRIVAL_KEY = "plexora.figure_builder.openOnArrival";

    function take() {
        try {
            if (window.sessionStorage.getItem(ARRIVAL_KEY) !== "1") return false;
            window.sessionStorage.removeItem(ARRIVAL_KEY);
            return true;
        } catch (error) {
            return false;
        }
    }

    function open(tries = 40) {
        const loader = window.PlexoraToolLoader;
        if (!loader || typeof loader.openTool !== "function") {
            // The loader is core's and may not be up yet on this page.
            if (tries > 0) setTimeout(() => open(tries - 1), 150);
            return;
        }
        if (typeof loader.isToolOpen === "function" && loader.isToolOpen("figure_builder")) return;
        try {
            loader.openTool("figure_builder");
        } catch (error) {
            console.error("figure builder: could not open on arrival", error);
        }
    }

    function arrive() {
        // Only on a viewer: the note waits through the picker page.
        if (!window.flaskVariables?.datasource) return;
        if (take()) open();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", arrive, { once: true });
    } else {
        arrive();
    }
    window.addEventListener("plexora:viewer-shown", arrive);
})();
