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

    async function controlled() {
        if (!("serviceWorker" in global.navigator)) {
            throw new Error("this browser has no service workers, which the website needs");
        }
        await global.navigator.serviceWorker.register("/client/src/js/workers/backendRelay.js",
                                                      { scope: "/" });
        if (global.navigator.serviceWorker.controller) return;
        await new Promise((resolve) => {
            global.navigator.serviceWorker.addEventListener("controllerchange", resolve, { once: true });
        });
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
        await controlled();
        status("Starting Python");
        global.PlexoraPython.on("mark", ({ mark }) => { if (LABELS[mark]) status(LABELS[mark]); });
        // `?pydebug` on the address: the worker's test hooks (`run`, `sync`,
        // `trace`), for diagnosing the site in development.
        const debug = new URLSearchParams(global.location.search).has("pydebug");
        const info = await global.PlexoraPython.start({ base: shell.py, debug });
        if (info.error) throw new Error(info.error);
        // The files picked on earlier visits, mounted again where the browser
        // still grants access; the rest wait for a click (PlexoraPicks.reconnect).
        await global.PlexoraPicks.restore().catch(() => null);
        const frame = doc.createElement("iframe");
        frame.id = "plexora-app";
        frame.title = "Plexora";
        frame.className = "plexora-app-frame";
        frame.src = here(global.location);
        frame.addEventListener("load", () => {
            const box = doc.getElementById("plexora-boot");
            if (box) box.hidden = true;
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

    async function frameBoot() {
        browserVariables();
        // The host's Python and picks, not a second worker.
        global.PlexoraPython = host.PlexoraPython;
        global.PlexoraPicks = host.PlexoraPicks;
        global.PlexoraNode = host.PlexoraNode;
        global.PlexoraShellFramed = true;
        if (global.PlexoraNode) global.PlexoraNode.revalidate();
        await script("/client/src/js/services/backendRoutes.js");
        await script("/client/src/js/services/backend.js");
        const response = await global.PlexoraPython.fetch(
            here(global.location), { headers: { Accept: "text/html" } });
        const html = await response.text();
        // A project's page: the browser node asks Python what it may serve
        // now, ahead of the page's own requests, so its first tile does not
        // wait behind them (or behind the work opening the project queues).
        const project = projectOf(html);
        if (project && global.PlexoraNode) global.PlexoraNode.project(project);
        doc.open();
        doc.write(html);
        doc.close();
        mirror();
    }

    (host ? frameBoot() : hostBoot())
        .catch((error) => fail(`Plexora could not start: ${error.message || error}`));
})(window);
