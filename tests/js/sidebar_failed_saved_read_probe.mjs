/**
 * A saved-state read that FAILED is not a sample with nothing saved.
 *
 * At boot `ViewerSidebar.init` reads the project's saved channel list and each
 * sidebar module's saved state (Thresholding: the saved gates) in parallel. A
 * sample with nothing saved answers `[]`, and init then writes its default
 * guess so the project has a starting point -- `persistChannelList()` and a
 * module's `persistIfNeeded(false)`.
 *
 * `DataLayer.getSavedChannelList` and `GatingApi.getSavedGatingList` swallow
 * their errors, so a read that failed -- a 502 HTML page from a proxy, a JSON
 * 500, a dropped tunnel -- resolves `undefined` (or an error object). init must
 * not take that for "nothing saved": writing the defaults then replaces the
 * sample's real saved channels and gates, and the gate save goes out with no
 * `base` (the failed read never set `GatingApi.revision`), so the server stores
 * the default list whole.
 *
 * The real ViewerSidebar, DataLayer, GatingApi and GatingSidebarController
 * methods, against a stubbed `fetch` that records every request. A request
 * the server would answer is what is asserted on, not a method call.
 *
 * Run directly:  node tests/js/sidebar_failed_saved_read_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora", "client", "src", "js");
const GATING = join(REPO, "plexora", "plugins", "gating", "static");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const settle = async () => {
    for (let i = 0; i < 50; i += 1) await Promise.resolve();
};

// -- the server, as fetch sees it --------------------------------------------

/** What each GET answers, set per scenario. A function of the route. */
let answer = () => ({ ok: true, status: 200, body: [] });
const requests = [];

function response({ ok, status, body, html, headers = {} }) {
    return {
        ok, status,
        headers: { get: (name) => headers[name] ?? null },
        async json() {
            if (html) throw new SyntaxError("Unexpected token '<', \"<html><hea\"... is not valid JSON");
            return JSON.parse(JSON.stringify(body));
        },
    };
}

async function fakeFetch(url, options = {}) {
    const path = String(url).split("?")[0].replace(/^\//, "");
    const method = (options.method || "GET").toUpperCase();
    requests.push({ path, method, body: options.body ? JSON.parse(options.body) : null });
    if (method === "POST") return response({ ok: true, status: 200, body: { success: true, revision: "r2" } });
    const spec = answer(path);
    if (spec === "network") throw new TypeError("Failed to fetch");
    return response(spec);
}

const posts = (path) => requests.filter((r) => r.method === "POST" && r.path === path);

// -- the page ------------------------------------------------------------------

const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    Math, Number, Object, Array, Set, Map, JSON, String, Boolean, Promise,
    URLSearchParams, parseFloat, parseInt,
    fetch: (...args) => fakeFetch(...args),
    plexoraUrl: (route) => `/${route}`,
    datasource: "sampleA",
    // main.js's globals persistChannelList reads.
    imageChannelsIdx: { 0: "DAPI", 1: "CD3" },
    imageChannels: { DAPI: 0, CD3: 1 },
    document: { getElementById: () => null },
    setTimeout: () => 0,
    clearTimeout: () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
createContext(sandbox);
runInContext(`${readFileSync(join(JS, "views", "viewerSidebar.js"), "utf8")}
    ;globalThis.ViewerSidebar = ViewerSidebar;`, sandbox, { filename: "viewerSidebar.js" });
runInContext(`${readFileSync(join(JS, "services", "dataLayer.js"), "utf8")}
    ;globalThis.DataLayer = DataLayer;`, sandbox, { filename: "dataLayer.js" });
runInContext(`${readFileSync(join(GATING, "gatingApi.js"), "utf8")}
    ;globalThis.GatingApi = GatingApi;`, sandbox, { filename: "gatingApi.js" });
runInContext(`${readFileSync(join(GATING, "gatingSidebarController.js"), "utf8")}
    ;globalThis.GatingSidebarController = GatingSidebarController;`, sandbox,
{ filename: "gatingSidebarController.js" });

/** Thresholding's controller: the real restore and save methods, its real API. */
function gatingModule() {
    const module = Object.create(sandbox.GatingSidebarController.prototype);
    Object.assign(module, {
        api: new sandbox.GatingApi({ url: (route) => `/${route}`, datasource: "sampleA" }),
        ctx: { dataset: { table: { sourceKind: "csv" } } },
        agentPreview: null,
        // What a page holds before anything is restored: every marker at its
        // full range.
        gatingList: { gating_channels: { CD3: [0, 4000], CD8: [0, 4000] }, selections: {} },
        getGateMarkerNames: () => ["CD3", "CD8"],
        setGateMarker(name) { this.gateMarker = name; },
        applySavedGating(rows) { this.restored = rows; },
        // Builds the panel's markup; nothing it does decides what is saved.
        setup() {},
        async reloadFromServer() {},
    });
    return module;
}

/** The project's sidebar: the real init, the real channel-list save. */
function sidebar() {
    const dataLayer = Object.create(sandbox.DataLayer.prototype);
    Object.assign(dataLayer, {
        imageBitRange: [0, 65536],
        getFullChannelName: (name) => name,
    });
    const view = Object.create(sandbox.ViewerSidebar.prototype);
    Object.assign(view, {
        persist: true, columns: ["DAPI", "CD3"], sidebarModules: [], channelSlots: [],
        markerRangeOverrides: new Map(), maxChannelSlots: 5, _restoring: false,
        dataLayer,
    });
    // DOM building and drawing, none of which decides what is saved.
    for (const name of ["setupSidebarShell", "bindActions", "ensureContrastBar", "bindChannelKeys",
        "initChannelSlots", "applyInitialChannels", "syncContrastBar", "startGatingContrast"]) {
        view[name] = () => {};
    }
    view.el = () => null;
    view.applySavedChannels = async () => {};
    return view;
}

async function boot(route) {
    requests.length = 0;
    answer = route;
    const view = sidebar();
    view.registerModule(gatingModule());
    await view.init({});
    await settle();
    return view;
}

const SAVED_ROWS = {
    get_saved_channel_list: [{ channel: "CD3", color: "ff0000" }],
    "plugins/gating/get_saved_gating_list": [{ channel: "CD3", gate_start: 812, gate_end: 4000, gate_active: true }],
};

// -- the harness says what it should first -------------------------------------

await boot(() => ({ ok: true, status: 200, body: [] }));
check("a sample with nothing saved gets the default channel list written",
      posts("save_channel_list").length === 1);
check("a sample with nothing saved gets the default gate list written",
      posts("plugins/gating/save_gating_list").length === 1);

await boot((path) => ({ ok: true, status: 200, body: SAVED_ROWS[path] ?? [],
                        headers: { "X-Gating-Revision": "r1" } }));
check("a sample with saved channels and gates is not written at boot",
      posts("save_channel_list").length === 0 && posts("plugins/gating/save_gating_list").length === 0);

// -- a read that failed ----------------------------------------------------------

const FAILURES = [
    ["a 502 page from the proxy", () => ({ ok: false, status: 502, html: true })],
    ["a JSON 500", () => ({ ok: false, status: 500, body: { code: "internal", message: "boom" } })],
    ["a dropped connection", () => "network"],
];
for (const [label, route] of FAILURES) {
    await boot(route);
    const channelPosts = posts("save_channel_list");
    const gatePosts = posts("plugins/gating/save_gating_list");
    check(`${label} on the saved channel list writes no channel list over it`,
          channelPosts.length === 0,
          channelPosts.length ? `(posted save_channel_list x${channelPosts.length})` : "");
    check(`${label} on the saved gates writes no gate list over them`,
          gatePosts.length === 0,
          gatePosts.length ? `(posted save_gating_list x${gatePosts.length}, base=${JSON.stringify(gatePosts[0].body.base)}, `
              + `channels=${JSON.stringify(gatePosts[0].body.channels)})` : "");
}

// -- Thresholding opened mid-session ----------------------------------------------

{
    const view = await boot((path) => ({ ok: true, status: 200, body: SAVED_ROWS[path] ?? [],
                                         headers: { "X-Gating-Revision": "r1" } }));
    view.sidebarModules.length = 0;
    requests.length = 0;
    answer = () => ({ ok: false, status: 502, html: true });
    await view.registerModuleLate(gatingModule());
    await settle();
    const gatePosts = posts("plugins/gating/save_gating_list");
    check("a tool opened mid-session whose saved read failed writes no gate list",
          gatePosts.length === 0,
          gatePosts.length ? `(posted save_gating_list x${gatePosts.length}, base=${JSON.stringify(gatePosts[0].body.base)})` : "");
}

if (failures.length) {
    console.log(`${failures.length} check(s) failed`);
    process.exit(1);
}
console.log("all checks passed");
