/**
 * launchContext.js -- open a project showing what the link asked for.
 *
 * A launch link (`GET /<project>?launch=<json>`, built by a notebook, by
 * `/desktop/open {context}`, or by SCIMAP Pro's `hl.viewImage`) can say more
 * than which channels to turn on: colour the cells by a column, point at some
 * cells, outline some regions, look at one place. The server validates all of
 * it and resolves cell ids to positions (page_routes.launch_from_dict); this
 * file applies what reaches the page as `window.flaskVariables.launch`.
 *
 * Channels are not here -- viewerSidebar.js applies them where it restores the
 * saved channels, in their place. Everything else goes through the viewer's
 * own command handlers (agentBridge.js `run`), so a link and an agent showing
 * the same thing take the same path:
 *
 *   overlay    -> set_color_by (Cell Explorer, opened if it is not; skipped
 *                 when the link opens a different tool, which then holds the
 *                 cell layer)
 *   viewport   -> fit_region
 *   highlight  -> highlight_cells, for ten minutes
 *   regions    -> show_shapes with ttl_ms 0: they stay until replaced
 *
 * One page view's state, never saved: nothing here writes to the project.
 * A failure in one step is logged and the rest still run -- a link whose
 * column the table does not have should still open the image.
 */
window.PlexoraLaunchContext = (function () {
    "use strict";

    const HIGHLIGHT_TTL_MS = 600000;

    function launch() {
        return (window.flaskVariables && window.flaskVariables.launch) || {};
    }

    function activeTool() {
        return (window.flaskVariables && window.flaskVariables.active_tool) || "";
    }

    async function step(name, work) {
        try {
            return await work();
        } catch (error) {
            console.error(`launchContext: ${name} was not applied`, error);
            return null;
        }
    }

    /** Apply the launch context once. Resolves to what was applied, by step. */
    async function apply(context) {
        const wanted = context || launch();
        const bridge = window.PlexoraAgentBridge;
        const applied = {};
        if (!bridge || typeof bridge.run !== "function") return applied;
        const tool = activeTool();
        // `overlay` is the colour-by column (a link's `color_by` arrives
        // under this name). Cell Explorer reads it itself when it opens with
        // the page; this covers the page where it is not open yet.
        if (wanted.overlay && (!tool || tool === "cell_explorer")) {
            applied.overlay = await step("overlay",
                () => bridge.run("set_color_by", { column: wanted.overlay }));
        }
        if (wanted.viewport) {
            applied.viewport = await step("viewport", () => bridge.run("fit_region", wanted.viewport));
        }
        if (Array.isArray(wanted.highlight) && wanted.highlight.length) {
            applied.highlight = await step("highlight", () => bridge.run("highlight_cells", {
                cells: wanted.highlight, ttl_ms: HIGHLIGHT_TTL_MS, clear: true,
            }));
        }
        if (Array.isArray(wanted.regions) && wanted.regions.length) {
            applied.regions = await step("regions", () => bridge.run("show_shapes", {
                shapes: wanted.regions, ttl_ms: 0, clear: true,
            }));
        }
        return applied;
    }

    function hasWork(context) {
        return Boolean(context.overlay || context.viewport
            || (Array.isArray(context.highlight) && context.highlight.length)
            || (Array.isArray(context.regions) && context.regions.length));
    }

    function boot() {
        const context = launch();
        if (!hasWork(context)) return;
        // After the viewer, the sidebar and the layer stack exist -- the same
        // moment agentBridge.js starts.
        Promise.resolve(window.__plexoraReady).catch(() => {}).then(() => apply(context));
    }

    if (typeof document !== "undefined") {
        if (document.readyState === "complete"
            || (document.readyState === "interactive" && window.__plexoraReady)) {
            boot();
        } else {
            document.addEventListener("DOMContentLoaded", boot, { once: true });
        }
    }

    return { apply, hasWork };
})();
