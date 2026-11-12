/**
 * The desktop's slow-read notices (client src/js/services/handoffNotice.js
 * loaded by templates/base.html with `data-reads="desktop"`): the page asks
 * its server's GET /read_pace, lines the server's clock up with its own, and
 * says "Waiting on the network" / "Reading a slow drive" the website's way.
 * Prints JSON; tests/test_envelope.py asserts on it.
 *
 * Run:  node tests/js/desktop_pace_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const client = path.join(REPO, "plexora", "client", "src");
const limits = JSON.parse(readFileSync(path.join(client, "envelope.json"), "utf-8"));

// The server's clock an hour behind the page's: its intervals still count.
const SKEW = -3600_000;
const serverNow = () => Date.now() + SKEW;
//: Twelve seconds of waiting, one interval a second, at `rate` bytes a second.
const waits = (cause, source, rate, first) => Array.from({ length: 12 }, (_, i) => {
    const end = serverNow() - (11 - i) * 1000;
    return { cause, source, start: end - 1000, end, busy: 1000, bytes: rate, seq: first + i };
});

const asked = [];
let answers = [
    { events: waits("network", "transfer.rc.hms.harvard.edu", 3e6, 1), last: 12 },
    { events: waits("drive", "melanoma.ome.tif", 4e6, 13), last: 24 },
    { events: waits("network", "fast.example.org", 400e6, 25), last: 36 },
];
const toasts = [];
const opened = [];
const store = new Map();
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
globalThis.flaskVariables = { datasource: "melanoma" };
globalThis.PlexoraToast = { show: (options) => toasts.push(options) };
globalThis.PlexoraConnectionModal = { open: (options) => opened.push(options) };
globalThis.document = {
    visibilityState: "visible",
    currentScript: { dataset: { reads: "desktop" },
                     src: "http://127.0.0.1:5000/base/client/src/js/services/handoffNotice.js?v=1" },
};
globalThis.fetch = async (url) => {
    asked.push(String(url));
    if (String(url).endsWith("/envelope.json")) return { ok: true, json: async () => limits };
    const body = answers.shift() || { events: [], last: 36 };
    return { ok: true, json: async () => ({ ...body, now: serverNow() }) };
};
const timers = [];
const realSetInterval = globalThis.setInterval;
globalThis.setInterval = (fn, ms) => { timers.push(ms); return realSetInterval(fn, 30); };

new Function(readFileSync(path.join(client, "js", "services", "handoffNotice.js"), "utf-8"))();
await new Promise((r) => setTimeout(r, 400));
const said = toasts.map((t) => ({ title: t.title, note: t.note,
                                  actions: (t.actions || []).map((a) => (a.primary ? `*${a.label}` : a.label)) }));
toasts.find((t) => t.title === "Waiting on the network")?.actions?.[0]?.onSelect();
console.log(JSON.stringify({ said, opened, asked: asked.slice(0, 5), every: timers[0],
                             wired: typeof globalThis.PlexoraEnvelope.watchReads }));
process.exit(0);
