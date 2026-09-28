/**
 * The browser half of licensing, run in node against the shipped scripts.
 *
 *   - toolLoader.js: a `locked` panel answer opens the Paid modal and mounts
 *     NOTHING; a quiet (restore) load reports it and opens nothing.
 *   - paidFeature.js: the modal's words and choices, trial and licence
 *     routes, the page hint, and that loading it does nothing at all.
 *
 * Run directly:  node tests/js/license_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(REPO, path), "utf8");

async function check(label, fn) {
    await fn();
    console.log("  ok  " + label);
}

// -- paidFeature.js -------------------------------------------------------------

function paidSandbox({ status = {}, answer = null, flask = {} } = {}) {
    const fetched = [];
    const opened = [];
    const dialogs = [];
    const storage = [];
    const ctx = {
        console,
        Promise, Object, Array, JSON, String, Date, Error,
        setTimeout, clearTimeout,
        plexoraUrl: (path) => `/base/${path}`,
        fetch: async (url) => {
            fetched.push(url);
            return { json: async () => ({ success: true, license: status }) };
        },
        document: {
            createElement: (tag) => ({ tagName: tag, className: "", textContent: "", title: "" }),
        },
        location: { href: "/base/" },
        localStorage: { setItem: (...a) => storage.push(a), getItem: () => null },
        open: (url) => opened.push(url),
    };
    ctx.window = ctx;
    ctx.window.flaskVariables = flask;
    ctx.window.PlexoraConfirm = {
        choose: async (spec) => { dialogs.push(spec); return answer; },
        tell: async (spec) => { dialogs.push(spec); return true; },
    };
    createContext(ctx);
    runInContext(read("plexora/client/src/js/services/paidFeature.js"), ctx);
    return { P: ctx.PlexoraPaid, ctx, fetched, opened, dialogs, storage };
}

await check("loading paidFeature.js fetches nothing, stores nothing, shows nothing", () => {
    const { fetched, dialogs, storage } = paidSandbox();
    assert.equal(fetched.length, 0);
    assert.equal(dialogs.length, 0);
    assert.equal(storage.length, 0);
});

await check("the page hint is hierarchical and false on Free", () => {
    const free = paidSandbox({ flask: { license: { paid: false, entitlements: [] } } });
    assert.equal(free.P.allows("ai:gating"), false);
    assert.equal(free.P.allows(null), true);
    assert.equal(free.P.allows("free"), true);
    const paid = paidSandbox({ flask: { license: { paid: true, entitlements: ["ai"] } } });
    assert.equal(paid.P.allows("ai:gating:session"), true);
    assert.equal(paid.P.allows("aix"), false);
    const narrow = paidSandbox({ flask: { license: { paid: true, entitlements: ["ai:evidence"] } } });
    assert.equal(narrow.P.allows("ai:gating"), false);
});

await check("the modal names the feature, says Free keeps working, and offers a trial on Free", async () => {
    const { P, dialogs } = paidSandbox({ status: { state: "free", paid: false } });
    await P.explain({ entitlement: "ai:gating:session", label: "AI gating sessions",
                      summary: "Guided gating sessions an agent runs." });
    assert.equal(dialogs.length, 1);
    const spec = dialogs[0];
    assert.match(spec.title, /AI gating sessions is a Paid feature/);
    assert.ok(spec.body.some((line) => /Everything Free keeps working/.test(line)));
    assert.deepEqual(Array.from(spec.choices, (c) => c.value), [null, "license", "trial"]);
});

await check("a lapsed licence says so", async () => {
    const { P, dialogs } = paidSandbox({ status: { state: "expired", paid: false } });
    await P.explain({ label: "X", state: "expired" });
    assert.ok(dialogs[0].body.some((line) => /expired/.test(line)));
});

await check("Start a Trial opens the portal's trial page in the browser", async () => {
    const url = "https://license.example/portal/trial?fp=" + "a".repeat(64);
    const { P, opened } = paidSandbox({ status: { state: "free", trial_url: url }, answer: "trial" });
    assert.equal(await P.explain({ label: "X" }), "trial");
    assert.deepEqual(Array.from(opened), [url]);
});

await check("Enter License goes to Settings > License", async () => {
    const { P, ctx } = paidSandbox({ status: { state: "free" }, answer: "license" });
    await P.explain({ label: "X" });
    assert.equal(ctx.location.href, "/base/settings#license");
});

await check("with no licence service, a trial explains itself instead of opening nothing", async () => {
    const { P, opened, dialogs } = paidSandbox({ status: { state: "free", trial_url: "" } });
    assert.equal(await P.startTrial(), false);
    assert.equal(opened.length, 0);
    assert.match(dialogs[0].title, /Trials are not available/);
});

await check("the badge says Paid", () => {
    const { P } = paidSandbox();
    assert.equal(P.badge().textContent, "Paid");
    assert.equal(P.badge().className, "plx-paid-badge");
});

// -- toolLoader.js: the locked outcome --------------------------------------------

function loaderSandbox(payload) {
    const mounted = [];
    const explained = [];
    const element = (tag) => ({
        tagName: tag, dataset: {}, attributes: {},
        classList: { add() {}, remove() {}, toggle() {} },
        set innerHTML(v) { if (this.attributes["data-tool-panel"]) mounted.push(v); },
        addEventListener() {}, querySelector: () => null, appendChild: (n) => n,
        insertBefore: (n) => n, setAttribute(name, value) { this.attributes[name] = String(value); },
        firstChild: null,
    });
    const ctx = {
        console, Promise, Object, Array, Map, Set, JSON, String, Error, setTimeout, clearTimeout,
        document: {
            querySelector: () => null, querySelectorAll: () => [], createElement: element,
            getElementById: (id) => element(`#${id}`),
            head: { appendChild: (node) => { mounted.push(node.src || node.href); return node; } },
            addEventListener() {},
        },
        fetch: async () => ({ ok: true, status: 403, json: async () => payload }),
    };
    ctx.window = {
        flaskVariables: { datasource: "probe" }, PLEXORA_BASE_URL: "",
        __plexoraReady: Promise.resolve(), addEventListener() {}, removeEventListener() {},
        Plexora: { plugins: new Map() },
        __plexora: { activatePlugin: async () => { throw new Error("must not activate a locked tool"); } },
        PlexoraPaid: { explain: async (info) => { explained.push(info); return null; } },
    };
    Object.assign(ctx, ctx.window);
    createContext(ctx);
    runInContext(read("plexora/client/src/js/views/cardList.js"), ctx);
    runInContext(read("plexora/client/src/js/views/toolLoader.js"), ctx);
    return { L: ctx.window.PlexoraToolLoader || ctx.PlexoraToolLoader, mounted, explained };
}

const LOCKED = { locked: { entitlement: "plugin:spatial_stats", plan_required: "paid", tool: "spatial_stats",
                           label: "Spatial Stats", summary: "A Paid Plexora feature.", state: "free" } };

await check("a locked tool opens the Paid modal and mounts nothing", async () => {
    const { L, mounted, explained } = loaderSandbox(LOCKED);
    const outcome = await L.openTool("spatial_stats");
    assert.equal(explained.length, 1);
    assert.equal(explained[0].label, "Spatial Stats");
    assert.equal(mounted.length, 0);
    assert.equal(outcome.skipped, "locked");
    assert.equal(L.loadedTools().length, 0);
});

await check("a quiet (restore) load of a locked tool opens nothing", async () => {
    const { L, explained } = loaderSandbox(LOCKED);
    const result = await L.restore({ loaded: [{ name: "spatial_stats", visible: true }] }, { started: true });
    assert.equal(explained.length, 0);
    assert.equal(result.skipped.length, 1);
    assert.match(result.skipped[0], /locked/);
});
