/**
 * The files the website's chunk cache is kept in (plexora/browser/cachefiles.py):
 * OPFS plexora/cache/, read and written by Python synchronously.
 *
 * Python reaches OPFS only through sync access handles, and a handle is
 * opened with a promise, which Python cannot wait on in every browser. So
 * every file the cache will use is opened here, before Python starts: two
 * index snapshots (`index-0`, `index-1`) and SEGMENTS segment files, which
 * Python packs values into. The files stay open for the worker's life.
 *
 * A sync access handle is exclusive. The tab being reloaded may still hold
 * them for a moment, so a refused open is tried again for a while; a second
 * tab of the site never gets them and caches in memory instead ({error}).
 *
 *   openCacheFiles({segments, dir}) -> {files, quota} | {error}
 *   files: {count, size(i), read(i, at, length) -> Uint8Array,
 *           write(i, at, bytes) -> written, truncate(i, length), flush(i)}
 *   (files 0 and 1 are the index snapshots, 2..count-1 the segments)
 */

export const SEGMENTS = 32;
const INDEX_FILES = 2;
const RETRY_MS = 250;
const RETRIES = 12;

const nameOf = (i) => (i < INDEX_FILES ? `index-${i}` : `segment-${String(i - INDEX_FILES).padStart(2, "0")}`);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function cacheDir() {
    const root = await navigator.storage.getDirectory();
    const plexora = await root.getDirectoryHandle("plexora", { create: true });
    return plexora.getDirectoryHandle("cache", { create: true });
}

async function openAll(dir, count) {
    const handles = [];
    try {
        for (let i = 0; i < count; i += 1) {
            const file = await dir.getFileHandle(nameOf(i), { create: true });
            handles.push(await file.createSyncAccessHandle());
        }
        return handles;
    } catch (error) {
        for (const handle of handles) {
            try { handle.close(); } catch (closing) { /* already closed */ }
        }
        throw error;
    }
}

export async function openCacheFiles({ segments = SEGMENTS, dir = null, retries = RETRIES } = {}) {
    if (typeof FileSystemFileHandle === "undefined"
        || typeof FileSystemFileHandle.prototype.createSyncAccessHandle !== "function") {
        return { error: "this browser cannot write the site's storage from a worker" };
    }
    const count = INDEX_FILES + segments;
    let handles = null;
    let last = null;
    try {
        const folder = dir || await cacheDir();
        for (let attempt = 0; attempt <= retries && !handles; attempt += 1) {
            try {
                handles = await openAll(folder, count);
            } catch (error) {
                last = error;
                // Held by another worker: the page being reloaded lets go in
                // a moment, another tab never does.
                if (error?.name !== "NoModificationAllowedError") break;
                await sleep(RETRY_MS);
            }
        }
    } catch (error) {
        last = error;
    }
    if (!handles) {
        const held = last?.name === "NoModificationAllowedError";
        return { error: held ? "another tab of this site is using it" : String(last?.message || last) };
    }
    let quota = null;
    try {
        quota = (await navigator.storage.estimate()).quota ?? null;
    } catch (error) {
        quota = null;
    }
    const files = {
        count,
        size: (i) => handles[i].getSize(),
        read: (i, at, length) => {
            const buffer = new Uint8Array(length);
            const got = handles[i].read(buffer, { at });
            return got === length ? buffer : buffer.subarray(0, got);
        },
        write: (i, at, bytes) => handles[i].write(bytes, { at }),
        truncate: (i, length) => handles[i].truncate(length),
        flush: (i) => handles[i].flush(),
    };
    return { files, quota };
}
