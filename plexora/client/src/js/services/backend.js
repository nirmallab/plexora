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
 * A static build of the same pages (no Flask behind them; the browser primary
 * and node answer in the page, a paired Python node answers the rest) is a
 * different kind. Its route table (backendRoutes.js, generated from the Flask
 * app's url map, loaded before this file) classes every endpoint pattern as
 * "js" (answered in the page), "python" (a paired Python node), "static" (a
 * file of the build), "handoff" (another app's) or "network" (as it is), and
 * `fetch` will answer by that class. Not yet: until those backends exist a
 * page of any kind sends every request to the network, exactly as here.
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
 *   fetch(...)    `window.fetch(...)`
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

    const variables = global.flaskVariables || {};
    const kind = typeof variables.backend === "string" && variables.backend
        ? variables.backend : SERVER;
    const server = kind === SERVER;
    const routes = server ? null : (global.PlexoraBackendRoutes || null);

    function supports(feature) {
        if (!server || typeof feature !== "string") return false;
        return Object.prototype.hasOwnProperty.call(SERVER_FEATURES, feature)
            && SERVER_FEATURES[feature] === true;
    }

    function verify(path) {
        if (server) return Promise.resolve({ ok: true, skipped: SERVER });
        // What a static build checks -- that `routes` classes `path` and that
        // what the class names is there -- arrives with its backends.
        return Promise.resolve({ ok: false, path: String(path ?? ""),
                                 reason: `no ${kind} backend in this build` });
    }

    function fetch(...args) {
        return global.fetch(...args);
    }

    global.PlexoraBackend = Object.freeze({
        kind,
        target: server ? SERVER : "network",
        routes,
        supports,
        verify,
        fetch,
    });
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraBackend;
    }
})(typeof window !== "undefined" ? window : globalThis);
