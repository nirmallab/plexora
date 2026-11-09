/**
 * PlexoraDerived -- one index of the copies the website makes from the
 * user's files, and one order to look for them in.
 *
 * The page builds a flat image's pyramidized copy and a flat mask's label
 * pyramid itself (services/browserPyramid.js). Such a copy can be in one of
 * four homes, and before this index each was looked in by its own rule, so a
 * copy in one home was rebuilt when the rule of another applied:
 *   beside     a folder the user chose for it (beside the original, usually);
 *   plexora    Plexora's folder, Documents/Plexora/pyramids/<key>/ -- the
 *              place the user chose for their projects (services/browserStore.js);
 *   opfs       this site's own storage, OPFS plexora/derived/<key>/;
 *   indexeddb  a browser that could write neither keeps it in IndexedDB.
 * Every copy is recorded here when it is built or found, in IndexedDB
 * `plexora-copies` (version 2: the `derived` store, keyed by the path Python
 * knows it by, /derived/<key>/<name>; version 1's `copies` records are moved
 * in when the database is first opened at version 2):
 *   {path, key, name, kind: "image"|"labels", home, folder? (beside: the
 *    folder's handle), blob? (indexeddb), bytes, builtBy, made,
 *    movedFrom? (a copy moved out of the site's storage), moveFailed?}
 *
 * `prepare()` runs at boot, between the store's and Python's start
 * (services/shell.js), and needs no click: it resolves every record it can,
 * drops those whose copy is gone (Safari clears a site's storage after a week
 * unused), and records the finished copies it finds in the site's storage and
 * in Plexora's folder that nobody recorded. A copy in a folder the browser
 * will not open again without a click is *waiting*: Python counts it as there
 * (providers/browser.py `derived_status`), so the project neither rebuilds it
 * nor asks for it to be found, and one click (`reconnect`, from the loader's
 * "Open folder" or Settings › Data) brings it back.
 *
 * The store's choice is the copies' home: where projects are kept in
 * Plexora's folder, a copy built in the site's storage (that visit had not
 * opened the folder) is moved there in the background (`migrate`), and the
 * site's copy removed on the next visit, when nothing has it open.
 *
 *   prepare()               -> {ready, waiting}: at boot, no click
 *   restore()               -> the paths mounted for Python (after it started)
 *   lookup(key, name)       -> {file, home, trusted} or null: every home, in order
 *   record(entry, file)     a copy built or found: remembered, ready
 *   copyTo(path, folder)    a held copy written into a folder the user chose
 *   reconnect()             from a click: the waiting copies, asked for again
 *   requestFolders()        from a click before Python starts: the same, by the records alone
 *   migrate()               the site's copies moved into Plexora's folder
 *   list() / usage()        what Settings › Data shows
 *   move(path) / remove(path)   Settings › Data's actions (`remove`: {reload})
 *
 * Classic script, `window.PlexoraDerived`; needs PlexoraStore, and
 * PlexoraPython / PlexoraPicks once Python has started.
 */
(function (global) {
    "use strict";

    const DB = "plexora-copies";
    const VERSION = 2;
    const STORE = "derived";
    //: Version 1's store: {path, key, name, folder | blob, bytes, made}.
    const OLD = "copies";
    const ROOT = "/derived";
    //: How long after Python has started the site's copies start moving into
    //: Plexora's folder: never while a project is opening.
    const MIGRATE_AFTER_MS = 20000;
    //: The records, by path, as kept in IndexedDB.
    const records = new Map();
    //: What this visit knows of each: {state: "ready"|"waiting"|"missing", file, why}.
    const live = new Map();
    let prepared = null;
    let migrating = null;

    const labels = (name) => /\.labels\.pyramid\.ome\.tiff?$/i.test(String(name || ""));
    const kindOf = (name) => (labels(name) ? "labels" : "image");
    const pathOf = (key, name) => `${ROOT}/${key}/${name}`;

    //: A version 1 record as version 2 keeps it. Version 1 kept a folder's
    //: handle for both folders; Plexora's own is the copy's key folder
    //: (pyramids/<key>), any other the folder the user chose.
    function fromV1(record) {
        const home = record.blob ? "indexeddb"
            : record.folder && record.folder.name === record.key ? "plexora" : "beside";
        const out = { ...record, home, kind: record.kind || kindOf(record.name), builtBy: "page" };
        if (home === "plexora") delete out.folder; // reached through the store's folder, granted with it
        return out;
    }

    function database() {
        return new Promise((resolve, reject) => {
            const request = global.indexedDB.open(DB, VERSION);
            request.onupgradeneeded = (event) => {
                const db = request.result;
                const tx = request.transaction;
                if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: "path" });
                if (event.oldVersion >= 1 && db.objectStoreNames.contains(OLD)) {
                    const old = tx.objectStore(OLD).getAll();
                    old.onsuccess = () => {
                        const into = tx.objectStore(STORE);
                        for (const record of old.result || []) {
                            if (record && record.path) into.put(fromV1(record));
                        }
                        db.deleteObjectStore(OLD);
                    };
                }
            };
            request.onsuccess = () => resolve(request.result);
            // Blocked while another tab of an older site holds version 1 open:
            // the open goes on once it closes (it closes after each transaction).
            request.onerror = () => reject(request.error);
        });
    }

    async function transact(mode, work) {
        const db = await database();
        try {
            return await new Promise((resolve, reject) => {
                const tx = db.transaction(STORE, mode);
                const result = work(tx.objectStore(STORE));
                tx.oncomplete = () => resolve(result && "result" in result ? result.result : result);
                tx.onerror = () => reject(tx.error);
            });
        } finally {
            db.close();
        }
    }

    //: True when kept: a database that will not take it (no room, a private
    //: window) leaves the copy for this visit only.
    const put = (record) => transact("readwrite", (store) => store.put(record)).then(() => true, () => false);
    const drop = (path) => transact("readwrite", (store) => store.delete(path)).catch(() => null);

    async function opfsDerived() {
        const root = await global.navigator.storage.getDirectory();
        const plexora = await root.getDirectoryHandle("plexora", { create: true });
        return plexora.getDirectoryHandle("derived", { create: true });
    }

    //: The finished copy under `key` in the site's storage, or null: its
    //: manifest is written last (workers/pyramidWorker.js), and the file must
    //: be the size it says. A copy moved to Plexora's folder has no manifest.
    async function opfsCopy(dir, name) {
        try {
            const manifest = JSON.parse(await (await (await dir.getFileHandle("manifest.json")).getFile()).text());
            if (name && manifest.name !== name) return null;
            const file = await (await dir.getFileHandle(manifest.name)).getFile();
            return file.size === manifest.bytes ? { manifest, file } : null;
        } catch (error) {
            return null;
        }
    }

    const store = () => global.PlexoraStore || null;
    const pyramids = () => (store() && store().pyramids ? store().pyramids() : null);
    //: Projects kept in Plexora's folder, whether or not it is open this visit.
    const folderChosen = () => {
        const current = store() ? store().current() : null;
        return Boolean(current && (current.kind === "folder" || current.notOpened));
    };

    async function inPyramids(key, name) {
        const folder = pyramids();
        if (!folder) return null;
        try {
            return await (await (await folder.getDirectoryHandle(key)).getFileHandle(name)).getFile();
        } catch (error) {
            return null;
        }
    }

    async function granted(handle, ask) {
        const options = { mode: "read" };
        try {
            if (await handle.queryPermission(options) === "granted") return true;
            return Boolean(ask) && await handle.requestPermission(options) === "granted";
        } catch (error) {
            return false;
        }
    }

    //: The copy a record names, read now if the browser lets the page:
    //: {state, file?, why?}. `ask`: from a click, the browser may ask the user.
    async function resolveRecord(record, ask = false) {
        const sized = (file) => (!record.bytes || file.size === record.bytes
            ? { state: "ready", file } : { state: "missing", why: "changed since it was made" });
        if (record.home === "indexeddb") {
            if (!record.blob || record.blob.size !== record.bytes) return { state: "missing", why: "not in this browser" };
            return { state: "ready", file: new File([record.blob], record.name, { lastModified: record.made || 0 }) };
        }
        if (record.home === "opfs") {
            const found = await opfsDerived().then((dir) => dir.getDirectoryHandle(record.key))
                .then((dir) => opfsCopy(dir, record.name)).catch(() => null);
            return found ? { state: "ready", file: found.file } : { state: "missing", why: "cleared by the browser" };
        }
        if (record.home === "plexora") {
            if (!pyramids() && ask && store() && store().reopen) await store().reopen().catch(() => null);
            if (!pyramids()) {
                return folderChosen() ? { state: "waiting", why: "Plexora's folder is not open" }
                    : { state: "missing", why: "kept in Plexora's folder, which this browser no longer uses" };
            }
            const file = await inPyramids(record.key, record.name);
            return file ? sized(file) : { state: "missing", why: "not in Plexora's folder" };
        }
        if (record.home === "beside" && record.folder) {
            if (!await granted(record.folder, ask)) return { state: "waiting", why: `“${record.folder.name}” is not open` };
            try {
                return sized(await (await record.folder.getFileHandle(record.name)).getFile());
            } catch (error) {
                return { state: "missing", why: `not in “${record.folder.name}”` };
            }
        }
        return { state: "missing", why: "nowhere to look" };
    }

    function remember(record, state) {
        records.set(record.path, record);
        live.set(record.path, state);
    }

    async function forget(path) {
        records.delete(path);
        live.delete(path);
        await drop(path);
    }

    /**
     * At boot, with no click: every recorded copy resolved where the browser
     * lets the page read it, copies gone from the site's storage forgotten,
     * finished copies nobody recorded (the site's storage, Plexora's folder)
     * recorded, and the site's copies a move finished removed. Once per visit.
     */
    function prepare() {
        if (prepared) return prepared;
        prepared = (async () => {
            let kept = [];
            try {
                kept = await transact("readonly", (store) => store.getAll());
            } catch (error) {
                kept = []; // no IndexedDB (a private window in an old Safari): nothing recorded
            }
            for (const record of kept) records.set(record.path, record);
            // The site's storage: every finished copy, recorded or not.
            const seen = new Set();
            try {
                const derived = await opfsDerived();
                for await (const [key, handle] of derived.entries()) {
                    if (handle.kind !== "directory") continue;
                    const found = await opfsCopy(handle, null);
                    if (!found) {
                        // A copy moved to Plexora's folder on an earlier visit:
                        // nothing mounts it now (no manifest), so it goes.
                        const moved = [...records.values()].some((r) => r.key === key && r.movedFrom === "opfs"
                                                                       && r.home !== "opfs");
                        if (moved) await derived.removeEntry(key, { recursive: true }).catch(() => null);
                        continue;
                    }
                    const path = pathOf(key, found.manifest.name);
                    seen.add(path);
                    const record = records.get(path);
                    if (record && record.home !== "opfs") continue; // recorded elsewhere: that one is the copy
                    if (!record) {
                        const made = { path, key, name: found.manifest.name, kind: found.manifest.kind || kindOf(found.manifest.name),
                                       home: "opfs", bytes: found.file.size, builtBy: "page", made: found.file.lastModified };
                        records.set(path, made);
                        await put(made);
                    }
                    live.set(path, { state: "ready", file: found.file });
                }
            } catch (error) {
                // No OPFS (a private window, an old Safari): nothing was built there.
            }
            // Plexora's folder, when open: copies nobody here recorded (made
            // by another browser on this computer, or before this index).
            const folder = pyramids();
            if (folder) {
                try {
                    for await (const [key, dir] of folder.entries()) {
                        if (dir.kind !== "directory") continue;
                        for await (const [name, handle] of dir.entries()) {
                            if (handle.kind !== "file" || name.endsWith(".crswap") || name.startsWith(".")) continue;
                            const path = pathOf(key, name);
                            const record = records.get(path);
                            if (record && record.home !== "opfs") continue;
                            const file = await handle.getFile();
                            if (record && record.bytes && file.size !== record.bytes) continue; // not that copy
                            if (record) {
                                // Moved here by a visit that stopped before it
                                // said so: the site's copy is the one to go.
                                const own = await opfsDerived().then((d) => d.getDirectoryHandle(key)).catch(() => null);
                                if (own) await own.removeEntry("manifest.json").catch(() => null);
                            }
                            const made = { path, key, name, kind: kindOf(name), home: "plexora", bytes: file.size,
                                           builtBy: "page", made: file.lastModified,
                                           ...(record ? { movedFrom: "opfs" } : {}) };
                            records.set(path, made);
                            await put(made);
                            live.set(path, { state: "ready", file });
                        }
                    }
                } catch (error) {
                    // A folder the browser stopped letting the page read: its records wait.
                }
            }
            for (const record of [...records.values()]) {
                if (live.has(record.path)) continue;
                if (record.home === "opfs" && !seen.has(record.path)) {
                    await forget(record.path); // cleared by the browser
                    continue;
                }
                const state = await resolveRecord(record, false);
                if (state.state === "missing" && (record.home === "indexeddb" || record.home === "opfs")) {
                    await forget(record.path);
                    continue;
                }
                live.set(record.path, state);
            }
            return summary();
        })();
        prepared.catch(() => null);
        return prepared;
    }

    function summary() {
        const out = { ready: [], waiting: [], missing: [] };
        for (const [path, state] of live) out[state.state]?.push(path);
        return out;
    }

    function adopt(path, file) {
        if (global.PlexoraPicks && global.PlexoraPicks.adopt) global.PlexoraPicks.adopt(path, file);
    }

    //: Mount for Python, and register as the page's file (the browser node
    //: serves its tiles). The site's copies the worker mounted itself at boot
    //: (workers/pythonWorker.js `mountDerived`); a folder already mounted is
    //: left as it is there.
    async function mountReady(path) {
        const state = live.get(path);
        const record = records.get(path);
        if (!state || state.state !== "ready" || !state.file || !record) return false;
        if (record.home !== "opfs" && global.PlexoraPython) {
            await global.PlexoraPython.mount(`${ROOT}/${record.key}`, [state.file]);
        }
        adopt(path, state.file);
        return true;
    }

    /**
     * After Python has started (services/shell.js, through PlexoraPyramid):
     * every ready copy mounted and registered. Then, where projects are kept in
     * Plexora's folder, the site's copies move there in the background.
     */
    async function restore() {
        await prepare();
        const out = [];
        for (const path of [...live.keys()]) {
            try {
                if (await mountReady(path)) out.push(path);
            } catch (error) {
                live.set(path, { state: "missing", why: String(error && error.message || error) });
            }
        }
        if (pyramids()) global.setTimeout(() => migrate().catch(() => null), MIGRATE_AFTER_MS);
        return out;
    }

    /**
     * A copy of `key`/`name` wherever one is, before anything is built, in
     * this order: the index, Plexora's folder (open), the site's storage,
     * IndexedDB. {file, home, trusted}: `trusted` when this page made or
     * recorded it (a file merely found under the name in Plexora's folder is
     * Python's to judge first, `existing`), or null. `browserOnly`: only the
     * browser's own copies (Python refused the one in a folder).
     */
    async function lookup(key, name, { browserOnly = false } = {}) {
        await prepare();
        const path = pathOf(key, name);
        const known = live.get(path);
        const record = records.get(path);
        const own = (home) => home === "opfs" || home === "indexeddb";
        if (known && known.state === "ready" && known.file && record && (!browserOnly || own(record.home))) {
            return { file: known.file, home: record.home, trusted: true, folder: record.folder || null };
        }
        const there = browserOnly ? null : await inPyramids(key, name);
        if (there) return { file: there, home: "plexora", trusted: false };
        try {
            const found = await opfsCopy(await (await opfsDerived()).getDirectoryHandle(key), name);
            if (found) return { file: found.file, home: "opfs", trusted: true };
        } catch (error) { /* not in the site's storage */ }
        if (record && record.home === "indexeddb") {
            const state = await resolveRecord(record);
            if (state.state === "ready") return { file: state.file, home: "indexeddb", trusted: true };
        }
        return null;
    }

    /**
     * A copy built or found: {path, key, name, kind, home, folder?, blob?,
     * bytes, builtBy}. Ready this visit with `file`; the record, or null when
     * the database would not keep it.
     */
    async function record(entry, file) {
        await prepare().catch(() => null);
        const was = records.get(entry.path);
        const made = { builtBy: "page", made: Date.now(), ...entry };
        if (!made.kind) made.kind = kindOf(made.name);
        if (made.bytes == null) made.bytes = file ? file.size : null;
        if (!made.folder) delete made.folder;
        if (was && was.movedFrom && !made.movedFrom) made.movedFrom = was.movedFrom;
        remember(made, file ? { state: "ready", file } : { state: "missing" });
        return (await put(made)) ? made : null;
    }

    /**
     * From a click: the copies waiting on a folder the browser asks about
     * again, asked for, mounted where granted. {mounted, waiting}. `ask`
     * false: only looked at again, with no question (Plexora's folder was
     * just opened by another click).
     */
    async function reconnect({ ask = true } = {}) {
        await prepare();
        const mounted = [];
        for (const [path, state] of [...live]) {
            if (state.state !== "waiting") continue;
            const record = records.get(path);
            const now = await resolveRecord(record, ask);
            live.set(path, now);
            if (now.state === "ready" && await mountReady(path).catch(() => false)) mounted.push(path);
        }
        if (pyramids()) migrate().catch(() => null);
        return { mounted, waiting: summary().waiting };
    }

    /**
     * From the loader's "Open folder" click, before `prepare` has run: the
     * folders the recorded copies beside their originals are in, asked for in
     * the same click, so `prepare` finds them granted. Best effort: the
     * browser's activation may have lapsed while the user answered for the
     * projects' folder.
     */
    async function requestFolders() {
        let kept = [];
        try {
            kept = await transact("readonly", (store) => store.getAll());
        } catch (error) {
            return 0;
        }
        let asked = 0;
        const done = new Set();
        for (const record of kept) {
            if (record.home !== "beside" || !record.folder || done.has(record.folder.name)) continue;
            done.add(record.folder.name);
            if (await granted(record.folder, true)) asked += 1;
        }
        return asked;
    }

    async function copyInto(dir, name, file) {
        const there = await dir.getFileHandle(name).then((h) => h.getFile()).catch(() => null);
        if (there) {
            // Never written over: the same copy when it is the same size, else
            // somebody's own file.
            if (there.size === file.size) return there;
            throw new Error(`a different “${name}” is already there`);
        }
        const handle = await dir.getFileHandle(name, { create: true });
        const writable = await handle.createWritable();
        try {
            await file.stream().pipeTo(writable);
        } catch (error) {
            try { await writable.abort(); } catch (ignored) { /* aborted already by pipeTo */ }
            await dir.removeEntry(name).catch(() => null);
            throw error;
        }
        return handle.getFile();
    }

    //: One of the site's copies (or IndexedDB's) moved into Plexora's
    //: folder -- or, `into` a folder the user chose for it, into that one:
    //: written, checked, recorded there. The site's copy loses its manifest
    //: now -- no later boot mounts it -- and its file goes at the next visit's
    //: `prepare` (this visit's Python may be reading it).
    async function moveOne(path, into = null) {
        const record = records.get(path);
        const state = live.get(path);
        const folder = into ? null : pyramids();
        if (!record || !(into || folder) || !state || state.state !== "ready" || !state.file) return false;
        if (record.home !== "opfs" && record.home !== "indexeddb") return false;
        try {
            const dir = into || await folder.getDirectoryHandle(record.key, { create: true });
            const file = await copyInto(dir, record.name, state.file);
            if (file.size !== state.file.size) throw new Error("the copy in the folder is not whole");
            const from = record.home;
            const moved = { ...record, home: into ? "beside" : "plexora", movedFrom: from, made: record.made || Date.now(),
                            ...(into ? { folder: into } : {}) };
            delete moved.blob;
            delete moved.moveFailed;
            if (!into) delete moved.folder;
            records.set(path, moved);
            await put(moved);
            if (from === "opfs") {
                const own = await (await opfsDerived()).getDirectoryHandle(record.key).catch(() => null);
                if (own) await own.removeEntry("manifest.json").catch(() => null);
            }
            // This visit keeps reading the file it has; the next reads the folder's.
            return true;
        } catch (error) {
            const why = error && error.name === "QuotaExceededError" ? "the disk is full"
                : error && error.name === "NotAllowedError" ? "the browser did not allow it"
                    : String(error && error.message || error);
            const failed = { ...record, moveFailed: why };
            records.set(path, failed);
            await put(failed);
            return false;
        }
    }

    //: The import's question chose a folder for a copy the browser already
    //: holds: nothing is built again, the held copy is written there.
    async function copyTo(path, folder) {
        await prepare();
        return moveOne(path, folder);
    }

    /**
     * Where projects are kept in Plexora's folder: every copy still in the
     * site's storage (or IndexedDB) moved there, one at a time. Idempotent;
     * a copy that cannot move (a full disk, a refused write) stays where it
     * is and says why (Settings › Data).
     */
    function migrate() {
        if (migrating) return migrating;
        migrating = (async () => {
            await prepare();
            if (!pyramids()) return 0;
            let moved = 0;
            for (const record of [...records.values()]) {
                if ((record.home === "opfs" || record.home === "indexeddb") && !record.moveFailed) {
                    if (await moveOne(record.path)) moved += 1;
                }
            }
            return moved;
        })().finally(() => { migrating = null; });
        return migrating;
    }

    //: Where a copy is, as Settings › Data says it.
    function placeOf(record) {
        if (record.home === "beside") return record.folder ? `“${record.folder.name}”` : "beside the original";
        if (record.home === "plexora") {
            const current = store() ? store().current() : {};
            return current.label || current.notOpened || "Plexora's folder";
        }
        return "this browser";
    }

    //: Every copy: {path, name, kind, home, place, bytes, builtBy, state, why, moveFailed}.
    async function list() {
        await prepare();
        return [...records.values()].map((record) => {
            const state = live.get(record.path) || { state: "missing" };
            return { path: record.path, name: record.name, kind: record.kind, home: record.home,
                     place: placeOf(record), bytes: record.bytes, builtBy: record.builtBy || "page",
                     state: state.state, why: state.why || null, moveFailed: record.moveFailed || null,
                     canMove: (record.home === "opfs" || record.home === "indexeddb") && Boolean(pyramids()) };
        });
    }

    //: From Settings' click: move one now (a failure said before is tried again).
    async function move(path) {
        const record = records.get(path);
        if (!record) return false;
        if (!pyramids() && folderChosen() && store().reopen) await store().reopen().catch(() => null);
        if (record.moveFailed) records.set(path, { ...record, moveFailed: undefined });
        return moveOne(path);
    }

    /**
     * From Settings' click: a copy deleted where it is and forgotten.
     * {reload}: Python has it open this visit, so Plexora must start again
     * before a project uses it (a mask's pyramid is then built again; an
     * image's copy is asked for). A copy in the user's own folder is theirs:
     * only Plexora's copies are deleted, the record of the user's is dropped.
     */
    async function remove(path) {
        const record = records.get(path);
        if (!record) return { reload: false };
        const was = live.get(path);
        try {
            if (record.home === "opfs") {
                await (await opfsDerived()).removeEntry(record.key, { recursive: true });
            } else if (record.home === "plexora" && pyramids()) {
                const dir = await pyramids().getDirectoryHandle(record.key);
                await dir.removeEntry(record.name);
                const left = await dir.keys().next();
                if (left.done) await pyramids().removeEntry(record.key).catch(() => null);
            }
        } catch (error) {
            // Gone already.
        }
        await forget(path);
        return { reload: Boolean(was && was.state === "ready") };
    }

    //: Bytes the site keeps in the browser: copies, the projects' store, and
    //: the rest (the chunk cache, telemetry). For Settings › Data.
    async function usage() {
        const out = { derived: 0, state: 0, other: 0 };
        const sum = async (dir) => {
            let total = 0;
            for await (const [, handle] of dir.entries()) {
                total += handle.kind === "directory" ? await sum(handle) : (await handle.getFile()).size;
            }
            return total;
        };
        try {
            const root = await (await global.navigator.storage.getDirectory()).getDirectoryHandle("plexora");
            for await (const [name, handle] of root.entries()) {
                const bytes = handle.kind === "directory" ? await sum(handle) : (await handle.getFile()).size;
                if (name === "derived") out.derived += bytes;
                else if (name === "state") out.state += bytes;
                else out.other += bytes;
            }
        } catch (error) {
            // No OPFS.
        }
        return out;
    }

    //: What Python asks (providers/browser.py `derived_index`): every copy
    //: by its path, with whether this page can read it now.
    function index() {
        const copies = {};
        for (const [path, record] of records) {
            const state = live.get(path) || { state: "missing" };
            copies[path] = { state: state.state, home: record.home, kind: record.kind, key: record.key,
                             name: record.name, place: placeOf(record) };
        }
        return { copies };
    }

    if (global.PlexoraPython && global.PlexoraPython.answers) {
        global.PlexoraPython.answers("derived_index", async () => {
            await prepare().catch(() => null);
            return { value: index() };
        });
    }

    global.PlexoraDerived = Object.freeze({
        prepare,
        restore,
        lookup,
        record,
        copyTo,
        reconnect,
        requestFolders,
        migrate,
        list,
        usage,
        move,
        remove,
        index,
        waiting: () => summary().waiting.map((path) => ({ path, folder: placeOf(records.get(path)) })),
        pathOf,
        kindOf,
    });
})(window);
