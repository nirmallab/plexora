/**
 * toolLoader.js - Always loaded, unlike a plugin's own scripts, which are fetched
 * lazily and named by the plugin's descriptor (see plexora/api/plugin.py's
 * Plugin.scripts and asset_urls).
 * Intercepts the navbar Tools-menu links so opening/closing an addon tool mid-session
 * never navigates: on first open it fetches the tool's sidebar HTML, stylesheets and
 * scripts from `/<datasource>/tools/<tool>/panel`, injects all three, and hands off to
 * main.js's `__plexora.activatePlugin()` to register the plugin exactly as it
 * would be at boot. Later opens just toggle visibility -- nothing is re-fetched.
 *
 * Each tool gets its OWN mount inside the shared slot. That was not so while
 * gating was the only plugin: the fragment was written straight to
 * `slot.innerHTML`, which is a whole-slot replace. With a second plugin that is
 * destructive in a way nothing reports -- opening B wipes A's panel out of the
 * DOM while A's controller keeps the element handles it took at setup(), and the
 * re-open path here (which only unhides the slot) then shows an empty panel and
 * a live controller wired to nodes that are no longer on the page.
 *
 * Visibility is therefore a function of one variable -- which tool is active --
 * applied in `paint()`, rather than something each call site toggles for itself.
 *
 * Switching away also has to TELL the tool. A sidebar panel can be hidden and
 * left running, but a tool that reaches outside its panel -- viewer-canvas
 * pointer handlers, document-level keyboard shortcuts -- has to stand those down
 * or it keeps eating input for a panel the user cannot see. That is what
 * `onHide()` is for (see pluginRegistry.js); `ctx.onCleanup` remains the
 * full-teardown path for when a plugin is deactivated outright.
 *
 * Deliberately kept off the `__plexora` object: that object is created fresh by
 * main.js (`const __plexora = window.__plexora = {...}`), which -- because this
 * script runs first in document order -- would clobber anything stored on it before
 * main.js's own init() runs.
 */
window.PlexoraToolLoader = (function () {
    const loadedTools = new Map(); // toolName -> { slotIds, sidebarController }

    //: The tool whose panels are showing, or null when none is. Every
    //: show/hide decision is derived from this rather than tracked per element.
    let activeToolName = null;

    const HIDDEN = "tool-panel-hidden";
    const MOUNT_ATTR = "data-tool-panel";

    /** One tool's wrapper inside one slot, created on demand. */
    function mountFor(slotId, toolName, create) {
        const slot = document.getElementById(slotId);
        if (!slot) return null;
        let mount = slot.querySelector?.(`[${MOUNT_ATTR}="${toolName}"]`) || null;
        if (!mount && create) {
            mount = document.createElement("div");
            mount.className = "tool-panel-mount";
            mount.setAttribute(MOUNT_ATTR, toolName);
            slot.appendChild(mount);
        }
        return mount;
    }

    /**
     * Show the active tool's mounts and hide everyone else's.
     *
     * A slot is hidden when nothing in it is showing, which is what the class on
     * the slot itself has always meant -- the server renders it that way for a
     * page opened with no tool.
     */
    function paint() {
        const slotIds = new Set();
        loadedTools.forEach((entry) => entry.slotIds.forEach((id) => slotIds.add(id)));

        slotIds.forEach((slotId) => {
            const slot = document.getElementById(slotId);
            if (!slot) return;
            let showing = false;
            loadedTools.forEach((entry, name) => {
                if (!entry.slotIds.includes(slotId)) return;
                const visible = name === activeToolName;
                showing = showing || visible;
                const mount = mountFor(slotId, name);
                if (mount) mount.classList.toggle(HIDDEN, !visible);
            });
            slot.classList.toggle(HIDDEN, !showing);
        });
    }

    /**
     * Stand the showing tool down, unless it is the one being opened.
     *
     * Called before another tool is shown rather than when its own close button
     * is pressed, so a tool never has to know it was switched away from.
     */
    function standDown(except) {
        if (!activeToolName || activeToolName === except) return;
        const entry = loadedTools.get(activeToolName);
        activeToolName = null;
        try {
            entry?.sidebarController?.onHide?.();
        } catch (error) {
            console.error("toolLoader: onHide() failed", error);
        }
    }

    function show(toolName) {
        standDown(toolName);
        activeToolName = toolName;
        paint();
        // Before onShow(), not after: a controller that re-applies its cell
        // colours there goes through an owner-gated setter, and would be turned
        // away for not yet holding the layer it is about to be handed. Only
        // plugins that declared ownsCellLayer are affected -- see
        // main.js's reclaimCellLayer.
        try {
            window.__plexora?.reclaimCellLayer?.(toolName);
        } catch (error) {
            console.error("toolLoader: reclaimCellLayer() failed", error);
        }
        try {
            loadedTools.get(toolName)?.sidebarController?.onShow?.();
        } catch (error) {
            console.error("toolLoader: onShow() failed", error);
        }
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

    function loadStyle(href) {
        if (document.querySelector(`link[rel="stylesheet"][href="${href}"]`)) return Promise.resolve();
        return new Promise((resolve) => {
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = href;
            // Resolves either way, unlike loadScript: a plugin whose stylesheet
            // is missing still works, so refusing to open it would turn a
            // cosmetic fault into a broken tool. The console line is what says
            // an unstyled panel was not intended.
            link.onload = () => resolve();
            link.onerror = () => {
                console.error(`toolLoader: failed to load ${href}`);
                resolve();
            };
            document.head.appendChild(link);
        });
    }

    async function openTool(toolName, linkEl) {
        if (loadedTools.has(toolName)) {
            show(toolName);
            return;
        }

        linkEl?.classList.add("tool-loading");
        try {
            const datasource = window.flaskVariables?.datasource;
            const baseUrl = window.PLEXORA_BASE_URL || "";
            const response = await fetch(`${baseUrl}/${datasource}/tools/${toolName}/panel`);
            const payload = await response.json();

            if (payload.needs) {
                // The tool is installed and compatible but the project is
                // missing something it declared. Ask for exactly that, then
                // re-enter -- navigating away to collect a column name would
                // tear down and rebuild the whole viewer to answer one
                // question, which is the reason this lazy path exists.
                const satisfied = await window.PlexoraRequirements.collect(
                    datasource, payload.needs);
                if (!satisfied) return;
                return openTool(toolName, linkEl);
            }

            if (payload.redirect) {
                // Unknown datasource or an uninstalled tool -- the server has
                // decided where to send us.
                window.location.href = payload.redirect;
                return;
            }

            // Stylesheets before the markup, and awaited: the fragments below
            // are shown as soon as their slots are unhidden, and a plugin's own
            // CSS is the only thing that styles them. Skipping this is what
            // made gating's panels render raw -- the file input as a bare
            // "Choose File", the download panel with no surface -- whenever the
            // tool was opened from the Tools menu rather than loaded with
            // ?tool=..., which is the path base.html covers.
            await Promise.all((payload.styles || []).map(loadStyle));

            const slotIds = Object.keys(payload.fragments || {});
            slotIds.forEach((slotId) => {
                // Into this tool's own mount, never the slot: the slot is shared
                // and writing the whole of it destroys any other tool's panel.
                const mount = mountFor(slotId, toolName, true);
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
            show(toolName);
        } finally {
            linkEl?.classList.remove("tool-loading");
        }
    }

    function hideTool(toolName) {
        if (!loadedTools.has(toolName)) return;
        standDown();
        paint();
    }

    /**
     * Wrap a server-rendered panel in the same per-tool mount the lazy path
     * creates, so both paths leave the slot in the same shape.
     *
     * Without this, a page opened with ?tool=gating has gating's markup sitting
     * loose in the slot; opening a second tool would append its mount alongside
     * and `paint()` would have nothing to hide the first one by.
     */
    function adopt(slotId, toolName) {
        const slot = document.getElementById(slotId);
        if (!slot || !slot.appendChild) return;
        if (slot.querySelector?.(`[${MOUNT_ATTR}="${toolName}"]`)) return;
        const mount = document.createElement("div");
        mount.className = "tool-panel-mount";
        mount.setAttribute(MOUNT_ATTR, toolName);
        while (slot.firstChild) mount.appendChild(slot.firstChild);
        slot.appendChild(mount);
    }

    // Called by main.js when a tool was already active at boot (a direct/bookmarked
    // ?tool=gating load rendered its panel server-side, not through openTool() above) --
    // without this, the close button's hideToolPanel() would find nothing to hide, and
    // a later Tools-menu click would re-fetch/re-activate a module that's already live.
    function registerLoaded(toolName, slotIds, sidebarController) {
        if (loadedTools.has(toolName)) return;
        slotIds.forEach((slotId) => adopt(slotId, toolName));
        loadedTools.set(toolName, { slotIds, sidebarController });
        // Through show(), so this path is not a second, quieter version of the
        // menu one. A server-rendered panel is every bit as visible as a lazily
        // opened one and needs the same onShow(): ROI attaches its viewer-canvas
        // and document handlers there, so setting activeToolName directly gave
        // a ?tool=roi page a panel that looked right and a pen that drew
        // nothing -- no pointer handlers, no shortcuts, and no error to say so.
        show(toolName);
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("a[data-tool]").forEach((link) => {
            link.addEventListener("click", (event) => {
                event.preventDefault();
                openTool(link.dataset.tool, link);
            });
        });
    });

    return {
        hideToolPanel: hideTool,
        registerLoaded,
        /** Which tool's panels are showing, for a tool deciding whether its own
         *  global shortcuts should be listening. */
        activeTool: () => activeToolName,
    };
})();
