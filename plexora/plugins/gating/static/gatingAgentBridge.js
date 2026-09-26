/**
 * gatingAgentBridge.js - Thresholding's answers to the viewer control plane.
 *
 * Core's services/agentBridge.js runs an external agent's commands against
 * this tab and re-dispatches the events it publishes. It never names a
 * plugin: it announces, and whichever plugin owns the subject answers. This
 * file is that answer for gating, in three listeners:
 *
 *   `plexora:agent-state-changed` with `plugin === "gating"` -- the agent
 *     saved gates for this project from another process (a notebook, its own
 *     MCP server). Re-read them the way a page load does, and redraw.
 *   `plexora:agent-command` for `set_active_marker` -- pick the marker the
 *     user is gating, through the same setter the marker dropdown uses.
 *   `plexora:agent-state` -- what `get_state` reports for this tool: the
 *     marker on screen and every gate somebody has actually narrowed.
 *
 * Script scope, and registered once: a plugin's scripts are run once per page
 * however many times the tool is opened and closed, so the controller is looked
 * up at event time (`__plexora.plugins`) rather than captured -- a tool that was
 * removed and reopened has a new one, and a closed tool simply does not answer.
 */
(function () {
    const PLUGIN = "gating";

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
            console.error("gating: could not take on the agent's gates", error);
        });
    });

    window.addEventListener("plexora:agent-command", (event) => {
        const detail = event.detail || {};
        if (detail.type !== "set_active_marker" || detail.claimed) return;
        const live = controller();
        if (!live) return;
        const marker = String(detail.arguments?.marker || "");
        const names = live.getGateMarkerNames();
        if (!names.includes(marker)) {
            // Claimed and rejected rather than left for nobody: this IS the
            // plugin that owns markers, and "no plugin handled it" would send
            // the agent looking for a different one.
            detail.claim(Promise.reject(new Error(
                `${marker || "(no marker)"} is not a marker Thresholding can gate`)), PLUGIN);
            return;
        }
        live.setGateMarker(marker);
        detail.claim({ active_marker: live.gateMarker }, PLUGIN);
    });

    window.addEventListener("plexora:agent-state", (event) => {
        const live = controller();
        if (!live || !event.detail?.contribute) return;
        event.detail.contribute(PLUGIN, {
            active_marker: live.gateMarker || null,
            gates: live.getCustomGatedChannels(),
        });
    });
})();
