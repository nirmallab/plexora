/**
 * PlexoraRefusalCount -- how often the website turns a browser away, counted.
 *
 * The loader (services/shell.js) asks a Chrome or Edge too old for the
 * website's Python (no JSPI: pythonClient.js `unsupported`) to update, and
 * stops there: Python never starts, so the website's telemetry
 * (plexora/browser/telemetry.py, in the worker) never runs. This sends the
 * one count such a page has, `site.summary` / `refused` (plexora/telemetry/
 * schema.py), from the page itself, to the same service in the same upload
 * format: register for an install token, then one batch under it
 * (plexora/telemetry/uploader.py; backend/src/telemetry/ingest.ts).
 *
 * Under the website's rules (plexora/telemetry/config.py `resolve`), in their
 * order:
 *
 * - a build that names no service (shell.json without `telemetry`: every
 *   test's, the preview built by hand) sends nothing -- the loader does not
 *   even load this script;
 * - Do Not Track or Global Privacy Control: nothing;
 * - the service's pause (`disabled_until` in what it answered last time,
 *   kept here): nothing until it ends;
 * - the mode the visitor chose on this site before (Settings > Usage data,
 *   kept in the site's IndexedDB `plexora-telemetry`, `prefs.json`): "off"
 *   sends nothing; when that choice cannot be read (an error, or no answer
 *   in 2 s), nothing either -- the one place this is stricter than the
 *   worker, which then runs as if nothing was chosen;
 * - otherwise anonymous, whatever was chosen: the count has no diagnostics
 *   field.
 *
 * At most once per browser per (UTC) day, remembered in localStorage
 * (`plexora-refused`: the day, the pause, and the random install id the
 * service requires, which is this count's alone -- never the worker's);
 * where the page may keep nothing there, nothing is sent. Fire and forget:
 * keepalive requests, every error swallowed, nothing shown.
 *
 *   send({options, version, refused}) -> promise of an outcome word
 *       options: shell.js telemetryOptions(shell) ({endpoint, dnt, context:
 *       {os}}, or null); version: the site's; refused: unsupported()'s
 *       {browser, version}. Tests pass `nav`, `storage`, `idb`, `fetch`,
 *       `now` (ms) and `random(n)` -> n random bytes.
 *   storedMode(idb) -> promise of the chosen mode, or null when none was
 *
 * Classic script, `window.PlexoraRefusalCount`, loaded by the loader only
 * when it has refused a browser and the build names the service.
 */
(function (global) {
    "use strict";

    const SCHEMA = 1;
    const KEY = "plexora-refused";
    const DB = "plexora-telemetry";
    const PREFS = "prefs.json";
    const MAJOR_MAX = 999;
    const OS = ["mac", "windows", "linux", "other"];
    //: schema.py CLIENT_BLOCK `plexora_version`, anchored.
    const VERSION = /^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}(?:[a-z]+[0-9]*)?(?:\.dev[0-9]+)?(?:\+[0-9a-z.]{1,24})?$/;

    function randomBytes(n) {
        const bytes = new Uint8Array(n);
        global.crypto.getRandomValues(bytes);
        return bytes;
    }

    const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

    //: Do Not Track or Global Privacy Control, as shell.js reads them.
    function declined(nav) {
        return Boolean(nav && (nav.doNotTrack === "1" || nav.globalPrivacyControl === true))
            || global.doNotTrack === "1";
    }

    //: The mode chosen in Settings > Usage data on this site, from the
    //: worker's copy of telemetry's settings block; null when nothing was
    //: (no database: it is not created here). Rejects when it cannot tell.
    function storedMode(idb, wait = 2000) {
        if (!idb) return Promise.resolve(null);
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error("no answer")), wait);
            const done = (fn, value) => { clearTimeout(timer); fn(value); };
            let request;
            let fresh = false;
            try {
                request = idb.open(DB);
            } catch (error) {
                done(reject, error);
                return;
            }
            // None yet: nothing was chosen. Aborting the upgrade leaves no
            // database behind.
            request.onupgradeneeded = () => {
                fresh = true;
                try { request.transaction.abort(); } catch (error) { /* already ending */ }
            };
            request.onerror = (event) => {
                if (event && event.preventDefault) event.preventDefault();
                if (fresh) done(resolve, null);
                else done(reject, request.error || new Error("unreadable"));
            };
            request.onblocked = () => done(reject, new Error("blocked"));
            request.onsuccess = () => {
                const db = request.result;
                const finish = (fn, value) => { try { db.close(); } catch (error) { /* closed */ } done(fn, value); };
                if (!db.objectStoreNames.contains("files")) {
                    finish(resolve, null);
                    return;
                }
                let get;
                try {
                    get = db.transaction("files", "readonly").objectStore("files").get(PREFS);
                } catch (error) {
                    finish(reject, error);
                    return;
                }
                get.onerror = () => finish(reject, get.error || new Error("unreadable"));
                get.onsuccess = () => {
                    const value = get.result;
                    if (value === undefined || value === null) {
                        finish(resolve, null);
                        return;
                    }
                    let block = null;
                    try {
                        const text = typeof value === "string" ? value : new TextDecoder().decode(value);
                        block = JSON.parse(text);
                    } catch (error) {
                        block = null;  // as config.read_prefs: an unreadable file chose nothing
                    }
                    const mode = block && typeof block === "object" ? block.mode : null;
                    finish(resolve, typeof mode === "string" ? mode : null);
                };
            };
        });
    }

    //: localStorage, or null where the page may keep nothing.
    function storageOf(given) {
        if (given !== undefined) return given;
        try {
            return global.localStorage || null;
        } catch (error) {
            return null;
        }
    }

    function readMark(storage) {
        const raw = storage.getItem(KEY);
        if (!raw) return {};
        try {
            const mark = JSON.parse(raw);
            return mark && typeof mark === "object" && !Array.isArray(mark) ? mark : {};
        } catch (error) {
            return {};
        }
    }

    function writeMark(storage, mark) {
        storage.setItem(KEY, JSON.stringify(mark));
    }

    //: The batch: one `site.summary` row, in the hour it happened.
    function upload({ id, batch, client, refused, os, now }) {
        const browser = String(refused.browser || "").toLowerCase() === "edge" ? "edge" : "chrome";
        const major = Math.max(0, Math.min(MAJOR_MAX, Math.floor(Number(refused.version) || 0)));
        return {
            schema: SCHEMA,
            batch_id: batch,
            client: { install_id: id, ...client },
            events: [{ type: "site.summary", window: new Date(now).toISOString().slice(0, 13),
                       rows: [{ k: "refused", d: { browser, major, os }, n: 1 }] }],
        };
    }

    //: What the service is told about this browser: the site's, and nothing
    //: a desktop install would not say (schema.py CLIENT_BLOCK).
    function clientBlock(version, os) {
        const client = { os, runtime: "web", launch_mode: "web", deployment: "web",
                         install_kind: "web", mode: "anonymous" };
        if (typeof version === "string" && version.length <= 48 && VERSION.test(version)) {
            client.plexora_version = version;
        }
        return client;
    }

    async function send({ options = null, version = "", refused = null, nav = global.navigator,
                          storage, idb, fetch = global.fetch, now = Date.now(),
                          random = randomBytes } = {}) {
        const endpoint = options && options.endpoint ? String(options.endpoint).replace(/\/+$/, "") : "";
        if (!endpoint || !refused) return "off:build";
        if (options.dnt || declined(nav)) return "off:do_not_track";
        const kept = storageOf(storage);
        if (!kept) return "no_storage";
        let mark;
        try {
            mark = readMark(kept);
        } catch (error) {
            return "no_storage";
        }
        if (Number(mark.paused_until) > now / 1000) return "off:server";
        const today = new Date(now).toISOString().slice(0, 10);
        if (mark.day === today) return "counted_today";
        let chosen;
        try {
            chosen = await storedMode(idb !== undefined ? idb : (() => {
                try { return global.indexedDB || null; } catch (error) { return undefined; }
            })());
        } catch (error) {
            return "off:unknown_choice";
        }
        if (chosen === "off") return "off:settings";

        const id = typeof mark.id === "string" && /^[0-9a-f]{32}$/.test(mark.id) ? mark.id : hex(random(16));
        const next = { id, day: today };
        try {
            writeMark(kept, next);
        } catch (error) {
            return "no_storage";
        }
        const pause = (answer) => {
            const until = Number(answer && answer.config && answer.config.disabled_until);
            if (until > now / 1000) {
                try { writeMark(kept, { ...next, paused_until: until }); } catch (error) { /* kept nothing */ }
                return true;
            }
            return false;
        };
        const post = (path, body, headers = {}) => {
            const init = { method: "POST", mode: "cors", credentials: "omit", cache: "no-store",
                           referrerPolicy: "no-referrer", body,
                           headers: { "Content-Type": "application/json", ...headers } };
            // keepalive: the count outlives a tab closed at once. An old
            // Chrome that cannot preflight one is asked again without it.
            return fetch(endpoint + path, { ...init, keepalive: true }).catch(() => fetch(endpoint + path, init));
        };
        const read = (response) => response.json().catch(() => null);

        const os = OS.includes(options.context && options.context.os) ? options.context.os : "other";
        const client = clientBlock(version, os);
        try {
            const registered = await post("/v1/telemetry/register",
                                          JSON.stringify({ schema: SCHEMA, install_id: id, client }));
            const answer = await read(registered);
            if (pause(answer)) return "paused";
            const token = answer && answer.install_token;
            if (registered.status !== 200 || typeof token !== "string" || !token || token.length > 1024) {
                return "unregistered";
            }
            const batch = upload({ id, batch: hex(random(16)), client, refused, os, now });
            const sent = await post("/v1/telemetry/events", JSON.stringify(batch),
                                    { Authorization: `Bearer ${token}` });
            pause(await read(sent));
            return sent.status >= 200 && sent.status < 300 ? "sent" : "failed";
        } catch (error) {
            return "failed";
        }
    }

    global.PlexoraRefusalCount = Object.freeze({ send, storedMode, KEY, DB });
})(typeof window !== "undefined" ? window : globalThis);
