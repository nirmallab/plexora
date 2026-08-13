/**
 * appModules.js - Client-side extension point for optional add-on modules (gating, and
 * later roi, etc.) layered on top of the core viewer. A module self-registers a definition
 * here at script-load time; main.js then drives it through this registry instead of
 * hardcoding the concrete module class. Only the active module's scripts are ever loaded
 * (see base.html's `active_module` conditionals), so in practice this registry holds 0 or 1
 * entries.
 *
 * Module definition shape (all keys optional except `name`):
 *   {
 *     name: string,
 *     createInstance(ctx): object                 - ctx: { config, columns, dataLayer, eventHandler }
 *     createSidebarController(ctx): object|null    - ctx: { sidebar, dataLayer, eventHandler, config, moduleInstance }
 *     bindEvents(ctx): void                        - ctx: { eventHandler, dataLayer, seaDragonViewer, channelList,
 *                                                            moduleInstance, updateSeaDragonSelection,
 *                                                            updateCentroidsForGate, runSegmentationGate }
 *   }
 */
window.AppModules = {
    registry: [],
    register(def) {
        this.registry.push(def);
    },
};
