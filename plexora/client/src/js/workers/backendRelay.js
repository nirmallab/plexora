// The website's service worker: a relay, and nothing else.
//
// Every same-origin request a page of the site makes -- fetch, XMLHttpRequest
// (OpenSeadragon's tiles), an <img> (a project's thumbnail), a stylesheet's
// url() -- reaches this worker first. A request for a file of the site, or one
// no route of the app names, goes to the network as it is. A request for a
// route (backendRoutes.js: "python", "js", "handoff") is handed to the page
// that made it, whose services/backend.js answers it from Plexora's Python in
// that page's worker, and the answer comes back here as the response.
//
// Stateless on purpose: it caches nothing and remembers nothing, so it can be
// stopped at any moment, it never serves an old version of anything (the
// site's files go to the network and the HTTP cache), and the site's kill
// switch is never behind it. Navigations are never touched: the site serves
// its loader for every path.
importScripts("../services/backendRoutes.js");

const ROUTES = (self.PlexoraBackendRoutes || []).map((row) => ({ ...row, regex: new RegExp(row.pattern) }));
//: How long a page may stay silent before the request fails. A page still at
//: work on an answer says so every 30 s (services/backend.js), so a long one
//: is waited for and only a page that has gone (closed, crashed) times out.
//: (Chrome itself ends a fetch a service worker holds past 5 minutes: the
//: import, which can wait that long on a pyramid, does not come through here.)
const TIMEOUT_MS = 120000;
//: The site's own files, whatever a route pattern would say of them
//: (`/<datasource>` matches any single segment): the client, its Python.
const FILES = ["/client/", "/py/", "/tests/"];
//: The site's base path: "" at a site's root, the app's root when a node
//: serves it behind a portal (services/shell.js `BASE`): this worker's scope.
const BASE = new URL(self.registration.scope).pathname.replace(/\/+$/, "");

//: A path of the site as the app's route table names it: the base taken
//: off; null for a path outside the site (beside it on a portal: the node's
//: own `/node/v1/...`), which is the network's.
function routeOf(pathname) {
    if (!BASE) return pathname;
    if (pathname === BASE || pathname.startsWith(BASE + "/")) return pathname.slice(BASE.length) || "/";
    return null;
}

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
// A page of the site this worker does not control asks to be (shell.js
// `claimed`): a navigation the browser did not hand to the worker (a forced
// reload; Playwright's Firefox after the first) still needs its route
// requests relayed.
self.addEventListener("message", (event) => {
    if (event.data === "plexora-claim") event.waitUntil(self.clients.claim());
});

function classify(url, method) {
    for (const row of ROUTES) {
        if ((row.methods.includes(method) || (method === "HEAD" && row.methods.includes("GET")))
            && row.regex.test(url.pathname)) {
            return row;
        }
    }
    return null;
}

async function pageFor(event) {
    const own = event.clientId ? await self.clients.get(event.clientId) : null;
    if (own && own.type === "window") return own;
    // A worker's request (a decoder fetching tiles) is answered by a window.
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: false });
    return windows[0] || null;
}

async function relay(event) {
    const request = event.request;
    const page = await pageFor(event);
    if (!page) return fetch(request);
    const body = ["GET", "HEAD"].includes(request.method) ? null : await request.arrayBuffer();
    const channel = new MessageChannel();
    const answered = new Promise((resolve, reject) => {
        let timer = null;
        const arm = () => {
            clearTimeout(timer);
            timer = setTimeout(() => reject(new Error("the page did not answer")), TIMEOUT_MS);
        };
        arm();
        channel.port1.onmessage = ({ data }) => {
            if (data && data.working) { arm(); return; }
            clearTimeout(timer);
            resolve(data);
        };
    });
    page.postMessage({ type: "plexora-relay", url: request.url, method: request.method,
                       headers: [...request.headers.entries()], body },
                     [channel.port2, ...(body ? [body] : [])]);
    try {
        const answer = await answered;
        const empty = answer.status === 204 || answer.status === 304 || request.method === "HEAD";
        return new Response(empty ? null : answer.body, { status: answer.status, headers: answer.headers });
    } catch (error) {
        return new Response(String(error.message), { status: 504, headers: { "Content-Type": "text/plain" } });
    }
}

//: Answered here: whether this worker sees the asking page's requests
//: (services/shell.js `relayed`; a page it does not see answers them itself,
//: services/pageRelay.js).
const PROBE = "/__plexora/relayed";

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.mode === "navigate") return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    const route = routeOf(url.pathname);
    if (route === PROBE) {
        event.respondWith(new Response(null, { status: 204, headers: { "X-Plexora-Relayed": "1" } }));
        return;
    }
    if (route === null || FILES.some((prefix) => route.startsWith(prefix))) return;
    const row = classify({ pathname: route }, request.method);
    // A page fetched rather than navigated to (the app's router asks for a
    // fragment) is rendered by the page's Python like any route.
    if (!row || row.class === "network") return;
    event.respondWith(relay(event));
});
