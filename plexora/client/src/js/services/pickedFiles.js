/**
 * PlexoraPicks -- the user's own files, on the website.
 *
 * The desktop app and a server hand Plexora a path on a disk it can open. A web
 * page cannot see a path: the user picks a file or a folder, and the browser
 * hands the page an object it may read. This module turns a pick into a path
 * Plexora's Python can open, so every route after it (`/import/inspect`,
 * `/import/sample`, the tile reads) runs exactly as it does on the desktop:
 *
 * - each pick is mounted read-only in the worker's file system under a fresh
 *   `/picked/<id>/` (a folder with its tree, so an OME-Zarr or a Xenium run
 *   keeps its layout), and its path is what the picker answers with;
 * - the pick is remembered in IndexedDB on this site: the File System Access
 *   handles where the browser has them (Chromium), or what was picked (name,
 *   size, date) where it does not, so a later visit can mount it again --
 *   silently where the browser still grants access, after one "Reconnect"
 *   where it asks again, by picking it again where it keeps no handle.
 *
 *   pick({mode, multiple, ask}) -> a path, an array of them, or null (cancelled);
 *                             an MRXS slide brings the folder beside it, chosen
 *                             after `ask(text)` (the caller's one-button question)
 *                             and mounted with it (/picked/<id>/<name>.mrxs and
 *                             /picked/<id>/<name>/...)
 *   restore()              -> {mounted: [id], waiting: [pick]} at boot
 *   reconnect()            -> mounts every waiting pick (call from a click)
 *   list()                 -> every remembered pick
 *   fileFor(path)          -> the File behind a mounted path, or null
 *   filesUnder(prefix)     -> [{path, file}] mounted under a folder (an MRXS's)
 *   handleFor(path)        -> its File System Access handle, where the browser gave one
 *   canWriteFolders()      -> whether this browser lets the page write into a
 *                             folder the user grants (Chromium's File System Access)
 *   find(missing)          -> from a click: a project's files found in a folder the
 *                             user chooses, mounted like a pick: {recorded: path}
 *
 * Classic script, `window.PlexoraPicks`. Needs `window.PlexoraPython`.
 */
(function (global) {
    "use strict";

    const DB = "plexora-picks";
    const STORE = "picks";
    const ROOT = "/picked";
    const CANCEL_GRACE_MS = 1000;
    const waiting = [];
    //: Every mounted file by its path in Python's files: what the page's own
    //: readers open (services/browserNode.js) to read what Python would.
    const mounted = new Map();
    //: The handle behind a mounted file, where the browser gave one: what a
    //: folder picker can open beside (services/browserPyramid.js).
    const handles = new Map();

    function database() {
        return new Promise((resolve, reject) => {
            const request = global.indexedDB.open(DB, 1);
            request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "id" });
            request.onsuccess = () => resolve(request.result);
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

    const remember = (record) => transact("readwrite", (store) => store.put(record));
    const list = () => transact("readonly", (store) => store.getAll());

    function newId() {
        const bytes = new Uint8Array(6);
        global.crypto.getRandomValues(bytes);
        return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    }

    //: A folder's files, with their paths inside it.
    async function walk(directory, prefix = "") {
        const found = [];
        for await (const [name, child] of directory.entries()) {
            if (child.kind === "directory") found.push(...await walk(child, `${prefix}${name}/`));
            else found.push({ path: `${prefix}${name}`, file: await child.getFile(), handle: child });
        }
        return found;
    }

    //: `<input type=file>`, for a browser without the File System Access API.
    function chooseWithInput({ directory = false, multiple = false } = {}) {
        return new Promise((resolve) => {
            const input = global.document.createElement("input");
            input.type = "file";
            input.multiple = multiple;
            if (directory) input.webkitdirectory = true;
            input.style.display = "none";
            let done = false;
            const finish = (files) => {
                if (done) return;
                done = true;
                resolve(files);
                input.remove();
            };
            input.addEventListener("change", () => finish(Array.from(input.files || [])));
            // A cancel may come before the files: Firefox fires one as a
            // driver takes over its dialog (Playwright), then sets the files.
            // A change within a second of it still counts.
            input.addEventListener("cancel", () => setTimeout(() => finish([]), CANCEL_GRACE_MS));
            global.document.body.appendChild(input);
            input.click();
        });
    }

    //: `python`: who mounts the files for Python -- the worker, or `later`'s
    //: stand-in, which mounts nothing yet (`reopen`). The paths are the same
    //: either way: the worker mounts each file at `<dir>/<name>`.
    async function mountFiles(id, files, fileHandles = null, python = global.PlexoraPython) {
        const paths = await python.mount(`${ROOT}/${id}`, files);
        paths.forEach((path, i) => {
            mounted.set(path, files[i]);
            if (fileHandles && fileHandles[i]) handles.set(path, fileHandles[i]);
        });
        return paths;
    }

    async function mountTree(id, name, entries, python = global.PlexoraPython) {
        const blobs = entries.map(({ path, file }) => ({ name: `${name}/${path}`, data: file }));
        await python.mountTree(`${ROOT}/${id}`, blobs);
        for (const { name: inside, data } of blobs) mounted.set(`${ROOT}/${id}/${inside}`, data);
        // Each file's own handle, where the browser gave the folder's: what a
        // save into one of them writes through (services/sourceWrites.js).
        for (const { path, handle } of entries) {
            if (handle) handles.set(`${ROOT}/${id}/${name}/${path}`, handle);
        }
        return `${ROOT}/${id}/${name}`;
    }

    //: An MRXS slide is its .mrxs and the folder of the same name beside it,
    //: where the pixels are (OpenSlide's MIRAX layout).
    const MRXS = /\.mrxs$/i;

    /**
     * The folder an MRXS's pixels are in, from the user: the folder named
     * after the slide, or the one holding both (the folder of that name is
     * then taken from inside it). `ask(text)` is the caller's one-button
     * question (a click on it is the gesture the folder picker needs); with
     * none, the picker is opened straight away, which a browser may refuse.
     * -> {name, entries: [{path inside the slide's folder, file, handle}], handle} or null.
     */
    async function slideFolder(file, fileHandle, ask) {
        const stem = file.name.replace(MRXS, "");
        if (ask && !await ask(`Choose the folder "${stem}" that sits beside ${file.name}.`)) return null;
        let entries;
        let handle = null;
        if (global.showDirectoryPicker) {
            const options = { id: "plexora-slide", mode: "read" };
            if (fileHandle) options.startIn = fileHandle;
            handle = await global.showDirectoryPicker(options);
            entries = await walkNamed(handle);
        } else {
            const files = await chooseWithInput({ directory: true });
            if (!files.length) return null;
            entries = files.map((f) => ({ path: f.webkitRelativePath || f.name, file: f, handle: null }));
        }
        const inside = slideEntries(stem, entries);
        if (!inside) {
            throw new Error(`${file.name}: that folder has no Slidedat.ini; choose the folder "${stem}" `
                            + "the scanner wrote beside the .mrxs");
        }
        return { name: stem, entries: inside, handle };
    }

    //: A folder's files as "<folder>/<path>", as `<input webkitdirectory>` names them.
    async function walkNamed(handle) {
        return (await walk(handle)).map((entry) => ({ ...entry, path: `${handle.name}/${entry.path}` }));
    }

    /**
     * An MRXS's files out of a chosen folder's ("<chosen>/<path>" each): the
     * folder itself, or the folder named `stem` inside it -- with paths inside
     * that folder; null when neither holds a Slidedat.ini.
     */
    function slideEntries(stem, entries) {
        const lower = stem.toLowerCase();
        const split = (path) => path.split("/");
        const hasIndex = (list) => list.some((e) => /^slidedat\.ini$/i.test(e.path));
        const nested = entries.filter((e) => split(e.path).length > 2 && split(e.path)[1].toLowerCase() === lower)
            .map((e) => ({ ...e, path: split(e.path).slice(2).join("/") }));
        if (hasIndex(nested)) return nested;
        const itself = entries.map((e) => ({ ...e, path: split(e.path).slice(1).join("/") }));
        return hasIndex(itself) ? itself : null;
    }

    //: The .mrxs at /picked/<id>/<name>.mrxs and its folder at /picked/<id>/<stem>/.
    async function mountSlide(id, file, folder, python = global.PlexoraPython) {
        const blobs = [{ name: file.name, data: file },
                       ...folder.entries.map(({ path, file: f }) => ({ name: `${folder.name}/${path}`, data: f }))];
        await python.mountTree(`${ROOT}/${id}`, blobs);
        for (const { name, data } of blobs) mounted.set(`${ROOT}/${id}/${name}`, data);
        return `${ROOT}/${id}/${file.name}`;
    }

    async function pick({ mode = "file", multiple = false, ask = null } = {}) {
        const id = newId();
        try {
            if (mode === "directory") {
                if (global.showDirectoryPicker) {
                    const handle = await global.showDirectoryPicker({ id: "plexora", mode: "read" });
                    const path = await mountTree(id, handle.name, await walk(handle));
                    await remember({ id, kind: "directory", name: handle.name, handle, picked: Date.now() });
                    return path;
                }
                const files = await chooseWithInput({ directory: true });
                if (!files.length) return null;
                const name = (files[0].webkitRelativePath || files[0].name).split("/")[0];
                const entries = files.map((file) => ({
                    path: (file.webkitRelativePath || file.name).split("/").slice(1).join("/") || file.name, file,
                }));
                const path = await mountTree(id, name, entries);
                await remember({ id, kind: "directory", name, handle: null, picked: Date.now(),
                                 files: entries.map(({ path: p, file }) => ({ path: p, size: file.size,
                                                                               modified: file.lastModified })) });
                return path;
            }
            let handles = null;
            let files;
            if (global.showOpenFilePicker) {
                handles = await global.showOpenFilePicker({ id: "plexora", multiple });
                files = await Promise.all(handles.map((handle) => handle.getFile()));
            } else {
                files = await chooseWithInput({ multiple });
            }
            if (!files.length) return null;
            if (files.length === 1 && MRXS.test(files[0].name)) {
                // An MRXS: its folder too, mounted beside it.
                const folder = await slideFolder(files[0], handles && handles[0], ask);
                if (!folder) return null;
                const path = await mountSlide(id, files[0], folder);
                await remember({ id, kind: "slide", handles, folder: folder.handle, name: folder.name,
                                 picked: Date.now(), count: folder.entries.length,
                                 files: [{ name: files[0].name, size: files[0].size,
                                           modified: files[0].lastModified }] });
                return multiple ? [path] : path;
            }
            const paths = await mountFiles(id, files, handles);
            await remember({ id, kind: "files", handles, picked: Date.now(),
                             files: files.map((file) => ({ name: file.name, size: file.size,
                                                           modified: file.lastModified })) });
            return multiple ? paths : paths[0];
        } catch (error) {
            if (error && error.name === "AbortError") return null; // cancelled
            throw error;
        }
    }

    async function sha256(blob) {
        const digest = await global.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
        return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
    }

    //: Whether `file` is the one a project recorded (server/models/sources.py
    //: `same_file`): the same size, and the same time to the millisecond or
    //: the same bytes at both ends.
    async function sameFile(file, facts) {
        if (file.size !== facts.size) return false;
        if (facts.mtime_ms != null && Math.abs(file.lastModified - facts.mtime_ms) <= 1) return true;
        if (!facts.head || !facts.tail) return false;
        const end = 1 << 20;
        return await sha256(file.slice(0, end)) === facts.head
            && await sha256(file.slice(Math.max(0, file.size - end))) === facts.tail;
    }

    /**
     * From a click: a project's files that this page has no path for
     * ([{recorded, name, size, mtime_ms, head, tail}], from Python's
     * /project/<name>/files_here), looked for in a folder the user chooses
     * (and its subfolders), each by name and then by `sameFile`. The ones
     * found are mounted like a pick and remembered; {recorded: path here}.
     */
    async function find(missing) {
        let entries;
        try {
            if (global.showDirectoryPicker) {
                entries = await walk(await global.showDirectoryPicker({ id: "plexora-find", mode: "read" }));
            } else {
                // No folder picker (Firefox, Safari): the files themselves
                // ("Locate file…"); a folder input would list, and ask to
                // upload, everything under the folder.
                entries = (await chooseWithInput({ multiple: missing.length > 1 }))
                    .map((file) => ({ path: file.name, file, handle: null }));
            }
        } catch (error) {
            if (error && error.name === "AbortError") return {};
            throw error;
        }
        const byName = new Map();
        for (const entry of entries) {
            if (!byName.has(entry.file.name)) byName.set(entry.file.name, []);
            byName.get(entry.file.name).push(entry);
        }
        const out = {};
        for (const item of missing) {
            let hit = null;
            for (const entry of byName.get(item.name) || []) {
                if (await sameFile(entry.file, item)) { hit = entry; break; }
            }
            if (!hit) continue;
            // One mount per name: two found files may share one.
            const id = newId();
            const handles = hit.handle ? [hit.handle] : null;
            const [path] = await mountFiles(id, [hit.file], handles);
            await remember({ id, kind: "files", handles, picked: Date.now(),
                             files: [{ name: hit.file.name, size: hit.file.size, modified: hit.file.lastModified }] });
            out[item.recorded] = path;
        }
        return out;
    }

    async function granted(handle, ask) {
        const options = { mode: "read" };
        if (await handle.queryPermission(options) === "granted") return true;
        return ask && await handle.requestPermission(options) === "granted";
    }

    async function mountRecord(record, ask, python = global.PlexoraPython) {
        if (record.kind === "slide" && record.handles && record.folder) {
            if (!await granted(record.handles[0], ask) || !await granted(record.folder, ask)) return false;
            const entries = slideEntries(record.name, await walkNamed(record.folder));
            if (!entries) return false;
            await mountSlide(record.id, await record.handles[0].getFile(), { name: record.name, entries }, python);
            return true;
        }
        if (record.kind === "directory" && record.handle) {
            if (!await granted(record.handle, ask)) return false;
            await mountTree(record.id, record.name, await walk(record.handle), python);
            return true;
        }
        if (record.kind === "files" && record.handles) {
            for (const handle of record.handles) {
                if (!await granted(handle, ask)) return false;
            }
            await mountFiles(record.id, await Promise.all(record.handles.map((h) => h.getFile())), record.handles,
                             python);
            return true;
        }
        return false; // no handle kept: picked again by the user ("Locate file")
    }

    //: record id -> Python's half of its mount, for a pick reopened before
    //: Python started (`reopen`); `restore` does it.
    const unmounted = new Map();
    //: record id -> the promise of its reopening.
    const reopening = new Map();

    //: Python's mounts written down to be done later, with the paths the
    //: worker would give (`<dir>/<name>`).
    function later(calls) {
        return {
            mount: async (dir, files) => {
                calls.push(() => global.PlexoraPython.mount(dir, files));
                return files.map((file) => `${dir}/${file.name}`);
            },
            mountTree: async (dir, blobs) => {
                calls.push(() => global.PlexoraPython.mountTree(dir, blobs));
            },
        };
    }

    //: The files picked on earlier visits (those of `ids`, or all), opened
    //: again where the browser still grants them without asking -- for the
    //: page only: Python's mounts wait for `restore`. What a reload of the
    //: site shows before Python is up reads its pixels from these
    //: (services/keptAnswers.js `early`). Each record once.
    async function reopen(ids = null) {
        const wanted = ids ? new Set(ids) : null;
        for (const record of await list()) {
            if ((wanted && !wanted.has(record.id)) || reopening.has(record.id)) continue;
            reopening.set(record.id, (async () => {
                const calls = [];
                try {
                    if (await mountRecord(record, false, later(calls))) {
                        unmounted.set(record.id, calls);
                        return true;
                    }
                } catch (error) { /* `restore` tries it again, with Python */ }
                return false;
            })());
        }
        const opened = [];
        for (const [id, done] of reopening) if ((!wanted || wanted.has(id)) && await done) opened.push(id);
        return { opened };
    }

    async function restore() {
        const mounted = [];
        waiting.length = 0;
        // What `reopen` opened is only Python's to mount now: the same File
        // objects, at the same paths, not read from their handles again.
        await Promise.all([...reopening.values()]);
        for (const record of await list()) {
            try {
                const calls = unmounted.get(record.id);
                if (calls) {
                    unmounted.delete(record.id);
                    for (const call of calls) await call();
                    mounted.push(record.id);
                } else if (await mountRecord(record, false)) {
                    mounted.push(record.id);
                } else {
                    waiting.push(record);
                }
            } catch (error) {
                waiting.push(record);
            }
        }
        return { mounted, waiting: waiting.slice() };
    }

    async function reconnect() {
        const done = [];
        for (const record of waiting.splice(0)) {
            try {
                if (await mountRecord(record, true)) done.push(record.id);
                else waiting.push(record);
            } catch (error) {
                waiting.push(record);
            }
        }
        return { mounted: done, waiting: waiting.slice() };
    }

    global.PlexoraPicks = Object.freeze({ pick, reopen, restore, reconnect, list,
                                          waiting: () => waiting.slice(),
                                          fileFor: (path) => mounted.get(path) || null,
                                          //: Every mounted file under the folder `prefix`
                                          //: ("/picked/<id>/<folder>/"): [{path, file}].
                                          filesUnder: (prefix) => [...mounted.entries()]
                                              .filter(([path]) => path.startsWith(prefix))
                                              .map(([path, file]) => ({ path, file })),
                                          handleFor: (path) => handles.get(path) || null,
                                          canWriteFolders: () => typeof global.showDirectoryPicker === "function",
                                          find,
                                          //: A file mounted by other means (a test, a drop).
                                          adopt: (path, file) => mounted.set(path, file) });
})(window);
