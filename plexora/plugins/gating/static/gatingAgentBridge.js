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
 *   `plexora:agent-command` for `preview_gate` -- move the slider to a
 *     candidate gate so the user sees what it would call positive, WITHOUT
 *     saving: the stored gate is put back in the list the sidebar autosaves.
 *   `gating.session` events -- an automatic-gating session mirrored into this
 *     tab: a pill over the panel with Pause / Resume / Take over.
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
        if (detail.kind === "gating.session") {
            showPill(detail.payload || {});
            return;
        }
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

    // A candidate gate on the slider, not in the saved list. setGateRange with
    // a BRUSH MOVE repaints the cells and does not save; the list entry it
    // also writes is put straight back, so no later autosave can persist a
    // gate the agent was only showing.
    window.addEventListener("plexora:agent-command", (event) => {
        const detail = event.detail || {};
        if (detail.type !== "preview_gate" || detail.claimed) return;
        const live = controller();
        if (!live) return;
        const args = detail.arguments || {};
        const marker = String(args.marker || "");
        if (!live.getGateMarkerNames().includes(marker)) {
            detail.claim(Promise.reject(new Error(
                `${marker || "(no marker)"} is not a marker Thresholding can gate`)), PLUGIN);
            return;
        }
        if (live.gateMarker !== marker) live.setGateMarker(marker);
        const fullName = live.dataLayer.getFullChannelName(marker);
        const range = live.getGateRange(marker);
        const stored = live.gatingList.gating_channels[fullName];
        const low = Number(args.low);
        const high = args.high === null || args.high === undefined ? (stored || range)[1]
            : Number(args.high);
        if (!Number.isFinite(low) || !Number.isFinite(high) || !(low < high)) {
            detail.claim(Promise.reject(new Error("preview_gate needs low < high")), PLUGIN);
            return;
        }
        live.setGateRange([low, high], CSVGatingList.events.GATING_BRUSH_MOVE);
        if (stored) live.gatingList.gating_channels[fullName] = stored;
        else delete live.gatingList.gating_channels[fullName];
        // The cells are drawn from `selections`, which now hold the preview;
        // persistGatingList saves the stored gate in its place until the user
        // (or a reload) replaces it.
        live.agentPreview = { fullName, stored: stored ? [...stored] : null };
        detail.claim({ marker, low, high, saved: false }, PLUGIN);
    });

    // -- the session pill -----------------------------------------------------

    let pillSession = null;

    function pill() {
        return document.getElementById("gating_agent_pill");
    }

    function showPill(payload) {
        const element = pill();
        const live = controller();
        if (!element || !live || !payload.session_id) return;
        if (payload.event === "finished") {
            element.hidden = true;
            pillSession = null;
            return;
        }
        pillSession = payload.session_id;
        element.replaceChildren();
        const text = document.createElement("span");
        text.className = "gating-agent-pill-text";
        text.textContent = payload.paused ? "Agent gating paused" : "An agent is gating this image";
        element.appendChild(text);
        const actions = payload.paused ? [["resume", "Resume"]]
            : [["pause", "Pause"], ["take_over", "Take over"]];
        actions.forEach(([action, label]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "gating-agent-pill-action";
            button.textContent = label;
            button.title = action === "take_over"
                ? "Pause the agent and lock the marker on screen, so its gate is yours"
                : `${label} the agent's gating session`;
            button.addEventListener("click", () => controlSession(action));
            element.appendChild(button);
        });
        element.hidden = false;
    }

    async function controlSession(action) {
        const live = controller();
        if (!live || !pillSession) return;
        try {
            await live.api.controlAgentSession(pillSession, action,
                                               action === "take_over" ? live.gateMarker : null);
            showPill({ session_id: pillSession, paused: action !== "resume" });
            if (action === "take_over") await live.loadProvenance();
        } catch (error) {
            window.PlexoraToast?.show?.({ title: "The agent's session did not answer",
                                         note: String(error.message || error), lines: [] });
        }
    }

    window.addEventListener("plexora:agent-state", (event) => {
        const live = controller();
        if (!live || !event.detail?.contribute) return;
        event.detail.contribute(PLUGIN, {
            active_marker: live.gateMarker || null,
            gates: live.getCustomGatedChannels(),
        });
    });
})();
