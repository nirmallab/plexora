/**
 * The pages the website shows before its Python has started (services/shell.js
 * `pageStore`, `timeless`), run in node against the shipped file: the two are
 * taken out of shell.js's closure, with a Cache Storage stand-in.
 *
 * - only the home page is kept or handed back; any other route, a query, an
 *   empty answer: nothing;
 * - a kept page comes back as it was put, under the site version, the Python
 *   bundle and the base path, and a new version's page drops the old one;
 * - no Cache Storage, or one that throws: nothing kept, nothing thrown;
 * - two answers differing only in a `<time>`'s words ("3 hours ago" a day
 *   later) are the same page; any other difference is not;
 * - a first visit at the site's root, with nothing kept, shows the home page
 *   the site was built with (`builtHome`, shell.json `home`); never under a
 *   path, never for another page, nothing when it is missing.
 *
 * Run directly:  node tests/js/shell_pages_probe.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/shell.js"), "utf8");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

/** `function name(...) {...}` or `const name = ...;` as written in shell.js. */
function lift(name) {
    let fn = SOURCE.indexOf(`function ${name}(`);
    if (fn >= 0) {
        if (SOURCE.slice(Math.max(0, fn - 6), fn) === "async ") fn -= 6;
        let depth = 0;
        let end = SOURCE.indexOf("{", fn);
        for (; end < SOURCE.length; end += 1) {
            if (SOURCE[end] === "{") depth += 1;
            if (SOURCE[end] === "}" && --depth === 0) break;
        }
        return SOURCE.slice(fn, end + 1);
    }
    const found = new RegExp(`const ${name} = [\\s\\S]*?;\\n`).exec(SOURCE);
    if (!found) throw new Error(`shell.js has no ${name}`);
    return found[0];
}

/** Cache Storage as much as pageStore uses it, in memory. */
function cacheStorage() {
    const stores = new Map();
    return {
        stores,
        async open(name) {
            if (!stores.has(name)) stores.set(name, new Map());
            const entries = stores.get(name);
            return {
                async match(url) { return entries.has(url) ? new Response(entries.get(url)) : undefined; },
                async put(url, response) { entries.set(String(url), await response.text()); },
                async keys() { return [...entries.keys()].map((url) => ({ url })); },
                async delete(request) { return entries.delete(request.url); },
            };
        },
    };
}

const make = new Function("global", "at", `
    ${lift("PAGE_CACHE")}
    ${lift("EARLY_ROUTES")}
    ${lift("pageStore")}
    ${lift("timeless")}
    return { pageStore, timeless, EARLY_ROUTES };
`);

function site({ caches, base = "" } = {}) {
    const global = { caches, location: { origin: "https://viewer.plexoraapp.com" } };
    return make(global, (path) => base + path);
}

const HOME = "<!doctype html><title>Plexora</title><body>Recent <time datetime=\"2026-11-07T10:00\">3 hours ago</time></body>";

// -- what is kept ---------------------------------------------------------------------------

{
    const caches = cacheStorage();
    const { pageStore, EARLY_ROUTES } = site({ caches });
    check("the home page is the only page shown before Python", EARLY_ROUTES.size === 1 && EARLY_ROUTES.has("/"));
    const pages = pageStore({ version: "0.0.36" }, "https://assets.plexoraapp.com/py/dev/abc/");
    check("nothing is kept at first", (await pages.get("/")) === null);
    await pages.put("/", HOME);
    check("the home page comes back as Python drew it", (await pages.get("/")) === HOME);
    for (const route of ["/settings", "/demo", "/?launch=x", "/open_project"]) {
        await pages.put(route, "<p>other</p>");
        check(`${route} is neither kept nor handed back`, (await pages.get(route)) === null);
    }
    await pages.put("/", "");
    check("an empty answer does not replace a kept page", (await pages.get("/")) === HOME);
    const keys = [...caches.stores.get("plexora-pages").keys()];
    check("one page kept, under the version and the bundle", keys.length === 1
          && keys[0].includes("0.0.36") && keys[0].includes(encodeURIComponent("py/dev/abc")), JSON.stringify(keys));

    const next = pageStore({ version: "0.0.37" }, "https://assets.plexoraapp.com/py/dev/abc/");
    check("a new site version shows nothing kept by the old one", (await next.get("/")) === null);
    await next.put("/", "<p>new</p>");
    check("...and keeping its own drops the old one", caches.stores.get("plexora-pages").size === 1
          && (await next.get("/")) === "<p>new</p>" && (await pages.get("/")) === null);
    const rebuilt = pageStore({ version: "0.0.37" }, "https://assets.plexoraapp.com/py/dev/def/");
    check("so does a new Python bundle under the same version", (await rebuilt.get("/")) === null);
}

{
    const caches = cacheStorage();
    const root = site({ caches }).pageStore({ version: "1" }, "py/");
    const portal = site({ caches, base: "/rnode/node1/8000/app" }).pageStore({ version: "1" }, "py/");
    await root.put("/", "<p>root</p>");
    await portal.put("/", "<p>portal</p>");
    check("a site under a path (a node behind a portal) keeps its own home page",
          (await root.get("/")) === "<p>root</p>" && (await portal.get("/")) === "<p>portal</p>");
}

// -- no Cache Storage -------------------------------------------------------------------------

{
    const none = site({ caches: undefined }).pageStore({ version: "1" }, "py/");
    let threw = false;
    try {
        await none.put("/", HOME);
        check("without Cache Storage nothing is kept", (await none.get("/")) === null);
    } catch (error) {
        threw = true;
    }
    check("...and nothing is thrown", threw === false);
    const broken = site({ caches: { open: async () => { throw new Error("SecurityError"); } } })
        .pageStore({ version: "1" }, "py/");
    await broken.put("/", HOME);
    check("a Cache Storage that refuses is the same as none", (await broken.get("/")) === null);
}

// -- the same page ----------------------------------------------------------------------------

{
    const { timeless } = site({ caches: cacheStorage() });
    const later = HOME.replace("3 hours ago", "1 day ago");
    check("a page whose times have moved on is the same page", timeless(later) === timeless(HOME) && later !== HOME);
    check("...but not one with another row", timeless(HOME.replace("Recent", "Recent <a>B</a>")) !== timeless(HOME));
    check("...nor one opened at another time", timeless(HOME.replace("10:00", "11:00")) !== timeless(HOME));
}

// -- a first visit: the home page the site was built with -----------------------------------

{
    const liftBuilt = new Function("global", "at", "BASE", `${lift("builtHome")}\nreturn builtHome;`);
    const asked = [];
    const global = { fetch: async (url, init) => { asked.push([url, init && init.cache]);
                                                   return new Response("<p>built</p>", { status: 200 }); } };
    const builtHome = liftBuilt(global, (path) => path, "");
    const shell = { home: "client/home.html" };
    check("a first visit at the site's root shows the page the site was built with",
          (await builtHome(shell, "/")) === "<p>built</p>" && asked[0][0] === "/client/home.html"
          && asked[0][1] === "no-cache", JSON.stringify(asked));
    check("...only for the home page", (await builtHome(shell, "/settings")) === null && asked.length === 1);
    check("...and only when the build has one", (await builtHome({}, "/")) === null && asked.length === 1);
    const underPath = liftBuilt(global, (path) => `/app${path}`, "/app");
    check("a site under a path (a node's) never shows it: it was drawn for the root",
          (await underPath(shell, "/")) === null && asked.length === 1);
    const missing = liftBuilt({ fetch: async () => new Response("", { status: 404 }) }, (path) => path, "");
    const offline = liftBuilt({ fetch: async () => { throw new TypeError("Failed to fetch"); } }, (path) => path, "");
    check("a build whose page is missing, or a fetch that fails, shows nothing early",
          (await missing(shell, "/")) === null && (await offline(shell, "/")) === null);
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nshell pages probe OK: all ${checks} checks passed`);
