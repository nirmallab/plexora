/**
 * The website's project store, kept in step with a folder that another app
 * may write too.
 *
 * Python reads and writes its data root, /data, in memory (Emscripten's
 * MEMFS: it needs a file system it can call synchronously). The store itself
 * is a folder -- OPFS plexora/state, or Plexora's folder on the computer the
 * user granted, Documents/Plexora/projects (services/browserStore.js), which
 * the desktop app reads and writes as its own library. So the two are kept in
 * step here, and never by mirroring one onto the other: Pyodide's NativeFS
 * write-back deletes from the folder whatever the memory copy does not hold,
 * so a project the desktop app saved while a tab was open was deleted by the
 * tab's next save.
 *
 * What was last read or written is remembered, per file, with its time on
 * both sides (`known`):
 *  - push: a file changed in memory since then is written to the folder; a
 *    file deleted in memory is deleted from the folder only if nobody changed
 *    it there since. A file the page never knew is never touched.
 *  - pull: a file new or changed in the folder, and not changed in memory, is
 *    read in; one deleted from the folder and not changed in memory is
 *    deleted. A file Python has open is left for the next pull.
 * When both sides changed one file, the page's copy is written (last writer
 * wins, as between two desktop windows).
 *
 * A folder the desktop app writes too (`open(handle, {shared: true})`) may
 * hold what is not the page's to read: the caches of one machine a data root
 * keeps and its server's token (`KEPT_OUT`), never read in, written or
 * deleted: the page's Python may make its own at the same path, which stays in
 * memory. And files too big to hold in memory (`HELD_BYTES`: a pyramid
 * Python built into a project, a big table): those are read where they lie,
 * a slice at a time (Emscripten's WORKERFS, mounted under /held/<n> and
 * linked at their path, `linkHeld`), never copied into memory -- before,
 * Python never saw them, and built them again every visit. One Python writes
 * anew at such a path is written to the folder like any other file, and then
 * read from there in its turn, which gives its bytes back to the heap. Where
 * this Emscripten cannot read through a link (`canLink`, probed once) they
 * are left in the folder, unread, as they always were.
 *
 * The store holds every project and a page opens one, so a file of
 * `LAZY_BYTES` or more is read when Python first reads it, not when the folder
 * is opened (`placeLazy`); one never read is never written back.
 *
 *   createStore(FS, root = "/data", log, {readSync, heldRoot}) ->
 *       {open(handle, {shared}), push(), pull(), adopt(paths), files(), held(), linked(), unread()}
 */

const now = () => performance.now();

//: What a page never reads from a shared folder, nor writes or deletes there:
//: a data root's caches of one machine (plexora/paths.py: the web chunk
//: cache, the .ims chunk indexes, downloaded model weights, the telemetry
//: queue), often gigabytes and each made again where it is needed; the
//: desktop's server token; and write probes.
const KEPT_OUT = new Set([".remote_cache", ".ims_index", ".models", ".telemetry", "server_token.json"]);
//: ... and every name that begins with one of these: what project bundles
//: keep beside the projects (server/models/bundles.py) -- an import's staging
//: folder (`.importing-<bundle>`) and exports waiting to be fetched
//: (`.bundles`), each a whole project or more. The previous versions an
//: Update or a Replace moved aside (`.replaced`) are NOT here: they are the
//: user's only copy of the old project and must reach the folder.
const KEPT_OUT_PREFIXES = [".importing-", ".bundles"];
const PROBE_PREFIX = ".plexora-write-probe";
//: A file at least this big in a shared folder is left there unread: the
//: page holds the whole store in memory.
export const HELD_BYTES = 256 * 1024 * 1024;
//: A file at least this big is read when Python first reads it.
export const LAZY_BYTES = 64 * 1024;
//: Emscripten's errno for an I/O error: what a read of a file the folder
//: changed under it gets.
const EIO = 29;
//: Where held files are mounted, one folder each (outside the store's root).
const HELD_ROOT = "/held";
//: The held files read where they lie: TIFFs -- the pyramids Python builds
//: into a project, which it writes beside and moves into place
//: (`os.replace`, which replaces the link). Anything else may be opened to be
//: written in place (a project's SQLite database), which a file read in the
//: folder cannot take, so it is left there as before.
export const linkable = (rel) => /\.(ome\.)?tiff?$/i.test(String(rel));

// WORKERFS dates a mounted file by `file.lastModifiedDate`, which Firefox no
// longer has: a file is dated by its own `lastModified`, as the worker's own
// mounts are (workers/pythonWorker.js `datedFiles`, whose flag this shares,
// so WORKERFS is patched once whichever mounts first).
function datedFiles(workerfs) {
    if (workerfs.plexoraDated) return workerfs;
    const createNode = workerfs.createNode;
    workerfs.createNode = function (parent, name, mode, dev, contents, mtime) {
        const own = contents && typeof contents.lastModified === "number" ? new Date(contents.lastModified) : mtime;
        return createNode.call(this, parent, name, mode, dev, contents, own);
    };
    workerfs.plexoraDated = true;
    return workerfs;
}

export function keptOut(rel) {
    const top = String(rel).split("/")[0];
    return KEPT_OUT.has(top) || top.startsWith(PROBE_PREFIX) || KEPT_OUT_PREFIXES.some((prefix) => top.startsWith(prefix));
}

//: FNV-1a over a file's bytes: whether a file rewritten within the same
//: millisecond, at the same size, changed (memory file times are in ms).
function fingerprint(bytes) {
    let hash = 0x811c9dc5;
    for (let i = 0; i < bytes.length; i += 1) {
        hash ^= bytes[i];
        hash = Math.imul(hash, 0x01000193);
    }
    return hash >>> 0;
}

export function createStore(FS, root = "/data", log = () => {}, options = {}) {
    //: A File's bytes, now: the worker can wait on nothing while Python reads.
    const readSync = options.readSync
        || ((file) => new Uint8Array(new FileReaderSync().readAsArrayBuffer(file)));
    //: Where this store mounts its held files: a folder of its own when the
    //: worker keeps more than one store (pythonWorker.js `libraries`), or
    //: two would both mount /held/1.
    const heldRoot = options.heldRoot || HELD_ROOT;
    let handle = null;
    //: Files placed in memory whose bytes are still in the folder (rel -> node).
    const unread = new Map();
    //: rel path -> {local, remote, size, hash}: as last read or written.
    const known = new Map();
    //: When the last write-back looked (ms, the clock memory file times use).
    let lastPush = 0;
    //: Folders known to exist on both sides (an empty project folder is kept too).
    const knownDirs = new Set();
    //: A folder the desktop app writes too: `keptOut` and `held` apply.
    let shared = false;
    //: Files of a shared folder left there for their size (rel path -> bytes).
    const held = new Map();
    //: Held files read where they lie: rel -> {dir, target, mtime, size}.
    const links = new Map();
    //: Held files whose link Python removed (rel -> the folder file's time):
    //: not linked again while the folder holds that same file.
    const unlinked = new Map();
    let nextHeld = 1;
    //: Whether this FS reads through a link into WORKERFS (`canLink`); null: not probed.
    let linksWork = null;
    //: Held files are linked (`linkHeld`) and so walked past as links; where
    //: they cannot be, they are left out by name, as before.
    const readInPlace = (rel) => Boolean(linksWork) && linkable(rel);
    const skipped = (rel) => shared && (keptOut(rel) || (held.has(rel) && !readInPlace(rel)));
    let chain = Promise.resolve();
    const queue = (work) => (chain = chain.then(work, work));

    const full = (rel) => `${root}/${rel}`;

    function localWalk() {
        const files = new Map();
        const dirs = new Set();
        const visit = (rel) => {
            let names;
            try {
                names = FS.readdir(rel ? full(rel) : root);
            } catch (error) {
                return;
            }
            for (const name of names) {
                if (name === "." || name === "..") continue;
                const child = rel ? `${rel}/${name}` : name;
                if (skipped(child)) continue;
                // A held file's link: the folder's file, read where it lies.
                if (isLink(full(child))) continue;
                const stat = FS.stat(full(child));
                if (FS.isDir(stat.mode)) {
                    dirs.add(child);
                    visit(child);
                } else {
                    files.set(child, { mtime: stat.mtime.getTime(), size: stat.size });
                }
            }
        };
        visit("");
        return { files, dirs };
    }

    async function remoteWalk() {
        const files = new Map();
        const dirs = new Set();
        const big = new Map();
        const visit = async (dir, rel) => {
            for await (const [name, child] of dir.entries()) {
                if (name.endsWith(".crswap")) continue; // a write in progress
                const path = rel ? `${rel}/${name}` : name;
                if (shared && keptOut(path)) continue;
                if (child.kind === "directory") {
                    dirs.add(path);
                    await visit(child, path);
                } else {
                    const file = await child.getFile();
                    if (shared && file.size >= HELD_BYTES) {
                        big.set(path, file);
                        continue;
                    }
                    files.set(path, { mtime: file.lastModified, size: file.size, file });
                }
            }
        };
        await visit(handle, "");
        const reads = big.size > 0 && canLink();
        for (const [path, file] of big) {
            if (!held.has(path)) log(`store: ${path} (${Math.round(file.size / 2 ** 20)} MB) is `
                                     + (reads && linkable(path) ? "read in the folder, where it is" : "left in the folder")
                                     + ", too big to hold in memory");
        }
        held.clear();
        for (const [path, file] of big) held.set(path, file.size);
        return { files, dirs, big };
    }

    function isLink(path) {
        if (typeof FS.lstat !== "function" || typeof FS.isLink !== "function") return false;
        try {
            return FS.isLink(FS.lstat(path).mode);
        } catch (error) {
            return false;
        }
    }

    /**
     * Whether this Emscripten reads a WORKERFS file through a link in memory,
     * and lets Python replace the link with a file of its own (`os.replace`):
     * tried once, on a three-byte file, before any held file is linked.
     */
    function canLink() {
        if (linksWork !== null) return linksWork;
        linksWork = false;
        const WORKERFS = FS.filesystems && FS.filesystems.WORKERFS;
        if (!WORKERFS || typeof FS.symlink !== "function" || typeof FS.mount !== "function"
            || typeof FS.lstat !== "function" || typeof File === "undefined") return false;
        const dir = `${heldRoot}/probe`;
        const link = `${heldRoot}/probe-link`;
        const fresh = `${heldRoot}/probe-fresh`;
        try {
            FS.mkdirTree(dir);
            FS.mount(datedFiles(WORKERFS), { files: [new File([new Uint8Array([1, 2, 3])], "probe.bin")] }, dir);
            FS.symlink(`${dir}/probe.bin`, link);
            const read = FS.readFile(link);
            FS.writeFile(fresh, new Uint8Array([9]));
            FS.rename(fresh, link);
            const replaced = FS.readFile(link);
            linksWork = read.length === 3 && read[2] === 3 && replaced.length === 1 && replaced[0] === 9
                && !isLink(link);
            FS.unlink(link);
            FS.unmount(dir);
        } catch (error) {
            log(`store: big files are left in the folder (${error?.message || error})`);
            linksWork = false;
        }
        return linksWork;
    }

    //: The folder's held `file` at `rel`, read where it lies: WORKERFS under
    //: /held/<n>, linked at its path in place of anything there.
    function linkHeld(rel, file) {
        const path = full(rel);
        const before = links.get(rel);
        if (before) {
            // The file it read before: nothing reads it through this path now.
            try { FS.unmount(before.dir); } catch (error) { /* still open somewhere */ }
        }
        const dir = `${heldRoot}/${nextHeld++}`;
        FS.mkdirTree(dir);
        FS.mount(datedFiles(FS.filesystems.WORKERFS), { files: [file] }, dir);
        FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
        try { FS.unlink(path); } catch (error) { /* not there yet */ }
        const target = `${dir}/${file.name}`;
        FS.symlink(target, path);
        unread.delete(rel);
        unlinked.delete(rel);
        links.set(rel, { dir, target, mtime: file.lastModified, size: file.size });
    }

    //: Whether `rel` is still the link this store made for it.
    function ownLink(rel) {
        const link = links.get(rel);
        if (!link || !isLink(full(rel))) return false;
        try {
            return FS.readlink(full(rel)) === link.target;
        } catch (error) {
            return false;
        }
    }

    function dropLinks() {
        for (const { dir } of links.values()) {
            try { FS.unmount(dir); } catch (error) { /* a file of it still open */ }
        }
        links.clear();
        unlinked.clear();
    }

    /**
     * The held files of a walk of the folder, linked where they lie: a new
     * one, or one the folder changed; one Python wrote itself and the folder
     * now holds as written (pushed, unchanged since) is swapped for its link,
     * which gives its bytes back to the heap. A link Python removed stays
     * removed while the folder holds the same file. Nothing while a file
     * there is open.
     */
    function linkAll(big) {
        if (!shared || !canLink()) return 0;
        let count = 0;
        for (const [rel, file] of big) {
            if (!linkable(rel)) continue;
            const link = links.get(rel);
            if (link && link.mtime === file.lastModified && link.size === file.size && ownLink(rel)) continue;
            if (unlinked.get(rel) === file.lastModified) continue;
            const path = full(rel);
            if (link && !ownLink(rel) && !isLink(path) && !exists(path) && link.mtime === file.lastModified) {
                unlinked.set(rel, file.lastModified); // Python removed it
                continue;
            }
            if (exists(path) && !isLink(path)) {
                // Python's own file at that path: swapped only once the folder
                // holds what it wrote, and never while it is open.
                const was = known.get(rel);
                let here = null;
                try { here = FS.stat(path); } catch (error) { here = null; }
                if (!was || !here || here.mtime.getTime() !== was.local || isOpen(rel)) continue;
                known.delete(rel);
            } else if (isOpen(rel)) {
                continue;
            }
            try {
                linkHeld(rel, file);
                count += 1;
            } catch (error) {
                log(`store: ${rel} could not be read in the folder (${error?.message || error})`);
            }
        }
        return count;
    }

    function exists(path) {
        try {
            FS.lstat ? FS.lstat(path) : FS.stat(path);
            return true;
        } catch (error) {
            return false;
        }
    }

    async function folderOf(rel, create) {
        let dir = handle;
        for (const part of rel.split("/").slice(0, -1)) dir = await dir.getDirectoryHandle(part, { create });
        return dir;
    }

    async function writeRemote(rel, data) {
        const dir = await folderOf(rel, true);
        const file = await dir.getFileHandle(rel.split("/").pop(), { create: true });
        const writable = await file.createWritable();
        await writable.write(data);
        await writable.close();
        return (await file.getFile()).lastModified;
    }

    function isOpen(rel) {
        try {
            const node = FS.lookupPath(full(rel)).node;
            return FS.streams.some((stream) => stream && stream.node === node);
        } catch (error) {
            return false;
        }
    }

    function readIn(rel, data, mtime) {
        const path = full(rel);
        FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
        unread.delete(rel);
        FS.writeFile(path, data);
        FS.utime(path, mtime, mtime);
    }

    /**
     * The folder's `file` in memory at `rel`, its bytes read only when Python
     * first reads or changes it. MEMFS keeps a file's bytes in
     * `node.contents` and its size in `node.usedBytes`, and looks at the first
     * only to read or write them (never for a stat or a listing): here that is
     * a getter that reads the bytes from the File then, and becomes a plain
     * property once it has, or once Python writes the file anew. A File the
     * folder changed since cannot be read: Python is told EIO, never given
     * other bytes, and the next pull places the new one.
     */
    function placeLazy(rel, file, mtime, size) {
        const path = full(rel);
        FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
        try { FS.unlink(path); } catch (error) { /* not there yet */ }
        FS.writeFile(path, new Uint8Array(0));
        const node = FS.lookupPath(path).node;
        const settle = (value) => {
            if (unread.get(rel) === node) unread.delete(rel);
            Object.defineProperty(node, "contents", { value, writable: true, configurable: true, enumerable: true });
        };
        Object.defineProperty(node, "contents", {
            configurable: true,
            enumerable: true,
            get() {
                let bytes;
                try {
                    bytes = readSync(file);
                } catch (error) {
                    log(`store: ${rel} changed in the folder before it was read (${error?.name || error})`);
                    throw new FS.ErrnoError(EIO);
                }
                const was = known.get(rel);
                if (was && was.hash === null && was.size === bytes.length) was.hash = fingerprint(bytes);
                settle(bytes);
                return bytes;
            },
            set(value) { settle(value); },
        });
        node.usedBytes = size;
        FS.utime(path, mtime, mtime);
        unread.set(rel, node);
    }

    //: The folder's file at `rel` placed in memory: read now if small, else
    //: when Python first reads it. Returns what `known` keeps of it.
    async function place(rel, file) {
        if (file.size >= LAZY_BYTES) {
            placeLazy(rel, file.file, file.mtime, file.size);
            return { local: file.mtime, remote: file.mtime, size: file.size, hash: null };
        }
        const bytes = new Uint8Array(await file.file.arrayBuffer());
        readIn(rel, bytes, file.mtime);
        return { local: file.mtime, remote: file.mtime, size: file.size, hash: fingerprint(bytes) };
    }

    function clear(path = root) {
        for (const name of FS.readdir(path)) {
            if (name === "." || name === "..") continue;
            const child = `${path}/${name}`;
            if (!isLink(child) && FS.isDir(FS.stat(child).mode)) {
                clear(child);
                FS.rmdir(child);
            } else {
                FS.unlink(child);
            }
        }
    }

    //: Use `dir` from now on: what it holds is read into memory in place of
    //: what was there. `shared`: a folder the desktop app writes too.
    function open(dir, options = {}) {
        return queue(async () => {
            const began = now();
            FS.mkdirTree(root);
            clear();
            dropLinks();
            known.clear();
            knownDirs.clear();
            held.clear();
            unread.clear();
            shared = Boolean(options.shared);
            handle = dir;
            const remote = await remoteWalk();
            for (const rel of remote.dirs) {
                FS.mkdirTree(full(rel));
                knownDirs.add(rel);
            }
            for (const [rel, file] of remote.files) known.set(rel, await place(rel, file));
            const linked = linkAll(remote.big);
            return { files: known.size, unread: unread.size, held: held.size, linked,
                     ms: Math.round(now() - began) };
        });
    }

    //: What changed in memory, written to the folder.
    function push() {
        return queue(async () => {
            if (!handle) return null;
            const looked = Date.now();
            const local = localWalk();
            let written = 0;
            let removed = 0;
            for (const dir of local.dirs) {
                if (knownDirs.has(dir)) continue;
                let made = handle;
                for (const part of dir.split("/")) made = await made.getDirectoryHandle(part, { create: true });
                knownDirs.add(dir);
            }
            for (const [rel, file] of local.files) {
                const was = known.get(rel);
                let bytes = null;
                if (was && was.local === file.mtime && was.size === file.size) {
                    // Never read: its bytes are still the folder's.
                    if (unread.has(rel)) continue;
                    // Same time and size: unchanged, unless it was written in
                    // the millisecond the last write-back looked.
                    if (file.mtime < lastPush - 1) continue;
                    bytes = FS.readFile(full(rel));
                    if (fingerprint(bytes) === was.hash) continue;
                }
                bytes = bytes || FS.readFile(full(rel));
                const remote = await writeRemote(rel, bytes);
                known.set(rel, { local: file.mtime, remote, size: file.size, hash: fingerprint(bytes) });
                written += 1;
            }
            lastPush = looked;
            for (const [rel, was] of [...known]) {
                if (local.files.has(rel)) continue;
                known.delete(rel);
                unread.delete(rel);
                try {
                    const dir = await folderOf(rel, false);
                    const name = rel.split("/").pop();
                    const there = await (await dir.getFileHandle(name)).getFile();
                    // Changed in the folder since: another app's now, kept.
                    if (there.lastModified === was.remote) {
                        await dir.removeEntry(name);
                        removed += 1;
                    }
                } catch (error) {
                    // Already gone.
                }
            }
            for (const dir of [...knownDirs].sort((a, b) => b.length - a.length)) {
                if (local.dirs.has(dir)) continue;
                knownDirs.delete(dir);
                try {
                    const parent = await folderOf(`${dir}/x`, false);
                    const name = dir.split("/").pop();
                    const it = (await parent.getDirectoryHandle(name)).keys();
                    if ((await it.next()).done) await parent.removeEntry(name); // only an empty one
                } catch (error) {
                    // Already gone.
                }
            }
            return { written, removed };
        });
    }

    //: What another app changed in the folder, read in.
    function pull() {
        return queue(async () => {
            if (!handle) return null;
            const remote = await remoteWalk();
            const local = localWalk();
            let read = 0;
            let dropped = 0;
            for (const dir of remote.dirs) {
                if (!local.dirs.has(dir)) FS.mkdirTree(full(dir));
                knownDirs.add(dir);
            }
            for (const [rel, file] of remote.files) {
                const was = known.get(rel);
                const here = local.files.get(rel);
                if (here && (!was || here.mtime !== was.local)) continue; // changed here: pushed next
                if (was && was.remote === file.mtime) continue; // unchanged there
                if (isOpen(rel)) continue;
                known.set(rel, await place(rel, file));
                read += 1;
            }
            for (const [rel, was] of [...known]) {
                if (remote.files.has(rel)) continue;
                if (remote.big.has(rel)) {
                    // Grown past HELD_BYTES in the folder -- most often a file
                    // Python wrote and this store pushed. Linked below where
                    // links work; otherwise Python keeps the copy it has (it
                    // was dropped once, and built again within the visit).
                    if (!(canLink() && linkable(rel))) known.delete(rel);
                    continue;
                }
                const here = local.files.get(rel);
                if (here && here.mtime !== was.local) continue; // changed here: written back next
                if (isOpen(rel)) continue;
                try { FS.unlink(full(rel)); } catch (error) { /* gone */ }
                unread.delete(rel);
                known.delete(rel);
                dropped += 1;
            }
            const linked = linkAll(remote.big);
            for (const rel of [...links.keys()]) {
                // Gone from the folder: its link goes too, unless Python has it open.
                if (remote.big.has(rel) || isOpen(rel)) continue;
                if (ownLink(rel)) { try { FS.unlink(full(rel)); } catch (error) { /* gone */ } }
                try { FS.unmount(links.get(rel).dir); } catch (error) { /* in use */ }
                links.delete(rel);
            }
            return { read, dropped, linked };
        });
    }

    /**
     * Files another worker wrote straight into the folder (services/
     * bundleWeb.js: an import's large members, unpacked by its own worker so
     * they never pass through Python's heap), known from now on as a pull
     * would have read them -- present to Python, their bytes left in the
     * folder until it reads them (`placeLazy`), or held there when too big
     * to hold -- so the next push neither writes them back nor deletes them,
     * and the next pull does not read them again. Paths relative to the
     * store; one not in the folder is `missing`. -> {placed, held, linked, missing}.
     */
    function adopt(paths) {
        return queue(async () => {
            if (!handle) return null;
            let placed = 0;
            const missing = [];
            const big = new Map();
            for (const rel of paths || []) {
                if (shared && keptOut(rel)) continue;
                let file = null;
                try {
                    const dir = await folderOf(rel, false);
                    file = await (await dir.getFileHandle(rel.split("/").pop())).getFile();
                } catch (error) {
                    missing.push(rel);
                    continue;
                }
                const parts = rel.split("/");
                for (let i = 1; i < parts.length; i += 1) {
                    const dir = parts.slice(0, i).join("/");
                    FS.mkdirTree(full(dir));
                    knownDirs.add(dir);
                }
                if (shared && file.size >= HELD_BYTES) {
                    held.set(rel, file.size);
                    big.set(rel, file);
                    continue;
                }
                const was = known.get(rel);
                if (was && was.remote === file.lastModified && was.size === file.size) continue; // a pull came first
                if (isOpen(rel)) continue; // the next pull places it
                placeLazy(rel, file, file.lastModified, file.size);
                known.set(rel, { local: file.lastModified, remote: file.lastModified, size: file.size, hash: null });
                placed += 1;
            }
            const linked = linkAll(big);
            return { placed, held: big.size, linked, missing };
        });
    }

    return {
        open,
        push,
        pull,
        adopt,
        get attached() { return Boolean(handle); },
        files: () => known.size,
        //: The files of a shared folder left there for their size.
        held: () => [...held.keys()],
        //: Of those, the ones Python reads where they lie (`linkHeld`).
        linked: () => [...links.keys()],
        //: The files in memory whose bytes have not been read from the folder.
        unread: () => [...unread.keys()],
    };
}
