/**
 * navbarControls.js
 *
 * Wires the unified File/Edit/Tools/View navbar (base.html) to existing
 * viewer functionality. Plain global script (not a module), loaded on every
 * page -- every handler below is written with optional chaining so it
 * no-ops cleanly on pages that don't have a viewer/sidebar at all (quick-look
 * home, upload wizard, datasource config).
 */
(function () {
    function onReady(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    onReady(() => {
        // File > Export Image
        document.getElementById("nav_export_image")?.addEventListener("click", () => {
            window.__plexora?.seaDragonViewer?.downloadCurrentView?.();
        });

        // File > Quit -- terminates the local server process (see
        // system_routes.py); the fetch is expected to error out as the
        // process exits mid-response, so that's swallowed rather than surfaced.
        document.getElementById("nav_quit")?.addEventListener("click", async () => {
            const confirmed = confirm("Quit Plexora? This will close the local server and this page will stop working.");
            if (!confirmed) return;
            try {
                await fetch(plexoraUrl("shutdown"), { method: "POST", keepalive: true });
            } catch (error) {
                // Expected.
            }
        });

        const sidebarToggle = document.getElementById("nav_toggle_sidebar");
        const scalebarToggle = document.getElementById("nav_toggle_scalebar");
        const outlinesToggle = document.getElementById("nav_toggle_outlines");
        const centroidsToggle = document.getElementById("nav_toggle_centroids");
        const hdToggle = document.getElementById("nav_toggle_hd");

        const sidebarShell = document.getElementById("bodyDiv");
        const sidebarCollapseButton = document.getElementById("sidebar_collapse_button");
        const outlinesEl = document.getElementById("gating_controls_outlines");
        const centroidsEl = document.getElementById("gating_controls_centroids");
        const hdEl = document.getElementById("viewer_controls_hd");

        // View > Show Sidebar -- reuses the existing collapse/expand toggle
        // (viewerSidebar.js) rather than duplicating the collapse logic; synced
        // from the shell's class each time the View menu opens, since the
        // sidebar has no change event of its own to listen for.
        sidebarToggle?.addEventListener("change", () => sidebarCollapseButton?.click());
        document.getElementById("navbarViewDropdown")?.addEventListener("show.bs.dropdown", () => {
            if (sidebarToggle && sidebarShell) {
                sidebarToggle.checked = !sidebarShell.classList.contains("sidebar-collapsed");
            }
        });

        // View > Show Scalebar -- sole owner of this state, no sidebar counterpart.
        scalebarToggle?.addEventListener("change", (e) => {
            window.__plexora?.seaDragonViewer?.setScalebarVisible?.(e.target.checked);
        });

        // View > Show Outlines / Show Centroids / HD Mode -- two-way mirror
        // against the existing sidebar checkboxes. The write direction sets
        // the sidebar checkbox and dispatches "change" on it, so all the real
        // work (segmentation loading, setHdMode, etc.) stays in
        // viewerControls.js -- nothing is duplicated here. The read direction
        // listens for the plexora:*-changed events those handlers dispatch
        // (including their async auto-default/fallback paths on load).
        function wireMirror(navEl, sidebarEl, eventName) {
            if (!navEl || !sidebarEl) return;
            navEl.addEventListener("change", () => {
                sidebarEl.checked = navEl.checked;
                sidebarEl.dispatchEvent(new Event("change", { bubbles: true }));
            });
            window.addEventListener(eventName, (e) => {
                navEl.checked = Boolean(e.detail?.enabled);
            });
        }
        wireMirror(outlinesToggle, outlinesEl, "plexora:outlines-changed");
        wireMirror(centroidsToggle, centroidsEl, "plexora:centroids-changed");
        wireMirror(hdToggle, hdEl, "plexora:hd-mode-changed");
    });
})();
