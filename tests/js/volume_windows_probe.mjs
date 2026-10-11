/**
 * The windows a volume's tiles are quantized with (services/volumeWindows.js).
 *
 * Pinned: one request for a batch of channels, and none for a window already
 * held; a provisional window is asked for again until it is exact, backing
 * off; a change is announced once with the window before it; and a window is
 * written into an address exactly as the server's `f"{x:g}"` writes it.
 *
 * Run directly:  node tests/js/volume_windows_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

const timers = [];
const events = [];
const box = {
    console,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { events.push(event); return true; },
};
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(REPO, "plexora/client/src/js/services/volumeWindows.js"), "utf8"),
             createContext(box));
const VolumeWindows = box.PlexoraVolumeWindows;

let answers = { a: { qmin: 0, qmax: 812, exact: false }, b: { qmin: 0, qmax: 4095, exact: true } };
const asked = [];
const fetchImpl = async (url) => {
    asked.push(url);
    const names = [...new URL(`http://x/${url}`).searchParams.getAll("channel")];
    return { ok: true, json: async () => ({ windows: Object.fromEntries(names.map((n) => [n, answers[n]])) }) };
};

const windows = new VolumeWindows("vol", { fetchImpl });
await windows.ensure(["a", "b"]);
check("one request for a batch of channels", asked.length === 1 && asked[0].includes("channel=a")
    && asked[0].includes("channel=b"));
await windows.ensure(["a", "b"]);
check("none for windows already held", asked.length === 1);
check("a window is written as the server writes it",
    windows.stamp("a") === "0,812" && VolumeWindows.formatG(1234.5) === "1234.5"
    && VolumeWindows.formatG(0.000123456789) === "0.000123457");
check("a provisional window is asked for again, later", timers.length === 1 && timers[0].ms >= 1000);

answers = { a: { qmin: 0, qmax: 4095, exact: true } };
await timers[0].fn();
const change = events.find((e) => e.type === "plexora:volume-window");
check("the exact window replaces it, announced once with the one before",
    windows.peek("a").exact && change?.detail?.name === "a" && change.detail.window[1] === 4095
    && change.detail.previous[1] === 812 && events.length === 1);
check("and nothing is asked once every window is exact", timers.length === 1);

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
