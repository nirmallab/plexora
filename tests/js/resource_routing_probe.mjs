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
function load({ routes = {}, probeOk = false, storage = {}, local = undefined,
                desktop = null, node = null } = {}) {
    const calls = { fetched: [], stored: [], granted: [], scripts: [] };
    const store = Object.assign({}, storage);

    const fakeWindow = {
        setTimeout: () => 0,
        clearTimeout: () => {},
        sessionStorage: {
            getItem: (key) => (key in store ? store[key] : null),
            setItem: (key, value) => { store[key] = value; calls.stored.push(key); },
            removeItem: (key) => { delete store[key]; },
        },
        // The desktop app's bridge (services/desktopBridge.js), or null as in
        // a browser tab -- including a tab of the desktop's own server.
        PlexoraDesktop: desktop,
        PlexoraNode: node,
    };
    if (desktop && typeof desktop.grantLocalFiles === "function") {
        const grant = desktop.grantLocalFiles;
        desktop.grantLocalFiles = (...args) => { calls.granted.push(args); return grant(...args); };
    }

    const context = createContext({
        window: fakeWindow,
        document: {
            head: { appendChild: (tag) => calls.scripts.push(tag.src) },
            createElement: () => ({}),
        },
        AbortController: class { constructor() { this.signal = null; } abort() {} },
        plexoraUrl: (p) => `/${p}`,
        fetch: (url) => {
            calls.fetched.push(url);
            if (url.startsWith("/resource_routing")) {
                const body = local === undefined ? { routes } : { routes, local };
                return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
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

// -- a remembered "unreachable" is retried; only a "yes" is kept ---------

{
    const first = load({ routes: { image: NODE_ROUTE }, probeOk: false });
    await first.routing.load("split");

    const second = load({
        routes: { image: NODE_ROUTE },
        probeOk: true,   // the node is back
        storage: first.store,
    });
    const resolved = await second.routing.load("split");
    assert.equal(resolved.routes.image.mode, "direct",
        "a remembered 'unreachable' was true of a moment -- a node "
        + "mid-restart, a tunnel not yet up -- and reusing it pinned the tab "
        + "to the proxy hop for as long as it stayed open");
    assert.equal(
        second.calls.fetched.filter((u) => u.includes("/node/v1/health")).length,
        1, "so it is probed again");
}

// -- what the page is actually using, and asking again from scratch ------

{
    const { routing } = load({ routes: { image: NODE_ROUTE }, probeOk: true });
    assert.equal(routing.held("split"), null,
        "nothing is held before anything resolved");

    const resolved = await routing.load("split");
    assert.equal(routing.held("split"), resolved,
        "held() answers with the very table the page was handed -- it is what "
        + "the globe compares the registry's current answer against");

    const again = await routing.refresh("split");
    assert.notEqual(again, resolved, "refresh() resolves anew");
    assert.equal(routing.held("split"), again, "and held() moves with it");
}

// -- a routing request that fails is not a broken viewer -----------------

{
    const { routing } = load({ routes: {} });
    const resolved = await routing.load("");
    assert.equal(Object.keys(resolved.routes).length, 0,
        "no datasource resolves to 'everything local' rather than throwing");
}

// -- local-browser: the desktop app's page reads the files itself --------
//
// The third routing (plan Phase 6, Settings > Advanced > Browser rendering).
// The guard worth having is the negative one again: without the setting the
// page must come out exactly as it always did -- one request, nothing probed,
// nothing stored, no `local` -- whatever else is true of the page.

/** What the desktop app's server answers with the setting on. */
const LOCAL_ON = Object.freeze({
    setting: "auto", served: true, name: "tonsil", path: "C:\\data\\tonsil.ome.tif",
    format: "ome-tiff", levels: [[3, 4096, 4096], [3, 2048, 2048]], dtype: "uint16",
    tile: [1024, 1024],
    labels: { served: true, path: "C:\\data\\mask.tif", format: "tiff",
              levels: [[0, 4096, 4096]], dtype: "uint32" },
});

/** A desktop bridge with a shell that grants files (desktopBridge.js). */
function desktopShell(granted = []) {
    return {
        localFiles: "http://plexora-file.localhost",
        grantLocalFiles: async () => granted,
    };
}

function assertPlain(resolved, calls, why) {
    assert.equal(Object.keys(resolved.routes).length, 0, why);
    assert.equal(resolved.local, undefined, `${why}: no local`);
    assert.equal(calls.fetched.length, 1, `${why}: the routing question only`);
    assert.equal(calls.stored.length, 0, `${why}: nothing stored`);
    assert.equal(calls.granted.length, 0, `${why}: nothing granted`);
}

{
    // The setting off: the server says nothing about it -- even to the app.
    const { routing, calls } = load({ routes: {}, desktop: desktopShell() });
    const resolved = await routing.load("tonsil");
    assertPlain(resolved, calls, "the setting off");
    assert.equal(routing.localRoute(resolved), null, "local-browser is not chosen");
}

{
    // A `local` that does not say the setting is on is not one (a server
    // that got it wrong must still not move the page's tiles).
    for (const setting of [undefined, "off", "on", true]) {
        const { routing, calls } = load({ routes: {}, desktop: desktopShell(),
                                          local: { ...LOCAL_ON, setting } });
        const resolved = await routing.load("tonsil");
        assertPlain(resolved, calls, `setting ${String(setting)}`);
        assert.equal(routing.localRoute(resolved), null);
    }
}

{
    // On, but not in the app's window: a browser tab of the desktop's own
    // server ("Open in Browser") has no shell to read the files through.
    const { routing, calls } = load({ routes: {}, local: LOCAL_ON, desktop: null });
    const resolved = await routing.load("tonsil");
    assertPlain(resolved, calls, "no desktop bridge");
    // ...nor in a window whose shell does not grant files (an older app).
    const old = load({ routes: {}, local: LOCAL_ON,
                       desktop: { localFiles: null, grantLocalFiles: async () => [] } });
    assertPlain(await old.routing.load("tonsil"), old.calls, "a shell without the scheme");
}

{
    // On, in the app, but nothing the page's reader takes: OME-Zarr, a
    // refused image and no mask.
    const zarr = load({ routes: {}, desktop: desktopShell(), local: {
        ...LOCAL_ON, format: "ome-zarr", path: "C:\\data\\tonsil.zarr",
        labels: { served: false, reason: "no mask" } } });
    assertPlain(await zarr.routing.load("tonsil"), zarr.calls, "an OME-Zarr image, no mask");
    const refused = load({ routes: {}, desktop: desktopShell(), local: {
        ...LOCAL_ON, served: false, reason: "not a TIFF file",
        labels: { served: false, reason: "no mask" } } });
    assertPlain(await refused.routing.load("tonsil"), refused.calls, "nothing served");
}

{
    // All three hold: local-browser is chosen, and it is all that changes --
    // still one request, nothing probed or stored, no node route, and the
    // tile addresses stay the server's (the scheduler asks the page first).
    const { routing, calls } = load({ routes: {}, local: LOCAL_ON, desktop: desktopShell() });
    const resolved = await routing.load("tonsil");
    const chosen = routing.localRoute(resolved);
    assert.equal(chosen.mode, "local-browser");
    assert.equal(chosen.mode, routing.LOCAL_BROWSER);
    assert.equal(chosen.image, true);
    assert.equal(chosen.labels, true);
    assert.equal(chosen.spec.path, LOCAL_ON.path);
    assert.equal(Object.keys(resolved.routes).length, 0, "local-browser is not a node route");
    assert.equal(routing.tileSource(resolved, "image"), null, "tile URLs stay the server's");
    assert.equal(routing.unreachable(resolved).length, 0, "and no node is unreachable");
    assert.equal(calls.fetched.length, 1);
    assert.equal(calls.stored.length, 0);
    assert.equal(calls.granted.length, 0, "loading the routing grants nothing by itself");

    // Only the mask readable: chosen for the mask alone.
    const maskOnly = load({ routes: {}, desktop: desktopShell(), local: {
        ...LOCAL_ON, served: false, reason: "a z-stack" } });
    const own = maskOnly.routing.localRoute(await maskOnly.routing.load("tonsil"));
    assert.equal(own.image, false);
    assert.equal(own.labels, true);
}

{
    // attachLocal points the page's browser node at the shell: its spec is
    // the server's `local` (the answer in hand first), and its files are what
    // the shell grants FOR THE PROJECT -- the page names a project, never a
    // path.
    const used = [];
    const opened = [];
    const node = { decoded: () => null, use: (source) => used.push(source),
                   project: (name) => opened.push([name, used.length]) };
    const granted = [
        { kind: "image", path: LOCAL_ON.path, url: "http://plexora-file.localhost/ab", size: 9 },
        { kind: "labels", path: LOCAL_ON.labels.path, url: null, size: null, error: "on the network" },
    ];
    const { routing, calls } = load({ routes: {}, local: LOCAL_ON, desktop: desktopShell(granted), node });
    const route = routing.localRoute(await routing.load("tonsil"));
    const attached = await routing.attachLocal("tonsil", route);
    assert.equal(attached, node, "the node already on the page is used, not loaded again");
    assert.equal(calls.scripts.length, 0);
    assert.equal(used.length, 1);
    assert.deepEqual(opened, [["tonsil", 1]],
                     "the project's files are granted and opened at once, after the source is set");
    const spec = await used[0].spec("tonsil");
    assert.equal(spec.path, LOCAL_ON.path, "the first check takes the answer in hand");
    const files = await used[0].files("tonsil", spec);
    assert.deepEqual(calls.granted, [["tonsil"]], "the shell is asked about the project by name");
    assert.equal(files(LOCAL_ON.path).url, "http://plexora-file.localhost/ab");
    assert.equal(files(LOCAL_ON.path).size, 9);
    assert.equal(files(LOCAL_ON.labels.path), null, "a file the shell refused is not read");
    assert.equal(files("C:\\Windows\\win.ini"), null, "nor anything else");
    await assert.rejects(routing.attachLocal("tonsil", null), /not a local-browser/);
}

console.log("resource routing probe OK");
