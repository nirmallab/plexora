/**
 * navbarControls.js
 *
 * Wires the navbar's File and View menus (base.html) to existing viewer
 * functionality. The Tools menu and the View menu's tool rows are
 * toolLoader.js's. Plain global script (not a module), loaded on every
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
        // POST with the picked path. Both ends of that still exist -- the
        // picker is browsePicker.js, and the POST is `/import/sample`, which
        // the home page's Select File / Select Folder pair and this menu's own
        // Import Sample row both reach. So this is a handler that went with
        // its menu row rather than a feature that was removed.

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
            // In the desktop app, Quit is the app's own: it closes the window,
            // and the shell stops the server it started. Nothing to confirm --
            // it is what File > Quit means in every other desktop program.
            if (window.PlexoraDesktop) {
                window.PlexoraDesktop.quit();
                return;
            }
            const confirmed = await window.PlexoraConfirm.ask({
                title: "Quit Plexora?",
                body: "This stops the local server, and this page will stop working.",
                confirm: "Quit",
            });
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

        // View > Scalebar -- the one checkbox left in the View menu, because
        // it is the one control there with no other home: nothing in the
        // sidebar shows or hides the bar. The menu's other rows are tools
        // (Rotate, Flip), which toolLoader.js opens like any other.
        //
        // What used to be here -- a Sidebar checkbox, four Cells radios and
        // HD mode -- each mirrored a control the sidebar already carries, and
        // was a second answer to the same question that had to be kept in
        // step with the first. The sidebar's own collapse button carries the
        // mod+\ chord now (index.html).
        document.getElementById("nav_toggle_scalebar")?.addEventListener("change", (e) => {
            window.__plexora?.seaDragonViewer?.setScalebarVisible?.(e.target.checked);
        });
    });
})();
