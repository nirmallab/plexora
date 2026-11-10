/**
 * PlexoraRouting -- whether this browser fetches a resource from the server it
 * is talking to, or straight from the machine that holds it.
 *
 * A project whose image, mask and table are all on this server -- which is
 * every ordinary project -- never gets here in any meaningful sense:
 * `/resource_routing` answers `{}`, nothing is probed, nothing is stored, and
 * every URL is built exactly as it was before this file existed. The cost of
 * the whole mechanism, for the case that does not use it, is one request that
 * returns four bytes.
 *
 * When a resource IS on a data node there are two ways to reach it and the
 * difference is a network hop per tile:
 *
 *   proxy    browser -> this server -> node.  Always works when this server
 *            can reach the node, which it could when the project loaded.
 *   direct   browser -> node.  One hop instead of two.
 *
 * **Which one is right is not something the server can know.** Three things
 * have to hold at once for direct to work -- the node's address has to resolve
 * from the browser's network, the node has to have been started with this
 * viewer's origin in `--allow-origin`, and the token has to match -- and every
 * one of them is a fact about the machine this script is running on. A cluster
 * node is reachable from a laptop through a tunnel and from nowhere else; a
 * portal rewrites addresses; a laptop node is invisible to the server that
 * serves the page. So the browser asks, once, by fetching the node's own
 * health endpoint, and falls back to the proxy on anything short of a clean
 * answer.
 *
 * The verdict is cached in sessionStorage, because it is a fact about this
 * tab's network position: it does not change while the tab is open, and asking
 * again on every navigation inside the app would put a 1.5-second timeout in
 * front of a page that is otherwise instant.
 *
 * A third routing, `local-browser`, is the desktop app's (plan Phase 6,
 * Settings > Advanced > Browser rendering; off until the website's GA): the
 * page reads the project's own image and mask itself, as the website's
 * browser node reads a picked file -- in workers, by byte range, through the
 * shell's `plexora-file` scheme -- instead of asking the server for each
 * tile. The server offers it (`local` beside `routes`) only from the desktop
 * app with the setting on; it is chosen only when all of these hold:
 *
 *   - the answer's `local.setting` is "auto" (the setting is on);
 *   - this page is in the desktop app's window, with a shell that grants files
 *     (`PlexoraDesktop.grantLocalFiles` and `.localFiles`);
 *   - the image or the mask is served and in a format the page's reader takes
 *     (`LOCAL_FORMATS`: OME-TIFF, TIFF and OME-Zarr).
 *
 * It is kept apart from `routes` (`resolved.local`, `localRoute`), which are a
 * node's routes everywhere they are read; nothing is probed or stored for it.
 * `attachLocal` loads the browser node into the page and points it at the
 * shell; the viewer's tile scheduler then asks it first (views/tileFetch.js
 * `local`), and the server answers whatever it declines.
 */
window.PlexoraRouting = (function () {
    "use strict";

    //: Long enough for a real round trip through a tunnel, short enough that a
    //: node which is not there does not become the thing the user waits for.
    //: The proxy is behind this timeout and always works, so being wrong here
    //: costs a hop, never a failure.
    const PROBE_TIMEOUT_MS = 1500;

    const STORAGE_PREFIX = "plexora.routing.";

    //: datasource -> Promise of a resolved routing table. One per page, so the
    //: several things that ask (the viewer, the mini-map, the label layer) all
    //: wait on the same probe rather than starting their own.
    const pending = new Map();

    //: datasource -> the last fully-resolved table this page handed out: what
    //: the page is ACTUALLY using, kept for anything that compares it against
    //: the registry's current answer (the globe's stale check). Overwritten
    //: when a re-resolution lands, never cleared by `forget` -- while a fresh
    //: probe is in flight the old table is still the truthful account of what
    //: the tiles on screen were built from.
    const settled = new Map();

    //: The answer for a project with nothing on a node. Frozen and shared:
    //: every lookup against it returns "local", which is what every call site
    //: already assumed.
    const NOTHING_REMOTE = Object.freeze({ routes: Object.freeze({}) });

    //: The third routing's name (see the header).
    const LOCAL_BROWSER = "local-browser";
    //: The formats the page's own readers take (services/browserNode.js,
    //: node/tiffReader.js and node/zarrReader.js), as plexora/browser/
    //: node_spec.py names them. The slide formats are the server's until the
    //: desktop's page reads them.
    const LOCAL_FORMATS = Object.freeze(["ome-tiff", "tiff", "ome-zarr"]);

    /**
     * The `local-browser` routing for the server's `local` answer, or null:
     * the setting on, this page in the desktop app's window with a shell that
     * grants files, and the image or the mask served in a format the page
     * reads. Each of the three is a reason on its own to stay with the server.
     */
    function chooseLocal(local) {
        if (!local || typeof local !== "object" || local.setting !== "auto") return null;
        const desktop = window.PlexoraDesktop;
        if (!desktop || typeof desktop.grantLocalFiles !== "function" || !desktop.localFiles) return null;
        const readable = (part) => Boolean(part && part.served === true && typeof part.path === "string"
            && LOCAL_FORMATS.indexOf(part.format) >= 0);
        const image = readable(local);
        const labels = readable(local.labels);
        if (!image && !labels) return null;
        return Object.freeze({ mode: LOCAL_BROWSER, image: image, labels: labels, spec: local });
    }

    /** `resolved` with the `local-browser` routing beside its routes, when
     *  `local` is chosen; `resolved` itself otherwise. */
    function withLocal(resolved, local) {
        const chosen = chooseLocal(local);
        if (!chosen) return resolved;
        return Object.freeze({ routes: resolved.routes, local: chosen });
    }

    /** The `local-browser` routing of a resolved table, or null. */
    function localRoute(resolved) {
        return (resolved && resolved.local && resolved.local.mode === LOCAL_BROWSER) ? resolved.local : null;
    }

    function storageKey(datasource) {
        return STORAGE_PREFIX + datasource;
    }

    /** Remembered verdicts for this tab, or {} when storage is unavailable.
     *
     *  sessionStorage throws outright in some contexts (a browser set to block
     *  site data, a sandboxed frame), so every touch is guarded -- the feature
     *  degrades to probing once per page load, which is correct, just slower.
     */
    function remembered(datasource) {
        try {
            return JSON.parse(window.sessionStorage.getItem(storageKey(datasource)) || "{}");
        } catch (e) {
            return {};
        }
    }

    function remember(datasource, verdicts) {
        try {
            window.sessionStorage.setItem(storageKey(datasource), JSON.stringify(verdicts));
        } catch (e) {
            /* not worth a message: the only cost is probing again next page */
        }
    }

    /**
     * Whether this browser can reach one node, right now.
     *
     * Deliberately the node's OWN health endpoint rather than anything this
     * server proxies: what is being tested is the path that direct routing
     * would use, including CORS and the token, and testing anything else would
     * answer a different question.
     */
    function probe(route) {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
        return fetch(route.health, {
            signal: controller.signal,
            // Never send this app's cookie to another origin, and never accept
            // one back: the node authenticates with the token in the URL and
            // has no session of its own.
            credentials: "omit",
            cache: "no-store",
        })
            .then((response) => response.ok)
            .catch(() => false)
            .finally(() => window.clearTimeout(timer));
    }

    /**
     * Resolve every resource's route for one project.
     *
     * Resolves to `{ routes: { <kind>: {mode, base, query} } }` where `mode` is
     * "direct" or "proxy". A kind that is absent is local, and callers treat
     * absence and "proxy" identically -- both mean "ask this server", which is
     * the URL they would have built anyway. With `local` beside `routes` when
     * the desktop app's page reads the project's files itself
     * (`local-browser`, see the header and `localRoute`).
     */
    function load(datasource) {
        if (!datasource) return Promise.resolve(NOTHING_REMOTE);
        if (pending.has(datasource)) return pending.get(datasource);

        const promise = fetch(plexoraUrl("resource_routing")
                              + "?datasource=" + encodeURIComponent(datasource))
            .then((response) => (response.ok ? response.json() : { routes: {} }))
            .then((body) => Promise.resolve(decide(datasource, body.routes || {}))
                .then((resolved) => withLocal(resolved, body.local)))
            .catch(() => NOTHING_REMOTE)
            .then((resolved) => {
                settled.set(datasource, resolved);
                return resolved;
            });

        pending.set(datasource, promise);
        return promise;
    }

    function decide(datasource, candidates) {
        const kinds = Object.keys(candidates);
        if (!kinds.length) return NOTHING_REMOTE;

        const known = remembered(datasource);
        const verdicts = {};

        // One probe per NODE, not per resource: an image and a mask served by
        // the same machine are one question about one address, and probing
        // twice would double the wait for the commonest split there is.
        const byNode = new Map();
        kinds.forEach((kind) => {
            const route = candidates[kind];
            if (!byNode.has(route.node)) byNode.set(route.node, route);
        });

        const asked = Array.from(byNode.entries()).map(([node, route]) => {
            // Only a remembered YES is reused. A remembered "unreachable" was
            // true of a moment -- a node mid-restart, a tunnel not yet up --
            // and reusing it pinned the whole tab to the proxy hop for as long
            // as it stayed open, silently, after the node came back. Probing
            // again costs this one load at most PROBE_TIMEOUT_MS; being wrong
            // the other way cost every tile an extra hop for the afternoon.
            if (known[node] === true) {
                verdicts[node] = true;
                return Promise.resolve();
            }
            return probe(route).then((reachable) => { verdicts[node] = reachable; });
        });

        return Promise.all(asked).then(() => {
            remember(datasource, verdicts);
            const routes = {};
            kinds.forEach((kind) => {
                const route = candidates[kind];
                routes[kind] = {
                    mode: verdicts[route.node] ? "direct" : "proxy",
                    node: route.node,
                    base: route.tile_base,
                    appendKey: route.append_key !== false,
                    // A volume's node tiles also carry the project's z/t
                    // reading (a node has no project); getTileUrl adds the
                    // view itself. Absent for every planar image.
                    query: [route.query, route.volume_query].filter(Boolean).join("&"),
                };
            });
            return { routes: routes };
        });
    }

    /**
     * The tile base and query for one resource, from an already-loaded table.
     *
     * Returns null for anything this server should serve -- a local resource, a
     * node we could not reach, a project that was never asked about. Callers
     * read null as "carry on as before", which keeps the single-server path
     * free of any branch that could get this wrong.
     */
    function tileSource(resolved, kind) {
        const route = resolved && resolved.routes && resolved.routes[kind];
        if (!route || route.mode !== "direct") return null;
        return { base: route.base, appendKey: route.appendKey, query: route.query };
    }

    /** Which nodes this page decided it could not reach, for reporting. */
    function unreachable(resolved) {
        const routes = (resolved && resolved.routes) || {};
        const names = [];
        Object.keys(routes).forEach((kind) => {
            const node = routes[kind].node;
            if (routes[kind].mode !== "direct" && names.indexOf(node) === -1) {
                names.push(node);
            }
        });
        return names;
    }

    /** Drop this tab's remembered verdicts, so the next load probes again.
     *  For the case a user fixes a tunnel without reopening the browser. */
    function forget(datasource) {
        try {
            window.sessionStorage.removeItem(storageKey(datasource));
        } catch (e) { /* nothing to drop */ }
        pending.delete(datasource);
    }

    /**
     * The routing table this page is currently using, or null.
     *
     * Synchronous and never resolving anything: it answers "what were the
     * tiles on screen built from", which is a fact about the past. The globe
     * compares it against `/resource_routing`'s current answer to tell whether
     * the open project is still addressed to where a node was before it was
     * reconnected.
     */
    function held(datasource) {
        return settled.get(datasource) || null;
    }

    /**
     * Ask again from scratch: drop the memoised table AND the remembered
     * verdicts, then resolve anew. What a reconnect calls -- the node came
     * back on a new port with a new token, so both halves of what this tab
     * remembers are about an address that has gone.
     */
    function refresh(datasource) {
        forget(datasource);
        return load(datasource);
    }

    /**
     * Whether `origin` is a node this page decided to fetch from directly.
     * Read-only, for services/performanceTelemetry.js, which classifies a
     * tile request's source as `direct` by its origin alone and keeps
     * nothing else about it.
     */
    function isDirectOrigin(origin) {
        for (const resolved of settled.values()) {
            const routes = (resolved && resolved.routes) || {};
            for (const kind of Object.keys(routes)) {
                const route = routes[kind];
                if (route.mode !== "direct" || !route.base) continue;
                try {
                    if (new URL(route.base, window.location.href).origin === origin) return true;
                } catch (e) { /* not a URL: not this one */ }
            }
        }
        return false;
    }

    // -- local-browser: the desktop app's page reads the files itself ---------

    //: The browser node's script and its tag, loaded once into a page that
    //: chose `local-browser` -- nowhere else (the website's shell loads its own).
    const NODE_SCRIPT = "client/src/js/services/browserNode.js";
    const NODE_SCRIPT_TAG = "20261110_tile_rows";
    let nodeScript = null;

    function loadNode() {
        const ready = window.PlexoraNode;
        if (ready && typeof ready.decoded === "function") return Promise.resolve(ready);
        if (!nodeScript) {
            nodeScript = new Promise((resolve, reject) => {
                const tag = document.createElement("script");
                tag.src = plexoraUrl(NODE_SCRIPT) + "?v=" + NODE_SCRIPT_TAG;
                tag.async = true;
                tag.onload = () => (window.PlexoraNode && typeof window.PlexoraNode.decoded === "function"
                    ? resolve(window.PlexoraNode) : reject(new Error("the browser node did not load")));
                tag.onerror = () => reject(new Error("the browser node did not load"));
                document.head.appendChild(tag);
            }).catch((error) => {
                nodeScript = null;
                throw error;
            });
        }
        return nodeScript;
    }

    /**
     * Point the page's browser node at `datasource`'s own files (`route`, a
     * `local-browser` routing) and resolve the node, whose `decoded` the
     * viewer's tile scheduler then asks before the server (views/tileFetch.js
     * `local`). The node's spec is the server's `local` answer -- the one in
     * hand first, `/resource_routing` again when the node re-checks -- and its
     * files are the ones the shell grants for the project (the page names the
     * project, never a path). The grant and the files' opening start here,
     * before any tile is asked for. Rejects when the node cannot be loaded;
     * the caller then leaves every tile to the server.
     */
    async function attachLocal(datasource, route) {
        if (!route || route.mode !== LOCAL_BROWSER) throw new Error("not a local-browser routing");
        const node = await loadNode();
        const desktop = window.PlexoraDesktop;
        let first = route.spec;
        node.use({
            spec: async (name) => {
                if (first && name === datasource) {
                    const spec = first;
                    first = null;
                    return spec;
                }
                const response = await fetch(plexoraUrl("resource_routing")
                                             + "?datasource=" + encodeURIComponent(name));
                const body = response.ok ? await response.json() : {};
                if (!body.local || body.local.setting !== "auto") throw new Error("browser rendering is off");
                return body.local;
            },
            files: async (name) => {
                const granted = await desktop.grantLocalFiles(name);
                const byPath = new Map();
                (granted || []).forEach((file) => {
                    if (!file || !file.url || !file.path) return;
                    // An OME-Zarr store's folder: its files are read by key
                    // under the grant's URL (node/zarrReader.js `{base}`).
                    byPath.set(file.path, file.store ? { store: { base: file.url } }
                        : { url: file.url, size: file.size });
                });
                return (path) => byPath.get(path) || null;
            },
        });
        // Grant and open the files now, not at the first tile: the page asks
        // for its channels and metadata meanwhile (main.js attaches as soon
        // as the routing is known). `project` never rejects; a refusal is
        // Python's to answer, tile by tile.
        if (typeof node.project === "function") node.project(datasource);
        return node;
    }

    return { load, tileSource, unreachable, forget, held, refresh, isDirectOrigin,
             localRoute, attachLocal, LOCAL_BROWSER, LOCAL_FORMATS, PROBE_TIMEOUT_MS };
})();
