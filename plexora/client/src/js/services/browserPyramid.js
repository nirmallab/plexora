/**
 * PlexoraPyramid -- the website's builder of a flat image's pyramid.
 *
 * An image that is one big plane needs a pyramidized copy before the viewer
 * can show it (image_pyramid.pyramidize_image). On the website Python has one
 * thread and a 32-bit heap, and a copy it wrote would sit in the browser's
 * memory; so when Python's import reaches that step for a picked TIFF it asks
 * the page (providers/browser.py `page_pyramid`), and the page builds the
 * same copy with its own workers (workers/pyramidWorker.js over node/
 * pyramid.js) into the site's storage, OPFS plexora/derived/<key>/.
 *
 *   "pyramid" {path, key, name, shape, dtype, factors, tile, description}
 *       -> {id}: a build started, or a finished copy of the same source found
 *   "pyramid_status" {id}
 *       -> {state: "building"|"done"|"failed", tiles, total, path, error}; the
 *          first "done" carries `mount`, which the worker mounts for Python at
 *          /derived/<key>/ before Python sees the answer
 *
 * Finished copies are the page's files like the user's picks: registered
 * with PlexoraPicks under their path (so the browser node serves their tiles)
 * at every load, and mounted for Python by the worker at boot.
 *
 * Classic script, `window.PlexoraPyramid`; needs PlexoraPython and PlexoraPicks.
 */
(function (global) {
    "use strict";

    const WORKER = "/client/src/js/workers/pyramidWorker.js";
    const HANDS = "/client/dist/tiffWorker_bundle.js";
    const ROOT = "/derived";
    const WORKERS = Math.max(1, Math.min(12, (global.navigator.hardwareConcurrency || 4) - 2));
    const builds = new Map();
    let nextId = 1;

    async function derived() {
        const root = await global.navigator.storage.getDirectory();
        const plexora = await root.getDirectoryHandle("plexora", { create: true });
        return plexora.getDirectoryHandle("derived", { create: true });
    }

    //: The finished copy under `key`, or null: its manifest is written last,
    //: and the file must be the size it says.
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

    function adopt(build) {
        if (global.PlexoraPicks && global.PlexoraPicks.adopt) global.PlexoraPicks.adopt(build.path, build.file);
    }

    async function start({ path, key, name, shape, dtype, factors, tile, description }) {
        const id = nextId++;
        const build = { id, state: "building", tiles: 0, total: 0, path: `${ROOT}/${key}/${name}`,
                        dir: `${ROOT}/${key}`, file: null, error: null, began: performance.now() };
        const found = await finished(key, name);
        if (found) {
            Object.assign(build, { state: "done", file: found.file, adopted: true });
            adopt(build);
            builds.set(id, build);
            return { value: { id } };
        }
        const file = global.PlexoraPicks ? global.PlexoraPicks.fileFor(path) : null;
        if (!file) throw new Error("the page does not hold that file now");
        const worker = new Worker(WORKER, { type: "module" });
        worker.onmessage = ({ data }) => {
            if (data.progress) {
                build.tiles = data.progress.tiles;
                build.total = data.progress.total;
            } else if (data.done) {
                Object.assign(build, { state: "done", file: data.file, timings: data.timings,
                                       ms: Math.round(performance.now() - build.began) });
                adopt(build);
                worker.terminate();
            } else if (data.error) {
                Object.assign(build, { state: "failed", error: data.error });
                worker.terminate();
            }
        };
        worker.onerror = (event) => {
            Object.assign(build, { state: "failed", error: event.message || "the pyramid worker failed" });
            worker.terminate();
        };
        worker.postMessage({ op: "build", file, key, name, shape, dtype, factors, tile, description,
                             hands: HANDS, workers: WORKERS });
        builds.set(id, build);
        return { value: { id } };
    }

    async function status({ id }) {
        const build = builds.get(id);
        if (!build) return { value: { state: "failed", error: "no such build in this page" } };
        const value = { state: build.state, tiles: build.tiles, total: build.total, path: build.path,
                        error: build.error };
        if (build.state === "done" && !build.mountSent) {
            build.mountSent = true;
            value.mount = { dir: build.dir, files: [build.file] };
        }
        return { value };
    }

    //: Every finished copy in this site's storage, as the page's files again.
    async function restore() {
        const out = [];
        try {
            for await (const [key, handle] of (await derived()).entries()) {
                if (handle.kind !== "directory") continue;
                const found = await finished(key, null);
                if (!found) continue;
                const path = `${ROOT}/${key}/${found.manifest.name}`;
                if (global.PlexoraPicks && global.PlexoraPicks.adopt) global.PlexoraPicks.adopt(path, found.file);
                out.push(path);
            }
        } catch (error) {
            // No OPFS (a private window, an old Safari): nothing was built here.
        }
        return out;
    }

    if (global.PlexoraPython && global.PlexoraPython.answers) {
        global.PlexoraPython.answers("pyramid", start);
        global.PlexoraPython.answers("pyramid_status", status);
    }
    const restored = restore();

    global.PlexoraPyramid = Object.freeze({
        restored,
        builds: () => [...builds.values()].map(({ file, ...rest }) => ({ ...rest, bytes: file ? file.size : null })),
    });
})(window);
