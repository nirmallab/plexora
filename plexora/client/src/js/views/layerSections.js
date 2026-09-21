/**
 * Plugins that are viewer LAYERS rather than tools.
 *
 * A tool is something the user opens, works in and closes: it is in the Tools
 * menu, it takes over the sidebar, toolLoader.js stands it down when another
 * tool opens, and closing it puts the sidebar back. A layer is none of that.
 * It is part of what the viewer IS for this sample, so it is rendered on every
 * view of a sample that has the data, and there is no state of the app in
 * which it makes sense to close it and leave the sample open.
 *
 * That difference is why these do NOT go through toolLoader.registerLoaded.
 * That path's standDown() folds whatever is showing the moment any tool
 * opens, which is right for two tools competing for one canvas and wrong for
 * a layer: opening Gating would have turned the transcripts off.
 *
 * WHAT THIS MODULE IS NOW. It used to own a section's chrome -- its heading,
 * its chevron, its visibility checkbox -- and a plugin panel was a section of
 * the sidebar sitting above the Layers list. Which meant the transcript layer
 * was switched in one place, listed in another, and (once it claimed its
 * layer) carded in a third. There is one place now: its card. So a plugin's
 * panel is markup STAGED in `#layer_section_slot` and moved into that card by
 * layerManager.js, and what is left here is the staging: where a mount is,
 * which layer it belongs to, and the viewer's own show/hide forwarded to
 * whoever is drawing.
 *
 * Nothing here builds a card, and nothing here folds one -- the card owns
 * both, because a card that folded differently depending on whose panel was
 * inside it is exactly the drift one card list exists to prevent.
 *
 * Server side: `Plugin.LAYER_SECTION_SLOT`, `plugins.layer_sections_for`,
 * `Requires.first_layer`, and the `#layer_section_slot` div in index.html.
 */
(function () {
    "use strict";

    const SLOT = "#layer_section_slot";

    //: name -> the plugin's sidebar controller, once main.js has built it.
    const controllers = new Map();

    /** Every layer-section mount on the page, as `[name, element]`. */
    function mounts() {
        return Array.from(
            document.querySelectorAll(`${SLOT} [data-layer-section]`)
        ).map((el) => [el.getAttribute("data-layer-section"), el]);
    }

    /** Whether a plugin rendered into the layer slot on this page. */
    function isLayerSection(name) {
        if (!name) return false;
        return Boolean(document.querySelector(
            `${SLOT} [data-layer-section="${name}"]`));
    }

    /**
     * The markup staged for one layer's card body, or null.
     *
     * Returned rather than cloned, and the caller appends it -- which MOVES
     * it, because that is the whole point. A plugin's controller takes its
     * element handles once and keeps them; a clone would leave every one of
     * them pointing at the copy still sitting in the slot, and nothing would
     * report that. The same reason `cardList.buildCard` wraps a body rather
     * than re-parenting it later.
     */
    function bodyFor(layerId) {
        if (!layerId) return null;
        return document.querySelector(`${SLOT} [data-layer-body="${layerId}"]`);
    }

    /**
     * The same, found by what the layer IS rather than by its id.
     *
     * For a layer that did not exist when the page was rendered -- imported
     * mid-session, or still being built when `/config` was first served -- so
     * its id could not be written into the mount. The modality is stable
     * across both, which is why it is carried as well.
     */
    function bodyForModality(modality) {
        if (!modality) return null;
        return document.querySelector(`${SLOT} [data-layer-modality="${modality}"]`);
    }

    /** The markup staged for one layer card's HEADER, or null. Same move, and
     *  the same reason: a plugin's kebab keeps its own handlers. */
    function extrasFor(layerId) {
        if (!layerId) return null;
        return document.querySelector(`${SLOT} [data-layer-extras="${layerId}"]`);
    }

    /** The extras a plugin's own panel staged inside its mount, or null. */
    function extrasIn(mount) {
        return mount?.querySelector?.("[data-layer-extras]") || null;
    }

    /**
     * Hand a plugin's controller over, once main.js has built it.
     *
     * Called instead of `PlexoraToolLoader.registerLoaded` -- see the header.
     */
    function register(name, controller) {
        if (!name || !controller) return;
        controllers.set(name, controller);
    }

    function forEachController(action) {
        for (const [name, controller] of controllers.entries()) {
            try {
                action(controller);
            } catch (error) {
                console.error(`layerSections: ${name} failed on a viewer change`, error);
            }
        }
    }

    // The viewer can be swapped out from under a section by the router
    // without the page reloading, and a section drawing on the canvas has to
    // stop when the canvas goes. Same pair toolLoader forwards to its tools.
    window.addEventListener("plexora:viewer-hidden",
        () => forEachController((controller) => controller?.onHide?.()));
    window.addEventListener("plexora:viewer-shown",
        () => forEachController((controller) => controller?.onShow?.()));

    window.PlexoraLayerSections = {
        isLayerSection, register, mounts, bodyFor, bodyForModality,
        extrasFor, extrasIn,
        names: () => mounts().map(([name]) => name),
    };
})();
