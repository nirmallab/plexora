/**
 * services/telemetry.js: the tab's aggregate, and when it leaves.
 *
 *   - OFF IS FREE: once the server says off, nothing is kept and nothing sent.
 *   - BANDS MATCH THE SERVER: the same nine millisecond bins and log bands.
 *   - THE TAB ONLY TALKS TO ITS OWN SERVER, and only with rows.
 *   - HIDDEN -> keepalive fetch; GONE -> sendBeacon with a JSON Blob.
 *   - A FAILED POST KEEPS ITS ROWS; a refused one (400) does not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/telemetry.js"), "utf8");

let checks = 0;
async function check(label, fn) {
    await fn();
    checks += 1;
    console.log("  ok  " + label);
}

function sandbox({ status = { mode: "anonymous", enabled: true, ingest_interval_s: 300 },
                   answer = 204, toast = null } = {}) {
    const posts = [];
    const beacons = [];
    const windowListeners = {};
    const documentListeners = {};
    const ctx = {
        console,
        crypto: { getRandomValues: (buffer) => buffer.fill(7) },
        setInterval: () => 1,
        clearInterval: () => {},
        Blob: class { constructor(parts, options) { this.text = parts.join(""); this.type = options.type; } },
        navigator: { sendBeacon: (url, blob) => { beacons.push({ url, blob }); return true; } },
        document: {
            visibilityState: "visible",
            addEventListener: (name, fn) => { (documentListeners[name] ||= []).push(fn); },
        },
        plexoraUrl: (path) => "/" + path,
        fetch: async (url, options = {}) => {
            if (url.startsWith("/telemetry/status")) {
                return { ok: true, json: async () => status };
            }
            posts.push({ url, options, body: options.body ? JSON.parse(options.body) : null });
            const code = typeof ctx.answer === "function" ? ctx.answer() : ctx.answer;
            if (code instanceof Error) throw code;
            return { ok: code >= 200 && code < 300, status: code };
        },
        answer,
        addEventListener: (name, fn) => { (windowListeners[name] ||= []).push(fn); },
        PlexoraToast: toast,
    };
    ctx.window = ctx;
    createContext(ctx);
    runInContext(SOURCE, ctx);
    return { ctx, posts, beacons, windowListeners, documentListeners,
             T: ctx.PlexoraTelemetry };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

await check("the bins are the server's: 16 is <16, 17 is 16-50, 5001 is >5k", () => {
    const { T } = sandbox();
    assert.equal(T.msBin(0), 0);
    assert.equal(T.msBin(16), 0);
    assert.equal(T.msBin(17), 1);
    assert.equal(T.msBin(5000), 7);
    assert.equal(T.msBin(5001), 8);
    assert.equal(T.band10(0), "0");
    assert.equal(T.band10(12345), "10k");
    assert.equal(T.bandPow2(3), "2-3");
    assert.equal(T.bandPow2(300), "256+");
});

await check("counts and histograms fold into one row per key and dims", async () => {
    const { T } = sandbox();
    await settle();
    T.count("tool.summary", "open", 1, { tool: "gating" });
    T.count("tool.summary", "open", 2, { tool: "gating" });
    T.observe("render.summary", "tile_ms", 12, { browser: "chrome" });
    T.observe("render.summary", "tile_ms", 70, { browser: "chrome" });
    const rows = [...T._rows.values()];
    assert.equal(rows.length, 2);
    const hist = rows.find((row) => row.k === "tile_ms");
    assert.deepEqual([...hist.h], [1, 0, 1, 0, 0, 0, 0, 0, 0]);
    assert.equal(rows.find((row) => row.k === "open").n, 3);
});

await check("off keeps nothing and posts nothing", async () => {
    const { T, posts } = sandbox({ status: { mode: "off" } });
    T.count("tool.summary", "open", 1, { tool: "roi" });
    await settle();
    assert.equal(T.enabled, false);
    assert.equal(T._rows.size, 0);
    T.count("tool.summary", "open", 1, { tool: "roi" });
    assert.equal(T._rows.size, 0);
    await T.post("fetch");
    assert.equal(posts.length, 0);
});

await check("calls before the server answers are kept, and posted once it says on", async () => {
    const { T, posts } = sandbox();
    T.count("tool.summary", "open", 1, { tool: "roi" });
    await settle();
    await T.post("fetch");
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, "/telemetry/ingest");
    assert.equal(posts[0].body.rows[0].n, 1);
});

await check("a post goes only to this tab's own server, with only rows", async () => {
    const { T, posts } = sandbox();
    await settle();
    T.feature("gate.commit", "gating");
    await T.post("fetch");
    const body = posts[0].body;
    assert.deepEqual(Object.keys(body).sort(), ["rows", "schema", "viewer_id"]);
    assert.match(body.viewer_id, /^[0-9a-f]{32}$/);
    assert.deepEqual(body.rows[0], { e: "feature.summary", k: "n",
                                     d: { feature: "gate.commit", plugin: "gating" }, n: 1 });
});

await check("a feature not on the list is dropped in the tab", async () => {
    const { T } = sandbox();
    await settle();
    T.feature("export.patient_29384", "gating");
    assert.equal(T._rows.size, 0);
});

await check("somebody else's plugin is named by a hash, or the server's own label", async () => {
    const { T, ctx } = sandbox({ status: { mode: "anonymous", enabled: true,
                                           plugin_labels: { acme_lab: "ext:1234abcd" } } });
    await settle();
    assert.equal(T.ownerLabel("gating"), "gating");
    assert.equal(T.ownerLabel("acme_lab"), "ext:1234abcd");
    assert.match(T.ownerLabel("other_lab"), /^ext:[0-9a-f]{8}$/);
    void ctx;
});

await check("a failed post keeps its rows for the next one", async () => {
    const { T, posts, ctx } = sandbox({ answer: new Error("offline") });
    await settle();
    T.count("tool.summary", "open", 4, { tool: "roi" });
    await T.post("fetch");
    assert.equal(T._rows.size, 1);
    ctx.answer = 204;
    await T.post("fetch");
    assert.equal(posts.length, 2);
    assert.equal(posts[1].body.rows[0].n, 4);
    assert.equal(T._rows.size, 0);
});

await check("a refused post (400) is not retried", async () => {
    const { T, ctx } = sandbox({ answer: 400 });
    await settle();
    T.count("tool.summary", "open", 1, { tool: "roi" });
    await T.post("fetch");
    assert.equal(T._rows.size, 0);
    void ctx;
});

await check("hidden -> keepalive fetch; pagehide -> sendBeacon with a JSON Blob", async () => {
    const { T, posts, beacons, windowListeners, documentListeners, ctx } = sandbox();
    await settle();
    T.count("tool.summary", "open", 1, { tool: "roi" });
    ctx.document.visibilityState = "hidden";
    documentListeners.visibilitychange.forEach((fn) => fn());
    await settle();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].options.keepalive, true);
    T.count("tool.summary", "close", 1, { tool: "roi" });
    windowListeners.pagehide.forEach((fn) => fn());
    assert.equal(beacons.length, 1);
    assert.equal(beacons[0].url, "/telemetry/ingest");
    assert.equal(beacons[0].blob.type, "application/json");
    assert.equal(JSON.parse(beacons[0].blob.text).rows[0].k, "close");
});

await check("the notice appears when due, and OK tells the server it was seen", async () => {
    const shown = [];
    const { posts } = sandbox({
        status: { mode: "anonymous", enabled: true, notice_pending: true },
        toast: { show: (options) => { shown.push(options); } },
    });
    await settle();
    assert.equal(shown.length, 1);
    assert.equal(shown[0].timeout, 0);
    assert.ok(!/patient|http/.test(JSON.stringify(shown[0])));
    shown[0].actions.find((action) => action.label === "OK").onSelect();
    await settle();
    assert.equal(posts.filter((post) => post.url === "/telemetry/notice_seen").length, 1);
});

await check("no notice when it is not due", async () => {
    const shown = [];
    sandbox({ toast: { show: (options) => { shown.push(options); } } });
    await settle();
    assert.equal(shown.length, 0);
});

console.log(`\nall checks passed (${checks})`);
