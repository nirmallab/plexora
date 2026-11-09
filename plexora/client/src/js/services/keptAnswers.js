/**
 * PlexoraKept -- on the website, Python's own answers to a project's first
 * screen, kept in the browser, so that a reload of the site shows the viewer
 * while Python starts again instead of after it.
 *
 * A reload of the site starts Python over: 5 s and more on a fast computer
 * before it can answer anything, and the viewer asked it for everything --
 * its page, /config, the channels and their windows -- so nothing was drawn
 * until then (the local server draws the same project again in 0.3 s). The
 * pixels never needed Python: the browser node reads them from the picked
 * file (services/browserNode.js). What the first screen needs from Python is
 * a short list of answers that are the same from one start to the next
 * (plexora/browser/routes.py `KEPT`), so Python's answers to those are kept as
 * it gives them, per site version and Python bundle, in Cache Storage:
 *
 * - every 200 Python gives to a kept GET route is kept (`observe`, from
 *   services/pythonClient.js), and a kept route's failure drops its copy;
 * - a write that succeeded asks the project's state routes again once the
 *   page is quiet, so what is kept follows what was saved (`STATEFUL`);
 * - on a reload of a project's viewer, the host shows the kept page at once
 *   (services/shell.js `early`) when everything it needs is here: the page,
 *   the browser node's spec, and the files it names, reopened without Python
 *   (PlexoraPicks.reopen). The page's kept GETs are answered from here
 *   (services/backend.js); anything else waits for Python, as before;
 * - a page given kept answers sends no write until each of those answers has
 *   been asked of Python again (`checked`): the same, and its writes go; any
 *   different, and they are refused -- a page drawn from an old answer could
 *   save the old value back over a newer one -- and the page is replaced (or,
 *   if it has been used, offers a reload). Nothing is answered from here once
 *   Python is up.
 *
 *   configure({stamp})       the site version and bundle the answers are for
 *   starting(started)        Python's start; kept answers are given until then
 *   waiting()                whether Python has yet to start
 *   observe(url, method, status, headers, bytes, asked)
 *                            every answer Python gave (pythonClient.js `send`);
 *                            `asked`: the request's own headers
 *   answer(request, page)    before Python is up: a kept answer, or null;
 *                            `page` is the window that asked
 *   cleared(page)            a write of `page`'s may be sent (resolves true),
 *                            or must be refused (false)
 *   checked(page)            `page`'s kept answers asked again once Python is
 *                            up: {checked, changed: [route...]}, or null
 *   early(route)             a project's viewer to show at once: {html, project, opens,
 *                            page} -- `opens` a tool to open once Python is up, the
 *                            page then the plain viewer at `page`
 *   spec(name, ask)          the browser node's spec for `name`, `ask()` being
 *                            Python's (services/browserNode.js)
 *   report()                 what was kept, given and found changed
 *
 * Classic script, `window.PlexoraKept`, in the site's host page; the app's
 * frame borrows it (services/shell.js frameBoot). Nothing of the desktop app,
 * a server, a notebook or a node loads it.
 */
(function (global) {
    "use strict";

    //: plexora/browser/routes.py `KEPT`, by endpoint (tests/test_kept_answers.py).
    const ENDPOINTS = Object.freeze([
        "image_viewer", "serve_config", "get_view_transform", "volume_info", "get_view_state_route",
        "resource_routing", "resource_status", "get_channel_names", "get_ome_metadata",
        "get_database_description", "init_database", "get_saved_channel_list",
        "get_image_channel_stats", "get_channel_gmm", "get_display_contrast",
    ]);
    const KEPT = new Set(ENDPOINTS);
    //: What a write can change: asked again after one (`recapture`).
    const STATEFUL = new Set(["image_viewer", "serve_config", "get_view_transform", "get_view_state_route",
                              "get_saved_channel_list", "get_display_contrast", "resource_routing",
                              "resource_status"]);
    //: Keys an answer carries that change with every start and say nothing
    //: about what the page draws: left out when a kept answer is compared.
    const VOLATILE = { serve_config: ["lastOpenedAt"] };
    //: Query keys that only defeat a cache (main.js asks `config?t=<now>`).
    const BUSTERS = new Set(["t", "_"]);
    const CACHE = "plexora-kept";
    //: Quiet this long after a write before its project's state is asked again.
    const RECAPTURE_QUIET_MS = 1500;

    let stamp = null;
    let up = false;
    let started = null;
    //: page window -> [{key, url, endpoint, bytes}] given to it from here.
    const given = new Map();
    //: page window -> promise of its check.
    const checks = new Map();
    //: project -> the spec given from here (JSON), for the check.
    const specsGiven = new Map();
    let specCheck = null;
    let lastProject = null;
    let lastActivity = 0;
    let recaptureTimer = null;
    const counts = { kept: 0, given: 0, dropped: 0, refused: 0 };
    const results = [];

    const base = () => String(global.PLEXORA_SITE_BASE || "");
    const open = () => (global.caches ? global.caches.open(CACHE) : Promise.reject(new Error("no Cache Storage")));
    //: Every write to the cache, one after another: a route's answer kept and
    //: then dropped (it failed next time) must end dropped, not kept.
    let writes = Promise.resolve();
    function write(work) {
        const next = writes.then(() => open()).then(work);
        writes = next.catch(() => null);
        return next;
    }
    const now = () => (global.performance ? global.performance.now() : Date.now());

    //: The route as the app names it (the site's base taken off), or null.
    function routeOf(pathname) {
        const prefix = base();
        if (!prefix) return pathname;
        if (pathname === prefix) return "/";
        return pathname.startsWith(prefix + "/") ? pathname.slice(prefix.length) : null;
    }

    function endpointOf(url, method = "GET") {
        const backend = global.PlexoraBackend;
        const row = backend && typeof backend.classify === "function" ? backend.classify(url, method) : null;
        return row && KEPT.has(row.endpoint) ? row.endpoint : null;
    }

    function decode(part) {
        try {
            return decodeURIComponent(part);
        } catch (error) {
            return null;
        }
    }

    //: Where a kept answer is: the route with its query sorted and without
    //: cache-busters (`normal`), the project it is about ("_" for none), and
    //: its key in Cache Storage.
    function locate(endpoint, href) {
        const url = new URL(String(href), global.location.href);
        const route = routeOf(url.pathname);
        if (route === null) return null;
        const query = [...url.searchParams.entries()].filter(([name]) => !BUSTERS.has(name))
            .sort((a, b) => (a[0] === b[0] ? (a[1] < b[1] ? -1 : 1) : (a[0] < b[0] ? -1 : 1)));
        const search = new URLSearchParams(query).toString();
        const normal = search ? `${route}?${search}` : route;
        let project = url.searchParams.get("datasource");
        if (!project) {
            const inPath = /^\/(?:view_transform|view_state)\/([^/]+)$/.exec(route)
                || /^\/volume\/([^/]+)\/info$/.exec(route)
                || (endpoint === "image_viewer" ? /^\/([^/]+)$/.exec(route) : null);
            project = inPath ? decode(inPath[1]) : null;
        }
        project = project || "_";
        const key = new URL(`${base()}/__plexora/kept/${encodeURIComponent(project)}/`
                            + `${encodeURIComponent(normal)}?v=${encodeURIComponent(stamp)}`,
                            global.location.origin).href;
        return { normal, project, key, endpoint };
    }

    const specKey = (project) => new URL(`${base()}/__plexora/kept/${encodeURIComponent(project)}/`
                                         + `__spec?v=${encodeURIComponent(stamp)}`, global.location.origin).href;

    //: The project a page Python rendered is for (its variables block's
    //: `datasource`), as services/shell.js reads it; "" for none.
    function projectOfPage(html) {
        const found = /<script type="application\/json" id="plexora-variables">([\s\S]*?)<\/script>/.exec(html);
        if (!found) return "";
        try {
            return JSON.parse(found[1]).datasource || "";
        } catch (error) {
            return "";
        }
    }

    function headerOf(headers, name) {
        const wanted = name.toLowerCase();
        for (const [key, value] of headers || []) if (String(key).toLowerCase() === wanted) return value;
        return null;
    }

    //: A routing answer that sends anything to a node is the node's to give.
    function namesANode(bytes) {
        try {
            const answer = JSON.parse(new TextDecoder().decode(bytes));
            if (!answer || typeof answer !== "object") return true;
            const routes = answer.routes;
            return Boolean((routes && typeof routes === "object" && Object.keys(routes).length)
                           || answer.local || answer.node || answer.token);
        } catch (error) {
            return true;
        }
    }

    function configure(options = {}) {
        const next = options.stamp ? String(options.stamp) : null;
        if (!next || next === stamp) return;
        stamp = next;
        // Every copy kept for another version goes: its answers may not be
        // this version's.
        const mine = stamp;
        write(async (cache) => {
            for (const request of await cache.keys()) {
                if (new URL(request.url).searchParams.get("v") !== mine) await cache.delete(request);
            }
        }).catch(() => null);
    }

    function starting(promise) {
        started = Promise.resolve(promise).then(() => {
            up = true;
            // Checked at once, not when the page first writes: its writes
            // wait for the check, and the page may be replaced by it.
            for (const page of given.keys()) checked(page);
        });
        started.catch(() => null);
        return started;
    }

    const waiting = () => Boolean(stamp) && !up;

    function noteActivity() {
        lastActivity = now();
    }

    //: Every answer Python gave. A kept route's 200 is kept (its failure
    //: drops what was kept for it); a write that went through has its
    //: project's state asked again soon.
    function observe(href, method, status, headers, bytes, asked = []) {
        noteActivity();
        if (!stamp) return;
        const verb = String(method || "GET").toUpperCase();
        if (verb === "GET") {
            const endpoint = endpointOf(href, "GET");
            if (!endpoint) return;
            const where = locate(endpoint, href);
            if (!where) return;
            if (status !== 200) {
                drop(where.key);
                return;
            }
            // A fragment of a page (services/appRouter.js asks for one with
            // this header, at the page's own address) is not the page.
            if (where.endpoint === "image_viewer" && headerOf(asked, "x-plexora-fragment")) return;
            keep(where, headers, bytes).catch(() => null);
            return;
        }
        if (verb !== "HEAD" && status < 400) recaptureSoon();
    }

    function drop(key) {
        write((cache) => cache.delete(key)).then((gone) => { if (gone) counts.dropped += 1; }).catch(() => null);
    }

    async function keep(where, headers, bytes) {
        if (!bytes) return;
        const type = headerOf(headers, "content-type") || "application/octet-stream";
        if (where.endpoint === "image_viewer") {
            // Only a project's viewer, as Python draws it for that project.
            if (!/text\/html/i.test(type)) return;
            const html = new TextDecoder().decode(bytes);
            if (!/^\s*<!doctype html/i.test(html)) return;
            if (!where.project || projectOfPage(html) !== where.project) return;
            lastProject = where.project;
        } else if (where.endpoint === "resource_routing" && namesANode(bytes)) {
            drop(where.key);
            return;
        } else if (where.project !== "_") {
            lastProject = where.project;
        }
        const copy = bytes.slice();
        await write((cache) => cache.put(where.key, new Response(copy, { status: 200,
                                                                         headers: { "Content-Type": type } })));
        counts.kept += 1;
    }

    //: Before Python is up, a kept GET's answer, recorded against `page`.
    async function answer(request, page) {
        if (!waiting()) return null;
        const verb = String(request.method || "GET").toUpperCase();
        if (verb !== "GET" && verb !== "HEAD") return null;
        const endpoint = endpointOf(request.url, "GET");
        // The page itself is the shell's to show (`early`), not a fetch's.
        if (!endpoint || endpoint === "image_viewer") return null;
        const where = locate(endpoint, request.url);
        if (!where) return null;
        const found = await (await open()).match(where.key).catch(() => null);
        if (!found) return null;
        const bytes = new Uint8Array(await found.arrayBuffer());
        if (!given.has(page)) given.set(page, []);
        const list = given.get(page);
        if (!list.some((entry) => entry.key === where.key)) {
            list.push({ key: where.key, url: where.normal, endpoint, bytes });
        }
        counts.given += 1;
        // Python came up while this was read: checked like the rest.
        if (up) checked(page);
        return new Response(verb === "HEAD" ? null : bytes, {
            status: 200,
            headers: { "Content-Type": found.headers.get("Content-Type") || "application/json",
                       "X-Plexora-Kept": "1" },
        });
    }

    //: Whether two answers of `endpoint` say the same.
    function same(endpoint, a, b) {
        if (a.length === b.length && a.every((byte, i) => byte === b[i])) return true;
        const keys = VOLATILE[endpoint];
        if (!keys) return false;
        const strip = (value) => {
            if (Array.isArray(value)) return value.map(strip);
            if (!value || typeof value !== "object") return value;
            const out = {};
            for (const key of Object.keys(value).sort()) if (!keys.includes(key)) out[key] = strip(value[key]);
            return out;
        };
        try {
            const text = (bytes) => JSON.stringify(strip(JSON.parse(new TextDecoder().decode(bytes))));
            return text(a) === text(b);
        } catch (error) {
            return false;
        }
    }

    //: Asks Python again, in the order they were given, for every answer
    //: `page` was given from here (and each spec the browser node was);
    //: each fresh answer is kept as it comes (`observe`).
    async function reconcile(page) {
        const list = given.get(page) || [];
        const changed = [];
        // By index: an answer given while this runs is checked too.
        for (let i = 0; i < list.length; i += 1) {
            const entry = list[i];
            let fresh = null;
            try {
                const response = await global.PlexoraPython.fetch(base() + entry.url);
                fresh = response.status === 200 ? new Uint8Array(await response.arrayBuffer()) : null;
            } catch (error) {
                fresh = null;
            }
            if (!fresh || !same(entry.endpoint, entry.bytes, fresh)) changed.push(entry.url);
        }
        changed.push(...await specsChecked());
        const result = { checked: list.length, changed };
        results.push(result);
        if (changed.length) console.warn("kept answers: changed since they were kept:", changed.join(", "));
        return result;
    }

    function checked(page) {
        if (!given.has(page) || !started) return Promise.resolve(null);
        if (!checks.has(page)) checks.set(page, started.then(() => reconcile(page)));
        return checks.get(page);
    }

    //: A write from `page` may go: none of its kept answers has changed.
    async function cleared(page) {
        if (!given.has(page)) return true;
        try {
            const result = await checked(page);
            const go = !result || result.changed.length === 0;
            if (!go) counts.refused += 1;
            return go;
        } catch (error) {
            counts.refused += 1;
            return false;
        }
    }

    // -- the browser node's spec (services/browserNode.js `source.spec`) ---------

    async function readSpec(project) {
        const found = await (await open()).match(specKey(project)).catch(() => null);
        if (!found) return null;
        try {
            return await found.json();
        } catch (error) {
            return null;
        }
    }

    function putSpec(project, value) {
        write((cache) => cache.put(specKey(project), new Response(JSON.stringify(value), {
            status: 200, headers: { "Content-Type": "application/json" } }))).catch(() => null);
    }

    //: The spec Python gives the browser node for `name` -- kept as it is
    //: given, and given from here while Python starts.
    async function spec(name, ask) {
        if (waiting()) {
            const kept = await readSpec(name);
            if (kept) {
                specsGiven.set(name, JSON.stringify(kept));
                return kept;
            }
        }
        const fresh = await ask();
        if (fresh && stamp) putSpec(name, fresh);
        return fresh;
    }

    //: Each spec given from here, asked of Python again (once); the node then
    //: checks every project again, and keeps open what is the same.
    function specsChecked() {
        if (!specCheck) {
            specCheck = (async () => {
                const changed = [];
                for (const [name, was] of specsGiven) {
                    let fresh = null;
                    try {
                        fresh = await global.PlexoraPython.nodeSpec(name);
                    } catch (error) {
                        fresh = null;
                    }
                    if (fresh) putSpec(name, fresh);
                    if (!fresh || JSON.stringify(fresh) !== was) changed.push(`spec:${name}`);
                }
                if (specsGiven.size && global.PlexoraNode) global.PlexoraNode.revalidate();
                return changed;
            })();
        }
        return specCheck;
    }

    // -- the page shown at once ------------------------------------------------------

    //: A file the browser node can read for `path` here and now.
    function holds(picks, path) {
        if (!path) return false;
        if (picks.fileFor(path)) return true;
        return Boolean(picks.filesUnder && picks.filesUnder(path).length);
    }

    //: A project's viewer, kept, whose image the page can draw now: its page,
    //: its spec, and the picked file the spec names, reopened without Python.
    async function early(route) {
        if (!stamp) return null;
        let address = null;
        try {
            address = new URL(String(route || ""), "http://kept.invalid");
        } catch (error) {
            return null;
        }
        const match = /^\/([^/]+)$/.exec(address.pathname);
        const project = match ? decode(match[1]) : null;
        if (!project) return null;
        // `?tool=` (services/toolLoader.js keeps it in step with the open
        // tool, so a reload keeps the tool) is the one question a project's
        // viewer is asked; any other and the page waits for Python, as before.
        if ([...address.searchParams.keys()].some((name) => name !== "tool")) return null;
        const tool = address.searchParams.get("tool") || "";
        const cache = await open();
        const lookup = async (href) => {
            const where = locate("image_viewer", href);
            return where ? cache.match(where.key).catch(() => null) : null;
        };
        const plain = `/${encodeURIComponent(project)}`;
        // The page as Python drew it for this address; a tool opened in the
        // page never was (its address was only written back), so then the
        // plain viewer, and the tool opened in it once Python is up.
        let found = tool ? await lookup(`${base()}${plain}${address.search}`) : null;
        let opens = "";
        if (!found) {
            found = await lookup(`${base()}${plain}`);
            opens = tool;
        }
        if (!found) return null;
        const html = await found.text();
        if (projectOfPage(html) !== project) return null;
        const kept = await readSpec(project);
        if (!kept || !kept.served || !kept.path) return null;
        const picks = global.PlexoraPicks;
        if (!picks || typeof picks.reopen !== "function") return null;
        // Only the picks the spec names (`/picked/<id>/...`): a folder picked
        // for something else is not walked before the page shows.
        const ids = [kept.path, kept.labels && kept.labels.served ? kept.labels.path : null]
            .map((path) => (/^\/picked\/([^/]+)\//.exec(path || "") || [])[1]).filter(Boolean);
        if (!ids.length) return null;
        await picks.reopen(ids).catch(() => null);
        if (!holds(picks, kept.path)) return null;
        lastProject = project;
        return { html, project, opens, page: opens ? plain : null };
    }

    // -- after a write ---------------------------------------------------------------

    function recaptureSoon() {
        if (!lastProject) return;
        global.clearTimeout(recaptureTimer);
        recaptureTimer = global.setTimeout(recapture, RECAPTURE_QUIET_MS);
    }

    //: The state routes kept for `project` (and for no project), as asked.
    async function keptFor(project) {
        const cache = await open();
        const prefixes = [project, "_"].map((name) => `${base()}/__plexora/kept/${encodeURIComponent(name)}/`);
        const found = [];
        for (const request of await cache.keys()) {
            const url = new URL(request.url);
            if (url.searchParams.get("v") !== stamp) continue;
            const prefix = prefixes.find((p) => url.pathname.startsWith(p));
            if (!prefix) continue;
            const normal = decode(url.pathname.slice(prefix.length));
            if (!normal || normal === "__spec") continue;
            const endpoint = endpointOf(base() + normal, "GET");
            if (endpoint && STATEFUL.has(endpoint)) found.push({ normal, endpoint });
        }
        return found;
    }

    async function recapture() {
        recaptureTimer = null;
        if (!up || !lastProject) return;
        if (now() - lastActivity < RECAPTURE_QUIET_MS) {
            recaptureTimer = global.setTimeout(recapture, RECAPTURE_QUIET_MS);
            return;
        }
        try {
            for (const { normal, endpoint } of await keptFor(lastProject)) {
                // Through pythonClient.js, which keeps the answer (`observe`).
                const init = endpoint === "image_viewer" ? { headers: { Accept: "text/html" } } : undefined;
                await global.PlexoraPython.fetch(base() + normal, init).catch(() => null);
            }
        } catch (error) { /* kept as it was: a later check finds the difference */ }
    }

    function report() {
        return {
            stamp, up, ...counts,
            pages: [...given.values()].map((list) => list.map((entry) => entry.url)),
            specs: [...specsGiven.keys()],
            results: results.slice(),
        };
    }

    global.PlexoraKept = Object.freeze({
        ENDPOINTS, configure, starting, waiting, observe, answer, cleared, checked, early, spec, report,
        //: For tests: the normalisation and comparison as answers are kept.
        locate, same,
    });
})(window);
