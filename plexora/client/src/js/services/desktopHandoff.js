/**
 * PlexoraHandoff -- the website's "Open in Desktop App".
 *
 * The desktop app reads the projects the website keeps on this computer,
 * Documents/Plexora/projects, as its own (plexora/paths.py library_roots),
 * and finds their files itself (server/utils/relink.py). So opening a project
 * there is one link: `plexora://open?project=<name>`, which the desktop app
 * answers (desktop/src-tauri/src/opens.rs). Projects kept in the browser are
 * first moved into that folder (services/browserStore.js: the user confirms
 * the place once, and the browser asks to allow editing); nothing is exported
 * or imported.
 *
 * Whether the app answered cannot be asked; the page losing focus within
 * LAUNCH_MS is taken as yes (the app came forward, or the browser asked to
 * open it), and otherwise the download is offered.
 *
 *   open(project, app) -> from a click; `app` is the app's frame window (toasts)
 *
 * Classic script, `window.PlexoraHandoff`; needs PlexoraStore and PlexoraPython.
 */
(function (global) {
    "use strict";

    const LAUNCH_MS = 1500;
    const DOWNLOAD = "https://github.com/nirmallab/plexora/releases/latest";

    //: Follow `url` and say whether the page lost focus soon after.
    function launch(url, app) {
        return new Promise((resolve) => {
            let left = false;
            const away = () => { left = true; };
            const windows = [global, app].filter(Boolean);
            for (const win of windows) {
                win.addEventListener("blur", away);
                win.document.addEventListener("visibilitychange", away);
            }
            const link = global.document.createElement("a");
            link.href = url;
            link.style.display = "none";
            global.document.body.appendChild(link);
            link.click();
            link.remove();
            global.setTimeout(() => {
                for (const win of windows) {
                    win.removeEventListener("blur", away);
                    win.document.removeEventListener("visibilitychange", away);
                }
                resolve(left);
            }, LAUNCH_MS);
        });
    }

    async function open(project, app) {
        const store = global.PlexoraStore;
        const toast = (options) => app && app.PlexoraToast && app.PlexoraToast.show(options);
        if (!store) return false;
        if (store.current().kind !== "folder") {
            if (!store.canWriteFolders()) {
                toast({ title: "Open it in the desktop app",
                        note: "This browser cannot save projects on this computer; Chrome and Edge can." });
                return false;
            }
            const moved = await store.useFolder(null).catch(() => null);
            if (!moved) return false;
            toast({ title: `Projects are saved in ${moved.label}` });
        } else {
            await global.PlexoraPython.flush();
        }
        const answered = await launch(`plexora://open?project=${encodeURIComponent(project)}`, app);
        if (!answered) {
            toast({ title: "Get the desktop app", note: "It opens this project with nothing to import.",
                    timeout: 0,
                    actions: [{ label: "Download", primary: true,
                                onSelect: () => global.open(DOWNLOAD, "_blank", "noopener") }] });
        }
        return answered;
    }

    global.PlexoraHandoff = Object.freeze({ open });
})(window);
