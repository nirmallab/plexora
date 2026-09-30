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
 *     The status line follows every session event (running, paused, done).
 *   `plexora:agent-state-changed` with `plugin === "qc"` -- QC's store changed
 *     from another process (strictness, approval): reload.
 *   `plexora:agent-state-changed` with `plugin === "roi"` -- the ROI document
 *     changed (QC's own writes announce under the ROI plugin's name, so an
 *     open ROI panel reloads): reload, so a region QC just wrote is on the
 *     tissue without the ROI tool being open.
 *   `plexora:agent-state-changed` with `kind` `qc.registration*` /
 *     `qc.segmentation*` / `qc.blur*` -- the Registration Check, Segmentation
 *     QC or Blur QC changed
 *     (the panel's own act, another tab's, or an agent's): the new state is
 *     applied to the row and the viewer, without reloading the rest.
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
            controller()?.noteSession?.(payload);
            if (["unit_closed", "finished", "answered", "limit_answered"].includes(payload.event)) {
                reloadSoon();
            }
            return;
        }
        // The image checks' own events (and their receipts' echoes) carry
        // the new state: applied as they are, never a reload of everything.
        if (kind.startsWith("qc.registration")) {
            controller()?.registration?.onEvent?.(kind, detail.payload || {});
            return;
        }
        if (kind.startsWith("qc.segmentation")) {
            controller()?.segmentation?.onEvent?.(kind, detail.payload || {});
            return;
        }
        if (kind.startsWith("qc.blur")) {
            controller()?.blur?.onEvent?.(kind, detail.payload || {});
            // Regions written become ROIs: the ROI QC tree follows.
            if (kind === "qc.blur_write_regions") reloadSoon();
            return;
        }
        if (detail.plugin === PLUGIN || detail.plugin === "roi") reloadSoon();
    });

    window.addEventListener("plexora:agent-state", (event) => {
        const live = controller();
        if (!live || !event.detail?.contribute) return;
        const state = live.state || {};
        event.detail.contribute(PLUGIN, {
            result_id: state.active_result_id || null,
            regions: (state.regions || []).length,
            hidden: [...(live.hidden || [])],
            // ROI QC's header eye off: no region or QC cell drawn, whatever `hidden` says.
            roi_muted: Boolean(live.roiMuted),
            strictness: (state.strictness || {}).preset || null,
            registration: live.registration?.state ? {
                active: live.registration.state.active,
                reference: live.registration.state.reference,
                comparison: live.registration.state.comparison,
                flicker: live.registration.state.flicker,
                highlighted_pct: live.registration.last?.stats?.highlighted_pct ?? null,
                // Play's per-channel scores: comparison -> % displaced.
                scores: Object.fromEntries([...(live.registration.scores || new Map())]
                    .map(([name, stats]) => [name, stats?.highlighted_pct ?? null])),
                // The flicker is the zebra over the misaligned areas.
                views: { ...(live.registration.views || {}) },
            } : null,
            segmentation_qc: live.segmentation?.summary() || null,
            blur_qc: live.blur?.summary() || null,
        });
    });
})();
