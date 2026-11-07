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
 *   pick({mode, multiple}) -> a path, an array of them, or null (cancelled)
 *   restore()              -> {mounted: [id], waiting: [pick]} at boot
 *   reconnect()            -> mounts every waiting pick (call from a click)
 *   list()                 -> every remembered pick
 *   fileFor(path)          -> the File behind a mounted path, or null
 *
 * Classic script, `window.PlexoraPicks`. Needs `window.PlexoraPython`.
 */
(function (global) {
    "use strict";

    const DB = "plexora-picks";
    const STORE = "picks";
    const ROOT = "/picked";
    const waiting = [];
    //: Every mounted file by its path in Python's files: what the page's own
    //: readers open (services/browserNode.js) to read what Python would.
    const mounted = new Map();

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
            else found.push({ path: `${prefix}${name}`, file: await child.getFile() });
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
            input.addEventListener("change", () => {
                resolve(Array.from(input.files || []));
                input.remove();
            });
            input.addEventListener("cancel", () => { resolve([]); input.remove(); });
            global.document.body.appendChild(input);
            input.click();
        });
    }

    async function mountFiles(id, files) {
        const paths = await global.PlexoraPython.mount(`${ROOT}/${id}`, files);
        paths.forEach((path, i) => mounted.set(path, files[i]));
        return paths;
    }

    async function mountTree(id, name, entries) {
        const blobs = entries.map(({ path, file }) => ({ name: `${name}/${path}`, data: file }));
        await global.PlexoraPython.mountTree(`${ROOT}/${id}`, blobs);
        for (const { name: inside, data } of blobs) mounted.set(`${ROOT}/${id}/${inside}`, data);
        return `${ROOT}/${id}/${name}`;
    }

    async function pick({ mode = "file", multiple = false } = {}) {
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
            const paths = await mountFiles(id, files);
            await remember({ id, kind: "files", handles, picked: Date.now(),
                             files: files.map((file) => ({ name: file.name, size: file.size,
                                                           modified: file.lastModified })) });
            return multiple ? paths : paths[0];
        } catch (error) {
            if (error && error.name === "AbortError") return null; // cancelled
            throw error;
        }
    }

    async function granted(handle, ask) {
        const options = { mode: "read" };
        if (await handle.queryPermission(options) === "granted") return true;
        return ask && await handle.requestPermission(options) === "granted";
    }

    async function mountRecord(record, ask) {
        if (record.kind === "directory" && record.handle) {
            if (!await granted(record.handle, ask)) return false;
            await mountTree(record.id, record.name, await walk(record.handle));
            return true;
        }
        if (record.kind === "files" && record.handles) {
            for (const handle of record.handles) {
                if (!await granted(handle, ask)) return false;
            }
            await mountFiles(record.id, await Promise.all(record.handles.map((h) => h.getFile())));
            return true;
        }
        return false; // no handle kept: picked again by the user ("Locate file")
    }

    async function restore() {
        const mounted = [];
        waiting.length = 0;
        for (const record of await list()) {
            try {
                if (await mountRecord(record, false)) mounted.push(record.id);
                else waiting.push(record);
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

    global.PlexoraPicks = Object.freeze({ pick, restore, reconnect, list,
                                          waiting: () => waiting.slice(),
                                          fileFor: (path) => mounted.get(path) || null,
                                          //: A file mounted by other means (a test, a drop).
                                          adopt: (path, file) => mounted.set(path, file) });
})(window);
