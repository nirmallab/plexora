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
        // File > Add Image used to be wired here: a native file picker, then a
        // POST to /quick_view with the picked path. Both ends of that still
        // exist -- browsePicker.js, and the home page's Quick Look drop zone,
        // which is the way in now -- so this is a handler that went with its
        // menu row rather than a feature that was removed.

        // File > Export Image submenu -- hover/focus reveals it via CSS
        // (see main.css); this click handler is only the touch/keyboard
        // fallback for devices without hover. Ignore clicks that originated
        // on the PNG/PDF buttons themselves, so they run their own handler
        // below and close the dropdown normally instead of re-toggling.
        const exportMenu = document.getElementById("nav_export_menu");
        exportMenu?.addEventListener("click", (event) => {
            if (event.target.closest(".nav-submenu")) return;
            exportMenu.classList.toggle("open");
        });
        document.getElementById("nav_export_image_png")?.addEventListener("click", () => {
            window.__plexora?.seaDragonViewer?.downloadCurrentView?.("png");
        });
        document.getElementById("nav_export_image_pdf")?.addEventListener("click", () => {
            window.__plexora?.seaDragonViewer?.downloadCurrentView?.("pdf");
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
            document.body.innerHTML = `
                <div class="plexora-quit-overlay">
                    <span class="fas fa-power-off plexora-quit-icon"></span>
                    <h1>Plexora has quit</h1>
                    <p>The local server has stopped. You can close this tab.</p>
                </div>`;
        });

        const sidebarToggle = document.getElementById("nav_toggle_sidebar");
        const scalebarToggle = document.getElementById("nav_toggle_scalebar");
        const hdToggle = document.getElementById("nav_toggle_hd");

        const sidebarShell = document.getElementById("bodyDiv");
        const sidebarCollapseButton = document.getElementById("sidebar_collapse_button");
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

        // View > HD Mode -- two-way mirror against the sidebar checkbox. The
        // write direction sets the sidebar checkbox and dispatches "change" on
        // it, so all the real work (setHdMode) stays in viewerControls.js --
        // nothing is duplicated here. The read direction listens for the
        // plexora:*-changed event that handler dispatches.
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
        wireMirror(hdToggle, hdEl, "plexora:hd-mode-changed");

        // View > Cells -- the same one-of-four choice the sidebar offers, and
        // the same single implementation behind it: this hands the mode to
        // ViewerControls rather than reproducing any of the loading work.
        //
        // Which options are usable is a property of the project (is there a
        // mask? does it store whole labels? are there coordinates?), which lives
        // in the config the viewer holds. Read on open rather than pushed here,
        // because this script binds on DOMContentLoaded and the config arrives
        // later -- an availability event fired at init would land before anyone
        // is listening.
        const cellModeRadios = Array.from(
            document.querySelectorAll('input[name="nav_cell_mode"]'));

        cellModeRadios.forEach((radio) => {
            radio.addEventListener("change", () => {
                if (!radio.checked) return;
                const controls = window.__plexora?.viewerControls;
                if (!controls) return;
                // A menu click is as much a decision as a sidebar click, and
                // must equally outrank whatever the automatic fallback chose.
                if (window.__plexora?.seaDragonViewer) {
                    window.__plexora.seaDragonViewer.centroidsFromFallback = false;
                }
                controls.selectMode(radio.value);
            });
        });

        // Mirrors what the sidebar control OFFERS, not merely what the project
        // can draw: with a plugin layer active the choice is narrowed to the
        // modes that plugin uses, and "No Cells" goes away entirely -- the
        // plugin's own card is what turns its layer off. A menu that kept
        // offering the full four would be a second, disagreeing answer to the
        // same question.
        function syncCellMode() {
            const controls = window.__plexora?.viewerControls;
            if (!controls || !cellModeRadios.length) return;
            const offered = controls.offeredModes();
            const available = controls.availability();
            cellModeRadios.forEach((radio) => {
                const usable = Boolean(offered[radio.value]);
                radio.checked = radio.value === controls.mode;
                radio.disabled = !usable;
                // Same rule as the sidebar buttons: a mode this PROJECT cannot
                // draw stays visible and disabled (that is a fact worth seeing),
                // a mode the active PLUGIN does not use is hidden.
                const item = radio.closest(".nav-check-item");
                if (item) item.hidden = !usable && Boolean(available[radio.value]);
            });
        }

        window.addEventListener("plexora:cell-mode-changed", syncCellMode);
        document.getElementById("navbarViewDropdown")
            ?.addEventListener("show.bs.dropdown", syncCellMode);
    });
})();
