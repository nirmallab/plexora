/**
 * PlexoraRouting -- what it does, and what it must not do.
 *
 * The property worth guarding hardest is the negative one: a project whose
 * data is all on this server has to come out the far side having probed
 * nothing, stored nothing and changed nothing. That path is every existing
 * project, it runs before the viewer draws a pixel, and a 1.5-second timeout
 * accidentally introduced into it would be a regression nobody could see in a
 * test that only checked the interesting case.
 *
 * Run against the shipped source in a VM with a fake fetch and a fake
 * sessionStorage, so what is measured is the file the browser loads.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, "..", "..");
const source = await readFile(
    path.join(repoRoot, "plexora/client/src/js/services/resourceRouting.js"), "utf8");

/** A fresh module instance, with every outside call recorded.
 *
 *  `probeOk` stands for the browser's own network: whether a fetch of the
 *  node's health URL succeeds. Keyed on nothing, because that is the point --
 *  the module must not be able to tell which node it is asking about from
 *  anything other than the address it was given.
 */
function load({ routes = {}, probeOk = false, storage = {} } = {}) {
    const calls = { fetched: [], stored: [] };
    const store = Object.assign({}, storage);

    const fakeWindow = {
        setTimeout: () => 0,
        clearTimeout: () => {},
        sessionStorage: {
            getItem: (key) => (key in store ? store[key] : null),
            setItem: (key, value) => { store[key] = value; calls.stored.push(key); },
            removeItem: (key) => { delete store[key]; },
        },
    };

    const context = createContext({
        window: fakeWindow,
        AbortController: class { constructor() { this.signal = null; } abort() {} },
        plexoraUrl: (p) => `/${p}`,
        fetch: (url) => {
            calls.fetched.push(url);
            if (url.startsWith("/resource_routing")) {
                return Promise.resolve({ ok: true, json: () => Promise.resolve({ routes }) });
            }
            // A node health probe.
            return probeOk
                ? Promise.resolve({ ok: true })
                : Promise.reject(new Error("unreachable"));
        },
        Promise,
        JSON,
        Object,
        Map,
        Array,
        encodeURIComponent,
        console,
    });
    runInContext(source, context);
    return { routing: context.window.PlexoraRouting, calls, store };
}

const NODE_ROUTE = {
    node: "o2",
    resource_id: "slide",
    endpoint: "http://compute-3:8642",
    health: "http://compute-3:8642/node/v1/health?t=abc",
    query: "t=abc&tw=1024&th=1024",
    tile_base: "http://compute-3:8642/node/v1/image/slide/tile/",
    append_key: true,
};

// -- the ordinary project ------------------------------------------------

{
    const { routing, calls } = load({ routes: {} });
    const resolved = await routing.load("plain");

    // Object.keys rather than deepEqual: the object was made inside the VM,
    // so it has that realm's prototype and a strict deep-equal against a
    // literal here compares two different Objects.
    assert.equal(Object.keys(resolved.routes).length, 0, "nothing to route");
    assert.equal(calls.fetched.length, 1,
        "exactly one request -- the routing question itself, and no probe");
    assert.equal(calls.stored.length, 0,
        "nothing is remembered about a project with nothing on a node");
    assert.equal(routing.tileSource(resolved, "image"), null,
        "a local resource resolves to null, which callers read as 'carry on'");
}

// -- a node this browser can reach ---------------------------------------

{
    const { routing, calls } = load({
        routes: { image: NODE_ROUTE },
        probeOk: true,
    });
    const resolved = await routing.load("split");

    assert.equal(resolved.routes.image.mode, "direct");
    const source_ = routing.tileSource(resolved, "image");
    assert.equal(source_.base, NODE_ROUTE.tile_base);
    assert.equal(source_.query, NODE_ROUTE.query);
    assert.equal(source_.appendKey, true);
    assert.ok(calls.fetched.some((u) => u.includes("/node/v1/health")),
        "the probe asks the node itself, not something this server proxies");
    assert.equal(routing.unreachable(resolved).length, 0);
}

// -- a node this browser cannot reach ------------------------------------

{
    const { routing } = load({
        routes: { image: NODE_ROUTE },
        probeOk: false,
    });
    const resolved = await routing.load("split");

    assert.equal(resolved.routes.image.mode, "proxy");
    assert.equal(routing.tileSource(resolved, "image"), null,
        "a proxied resource resolves to null, exactly like a local one -- the "
        + "caller builds the URL it would have built anyway");
    assert.equal(routing.unreachable(resolved).join(","), "o2");
}

// -- one probe per machine, not per resource -----------------------------

{
    const seg = Object.assign({}, NODE_ROUTE, {
        resource_id: "mask", append_key: false,
        tile_base: "http://compute-3:8642/node/v1/seg/mask/tile/",
    });
    const { routing, calls } = load({
        routes: { image: NODE_ROUTE, segmentation: seg },
        probeOk: true,
    });
    const resolved = await routing.load("split");

    const probes = calls.fetched.filter((u) => u.includes("/node/v1/health"));
    assert.equal(probes.length, 1,
        "an image and a mask on one machine are one question about one address");
    assert.equal(resolved.routes.segmentation.mode, "direct");
    assert.equal(routing.tileSource(resolved, "segmentation").appendKey, false,
        "a mask has one plane and names no channel in its path");
}

// -- the verdict is remembered for the tab -------------------------------

{
    const { routing, calls, store } = load({
        routes: { image: NODE_ROUTE },
        probeOk: true,
    });
    await routing.load("split");
    assert.equal(Object.keys(store).length, 1, "one entry, keyed by project");

    const second = load({
        routes: { image: NODE_ROUTE },
        probeOk: false,   // would say proxy if it were asked again
        storage: store,
    });
    const resolved = await second.routing.load("split");
    assert.equal(resolved.routes.image.mode, "direct",
        "the remembered verdict is used rather than re-probed");
    assert.equal(
        second.calls.fetched.filter((u) => u.includes("/node/v1/health")).length, 0,
        "and no probe is issued at all");
}

// -- a routing request that fails is not a broken viewer -----------------

{
    const { routing } = load({ routes: {} });
    const resolved = await routing.load("");
    assert.equal(Object.keys(resolved.routes).length, 0,
        "no datasource resolves to 'everything local' rather than throwing");
}

console.log("resource routing probe OK");
