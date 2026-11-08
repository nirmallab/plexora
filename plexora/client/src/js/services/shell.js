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
 * The host also holds the licence (services/licenseClient.js, plan D8): it
 * hands Python its files before the first page is asked for, answers the
 * licence routes for every page of the frame, and tells the frame's page when
 * the licence changed elsewhere (another tab, the background refresh).
 *
 * Reads `/client/shell.json` (`client/static_build.py`): `{version, py,
 * licence?, telemetry?}`, where `py` is the base of the Python bundle (the
 * assets bucket on the site), `licence` the platform's address when it is not
 * api.biocognia.com (a test build's), and `telemetry.endpoint` the usage-count
 * service the site's builds name (none: nothing is sent).
 *
 * **Under a path.** viewer.plexoraapp.com serves the site at its root. A data
 * node serving the app (`plexora node serve --app`) serves it at `/app/`,
 * and behind an Open OnDemand portal that is `/rnode/<host>/<port>/app/` on
 * the portal's address. The loader's `<base href>` names the site's root
 * either way (server/node/webapp.py writes it per address), and `BASE` is its
 * path: every file the site loads is under it, and every address of the app
 * is `BASE` + the route Python answers (`inApp`). Python renders its pages
 * with `base_url` = `BASE`, so their links stay under it too.
 */
(function (global) {
    "use strict";

    const doc = global.document;
    const host = global.parent !== global && global.parent.PlexoraShellHost
        ? global.parent : null;

    //: The site's base path: "" at a site's root, `/rnode/<host>/<port>/app`
    //: behind a portal (the loader's `<base href>`; see the header). Read
    //: before the frame's document is replaced by the app's page.
    const BASE = (() => {
        try {
            return new URL(doc.baseURI).pathname.replace(/\/+$/, "");
        } catch (error) {
            return "";
        }
    })();
    global.PLEXORA_SITE_BASE = BASE;
    //: A file of the site, by its path from the site's root.
    const at = (path) => BASE + path;
    //: The route an address of the app names, as Python answers it.
    const inApp = (where) => (where.pathname.slice(BASE.length) || "/") + where.search + where.hash;

    const status = (text) => {
        const node = doc.getElementById("plexora-boot-status");
        if (node) node.textContent = text;
    };
    //: What the loader says while nothing is being asked: Python's progress
    //: (`progress`) arrives while a question may be up, and waits for it.
    let asking = 0;
    let progressText = "";
    const progress = (text) => {
        progressText = text;
        if (!asking) status(text);
        // Said in the host for a frame's loader to say too (frameBoot).
        if (!host) global.PlexoraShellProgress = text;
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
        asking += 1;
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
                    asking -= 1;
                    if (!asking && progressText) status(progressText);
                    resolve(action.value);
                });
                row.appendChild(button);
            }
            box.appendChild(row);
        });
    }

    //: The app's page in the frame, once it can show a dialog and a notice:
    //: the frame loads the loader first and writes the app's page into it
    //: after. Null when it never does (30 s).
    async function whenApp(frame) {
        let app = null;
        for (let i = 0; i < 150 && !(app && app.PlexoraConfirm && app.PlexoraToast
                                       && app.PlexoraShellFramed); i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 200));
            app = frame.contentWindow;
        }
        return app && app.PlexoraConfirm ? app : null;
    }

    //: The first visit asks once where the projects are kept
    //: (services/browserStore.js), in the app's own dialog; dismissing it
    //: keeps them in the browser. Only where the browser can write folders.
    async function askWhereOnce(frame) {
        const store = global.PlexoraStore;
        if (!store || !(store.shouldAsk() || store.shouldWarn())) return;
        const app = await whenApp(frame);
        if (!app) return;
        if (!store.shouldAsk()) {
            await keepInBrowser(app, store);
            return;
        }
        const chosen = await app.PlexoraConfirm.choose({
            title: "Save projects on this computer?",
            // A page is never given Documents itself (services/browserStore.js
            // `chooseHome`): the user chooses Plexora's folder in it.
            body: "Your figures, thresholds and gates go in Documents › Plexora, where the desktop app "
                + "finds them too. Choose that folder in the next window, or make it with New Folder. "
                + "Kept in the browser, they are lost if its browsing data is cleared.",
            choices: [{ value: "browser", label: "Keep in browser" },
                      { value: "folder", label: "Save on this computer", kind: "primary", focus: true }],
            close: true,
            inline: true,
        });
        if (chosen === "folder") {
            let why = null;
            const done = await store.useFolder(null).catch((error) => {
                why = error && error.message;
                return null;
            });
            if (done) {
                app.PlexoraToast?.show({ title: `Projects are saved in ${done.label}` });
                return;
            }
            app.PlexoraToast?.show({ title: "Projects kept in this browser",
                                     note: why ? `${why} Change it in Settings › Data.` : "Change it in Settings › Data." });
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

    //: Files fetched together and run one after another, in the order given
    //: (a script inserted with `async` off keeps its place): the loader's
    //: services, which used to wait on each other's download one by one.
    function scripts(sources) {
        return Promise.all(sources.map((src) => new Promise((resolve, reject) => {
            const node = doc.createElement("script");
            node.src = src;
            node.async = false;
            node.onload = resolve;
            node.onerror = () => reject(new Error(`could not load ${src}`));
            doc.head.appendChild(node);
        })));
    }

    //: The pages this browser shows before Python has started: the last
    //: answer Python gave for each, kept in Cache Storage per site version,
    //: Python bundle and base path. Only the home page: what it does is open
    //: something, which waits for Python anyway, while a page that saves what
    //: it shows (Settings, a project) shown from an old copy could save old
    //: values back. A page shown from here is checked against Python's own
    //: answer once Python is up (frameBoot `early`).
    const PAGE_CACHE = "plexora-pages";
    const EARLY_ROUTES = new Set(["/"]);

    function pageStore(shell, py) {
        const caches = global.caches;
        const stamp = encodeURIComponent(`${shell.version || ""}|${py}`);
        const key = (route) => new URL(at(`/__plexora/page${route}?v=${stamp}`), global.location.origin).href;
        const open = () => (caches ? caches.open(PAGE_CACHE) : Promise.reject(new Error("no Cache Storage")));
        return Object.freeze({
            //: The HTML kept for `route`, or null.
            async get(route) {
                if (!EARLY_ROUTES.has(route)) return null;
                try {
                    const found = await (await open()).match(key(route));
                    return found ? await found.text() : null;
                } catch (error) {
                    return null;
                }
            },
            //: Python's answer for `route` kept, and every copy from another
            //: version dropped.
            async put(route, html) {
                if (!EARLY_ROUTES.has(route) || typeof html !== "string" || !html) return;
                try {
                    const cache = await open();
                    const mine = key(route);
                    for (const request of await cache.keys()) {
                        const url = new URL(request.url);
                        if (url.pathname === new URL(mine).pathname && request.url !== mine) await cache.delete(request);
                    }
                    await cache.put(mine, new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } }));
                } catch (error) { /* nothing kept: the next visit waits for Python, as before */ }
            },
        });
    }

    //: The home page as Python draws it on a first visit, rendered when the
    //: site was built (client/static_build.py `home_page`; shell.json `home`):
    //: what a browser that has kept nothing yet shows while Python starts.
    //: At the site's root only: a node serves the app under a path of its own.
    async function builtHome(shell, route) {
        if (!shell.home || BASE || route !== "/") return null;
        try {
            const response = await global.fetch(at(`/${shell.home}`), { cache: "no-cache" });
            return response.ok ? await response.text() : null;
        } catch (error) {
            return null;
        }
    }

    //: Two answers for a page the same but for what time has changed: the
    //: words of each `<time>` ("3 hours ago" a day later).
    const timeless = (html) => String(html).replace(/(<time\b[^>]*>)[^<]*(<\/time>)/g, "$1$2");

    function browserVariables() {
        global.flaskVariables = { backend: "browser", base_url: BASE };
        global.PLEXORA_BASE_URL = BASE;
    }

    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    //: The site's service worker registered, and this page controlled by it.
    //: False where the browser has none (a private window may not): the app's
    //: pages then answer their own requests (`relayed`, services/pageRelay.js).
    async function controlled() {
        if (!global.navigator.serviceWorker) return false;
        try {
            await global.navigator.serviceWorker.register(at("/client/src/js/workers/backendRelay.js"),
                                                          { scope: at("/") });
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
            const response = await global.fetch(at("/__plexora/relayed"), { cache: "no-store" });
            return response.headers.get("X-Plexora-Relayed") === "1";
        } catch (error) {
            return false;
        }
    }

    const LABELS = { core: "Starting Python", packages: "Loading packages",
                     wheels: "Loading Plexora", ready: "Opening" };

    //: The licence this browser holds, restored into Python's files; null
    //: when it cannot start (the site then runs as Free, as a desktop does
    //: with a damaged licence file).
    async function startLicence(shell, loading) {
        try {
            const module = await loading;
            return await module.startInPage({ config: shell.licence || {}, version: shell.version,
                                              python: global.PlexoraPython });
        } catch (error) {
            console.warn("[licence] not started:", error);
            return null;
        }
    }

    //: A change made elsewhere (another tab, the background refresh) shown in
    //: the frame's page: its licence hint, and Settings if it is open. The
    //: page that asked for a change draws the answer itself.
    function followLicence(frame) {
        const licence = global.PlexoraLicense;
        if (!licence) return;
        const key = (status) => JSON.stringify(status ? [status.state, status.entitlements] : null);
        let shown = key(licence.status());
        licence.on(({ kind, status, from }) => {
            if (kind !== "changed" || !status) return;
            const now = key(status);
            if (from === "local" || now === shown) {
                shown = now;
                return;
            }
            shown = now;
            const app = frame.contentWindow;
            try {
                app.PlexoraPro?.adopt?.(status);
                app.dispatchEvent(new app.CustomEvent("plexora:license-external", { detail: status }));
            } catch (error) { /* the frame is between pages */ }
        });
    }

    //: The address the frame shows, as the address bar should say it.
    const here = (where) => where.pathname + where.search + where.hash;

    //: A node's pairing link (`<site>/connect#v=1&e=…&t=…`, printed by
    //: `plexora node serve --pair`): its fragment, taken and cleared from the
    //: address bar and the history before anything else runs, so the token
    //: is never in either; the app opens at the site's start page. Null for
    //: any other address.
    function takePairingLink() {
        const where = global.location;
        if (where.pathname.replace(/\/+$/, "") !== at("/connect")) return null;
        const fragment = where.hash || "";
        global.history.replaceState(null, "", at("/"));
        return fragment;
    }

    //: Telemetry for the worker (plexora/browser/telemetry.py): the desktop's
    //: anonymous usage counts, when the build names the service
    //: (shell.json `telemetry`), with what a desktop's environment would say:
    //: Do Not Track (or Global Privacy Control) turns it off, as DO_NOT_TRACK
    //: does there, and the machine's coarse facts, which the worker cannot
    //: ask for. Null (off) for a build that names no service.
    function telemetryOptions(shell) {
        const endpoint = shell && shell.telemetry && shell.telemetry.endpoint;
        if (!endpoint) return null;
        const nav = global.navigator || {};
        const dnt = nav.doNotTrack === "1" || global.doNotTrack === "1" || nav.globalPrivacyControl === true;
        const platform = String((nav.userAgentData && nav.userAgentData.platform) || nav.platform || nav.userAgent || "");
        const os = /Win/i.test(platform) ? "windows" : /Mac/i.test(platform) ? "mac"
            : /Linux|X11|CrOS/i.test(platform) ? "linux" : "other";
        const context = { os };
        if (nav.hardwareConcurrency) context.cpus = nav.hardwareConcurrency;
        if (nav.deviceMemory) context.memory_gb = nav.deviceMemory;
        return { endpoint: String(endpoint), dnt, context };
    }

    //: A browser turned away (too old for the website's Python), counted
    //: once the message is up: anonymously, at most once a day, under
    //: telemetry's rules (services/refusalCount.js). Only a build that names
    //: the service, in a browser not asking for Do Not Track or Global
    //: Privacy Control, loads the script; nothing here can delay or break
    //: the message.
    function countRefusal(shell, old) {
        const options = telemetryOptions(shell);
        if (!options || options.dnt) return;
        script(at("/client/src/js/services/refusalCount.js"))
            .then(() => global.PlexoraRefusalCount.send({ options, version: shell.version, refused: old }))
            .catch(() => null);
    }

    //: The desktop app's download page (services/desktopHandoff.js's too).
    const DOWNLOAD = "https://github.com/nirmallab/plexora/releases/latest";
    const UPDATE_CHROME = "https://support.google.com/chrome/answer/95414";
    const UPDATE_EDGE = "https://support.microsoft.com/microsoft-edge/settings-update";

    //: The site's switches (plan Phase 5): `flags.json` on the assets bucket,
    //: cached five minutes (`tools/browser_build/publish.py flags`). `viewer`
    //: "desktop-only" pauses the website (the kill switch) and "preview" marks
    //: it; `min_site_version` stops a site older than it; `models.<name>.enabled`
    //: false switches a model off. Unreachable, slow or malformed: no flags --
    //: a missing file never locks anyone out.
    async function readFlags(shell) {
        if (!shell.flags) return {};
        try {
            const where = new URL(shell.flags, new URL(at("/"), global.location.origin)).href;
            const response = await Promise.race([global.fetch(where, { mode: "cors" }),
                                                 wait(3000).then(() => null)]);
            if (!response || !response.ok) return {};
            const flags = await response.json();
            return flags && typeof flags === "object" && !Array.isArray(flags) ? flags : {};
        } catch (error) {
            return {};
        }
    }

    //: Whether dotted version `version` is older than `minimum`, part by part
    //: (a part that is not a number counts as 0).
    function older(version, minimum) {
        const parts = (text) => String(text || "").split(/[.+-]/).map((part) => parseInt(part, 10) || 0);
        const [a, b] = [parts(version), parts(minimum)];
        for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
            if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) < (b[i] || 0);
        }
        return false;
    }

    //: The loader's last word when the site does not start: one line and one
    //: button. Python never loads.
    //: The loader's last word: `text`, and its buttons on one row (`more`:
    //: [{label, act}] after the primary one).
    function stop(text, label, act, more = []) {
        status(text);
        const row = doc.createElement("div");
        row.className = "plexora-boot-actions";
        [{ label, act, primary: true }, ...more].forEach((action) => {
            const button = doc.createElement("button");
            button.type = "button";
            if (action.primary) button.className = "is-primary";
            button.textContent = action.label;
            button.addEventListener("click", action.act);
            row.appendChild(button);
        });
        doc.getElementById("plexora-boot").appendChild(row);
    }

    async function hostBoot() {
        global.PlexoraShellHost = true;
        const pairing = takePairingLink();
        browserVariables();
        const shell = await (await global.fetch(at("/client/shell.json"), { cache: "no-cache" })).json();
        // The site's switches, and the two files that can start Python,
        // fetched together.
        const [flags] = await Promise.all([
            readFlags(shell),
            scripts([at("/client/src/js/services/backendRoutes.js"), at("/client/src/js/services/pythonClient.js")]),
        ]);
        global.PlexoraFlags = Object.freeze(flags);
        if (flags.viewer === "desktop-only") {
            return stop(flags.message || "The website is paused for now. The desktop app works as usual.",
                        "Get the desktop app", () => global.open(DOWNLOAD, "_blank", "noopener"));
        }
        if (flags.min_site_version && older(shell.version, flags.min_site_version)) {
            return stop("This copy of Plexora is out of date.", "Reload", () => global.location.reload());
        }
        if (flags.viewer === "preview") {
            const mark = doc.createElement("div");
            mark.id = "plexora-preview-mark";
            mark.textContent = "Preview";
            doc.body.appendChild(mark);
        }
        // The bundle's address, absolute or from the site's root.
        const py = new URL(shell.py, new URL(at("/"), global.location.origin)).href;
        // A Chrome or Edge too old to run the website's Python (no JSPI)
        // crashes or hangs trying: asked to update, before anything starts.
        const old = global.PlexoraPython.unsupported();
        if (old) {
            stop(`Update ${old.browser} to use Plexora here (version ${old.version}; it needs 137 or later).`,
                 `How to update ${old.browser}`,
                 () => global.open(old.browser === "Edge" ? UPDATE_EDGE : UPDATE_CHROME, "_blank", "noopener"),
                 [{ label: "Get the desktop app", act: () => global.open(DOWNLOAD, "_blank", "noopener") }]);
            return countRefusal(shell, old);
        }
        // Python's downloads first -- the longest part of a visit by far --
        // and everything below while they run.
        global.PlexoraPython.on("mark", ({ mark }) => { if (LABELS[mark]) progress(LABELS[mark]); });
        global.PlexoraPython.warm({ base: py, root: at("/") });
        progress("Starting Python");
        // The page this address shows, as Python last drew it -- or, on a
        // first visit, as the site's build drew it -- looked up now.
        const pages = pageStore(shell, py);
        global.PlexoraPages = pages;
        const route = inApp(global.location);
        const kept = pages.get(route).then((html) => html || builtHome(shell, route));
        // An ES module, fetched while Python starts.
        const licenceModule = import(at("/client/src/js/services/licenseClient.js"));
        licenceModule.catch(() => null);
        const control = controlled();
        await scripts([
            at("/client/src/js/services/backend.js"),
            at("/client/src/js/services/pickedFiles.js"),
            // Saving into a picked file Python wrote (Settings > Data, off by default).
            at("/client/src/js/services/sourceWrites.js"),
            at("/client/src/js/services/browserNode.js"),
            at("/client/src/js/services/browserSlides.js"),
            at("/client/src/js/services/browserTable.js"),
            at("/client/src/js/services/browserPyramid.js"),
            at("/client/src/js/services/browserStore.js"),
            at("/client/src/js/services/desktopHandoff.js"),
            at("/client/src/js/services/handoffNotice.js"),
            at("/client/src/js/services/connectorClient.js"),
            // Connections to servers, held here so they outlive the app
            // frame's pages (docs/internal/WEB_REMOTE_RELAY.md).
            at("/client/src/js/services/relayRoom.js"),
            at("/client/src/js/services/webRemote.js"),
            // Google sign-in for Google Cloud (plan L9): the token is held
            // here, in the host, for the tab's life, and each page's frame
            // borrows it.
            at("/client/src/js/services/googleAuth.js"),
            // Magic select's model: onnxruntime-web from the Python bundle,
            // the model files from the assets' models folder.
            at("/client/src/js/services/onnxExecutor.js"),
        ]);
        global.PlexoraGoogleAuth.configure(shell.google || {});
        global.PlexoraWebRemote.configure({
            relay: shell.relay,
            bundle: `${at("/client/dist/ssh_bundle.js")}?v=${encodeURIComponent(shell.version || "")}`,
            // When the website's remote servers are behind a licence, the
            // relay asks for the certificate the page holds.
            licence: () => (global.PlexoraLicense && typeof global.PlexoraLicense.certificate === "function"
                ? global.PlexoraLicense.certificate() : null),
        });
        const models = new URL(shell.models || "/models/", new URL(at("/"), global.location.origin)).href;
        const sam = ((flags.models || {}).mobilesam || {}).enabled === false;
        global.PlexoraOnnx.configure({ ort: shell.ort || `${py}ort/`, models,
                                       disabled: sam ? "Magic select is switched off on the website for now." : null });
        await control;
        // Where the projects are kept: a folder the user granted, opened again
        // (after one click when the browser asks), or this browser's storage.
        // Chrome's answer "Allow on every visit" ends the question for good.
        const store = await global.PlexoraStore.prepare((record) => askInLoader(
            `Your projects are in ${global.PlexoraStore.labelOf(record.parent)}. `
            + "To open it without this question next time, choose “Allow on every visit”.",
            [{ value: false, label: "Use browser this time" },
             { value: true, label: "Open folder", primary: true }])).catch(() => null);
        // `?pydebug` on the address: the worker's test hooks (`run`, `sync`,
        // `trace`), for diagnosing the site in development.
        const debug = new URLSearchParams(global.location.search).has("pydebug");
        const booting = global.PlexoraPython.start({ base: py, root: at("/"), siteBase: BASE,
                                                     debug, store, telemetry: telemetryOptions(shell) });
        //: Python started, with everything a page's requests may need: the
        //: licence in its files, the store's state, the files picked on
        //: earlier visits mounted. A page shown before then (frameBoot
        //: `early`) holds its requests until it resolves (services/backend.js).
        const started = (async () => {
            const info = await booting;
            if (info.error) throw new Error(info.error);
            // What Help › About says on the website (views/helpMenu.js).
            global.PlexoraSite = Object.freeze({ version: info.version || shell.version || "" });
            // The licence, in Python's files before the first page is rendered.
            global.PlexoraLicense = await startLicence(shell, licenceModule);
            global.PlexoraStore.booted(info.store);
            // The files picked on earlier visits, mounted again where the browser
            // still grants access; the rest wait for a click (PlexoraPicks.reconnect).
            await global.PlexoraPicks.restore().catch(() => null);
            // And the pyramidized copies saved in folders beside their originals.
            await global.PlexoraPyramid.restore().catch(() => null);
            return info;
        })();
        started.catch(() => null);
        global.PlexoraShellStarted = started;
        // The app's frame now when this page was kept, with the kept page
        // handed to it; otherwise once Python has started, as it always was.
        const early = await kept;
        if (early) {
            global.PlexoraShellEarly = { route, html: early };
        } else {
            await started;
        }
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
                started.then(() => askWhereOnce(frame)).catch(() => null);
            }
            started.then(() => keepHome(frame, pages)).catch(() => null);
        });
        doc.body.appendChild(frame);
        await started.catch((error) => {
            // Python did not start under a kept page: the page goes, and the
            // loader says why, as it would have without one.
            frame.remove();
            const box = doc.getElementById("plexora-boot");
            if (box) box.hidden = false;
            throw error;
        });
        followLicence(frame);
        // A node's pairing link: paired once the app can say how it went.
        if (pairing !== null) {
            whenApp(frame).then((app) => global.PlexoraConnector.pair(
                global.PlexoraConnector.parse(pairing) || { invalid: true }, app)).catch(() => null);
        }
    }

    //: The home page kept as Python would draw it now, a few seconds after the
    //: frame shows any other page: opening a project changes its Recent rows,
    //: and the next visit's home page should not be the one from before.
    let homeTimer = null;
    function keepHome(frame, pages) {
        let route = null;
        try {
            route = inApp(frame.contentWindow.location);
        } catch (error) {
            return;
        }
        if (EARLY_ROUTES.has(route)) return;
        global.clearTimeout(homeTimer);
        homeTimer = global.setTimeout(async () => {
            try {
                const response = await global.PlexoraPython.fetch("/", { headers: { Accept: "text/html" } });
                if (response.ok) await pages.put("/", await response.text());
            } catch (error) { /* kept as it was */ }
        }, 5000);
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

    //: Takes this frame out of the host's layout for two of the host's frames,
    //: and resolves to the function that puts it back; frameBoot opens the
    //: app's document in between.
    //:
    //: Chrome (157) never gives pointer capture to a document opened while
    //: the pointer is over its frame -- over the last page of the app, or
    //: over this loader while it waited on Python, which is every page but a
    //: first one the mouse has not yet touched. `setPointerCapture` does
    //: nothing, and every pointer event arrives as if the pointer had just
    //: come in from outside the page: pointerover and pointerenter from the
    //: document down. OpenSeadragon takes the position from that pointerenter
    //: as the drag's last position, so every drag measured 0 px and the image
    //: zoomed but never panned; anything else that captures the pointer (a
    //: slider dragged past its end) lost it. The document stays like that
    //: when the pointer leaves and comes back. Opened while the frame has no
    //: layout box it is an ordinary document. Hiding it any other way
    //: (pointer-events, visibility, display for less than two frames) is not
    //: enough. Reproduced outside Plexora with a host page, an iframe and
    //: document.open alone (tests/test_shell_pointer_capture.py).
    //:
    //: The host's frames, not this one's: a frame without a box draws none.
    //: They can come late -- 0.46 and 0.71 s on the preview in headless
    //: Chrome while a project opened -- and a frame cut short by a timer is
    //: no use, so the timer is only for frames that never come. A tab in
    //: the background draws none at all; it is opened as before.
    function hiddenForOpen() {
        const frame = global.frameElement;
        if (!frame || host.document.visibilityState !== "visible") return Promise.resolve(() => {});
        const before = frame.style.display;
        frame.style.display = "none";
        const shown = () => { frame.style.display = before; };
        return new Promise((resolve) => {
            const done = () => {
                global.clearTimeout(timer);
                resolve(shown);
            };
            const timer = global.setTimeout(done, 5000);
            host.requestAnimationFrame(() => host.requestAnimationFrame(done));
        });
    }

    //: The app's page `html` in place of this loader. `project`: the
    //: project it shows, or "".
    async function writePage(html, project, address) {
        const shown = await hiddenForOpen();
        doc.open();
        shown();
        // WebKit gives a document opened here the host's address (it takes
        // the entry document from the host's promise this continues): this
        // page's own address back, before the page's scripts read it.
        if (here(global.location) !== address) global.history.replaceState(null, "", address);
        doc.write(html);
        doc.close();
        doc.documentElement.classList.add("is-website");
        // More than a browser tab handles well: the desktop app offered.
        if (project && host.PlexoraEnvelope) host.PlexoraEnvelope.check(project, global).catch(() => null);
        // On the document, not the button: the app's router swaps pages in
        // place, so the project is the one shown when the item is clicked.
        doc.addEventListener("click", (event) => {
            const item = event.target.closest && event.target.closest("#nav_open_desktop, #nav_open_bundle");
            if (!item || !host.PlexoraHandoff) return;
            if (item.id === "nav_open_bundle") {
                host.PlexoraHandoff.openFile(global);
                return;
            }
            const shown = global.flaskVariables && global.flaskVariables.datasource;
            if (shown) host.PlexoraHandoff.open(shown, global);
        });
        mirror();
    }

    //: The written page parsed, its deferred scripts run.
    const parsed = () => (doc.readyState !== "loading" ? Promise.resolve()
        : new Promise((resolve) => doc.addEventListener("DOMContentLoaded", resolve, { once: true })));

    //: The page the host kept for this address (hostBoot `early`), taken once.
    function takeEarly(route) {
        const early = host.PlexoraShellEarly;
        host.PlexoraShellEarly = null;
        return early && early.route === route && typeof early.html === "string" ? early.html : null;
    }

    //: A kept page shown at once, while Python starts. Its requests wait for
    //: Python (services/backend.js `PlexoraShellGate`) and the status chip
    //: says "Starting" meanwhile. Then Python's own answer for the address:
    //: kept for the next visit; the same, or the same but for its times
    //: (written in); otherwise the page loads again from Python -- unless it
    //: has been used (pressed, typed in), when it stays as it is: a reload
    //: would throw away what was being done, a file being picked among it.
    async function earlyPage(route, html, address) {
        await writePage(html, "", address);
        let used = false;
        const use = () => { used = true; };
        const USES = ["pointerdown", "keydown", "input", "change", "drop"];
        for (const type of USES) global.addEventListener(type, use, true);
        const task = parsed().then(() => (global.PlexoraStatus ? global.PlexoraStatus.begin("Starting") : null));
        let fresh = null;
        try {
            await host.PlexoraShellStarted;
            const response = await global.PlexoraPython.fetch(route, { headers: { Accept: "text/html" } });
            if (response.ok) fresh = await response.text();
        } catch (error) {
            // Python did not start: the host takes the frame down and says so.
            return;
        } finally {
            const shown = await task;
            if (shown) shown.done();
            for (const type of USES) global.removeEventListener(type, use, true);
        }
        if (fresh === null) return;
        host.PlexoraPages.put(route, fresh);
        if (fresh === html) return;
        // Left for another page meanwhile (a Recent row opened): `global` is
        // that page's window now, and nothing here is about it.
        if (global.document !== doc) return;
        if (timeless(fresh) === timeless(html)) {
            const times = new DOMParser().parseFromString(fresh, "text/html").querySelectorAll("time");
            doc.querySelectorAll("time").forEach((node, i) => {
                if (times[i]) node.textContent = times[i].textContent;
            });
            return;
        }
        if (!used) global.location.replace(global.location.href);
    }

    async function frameBoot() {
        const address = here(global.location);
        browserVariables();
        // The host's Python and picks, not a second worker.
        global.PlexoraPython = host.PlexoraPython;
        global.PlexoraPicks = host.PlexoraPicks;
        global.PlexoraNode = host.PlexoraNode;
        // The host's licence, which it holds only once Python has started: read
        // through to it, since a kept page is up before then.
        Object.defineProperty(global, "PlexoraLicense", {
            configurable: true, enumerable: true, get: () => host.PlexoraLicense,
        });
        global.PlexoraGoogleAuth = host.PlexoraGoogleAuth;
        global.PlexoraShellFramed = true;
        // What the page's requests wait for (services/backend.js).
        global.PlexoraShellGate = host.PlexoraShellStarted || null;
        if (global.PlexoraNode) global.PlexoraNode.revalidate();
        await claimed();
        await scripts([at("/client/src/js/services/backendRoutes.js"), at("/client/src/js/services/backend.js")]);
        // Where the site's worker does not see this page's requests, the
        // page answers them itself.
        if (!await relayed()) {
            await script(at("/client/src/js/services/pageRelay.js"));
            global.PlexoraPageRelay.install(global);
            global.PlexoraShellRelay = "page";
        }
        const route = inApp(global.location);
        const early = takeEarly(route);
        if (early !== null) return earlyPage(route, early, address);
        // A page asked for while Python starts (one opened from a kept page)
        // is drawn once the licence and the picked files are in; this loader
        // says how far Python has got meanwhile, as the host's would.
        if (host.PlexoraShellStarted) {
            if (host.PlexoraShellProgress) progress(host.PlexoraShellProgress);
            const off = global.PlexoraPython.on("mark", ({ mark }) => { if (LABELS[mark]) progress(LABELS[mark]); });
            try {
                await host.PlexoraShellStarted;
            } finally {
                off();
            }
        }
        const response = await global.PlexoraPython.fetch(route, { headers: { Accept: "text/html" } });
        let html = await response.text();
        const project = projectOf(html);
        if (project && await findFiles(project)) {
            html = await (await global.PlexoraPython.fetch(route, { headers: { Accept: "text/html" } })).text();
        }
        // The next visit's first page, shown before Python is up.
        if (response.ok && !project && host.PlexoraPages) host.PlexoraPages.put(route, html);
        // A project's page: the browser node asks Python what it may serve
        // now, ahead of the page's own requests, so its first tile does not
        // wait behind them (or behind the work opening the project queues).
        if (project && global.PlexoraNode) global.PlexoraNode.project(project);
        await writePage(html, project, address);
    }

    (host ? frameBoot() : hostBoot())
        .catch((error) => fail(`Plexora could not start: ${error.message || error}`));
})(window);
