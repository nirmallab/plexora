/**
 * PlexoraPyramid -- the website's builder of a flat image's or mask's pyramid.
 *
 * An image that is one big plane needs a pyramidized copy before the viewer
 * can show it (image_pyramid.pyramidize_image), and a flat segmentation mask a
 * label pyramid (segmentation_pyramid.pyramidize_segmentation_mask). On the
 * website Python has one thread and a 32-bit heap, and a copy it wrote would
 * sit in the browser's memory; so when Python's import reaches that step for a
 * picked TIFF it asks the page (providers/browser.py `page_pyramid`), and the
 * page builds the same file with its own workers (workers/pyramidWorker.js
 * over node/pyramid.js).
 *
 * Where the copy goes is the user's choice, made in the import's question
 * for an image (views/importSample.js): beside the original, in a folder they grant
 * (`chooseFolder`, Chromium only: a page may write into a folder only when
 * the user picks it and allows editing); otherwise in Plexora's own folder
 * when the projects are kept there (services/browserStore.js: Documents/
 * Plexora/pyramids/<key>/), else in the site's own storage, OPFS
 * plexora/derived/<key>/. A cancelled picker, or a folder write that fails,
 * leaves the copy in the browser. A file already under the copy's name in the
 * folder is never written over: Python judges whether it is a finished copy of
 * this image (`exists`), and if not the copy goes to the browser.
 *
 *   "pyramid" {path, key, name, kind, shape, dtype, factors, tile, description, browser?}
 *       -> {id}: a build started, the same build still running (asked again by
 *          a background job looking in, providers/browser.py), or a finished
 *          copy of the same source found
 *   "pyramid_status" {id, wait?}
 *       -> {state: "building"|"done"|"failed", tiles, total, path, where,
 *           existing, note, error}; the first "done" carries `mount`, which the
 *          worker mounts for Python at /derived/<key>/ before Python sees it.
 *          `wait` (ms): a build still running is answered when it ends or
 *          when that much time has passed, whichever is first
 *
 * Finished copies are the page's files like the user's picks: registered
 * with PlexoraPicks under their path (so the browser node serves their tiles)
 * and mounted for Python again at every load -- those in the browser by the
 * worker at boot, those in a folder here, where the browser still grants
 * access (`waiting()` / `reconnect()` otherwise, from a click).
 *
 * Classic script, `window.PlexoraPyramid`; needs PlexoraPython and PlexoraPicks.
 */
(function (global) {
    "use strict";

    // Under the site's base path (services/shell.js `BASE`; "" at its root).
    const WORKER = (globalThis.PLEXORA_SITE_BASE || "") + "/client/src/js/workers/pyramidWorker.js";
    const HANDS = (globalThis.PLEXORA_SITE_BASE || "") + "/client/dist/tiffWorker_bundle.js";
    const ROOT = "/derived";
    const WORKERS = Math.max(1, Math.min(12, (global.navigator.hardwareConcurrency || 4) - 2));
    const DB = "plexora-copies";
    const STORE = "copies";
    const builds = new Map();
    //: The folder chosen for the next build of a source, by its path.
    const targets = new Map();
    //: Copies in folders this visit cannot read yet (no permission without a click).
    const waiting = [];
    let nextId = 1;

    function database() {
        return new Promise((resolve, reject) => {
            const request = global.indexedDB.open(DB, 1);
            request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "path" });
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

    async function derived() {
        const root = await global.navigator.storage.getDirectory();
        const plexora = await root.getDirectoryHandle("plexora", { create: true });
        return plexora.getDirectoryHandle("derived", { create: true });
    }

    //: The finished copy under `key` in the browser, or null: its manifest is
    //: written last, and the file must be the size it says.
    async function finished(key, name) {
        try {
            const dir = await (await derived()).getDirectoryHandle(key);
            const manifest = JSON.parse(await (await (await dir.getFileHandle("manifest.json")).getFile()).text());
            if (name && manifest.name !== name) return null;
            const file = await (await dir.getFileHandle(manifest.name)).getFile();
            return file.size === manifest.bytes ? { manifest, file } : null;
        } catch (error) {
            return null;
        }
    }

    function adopt(path, file) {
        if (global.PlexoraPicks && global.PlexoraPicks.adopt) global.PlexoraPicks.adopt(path, file);
    }

    //: A notice in the app's frame (the host page has no toasts of its own).
    function notify(title, note) {
        const frame = global.document.getElementById("plexora-app");
        const toast = frame && frame.contentWindow && frame.contentWindow.PlexoraToast;
        if (toast) toast.show({ title, note });
    }

    const canWriteFolders = () => Boolean(global.PlexoraPicks && global.PlexoraPicks.canWriteFolders());

    /**
     * The folder to save `path`'s copy in, asked within the user's click (the
     * import's question): the picker opens at the image's own folder when the
     * page holds its handle. {name, beside} -- whether it is the original's
     * own folder, when that can be told -- or null when cancelled.
     */
    async function chooseFolder(path) {
        if (!canWriteFolders()) return null;
        const file = global.PlexoraPicks.handleFor ? global.PlexoraPicks.handleFor(path) : null;
        let folder;
        try {
            folder = await global.showDirectoryPicker({ id: "plexora-copies", mode: "readwrite",
                                                        ...(file ? { startIn: file } : {}) });
        } catch (error) {
            if (error && error.name === "AbortError") return null;
            throw error;
        }
        targets.set(path, folder);
        let beside = null;
        if (file) {
            const inside = await folder.resolve(file).catch(() => null);
            beside = Boolean(inside && inside.length === 1);
        }
        return { name: folder.name, beside };
    }

    function forgetFolder(path) {
        targets.delete(path);
    }

    //: A build has ended (done, or failed for good): the looks waiting on it
    //: are answered now.
    function settle(build) {
        const waiting = build.waiters || [];
        build.waiters = [];
        waiting.forEach((wake) => wake());
    }

    function run(build, message) {
        const worker = new Worker(WORKER, { type: "module" });
        const stop = () => worker.terminate();
        worker.onmessage = ({ data }) => {
            if (data.progress) {
                build.tiles = data.progress.tiles;
                build.total = data.progress.total;
            } else if (data.done && data.kept === "memory") {
                // This browser could not write the site's storage: the copy
                // came back whole, kept in IndexedDB where it can be.
                stop();
                keepInDatabase(build, data.file).then((where) => {
                    Object.assign(build, { state: "done", file: data.file, timings: data.timings, where,
                                           ms: Math.round(performance.now() - build.began) });
                    adopt(build.path, build.file);
                    if (where === "memory") {
                        build.note = "This browser cannot store it for the site; it is lost when the tab closes.";
                        notify("Pyramid kept until this tab closes", null);
                    }
                });
            } else if (data.done) {
                stop();
                Object.assign(build, { state: "done", file: data.file, timings: data.timings,
                                       ms: Math.round(performance.now() - build.began) });
                adopt(build.path, build.file);
                settle(build);
                if (message.folder) {
                    transact("readwrite", (store) => store.put({
                        path: build.path, key: build.key, name: build.name, folder: message.folder,
                        bytes: data.file.size, made: Date.now() })).catch(() => {});
                }
            } else if (data.exists) {
                stop();
                Object.assign(build, { state: "done", file: data.file, existing: true });
                adopt(build.path, build.file);
                settle(build);
            } else if (data.error) {
                stop();
                failed(build, message, data.error);
            }
        };
        worker.onerror = (event) => {
            stop();
            failed(build, message, event.message || "the pyramid worker failed");
        };
        worker.postMessage(message);
    }

    //: A copy built in memory, kept in IndexedDB beside the folder records
    //: ({path, key, name, blob, bytes}): "indexeddb", or "memory" when the
    //: database will not take it.
    async function keepInDatabase(build, file) {
        try {
            await transact("readwrite", (store) => store.put({
                path: build.path, key: build.key, name: build.name, blob: file, bytes: file.size, made: Date.now() }));
            return "indexeddb";
        } catch (error) {
            return "memory";
        }
    }

    //: A folder that could not take the copy: build it in the browser instead,
    //: and say so. A build in the browser that fails is the import's failure.
    function failed(build, message, error) {
        if (!message.folder) {
            Object.assign(build, { state: "failed", error });
            settle(build);
            return;
        }
        build.note = `It could not be saved in ${build.place} (${error}); it is kept in this browser instead.`;
        notify("Pyramid kept in this browser", build.note);
        Object.assign(build, { where: "browser", tiles: 0, total: 0 });
        run(build, { ...message, folder: null });
    }

    //: Plexora's pyramids folder for this copy, when the projects are kept in
    //: Plexora's folder (services/browserStore.js), else null.
    async function homeFolder(key) {
        const pyramids = global.PlexoraStore && global.PlexoraStore.pyramids();
        if (!pyramids) return null;
        try {
            return await pyramids.getDirectoryHandle(key, { create: true });
        } catch (error) {
            return null;
        }
    }

    async function start({ path, key, name, kind = "image", shape, dtype, factors, tile, description,
                           browser = false }) {
        // The same pyramid, building or built in this visit: a background job
        // asks again each time it looks in (it gives the worker back between
        // looks), and may ask once more after the build has ended.
        // Not when the user has just chosen a folder for this one.
        const same = !targets.has(path) && [...builds.values()].find((b) => b.key === key && b.name === name
            && b.state !== "failed" && (!browser || b.where === "browser"));
        if (same) return { value: { id: same.id } };
        const id = nextId++;
        const chosen = browser ? null : targets.get(path) || null;
        const folder = chosen || (browser ? null : await homeFolder(key));
        targets.delete(path);
        const build = { id, key, name, kind, state: "building", tiles: 0, total: 0, path: `${ROOT}/${key}/${name}`,
                        dir: `${ROOT}/${key}`, where: chosen ? "beside" : folder ? "plexora" : "browser",
                        place: chosen ? `“${chosen.name}”` : folder ? global.PlexoraStore.current().label
                            : "this browser",
                        file: null, error: null,
                        note: !browser ? null : kind === "labels"
                            ? "A file of that name in Plexora's folder is not this mask's pyramid; "
                              + "this one is kept in this browser."
                            : "A file of that name beside the original is not a copy of this image; "
                              + "this copy is kept in this browser.",
                        began: performance.now() };
        builds.set(id, build);
        if (!folder) {
            const found = await finished(key, name);
            const held = found ? null : await transact("readonly", (store) => store.get(build.path)).catch(() => null);
            if (found || (held && held.blob && held.bytes === held.blob.size)) {
                const file = found ? found.file : new File([held.blob], name);
                Object.assign(build, { state: "done", file, adopted: true, where: found ? "browser" : "indexeddb" });
                adopt(build.path, build.file);
                return { value: { id } };
            }
        }
        const file = global.PlexoraPicks ? global.PlexoraPicks.fileFor(path) : null;
        if (!file) throw new Error("the page does not hold that file now");
        // A long build, or one the browser has no room for, is said once
        // (services/handoffNotice.js); the build starts either way.
        if (global.PlexoraEnvelope && global.PlexoraEnvelope.beforeBuild) {
            global.PlexoraEnvelope.beforeBuild({ shape, dtype, kind, browser: !folder }).catch(() => null);
        }
        run(build, { op: "build", file, key, name, kind, shape, dtype, factors, tile, description,
                     hands: HANDS, workers: WORKERS, folder });
        return { value: { id } };
    }

    async function status({ id, wait = 0 }) {
        const build = builds.get(id);
        if (!build) return { value: { state: "failed", error: "no such build in this page" } };
        if (build.state === "building" && wait > 0) {
            await new Promise((resolve) => {
                const timer = setTimeout(resolve, Math.min(Number(wait), 5000));
                (build.waiters ||= []).push(() => { clearTimeout(timer); resolve(); });
            });
        }
        const value = { state: build.state, tiles: build.tiles, total: build.total, path: build.path,
                        where: build.where, existing: Boolean(build.existing), note: build.note,
                        error: build.error };
        if (build.state === "done" && !build.mountSent) {
            build.mountSent = true;
            value.mount = { dir: build.dir, files: [build.file] };
        }
        return { value };
    }

    async function mountCopy(record) {
        const file = record.blob ? new File([record.blob], record.name)
            : await (await record.folder.getFileHandle(record.name)).getFile();
        if (file.size !== record.bytes) return false; // changed since: not ours to read as the copy
        await global.PlexoraPython.mount(`${ROOT}/${record.key}`, [file]);
        adopt(record.path, file);
        return true;
    }

    //: Every finished copy, as the page's files again: those in the browser
    //: (the worker mounted them at boot), and those in folders still granted.
    async function restore() {
        const out = [];
        try {
            for await (const [key, handle] of (await derived()).entries()) {
                if (handle.kind !== "directory") continue;
                const found = await finished(key, null);
                if (!found) continue;
                const path = `${ROOT}/${key}/${found.manifest.name}`;
                adopt(path, found.file);
                out.push(path);
            }
        } catch (error) {
            // No OPFS (a private window, an old Safari): nothing was built here.
        }
        waiting.length = 0;
        let records = [];
        try { records = await transact("readonly", (store) => store.getAll()); } catch (error) { /* none */ }
        for (const record of records) {
            try {
                if (record.blob) {
                    // Kept in the database (this browser could not write the
                    // site's files): no permission to ask.
                    if (await mountCopy(record)) out.push(record.path);
                    continue;
                }
                if (await record.folder.queryPermission({ mode: "read" }) === "granted" && await mountCopy(record)) {
                    out.push(record.path);
                } else {
                    waiting.push(record);
                }
            } catch (error) {
                waiting.push(record);
            }
        }
        return out;
    }

    //: From a click: ask again for the folders whose copies are waiting.
    async function reconnect() {
        const done = [];
        for (const record of waiting.splice(0)) {
            try {
                if (await record.folder.requestPermission({ mode: "read" }) === "granted" && await mountCopy(record)) {
                    done.push(record.path);
                    continue;
                }
            } catch (error) { /* still waiting */ }
            waiting.push(record);
        }
        return { mounted: done, waiting: waiting.map((record) => record.path) };
    }

    if (global.PlexoraPython && global.PlexoraPython.answers) {
        global.PlexoraPython.answers("pyramid", start);
        global.PlexoraPython.answers("pyramid_status", status);
    }

    global.PlexoraPyramid = Object.freeze({
        restore,
        reconnect,
        chooseFolder,
        forgetFolder,
        canWriteFolders,
        waiting: () => waiting.map((record) => ({ path: record.path, folder: record.folder.name })),
        builds: () => [...builds.values()].map(({ file, waiters, ...rest }) => ({ ...rest, bytes: file ? file.size : null })),
    });
})(window);
