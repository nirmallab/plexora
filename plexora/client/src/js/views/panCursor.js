/**
 * panCursor.js -- the hand closing while the image is being dragged.
 *
 * The resting half of this is one CSS rule (`#openseadragon
 * .openseadragon-canvas { cursor: grab }` in viewer.css). Only "a drag is
 * happening right now" needs code: `:active` cannot say it, because OSD calls
 * preventDefault on the press, and that suppresses the compatibility mousedown
 * the browser drives `:active` from.
 *
 * Delegated from the document in the CAPTURE phase rather than bound to the
 * canvas, for two reasons. OSD cancels the events it handles -- preventDefault
 * AND stopPropagation -- so a bubble-phase listener would never hear the press.
 * And the canvas element belongs to whichever viewer built it (ImageViewer or
 * RgbImageViewer, and a fresh one on every project load), so nothing here has
 * to be told when one appears or goes.
 *
 * A tool that has claimed the cursor keeps it, at rest and mid-drag both:
 * roiTools.js and figureCaptureTool.js write `style.cursor` on this same
 * element, and an inline value beats either rule.
 *
 * A classic script, like every other file here; no build step, no imports.
 */
(function () {
    "use strict";

    const PANNING_CLASS = "is-panning";

    let panning = null;

    function end() {
        if (!panning) return;
        panning.classList.remove(PANNING_CLASS);
        panning = null;
    }

    document.addEventListener("pointerdown", (event) => {
        // Primary button only. A right-click on the canvas is OSD's
        // canvas-nonprimary-press, which is a selection click in this app and
        // pans nothing.
        if (event.button !== 0) return;
        const canvas = event.target?.closest?.(".openseadragon-canvas");
        if (!canvas) return;
        end();
        panning = canvas;
        canvas.classList.add(PANNING_CLASS);
    }, true);

    // Capture again on the way up, for the same reason as the press. OSD takes
    // pointer capture for the duration of a drag, so a release anywhere in the
    // page still arrives here.
    document.addEventListener("pointerup", end, true);
    document.addEventListener("pointercancel", end, true);
    // ...and a release over the browser's own chrome raises no pointerup in the
    // page at all. Without this the hand would stay closed until the next
    // press.
    window.addEventListener("blur", end);
})();
