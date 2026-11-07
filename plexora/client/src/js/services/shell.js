/**
 * The website's loader: what viewer.plexoraapp.com serves for every path.
 *
 * Plexora's pages are rendered by Plexora's own Python, which on the website
 * runs in a worker beside the page (services/pythonClient.js), and it takes
 * seconds to start. So the site runs in two layers, both this script:
 *
 * - **The host** -- the top-level page, loaded once. It sets the page up for
 *   the browser backend (the route table, the worker's client,
 *   services/backend.js, services/pickedFiles.js), registers the site's
 *   service worker (workers/backendRelay.js, which hands every request a page
 *   makes for a route to services/backend.js), starts Python behind a boot
 *   screen, mounts the files picked on earlier visits, and then shows the app
 *   in a frame filling the window.
 * - **The frame** -- every page of the app. Loading the address in the frame
 *   serves this loader again, which borrows the host's Python (and its
 *   picks) instead of starting its own, asks it for the page this address
 *   names -- the same HTML the desktop's server would have sent -- and writes
 *   it into the frame's document.
 *
 * A link, a form, `location.href = ...`, the app's own router: every way a
 * page moves to another is an ordinary navigation of the frame, with a fresh
 * document and nothing of the last page left running, and none of them starts
 * Python again. The host keeps the address bar and the tab's title in step
 * with the frame; the browser's Back and Forward move the frame, whose
 * entries are the tab's history.
 *
 * Reads `/client/shell.json` (`client/static_build.py`): `{version, py}`, where
 * `py` is the base of the Python bundle (the assets bucket on the site).
 */
(function (global) {
    "use strict";

    const doc = global.document;
    const host = global.parent !== global && global.parent.PlexoraShellHost
        ? global.parent : null;

    const status = (text) => {
        const node = doc.getElementById("plexora-boot-status");
        if (node) node.textContent = text;
    };
    const fail = (text) => {
        status(text);
        const box = doc.getElementById("plexora-boot");
        if (box) box.classList.add("plexora-boot-failed");
    };

    //: A question in the loader, before the app starts: `text`, then a button
    //: per action; resolves with the chosen action's value. (A click here is
    //: what lets the page ask the browser for a permission.)
    function askInLoader(text, actions) {
        status(text);
        const box = doc.getElementById("plexora-boot");
        const row = doc.createElement("div");
        row.className = "plexora-boot-actions";
        return new Promise((resolve) => {
            for (const action of actions) {
                const button = doc.createElement("button");
                button.type = "button";
                button.textContent = action.label;
                if (action.primary) button.className = "is-primary";
                button.addEventListener("click", () => {
                    row.remove();
                    resolve(action.value);
                });
                row.appendChild(button);
            }
            box.appendChild(row);
        });
    }

    //: The first visit asks once where the projects are kept
    //: (services/browserStore.js), in the app's own dialog; dismissing it
    //: keeps them in the browser. Only where the browser can write folders.
    async function askWhereOnce(frame) {
        const store = global.PlexoraStore;
        if (!store || !(store.shouldAsk() || store.shouldWarn())) return;
        // The frame loads the loader first and writes the app's page into it
        // after: wait for the app's own dialog.
        let app = null;
        for (let i = 0; i < 150 && !(app && app.PlexoraConfirm && app.PlexoraShellFramed); i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 200));
            app = frame.contentWindow;
        }
        if (!app || !app.PlexoraConfirm) return;
        if (!store.shouldAsk()) {
            await keepInBrowser(app, store);
            return;
        }
        const chosen = await app.PlexoraConfirm.choose({
            title: "Save projects on this computer?",
            body: "Your figures, thresholds and gates stay safe in Documents › Plexora. "
                + "Kept in the browser, they are lost if its browsing data is cleared.",
            choices: [{ value: "browser", label: "Keep in browser" },
                      { value: "folder", label: "Save on this computer", kind: "primary", focus: true }],
            close: true,
            inline: true,
        });
        if (chosen === "folder") {
            const done = await store.useFolder(null).catch(() => null);
            if (done) {
                app.PlexoraToast?.show({ title: `Projects are saved in ${done.label}` });
                return;
            }
            app.PlexoraToast?.show({ title: "Projects kept in this browser", note: "Change it in Settings › Data." });
        }
        await store.useBrowser();
    }

    //: Where the browser cannot write folders the projects stay in it, which
    //: may clear them (Safari after a week unused; any browser short of room)
    //: unless it agrees to keep them (`persist()`, from the click: Firefox
    //: asks the user). Said once; where nothing is kept, every visit.
    async function keepInBrowser(app, store) {
        if (store.current().storage === "memory") {
            await app.PlexoraConfirm.choose({
                title: "Projects are not kept",
                body: "This browser lets the site keep nothing: they are lost when the tab closes.",
                choices: [{ value: "ok", label: "OK", kind: "primary", focus: true }],
                close: true,
                inline: true,
            });
            return;
        }
        const storage = app.navigator.storage;
        if (storage && storage.persisted && await storage.persisted().catch(() => false)) {
            await store.useBrowser();
            return;
        }
        const chosen = await app.PlexoraConfirm.choose({
            title: "Projects are kept in this browser",
            body: "It may clear them if the site goes unused. Ask it to keep them?",
            choices: [{ value: "later", label: "Not now" },
                      { value: "keep", label: "Keep them", kind: "primary", focus: true }],
            close: true,
            inline: true,
        });
        await store.useBrowser();
        if (chosen !== "keep" || !storage || !storage.persist) return;
        const kept = await storage.persist().catch(() => false);
        app.PlexoraToast?.show(kept ? { title: "This browser will keep them" }
            : { title: "This browser may still clear them", note: "Chrome and Edge can save them on this computer." });
    }

    function script(src) {
        return new Promise((resolve, reject) => {
            const node = doc.createElement("script");
            node.src = src;
            node.onload = resolve;
            node.onerror = () => reject(new Error(`could not load ${src}`));
            doc.head.appendChild(node);
        });
    }

    function browserVariables() {
        global.flaskVariables = { backend: "browser", base_url: "" };
        global.PLEXORA_BASE_URL = "";
    }

    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    //: The site's service worker registered, and this page controlled by it.
    //: False where the browser has none (a private window may not): the app's
    //: pages then answer their own requests (`relayed`, services/pageRelay.js).
    async function controlled() {
        if (!global.navigator.serviceWorker) return false;
        try {
            await global.navigator.serviceWorker.register("/client/src/js/workers/backendRelay.js",
                                                          { scope: "/" });
        } catch (error) {
            return false;
        }
        return claimed();
    }

    //: This page controlled by the site's worker. A navigation the browser did
    //: not hand to an active worker (a forced reload; Playwright's Firefox
    //: after the first) is claimed by asking it.
    async function claimed() {
        const workers = global.navigator.serviceWorker;
        if (!workers) return false;
        if (workers.controller) return true;
        const changed = new Promise((resolve) => workers.addEventListener("controllerchange", resolve, { once: true }));
        const registration = await Promise.race([workers.ready, wait(5000).then(() => null)]);
        if (!registration) return false;
        if (!workers.controller && registration.active) registration.active.postMessage("plexora-claim");
        return Promise.race([changed.then(() => true), wait(5000).then(() => Boolean(workers.controller))]);
    }

    //: Whether the site's worker sees this page's requests: it answers this one
    //: itself. Being its `controller` is not enough (Playwright's Firefox never
    //: hands it a frame's requests).
    async function relayed() {
        try {
            const response = await global.fetch("/__plexora/relayed", { cache: "no-store" });
            return response.headers.get("X-Plexora-Relayed") === "1";
        } catch (error) {
            return false;
        }
    }

    const LABELS = { core: "Starting Python", packages: "Loading packages",
                     wheels: "Loading Plexora", ready: "Opening" };

    //: The address the frame shows, as the address bar should say it.
    const here = (where) => where.pathname + where.search + where.hash;

    async function hostBoot() {
        global.PlexoraShellHost = true;
        browserVariables();
        const shell = await (await global.fetch("/client/shell.json", { cache: "no-cache" })).json();
        await script("/client/src/js/services/backendRoutes.js");
        await script("/client/src/js/services/pythonClient.js");
        await script("/client/src/js/services/backend.js");
        await script("/client/src/js/services/pickedFiles.js");
        await script("/client/src/js/services/browserNode.js");
        await script("/client/src/js/services/browserTable.js");
        await script("/client/src/js/services/browserPyramid.js");
        await script("/client/src/js/services/browserStore.js");
        await script("/client/src/js/services/desktopHandoff.js");
        await controlled();
        // Where the projects are kept: a folder the user granted, opened again
        // (after one click when the browser asks), or this browser's storage.
        const store = await global.PlexoraStore.prepare((record) => askInLoader(
            `Your projects are in ${global.PlexoraStore.labelOf(record.parent)}.`,
            [{ value: false, label: "Use browser this time" },
             { value: true, label: "Open folder", primary: true }])).catch(() => null);
        status("Starting Python");
        global.PlexoraPython.on("mark", ({ mark }) => { if (LABELS[mark]) status(LABELS[mark]); });
        // `?pydebug` on the address: the worker's test hooks (`run`, `sync`,
        // `trace`), for diagnosing the site in development.
        const debug = new URLSearchParams(global.location.search).has("pydebug");
        const info = await global.PlexoraPython.start({ base: shell.py, debug, store });
        if (info.error) throw new Error(info.error);
        global.PlexoraStore.booted(info.store);
        // The files picked on earlier visits, mounted again where the browser
        // still grants access; the rest wait for a click (PlexoraPicks.reconnect).
        await global.PlexoraPicks.restore().catch(() => null);
        // And the pyramidized copies saved in folders beside their originals.
        await global.PlexoraPyramid.restore().catch(() => null);
        const frame = doc.createElement("iframe");
        frame.id = "plexora-app";
        frame.title = "Plexora";
        frame.className = "plexora-app-frame";
        frame.src = here(global.location);
        let asked = false;
        frame.addEventListener("load", () => {
            const box = doc.getElementById("plexora-boot");
            if (box) box.hidden = true;
            if (!asked) {
                asked = true;
                askWhereOnce(frame).catch(() => null);
            }
        });
        doc.body.appendChild(frame);
    }

    //: Keep the tab's address and title what the frame shows.
    function mirror() {
        const top = host;
        const sync = () => {
            try {
                if (here(top.location) !== here(global.location)) {
                    top.history.replaceState(null, "", here(global.location));
                }
                if (doc.title && top.document.title !== doc.title) top.document.title = doc.title;
            } catch (error) { /* the host went away */ }
        };
        for (const name of ["pushState", "replaceState"]) {
            const original = global.history[name].bind(global.history);
            global.history[name] = function (...args) {
                const result = original(...args);
                sync();
                return result;
            };
        }
        global.addEventListener("popstate", sync);
        global.addEventListener("hashchange", sync);
        global.addEventListener("load", sync);
        const title = doc.querySelector("title");
        if (title) new MutationObserver(sync).observe(title, { childList: true, characterData: true, subtree: true });
        sync();
    }

    //: The project a page Python rendered is for (its variables block's
    //: `datasource`), or "".
    function projectOf(html) {
        const found = /<script type="application\/json" id="plexora-variables">([\s\S]*?)<\/script>/.exec(html);
        if (!found) return "";
        try {
            return JSON.parse(found[1]).datasource || "";
        } catch (error) {
            return "";
        }
    }

    //: A project made in the desktop app names its files as that app knows
    //: them: asked once, in the loader, for the folder that holds them
    //: (PlexoraPicks.find), and what is found is recorded for Python. True
    //: when something was found (the page is then asked for again).
    async function findFiles(project) {
        const route = `/project/${encodeURIComponent(project)}/files_here`;
        let missing = [];
        try {
            missing = (await (await global.PlexoraPython.fetch(route)).json()).missing || [];
        } catch (error) {
            return false;
        }
        if (!missing.length || !global.PlexoraPicks || !global.PlexoraPicks.find) return false;
        // A folder to look in where the browser has a folder picker; the file
        // itself where it has not (Firefox, Safari).
        const folders = typeof global.showDirectoryPicker === "function";
        const label = folders ? "Find them…" : missing.length === 1 ? "Locate file…" : "Locate files…";
        const go = await askInLoader(
            missing.length === 1 ? "This project's image is on this computer." : "This project's files are on this computer.",
            [{ value: false, label: "Skip" }, { value: true, label, primary: true }]);
        if (!go) return false;
        status("Looking for the files");
        const found = await global.PlexoraPicks.find(missing).catch(() => ({}));
        if (!Object.keys(found).length) return false;
        await global.PlexoraPython.fetch(route, { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ found }) });
        return true;
    }

    async function frameBoot() {
        const address = here(global.location);
        browserVariables();
        // The host's Python and picks, not a second worker.
        global.PlexoraPython = host.PlexoraPython;
        global.PlexoraPicks = host.PlexoraPicks;
        global.PlexoraNode = host.PlexoraNode;
        global.PlexoraShellFramed = true;
        if (global.PlexoraNode) global.PlexoraNode.revalidate();
        await claimed();
        await script("/client/src/js/services/backendRoutes.js");
        await script("/client/src/js/services/backend.js");
        // Where the site's worker does not see this page's requests, the
        // page answers them itself.
        if (!await relayed()) {
            await script("/client/src/js/services/pageRelay.js");
            global.PlexoraPageRelay.install(global);
            global.PlexoraShellRelay = "page";
        }
        const response = await global.PlexoraPython.fetch(
            here(global.location), { headers: { Accept: "text/html" } });
        let html = await response.text();
        const project = projectOf(html);
        if (project && await findFiles(project)) {
            html = await (await global.PlexoraPython.fetch(
                here(global.location), { headers: { Accept: "text/html" } })).text();
        }
        // A project's page: the browser node asks Python what it may serve
        // now, ahead of the page's own requests, so its first tile does not
        // wait behind them (or behind the work opening the project queues).
        if (project && global.PlexoraNode) global.PlexoraNode.project(project);
        doc.open();
        // WebKit gives a document opened here the host's address (it takes
        // the entry document from the host's promise this continues): this
        // page's own address back, before the page's scripts read it.
        if (here(global.location) !== address) global.history.replaceState(null, "", address);
        doc.write(html);
        doc.close();
        doc.documentElement.classList.add("is-website");
        // On the document, not the button: the app's router swaps pages in
        // place, so the project is the one shown when the item is clicked.
        doc.addEventListener("click", (event) => {
            const item = event.target.closest && event.target.closest("#nav_open_desktop");
            const shown = global.flaskVariables && global.flaskVariables.datasource;
            if (item && shown && host.PlexoraHandoff) host.PlexoraHandoff.open(shown, global);
        });
        mirror();
    }

    (host ? frameBoot() : hostBoot())
        .catch((error) => fail(`Plexora could not start: ${error.message || error}`));
})(window);
