#!/usr/bin/env node
/**
 * What roiApi.js actually puts on the wire, run rather than read.
 *
 * The route tests post their own bodies and the panel probes stub the API, so
 * neither sees the request the browser sends. That gap shipped a `preview`
 * flag on the AnnData save -- where it threw on an undefined name -- instead
 * of on Map to cells, where its absence turned a preview into a write. Every
 * method is called with a fake fetch and its JSON body checked.
 *
 *   node tests/js/roi_api_body_probe.mjs [--source path/to/roiApi.js]
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const flag = process.argv.indexOf("--source");
const SOURCE = flag > 0 ? process.argv[flag + 1]
    : join(REPO, "plexora/plugins/roi/static/roiApi.js");

const checks = [];
const failures = [];
function check(what, actual, expected) {
    checks.push(what);
    if (JSON.stringify(actual) !== JSON.stringify(expected)) failures.push({ what, actual, expected });
}

const sent = [];
const context = createContext({
    console, JSON, URLSearchParams, Promise, Boolean, String, Object,
    fetch: async (url, init = {}) => {
        sent.push({ url, body: init.body ? JSON.parse(init.body) : null });
        return { ok: true, status: 200, json: async () => ({ success: true }) };
    },
});
runInContext(readFileSync(SOURCE, "utf8") + ";globalThis.__RoiApi = RoiApi;", context);
const api = new context.__RoiApi({ url: (path) => `/${path}`, datasource: "proj" });

async function body(call) {
    sent.length = 0;
    try { await call(); } catch (error) { return { threw: String(error) }; }
    return sent[0]?.body ?? null;
}

check("Map to cells sends the preview flag when asked",
    await body(() => api.mapToCells("pass2", false, true)),
    { datasource: "proj", name: "pass2", replace: false, preview: true });
check("...and a write says it is not one",
    await body(() => api.mapToCells("pass2", true)),
    { datasource: "proj", name: "pass2", replace: true, preview: false });
check("the AnnData save sends its key and replace, and nothing of Map to cells'",
    await body(() => api.saveToAnndata("rois", false)),
    { datasource: "proj", key: "rois", replace: false });

console.error(JSON.stringify({ source: SOURCE.replace(REPO + "/", ""), checked: checks.length, failures }, null, 2));
process.exit(failures.length ? 1 : 0);
