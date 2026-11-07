/**
 * PlexoraEnvelope -- on the website, saying when a project is more than a
 * browser tab handles well, and offering the desktop app, which opens it with
 * nothing to export (services/desktopHandoff.js).
 *
 * The limits are measured (client/src/envelope.json: the worker's heap per
 * table value, P9's 2.1 GiB at 36.6 M values against the ~3 GiB a tab's
 * worker has; the pyramid build rate, P7 and this site's runs) and scaled to
 * this device where the browser says how much memory it has. A project
 * within them gets no notice at all: the desktop app is offered only when it
 * is the better place.
 *
 * Once per project per tab; "Continue here" is remembered for 30 days.
 *
 *   check(project, app) -> when the app's frame opens a project
 *   judge(size, limits, device) -> {verdict: "website"|"desktop", title, note}
 *
 * Classic script, `window.PlexoraEnvelope`; uses PlexoraHandoff.
 */
(function (global) {
    "use strict";

    const LIMITS = "/client/src/envelope.json";
    const REMEMBER_DAYS = 30;
    const shown = new Set();
    let limits = null;

    const count = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : String(n));

    //: Whether `size` ({rows, columns}) fits a browser tab here.
    function judge(size, rules, device = {}) {
        const table = rules.table;
        const rows = Number(size && size.rows) || 0;
        const columns = Number(size && size.columns) || 0;
        const values = rows * columns;
        let limit = table.worker_values;
        if (device.memoryGb) limit *= Math.min(1, device.memoryGb / table.reference_device_memory_gb);
        if (values > limit) {
            const gb = (values * table.worker_bytes_per_value) / 1e9;
            return {
                verdict: "desktop",
                title: "Large for a browser tab",
                note: `${count(rows)} cells × ${columns} columns need about ${gb.toFixed(1)} GB here. `
                    + "The desktop app opens it with room to spare.",
            };
        }
        return { verdict: "website" };
    }

    function remembered(key) {
        try {
            const until = Number(global.localStorage.getItem(key));
            return Number.isFinite(until) && until > Date.now();
        } catch (error) {
            return false;
        }
    }

    function remember(key) {
        try {
            global.localStorage.setItem(key, String(Date.now() + REMEMBER_DAYS * 864e5));
        } catch (error) {
            // Not kept: asked again next time.
        }
    }

    async function sizeOf(app, project) {
        const answer = await app.fetch(`/project/${encodeURIComponent(project)}/size`);
        return answer.ok ? answer.json() : null;
    }

    async function check(project, app) {
        if (!project || shown.has(project)) return null;
        shown.add(project);
        limits = limits || await (await global.fetch(LIMITS)).json().catch(() => null);
        if (!limits) return null;
        // The table may still be loading behind the viewer: asked again once.
        let size = await sizeOf(app, project).catch(() => null);
        if (size && size.rows == null) {
            await new Promise((resolve) => global.setTimeout(resolve, 15000));
            size = await sizeOf(app, project).catch(() => null);
        }
        if (!size) return null;
        const device = { memoryGb: global.navigator.deviceMemory || null };
        const verdict = judge(size, limits, device);
        const key = `plexora-notice:${project}:${verdict.verdict}`;
        if (verdict.verdict !== "desktop" || remembered(key)) return verdict;
        for (let i = 0; i < 50 && !app.PlexoraToast; i += 1) await new Promise((r) => global.setTimeout(r, 200));
        app.PlexoraToast?.show({
            title: verdict.title, note: verdict.note, timeout: 0,
            actions: [
                { label: "Open in Desktop App", primary: true,
                  onSelect: () => global.PlexoraHandoff && global.PlexoraHandoff.open(project, app) },
                { label: "Continue here", onSelect: () => remember(key) },
            ],
        });
        return verdict;
    }

    global.PlexoraEnvelope = Object.freeze({ check, judge });
})(typeof window !== "undefined" ? window : globalThis);
