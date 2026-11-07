/**
 * The website's notice of what a browser tab handles well
 * (client src/js/services/handoffNotice.js `judge`) against the shipped
 * limits (client/src/envelope.json). Prints JSON; tests/test_envelope.py
 * asserts on it.
 *
 * Run:  node tests/js/envelope_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const client = path.join(REPO, "plexora", "client", "src");
// A classic script: run it with globalThis as its window.
new Function(readFileSync(path.join(client, "js", "services", "handoffNotice.js"), "utf-8"))();
const limits = JSON.parse(readFileSync(path.join(client, "envelope.json"), "utf-8"));
const judge = globalThis.PlexoraEnvelope.judge;

// check(): the limits fetched, the project's size asked, the toast shown once,
// and "Continue here" remembered.
const store = new Map();
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
globalThis.fetch = async () => ({ ok: true, json: async () => limits });
const toasts = [];
const app = { fetch: async () => ({ ok: true, json: async () => ({ rows: 2_000_000, columns: 90 }) }),
              PlexoraToast: { show: (options) => toasts.push(options) } };
await globalThis.PlexoraEnvelope.check("big", app);
await globalThis.PlexoraEnvelope.check("big", app);          // once per project per tab
toasts[0]?.actions?.find((a) => a.label === "Continue here")?.onSelect();
const remembered = [...store.keys()];

console.log(JSON.stringify({
    toasts: toasts.map((t) => ({ title: t.title, actions: (t.actions || []).map((a) => a.label) })),
    remembered,
    orion: judge({ rows: 1_260_000, columns: 29 }, limits),
    wide: judge({ rows: 1_260_000, columns: 90 }, limits),
    orionOnFourGb: judge({ rows: 1_260_000, columns: 29 }, limits, { memoryGb: 4 }),
    noTable: judge({ rows: null, columns: null }, limits),
}));
