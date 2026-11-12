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
 *   pick({mode, multiple, ask, gate, types, startIn})
 *                          -> a path, an array of them, or null (cancelled);
 *                             an MRXS or VSI slide brings the folder beside it,
 *                             chosen after `ask(text)` (the caller's one-button
 *                             question) and mounted with it (/picked/<id>/<name>.mrxs
 *                             and /picked/<id>/<name>/..., /picked/<id>/<name>.vsi
 *                             and /picked/<id>/_<name>_/...), a DICOM file the
 *                             folder it is in (the slide's other instances), and
 *                             an NDPIS the .ndpi files it names, from its folder;
 *                             `gate()`, when given, is
 *                             awaited once the files are chosen and before
 *                             anything is mounted or remembered: false is a
 *                             cancel that leaves nothing behind (the website's
 *                             first-use onboarding, views/webOnboarding.js);
 *                             `types` narrows the file dialog (showOpenFilePicker's
 *                             own shape) and `startIn` is a handle it opens beside
 *   take(dataTransfer, {gate, ask})
 *                          -> a promise of [path]: a drop, mounted where it lies
 *                             like a pick -- a folder with its tree, any size;
 *                             called in the drop event's own turn
 *   mountHandle(handle, {parent, from, ask, gate})
 *                          -> a handle the page holds (a row of a folder the user
 *                             gave the site, services/localFiles.js) mounted and
 *                             remembered like a pick; its path, or null
 *   startHandle(path)      -> the handle a dialog for a field holding `path` opens
 *                             beside, or null
 *   originOf(path)         -> where a mounted path was chosen from (a `/local/...`
 *                             path, services/localFiles.js), or ""
 *   restore()              -> {mounted: [id], waiting: [pick]} at boot
 *   reconnect({ask, ids})  -> mounts the waiting picks (those of `ids`, or all):
 *                             from a click with `ask` (the default), the browser
 *                             asked once -- its prompt lists every handle the
 *                             site held, and one refusal stops the asking; with
 *                             `ask: false`, only those it grants already (after
 *                             another of its prompts said yes)
 *   status(id)             -> {state, record}: why a pick is not open --
 *                             "mounted", "prompt" (the browser asks again),
 *                             "denied", "moved" (granted, but the file is not
 *                             where it was), "nohandle" (this browser keeps no
 *                             handles: Firefox, Safari), or "none" (no record)
 *   list()                 -> every remembered pick
 *   fileFor(path)          -> the File behind a mounted path, or null
 *   filesUnder(prefix)     -> [{path, file}] mounted under a folder (an MRXS's)
 *   handleFor(path)        -> its File System Access handle, where the browser gave one
 *   canWriteFolders()      -> whether this browser lets the page write into a
 *                             folder the user grants (Chromium's File System Access)
 *   find(missing)          -> from a click: a project's files found in a folder the
 *                             user chooses (or the files themselves, without a
 *                             folder picker), each by its name or, renamed since,
 *                             by its size, then checked as `sameFile`; mounted
 *                             like a pick: {recorded: path}, or null (cancelled)
 *   chooseFile(item)       -> from a click: one file the user chooses for `item`
 *                             ({name, size, mtime_ms, head, tail}), not mounted:
 *                             {file, handle, same}, or null (cancelled)
 *   keep(chosen, {ask})    -> a `chooseFile` answer mounted and remembered like a
 *                             pick (with an MRXS's folder and the like); its path
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
    //: record id -> where it was chosen (`from`): a pick out of a folder the
    //: user gave the site, which the next Browse of the same field opens at.
    const origins = new Map();

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
    function chooseWithInput({ directory = false, multiple = false, accept = "" } = {}) {
        return new Promise((resolve) => {
            const input = global.document.createElement("input");
            input.type = "file";
            input.multiple = multiple;
            if (directory) input.webkitdirectory = true;
            else if (accept) input.accept = accept;
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

    //: Slides whose pixels are in a folder beside the file picked, which is
    //: mounted with it under the name the scanner gave it: an MRXS's folder
    //: is named after the slide and holds its Slidedat.ini (OpenSlide's MIRAX
    //: layout); an Olympus VSI's is "_<name>_" and holds the .ets files of
    //: its scans (server/utils/formats/vsi.py).
    const SLIDES = [
        { suffix: /\.mrxs$/i, ext: ".mrxs", folder: (stem) => stem,
          holds: (path) => /^slidedat\.ini$/i.test(path), lacks: "has no Slidedat.ini" },
        { suffix: /\.vsi$/i, ext: ".vsi", folder: (stem) => `_${stem}_`,
          holds: (path) => /\.ets$/i.test(path), lacks: "has no .ets files" },
    ];
    const slideKind = (name) => SLIDES.find((kind) => kind.suffix.test(String(name))) || null;
    //: The name of the folder a slide's pixels are in.
    const slideFolderName = (kind, name) => kind.folder(String(name).replace(kind.suffix, ""));

    /**
     * The folder a slide's pixels are in, from the user: the folder named
     * as the scanner names it, or the one holding both (the folder of that
     * name is then taken from inside it). `ask(text)` is the caller's
     * one-button question (a click on it is the gesture the folder picker
     * needs); with none, the picker is opened straight away, which a browser
     * may refuse.
     * -> {name, entries: [{path inside the slide's folder, file, handle}], handle} or null.
     */
    async function slideFolder(file, fileHandle, ask) {
        const kind = slideKind(file.name);
        const folderName = slideFolderName(kind, file.name);
        if (ask && !await ask(`Choose the folder "${folderName}" that sits beside ${file.name}.`)) return null;
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
        const inside = slideEntries(kind, folderName, entries);
        if (!inside) {
            throw new Error(`${file.name}: that folder ${kind.lacks}; choose the folder "${folderName}" `
                            + `the scanner wrote beside the ${kind.ext}`);
        }
        return { name: folderName, entries: inside, handle };
    }

    //: A folder's files as "<folder>/<path>", as `<input webkitdirectory>` names them.
    async function walkNamed(handle) {
        return (await walk(handle)).map((entry) => ({ ...entry, path: `${handle.name}/${entry.path}` }));
    }

    /**
     * A slide's files out of a chosen folder's ("<chosen>/<path>" each): the
     * folder itself, or the folder named `folderName` inside it -- with paths
     * inside that folder; null when neither holds what `kind`'s folder holds.
     */
    function slideEntries(kind, folderName, entries) {
        const lower = folderName.toLowerCase();
        const split = (path) => path.split("/");
        const hasIndex = (list) => list.some((e) => kind.holds(e.path));
        const nested = entries.filter((e) => split(e.path).length > 2 && split(e.path)[1].toLowerCase() === lower)
            .map((e) => ({ ...e, path: split(e.path).slice(2).join("/") }));
        if (hasIndex(nested)) return nested;
        const itself = entries.map((e) => ({ ...e, path: split(e.path).slice(1).join("/") }));
        return hasIndex(itself) ? itself : null;
    }

    //: The slide's file at /picked/<id>/<name> and its folder at
    //: /picked/<id>/<folder name>/, where its reader looks for it.
    async function mountSlide(id, file, folder, python = global.PlexoraPython) {
        const blobs = [{ name: file.name, data: file },
                       ...folder.entries.map(({ path, file: f }) => ({ name: `${folder.name}/${path}`, data: f }))];
        await python.mountTree(`${ROOT}/${id}`, blobs);
        for (const { name, data } of blobs) mounted.set(`${ROOT}/${id}/${name}`, data);
        return `${ROOT}/${id}/${file.name}`;
    }

    //: A slide's folder read out of the folder the slide's file is in, where
    //: the page holds that folder's handle: no question. Null where it is not there.
    async function besideFolder(parent, file) {
        const kind = slideKind(file.name);
        const folderName = slideFolderName(kind, file.name);
        let handle;
        try {
            handle = await parent.getDirectoryHandle(folderName);
        } catch (error) {
            return null;
        }
        const entries = await walk(handle);
        return entries.some((e) => kind.holds(e.path)) ? { name: folderName, entries, handle } : null;
    }

    //: A Hamamatsu NDPIS is a short list of .ndpi files, one per channel,
    //: that sit beside it (server/utils/formats/ndpis.py): it is mounted with
    //: the folder it is in -- that list of files, not the rest of the folder.
    const NDPIS = /\.ndpis$/i;

    //: The .ndpi files an .ndpis names ("Image0=<name>.ndpi", "NoImages=3").
    async function ndpisMembers(file) {
        const named = new Map();
        let count = null;
        for (const line of (await file.text()).split(/\r?\n/)) {
            const at = line.indexOf("=");
            if (at < 0) continue;
            const key = line.slice(0, at).trim().toLowerCase();
            const value = line.slice(at + 1).trim();
            if (key === "noimages") count = Number.parseInt(value, 10);
            else if (/^image\d+$/.test(key)) named.set(Number(key.slice(5)), value.split(/[\\/]/).pop());
        }
        const names = [...named.keys()].sort((a, b) => a - b).map((key) => named.get(key));
        return Number.isFinite(count) ? names.slice(0, count) : names;
    }

    //: An NDPIS's files out of the folder it is in (a handle): itself and
    //: the .ndpi files it names, with their paths inside it; null when the
    //: folder lacks any of them.
    async function ndpisEntries(directory, setName) {
        let setHandle;
        try {
            setHandle = await directory.getFileHandle(setName);
        } catch (error) {
            return null;
        }
        const set = await setHandle.getFile();
        const entries = [{ path: setName, file: set, handle: setHandle }];
        for (const name of await ndpisMembers(set)) {
            try {
                const handle = await directory.getFileHandle(name);
                entries.push({ path: name, file: await handle.getFile(), handle });
            } catch (error) {
                return null;
            }
        }
        return entries;
    }

    /**
     * The folder an NDPIS is in, from the user, as for a DICOM file
     * (`dicomFolder`). -> {name, entries, handle} or null.
     */
    async function ndpisFolder(file, fileHandle, ask) {
        const members = await ndpisMembers(file);
        if (ask && !await ask(`${file.name} is a set of ${members.length} .ndpi files, one per channel. `
                              + `Choose the folder ${file.name} is in.`)) {
            return null;
        }
        let folder;
        if (global.showDirectoryPicker) {
            const options = { id: "plexora-slide", mode: "read" };
            if (fileHandle) options.startIn = fileHandle;
            const handle = await global.showDirectoryPicker(options);
            folder = { name: handle.name, entries: await ndpisEntries(handle, file.name), handle };
        } else {
            const files = await chooseWithInput({ directory: true });
            if (!files.length) return null;
            const inside = (f) => (f.webkitRelativePath || f.name).split("/").slice(1);
            const top = new Map(files.filter((f) => inside(f).length === 1).map((f) => [f.name, f]));
            top.set(file.name, file);
            const found = [file.name, ...members].map((name) => top.get(name));
            folder = {
                name: (files[0].webkitRelativePath || files[0].name).split("/")[0],
                entries: found.every(Boolean) ? found.map((f) => ({ path: f.name, file: f, handle: null })) : null,
                handle: null,
            };
        }
        if (!folder.entries) {
            throw new Error(`${file.name} names ${members.join(", ")}; that folder does not have them all. `
                            + `Choose the folder ${file.name} is in`);
        }
        return folder;
    }

    //: A DICOM slide is a folder of instances, one per level and more
    //: (dicom_wsi.py `_is_dicom_name`). On the desktop Python reads the rest
    //: from the folder a picked instance is in; here that folder has to be
    //: mounted with it.
    const DICOM = /\.(dcm|dicom)$/i;
    const isDicomName = (name) => DICOM.test(name) || String(name).toUpperCase() === "DICOMDIR";

    /**
     * A folder's DICOM instances, at its top and one level down (where an
     * export files them by series), with their paths inside it: what is
     * mounted of the folder a .dcm came from -- not the rest of it, which may
     * be somebody's whole Documents folder.
     */
    async function walkDicom(directory) {
        const found = [];
        for await (const [name, child] of directory.entries()) {
            if (child.kind === "directory") {
                for await (const [inner, grandchild] of child.entries()) {
                    if (grandchild.kind === "file" && isDicomName(inner)) {
                        found.push({ path: `${name}/${inner}`, file: await grandchild.getFile(), handle: grandchild });
                    }
                }
            } else if (isDicomName(name)) {
                found.push({ path: name, file: await child.getFile(), handle: child });
            }
        }
        return found;
    }

    /**
     * The folder a DICOM file picked alone is in, from the user, as for an
     * MRXS (`slideFolder`). -> {name, entries, handle} or null.
     */
    async function dicomFolder(file, fileHandle, ask) {
        if (ask && !await ask(`A DICOM slide is every file in its folder. Choose the folder ${file.name} is in.`)) {
            return null;
        }
        let folder;
        if (global.showDirectoryPicker) {
            const options = { id: "plexora-slide", mode: "read" };
            if (fileHandle) options.startIn = fileHandle;
            const handle = await global.showDirectoryPicker(options);
            folder = { name: handle.name, entries: await walkDicom(handle), handle };
        } else {
            const files = await chooseWithInput({ directory: true });
            if (!files.length) return null;
            const inside = (f) => (f.webkitRelativePath || f.name).split("/").slice(1);
            folder = {
                name: (files[0].webkitRelativePath || files[0].name).split("/")[0],
                entries: files.filter((f) => inside(f).length <= 2 && isDicomName(f.name))
                    .map((f) => ({ path: inside(f).join("/") || f.name, file: f, handle: null })),
                handle: null,
            };
        }
        if (!folder.entries.length) {
            throw new Error(`That folder has no DICOM files; choose the folder ${file.name} is in`);
        }
        return folder;
    }

    //: What a record keeps of each file, where the browser kept no handle.
    const facts = (file) => ({ name: file.name, size: file.size, modified: file.lastModified });

    /**
     * One file, mounted and remembered under `id`, with what it needs beside
     * it: an MRXS's or a VSI's folder, a DICOM file's neighbours, an NDPIS's
     * .ndpi files. `parent` is the folder it is in where the page holds it (a
     * folder the user gave the site), and then none is asked for. `from`:
     * where it was chosen, kept with the record. -> its path, or null when
     * the folder's question was cancelled.
     */
    async function mountOne(id, file, handle = null, { parent = null, ask = null, from = "" } = {}) {
        if (NDPIS.test(file.name)) {
            const set = parent
                ? { name: parent.name, entries: await ndpisEntries(parent, file.name), handle: parent }
                : await ndpisFolder(file, handle, ask);
            if (!set) return null;
            if (!set.entries) throw new Error(`${file.name}: the .ndpi files it names are not beside it`);
            const folderPath = await mountTree(id, set.name, set.entries);
            await remember({ id, kind: "directory", only: "ndpis", set: file.name, name: set.name,
                             handle: set.handle, from, picked: Date.now(),
                             files: set.entries.map(({ path, file: f }) => ({ path, size: f.size,
                                                                              modified: f.lastModified })) });
            origins.set(id, from);
            return `${folderPath}/${file.name}`;
        }
        if (slideKind(file.name)) {
            const folder = (parent && await besideFolder(parent, file)) || await slideFolder(file, handle, ask);
            if (!folder) return null;
            const path = await mountSlide(id, file, folder);
            await remember({ id, kind: "slide", handles: handle ? [handle] : null, folder: folder.handle,
                             name: folder.name, from, picked: Date.now(), count: folder.entries.length,
                             files: [facts(file)] });
            origins.set(id, from);
            return path;
        }
        if (DICOM.test(file.name)) {
            const series = parent
                ? { name: parent.name, entries: await walkDicom(parent), handle: parent }
                : await dicomFolder(file, handle, ask);
            if (!series) return null;
            const folderPath = await mountTree(id, series.name, series.entries);
            await remember({ id, kind: "directory", only: "dicom", name: series.name, handle: series.handle,
                             from, picked: Date.now(),
                             files: series.entries.map(({ path, file: f }) => ({ path, size: f.size,
                                                                                 modified: f.lastModified })) });
            origins.set(id, from);
            // The file chosen, inside its folder, where it is at its top: the
            // slide it is part of, as Python reads it from a desktop path.
            return series.entries.some((entry) => entry.path === file.name) ? `${folderPath}/${file.name}` : folderPath;
        }
        const [path] = await mountFiles(id, [file], handle ? [handle] : null);
        await remember({ id, kind: "files", handles: handle ? [handle] : null, from, picked: Date.now(),
                         files: [facts(file)] });
        origins.set(id, from);
        return path;
    }

    //: A file dialog's `types` as an input's `accept`: ".tif,.svs,...".
    function acceptOf(types) {
        if (!Array.isArray(types)) return "";
        return types.flatMap((type) => Object.values((type && type.accept) || {}).flat()).join(",");
    }

    /**
     * The files chosen (the browser's dialog first, so the click's activation
     * is still there for it), then `gate`, then the mount and the record. The
     * gate is where the website asks where its work is kept, on the first
     * pick only: before the mount, which waits for Python (a first visit's
     * pick would otherwise not come back until Python is up), and before the
     * record, which `restore` would mount again on every later visit. The
     * chosen files stay in this closure while it is open.
     *
     * `types` narrows the file dialog to the field's formats (the browser
     * still offers "All files"); `startIn` is a handle the dialog opens
     * beside -- a mask's, beside its image. Without one the browser opens
     * where this site's last dialog was.
     */
    async function pick({ mode = "file", multiple = false, ask = null, gate = null,
                          types = null, startIn = null } = {}) {
        const id = newId();
        const allowed = async () => !gate || Boolean(await gate());
        const opening = startIn && startIn.kind ? { startIn } : {};
        try {
            if (mode === "directory") {
                if (global.showDirectoryPicker) {
                    const handle = await global.showDirectoryPicker({ id: "plexora", mode: "read", ...opening });
                    if (!await allowed()) return null;
                    const path = await mountTree(id, handle.name, await walk(handle));
                    await remember({ id, kind: "directory", name: handle.name, handle, picked: Date.now() });
                    return path;
                }
                const files = await chooseWithInput({ directory: true });
                if (!files.length) return null;
                if (!await allowed()) return null;
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
                const options = { id: "plexora", multiple, ...opening };
                if (Array.isArray(types) && types.length) options.types = types;
                handles = await global.showOpenFilePicker(options);
                files = await Promise.all(handles.map((handle) => handle.getFile()));
            } else {
                files = await chooseWithInput({ multiple, accept: acceptOf(types) });
            }
            if (!files.length) return null;
            // Before an MRXS's or a DICOM file's folder is asked for too: its
            // question's click is the gesture that folder's dialog needs.
            if (!await allowed()) return null;
            if (files.length === 1) {
                const path = await mountOne(id, files[0], handles && handles[0], { ask });
                if (path === null) return null;
                return multiple ? [path] : path;
            }
            const paths = await mountFiles(id, files, handles);
            await remember({ id, kind: "files", handles, picked: Date.now(), files: files.map(facts) });
            return multiple ? paths : paths[0];
        } catch (error) {
            if (error && error.name === "AbortError") return null; // cancelled
            throw error;
        }
    }

    /**
     * A handle the page already holds -- a row of a folder the user gave the
     * site (services/localFiles.js) -- mounted and remembered like a pick:
     * a folder with its tree, a file with what it needs beside it, read from
     * `parent` (the folder it is in) without a question. `gate` as for
     * `pick`. -> its path, or null for a no.
     */
    async function mountHandle(handle, { parent = null, from = "", ask = null, gate = null } = {}) {
        try {
            if (gate && !await gate()) return null;
            const id = newId();
            if (handle.kind === "directory") {
                const path = await mountTree(id, handle.name, await walk(handle));
                await remember({ id, kind: "directory", name: handle.name, handle, from, picked: Date.now() });
                origins.set(id, from);
                return path;
            }
            return await mountOne(id, await handle.getFile(), handle, { parent, ask, from });
        } catch (error) {
            if (error && error.name === "AbortError") return null;
            throw error;
        }
    }

    //: A dropped folder's files where the browser gave an entry and no handle
    //: (Firefox, Safari): read in the batches `readEntries` hands out, until
    //: it hands out none.
    async function readEntries(directory, prefix = "") {
        const found = [];
        const reader = directory.createReader();
        for (;;) {
            const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
            if (!batch.length) return found;
            for (const entry of batch) {
                if (entry.isDirectory) {
                    found.push(...await readEntries(entry, `${prefix}${entry.name}/`));
                } else {
                    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
                    found.push({ path: `${prefix}${entry.name}`, file, handle: null });
                }
            }
        }
    }

    //: One dropped thing's files, whichever way the browser gave them.
    const treeOf = (one) => (one.handle ? walk(one.handle) : readEntries(one.entry));

    //: A File made in this page, of the same bytes (nothing is copied): a
    //: drop's comes from the app frame's page, which a navigation replaces.
    const own = (file) => (typeof global.File === "function"
        ? new global.File([file], file.name, { type: file.type, lastModified: file.lastModified }) : file);

    async function fileOf(one) {
        if (one.file && !one.entry && !one.handle) return one.file;
        // A drop's own File, where the drop gave one, read rather than its
        // handle's: Chrome reads a handle's file on the queue the site's
        // storage uses, and a file share's slow reads stalled that storage
        // (and Python's start in another tab) for seconds at a time; the
        // dropped File reads beside it. The handle is still what is remembered.
        if (one.file && one.handle) return own(one.file);
        if (one.handle) return one.handle.getFile();
        return new Promise((resolve, reject) => one.entry.file(resolve, reject));
    }

    //: A dropped item's handle, asked for now; null where the browser has none.
    function handleOf(item) {
        if (typeof item.getAsFileSystemHandle !== "function") return null;
        try {
            return Promise.resolve(item.getAsFileSystemHandle()).catch(() => null);
        } catch (error) {
            return null;
        }
    }

    /** One dropped file or folder, mounted and remembered. `partner`: an
     *  MRXS's folder, when it was dropped with it. */
    async function mountDropped(one, partner, ask) {
        const id = newId();
        if (one.directory) {
            const entries = await treeOf(one);
            const path = await mountTree(id, one.name, entries);
            await remember({ id, kind: "directory", name: one.name, handle: one.handle || null, picked: Date.now(),
                             ...(one.handle ? {} : { files: entries.map(({ path: p, file }) => ({
                                 path: p, size: file.size, modified: file.lastModified })) }) });
            return path;
        }
        const file = await fileOf(one);
        const kind = slideKind(file.name);
        if (partner && kind) {
            const entries = await treeOf(partner);
            if (entries.some((e) => kind.holds(e.path))) {
                // Named as the slide names it: its reader looks for the
                // folder by the slide file's own name.
                const path = await mountSlide(id, file, { name: slideFolderName(kind, file.name), entries });
                await remember({ id, kind: "slide", handles: one.handle ? [one.handle] : null,
                                 folder: partner.handle || null, name: partner.name, picked: Date.now(),
                                 count: entries.length, files: [facts(file)] });
                return path;
            }
        }
        return mountOne(id, file, one.handle || null, { ask });
    }

    /**
     * A drop, as picks: each file or folder dropped mounted where it lies,
     * whatever its size -- nothing is uploaded -- and remembered like a pick.
     * An MRXS or VSI dropped with its folder is one slide, and an NDPIS
     * dropped with its .ndpi files one set; alone, the folder is asked for,
     * as a DICOM file's is.
     *
     * Called in the drop event's own turn: a page may read a DataTransfer
     * only then, so every item's handle (Chromium: kept, like a pick's) and
     * entry (every browser) is asked for before anything is awaited. `gate`
     * as for `pick`, once the drop is read and before anything is mounted.
     * -> a promise of [path]; [] when nothing was taken.
     */
    function take(dataTransfer, { gate = null, ask = null } = {}) {
        const items = Array.from((dataTransfer && dataTransfer.items) || []).filter((item) => item.kind === "file");
        const dropped = items.map((item) => ({
            handle: handleOf(item),
            entry: typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null,
            file: typeof item.getAsFile === "function" ? item.getAsFile() : null,
        }));
        if (!items.length) {
            Array.from((dataTransfer && dataTransfer.files) || [])
                .forEach((file) => dropped.push({ handle: null, entry: null, file }));
        }
        return (async () => {
            const got = [];
            for (const one of dropped) {
                // A handle the browser refuses (a file in a folder it keeps
                // from sites, AppData) is no handle: the dropped File itself
                // still opens, only not remembered for the next visit.
                const handle = one.handle ? await Promise.resolve(one.handle).catch(() => null) : null;
                // The dropped File kept beside its handle: it is what is read
                // (`fileOf`); the handle is what finds the file next visit.
                if (handle) got.push({ name: handle.name, directory: handle.kind === "directory", handle,
                                       file: handle.kind === "directory" ? null : one.file });
                else if (one.entry) got.push({ name: one.entry.name, directory: one.entry.isDirectory, entry: one.entry });
                else if (one.file) got.push({ name: one.file.name, directory: false, file: one.file });
            }
            if (!got.length) return [];
            try {
                if (gate && !await gate()) return [];
                const paired = new Set();
                const pairs = new Map();
                for (const one of got) {
                    const kind = one.directory ? null : slideKind(one.name);
                    if (!kind) continue;
                    const folderName = slideFolderName(kind, one.name).toLowerCase();
                    const folder = got.find((o) => o.directory && o.name.toLowerCase() === folderName);
                    if (folder) { pairs.set(one, folder); paired.add(folder); }
                }
                const paths = [];
                // An NDPIS dropped with the .ndpi files it names: one set,
                // mounted side by side (alone, its folder is asked for).
                for (const one of got) {
                    if (one.directory || !NDPIS.test(one.name)) continue;
                    const set = await fileOf(one);
                    const names = (await ndpisMembers(set)).map((name) => name.toLowerCase());
                    const members = names.map((name) => got.find((o) => !o.directory && o.name.toLowerCase() === name));
                    if (!names.length || !members.every(Boolean)) continue;
                    const id = newId();
                    const files = [set, ...await Promise.all(members.map(fileOf))];
                    const kept = [one, ...members].map((o) => o.handle || null);
                    const [path] = await mountFiles(id, files, kept.every(Boolean) ? kept : null);
                    await remember({ id, kind: "files", handles: kept.every(Boolean) ? kept : null,
                                     picked: Date.now(), files: files.map(facts) });
                    paths.push(path);
                    [one, ...members].forEach((o) => paired.add(o));
                }
                for (const one of got) {
                    if (paired.has(one)) continue;
                    const path = await mountDropped(one, pairs.get(one) || null, ask);
                    if (path) paths.push(path);
                }
                return paths;
            } catch (error) {
                if (error && error.name === "AbortError") return [];
                throw error;
            }
        })();
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
     * (and its subfolders), each by name -- or by size, renamed since -- and
     * then by `sameFile`. The ones found are mounted like a pick and
     * remembered; {recorded: path here}, or null when the dialog was cancelled.
     */
    async function find(missing) {
        let entries;
        try {
            if (global.showDirectoryPicker) {
                entries = await walk(await global.showDirectoryPicker({ id: "plexora-find", mode: "read" }));
            } else {
                // No folder picker (Firefox, Safari): the files themselves
                // ("Choose files…"); a folder input would list, and ask to
                // upload, everything under the folder.
                const files = await chooseWithInput({ multiple: missing.length > 1 });
                if (!files.length) return null;
                entries = files.map((file) => ({ path: file.name, file, handle: null }));
            }
        } catch (error) {
            if (error && error.name === "AbortError") return null;
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
            // Renamed since it was recorded: a file of its size, which
            // `sameFile` then takes only by its date or its bytes.
            if (!hit && item.size != null) {
                for (const entry of entries) {
                    if (entry.file.name === item.name || entry.file.size !== item.size) continue;
                    if (await sameFile(entry.file, item)) { hit = entry; break; }
                }
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

    /**
     * From a click: one file the user chooses for `item` (a project file this
     * page has no path for), in the browser's dialog -- which opens where this
     * site's last one did -- or a file input. Not mounted yet: the caller
     * shows how it differs first, when it does. -> {file, handle, same}, or
     * null when cancelled.
     */
    async function chooseFile(item) {
        let file = null;
        let handle = null;
        try {
            if (global.showOpenFilePicker) {
                [handle] = await global.showOpenFilePicker({ id: "plexora", multiple: false });
                file = handle ? await handle.getFile() : null;
            } else {
                [file] = await chooseWithInput({ multiple: false });
            }
        } catch (error) {
            if (error && error.name === "AbortError") return null;
            throw error;
        }
        if (!file) return null;
        return { file, handle: handle || null, same: await sameFile(file, item || {}) };
    }

    //: A `chooseFile` answer mounted and remembered like a pick, with what it
    //: needs beside it (`ask`: the caller's one-button question, for an
    //: MRXS's folder and the like). -> its path, or null.
    async function keep(chosen, { ask = null } = {}) {
        try {
            return await mountOne(newId(), chosen.file, chosen.handle || null, { ask });
        } catch (error) {
            if (error && error.name === "AbortError") return null;
            throw error;
        }
    }

    //: Every handle a record holds: a folder's, a slide's file and folder,
    //: a few files'.
    const handlesOf = (record) => [record.handle, record.folder, ...(record.handles || [])].filter(Boolean);

    async function permissionOf(handle) {
        try {
            return await handle.queryPermission({ mode: "read" });
        } catch (error) {
            return "prompt";
        }
    }

    //: Why a record that is not mounted is not (`status`).
    async function stateOf(record) {
        const all = handlesOf(record);
        if (!all.length) return "nohandle";
        for (const handle of all) {
            const state = await permissionOf(handle);
            if (state !== "granted") return state === "denied" ? "denied" : "prompt";
        }
        // Every handle granted, and still not mounted: what it points at is
        // gone from where it was (getFile's NotFoundError).
        return "moved";
    }

    async function status(id) {
        await reconnecting;
        if ([...mounted.keys()].some((path) => idOf(path) === id)) return { state: "mounted", record: null };
        let record = waiting.find((one) => one.id === id) || null;
        if (!record) {
            try {
                record = (await list()).find((one) => one.id === id) || null;
            } catch (error) {
                record = null;
            }
        }
        if (!record) return { state: "none", record: null };
        return { state: await stateOf(record), record };
    }

    async function granted(handle, ask) {
        const options = { mode: "read" };
        if (await handle.queryPermission(options) === "granted") return true;
        return ask && await handle.requestPermission(options) === "granted";
    }

    async function mountRecord(record, ask, python = global.PlexoraPython) {
        if (record.from) origins.set(record.id, record.from);
        if (record.kind === "directory" && record.handle && record.only === "dicom") {
            // A DICOM file's folder: its instances, not the rest of it.
            if (!await granted(record.handle, ask)) return false;
            await mountTree(record.id, record.name, await walkDicom(record.handle), python);
            return true;
        }
        if (record.kind === "directory" && record.handle && record.only === "ndpis") {
            // An NDPIS's folder: the set and its .ndpi files, not the rest of it.
            if (!await granted(record.handle, ask)) return false;
            const entries = await ndpisEntries(record.handle, record.set);
            if (!entries) return false;
            await mountTree(record.id, record.name, entries, python);
            return true;
        }
        if (record.kind === "slide" && record.handles && record.folder) {
            if (!await granted(record.handles[0], ask) || !await granted(record.folder, ask)) return false;
            const file = await record.handles[0].getFile();
            // Records from before VSI slides are all MRXS.
            const kind = slideKind(file.name) || SLIDES[0];
            const entries = slideEntries(kind, record.name, await walkNamed(record.folder));
            if (!entries) return false;
            await mountSlide(record.id, file, { name: record.name, entries }, python);
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

    //: One reconnect at a time (the home page's Reconnect starts a quiet one
    //: that a project's loader may meet): each takes the waiting list whole,
    //: so a second running beside it would find it empty, and `status` would
    //: take a record it holds for one granted but gone.
    let reconnecting = Promise.resolve();

    function reconnect(options = {}) {
        const run = reconnecting.then(() => reconnectNow(options));
        reconnecting = run.catch(() => null);
        return run;
    }

    async function reconnectNow({ ask = true, ids = null } = {}) {
        const wanted = ids ? new Set(ids) : null;
        const done = [];
        const still = [];
        let asking = Boolean(ask);
        for (const record of waiting.splice(0)) {
            if (wanted && !wanted.has(record.id)) {
                still.push(record);
                continue;
            }
            try {
                if (await mountRecord(record, asking)) {
                    done.push(record.id);
                    continue;
                }
            } catch (error) { /* gone from where it was: still waiting */ }
            still.push(record);
            // The browser's prompt lists every handle the site held, so a no
            // (or a prompt dismissed) is the answer for the rest too: it is
            // not asked again, once a file.
            if (asking && handlesOf(record).length) {
                const state = await stateOf(record);
                if (state === "prompt" || state === "denied") asking = false;
            }
        }
        waiting.push(...still);
        return { mounted: done, waiting: waiting.slice() };
    }

    //: The id of the pick a mounted path belongs to: /picked/<id>/...
    const idOf = (path) => (String(path || "").startsWith(`${ROOT}/`) ? String(path).split("/")[2] || "" : "");

    /**
     * Where a browser's file dialog opens for a field holding `path`: beside
     * the file mounted there, or beside a file directly in the folder it
     * names (a card's Browse is handed the folder of its image, and a mask is
     * almost always next to its image). Never from inside a picked folder's
     * tree: a dialog that opens in an OME-Zarr's chunks helps nobody.
     */
    function startHandle(path) {
        const text = String(path || "");
        if (!idOf(text)) return null;
        if (handles.has(text)) return handles.get(text);
        const prefix = text.endsWith("/") ? text : `${text}/`;
        for (const [at, handle] of handles) {
            if (at.startsWith(prefix) && !at.slice(prefix.length).includes("/")) return handle;
        }
        return null;
    }

    global.PlexoraPicks = Object.freeze({ pick, take, mountHandle, startHandle, reopen, restore, reconnect, list,
                                          //: Where the pick behind a mounted path was chosen
                                          //: (services/localFiles.js), or "".
                                          originOf: (path) => origins.get(idOf(path)) || "",
                                          waiting: () => waiting.slice(),
                                          fileFor: (path) => mounted.get(path) || null,
                                          //: Every mounted file under the folder `prefix`
                                          //: ("/picked/<id>/<folder>/"): [{path, file}].
                                          filesUnder: (prefix) => [...mounted.entries()]
                                              .filter(([path]) => path.startsWith(prefix))
                                              .map(([path, file]) => ({ path, file })),
                                          handleFor: (path) => handles.get(path) || null,
                                          canWriteFolders: () => typeof global.showDirectoryPicker === "function",
                                          find, chooseFile, keep, status,
                                          //: The pick a mounted (or recorded) path belongs to.
                                          idOf,
                                          //: A file mounted by other means (a test, a drop).
                                          adopt: (path, file) => mounted.set(path, file) });
})(window);
