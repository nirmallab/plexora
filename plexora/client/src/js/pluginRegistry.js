/**
 * pluginRegistry.js - where client-side plugins announce themselves.
 *
 * A plugin's script self-registers at load time; main.js then activates
 * whatever is registered. Core never names a concrete plugin class.
 *
 * This replaces window.AppModules, which held an array core only ever read as
 * `registry[0]` -- a single slot dressed up as a list, matching the old
 * one-module-per-process server. Several plugins can now be active at once, so
 * registration is keyed by name and activation iterates.
 *
 * Plugin definition (only `name` is required):
 *
 *   Plexora.registerPlugin({
 *     name: "gating",
 *
 *     // Build the plugin's main object. ctx: { config, columns, dataLayer,
 *     // eventHandler, dataset, store, viewer, url, onCleanup }
 *     createInstance(ctx): object,
 *
 *     // Optional sidebar panel controller. ctx adds { sidebar, instance }.
 *     // May implement setup/fetchSaved/applyOrDefault/persistIfNeeded/onShow.
 *     //
 *     // onHide() is the counterpart of onShow(): toolLoader.js calls it when
 *     // this tool's panel is closed or another tool is opened over it. A
 *     // controller that only owns widgets inside its own panel can ignore it --
 *     // the panel is merely hidden and its state is kept, which is what makes
 *     // reopening instant. One that reaches OUTSIDE its panel must not: viewer
 *     // canvas handlers and document-level keyboard shortcuts go on listening
 *     // to a panel the user cannot see, so two tools loaded at once both act on
 *     // the same keypress. Stand those down in onHide() and re-arm in onShow().
 *     //
 *     // onVisibilityChange(on) is a different question from onShow/onHide, and
 *     // the difference is the point of the card model: SHOWN is "this is the
 *     // tool being worked on", VISIBLE is "this tool's drawing is on screen".
 *     // A plugin that colours cells needs nothing here -- core switches its
 *     // layer for it. A plugin that draws its own overlay (ROI) does, or the
 *     // eye on its card is a button with nothing behind it.
 *     createSidebarController(ctx): object | null,
 *
 *     // Wire event-bus handlers. ctx adds { viewer, channelList, instance }
 *     // plus the core actions updateSeaDragonSelection /
 *     // updateCentroidsForGate / runSegmentationGate.
 *     bindEvents(ctx): void,
 *
 *     // Declares that this plugin draws cells in the viewer. It gets a LAYER of
 *     // its own -- its own colours, its own gate, its own mode and opacity --
 *     // and a card in the sidebar. Several may be live at once and the card
 *     // order is the order they composite in, so a phenotype map and a gate can
 *     // be looked at together. See ImageViewer.registerCellLayer.
 *     ownsCellLayer: boolean,
 *
 *     // How this plugin would like the mask drawn when its layer is first
 *     // turned on: "filled" | "outlines" | "centroids". A tool that colours
 *     // every cell by a phenotype wants filled; one that marks a few cells
 *     // wants outlines over visible tissue. Ignored when the project's recorded
 *     // layer or the mask itself cannot do it, and it never overrules a choice
 *     // the user has already made. Defaults to "outlines". See enableCellLayer.
 *     preferredCellMode: string,
 *
 *     // Which representations this plugin can actually work with, as a subset
 *     // of ["centroids", "outlines", "filled"]. The shared Cells control offers
 *     // the intersection of this and what the project can draw while this
 *     // plugin's layer is the active one, so a tool that has no use for one of
 *     // them is not offering a button whose result it did not design for.
 *     // Omit for "whatever the project can do", which is the common case.
 *     supportedCellModes: string[],
 *
 *     // Release anything global. Called before the plugin is torn down.
 *     // Prefer ctx.onCleanup(fn), which is invoked for you.
 *     destroy(): void,
 *   });
 */
window.Plexora = window.Plexora || {};

window.Plexora.plugins = (function () {
    const byName = new Map();

    return {
        /** Register a plugin definition. Re-registering a name replaces it,
         *  which is what makes a script that gets loaded twice harmless. */
        register(definition) {
            if (!definition || !definition.name) {
                console.error("Plexora.registerPlugin: definition needs a name", definition);
                return;
            }
            byName.set(definition.name, definition);
        },

        get(name) {
            return byName.get(name) || null;
        },

        /** Every registered definition, in registration order. */
        all() {
            return Array.from(byName.values());
        },

        get size() {
            return byName.size;
        },
    };
})();

/** Convenience alias, so a plugin script reads as one call. */
window.Plexora.registerPlugin = function (definition) {
    window.Plexora.plugins.register(definition);
};
