/**
 * PlexoraHandoff -- the website's "Open in Desktop App".
 *
 * The desktop app reads the projects the website keeps on this computer,
 * Documents/Plexora/projects, as its own (plexora/paths.py library_roots),
 * and finds their files itself (server/utils/relink.py). So opening a project
 * there is one link: `plexora://open?project=<name>`, which the desktop app
 * answers (desktop/src-tauri/src/opens.rs). Projects kept in the browser are
 * first moved into a workspace (services/browserStore.js: the user chooses
 * the folder, and the browser asks to allow editing); nothing is exported or
 * imported. A workspace elsewhere than Documents/Plexora is the website's
 * until the desktop app is told of it (its Settings › Data › Locations):
 * that is said, rather than a link the desktop app could not follow.
 *
 * Whether the app answered cannot be asked; the page losing focus within
 * LAUNCH_MS is taken as yes (the app came forward, or the browser asked to
 * open it), and otherwise the download is offered.
 *
 *   open(project, app) -> from a click; `app` is the app's frame window (toasts)
 *   openApp(app)       -> from a click: the app brought forward, no project
 *   openFile(app)      -> from a click: a `.plexora` project file imported here
 *                         (the Import dialog, views/projectImport.js)
 *   download(project, app) -> the Export dialog on that project
 *   DOWNLOAD           the desktop app's download page
 *
 * Where the browser cannot save into a folder (Firefox, Safari), the project
 * goes as one file instead (server/models/bundles.py): exported here (the
 * Export dialog, then downloaded), and opened by the desktop app (it is the
 * app's file type).
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
                        note: "Download the project file, then open it with the desktop app.",
                        timeout: 0,
                        actions: [{ label: "Download", primary: true, onSelect: () => download(project, app) },
                                  { label: "Get the desktop app", onSelect: () => global.open(DOWNLOAD, "_blank", "noopener") }] });
                return false;
            }
            let why = null;
            // A workspace chosen before but not open this visit is asked for
            // again (one permission, no folder dialog); otherwise one is chosen.
            const lapsed = store.needsReconnect && store.needsReconnect();
            const moved = await (lapsed
                ? store.reconnect().then((done) => {
                    if (done.ok) return done;
                    why = done.reason === "denied" ? "The browser did not allow Plexora to open its folder."
                        : "Plexora's folder could not be opened. Choose it again in Settings › Data.";
                    return null;
                })
                : store.useFolder(null)).catch((error) => {
                why = error && error.message;
                return null;
            });
            if (!moved) {
                if (why) toast({ title: "Projects were not moved", note: why });
                return false;
            }
            if (moved.desktopFinds === false) return elsewhere(moved.label, app);
            toast({ title: `Projects are saved in ${moved.label}` });
        } else {
            const place = store.current();
            if (place.desktopFinds === false) return elsewhere(place.label, app);
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

    //: A workspace the desktop app does not look in by itself (anywhere but
    //: Documents/Plexora): what to do there, and the app brought forward
    //: with nothing to open. False: no project was handed over.
    function elsewhere(label, app) {
        app?.PlexoraToast?.show({
            title: "Add this folder in the desktop app",
            note: `Your projects are in “${label}”. The desktop app opens this folder once you add it `
                + "in the desktop app under Settings › Data › Locations.",
            timeout: 0,
            actions: [{ label: "Open desktop app", primary: true, onSelect: () => { openApp(app); } }],
        });
        return false;
    }

    //: The desktop app brought forward with nothing to open (a notice of
    //: services/handoff.js on a page with no project): the download offered
    //: when nothing answered.
    async function openApp(app) {
        const answered = await launch("plexora://open", app);
        if (!answered) {
            app?.PlexoraToast?.show({ title: "Get the desktop app", timeout: 0,
                                      actions: [{ label: "Download", primary: true,
                                                  onSelect: () => global.open(DOWNLOAD, "_blank", "noopener") }] });
        }
        return answered;
    }

    //: The project's `.plexora` file: the Export dialog (views/projectExport.js),
    //: whose website transport (services/bundleWeb.js) writes it in the
    //: page's own worker -- where the user saves it, or into this site's
    //: storage and then downloaded -- never whole in Python's memory.
    function download(project, app) {
        const dialog = app && app.PlexoraProjectExport;
        if (dialog && dialog.available()) {
            dialog.open({ names: [project] });
            return true;
        }
        app?.PlexoraToast?.show({ title: "Export is not ready yet", note: "Reload the page, then try again." });
        return false;
    }

    //: From a click: a `.plexora` file chosen, then the Import dialog
    //: (views/projectImport.js) on it, which says what is in it and where it
    //: goes before anything is added; the file is read where it is, a part
    //: at a time (services/bundleWeb.js), never sent whole.
    function openFile(app) {
        const dialog = app && app.PlexoraProjectImport;
        if (!dialog || !dialog.available()) {
            app?.PlexoraToast?.show({ title: "Import is not ready yet", note: "Reload the page, then try again." });
            return false;
        }
        const input = app.document.createElement("input");
        input.type = "file";
        input.accept = ".plexora";
        input.style.display = "none";
        input.addEventListener("change", () => {
            const file = input.files && input.files[0];
            input.remove();
            if (file) dialog.open({ file });
        });
        app.document.body.appendChild(input);
        input.click();
        return true;
    }

    global.PlexoraHandoff = Object.freeze({ open, openApp, openFile, download, DOWNLOAD });
})(window);
