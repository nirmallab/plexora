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
     * the URL they would have built anyway.
     */
    function load(datasource) {
        if (!datasource) return Promise.resolve(NOTHING_REMOTE);
        if (pending.has(datasource)) return pending.get(datasource);

        const promise = fetch(plexoraUrl("resource_routing")
                              + "?datasource=" + encodeURIComponent(datasource))
            .then((response) => (response.ok ? response.json() : { routes: {} }))
            .then((body) => decide(datasource, body.routes || {}))
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
                    query: route.query,
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

    return { load, tileSource, unreachable, forget, held, refresh,
             PROBE_TIMEOUT_MS };
})();
