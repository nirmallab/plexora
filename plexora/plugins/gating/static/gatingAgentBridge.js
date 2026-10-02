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
 *     tab: a pill over the panel saying so, with Take over. Pause, Resume and
 *     Stop live in core's agent panel (views/agentPanel.js), which hears the
 *     same events -- two places to pause one session was the ambiguity.
 *   `plexora:agent-restore` -- the agent's session is over and core is giving
 *     the viewer back: drop the candidate gate on the slider (the stored one
 *     comes back) and return to the marker the user was on.
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
    let pillPaused = false;

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
            pillPaused = false;
            return;
        }
        if (payload.session_id !== pillSession) pillPaused = false;
        pillSession = payload.session_id;
        // `control` carries the pause state; `started` is a fresh run.
        if (payload.paused !== undefined) pillPaused = Boolean(payload.paused);
        else if (payload.event === "started") pillPaused = false;
        // Taken over from the agent panel (core): the marker it was on is
        // locked now, and the provenance readout says so.
        if (payload.taken_over) live.loadProvenance?.();
        element.replaceChildren();
        const text = document.createElement("span");
        text.className = "gating-agent-pill-text";
        text.textContent = pillPaused ? "Agent gating paused" : "An agent is gating this image";
        text.title = text.textContent;
        element.appendChild(text);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "gating-agent-pill-action";
        button.dataset.action = "take_over";
        button.textContent = "Take over";
        button.title = "Pause the agent and lock the marker on screen, so its gate is yours";
        button.addEventListener("click", () => controlSession("take_over"));
        element.appendChild(button);
        element.hidden = false;
    }

    async function controlSession(action) {
        const live = controller();
        if (!live || !pillSession) return;
        try {
            await live.api.controlAgentSession(pillSession, action,
                                               action === "take_over" ? live.gateMarker : null);
            showPill({ session_id: pillSession, event: "control", paused: true });
            if (action === "take_over") {
                // The view is the user's again: their channels and windows
                // back, not the agent's inspection windows left on screen.
                await window.PlexoraAgentBridge?.restore?.({ reason: "taken_over" });
                await live.loadProvenance();
            }
        } catch (error) {
            window.PlexoraToast?.show?.({ title: "The agent's session did not answer",
                                         note: String(error.message || error), lines: [] });
        }
    }

    // The viewer given back. A candidate still on the slider goes (forcing
    // the same marker reselects its STORED gate and clears `agentPreview`),
    // then the marker the user was on before the agent moved it -- neither
    // mirrored into a channel slot: core restores the channels itself.
    window.addEventListener("plexora:agent-restore", (event) => {
        const detail = event.detail || {};
        const live = controller();
        if (!live) return;
        const work = Promise.resolve().then(() => {
            if (live.agentPreview && live.gateMarker) {
                live.setGateMarker(live.gateMarker, { force: true, syncSlot: false });
            }
            const leased = detail.plugins && detail.plugins[PLUGIN] && detail.plugins[PLUGIN].active_marker;
            if (leased && leased !== live.gateMarker && live.getGateMarkerNames().includes(leased)) {
                live.setGateMarker(leased, { syncSlot: false });
            }
            return { active_marker: live.gateMarker || null };
        });
        if (typeof detail.wait === "function") detail.wait(work);
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
