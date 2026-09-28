/**
 * Print one ingest body built by the shipped telemetry, errors and
 * performance scripts, for tests/test_telemetry_js.py to validate against the
 * server's own schema: the contract between the tab and /telemetry/ingest.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js/services");
const posted = [];
const ctx = {
    console,
    URL,
    Error,
    crypto: { getRandomValues: (buffer) => buffer.fill(9) },
    setInterval: () => 1,
    clearInterval: () => {},
    location: { origin: "http://127.0.0.1:8000", href: "http://127.0.0.1:8000/" },
    navigator: { userAgent: "Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0", hardwareConcurrency: 64 },
    devicePixelRatio: 1,
    performance: { now: () => 800 },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    document: { visibilityState: "visible", addEventListener: () => {} },
    addEventListener: () => {},
    plexoraUrl: (path) => "/" + path,
    PLEXORA_BASE_URL: "",
    fetch: async (url, options = {}) => {
        if (url.startsWith("/telemetry/status")) {
            return { ok: true, json: async () => ({ mode: "diagnostics", enabled: true }) };
        }
        posted.push(JSON.parse(options.body));
        return { ok: true, status: 204 };
    },
};
ctx.window = ctx;
createContext(ctx);
for (const name of ["telemetry.js", "errors.js", "performanceTelemetry.js"]) {
    runInContext(readFileSync(join(JS, name), "utf8"), ctx);
}
await new Promise((resolve) => setTimeout(resolve, 0));
const { PlexoraTelemetry: T, PlexoraPerf: P, PlexoraErrors: E } = ctx;
T.tool("open", "gating");
T.tool("fold", "roi");
T.tool("load_failed", "some_lab_plugin", { why: "script" });
T.toolLoad("figure_builder", 420);
T.feature("gate.commit", "gating");
T.feature("gene.add", "transcripts");
P.decoded("webp", "worker", 9);
P.decoded("gray16", "inline", 40);
P.decodeFallback();
P.labelRenderer("gpu");
P.noteGl({ gl: { getExtension: () => null, canvas: null }, _tileTextureCache: { hits: 3, misses: 1 } });
P._onResource({ initiatorType: "xmlhttprequest",
                name: "http://127.0.0.1:8000/generated/data/p/c_0/0/0_0.png",
                startTime: 0, responseEnd: 30, transferSize: 2000,
                serverTiming: [{ name: "total", duration: 20 }, { name: "cache", description: "hit" }] });
const error = new Error("x");
error.stack = "Error: x\n    at f (http://127.0.0.1:8000/client/src/js/main.js:1:1)";
E.report(error, { component: "plugin", action: "activate", plugin: "gating", message: "m" });
ctx.console = { error: () => {} };
await T.post("fetch");
process.stdout.write(JSON.stringify(posted[0]));
