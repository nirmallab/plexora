/**
 * roiAgentBridge.js - ROI's answers to the viewer control plane.
 *
 * Core's services/agentBridge.js runs an external agent's commands against
 * this tab and re-dispatches the events it publishes, without ever naming a
 * plugin. This file is ROI's side of that, in three listeners:
 *
 *   `plexora:agent-state-changed` with `plugin === "roi"` -- the agent saved
 *     regions for this project from another process. Reload them, unless this
 *     tab has edits of its own still unsaved (reloadFromServer defers, and
 *     says so, rather than drop them).
 *   `plexora:agent-command` for `focus_roi` -- select the region and frame it.
 *     Claiming is what lets core tell "selected and framed" from its own
 *     fallback of fitting a bare box.
 *   `plexora:agent-state` -- what `get_state` reports for this tool.
 *
 * Script scope, registered once per page; the controller is looked up when an
 * event arrives, so a closed ROI tool simply does not answer and a reopened one
 * answers with its new controller.
 */
(function () {
    const PLUGIN = "roi";

    function controller() {
        const record = window.__plexora?.plugins?.get(PLUGIN);
        return record?.sidebarController || null;
    }

    window.addEventListener("plexora:agent-state-changed", (event) => {
        const detail = event.detail || {};
        if (detail.plugin !== PLUGIN) return;
        const live = controller();
        if (!live?.reloadFromServer) return;
        live.reloadFromServer().catch((error) => {
            console.error("ROI: could not take on the agent's regions", error);
        });
    });

    window.addEventListener("plexora:agent-command", (event) => {
        const detail = event.detail || {};
        if (detail.type !== "focus_roi" || detail.claimed) return;
        const live = controller();
        const id = detail.arguments?.roi_id;
        // Not claimed for a region this project does not have: core then falls
        // back to the box the agent sent, which is the better answer than an
        // error when the agent knows where the region is and this tab does not.
        if (!live?.focusRegion || !id || !live.store?.feature(id)) return;
        detail.claim(live.focusRegion(id), PLUGIN);
    });

    window.addEventListener("plexora:agent-state", (event) => {
        const live = controller();
        if (!live || !event.detail?.contribute) return;
        const store = live.store;
        event.detail.contribute(PLUGIN, {
            regions: store.features.length,
            categories: store.categories.map((category) => category.label),
            selected: store.selectionId || null,
            status: store.status,
            unsaved: Boolean(store.hasUnsavedWork),
        });
    });
})();
