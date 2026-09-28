/**
 * services/errors.js: a failure's fingerprint, and nothing else about it.
 *
 *   - The message is logged, never counted.
 *   - The script basename survives only for Plexora's own scripts on this
 *     page's own origin; anything else is `external`, a stack-less throw
 *     `inline`.
 *   - A loop that throws cannot fill the aggregate: twenty new fingerprints
 *     per tab per five minutes.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js/services");

let checks = 0;
function check(label, fn) {
    fn();
    checks += 1;
    console.log("  ok  " + label);
}

function sandbox() {
    const logged = [];
    const counted = [];
    const listeners = {};
    const ctx = {
        console: { error: (...args) => logged.push(args), log: () => {}, warn: () => {} },
        location: { origin: "http://127.0.0.1:8000" },
        addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); },
        Error,
        PlexoraTelemetry: {
            enabled: true,
            count: (event, key, n, dims) => counted.push({ event, key, n, dims }),
            ownerLabel: (name) => (name === "gating" ? "gating" : "ext:0000abcd"),
            fnv8: (text) => {
                let hash = 0x811c9dc5;
                for (let i = 0; i < text.length; i += 1) {
                    hash ^= text.charCodeAt(i);
                    hash = Math.imul(hash, 0x01000193) >>> 0;
                }
                return hash.toString(16).padStart(8, "0");
            },
        },
    };
    ctx.window = ctx;
    createContext(ctx);
    runInContext(readFileSync(join(JS, "errors.js"), "utf8"), ctx);
    return { E: ctx.PlexoraErrors, logged, counted, listeners, ctx };
}

function thrown(message, stack) {
    const error = new Error(message);
    error.stack = stack;
    return error;
}

const OWN = "TypeError: x\n    at load (http://127.0.0.1:8000/client/src/js/views/toolLoader.js?v=20260927:120:9)";
const PLUGIN = "Error: y\n    at f (http://127.0.0.1:8000/plugins/gating/static/gatingSidebarController.js?v=1:3:4)";
const PREFIXED = "Error: y\n    at f (http://127.0.0.1:8000/rnode/node17/8000/client/dist/vendor_bundle.js:1:2)";
const FOREIGN = "Error: z\n    at g (https://cdn.example/lib.js:1:1)";
const DATA_URL = "Error: z\n    at g (http://127.0.0.1:8000/generated/data/patient_29384/c_0/0/0_0.png:1:1)";

check("the basename survives only for Plexora's own scripts on this origin", () => {
    const { E } = sandbox();
    assert.equal(E.assetOf(OWN), "toolLoader.js");
    assert.equal(E.assetOf(PLUGIN), "gatingSidebarController.js");
    assert.equal(E.assetOf(PREFIXED), "vendor_bundle.js");
    assert.equal(E.assetOf(FOREIGN), "external");
    assert.equal(E.assetOf(DATA_URL), "external");
    assert.equal(E.assetOf(""), "inline");
});

check("report logs the message and counts only the fingerprint", () => {
    const { E, logged, counted } = sandbox();
    const error = thrown("could not open /Users/alice/patient_29384.ome.tif", OWN);
    error.name = "TypeError";
    E.report(error, { component: "tool_loader", action: "load", plugin: "gating",
                      message: "toolLoader: loading gating failed" });
    assert.equal(logged.length, 1);
    assert.equal(logged[0][0], "toolLoader: loading gating failed");
    assert.equal(counted.length, 1);
    const { dims } = counted[0];
    assert.deepEqual(Object.keys(dims).sort(),
                     ["action", "component", "exc_type", "file", "fp", "plugin", "where"]);
    assert.equal(dims.where, "browser");
    assert.equal(dims.exc_type, "TypeError");
    assert.equal(dims.file, "toolLoader.js");
    assert.match(dims.fp, /^[0-9a-f]{8}$/);
    assert.ok(!/alice|patient|http|\//.test(JSON.stringify(counted)));
    assert.equal(error.__plexoraReported, true);
});

check("the same failure has the same fingerprint whatever its message", () => {
    const { E, counted } = sandbox();
    E.report(thrown("one", OWN), { component: "viewer", action: "render" });
    E.report(thrown("two, different", OWN), { component: "viewer", action: "render" });
    assert.equal(counted[0].dims.fp, counted[1].dims.fp);
});

check("an unknown component or action is `other`", () => {
    const { E, counted } = sandbox();
    E.report(thrown("x", OWN), { component: "patient_29384", action: "open /data" });
    assert.equal(counted[0].dims.component, "other");
    assert.equal(counted[0].dims.action, "other");
});

check("twenty new fingerprints per window, then quiet", () => {
    const { E, counted } = sandbox();
    for (let i = 0; i < 30; i += 1) {
        E.report(thrown("x", `Error: x\n    at f (http://127.0.0.1:8000/client/src/js/f${i}.js:1:1)`),
                 { component: "viewer", action: "render" });
    }
    assert.equal(counted.length, 20);
});

check("uncaught errors and rejections are counted as window and promise", () => {
    const { listeners, counted } = sandbox();
    listeners.error.forEach((fn) => fn({ error: thrown("boom", OWN) }));
    listeners.error.forEach((fn) => fn({}));  // a resource error: no error object
    listeners.unhandledrejection.forEach((fn) => fn({ reason: "a string reason" }));
    assert.deepEqual(counted.map((c) => c.dims.component), ["window", "promise"]);
    assert.equal(counted[1].dims.file, "inline");
});

check("off counts nothing but still logs", () => {
    const { E, logged, counted, ctx } = sandbox();
    ctx.PlexoraTelemetry.enabled = false;
    E.report(thrown("x", OWN), { component: "viewer", action: "render" });
    assert.equal(logged.length, 1);
    assert.equal(counted.length, 0);
});

console.log(`\nall checks passed (${checks})`);
