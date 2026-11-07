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
 *   openApp(app)       -> from a click: the app brought forward, no project
 *   openFile(app)      -> from a click: a `.plexora` project file opened here
 *   DOWNLOAD           the desktop app's download page
 *
 * Where the browser cannot save into a folder (Firefox, Safari), the project
 * goes as one file instead (server/models/bundles.py): downloaded here, and
 * opened by the desktop app (it is the app's file type).
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

    //: The project's `.plexora` file, saved by the browser.
    function download(project, app) {
        const link = app.document.createElement("a");
        link.href = `${global.PLEXORA_SITE_BASE || ""}/project/${encodeURIComponent(project)}/bundle`;
        link.download = `${project}.plexora`;
        app.document.body.appendChild(link);
        link.click();
        link.remove();
    }

    //: From a click: a `.plexora` file chosen, put in this browser's projects
    //: (or Plexora's folder), and opened.
    function openFile(app) {
        const input = app.document.createElement("input");
        input.type = "file";
        input.accept = ".plexora";
        input.style.display = "none";
        input.addEventListener("change", async () => {
            const file = input.files && input.files[0];
            input.remove();
            if (!file) return;
            const answer = await app.fetch(`${global.PLEXORA_SITE_BASE || ""}/projects/bundle`, { method: "POST", body: file,
                                                                headers: { "Content-Type": "application/zip" } });
            const body = await answer.json().catch(() => ({}));
            if (!answer.ok || !body.project) {
                app.PlexoraToast?.show({ title: "That file could not be opened", note: body.error || "" });
                return;
            }
            app.location.href = `${global.PLEXORA_SITE_BASE || ""}/${encodeURIComponent(body.project)}`;
        });
        app.document.body.appendChild(input);
        input.click();
    }

    global.PlexoraHandoff = Object.freeze({ open, openApp, openFile, download, DOWNLOAD });
})(window);
