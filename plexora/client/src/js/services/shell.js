/**
 * The website's loader: what viewer.plexoraapp.com serves for every path.
 *
 * Plexora's pages are rendered by Plexora's own Python, which on the website
 * runs in a worker beside the page (services/pythonClient.js). This page puts
 * that in place and then shows the page the address asks for -- the same HTML
 * the desktop's server would have sent, rendered by the same templates:
 *
 * 1. `flaskVariables.backend = "browser"`, then the route table
 *    (backendRoutes.js), the worker's client and services/backend.js;
 * 2. the site's service worker (workers/backendRelay.js), which hands every
 *    request a page makes for a route to services/backend.js, and waits until
 *    it controls this page;
 * 3. Python: Pyodide, the base packages, the app (a boot screen meanwhile,
 *    with what is loading);
 * 4. the page itself, asked of Python for this address and written into this
 *    document. The window -- and with it the worker -- stays: the app's own
 *    router moves between its pages from here without loading anything again.
 *
 * Reads `/client/shell.json` (`client/static_build.py`): `{version, py}`, where
 * `py` is the base of the Python bundle (the assets bucket on the site).
 */
(function (global) {
    "use strict";

    const doc = global.document;
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

    async function boot() {
        global.flaskVariables = { backend: "browser", base_url: "" };
        global.PLEXORA_BASE_URL = "";
        const shell = await (await global.fetch("/client/shell.json", { cache: "no-cache" })).json();
        await script("/client/src/js/services/backendRoutes.js");
        await script("/client/src/js/services/pythonClient.js");
        await script("/client/src/js/services/backend.js");
        await controlled();
        status("Starting Python");
        global.PlexoraPython.on("mark", ({ mark }) => { if (LABELS[mark]) status(LABELS[mark]); });
        const info = await global.PlexoraPython.start({ base: shell.py });
        if (info.error) throw new Error(info.error);
        const response = await global.PlexoraPython.fetch(
            global.location.pathname + global.location.search, { headers: { Accept: "text/html" } });
        const html = await response.text();
        doc.open();
        doc.write(html);
        doc.close();
    }

    boot().catch((error) => fail(`Plexora could not start: ${error.message || error}`));
})(window);
