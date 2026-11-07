/**
 * A folder kept in IndexedDB, for a browser whose origin private file system
 * cannot be written (WebKit on Windows opens one and fails every write; a
 * private window may have none).
 *
 * It answers the part of the File System Access directory handle that the
 * project store uses (workers/storeSync.js, services/browserStore.js):
 * `entries()`, `keys()`, `getDirectoryHandle`, `getFileHandle`, `removeEntry`,
 * and a file handle's `getFile()` and `createWritable()` (`write(data)`,
 * `close()`, `abort()`). The store holds settings, gates and records, never
 * pixels, so a file is one value: its bytes, its time.
 *
 *   idbFolder(name = "plexora-state") -> a promise of the root directory handle
 *                                        (and its `close()`, the database's)
 *
 * One object store, keyed by the path inside the folder ("a/b/c"); a folder is
 * a {kind: "directory"} entry, a file {kind: "file", bytes, modified}.
 */

const STORE = "entries";

function openDatabase(name) {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(name, 1);
        request.onupgradeneeded = () => request.result.createObjectStore(STORE);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
}

function notFound(name) {
    return new DOMException(`${name} was not found`, "NotFoundError");
}

export async function idbFolder(name = "plexora-state") {
    const db = await openDatabase(name);
    const run = (mode, work) => new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        let result;
        Promise.resolve(work(tx.objectStore(STORE))).then((value) => { result = value; }, reject);
        tx.oncomplete = () => resolve(result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new DOMException("aborted", "AbortError"));
    });
    const request = (req) => new Promise((resolve, reject) => {
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
    const get = (path) => run("readonly", (store) => request(store.get(path)));
    const join = (dir, child) => (dir ? `${dir}/${child}` : child);

    //: The direct children of `dir`: [name, entry].
    async function children(dir) {
        const prefix = dir ? `${dir}/` : "";
        const range = prefix ? IDBKeyRange.bound(prefix, `${prefix}￿`) : null;
        const [keys, values] = await run("readonly", (store) => Promise.all([
            request(range ? store.getAllKeys(range) : store.getAllKeys()),
            request(range ? store.getAll(range) : store.getAll()),
        ]));
        const out = [];
        keys.forEach((key, i) => {
            const rest = key.slice(prefix.length);
            if (rest && !rest.includes("/")) out.push([rest, values[i]]);
        });
        return out;
    }

    function fileHandle(path) {
        const fileName = path.split("/").pop();
        return {
            kind: "file",
            name: fileName,
            async getFile() {
                const entry = await get(path);
                if (!entry || entry.kind !== "file") throw notFound(fileName);
                return new File([entry.bytes], fileName, { lastModified: entry.modified });
            },
            async createWritable() {
                const parts = [];
                let aborted = false;
                return {
                    async write(data) {
                        const chunk = data && data.type === "write" ? data.data : data;
                        if (chunk && data.type === "write" && data.position !== undefined && data.position !== null) {
                            throw new DOMException("this folder takes whole files only", "NotSupportedError");
                        }
                        parts.push(chunk);
                    },
                    async close() {
                        if (aborted) return;
                        const bytes = await new Blob(parts).arrayBuffer();
                        // A new time for every write, as a file system gives.
                        const before = await get(path);
                        const modified = Math.max(Date.now(), before && before.modified ? before.modified + 1 : 0);
                        await run("readwrite", (store) => request(store.put({ kind: "file", bytes, modified }, path)));
                    },
                    async abort() { aborted = true; },
                };
            },
        };
    }

    function directoryHandle(path) {
        const dirName = path ? path.split("/").pop() : "";
        const self = {
            kind: "directory",
            name: dirName,
            async* entries() {
                for (const [child, entry] of await children(path)) {
                    yield [child, entry.kind === "directory" ? directoryHandle(join(path, child))
                        : fileHandle(join(path, child))];
                }
            },
            async* keys() {
                for (const [child] of await children(path)) yield child;
            },
            async* values() {
                for await (const [, handle] of self.entries()) yield handle;
            },
            async getDirectoryHandle(child, { create = false } = {}) {
                const at = join(path, child);
                const entry = await get(at);
                if (entry && entry.kind !== "directory") throw new DOMException(`${child} is a file`, "TypeMismatchError");
                if (!entry) {
                    if (!create) throw notFound(child);
                    await run("readwrite", (store) => request(store.put({ kind: "directory" }, at)));
                }
                return directoryHandle(at);
            },
            async getFileHandle(child, { create = false } = {}) {
                const at = join(path, child);
                const entry = await get(at);
                if (entry && entry.kind !== "file") throw new DOMException(`${child} is a folder`, "TypeMismatchError");
                if (!entry) {
                    if (!create) throw notFound(child);
                    const bytes = new ArrayBuffer(0);
                    await run("readwrite", (store) => request(store.put({ kind: "file", bytes, modified: Date.now() }, at)));
                }
                return fileHandle(at);
            },
            async removeEntry(child, { recursive = false } = {}) {
                const at = join(path, child);
                const entry = await get(at);
                if (!entry) throw notFound(child);
                if (entry.kind === "directory") {
                    const inside = await children(at);
                    if (inside.length && !recursive) {
                        throw new DOMException(`${child} is not empty`, "InvalidModificationError");
                    }
                    await run("readwrite", (store) => request(store.delete(IDBKeyRange.bound(`${at}/`, `${at}/￿`))));
                }
                await run("readwrite", (store) => request(store.delete(at)));
            },
        };
        self[Symbol.asyncIterator] = self.entries;
        return self;
    }

    const root = directoryHandle("");
    //: Not a handle's: lets go of the database (before it is deleted).
    root.close = () => db.close();
    return root;
}
