/**
 * toolLoader.js - Always loaded, unlike a plugin's own scripts, which are fetched
 * lazily and named by the plugin's descriptor (see plexora/api/plugin.py's
 * Plugin.scripts and asset_urls).
 * Intercepts the navbar Tools-menu links so opening/closing an addon tool mid-session
 * never navigates: on first open it fetches the tool's sidebar HTML + scripts from
 * `/<datasource>/tools/<tool>/panel`, injects them, and hands off to
 * main.js's `__plexora.activatePlugin()` to register the plugin exactly as it
 * would be at boot. Later opens just toggle visibility -- nothing is re-fetched.
 *
 * Deliberately kept off the `__plexora` object: that object is created fresh by
 * main.js (`const __plexora = window.__plexora = {...}`), which -- because this
 * script runs first in document order -- would clobber anything stored on it before
 * main.js's own init() runs.
 */
window.PlexoraToolLoader = (function () {
    const loadedTools = new Map(); // toolName -> { slotIds, sidebarController }

    function setSlotsHidden(slotIds, hidden) {
        slotIds.forEach((slotId) => {
            const el = document.getElementById(slotId);
            if (el) el.classList.toggle("tool-panel-hidden", hidden);
        });
    }

    function loadScript(src) {
        if (document.querySelector(`script[src="${src}"]`)) return Promise.resolve();
        return new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = src;
            script.onload = () => resolve();
            script.onerror = () => reject(new Error(`toolLoader: failed to load ${src}`));
            document.head.appendChild(script);
        });
    }

    async function openTool(toolName, linkEl) {
        const existing = loadedTools.get(toolName);
        if (existing) {
            setSlotsHidden(existing.slotIds, false);
            existing.sidebarController?.onShow?.();
            return;
        }

        linkEl?.classList.add("tool-loading");
        try {
            const datasource = window.flaskVariables?.datasource;
            const baseUrl = window.PLEXORA_BASE_URL || "";
            const response = await fetch(`${baseUrl}/${datasource}/tools/${toolName}/panel`);
            const payload = await response.json();

            if (payload.redirect) {
                // Same fallback as the plain <a href> would have hit server-side --
                // no real feature data yet, hand off to the upload wizard.
                window.location.href = payload.redirect;
                return;
            }

            const slotIds = Object.keys(payload.fragments || {});
            slotIds.forEach((slotId) => {
                const mount = document.getElementById(slotId);
                if (mount) mount.innerHTML = payload.fragments[slotId];
            });

            for (const src of payload.scripts || []) {
                await loadScript(src);
            }

            // main.js runs before any tool can be opened (deferred, but earlier in
            // document order isn't guaranteed here -- this script loads first --
            // so wait on its own readiness promise rather than assuming it's done).
            await window.__plexoraReady;
            const moduleDef = window.Plexora?.plugins?.get(toolName);
            if (!moduleDef || !window.__plexora?.activatePlugin) return;
            const { sidebarController } = await window.__plexora.activatePlugin(moduleDef);

            loadedTools.set(toolName, { slotIds, sidebarController });
            setSlotsHidden(slotIds, false);
            sidebarController?.onShow?.();
        } finally {
            linkEl?.classList.remove("tool-loading");
        }
    }

    function hideTool(toolName) {
        const entry = loadedTools.get(toolName);
        if (entry) setSlotsHidden(entry.slotIds, true);
    }

    // Called by main.js when a tool was already active at boot (a direct/bookmarked
    // ?tool=gating load rendered its panel server-side, not through openTool() above) --
    // without this, the close button's hideToolPanel() would find nothing to hide, and
    // a later Tools-menu click would re-fetch/re-activate a module that's already live.
    function registerLoaded(toolName, slotIds, sidebarController) {
        if (!loadedTools.has(toolName)) {
            loadedTools.set(toolName, { slotIds, sidebarController });
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("a[data-tool]").forEach((link) => {
            link.addEventListener("click", (event) => {
                event.preventDefault();
                openTool(link.dataset.tool, link);
            });
        });
    });

    return { hideToolPanel: hideTool, registerLoaded };
})();
