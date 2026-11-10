/**
 * PlexoraBundleWeb -- a project's `.plexora` file on the website.
 *
 * The export and import dialogs (views/projectExport.js, projectImport.js)
 * ask everything of `window.PlexoraBundleTransport`. On the desktop app and a
 * server those are their routes' jobs. Here the website's Python has a 4 GiB
 * heap and leaves large files in the folder (workers/storeSync.js), so it
 * only decides, and the bytes move in a worker of their own
 * (workers/bundleWorker.js); the file is the one the desktop app writes and
 * reads (server/models/bundles.py, docs/internal/PROJECT_BUNDLES.md).
 *
 * Export
 *  1. The Save dialog, first, on the click (Chrome, Edge: the file is written
 *     once, where the user chose). Elsewhere (Firefox, Safari) it is written
 *     into this site's storage and downloaded from there when it is done.
 *  2. The store written back (`PlexoraPython.flush`), then
 *     `POST /projects/export/plan`: every member, and where it is.
 *  3. Each member found: a store's file in its folder (the worker opens it),
 *     a picked file from PlexoraPicks, a pyramid this site built from its
 *     storage, and what Python made for the export (database snapshots,
 *     entries) read from Python.
 *  4. The worker streams them, CRC-32 and SHA-256 as they go;
 *     `POST /projects/export/manifest` turns the sums into the manifest, with
 *     each project's revision, and the worker writes it first, then reads the
 *     file's directory back.
 * Import
 *  1. The file picked is mounted read-only for Python (WORKERFS): its zipfile
 *     reads the central directory and the members it needs a slice at a
 *     time, so the preview is Python's own, from the file itself.
 *  2. On Import, `POST /projects/import/preview` with `place_from` says where
 *     each member of PLACE_FROM bytes or more goes; the worker unpacks those
 *     straight into the store's folder, each CRC and sha256 checked, and
 *     Python is told they are there (`PlexoraPython.adopt`).
 *  3. `POST /projects/import` with `placed`: Python unpacks the rest (the
 *     database, whose rows it renames, the entries, bookmarks, figures),
 *     places the projects and writes the registry last. A failure or a
 *     cancel takes back what the worker wrote.
 *
 * The jobs run in the host page (services/shell.js loads this file there), so
 * a page change in the app's frame does not stop them; the frame's own copy
 * (base.html) installs the transport, on the website only, and works alone
 * where the host has none.
 *
 *   PlexoraBundleWeb.transport(win)  -> the dialogs' transport for window `win`
 *   PlexoraBundleWeb.jobs()          -> the jobs' statuses (tests)
 *
 * Classic script. Needs PlexoraPython (and PlexoraPicks for picked images).
 */
(function (global) {
    "use strict";

    //: An import's members of this many bytes or more are written by the
    //: page's worker; smaller ones by Python, as the store holds them in
    //: memory anyway (storeSync.js LAZY_BYTES).
    const PLACE_FROM = 64 * 1024;
    //: A file only Python can reach is read from it below this size; a larger
    //: one is left out of the file rather than held whole in two heaps.
    const READ_FROM_PYTHON_MAX = 256 * 1024 * 1024;
    const POLL_MS = 500;
    const EXPORT_STAGES = {snapshot: [0, 3, "Taking a snapshot"], pack: [3, 97, "Packing"],
                           verify: [97, 100, "Checking the file"]};
    const IMPORT_STAGES = {verify: [0, 3, "Checking the file"], unpack: [3, 93, "Unpacking"],
                           place: [93, 100, "Adding the projects"]};

    //: The window that runs the jobs and holds Python: the host page, from
    //: the app's frame or from itself.
    function hostOf(win) {
        try {
            if (win.parent && win.parent !== win && win.parent.PlexoraPython) return win.parent;
        } catch (error) {
            // Another origin: not the website's frame.
        }
        return win;
    }

    function createEngine(host) {
        const base = () => host.PLEXORA_SITE_BASE || global.PLEXORA_SITE_BASE || "";
        const python = () => host.PlexoraPython;
        const jobs = new Map();
        //: Save dialog files by the `dest` the dialog hands back.
        const saves = new Map();
        //: Picked `.plexora` files by the path Python reads them at.
        const bundles = new Map();
        let serial = 0;

        function token(prefix) {
            serial += 1;
            const random = Math.random().toString(16).slice(2, 10);
            return `${prefix}-${Date.now().toString(36)}${serial}${random}`;
        }

        async function call(path, body, fallback) {
            const response = await python().fetch(path, body === undefined ? undefined : {
                method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
            const answer = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(answer.error || fallback || `Plexora answered ${response.status}.`);
            return answer;
        }

        // -- the worker --------------------------------------------------------

        function mover() {
            const worker = new Worker(`${base()}/client/src/js/workers/bundleWorker.js`, {type: "module"});
            let pending = null;
            let onProgress = null;
            worker.onmessage = ({data}) => {
                if (data.progress) {
                    if (onProgress) onProgress(data.progress);
                    return;
                }
                const waiting = pending;
                pending = null;
                if (!waiting) return;
                if (data.error) waiting.reject(Object.assign(new Error(data.error), {cancelled: Boolean(data.cancelled)}));
                else waiting.resolve(data.packed || data.done);
            };
            worker.onerror = (event) => {
                if (event && event.preventDefault) event.preventDefault();
                const waiting = pending;
                pending = null;
                if (waiting) waiting.reject(new Error((event && event.message) || "The file's worker stopped."));
            };
            return {
                ask(message, progress) {
                    onProgress = progress || null;
                    return new Promise((resolve, reject) => {
                        pending = {resolve, reject};
                        worker.postMessage({job: 1, ...message});
                    });
                },
                cancel() { worker.postMessage({op: "cancel"}); },
                stop() { worker.terminate(); },
            };
        }

        // -- the stores --------------------------------------------------------

        //: {root: {handle} | {opfs}} for the stores the worker can open.
        function reachable(stores) {
            const out = {};
            for (const [root, spec] of Object.entries(stores || {})) {
                if (spec && spec.handle) out[root] = {handle: spec.handle};
                else if (spec && spec.opfs) out[root] = {opfs: spec.opfs};
            }
            return out;
        }

        //: The store a Python path is in: {store, rel}, or null.
        function storeOf(path, reach) {
            let best = null;
            for (const root of Object.keys(reach)) {
                if (String(path).startsWith(`${root}/`) && (!best || root.length > best.length)) best = root;
            }
            if (!best) return null;
            const rel = String(path).slice(best.length + 1);
            if (!rel || rel.split("/").some((part) => !part || part === "." || part === "..")) return null;
            return {store: best, rel};
        }

        //: Each plan member's source as the worker takes it.
        async function sourcesOf(members, reach) {
            const picks = host.PlexoraPicks;
            const out = [];
            for (const member of members) {
                const path = String(member.source || "");
                let source = null;
                const inStore = storeOf(path, reach);
                if (inStore) {
                    source = inStore;
                } else if (path.startsWith("/picked/") && picks && picks.fileFor(path)) {
                    source = {blob: picks.fileFor(path)};
                } else if (/^\/derived\/[^/]+\/[^/]+$/.test(path)) {
                    // A pyramid this site built (workers/pyramidWorker.js), in its storage.
                    const [, , key, name] = path.split("/");
                    source = {opfs: ["plexora", "derived", key, name]};
                } else if (Number(member.size || 0) < READ_FROM_PYTHON_MAX) {
                    try {
                        source = {blob: new Blob([await python().readFile(path)])};
                    } catch (error) {
                        source = {gone: "gone before it was packed"};
                    }
                } else {
                    source = {gone: "too large to read in this browser"};
                }
                out.push({bundle_path: member.bundle_path, size: member.size, source});
            }
            return out;
        }

        // -- statuses ----------------------------------------------------------

        function record(kind, stages) {
            const id = token(kind === "export" ? "webx" : "webi");
            const first = Object.keys(stages)[0];
            const job = {id, kind, stages, cancelled: false, mover: null, python: null, placed: [], made: [],
                         status: {job: id, kind, status: "pending", progress: 0, stage: first,
                                  stage_label: stages[first][2], error: null, done: 0, total: 0, result: null}};
            jobs.set(id, job);
            return job;
        }

        function stage(job, key, done = 0, total = 0) {
            const [from, to, label] = job.stages[key];
            const share = total > 0 ? Math.min(1, done / total) : 0;
            Object.assign(job.status, {stage: key, stage_label: label, done, total,
                                       progress: Math.round(from + (to - from) * share)});
        }

        function end(job, status, extra = {}) {
            Object.assign(job.status, {status, ...extra});
            if (status === "ready") Object.assign(job.status, {progress: 100, stage: "ready", stage_label: "Ready"});
            if (status === "cancelled") Object.assign(job.status, {progress: 0, stage: "cancelled", stage_label: "Stopped"});
            if (status === "failed") Object.assign(job.status, {stage: "failed", stage_label: "Failed"});
            if (job.mover) {
                job.mover.stop();
                job.mover = null;
            }
        }

        function checkCancelled(job) {
            if (job.cancelled) throw Object.assign(new Error("Stopped."), {cancelled: true});
        }

        // -- export ------------------------------------------------------------

        //: Exports written into this site's storage, but the ones still
        //: wanted: the last one stays until the next starts (it may be
        //: downloaded again).
        async function sweep(keep) {
            try {
                const root = await navigator.storage.getDirectory();
                const plexora = await root.getDirectoryHandle("plexora", {create: true});
                const folder = await plexora.getDirectoryHandle("bundles", {create: true});
                for await (const [name] of folder.entries()) {
                    if (!keep.has(name)) await folder.removeEntry(name, {recursive: true}).catch(() => {});
                }
            } catch (error) {
                // Nothing kept there.
            }
        }

        async function runExport(job, {ids, dataset, include, save, name}) {
            try {
                stage(job, "snapshot");
                await python().flush();
                checkCancelled(job);
                const plan = await call("/projects/export/plan", {ids, include, dataset: dataset || null},
                                        "The export could not be planned.");
                checkCancelled(job);
                const reach = reachable(await python().bundleStores());
                const members = await sourcesOf(plan.members || [], reach);
                checkCancelled(job);
                let out;
                if (save) {
                    out = {handle: save};
                    job.status.file = {name: save.name, size: null, path: save.name, url: null};
                } else {
                    await sweep(new Set([...jobs.values()].filter((other) => other.kind === "export"
                        && other.status.status === "pending").map((other) => other.id)));
                    out = {opfs: ["plexora", "bundles", job.id], name};
                    job.status.file = {name, size: null, path: null, url: null};
                }
                const total = members.reduce((sum, member) => sum + Number(member.size || 0), 0);
                stage(job, "pack", 0, total);
                job.mover = mover();
                const packed = await job.mover.ask({op: "export", members, reserve: plan.reserve, out,
                                                    stores: reach},
                                                   (progress) => stage(job, "pack", progress.done, total));
                // Packed, its file open, waiting for the manifest.
                job.waiting = true;
                checkCancelled(job);
                const manifest = await call("/projects/export/manifest", {
                    manifest: plan.manifest, sums: packed.sums, left_out: packed.left_out, entries: plan.entries,
                    stamps: plan.stamps, work_dir: plan.work_dir}, "The file's manifest could not be made.");
                checkCancelled(job);
                stage(job, "verify");
                job.waiting = false;
                const done = await job.mover.ask({op: "finish", manifest: JSON.stringify(manifest, null, 1)});
                job.saved = save || null;
                job.file = done.file || null;
                job.status.file.size = done.size;
                end(job, "ready", {result: {
                    size: done.size, left_out: manifest.left_out || [],
                    projects: (manifest.projects || []).map((p) => ({id: p.id, name: p.name, revision: p.revision}))}});
            } catch (error) {
                // A file left open while the page asked Python for the
                // manifest is taken away before the worker goes.
                if (job.mover && job.waiting) {
                    await Promise.race([job.mover.ask({op: "cancel"}).catch(() => null),
                                        new Promise((resolve) => setTimeout(resolve, 5000))]);
                }
                if (error && error.cancelled || job.cancelled) end(job, "cancelled");
                else end(job, "failed", {error: error.message || String(error)});
            }
        }

        // -- import ------------------------------------------------------------

        async function takeBack(job) {
            if (!job.placed.length) return;
            const remover = mover();
            try {
                await remover.ask({op: "remove", paths: job.placed, made: job.made, stores: job.reach});
            } catch (error) {
                // What is left is the next pull's to find.
            } finally {
                remover.stop();
            }
            job.placed = [];
            await python().pull().catch(() => null);
        }

        async function runImport(job, {path, choices, location}) {
            try {
                stage(job, "verify");
                const file = bundles.get(path) || null;
                const answer = await call("/projects/import/preview", {path, location: location || null,
                                                                      choices: choices || {}, place_from: PLACE_FROM},
                                          "That file could not be read.");
                checkCancelled(job);
                const reach = reachable(await python().bundleStores());
                job.reach = reach;
                const members = [];
                for (const item of answer.placements || []) {
                    const owner = storeOf(item.target, reach);
                    if (owner) members.push({path: item.path, ...owner, size: item.size, sha256: item.sha256 || null});
                }
                const placed = {};
                if (members.length && file) {
                    const total = members.reduce((sum, member) => sum + member.size, 0);
                    stage(job, "unpack", 0, total);
                    job.mover = mover();
                    const done = await job.mover.ask({op: "extract", file, members, stores: reach},
                                                     (progress) => stage(job, "unpack", progress.done, total));
                    job.mover.stop();
                    job.mover = null;
                    job.placed = done.placed || [];
                    job.made = done.made || [];
                    checkCancelled(job);
                    await python().adopt(job.placed.map((item) => `${item.store}/${item.rel}`));
                    for (const item of members) placed[item.path] = `${item.store}/${item.rel}`;
                }
                checkCancelled(job);
                const started = await call("/projects/import", {path, choices: choices || {}, location: location || null,
                                                               placed}, "The import could not start.");
                job.python = started.job;
                await follow(job, started);
            } catch (error) {
                if (job.mover) job.mover.cancel();
                await takeBack(job);
                if (error && error.cancelled || job.cancelled) end(job, "cancelled");
                else end(job, "failed", {error: error.message || String(error)});
            }
        }

        //: Python's import job, until it ends: its stages after the page's.
        async function follow(job, status) {
            let misses = 0;
            for (;;) {
                const [from, to] = [IMPORT_STAGES.unpack[1], 100];
                Object.assign(job.status, {stage: status.stage || job.status.stage,
                                           stage_label: status.stage_label || job.status.stage_label,
                                           done: status.done || 0, total: status.total || 0,
                                           progress: Math.max(job.status.progress,
                                                              Math.round(from + (to - from) * (Number(status.progress) || 0) / 100))});
                if (status.status === "ready") {
                    end(job, "ready", {result: status.result});
                    return;
                }
                if (status.status === "failed" || status.status === "cancelled") {
                    await takeBack(job);
                    end(job, status.status, {error: status.error || null});
                    return;
                }
                await new Promise((resolve) => setTimeout(resolve, POLL_MS));
                try {
                    status = await call(`/projects/import/${encodeURIComponent(job.python)}`, undefined,
                                        "Plexora lost track of the import.");
                    misses = 0;
                } catch (error) {
                    misses += 1;
                    if (misses >= 5) throw error;
                }
            }
        }

        // -- the transport -----------------------------------------------------

        function transport(win) {
            const kit = win.PlexoraProjectExport && win.PlexoraProjectExport._internal;
            const server = kit && typeof kit.createServerTransport === "function" ? kit.createServerTransport() : {};
            let suggested = "project.plexora";
            const mine = (job) => typeof job === "string" && jobs.has(job);
            const view = (job) => ({...jobs.get(job).status, file: jobs.get(job).status.file
                ? {...jobs.get(job).status.file} : undefined});

            return {
                ...server,
                kind: "website",
                available: () => Boolean(python()),
                /** The Save dialog where the browser has one, opened before
                 *  anything else on the click that asked; else the file is
                 *  written into this site's storage and downloaded. */
                chooseDestination({name} = {}) {
                    suggested = /\.plexora$/i.test(String(name || "")) ? String(name) : `${name || "project"}.plexora`;
                    if (typeof win.showSaveFilePicker !== "function") return Promise.resolve({dest: null});
                    let picking;
                    try {
                        picking = win.showSaveFilePicker({
                            suggestedName: suggested, id: "plexora-export",
                            types: [{description: "Plexora project", accept: {"application/octet-stream": [".plexora"]}}],
                        });
                    } catch (error) {
                        return Promise.resolve({dest: null});
                    }
                    return picking.then((handle) => {
                        const dest = token("save");
                        saves.set(dest, handle);
                        return {dest};
                    }, (error) => {
                        if (error && error.name === "AbortError") return null;
                        // No gesture left, or a dialog the browser refused:
                        // this site's storage and a download instead.
                        return {dest: null};
                    });
                },
                startExport({ids, dataset, include, dest} = {}) {
                    const job = record("export", EXPORT_STAGES);
                    const save = dest ? saves.get(dest) || null : null;
                    if (dest) saves.delete(dest);
                    job.status.file = {name: save ? save.name : suggested, size: null, path: null, url: null};
                    void runExport(job, {ids, dataset, include, save, name: suggested});
                    return Promise.resolve(view(job.id));
                },
                status(kind, job) {
                    if (mine(job)) return Promise.resolve(view(job));
                    return server.status(kind, job);
                },
                cancel(kind, job) {
                    if (!mine(job)) return server.cancel(kind, job);
                    const held = jobs.get(job);
                    held.cancelled = true;
                    if (held.mover) held.mover.cancel();
                    if (held.python) {
                        return call(`/projects/import/${encodeURIComponent(held.python)}/cancel`, {},
                                    "That job could not be stopped.").then(() => ({cancelled: true, job}));
                    }
                    return Promise.resolve({cancelled: true, job});
                },
                async deliver(status) {
                    const held = status && jobs.get(status.job);
                    if (!held) return server.deliver(status);
                    if (held.saved) return {saved: true, name: held.saved.name};
                    if (!held.file) throw new Error("The finished file is not there.");
                    const link = win.document.createElement("a");
                    const href = win.URL.createObjectURL(held.file);
                    link.href = href;
                    link.download = held.status.file.name || "project.plexora";
                    link.style.display = "none";
                    win.document.body.appendChild(link);
                    link.click();
                    link.remove();
                    win.setTimeout(() => win.URL.revokeObjectURL(href), 60000);
                    return {downloaded: true};
                },
                canReveal: () => false,
                /** The folder is picked by the browser's own dialog, which
                 *  needs the click that asked: the dialog hands the click on
                 *  to `findMissing` instead of asking `pickFolder` first. */
                picksFolderItself: true,
                /** A picked file, read where it is: mounted for Python, which
                 *  previews it from its own manifest. */
                async preview(source, location, onSent) {
                    let path = source && source.path;
                    if (source && source.file) {
                        const id = token("bundle").replace(/[^a-z0-9]/gi, "");
                        [path] = await python().mount(`/picked/${id}`, [source.file]);
                        bundles.set(path, source.file);
                        // Nothing is sent: Python reads it where it is.
                        if (typeof onSent === "function") onSent(1);
                    }
                    if (!path) throw new Error("Choose a .plexora file.");
                    const answer = await call("/projects/import/preview", {path, location: location || null},
                                              "That file could not be read.");
                    return {...answer, path, upload: null};
                },
                startImport({source, choices, location} = {}) {
                    const path = source && source.path;
                    if (!path) return Promise.reject(new Error("Choose a .plexora file."));
                    const job = record("import", IMPORT_STAGES);
                    void runImport(job, {path, choices, location});
                    return Promise.resolve(view(job.id));
                },
                /** The folder is chosen in `findMissing`, by the browser's own
                 *  dialog (PlexoraPicks.find), on what is left of the click. */
                pickFolder: () => Promise.resolve("this browser"),
                async findMissing(name) {
                    const route = `/project/${encodeURIComponent(name)}/files_here`;
                    const missing = (await call(route, undefined, "That project's files could not be listed.")).missing || [];
                    if (!missing.length) return {found: 0, missing: []};
                    const picks = host.PlexoraPicks;
                    if (!picks || typeof picks.find !== "function") throw new Error("This browser cannot look for files.");
                    const found = await picks.find(missing);
                    const count = Object.keys(found || {}).length;
                    if (count) await call(route, {found}, "What was found could not be saved.");
                    return {found: count, missing: missing.filter((item) => !(item.recorded in (found || {})))};
                },
            };
        }

        return {
            transport,
            jobs: () => [...jobs.values()].map((job) => ({...job.status})),
        };
    }

    const host = hostOf(global);
    if (host === global) {
        // The host page (services/shell.js): the jobs live here.
        if (!global.PlexoraBundleWeb) global.PlexoraBundleWeb = createEngine(global);
        return;
    }
    // The app's frame (base.html): the dialogs' transport, the host's jobs --
    // or, on a host without them, jobs of this frame's own, which last as
    // long as its page.
    if (!host.PlexoraPython) return;
    const engine = host.PlexoraBundleWeb || global.PlexoraBundleWebLocal
        || (global.PlexoraBundleWebLocal = createEngine(host));
    global.PlexoraBundleTransport = engine.transport(global);
})(window);
