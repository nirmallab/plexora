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
 * for an image (views/importSample.js): in Plexora's folder when the projects
 * are kept there (services/browserStore.js: Documents/Plexora/pyramids/
 * <key>/), or in a folder they grant (`chooseFolder`, Chromium only: a page
 * may write into a folder only when the user picks it and allows editing),
 * usually beside the original; otherwise in the site's own storage, OPFS
 * plexora/derived/<key>/ (moved into Plexora's folder later, when the
 * projects are kept there: services/derivedIndex.js `migrate`). A cancelled
 * picker, or a folder write that fails, leaves the copy in the browser. A
 * file already under the copy's name in a folder is never written over:
 * Python judges whether it is a finished copy of this image (`existing`), and
 * if not the copy goes to the browser.
 *
 * Before anything is built every home is looked in, in one order
 * (services/derivedIndex.js `lookup`): a copy made on an earlier visit, in
 * any of them, is used again, wherever the store is this visit.
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
 * and mounted for Python again at every load, through the index of every
 * copy the page made (services/derivedIndex.js): those in the browser by the
 * worker at boot, those in folders where the browser still grants access
 * (`waiting()` / `reconnect()` otherwise, from a click).
 *
 * Classic script, `window.PlexoraPyramid`; needs PlexoraPython, PlexoraPicks and
 * PlexoraDerived.
 */
(function (global) {
    "use strict";

    // Under the site's base path (services/shell.js `BASE`; "" at its root).
    const WORKER = (globalThis.PLEXORA_SITE_BASE || "") + "/client/src/js/workers/pyramidWorker.js";
    const HANDS = (globalThis.PLEXORA_SITE_BASE || "") + "/client/dist/tiffWorker_bundle.js";
    const ROOT = "/derived";
    const WORKERS = Math.max(1, Math.min(12, (global.navigator.hardwareConcurrency || 4) - 2));
    const builds = new Map();
    //: The folder chosen for the next build of a source, by its path.
    const targets = new Map();
    let nextId = 1;

    const derived = () => global.PlexoraDerived || null;

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

    // -- pace ----------------------------------------------------------------
    //
    // A build takes every core it is given, and the page's other work shared
    // them: on LSP11385 (Mac, 10 cores) a plain mask's build on eight hands
    // kept the first open of its project waiting 20 s (the local server: 4.8 s),
    // Python opening the 66 GB image in 11 s instead of 1 s and the table read
    // in 3.8 s instead of 0.6 s. So a build works on one hand while the page
    // waits on something -- a request Python has taken more than a moment over,
    // or queued behind one (PlexoraPython.busy: a status poll holds nothing
    // back), the browser node reading tiles (PlexoraNode.busy) -- and on all
    // of them otherwise. Not while a request waits on the build itself: an import of
    // a plain image looks at its build every quarter second without waiting
    // (providers/browser.py `page_pyramid`, `hold`), and gets every hand.
    const PACE_MS = 100;
    const AWAITED_MS = 1000;

    function waitedOn() {
        const python = global.PlexoraPython;
        const node = global.PlexoraNode;
        return Boolean((python && python.busy && python.busy()) || (node && node.busy && node.busy()));
    }

    function pace(build) {
        if (!build.worker) return;
        const awaited = performance.now() - (build.awaitedAt || -Infinity) < AWAITED_MS;
        const hands = !awaited && waitedOn() ? 1 : WORKERS;
        if (hands === build.hands) return;
        build.hands = hands;
        build.worker.postMessage({ op: "pace", hands });
    }

    function run(build, message) {
        const worker = new Worker(WORKER, { type: "module" });
        build.worker = worker;
        build.hands = message.workers;
        clearInterval(build.pacer);
        build.pacer = setInterval(() => pace(build), PACE_MS);
        const stop = () => {
            clearInterval(build.pacer);
            build.worker = null;
            worker.terminate();
        };
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
                    settle(build);
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
                // Every copy is recorded where it was made, so any later
                // visit finds it before building (services/derivedIndex.js).
                keep(build, message.folder ? build.where : "opfs", data.file, message.folder);
            } else if (data.exists) {
                stop();
                Object.assign(build, { state: "done", file: data.file, existing: true });
                adopt(build.path, build.file);
                settle(build);
                // Python judges it now; recorded meanwhile, so the copy it
                // adopts is found again next visit. One it refuses is
                // recorded over by the browser's own build.
                keep(build, build.where, data.file, message.folder);
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
        pace(build);
    }

    //: The copy recorded in the index under its home: "beside" (a folder the
    //: user chose), "plexora" (Plexora's folder), "opfs", "indexeddb".
    function keep(build, home, file, folder = null, extra = {}) {
        const index = derived();
        if (!index) return Promise.resolve(null);
        return index.record({ path: build.path, key: build.key, name: build.name, kind: build.kind, home,
                              ...(home === "beside" && folder ? { folder } : {}),
                              bytes: file.size, source: build.source, ...extra }, file).catch(() => null);
    }

    //: A copy built in memory, kept in IndexedDB in the index (`blob`):
    //: "indexeddb", or "memory" when the database will not take it.
    async function keepInDatabase(build, file) {
        return (await keep(build, "indexeddb", file, null, { blob: file })) ? "indexeddb" : "memory";
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
        targets.delete(path);
        const build = { id, key, name, kind, state: "building", tiles: 0, total: 0, path: `${ROOT}/${key}/${name}`,
                        dir: `${ROOT}/${key}`, source: path, where: "browser", place: "this browser",
                        file: null, error: null,
                        note: !browser ? null : kind === "labels"
                            ? "A file of that name in Plexora's folder is not this mask's pyramid; "
                              + "this one is kept in this browser."
                            : "A file of that name beside the original is not a copy of this image; "
                              + "this copy is kept in this browser.",
                        began: performance.now() };
        builds.set(id, build);
        // A copy made before, wherever it is (services/derivedIndex.js: the
        // index, Plexora's folder, the site's storage, IndexedDB), whatever
        // this visit's choices: built once, used from then on. A file found
        // under the name but never recorded here is handed back unread for
        // Python to judge (`existing`), as a folder's file always was.
        const index = derived();
        const found = index ? await index.lookup(key, name, { browserOnly: browser }).catch(() => null) : null;
        if (found) {
            const own = found.home === "opfs" || found.home === "indexeddb";
            Object.assign(build, { state: "done", file: found.file, existing: !found.trusted, adopted: found.trusted,
                                   where: own ? "browser" : found.home });
            adopt(build.path, build.file);
            if (!found.trusted) keep(build, found.home, found.file);
            if (chosen && own && index.copyTo) index.copyTo(build.path, chosen).catch(() => null);
            return { value: { id } };
        }
        // Where it is built: the folder chosen in the import's question; else
        // Plexora's folder when the projects are kept there and it is open;
        // else the site's own storage (moved to Plexora's folder later, when
        // that is where projects are kept).
        const folder = chosen || (browser ? null : await homeFolder(key));
        Object.assign(build, { where: chosen ? "beside" : folder ? "plexora" : "browser",
                               place: chosen ? `“${chosen.name}”` : folder ? global.PlexoraStore.current().label
                                   : "this browser" });
        if (chosen) {
            // A file already under the copy's name there -- a copy an earlier
            // visit made, asked for again when a project could not mount it
            // -- is handed back unread for Python to judge (`existing`), as
            // the worker would hand it back after opening the original in
            // every one of its readers.
            const there = await chosen.getFileHandle(name).then((handle) => handle.getFile()).catch(() => null);
            if (there) {
                Object.assign(build, { state: "done", file: there, existing: true });
                adopt(build.path, build.file);
                keep(build, "beside", there, chosen);
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
        // A look that does not wait is a request holding out for the build
        // (`page_pyramid`, `hold`): every hand, while it does (`pace`).
        if (!(Number(wait) > 0)) {
            build.awaitedAt = performance.now();
            pace(build);
        }
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

    //: Every finished copy, as the page's files again, mounted for Python
    //: where the browser still grants access (services/derivedIndex.js).
    async function restore() {
        const index = derived();
        return index ? index.restore() : [];
    }

    //: From a click: ask again for the folders whose copies are waiting.
    async function reconnect() {
        const index = derived();
        return index ? index.reconnect() : { mounted: [], waiting: [] };
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
        waiting: () => (derived() ? derived().waiting() : []),
        builds: () => [...builds.values()].map(({ file, waiters, worker, pacer, ...rest }) => ({ ...rest, bytes: file ? file.size : null })),
    });
})(window);
