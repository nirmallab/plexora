/**
 * qcAgentBridge.js - Quality Control's answers to the viewer control plane.
 *
 * Core's services/agentBridge.js runs an agent's commands against this tab and
 * re-dispatches the events it publishes without naming a plugin. QC's side:
 *
 *   `plexora:agent-state-changed` with `kind === "qc.session"` -- a QC session
 *     moved on (a region written, a unit closed, the session finished): the
 *     open QC panel reloads what it shows. Core's agent panel draws the
 *     session itself.
 *   `plexora:agent-state-changed` with `plugin === "qc"` -- QC's store changed
 *     from another process (strictness, approval): reload.
 *   `plexora:agent-state` -- what `get_state` reports for this tool.
 *
 * Take-over is posted to the session's control route by core's agent panel
 * (`control.url` in every event); nothing here needs to know it.
 */
(function () {
    const PLUGIN = "qc";

    function controller() {
        const record = window.__plexora?.plugins?.get(PLUGIN);
        return record?.sidebarController || null;
    }

    let pending = null;

    function reloadSoon() {
        window.clearTimeout(pending);
        pending = window.setTimeout(() => {
            const live = controller();
            if (live?.reload) live.reload().catch?.(() => {});
        }, 400);
    }

    window.addEventListener("plexora:agent-state-changed", (event) => {
        const detail = event.detail || {};
        const kind = String(detail.kind || "");
        if (kind === "qc.session") {
            const payload = detail.payload || {};
            if (["unit_closed", "finished", "answered"].includes(payload.event)) reloadSoon();
            return;
        }
        if (detail.plugin === PLUGIN) reloadSoon();
    });

    window.addEventListener("plexora:agent-state", (event) => {
        const live = controller();
        if (!live || !event.detail?.contribute) return;
        const state = live.state || {};
        event.detail.contribute(PLUGIN, {
            result_id: state.active_result_id || null,
            regions: (state.regions || []).length,
            strictness: (state.strictness || {}).preset || null,
        });
    });
})();
