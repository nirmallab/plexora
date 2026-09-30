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
 * Chrome a plugin appends to #openseadragon_wrapper (a dock, a floating
 * panel) may carry the attribute `data-viewer-furniture`; core's own canvas
 * popups, such as the dataset thumbnail grid, measure those elements and keep
 * off them. Core never looks for a plugin's class names instead.
 *
 * The shape a definition takes is the `PluginDefinition` typedef below; the
 * documentation site's browser plugin API pages are generated from these
 * typedefs (website/scripts/generate_browser_api.mjs), so keep them current.
 */

/**
 * What a plugin's browser script passes to `Plexora.registerPlugin`. Only
 * `name` is required; every hook is optional.
 *
 * @typedef {Object} PluginDefinition
 * @property {string} name - The plugin's name. Must equal the `name` of the
 *   Python `Plugin` descriptor, which is how the page connects the script to
 *   the tool.
 * @property {(ctx: PluginContext) => Object} [createInstance] - Build the
 *   plugin's main object when the tool is activated. Whatever it returns is
 *   handed to the other hooks as `ctx.instance`. If it has an
 *   `init(databaseDescription, viewer)` method, core calls it once before
 *   tiles load.
 * @property {(ctx: PluginContext) => (SidebarController|null)} [createSidebarController]
 *   - Build the controller for the plugin's sidebar panel. `ctx` also carries
 *   `sidebar` (the panel element) and `moduleInstance`.
 * @property {(ctx: PluginContext) => void} [bindEvents] - Subscribe to core
 *   events. `ctx` also carries `seaDragonViewer`, `moduleInstance`,
 *   `updateSeaDragonSelection`, `updateCentroidsForGate()` and
 *   `runSegmentationGate(showSpinner)`; the last two act on this plugin's own
 *   cell layer.
 * @property {boolean} [ownsCellLayer] - This plugin colours cells. It gets a
 *   cell layer of its own (colours, gate, mode, opacity) and a card in the
 *   sidebar. Several may be live at once; card order is compositing order.
 * @property {string} [preferredCellMode] - How the mask should be drawn when
 *   this plugin's layer is first turned on: `"filled"`, `"outlines"` or
 *   `"centroids"`. Defaults to `"outlines"`. Never overrules a choice the user
 *   already made, and is ignored when the mask cannot be drawn that way.
 * @property {string[]} [supportedCellModes] - The subset of
 *   `["centroids", "outlines", "filled"]` this plugin can work with. The Cells
 *   control offers only these while the plugin's layer is active. Omit for
 *   whatever the project can draw. Include `"none"` to keep the None option
 *   on the row while the layer is active (it is otherwise left to the card's
 *   eye).
 * @property {boolean} [cellLayerOnDemand] - The layer is registered but not
 *   turned on when the plugin activates; the plugin turns it on with
 *   `ctx.layers.showCells()` when its cells are asked for.
 * @property {PluginHelp} [help] - What the `?` in the tool's card header
 *   explains. Without it there is no `?`. Core draws the button and the
 *   dialog, and adds the open/close row for the descriptor's `shortcut`.
 * @property {() => void} [destroy] - Release anything global before the
 *   plugin is torn down. Prefer `ctx.onCleanup(fn)`, which is called for you.
 * @property {boolean} [lazy] - The script is on every viewer page rather than
 *   fetched when the tool opens. It is activated only when its panel is
 *   staged (`?tool=`) or the tool is opened.
 * @property {boolean} [hasLayer] - `false` when the tool draws nothing, so
 *   its card has no visibility toggle. Defaults to `true`.
 */

/**
 * Help shown by the `?` button in a tool's card header. All of it is plain
 * text, never HTML.
 *
 * @typedef {Object} PluginHelp
 * @property {string} summary - What the tool does. A blank line starts a new
 *   paragraph.
 * @property {string[]} [notes] - Bullet points.
 * @property {{keys: (string|string[]), label: string}[]} [shortcuts] - Keys
 *   the tool responds to. `keys` is a chord such as `"mod+shift+z"` (printed
 *   per platform) or a single key (`"x"` prints as X); an array prints several
 *   keys in one row.
 * @property {string} [docs] - A page of the documentation site to link to,
 *   relative to its `/docs/` root, e.g. `"plugins/roi"`. Bundled plugins set
 *   it; the help dialog shows a "Read the documentation" link.
 */

/**
 * The object `createSidebarController` returns. Every method is optional;
 * toolLoader.js calls them as the panel's life goes on.
 *
 * @typedef {Object} SidebarController
 * @property {() => (void|Promise<void>)} [setup] - Build the panel's widgets.
 *   Called once, the first time the panel is shown.
 * @property {() => (Object|Promise<Object>)} [fetchSaved] - Load this
 *   sample's saved state for the plugin.
 * @property {(saved: Object) => void} [applyOrDefault] - Apply saved state,
 *   or defaults when there is none.
 * @property {() => (void|Promise<void>)} [persistIfNeeded] - Save state that
 *   changed.
 * @property {() => void} [onShow] - The panel became the tool being worked
 *   on.
 * @property {() => void} [onHide] - The panel was closed or another tool
 *   opened over it. The panel is only hidden and keeps its state. A
 *   controller that listens outside its own panel (canvas handlers,
 *   document-level shortcuts) must stand those down here and re-arm them in
 *   `onShow`, or two tools act on the same keypress.
 * @property {(on: boolean) => void} [onVisibilityChange] - The card's eye was
 *   toggled. Needed only by a plugin that draws its own overlay; core already
 *   shows and hides cell layers.
 * @property {() => (Object|null)} [captureCarryState] - What this panel takes
 *   along when the user moves to the next sample of a dataset (Prev/Next on
 *   the canvas). The rule: an arrangement travels, a measurement does not.
 *   Which marker is being thresholded, which column colours the cells, which
 *   genes are on: carry those. A threshold, a contrast window, picked cell ids
 *   or a viewport: do not, they are readings of this image.
 * @property {(state: Object) => ({skipped?: string[]}|Promise<{skipped?: string[]}>)} [applyCarryState]
 *   - Re-apply a carried state on the next sample. Runs after
 *   `applyOrDefault`, so the sample's own saved state is already loaded.
 *   Return `{skipped: [...]}` naming what this sample cannot honour (a marker
 *   it lacks); core shows one notice for all plugins. Throwing counts as
 *   skipping everything.
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
