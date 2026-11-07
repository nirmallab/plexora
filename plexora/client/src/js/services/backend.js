/**
 * PlexoraBackend -- who answers this page's requests.
 *
 * Every page today is served by a Plexora: the desktop app's, `plexora` on a
 * laptop, a server on a cluster. That Plexora answers every request the page
 * makes, and it is the "server" backend. In it this file is a PASS-THROUGH:
 * `fetch` is `window.fetch` -- the same arguments, the same promise, called at
 * the moment it is called, so the wrapper appStatus.js installs right after
 * this file is in the path like it is for any other request -- `supports` says
 * yes to what the server can do, and `verify` has nothing to check. Nothing a
 * server-mode page does changes because this file is loaded.
 *
 * The website is the "browser" kind: the same Flask app runs in the page's
 * own worker (services/pythonClient.js, plexora/browser). Its route table
 * (backendRoutes.js, generated from the app's url map by
 * plexora/browser/routes.py) classes every endpoint as "js" (answered in the
 * page), "python" (the worker), "static" (a page of the site), "network" (a
 * file, as it is) or "handoff" (what a web page cannot do; answered with the
 * notice that says so, and the page told with a `plexora:handoff` event, which
 * services/handoff.js draws). `answer(request)` answers by that class; the site's
 * service worker (workers/backendRelay.js) hands it every same-origin request
 * the page makes -- fetch, XMLHttpRequest, an <img>, a stylesheet's url() --
 * so no caller changes. A "js" route goes to the page's browser node first
 * (services/browserNode.js: a project's tiles read from the picked file) and
 * to the worker where the node declines. Any other kind sends every request
 * to the network.
 *
 *   kind          "server", or `flaskVariables.backend` where the page says
 *   target        where what the page does not answer itself goes: "server",
 *                 the Plexora that served it; "network" for a kind whose
 *                 backends are not in this build
 *   routes        the route table, or null -- always null in server mode,
 *                 whatever was loaded: no table can move a server page's
 *                 requests
 *   supports(f)   whether this backend can do feature `f` (unknown: false)
 *   verify(path)  a promise of whether `path` is answered here as the server
 *                 would answer it; the server is its own answer:
 *                 `{ok: true, skipped: "server"}`
 *   fetch(...)    `window.fetch(...)` (browser: answered as `answer` would)
 *   answer(req)   browser: a promise of the Response for a Request
 *   classify(url, method)  browser: the route table's row for it, or null
 *
 * Classic script, `window.PlexoraBackend`; not deferred, and right before
 * appStatus.js in base.html, so it exists before anything issues a request.
 */
(function (global) {
    "use strict";

    const SERVER = "server";

    //: What the server answers today, by the feature name a page asks about
    //: (`PlexoraBackend.supports`): the things a static build may not have.
    //: Features of a static build alone -- the browser's own file picker for
    //: an import -- are not here, and say false.
    const SERVER_FEATURES = Object.freeze({
        //: AI gating, QC and chat: the gateway and its harness are Python.
        ai: true,
        //: Data nodes and SSH connections to a cluster (services/remoteState.js,
        //: services/connectionModal.js).
        remote: true,
        //: Interactive segmentation (services/segmentService.js).
        segment: true,
        //: A z-stack's planes, projections and sections (`/volume/...`).
        volume: true,
        //: 3-D surfaces of a z-stack (services/surfaceLayers.js).
        surfaces: true,
        //: The agent bridge an MCP client drives the viewer through
        //: (services/agentBridge.js, `/agent/v1`).
        agent: true,
    });

    const BROWSER = "browser";

    const variables = global.flaskVariables || {};
    const kind = typeof variables.backend === "string" && variables.backend
        ? variables.backend : SERVER;
    const server = kind === SERVER;
    const browser = kind === BROWSER;
    const routes = server ? null : (global.PlexoraBackendRoutes || null);
    //: The browser's own fetch, before anything wraps window.fetch.
    const nativeFetch = global.fetch;

    //: What the website can do itself, by the same names (a feature arriving
    //: in a later phase says false until it does).
    const BROWSER_FEATURES = Object.freeze({
        //: The browser's own file picker (services/pickedFiles.js).
        file_picker: true,
        volume: true,
        surfaces: true,
        agent: true,
    });

    const compiled = routes ? routes.map((row) => ({ ...row, regex: new RegExp(row.pattern) })) : [];

    //: A path of the site as the route table names it: the site's base path
    //: (services/shell.js `BASE`; "" at a site's root) taken off; null for a
    //: path outside the site (a node's own `/node/v1/...` beside it on a
    //: portal), which no route names.
    function routeOf(pathname) {
        const base = String(global.PLEXORA_SITE_BASE || "");
        if (!base) return pathname;
        if (pathname === base || pathname.startsWith(base + "/")) return pathname.slice(base.length) || "/";
        return null;
    }

    function classify(url, method = "GET") {
        const path = routeOf(new URL(String(url), global.location ? global.location.href : "http://x/").pathname);
        if (path === null) return null;
        const verb = String(method || "GET").toUpperCase();
        for (const row of compiled) {
            if ((row.methods.includes(verb) || (verb === "HEAD" && row.methods.includes("GET")))
                && row.regex.test(path)) {
                return row;
            }
        }
        return null;
    }

    //: The handoffs the page's notice (services/handoff.js) has not heard yet,
    //: the last few: it takes each one it hears, and when it loads, those
    //: answered before it was there.
    const handoffs = [];

    //: A handoff answered: the page says so (services/handoff.js) unless the
    //: panel that asked drew the notice itself.
    function told(detail) {
        handoffs.push(detail);
        if (handoffs.length > 8) handoffs.shift();
        if (typeof global.dispatchEvent !== "function" || typeof CustomEvent !== "function") return;
        try {
            global.dispatchEvent(new CustomEvent("plexora:handoff", { detail }));
        } catch (error) { /* a page between documents */ }
    }

    function json(value, status) {
        return new Response(JSON.stringify(value), {
            status, headers: { "Content-Type": "application/json" },
        });
    }

    async function answer(request) {
        const url = new URL(request.url, global.location.href);
        if (url.origin !== global.location.origin) return nativeFetch(request);
        const row = classify(url, request.method);
        const kindOf = row ? row.class : "network";
        // A "static" page asked for by fetch (the app's router wants a
        // fragment) is rendered by the worker; only a navigation gets the
        // site's own page, and navigations never come here.
        if (kindOf === "network") return nativeFetch(request);
        if (kindOf === "handoff") {
            told({ endpoint: row.endpoint, path: routeOf(url.pathname), method: request.method,
                   handoff: row.handoff });
            return json({ success: false, error: row.handoff.why, handoff: row.handoff }, 501);
        }
        // Under a base path (a node serving the app behind a portal), every
        // answerer below is handed the route as the app names it.
        const route = routeOf(url.pathname);
        if (route !== url.pathname) {
            request = new Request(new URL(route + url.search, url.origin).href, request);
        }
        // "js": the page's own answer where it has one -- the licence the page
        // holds (services/licenseClient.js), a project's tiles read from the
        // picked file (services/browserNode.js); Python's otherwise.
        if (kindOf === "js" && global.PlexoraLicense) {
            const answered = await global.PlexoraLicense.answer(request).catch(() => null);
            if (answered) return answered;
        }
        if (kindOf === "js" && global.PlexoraNode) {
            const answered = await global.PlexoraNode.answer(request).catch(() => null);
            if (answered) return answered;
        }
        const python = global.PlexoraPython;
        if (!python) return json({ success: false, error: "Plexora's Python is not loaded" }, 503);
        return python.fetch(request);
    }

    function supports(feature) {
        if (typeof feature !== "string") return false;
        const table = server ? SERVER_FEATURES : (browser ? BROWSER_FEATURES : null);
        return Boolean(table) && Object.prototype.hasOwnProperty.call(table, feature)
            && table[feature] === true;
    }

    function verify(path) {
        if (server) return Promise.resolve({ ok: true, skipped: SERVER });
        // What a static build checks -- that `routes` classes `path` and that
        // what the class names is there -- arrives with its backends.
        return Promise.resolve({ ok: false, path: String(path ?? ""),
                                 reason: `no ${kind} backend in this build` });
    }

    function fetch(...args) {
        if (browser) {
            const [input, init] = args;
            const request = input instanceof Request ? new Request(input, init)
                : new Request(new URL(String(input), global.location.href), init);
            return answer(request);
        }
        return global.fetch(...args);
    }

    // The service worker's side asks here (workers/backendRelay.js): one
    // message per request, answered on the port it came with. Registered once
    // per window, though a page written into it (the site's loader writes the
    // worker's page) loads this file again.
    if (browser && global.navigator && global.navigator.serviceWorker
        && !global.__plexoraRelayListening) {
        global.__plexoraRelayListening = true;
        global.navigator.serviceWorker.addEventListener("message", async (event) => {
            const data = event.data;
            if (!data || data.type !== "plexora-relay" || !event.ports[0]) return;
            const port = event.ports[0];
            try {
                const request = new Request(data.url, {
                    method: data.method, headers: data.headers,
                    body: ["GET", "HEAD"].includes(data.method) ? undefined : data.body,
                });
                const response = await global.PlexoraBackend.answer(request);
                const body = await response.arrayBuffer();
                port.postMessage({ status: response.status, headers: [...response.headers.entries()],
                                   body }, [body]);
            } catch (error) {
                port.postMessage({ status: 500, headers: [["Content-Type", "text/plain"]],
                                   body: new TextEncoder().encode(String(error && error.message || error)).buffer });
            }
        });
    }

    global.PlexoraBackend = Object.freeze({
        kind,
        target: server ? SERVER : (browser ? "worker" : "network"),
        routes,
        supports,
        verify,
        fetch,
        answer: browser ? answer : null,
        classify: browser ? classify : null,
        handoffs,
    });
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraBackend;
    }
})(typeof window !== "undefined" ? window : globalThis);
