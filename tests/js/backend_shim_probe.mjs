/**
 * PlexoraBackend in server mode (services/backend.js): a pass-through.
 *
 * Every page a Plexora serves loads the file right before appStatus.js, and in
 * that mode nothing about the page may change because it did. Pinned:
 *
 * - `kind` and `target` are "server" unless the page says otherwise
 *   (`flaskVariables.backend`), and a server page has no route table, whatever
 *   was loaded;
 * - `fetch` is `window.fetch`: the same arguments, the same `this`, the very
 *   promise it returned (rejected ones too), and read at call time -- so the
 *   wrapper appStatus.js installs after this file is in the path -- while
 *   `window.fetch` itself is left alone;
 * - `supports` says yes to what the server can do and no to anything else,
 *   prototype names included; `verify` resolves `{ok: true, skipped:
 *   "server"}`;
 * - the object cannot be changed by whoever holds it;
 * - the website's kind ("browser") answers by the route table the app
 *   generates (backendRoutes.js): a file goes to the network, a route to the
 *   page's Python worker, what a web page cannot do to a sentence (501), and
 *   a request to another site to the network as it is;
 * - a kind with no backend in this build still sends everything to the
 *   network.
 *
 * Run directly:  node tests/js/backend_shim_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/backend.js"), "utf8");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

/** A page: backend.js run in a window of its own, with a fetch that records. */
function page(globals = {}) {
    const calls = [];
    const box = { console, Promise, Object, ...globals };
    box.window = box;
    box.globalThis = box;
    box.fetch = function fetch(...args) {
        calls.push({ self: this, args });
        return box.nextAnswer || Promise.resolve({ ok: true, status: 200 });
    };
    const original = box.fetch;
    const context = createContext(box);
    runInContext(SOURCE, context);
    // The page's own `window`, as its scripts see it (vm hands them the
    // context's global for `box`).
    const self = runInContext("this", context);
    return { box, self, calls, original, backend: box.PlexoraBackend };
}

/** Field by field: objects made inside the page have its own prototypes. */
function sameFields(a, b) {
    return JSON.stringify(Object.entries(a || {}).sort()) === JSON.stringify(Object.entries(b).sort());
}

// -- kind and target ------------------------------------------------------------------

{
    const bare = page().backend;
    check("with nothing said, the kind is server and so is the target",
          bare.kind === "server" && bare.target === "server" && bare.routes === null,
          JSON.stringify({ kind: bare.kind, target: bare.target }));
    for (const variables of [{}, { backend: "" }, { backend: null }, { backend: 7 },
                             { backend: "server" }]) {
        const { backend } = page({ flaskVariables: variables });
        check(`flaskVariables ${JSON.stringify(variables)} is a server page`,
              backend.kind === "server" && backend.target === "server");
    }
    const table = { "/generated/data/<datasource>/<channel>/<level>/<tile>": "js" };
    const { backend } = page({ flaskVariables: { backend: "server" }, PlexoraBackendRoutes: table });
    check("a server page has no route table, whatever was loaded before it", backend.routes === null);
}

// -- fetch is window.fetch ---------------------------------------------------------------

{
    const { box, self, calls, original, backend } = page({ flaskVariables: { datasource: "demo" } });
    check("loading it leaves window.fetch as it was", box.fetch === original);

    const answer = Promise.resolve({ ok: true, status: 200 });
    box.nextAnswer = answer;
    const input = "/generated/data/demo/demo_0/3/4_5.png";
    const init = { credentials: "same-origin", priority: "high" };
    const returned = backend.fetch(input, init);
    check("fetch returns the very promise window.fetch returned", returned === answer);
    check("...called once, with the page's window as `this`",
          calls.length === 1 && calls[0].self === self);
    check("...and the same arguments, by identity",
          calls[0].args.length === 2 && calls[0].args[0] === input && calls[0].args[1] === init);

    const request = { url: "/health", method: "GET" };
    backend.fetch(request);
    check("one argument stays one argument", calls[1].args.length === 1 && calls[1].args[0] === request);
    backend.fetch(input, init, "extra");
    check("...and three stay three", calls[2].args.length === 3 && calls[2].args[2] === "extra");

    const refused = Promise.reject(new TypeError("Failed to fetch"));
    refused.catch(() => {});
    box.nextAnswer = refused;
    check("a refused fetch comes back as the same rejected promise", backend.fetch(input) === refused);
    box.nextAnswer = null;

    // appStatus.js wraps window.fetch AFTER this file loads (base.html).
    const wrapped = [];
    box.fetch = function plexoraFetch(...args) {
        wrapped.push(args);
        return original.apply(this, args);
    };
    backend.fetch(input, init);
    check("window.fetch is read when called: a wrapper installed later is in the path",
          wrapped.length === 1 && wrapped[0][0] === input && calls.at(-1).args[1] === init);
}

// -- supports and verify ---------------------------------------------------------------

{
    const { backend } = page();
    for (const feature of ["ai", "remote", "segment", "volume", "surfaces", "agent"]) {
        check(`the server supports "${feature}"`, backend.supports(feature) === true);
    }
    for (const feature of ["file_picker", "teleport", "", undefined, null, 1, "toString",
                           "constructor", "__proto__", "hasOwnProperty", "AI"]) {
        check(`...and not ${JSON.stringify(feature) ?? "undefined"}`, backend.supports(feature) === false);
    }
    const verdicts = await Promise.all(["/generated/data/demo/demo_0/3/4_5.png", "", undefined]
        .map((path) => backend.verify(path)));
    check("verify has nothing to check: {ok: true, skipped: \"server\"}",
          verdicts.every((verdict) => sameFields(verdict, { ok: true, skipped: "server" })),
          JSON.stringify(verdicts));
    check("...as a promise", backend.verify("/x") instanceof Promise
          || typeof backend.verify("/x")?.then === "function");
}

// -- nobody can change it -----------------------------------------------------------------

{
    const { backend } = page();
    let threw = false;
    try {
        backend.kind = "browser";
    } catch (error) {
        threw = error instanceof TypeError;
    }
    check("PlexoraBackend is frozen: its kind cannot be rewritten",
          Object.isFrozen(backend) && backend.kind === "server" && threw);
}

// -- the website ---------------------------------------------------------------------------

{
    // The table as generated (plexora/browser/routes.py), read by the page.
    const TABLE_SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/backendRoutes.js"), "utf8");
    const holder = {};
    runInContext(TABLE_SOURCE, createContext(holder));
    const table = holder.PlexoraBackendRoutes;
    const asked = [];
    const python = { fetch: async (request) => { asked.push(request.url); return new Response("py"); } };
    const location = { href: "https://viewer.plexoraapp.com/demo", origin: "https://viewer.plexoraapp.com" };
    const { box, calls, backend } = page({
        flaskVariables: { backend: "browser" }, PlexoraBackendRoutes: table, PlexoraPython: python,
        location, URL, Request, Response, Headers, TextEncoder,
    });
    box.nextAnswer = Promise.resolve(new Response("file"));
    check("the website is the browser kind, with the generated table",
          backend.kind === "browser" && backend.target === "worker" && backend.routes === table
          && Array.isArray(table) && table.length > 300);
    check("...where a tile is the page's own route and a script a file",
          backend.classify("/generated/data/demo/DAPI/0/3_4.png").class === "js"
          && backend.classify("/client/src/js/main.js").class === "network");
    check("...and /config wins over a project's page",
          backend.classify("/config").endpoint === "serve_config"
          && backend.classify("/demo").endpoint === "image_viewer");
    const routed = await backend.fetch("/projects");
    check("...a route is answered by the page's Python",
          (await routed.text()) === "py" && asked.length === 1 && calls.length === 0);
    const file = await backend.fetch("/client/src/js/main.js");
    check("...a file is fetched as it is", (await file.text()) === "file" && calls.length === 1);
    const elsewhere = await backend.fetch("https://example.org/data.json");
    check("...as is a request to another site", calls.length === 2 && elsewhere instanceof Response);
    const refused = await backend.fetch("/shutdown", { method: "POST" });
    const said = await refused.json();
    check("...and what a web page cannot do says so, with what to do instead",
          refused.status === 501 && said.success === false && typeof said.handoff.why === "string"
          && Array.isArray(said.handoff.actions));
    check("...with the features the website has", backend.supports("volume") === true
          && backend.supports("remote") === false);
}

// -- a kind with no backend here -------------------------------------------------------------

{
    const { box, calls, backend } = page({ flaskVariables: { backend: "elsewhere" } });
    check("a kind this build has no backend for is that kind", backend.kind === "elsewhere"
          && backend.target === "network");
    check("...and none of the server's features", backend.supports("ai") === false);
    const answer = Promise.resolve({ ok: true });
    box.nextAnswer = answer;
    check("...and still sends a request to the network as it is",
          backend.fetch("/generated/data/demo/demo_0/3/4_5.png") === answer && calls.length === 1);
    const verdict = await backend.verify("/generated/data/demo/demo_0/3/4_5.png");
    check("...and does not claim to have checked anything", verdict.ok === false, JSON.stringify(verdict));
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nbackend shim probe OK: all ${checks} checks passed`);
