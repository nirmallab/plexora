/**
 * A viewer preference whose save fails is not forgotten.
 *
 * `flush` takes `_pending` before it asks, so a PUT that is refused (a 5xx)
 * or never arrives (the network) used to drop the change: the layout on
 * screen was right, and the next session opened without it. What failed is
 * put back UNDER whatever changed since, and goes with the next save.
 *
 * viewerPrefs.js is run as the classic script the page loads.
 *
 * Run directly:  node tests/js/viewer_prefs_flush_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/viewerPrefs.js");

const failures = [];
function check(label, ok, detail = "") {
    console.log(`${ok ? "ok" : "FAIL"} - ${label}${ok || !detail ? "" : `  ${detail}`}`);
    if (!ok) failures.push(label);
}

const context = { console, setTimeout, clearTimeout, Promise, JSON, Object, Number, Math };
context.window = context;
createContext(context);
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "viewerPrefs.js" });
const ViewerPrefs = context.PlexoraViewerPrefsClass;

const sent = [];
let answer = () => Promise.resolve({ ok: true, status: 200 });
const prefs = new ViewerPrefs({
    fetchImpl: (url, options) => {
        if (options?.method === "PUT") sent.push(JSON.parse(options.body));
        return answer();
    },
    saveDelay: 1e9,
});

// -- a refused PUT --------------------------------------------------------------
answer = () => Promise.resolve({ ok: false, status: 503 });
prefs.set({ motion: "off", grid: { on: true } });
await prefs.flush();
check("a refused save is sent once", sent.length === 1);

// A change made after the failure wins over what failed.
prefs.set({ grid: { opacity: 0.5 } });
answer = () => Promise.resolve({ ok: true, status: 200 });
await prefs.flush();
const second = sent[1] || {};
check("the next save carries what the refused one held",
    second.motion === "off" && second.grid?.on === true, JSON.stringify(second));
check("...merged with what changed since",
    second.grid?.opacity === 0.5, JSON.stringify(second));

await prefs.flush();
check("a save that went through is not sent again", sent.length === 2, String(sent.length));

// -- a PUT that never arrives -----------------------------------------------------
answer = () => Promise.reject(new TypeError("Failed to fetch"));
prefs.set({ motion: "on" });
await prefs.flush();
prefs.set({ sections: { xz: 0.3 } });
answer = () => Promise.resolve({ ok: true, status: 200 });
await prefs.flush();
const fourth = sent[3] || {};
check("a save lost on the network goes with the next one",
    fourth.motion === "on" && fourth.sections?.xz === 0.3, JSON.stringify(fourth));

// -- a newer value of the same key is not overwritten by the failed one -----------
answer = () => Promise.resolve({ ok: false, status: 500 });
prefs.set({ motion: "off" });
const failing = prefs.flush();
prefs.set({ motion: "on" });
await failing;
answer = () => Promise.resolve({ ok: true, status: 200 });
await prefs.flush();
const last = sent[sent.length - 1] || {};
check("a value changed while the failed save was out is the one sent",
    last.motion === "on", JSON.stringify(last));

// -- a 4xx is the server saying no, not a failure to retry --------------------------
answer = () => Promise.resolve({ ok: false, status: 400 });
prefs.set({ motion: "off" });
await prefs.flush();
const before = sent.length;
answer = () => Promise.resolve({ ok: true, status: 200 });
await prefs.flush();
check("a refused (4xx) body is not sent again", sent.length === before, String(sent.length - before));

console.log(failures.length ? `\nFAILURES: ${failures.join(", ")}` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
