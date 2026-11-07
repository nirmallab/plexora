// Telemetry's files on the website, kept across visits in the site's
// IndexedDB (plexora/browser/telemetry.py says what they are and why).
//
// Python keeps telemetry's settings block (`prefs.json`: the mode, whether the
// notice was shown, the browser's random install id) and its queue
// (`queue.sqlite`: the counts not yet sent, the install token, the uploader's
// backoff and the server's config) under /telemetry, in the worker's memory.
// This copies them in from IndexedDB before telemetry starts and back after
// each tick and each Settings change: database `plexora-telemetry`, object
// store `files`, one record of bytes per file -- never in the projects folder,
// which the desktop app may share. Only the tab holding the Web Lock
// `plexora-telemetry-queue` restores and saves the queue; another tab keeps its
// own in memory (two copies of one queue would send its rows twice), and
// saves the settings block only.
//
//   createTelemetryStore({FS, kv, locks, home}) -> {restore(), save(), kept}
//   indexedKv(name) -> {get(key), put(key, bytes)}   the IndexedDB store
//
// An ES module, imported by workers/pythonWorker.js.

export const FILES = ["prefs.json", "queue.sqlite"];
export const PREFS = "prefs.json";
export const LOCK = "plexora-telemetry-queue";

/** The site's IndexedDB database `name`, one object store `files` of bytes by name. */
export function indexedKv(name = "plexora-telemetry") {
    let opened = null;
    const open = () => {
        if (!opened) {
            opened = new Promise((resolve, reject) => {
                const request = indexedDB.open(name, 1);
                request.onupgradeneeded = () => request.result.createObjectStore("files");
                request.onsuccess = () => {
                    const db = request.result;
                    db.onversionchange = () => { db.close(); opened = null; };
                    resolve(db);
                };
                request.onerror = () => reject(request.error);
                request.onblocked = () => reject(new Error("telemetry's store is blocked by another tab"));
            });
            opened.catch(() => { opened = null; });
        }
        return opened;
    };
    const run = async (mode, fn) => {
        const db = await open();
        return new Promise((resolve, reject) => {
            const tx = db.transaction("files", mode);
            const request = fn(tx.objectStore("files"));
            tx.oncomplete = () => resolve(request ? request.result : undefined);
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error || new Error("aborted"));
        });
    };
    return {
        get: (key) => run("readonly", (files) => files.get(key)),
        put: (key, bytes) => run("readwrite", (files) => files.put(bytes, key)).then(() => undefined),
    };
}

//: Whether this tab holds the queue's lock (held for the worker's life);
//: true where there are no Web Locks (one tab is all that can be told).
function holdLock(locks) {
    if (!locks || typeof locks.request !== "function") return Promise.resolve(true);
    return new Promise((resolve) => {
        locks.request(LOCK, { ifAvailable: true }, (lock) => {
            resolve(Boolean(lock));
            return lock ? new Promise(() => {}) : undefined;
        }).catch(() => resolve(false));
    });
}

export function createTelemetryStore({ FS, kv, locks = globalThis.navigator?.locks, home = "/telemetry" }) {
    //: What was last read or written per file, so an unchanged file is not
    //: written again (the queue changes only when something was counted).
    const last = new Map();
    const store = {
        kept: false,
        /** The files into `home`, the queue only where this tab holds the lock. */
        async restore() {
            store.kept = await holdLock(locks);
            FS.mkdirTree(home);
            for (const name of store.kept ? FILES : [PREFS]) {
                const bytes = await kv.get(name).catch(() => null);
                if (!bytes || !bytes.byteLength) continue;
                const data = new Uint8Array(bytes);
                FS.writeFile(`${home}/${name}`, data);
                last.set(name, data);
            }
            return store.kept;
        },
        /** What changed since, back into IndexedDB. Resolves the names written. */
        async save() {
            const written = [];
            for (const name of store.kept ? FILES : [PREFS]) {
                let data;
                try {
                    data = FS.readFile(`${home}/${name}`);
                } catch (error) {
                    continue;  // not made yet (telemetry off, or nothing chosen)
                }
                const before = last.get(name);
                if (before && before.length === data.length && before.every((b, i) => b === data[i])) continue;
                await kv.put(name, data);
                last.set(name, data);
                written.push(name);
            }
            return written;
        },
    };
    return store;
}
