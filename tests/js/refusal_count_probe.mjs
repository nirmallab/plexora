/**
 * The count of browsers the website turns away (client src/js/services/
 * refusalCount.js): its consent rules -- the build's switch, Do Not Track and
 * Global Privacy Control, the visitor's earlier choice in the site's
 * IndexedDB, the service's pause -- and its once-a-day rule, against a
 * stand-in fetch, localStorage and IndexedDB. Prints JSON (every case's
 * outcome, the requests it made, the bodies); tests/test_refusal_count.py
 * asserts on it and validates the bodies against plexora/telemetry/schema.py.
 *
 * Run:  node tests/js/refusal_count_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
new Function(readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "refusalCount.js"), "utf-8"))();
const { send, storedMode, KEY } = globalThis.PlexoraRefusalCount;

const ENDPOINT = "https://analytics.example";
const OPTIONS = { endpoint: `${ENDPOINT}/`, dnt: false, context: { os: "windows", cpus: 8 } };
const REFUSED = { browser: "Chrome", version: 131 };
const NOW = Date.parse("2026-11-07T14:20:00Z");
const DAY = 86400 * 1000;

function storage({ getThrows = false, setThrows = false } = {}) {
    const map = new Map();
    return {
        map,
        getItem: (key) => { if (getThrows) throw new Error("SecurityError"); return map.has(key) ? map.get(key) : null; },
        setItem: (key, value) => { if (setThrows) throw new Error("QuotaExceededError"); map.set(key, String(value)); },
    };
}

//: The site's IndexedDB as the worker leaves it: `prefs` the settings block
//: (an object, stored as JSON bytes), `exists` false for a browser that never
//: ran the website's Python, `fail` an open that errors, `hang` one that
//: never answers.
function idb({ exists = true, prefs, fail = false, hang = false } = {}) {
    const seen = { aborted: false };
    return {
        seen,
        open() {
            const request = {};
            setTimeout(() => {
                if (hang) return;
                if (fail) {
                    request.error = new Error("UnknownError");
                    request.onerror?.({ preventDefault() {} });
                    return;
                }
                if (!exists) {
                    request.transaction = { abort: () => { seen.aborted = true; } };
                    request.onupgradeneeded?.();
                    request.error = new Error("AbortError");
                    request.onerror?.({ preventDefault() {} });
                    return;
                }
                request.result = {
                    objectStoreNames: { contains: (name) => name === "files" },
                    close() {},
                    transaction: () => ({ objectStore: () => ({ get(key) {
                        const get = {};
                        setTimeout(() => {
                            get.result = key === "prefs.json" && prefs !== undefined
                                ? new TextEncoder().encode(typeof prefs === "string" ? prefs : JSON.stringify(prefs)) : undefined;
                            get.onsuccess?.();
                        });
                        return get;
                    } }) }),
                };
                request.onsuccess?.();
            });
            return request;
        },
    };
}

//: A stand-in for the Worker: register answers a token (and `config`),
//: events a 202. `fail`: every request rejects; `noKeepalive`: a keepalive
//: request rejects, as an old Chrome's preflighted one did.
function service({ config = { level_max: "diagnostics", disabled_until: 0 }, fail = false, noKeepalive = false } = {}) {
    const calls = [];
    const fetch = async (url, init) => {
        calls.push({ url, method: init.method, keepalive: Boolean(init.keepalive), mode: init.mode,
                     credentials: init.credentials, headers: init.headers, body: JSON.parse(init.body) });
        if (fail) throw new TypeError("Failed to fetch");
        if (noKeepalive && init.keepalive) throw new TypeError("Preflight request for request with keepalive specified is currently not supported");
        const path = new URL(url).pathname;
        if (path === "/v1/telemetry/register") {
            return { status: 200, json: async () => ({ schema: 1, install_token: "PLEXORAT1.a.b", expires: NOW / 1000 + 86400 * 90, config }) };
        }
        return { status: 202, json: async () => ({ schema: 1, accepted: 1, config }) };
    };
    return { calls, fetch };
}

const bytes = (() => {
    let n = 0;
    return (count) => Uint8Array.from({ length: count }, () => (n += 7) % 256);
})();

async function run(name, given = {}) {
    const svc = given.service || service();
    const kept = given.storage === undefined ? storage() : given.storage;
    const outcome = await send({ options: OPTIONS, version: "0.0.36", refused: REFUSED, nav: {},
                                 storage: kept, idb: idb({ exists: false }), fetch: svc.fetch, now: NOW,
                                 random: bytes, ...given.args });
    let mark = null;
    try { mark = kept && kept.map ? JSON.parse(kept.map.get(KEY) || "null") : null; } catch (error) { mark = "unreadable"; }
    const body = (suffix) => (svc.calls.filter((c) => c.url.endsWith(suffix)).pop() || {}).body || null;
    return { name, outcome, requests: svc.calls.map((c) => `${c.method} ${new URL(c.url).pathname}${c.keepalive ? " keepalive" : ""}`),
             calls: svc.calls, mark, register: body("/register"), batch: body("/events") };
}

const out = { cases: {} };
const record = (row) => { out.cases[row.name] = row; return row; };

// On: one registration and one batch, from a browser that never ran Python.
const kept = storage();
const svc = service();
const first = record(await run("on", { storage: kept, service: svc }));
// The same browser again that day, and the next day.
record(await run("sameDay", { storage: kept, args: { now: NOW + 3 * 3600 * 1000 } }));
record(await run("nextDay", { storage: kept, args: { now: NOW + DAY } }));
out.idAbortedFresh = (() => { const db = idb({ exists: false }); return storedMode(db).then((mode) => ({ mode, aborted: db.seen.aborted })); })();
out.idAbortedFresh = await out.idAbortedFresh;

// The build's switch: no service named.
record(await run("noEndpoint", { args: { options: { ...OPTIONS, endpoint: "" } } }));
record(await run("noOptions", { args: { options: null } }));
// Do Not Track, Global Privacy Control.
record(await run("dnt", { args: { options: { ...OPTIONS, dnt: true } } }));
record(await run("navDnt", { args: { nav: { doNotTrack: "1" } } }));
record(await run("gpc", { args: { nav: { globalPrivacyControl: true } } }));
// The visitor's earlier choice on this site.
record(await run("prefsOff", { args: { idb: idb({ prefs: { mode: "off", install_id: "f".repeat(32), notice_shown: true } }) } }));
record(await run("prefsDiagnostics", { args: { idb: idb({ prefs: { mode: "diagnostics" } }) } }));
record(await run("prefsAnonymous", { args: { idb: idb({ prefs: { mode: "anonymous" } }) } }));
record(await run("prefsNone", { args: { idb: idb({ prefs: { install_id: "e".repeat(32) } }) } }));
record(await run("prefsBroken", { args: { idb: idb({ prefs: "{not json" }) } }));
record(await run("noIndexedDb", { args: { idb: null } }));
record(await run("idbFails", { args: { idb: idb({ fail: true }) } }));
record(await run("idbHangs", { args: { idb: idb({ hang: true }) } }));
// Storage the page may not use.
record(await run("noStorage", { storage: null }));
record(await run("storageThrows", { storage: storage({ getThrows: true }) }));
record(await run("storageReadOnly", { storage: storage({ setThrows: true }) }));
// The service's pause: said at registration, kept, and honoured until it ends.
const pausedStore = storage();
const until = NOW / 1000 + 2 * 86400;
record(await run("paused", { storage: pausedStore, service: service({ config: { disabled_until: until } }) }));
record(await run("pausedNextDay", { storage: pausedStore, args: { now: NOW + DAY } }));
record(await run("pauseOver", { storage: pausedStore, args: { now: NOW + 3 * DAY } }));
// Failures are swallowed; an old Chrome's keepalive refusal is asked again without it.
record(await run("offline", { service: service({ fail: true }) }));
record(await run("noKeepalive", { service: service({ noKeepalive: true }) }));
// Edge, a version outside the bound, a site version the schema does not take.
record(await run("edgeClamped", { args: { refused: { browser: "Edge", version: 1234 }, version: "dev build",
                                          options: { ...OPTIONS, context: { os: "plan9" } } } }));

out.first = { register: first.calls[0], events: first.calls[1] };
out.idsAcross = { on: first.mark?.id, nextDay: out.cases.nextDay.mark?.id };
for (const row of Object.values(out.cases)) delete row.calls;
console.log(JSON.stringify(out));
