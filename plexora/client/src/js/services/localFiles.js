/**
 * PlexoraLocalFiles -- the user's own folders, in the explorer, on the website.
 *
 * On the desktop every Browse opens Plexora's explorer (services/explorer/),
 * which lists the disk through /list_dir and lets one click choose a file, a
 * folder or a folder-shaped dataset -- an OME-Zarr, a Xenium run, a DICOM
 * slide. A web page has no disk to list. What it can have, in Chrome and
 * Edge, is a folder the user gives it once ("Choose folder…", "Add
 * folder…"): a handle it may read, kept in IndexedDB like a pick's. This
 * module lists those folders, read-only, in the shape the explorer already
 * draws, by answering its own routes in the page:
 *
 *   POST /list_dir      one folder, as server/utils/dir_listing.py `listing`
 *   POST /stat_entries  sizes and dates the listing left out
 *   GET  /fs_places     the sidebar: the folders given, lapsed ones marked
 *   GET/POST /picker_prefs  last folder, Recent, pins, the view -- kept here
 *   POST /search_dir    not offered on the website yet: `unsupported`
 *
 * Paths are virtual: `/local` lists the folders given, `/local/<id>` is one
 * of them and `/local/<id>/a/b` is inside it. Every path the explorer goes to
 * came from an answer here, so it never builds one. What it answers with is
 * mounted where Python can open it (services/pickedFiles.js `mountHandle`) by
 * `take`, and that `/picked/...` path is what the field gets.
 *
 * A folder lapses when the browser stops granting it (Chrome asks again after
 * a restart): its listing answers 403 with `reconnect`, and the explorer's
 * Reconnect button asks the browser from the click.
 *
 * Datasets are marked here, by the names in each folder, by Python's own
 * rules (server/utils/dataset_marks.py `store_by_names`), so a listing never
 * waits behind Python's one thread. tests/test_website_pick.py holds the two
 * together, rule by rule. Only the names: the open folder's dataset is not
 * confirmed by reading its manifest, as the desktop's is; the importer still
 * reads it.
 *
 *   available()            whether this browser can give the site a folder
 *   answer(request, page)  a Response for one of the routes above, or null
 *   dialog({...})          the explorer's `systemDialog` on the website:
 *                          Choose file…, Choose folder…, Add folder…,
 *                          Reconnect, Remove, and a drop on its window
 *   take(chosen, {ask, gate})  what the explorer answered, mounted: a path
 *                          (or an array), or null for a no
 *   startFor(start)        where the explorer opens for a field's value
 *   folders()              the folders given: [{id, name, path, state}]
 *
 * Classic script, `window.PlexoraLocalFiles`, run in the site's host page
 * beside PlexoraPicks and borrowed by the app's frame (services/shell.js).
 */
(function (global) {
    "use strict";

    const DB = "plexora-folders";
    const FOLDERS = "folders";
    const PREFS = "prefs";
    const ROOT = "/local";
    const PICKED = "/picked/";

    // dir_listing.py's limits, so a folder given to the site lists as the
    // same folder would on the desktop.
    const LIST_DIR_LIMIT = 50000;
    const SCAN_HARD_CAP = 250000;
    const INLINE_STAT_LIMIT = 5000;
    const LISTING_BUDGET_MS = 2000;
    const CHILD_MARKER_LIMIT = 200;
    const CHILD_SCAN_CAP = 512;
    const STAT_ENTRIES_LIMIT = 500;
    //: browse_routes.py's.
    const RECENT_LIMIT = 8;
    const PINNED_LIMIT = 30;
    //: How many files are asked for their size and date at once.
    const STAT_BATCH = 32;

    // -- dataset_marks.py, by name ---------------------------------------------

    //: spatial_scene.XENIUM_FILES and XENIUM_TABLES.
    const XENIUM_NAMES = new Set([
        "morphology_focus", "morphology_focus.ome.tif", "morphology_mip.ome.tif",
        "morphology.ome.tif", "transcripts.parquet", "cell_boundaries.parquet",
        "nucleus_boundaries.parquet", "cells.parquet", "cell_feature_matrix.h5",
    ]);
    //: spatial_scene.SPATIALDATA_KINDS and SPATIALDATA_TABLES.
    const SPATIALDATA_GROUPS = ["images", "labels", "points", "shapes", "tables"];
    //: ome_zarr._ZARR_MARKERS.
    const ZARR_MARKERS = ["zarr.json", ".zgroup", ".zarray", ".zattrs"];
    const VISIUM_HD_BINNED = "binned_outputs";
    const VISIUM_HD_BIN = /^square_(\d{3})um$/;
    const VISIUM_SPATIAL = "spatial";
    //: spatialbridge's BUNDLE_LABELS.
    const LABELS = {
        zarr: "Zarr store", spatialdata: "SpatialData store", xenium: "Xenium run",
        visium: "Visium run", visium_hd: "Visium HD run", dicom: "DICOM slide",
    };
    //: dataset_marks.ENTITLEMENTS.
    const ENTITLEMENTS = {
        xenium: "transcriptomics:xenium",
        visium: "transcriptomics:visium",
        visium_hd: "transcriptomics:visium",
    };

    //: dicom_wsi._is_dicom_name.
    const isDicomName = (name) => /\.(dcm|dicom)$/i.test(name) || String(name).toUpperCase() === "DICOMDIR";

    function mark(kind, name = "") {
        const label = kind === "zarr" && String(name).toLowerCase().endsWith(".ome.zarr")
            ? "OME-Zarr image" : LABELS[kind];
        return { type: kind, label };
    }

    function storeByName(name) {
        return String(name).toLowerCase().endsWith(".zarr") ? mark("zarr", String(name)) : null;
    }

    /** `store_by_names`: a folder's mark from the names directly inside it. */
    function storeByNames(name, childNames) {
        const names = new Set(childNames || []);
        const text = String(name);
        const lowered = text.toLowerCase();
        const zarrNamed = lowered.endsWith(".zarr");
        const zarrMarked = ZARR_MARKERS.some((marker) => names.has(marker));
        const some = (test) => Array.from(names).some(test);
        if (names.has(VISIUM_HD_BINNED)
            || (lowered === VISIUM_HD_BINNED && some((n) => VISIUM_HD_BIN.test(n)))
            || (VISIUM_HD_BIN.test(text) && names.has(VISIUM_SPATIAL))) {
            return mark("visium_hd");
        }
        if (names.has("experiment.xenium") || Array.from(XENIUM_NAMES).filter((n) => names.has(n)).length >= 2) {
            return mark("xenium");
        }
        if (names.has(VISIUM_SPATIAL)
            && some((n) => n.endsWith("feature_bc_matrix") || n.endsWith("feature_bc_matrix.h5"))) {
            return mark("visium");
        }
        const groups = SPATIALDATA_GROUPS.filter((group) => names.has(group));
        if (groups.length && (zarrNamed || zarrMarked)) {
            // An OME-Zarr image files its own segmentations under `labels/`.
            if (groups.length === 1 && groups[0] === "labels") return mark("zarr", text);
            return mark("spatialdata");
        }
        if (some(isDicomName)) return mark("dicom");
        if (zarrNamed || zarrMarked) return mark("zarr", text);
        return null;
    }

    /**
     * The OPEN folder's dataset, `dataset_of` by its names alone: its own,
     * then DICOM instances one level down. `licensed` asked only for a type
     * that needs a licence.
     */
    function datasetOf(name, topNames, children, licensed) {
        let found = storeByNames(name, topNames);
        if (!found && children) {
            const below = Array.from(children.values()).some((names) => (names || []).some(isDicomName));
            if (below) found = mark("dicom");
        }
        if (!found) return null;
        const entitlement = ENTITLEMENTS[found.type];
        return { ...found, licensed: entitlement ? Boolean(licensed(entitlement)) : true };
    }

    // -- the folders given, and what is remembered ------------------------------

    function database() {
        return new Promise((resolve, reject) => {
            const request = global.indexedDB.open(DB, 1);
            request.onupgradeneeded = () => {
                request.result.createObjectStore(FOLDERS, { keyPath: "id" });
                request.result.createObjectStore(PREFS, { keyPath: "key" });
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function transact(store, mode, work) {
        const db = await database();
        try {
            return await new Promise((resolve, reject) => {
                const tx = db.transaction(store, mode);
                const result = work(tx.objectStore(store));
                tx.oncomplete = () => resolve(result && "result" in result ? result.result : result);
                tx.onerror = () => reject(tx.error);
            });
        } finally {
            db.close();
        }
    }

    //: The folder records, read once and kept: the host page outlives the
    //: app frame's pages.
    let records = null;

    async function loadFolders() {
        if (!records) {
            const stored = await transact(FOLDERS, "readonly", (store) => store.getAll()).catch(() => []);
            records = (stored || []).sort((a, b) => (a.added || 0) - (b.added || 0));
        }
        return records;
    }

    function newId() {
        const bytes = new Uint8Array(4);
        global.crypto.getRandomValues(bytes);
        return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    }

    async function permission(handle) {
        try {
            return await handle.queryPermission({ mode: "read" });
        } catch (error) {
            return "prompt";
        }
    }

    /**
     * A folder given to the site, kept. The same folder given twice is one;
     * one inside a folder already given is that folder's subfolder, and is
     * answered as its path there. -> its `/local/...` path.
     */
    async function addFolder(handle) {
        const known = await loadFolders();
        for (const record of known) {
            try {
                if (await record.handle.isSameEntry(handle)) return `${ROOT}/${record.id}`;
                if (await permission(record.handle) === "granted") {
                    const inside = await record.handle.resolve(handle);
                    if (inside && inside.length) return pathOf(record.id, inside);
                }
            } catch (error) { /* a folder gone from the disk: not this one */ }
        }
        const record = { id: newId(), name: handle.name, handle, added: Date.now() };
        await transact(FOLDERS, "readwrite", (store) => store.put(record));
        known.push(record);
        return `${ROOT}/${record.id}`;
    }

    /** A folder given, taken back: its listing, its pins and Recent with it. */
    async function forget(path) {
        const parsed = parse(path);
        if (!parsed || !parsed.id) return false;
        const known = await loadFolders();
        const at = known.findIndex((record) => record.id === parsed.id);
        if (at < 0) return false;
        await transact(FOLDERS, "readwrite", (store) => store.delete(parsed.id));
        known.splice(at, 1);
        const prefix = `${ROOT}/${parsed.id}`;
        const outside = (p) => p !== prefix && !String(p).startsWith(`${prefix}/`);
        const prefs = await readPrefs();
        await writePrefs({ ...prefs, last_dir: outside(prefs.last_dir) ? prefs.last_dir : "",
                           recent: prefs.recent.filter(outside), pinned: prefs.pinned.filter(outside) });
        for (const key of Array.from(cache.keys())) if (!outside(key)) cache.delete(key);
        return true;
    }

    async function reconnect(path) {
        const parsed = parse(path);
        const record = parsed && parsed.id ? (await loadFolders()).find((r) => r.id === parsed.id) : null;
        if (!record) return false;
        try {
            return await record.handle.requestPermission({ mode: "read" }) === "granted";
        } catch (error) {
            return false;
        }
    }

    function freshPrefs() {
        return { last_dir: "", recent: [], pinned: [], view: null };
    }

    async function readPrefs() {
        const stored = await transact(PREFS, "readonly", (store) => store.get("explorer")).catch(() => null);
        const strings = (value) => (Array.isArray(value) ? value.filter((v) => typeof v === "string") : []);
        if (!stored) return freshPrefs();
        return { last_dir: typeof stored.last_dir === "string" ? stored.last_dir : "",
                 recent: strings(stored.recent).slice(0, RECENT_LIMIT),
                 pinned: strings(stored.pinned).slice(0, PINNED_LIMIT),
                 view: stored.view && typeof stored.view === "object" ? stored.view : null };
    }

    function writePrefs(prefs) {
        return transact(PREFS, "readwrite", (store) => store.put({ key: "explorer", ...prefs }));
    }

    /** browse_routes.py `_update_picker_record`, over this browser's copy. */
    async function savePrefs(change) {
        const prefs = await readPrefs();
        const text = (field) => (typeof change[field] === "string" ? change[field].trim() : "");
        if (text("last_dir")) prefs.last_dir = text("last_dir");
        if (text("add_recent")) {
            const added = text("add_recent");
            prefs.recent = [added, ...prefs.recent.filter((p) => p !== added)].slice(0, RECENT_LIMIT);
        }
        if (text("pin") && !prefs.pinned.includes(text("pin"))) {
            prefs.pinned = [...prefs.pinned, text("pin")].slice(0, PINNED_LIMIT);
        }
        if (text("unpin")) prefs.pinned = prefs.pinned.filter((p) => p !== text("unpin"));
        if (Array.isArray(change.pinned_order)) {
            const ordered = Array.from(new Set(change.pinned_order.filter((p) => prefs.pinned.includes(p))));
            prefs.pinned = [...ordered, ...prefs.pinned.filter((p) => !ordered.includes(p))];
        }
        if (change.view && typeof change.view === "object") prefs.view = { ...(prefs.view || {}), ...change.view };
        await writePrefs(prefs);
        return prefs;
    }

    // -- paths ---------------------------------------------------------------------

    /** `/local` -> {id: ""}; `/local/<id>/a/b` -> {id, parts: ["a", "b"]}; else null. */
    function parse(path) {
        const text = String(path || "");
        if (text === ROOT || text === `${ROOT}/`) return { id: "", parts: [] };
        if (!text.startsWith(`${ROOT}/`)) return null;
        const [id, ...parts] = text.slice(ROOT.length + 1).split("/").filter(Boolean);
        return { id, parts };
    }

    function pathOf(id, parts = []) {
        return [`${ROOT}/${id}`, ...parts].join("/");
    }

    //: Folder handles by path, so going back up does not walk down again.
    const cache = new Map();

    class Lapsed extends Error {}
    class Gone extends Error {}

    /**
     * What a path names: {record, handle, parent, parts}. A name that is a
     * file comes back as the file, with the folder holding it as `parent`.
     */
    async function resolve(path) {
        const parsed = parse(path);
        if (!parsed || !parsed.id) throw new Gone("Not one of your folders.");
        const record = (await loadFolders()).find((r) => r.id === parsed.id);
        if (!record) throw new Gone("That folder is no longer in your folders.");
        if (await permission(record.handle) !== "granted") throw new Lapsed(record.name);
        let handle = record.handle;
        let parent = null;
        for (let i = 0; i < parsed.parts.length; i += 1) {
            const name = parsed.parts[i];
            const at = pathOf(parsed.id, parsed.parts.slice(0, i + 1));
            if (cache.has(at)) {
                parent = handle;
                handle = cache.get(at);
                continue;
            }
            try {
                const next = await handle.getDirectoryHandle(name);
                cache.set(at, next);
                parent = handle;
                handle = next;
            } catch (error) {
                if (error && error.name === "TypeMismatchError" && i === parsed.parts.length - 1) {
                    parent = handle;
                    handle = await handle.getFileHandle(name);
                    break;
                }
                if (error && error.name === "NotFoundError") throw new Gone(`Not there any more: ${name}`);
                throw error;
            }
        }
        return { record, handle, parent, parts: parsed.parts };
    }

    function crumbsFor(record, parts) {
        const trail = [{ label: "Folders", path: ROOT }, { label: record.name, path: pathOf(record.id) }];
        parts.forEach((part, i) => trail.push({ label: part, path: pathOf(record.id, parts.slice(0, i + 1)) }));
        return trail;
    }

    // -- one folder, as dir_listing.listing draws it -------------------------------

    const now = () => (global.performance && global.performance.now ? global.performance.now() : Date.now());

    //: Up to `cap` names directly inside a folder; [] when it cannot be read.
    async function childNames(handle, cap) {
        const found = [];
        try {
            for await (const name of handle.keys()) {
                found.push(name);
                if (found.length >= cap) break;
            }
        } catch (error) {
            return [];
        }
        return found;
    }

    async function statFiles(rows, children, deadline) {
        const files = rows.filter((row) => !row.is_dir);
        for (let i = 0; i < files.length; i += STAT_BATCH) {
            if (deadline !== null && now() >= deadline) return;
            await Promise.all(files.slice(i, i + STAT_BATCH).map(async (row) => {
                try {
                    const file = await children.get(row.name).getFile();
                    row.size = file.size;
                    row.mtime = Math.floor(file.lastModified / 1000);
                } catch (error) { /* one unreadable file is a row without a size */ }
            }));
        }
    }

    /**
     * One folder: {path, parent, crumbs, entries, truncated, total, partial,
     * dataset, platform}. A folder's own date is not something a handle
     * gives, so folders have none and only files make a listing `partial`.
     */
    async function listFolder(found, showHidden, licensed) {
        const { record, handle, parts } = found;
        const deadline = now() + LISTING_BUDGET_MS;
        const shown = [];
        const names = [];
        const children = new Map();
        let capped = false;
        for await (const [name, child] of handle.entries()) {
            names.push(name);
            if (showHidden || !name.startsWith(".")) {
                shown.push({ name, isDir: child.kind === "directory" });
                children.set(name, child);
            }
            if (names.length >= SCAN_HARD_CAP) {
                capped = true;
                break;
            }
        }
        shown.sort((a, b) => (a.isDir !== b.isDir ? (a.isDir ? -1 : 1)
            : a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0));
        const kept = shown.slice(0, LIST_DIR_LIMIT);
        // Inside each subfolder, for its mark, when there are few of them.
        const looked = new Map();
        if (shown.filter((entry) => entry.isDir).length <= CHILD_MARKER_LIMIT) {
            for (const entry of kept) {
                if (!entry.isDir) continue;
                if (now() >= deadline) break;
                looked.set(entry.name, await childNames(children.get(entry.name), CHILD_SCAN_CAP));
            }
        }
        const rows = kept.map((entry) => ({
            name: entry.name,
            is_dir: entry.isDir,
            size: null,
            path: pathOf(record.id, [...parts, entry.name]),
            mtime: null,
            kind: entry.isDir ? "dir" : "file",
            store: entry.isDir
                ? (looked.get(entry.name) && storeByNames(entry.name, looked.get(entry.name))) || storeByName(entry.name)
                : null,
        }));
        await statFiles(rows, children, rows.length <= INLINE_STAT_LIMIT ? deadline : now());
        const name = parts.length ? parts[parts.length - 1] : record.name;
        return {
            path: pathOf(record.id, parts),
            parent: parts.length ? pathOf(record.id, parts.slice(0, -1)) : ROOT,
            crumbs: crumbsFor(record, parts),
            entries: rows,
            truncated: capped || shown.length > LIST_DIR_LIMIT,
            total: shown.length,
            partial: rows.some((row) => !row.is_dir && row.mtime === null),
            dataset: datasetOf(name, names, looked.size ? looked : null, licensed),
            platform: "browser",
        };
    }

    /** `/local`: the folders given, a lapsed one listed (it opens on Reconnect). */
    async function listRoot() {
        const entries = [];
        for (const record of await loadFolders()) {
            const granted = await permission(record.handle) === "granted";
            const inside = granted ? await childNames(record.handle, CHILD_SCAN_CAP) : null;
            entries.push({ name: record.name, is_dir: true, size: null, path: pathOf(record.id), mtime: null,
                           kind: "dir", lapsed: !granted,
                           store: (inside && storeByNames(record.name, inside)) || storeByName(record.name) });
        }
        return { path: ROOT, parent: null, crumbs: [{ label: "Folders", path: ROOT }], entries,
                 truncated: false, total: entries.length, partial: false, dataset: null,
                 platform: "browser", place_root: true };
    }

    async function listDir(path, showHidden, licensed) {
        const parsed = parse(path);
        if (!parsed || !parsed.id) return listRoot();
        const found = await resolve(path);
        // A file's path opens the folder holding it, as on the desktop.
        if (found.handle.kind !== "directory") {
            return listFolder({ ...found, handle: found.parent, parts: found.parts.slice(0, -1) }, showHidden, licensed);
        }
        return listFolder(found, showHidden, licensed);
    }

    async function statEntries(path, names) {
        if (!Array.isArray(names)) throw new Error("names must be a list");
        if (names.length > STAT_ENTRIES_LIMIT) {
            throw new Error(`At most ${STAT_ENTRIES_LIMIT} names can be asked about at once.`);
        }
        const found = await resolve(path);
        const entries = {};
        for (const name of names) {
            if (typeof name !== "string" || !name || name === "." || name === ".." || name.includes("/")) {
                throw new Error(`Not a name in this folder: ${JSON.stringify(name)}`);
            }
            try {
                const file = await (await found.handle.getFileHandle(name)).getFile();
                entries[name] = { size: file.size, mtime: Math.floor(file.lastModified / 1000),
                                  kind: "file", is_dir: false };
            } catch (error) {
                if (error && error.name === "TypeMismatchError") {
                    entries[name] = { size: null, mtime: null, kind: "dir", is_dir: true };
                }
            }
        }
        return { path: pathOf(found.record.id, found.parts), entries };
    }

    /** The folders given, for the sidebar: [{id, name, path, state}]. */
    async function folders() {
        const out = [];
        for (const record of await loadFolders()) {
            out.push({ id: record.id, name: record.name, path: pathOf(record.id),
                       state: await permission(record.handle) });
        }
        return out;
    }

    // -- the routes ------------------------------------------------------------------

    function respond(status, body) {
        return new global.Response(JSON.stringify(body), {
            status, headers: { "Content-Type": "application/json" },
        });
    }

    const available = () => typeof global.showDirectoryPicker === "function";

    //: The routes answered here, and whether each lists a path of its own.
    const ROUTES = new Set(["/list_dir", "/stat_entries", "/fs_places", "/picker_prefs", "/search_dir"]);

    //: Whether a path is one this page lists: the folders given, a pick out
    //: of one, or "" (where the explorer opens with nothing to go on).
    const ours = (path) => !path || parse(path) !== null || String(path).startsWith(PICKED);

    /**
     * The explorer's request, answered from the folders given -- or null for
     * one that is not this page's to answer: a node's listing, a path in
     * Python's own files, a browser that cannot give the site a folder.
     * `page` is the window that asked (the app's frame), whose licence a
     * dataset's banner is about.
     */
    async function answer(request, page = null) {
        if (!available()) return null;
        const url = new URL(request.url);
        const base = String(global.PLEXORA_SITE_BASE || "");
        const route = base && url.pathname.startsWith(base) ? url.pathname.slice(base.length) : url.pathname;
        if (!ROUTES.has(route)) return null;
        const method = String(request.method || "GET").toUpperCase();
        let body = {};
        if (method === "POST") {
            try {
                body = JSON.parse(await request.clone().text() || "{}") || {};
            } catch (error) {
                return null;
            }
        }
        const node = method === "POST" ? body.node : url.searchParams.get("node");
        if (node) return null;
        const allows = page && page.PlexoraPro && page.PlexoraPro.allows;
        const licensed = (entitlement) => (allows ? allows(entitlement) : true);
        try {
            if (route === "/fs_places") {
                return respond(200, { platform: "browser", hostname: "", home: null, known: [], volumes: [],
                                      cloud: [], project: [], folders: await folders(), search: false });
            }
            if (route === "/picker_prefs") {
                const prefs = method === "POST" ? await savePrefs(body) : await readPrefs();
                return respond(200, { last_dir: prefs.last_dir, recent: prefs.recent, pinned: prefs.pinned,
                                      view: prefs.view || {}, drives: [] });
            }
            const path = String(body.path || "");
            if (!ours(path)) return null;
            const local = String(path).startsWith(PICKED) ? startFor(path) : path;
            if (route === "/search_dir") return respond(200, { unsupported: true, matches: [] });
            if (route === "/stat_entries") return respond(200, await statEntries(local, body.names));
            return respond(200, await listDir(local, Boolean(body.show_hidden), licensed));
        } catch (error) {
            if (error instanceof Lapsed) {
                const parsed = parse(String(body.path || ""));
                return respond(403, { error: `Plexora needs your permission to read “${error.message}” again.`,
                                      reconnect: parsed && parsed.id ? pathOf(parsed.id) : ROOT });
            }
            if (error instanceof Gone) return respond(404, { error: error.message });
            return respond(400, { error: (error && error.message) || "That folder could not be read." });
        }
    }

    // -- choosing ---------------------------------------------------------------------

    /**
     * Where the explorer opens for a field holding `start`: a folder given,
     * where a pick in the field came from, or nowhere in particular. The
     * folder a pick is mounted in (`/picked/<id>`: what a card's Browse is
     * handed, to open beside its image) is the folder the pick was chosen
     * in -- not the pick, which may be an OME-Zarr to be shown from outside.
     */
    function startFor(start) {
        const text = String(start || "");
        if (parse(text)) return text;
        if (!text.startsWith(PICKED) || !global.PlexoraPicks || !global.PlexoraPicks.originOf) return "";
        const from = global.PlexoraPicks.originOf(text) || "";
        const parsed = parse(from);
        if (!parsed || text.slice(PICKED.length).split("/").filter(Boolean).length > 1) return from;
        return parsed.parts.length ? pathOf(parsed.id, parsed.parts.slice(0, -1)) : ROOT;
    }

    /**
     * What the explorer answered, mounted (services/pickedFiles.js
     * `mountHandle`): a `/local/...` path, or several; a `/picked/...` one
     * (the browser's own dialog, a drop) is mounted already. `gate` once,
     * before the first mount. -> the same shape in `/picked/...` paths, or
     * null for a no.
     */
    async function take(chosen, { ask = null, gate = null } = {}) {
        const list = Array.isArray(chosen) ? chosen : [chosen];
        const out = [];
        let gated = false;
        for (const path of list) {
            if (!parse(path)) {
                out.push(path);
                continue;
            }
            let found;
            try {
                found = await resolve(path);
            } catch (error) {
                // Granted when it was listed, and not now (rare: a restart
                // between the two).
                if (error instanceof Lapsed) {
                    throw new Error(`Plexora needs your permission to read “${error.message}” again: `
                                    + "Browse, then Reconnect.");
                }
                throw error;
            }
            if (!gated && gate && !await gate()) return null;
            gated = true;
            const mounted = await global.PlexoraPicks.mountHandle(found.handle, {
                parent: found.parent, from: path, ask,
            });
            if (mounted === null) return null;
            out.push(mounted);
        }
        return Array.isArray(chosen) ? out : out[0];
    }

    /** Whether a folder is one dataset, by its names (and DICOM one down). */
    async function isDataset(handle) {
        const names = await childNames(handle, CHILD_SCAN_CAP);
        if (storeByNames(handle.name, names) || storeByName(handle.name)) return true;
        for (const name of names) {
            if (isDicomName(name)) return true;
        }
        return false;
    }

    /**
     * The explorer's `systemDialog` on the website (fileExplorer.js): the
     * browser's own two dialogs as buttons beside its title, and the rest
     * of what a page needs to keep folders -- answered in the page, so the
     * explorer stays open behind them. Each answer is null (nothing; the
     * explorer stays as it was), a path or paths (`/picked/...`: chosen),
     * `{show: path}` (a folder now given: go there) or `{error}`.
     *
     * "Choose folder…" never asks whether the folder IS the data or holds it:
     * a dataset (by its names) is the answer at once, for a field that takes
     * one; any other folder is kept in Folders and opened. A field that
     * takes only a folder takes whichever was chosen.
     */
    function dialog({ mode = "any", accept = null, many = false, ask = null, gate = null,
                      types = null, startIn = null } = {}) {
        const takesFiles = accept ? accept.files !== false && accept.files !== undefined : mode !== "directory";
        const takesFolders = accept ? Boolean(accept.directories || accept.datasets) : mode !== "file";
        const onlyFolders = !takesFiles && takesFolders;
        const picks = () => global.PlexoraPicks;
        const opening = startIn && startIn.kind ? { startIn } : {};

        async function chosenFolder(handle) {
            if (onlyFolders || (takesFolders && await isDataset(handle))) {
                return picks().mountHandle(handle, { ask, gate });
            }
            return { show: await addFolder(handle) };
        }

        async function folderFromDialog() {
            try {
                return await global.showDirectoryPicker({ id: "plexora", mode: "read", ...opening });
            } catch (error) {
                if (error && error.name === "AbortError") return null;
                throw error;
            }
        }

        const buttons = [];
        if (takesFiles) buttons.push({ kind: "file", label: "Choose file…", icon: "fas fa-file" });
        buttons.push({ kind: "directory", label: "Choose folder…", icon: "fas fa-folder-open" });

        return {
            app: "",
            inPage: true,
            buttons,
            available: async () => "kinds",
            async open(kind) {
                if (kind === "file") {
                    return picks().pick({ mode: "file", multiple: many, ask, gate, types, startIn });
                }
                const handle = await folderFromDialog();
                return handle ? chosenFolder(handle) : null;
            },
            async addFolder() {
                const handle = await folderFromDialog();
                return handle ? { show: await addFolder(handle) } : null;
            },
            reconnect,
            forget,
            /**
             * A drop on the explorer's window, read in the drop's own turn:
             * a file or a dataset is the answer, a folder is kept and
             * opened, as "Choose folder…" would.
             */
            drop(dataTransfer) {
                const items = Array.from((dataTransfer && dataTransfer.items) || [])
                    .filter((item) => item.kind === "file" && typeof item.getAsFileSystemHandle === "function");
                const asked = items.map((item) => {
                    try {
                        return Promise.resolve(item.getAsFileSystemHandle()).catch(() => null);
                    } catch (error) {
                        return Promise.resolve(null);
                    }
                });
                return (async () => {
                    const handles = (await Promise.all(asked)).filter(Boolean);
                    if (!handles.length) return null;
                    if (!many && handles.length > 1) return { error: "This takes one file or folder." };
                    if (handles.length === 1 && handles[0].kind === "directory") return chosenFolder(handles[0]);
                    if (onlyFolders) return { error: "This takes a folder, not a file." };
                    if (gate && !await gate()) return null;
                    const out = [];
                    for (const handle of handles) {
                        const path = await picks().mountHandle(handle, { ask });
                        if (path === null) return null;
                        out.push(path);
                    }
                    return many ? out : out[0];
                })();
            },
        };
    }

    global.PlexoraLocalFiles = Object.freeze({
        available, answer, dialog, take, startFor, folders,
        //: Python's rules, in the page (tests/test_website_pick.py).
        marks: Object.freeze({ storeByName, storeByNames, datasetOf }),
    });
})(window);
