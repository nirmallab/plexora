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
 *   createStore(FS, root = "/data") -> {open(handle), push(), pull(), files()}
 */

const now = () => performance.now();

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

export function createStore(FS, root = "/data", log = () => {}) {
    let handle = null;
    //: rel path -> {local, remote, size, hash}: as last read or written.
    const known = new Map();
    //: When the last write-back looked (ms, the clock memory file times use).
    let lastPush = 0;
    //: Folders known to exist on both sides (an empty project folder is kept too).
    const knownDirs = new Set();
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
        const visit = async (dir, rel) => {
            for await (const [name, child] of dir.entries()) {
                if (name.endsWith(".crswap")) continue; // a write in progress
                const path = rel ? `${rel}/${name}` : name;
                if (child.kind === "directory") {
                    dirs.add(path);
                    await visit(child, path);
                } else {
                    const file = await child.getFile();
                    files.set(path, { mtime: file.lastModified, size: file.size, file });
                }
            }
        };
        await visit(handle, "");
        return { files, dirs };
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
        FS.writeFile(path, data);
        FS.utime(path, mtime, mtime);
    }

    function clear(path = root) {
        for (const name of FS.readdir(path)) {
            if (name === "." || name === "..") continue;
            const child = `${path}/${name}`;
            if (FS.isDir(FS.stat(child).mode)) {
                clear(child);
                FS.rmdir(child);
            } else {
                FS.unlink(child);
            }
        }
    }

    //: Use `dir` from now on: what it holds is read into memory in place of
    //: what was there.
    function open(dir) {
        return queue(async () => {
            const began = now();
            FS.mkdirTree(root);
            clear();
            known.clear();
            knownDirs.clear();
            handle = dir;
            const remote = await remoteWalk();
            for (const rel of remote.dirs) {
                FS.mkdirTree(full(rel));
                knownDirs.add(rel);
            }
            for (const [rel, file] of remote.files) {
                const bytes = new Uint8Array(await file.file.arrayBuffer());
                readIn(rel, bytes, file.mtime);
                known.set(rel, { local: file.mtime, remote: file.mtime, size: file.size, hash: fingerprint(bytes) });
            }
            return { files: known.size, ms: Math.round(now() - began) };
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
                const bytes = new Uint8Array(await file.file.arrayBuffer());
                readIn(rel, bytes, file.mtime);
                known.set(rel, { local: file.mtime, remote: file.mtime, size: file.size, hash: fingerprint(bytes) });
                read += 1;
            }
            for (const [rel, was] of [...known]) {
                if (remote.files.has(rel)) continue;
                const here = local.files.get(rel);
                if (here && here.mtime !== was.local) continue; // changed here: written back next
                if (isOpen(rel)) continue;
                try { FS.unlink(full(rel)); } catch (error) { /* gone */ }
                known.delete(rel);
                dropped += 1;
            }
            return { read, dropped };
        });
    }

    return {
        open,
        push,
        pull,
        get attached() { return Boolean(handle); },
        files: () => known.size,
    };
}
