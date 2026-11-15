/**
 * What a bug report carries besides what the person wrote: the recent errors
 * services/errors.js keeps (scrubbed as they are caught, capped at ten) and the
 * diagnostics services/feedbackDiagnostics.js collects. Both run from source
 * in a vm with the few globals they read; neither needs a DOM beyond that.
 *
 * Run directly:  node tests/js/feedback_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const ERRORS = readFileSync(join(REPO, "plexora/client/src/js/services/errors.js"), "utf8");
const DIAGNOSTICS = readFileSync(join(REPO, "plexora/client/src/js/services/feedbackDiagnostics.js"), "utf8");

let failures = 0;
async function check(name, fn) {
    try {
        await fn();
        console.log(`PASS ${name}`);
    } catch (error) {
        failures += 1;
        console.log(`FAIL ${name}\n  ${error && error.stack || error}`);
    }
}

function world({ pathname = "/", datasource = "", parent = null, ua } = {}) {
    const listeners = {};
    const window = {
        location: { origin: "http://127.0.0.1:8765", pathname },
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        innerWidth: 1440, innerHeight: 900, devicePixelRatio: 2,
        screen: { width: 2560, height: 1440 },
        flaskVariables: { base_url: "", license: { plan: "pro" } },
        PlexoraToolLoader: { activeTool: () => "gating" },
    };
    window.self = window;
    window.top = window;
    window.parent = parent || window;
    window.window = window;
    const context = {
        window, console: { error() {}, warn() {}, log() {} },
        navigator: { userAgent: ua || "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", language: "en-GB", hardwareConcurrency: 8,
            maxTouchPoints: 0, platform: "MacIntel" },
        document: { body: { dataset: { plexoraDatasource: datasource } },
                    createElement: () => ({ getContext: () => null }) },
        Intl, Date, Math, JSON, Object, Array, String, Number, Promise, RegExp, Error, TypeError, Set, Map,
    };
    context.globalThis = context;
    createContext(context);
    runInContext(ERRORS, context);
    runInContext(DIAGNOSTICS, context);
    return { window, listeners, context };
}

await check("a URL becomes its script's name, a path <path>, an email <email>, a key <redacted>", () => {
    const { window } = world();
    const scrub = window.PlexoraErrors.scrub;
    assert.equal(scrub("at f (http://127.0.0.1:8765/client/src/js/views/x.js?v=3:10:5)"), "at f (x.js:10:5)");
    assert.equal(scrub("GET https://bucket.example.org/data/lung?sig=abc failed"), "GET <url> failed");
    assert.equal(scrub("open /Users/pat/Desktop/lung data/s1.ome.tiff failed"), "open <path> failed");
    assert.equal(scrub("C:\\Users\\pat\\x.tif and ~/projects/y"), "<path> and <path>");
    assert.equal(scrub("from pat.lee@lab.example.org"), "from <email>");
    assert.equal(scrub(`cert BIOC1.k.p.s key ${"a".repeat(44)}`), "cert <redacted> key <redacted>");
    assert.equal(scrub("ratio 1/2, a/b/c relative"), "ratio 1/2, a/b/c relative");
    assert.ok(scrub("x".repeat(5000)).length <= 2048);
});

await check("reported, uncaught and unhandled failures are kept, newest last, ten at most", () => {
    const { window, listeners } = world();
    for (let i = 0; i < 12; i += 1) {
        window.PlexoraErrors.report(new TypeError(`boom ${i} in /Users/pat/p${i}`), { component: "viewer", action: "load" });
    }
    listeners.error[0]({ error: new RangeError("uncaught one") });
    listeners.unhandledrejection[0]({ reason: "a plain string" });
    const kept = window.PlexoraErrors.recent();
    assert.equal(kept.length, 10);
    assert.equal(kept.at(-2).message, "uncaught one");
    assert.equal(kept.at(-2).source, "uncaught");
    assert.equal(kept.at(-1).message, "a plain string");
    assert.equal(kept.at(-1).source, "unhandled rejection");
    assert.equal(kept[0].message, "boom 4 in <path>");
    assert.equal(kept[0].source, "viewer load");
    assert.ok(kept.every((entry) => !/\/Users\//.test(JSON.stringify(entry))));
});

await check("the website's Python errors the host page kept are read and scrubbed again", () => {
    const host = { PlexoraPythonErrors: [{ at: "2999-01-01T00:00:00Z", message: "OSError: /home/pat/x.h5",
                                           stack: "File \"/lib/python3.12/a.py\", line 3" }] };
    const { window } = world({ parent: host });
    const kept = window.PlexoraErrors.recent();
    assert.equal(kept.at(-1).kind, "PythonError");
    assert.equal(kept.at(-1).message, "OSError: <path>");
    assert.ok(!kept.at(-1).stack.includes("/lib/"));
});

await check("diagnostics: the software, the system, the screen, the kind of page, never its address", async () => {
    const { window } = world({ pathname: "/lung_sample_3", datasource: "lung_sample_3" });
    window.PlexoraErrors.report(new Error("tile failed"), { component: "tiles", action: "fetch" });
    const sent = await window.PlexoraFeedbackDiagnostics.collect({ version: "0.0.57", build: "abc1234",
                                                                    platform: "macOS-15.1", python: "3.12.4" });
    assert.equal(sent.app_version, "0.0.57");
    assert.equal(sent.build, "abc1234");
    assert.equal(sent.mode, "browser");
    assert.equal(sent.os, "macOS");
    assert.equal(sent.browser, "Chrome");
    assert.equal(sent.screen, "2560×1440");
    assert.equal(sent.pixel_ratio, 2);
    assert.equal(sent.route, "viewer");
    assert.equal(sent.tool, "gating");
    assert.equal(sent.plan, "Pro");
    assert.equal(sent.errors.length, 1);
    assert.ok(!JSON.stringify(sent).includes("lung_sample_3"), "a project's name never leaves");
    for (const [key, value] of Object.entries(sent)) {
        assert.match(key, /^[a-z][a-z0-9_]{0,39}$/, "every key is one the platform keeps");
        if (key !== "errors") assert.ok(["string", "number", "boolean"].includes(typeof value), key);
    }
    const rows = window.PlexoraFeedbackDiagnostics.rows(sent);
    assert.ok(rows.some(([label, value]) => label === "Page" && value === "viewer"));
    assert.ok(!rows.some(([label]) => label === "errors"));
});

await check("the pages it can name: home, settings, a viewer; anything else is other", () => {
    assert.equal(world({ pathname: "/" }).window.PlexoraFeedbackDiagnostics.pageKind(), "home");
    assert.equal(world({ pathname: "/settings" }).window.PlexoraFeedbackDiagnostics.pageKind(), "settings");
    assert.equal(world({ pathname: "/x/y" }).window.PlexoraFeedbackDiagnostics.pageKind(), "other");
});

await check("Windows and Firefox are named from the user agent", async () => {
    const { window } = world({ ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0" });
    const sent = await window.PlexoraFeedbackDiagnostics.collect({});
    assert.equal(sent.os, "Windows");
    assert.equal(sent.browser, "Firefox");
    assert.equal(sent.browser_version, "133.0");
});

if (failures) {
    console.log(`${failures} check(s) failed`);
    process.exit(1);
}
console.log("all checks passed");
