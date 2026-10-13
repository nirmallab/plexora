/**
 * maskSurfaceWait.js -- a 3-D mask being turned into cell surfaces, watched
 * from inside the viewer.
 *
 * The first time Outlines or Filled is chosen in the 3-D view, the mask is
 * converted (services/maskSurfaces.js): a quick preview, then the full-quality
 * surfaces. This is that wait -- the modal on the first build, "Continue in
 * background" to the navbar chip, a failure reopened once with the server's
 * reason and a Retry. The modal and chip are views/jobWait.js's, the same as
 * the mask pyramid's wait; this is the words.
 *
 * maskSurfaces.js drives it (`start`, `progress`, `ready`, `failed`) from the
 * layer poll's events; nothing here asks the server anything.
 */
(function () {
    "use strict";

    window.PlexoraMaskSurfaceWait = window.PlexoraJobWait.create({
        mount: "mask_surface_chip",
        copy: {
            chip: "Building cell surfaces…",
            working: "Converting the mask to 3-D surfaces",
            opening: "Reading the mask. Every cell becomes a closed surface, in one pass over it.",
            note: "You can keep working, in 3-D or on the planes. A quick preview appears "
                + "first; the full-quality surfaces replace it when they are ready.",
            button: "Continue in background",
            readyTitle: "Cell surfaces ready",
            readyDetail: "Every cell of the mask is drawn as a surface.",
            failed: "Cell surfaces failed",
            failedDetail: "The mask could not be converted to surfaces.",
            chipWorkingTip: "Building the mask's cell surfaces — click to see progress",
            chipFailedTip: "The cell surfaces could not be built — click for the reason",
        },
        notify: { title: "Cell surfaces ready", body: "The 3-D mask is drawn as surfaces." },
        retry: () => window.PlexoraMaskSurfaces?.retry(),
    });
})();
