/**
 * Choosing a file or a folder on the website: services/pickedFiles.js (a
 * pick, a drop, a handle mounted where Python can open it),
 * services/localFiles.js (the explorer over the folders the user gives the
 * site) and the website branch of services/browsePicker.js.
 *
 * Run against handles, pickers and an IndexedDB small enough to read. What
 * has to hold:
 *
 *   - **Every field can take a folder.** Firefox and Safari ask File or
 *     Folder under the button pressed; Chrome and Edge open the explorer,
 *     and what it answers is mounted.
 *   - **"Choose folder" never asks whether the folder is the data.** A
 *     dataset is the answer at once; any other folder is kept and opened.
 *   - **A drop is the thing itself**, read in the drop's own turn, mounted
 *     where it lies: a folder with its tree, an MRXS with its folder, a
 *     DICOM file with its slide's other instances.
 *   - **A folder the browser stopped granting says so**, and Reconnect asks
 *     from the click.
 *   - The listing is the desktop's shape, and its dataset marks are Python's
 *     (tests/test_website_pick.py compares both against Python itself).
 *
 * Run directly:  node tests/js/website_pick_probe.mjs
 *   --marks CASES.json   also print `MARKS <json>`: the page's marks for each case
 *   --tree DIR           also print `LISTING <json>`: the page's listing of DIR
 */

import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join, basename, relative, sep } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(REPO, path), "utf8");
const PICKS = read("plexora/client/src/js/services/pickedFiles.js");
const LOCAL = read("plexora/client/src/js/services/localFiles.js");
const BROWSE = read("plexora/client/src/js/services/browsePicker.js");
const TYPES = read("plexora/client/src/js/services/explorer/explorerTypes.js");

const args = process.argv.slice(2);
const opt = (name) => (args.includes(name) ? args[args.indexOf(name) + 1] : null);

let checks = 0;
async function check(label, fn) {
    await fn();
    checks += 1;
    console.log("  ok  " + label);
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
//: A value made in the page's realm, as this one's: assert's deep equality
//: compares prototypes, and the page's Array is not this Array.
const plain = (value) => JSON.parse(JSON.stringify(value));

// -- handles ----------------------------------------------------------------------

function named(name, message = name) {
    const error = new Error(message);
    error.name = name;
    return error;
}

//: One permission for a whole tree, as the browser grants a folder.
function grant(state = "granted", after = "granted") {
    return { state, after, asked: 0 };
}

/** A file handle over bytes held here. */
function memFile(name, size = 4, lastModified = 1700000000000, text = "") {
    const file = { name, size, lastModified, kind: "file-object", async text() { return text; } };
    return { kind: "file", name, file, async getFile() { return file; } };
}

//: An .ndpis naming its .ndpi files, as NanoZoomer writes it (CRLF lines).
const NDPIS_TEXT = "[NanoZoomer Digital Pathology Image Set]\r\nNoImages=2\r\n"
    + "Image0=set-DAPI (387) .ndpi\r\nImage1=set-FITC (485).ndpi\r\n";

/**
 * A folder handle over a tree: {name: size | subtree}. Every handle in it
 * shares `permission`.
 */
function memDir(name, tree, permission = grant(), parent = null) {
    const handle = {
        kind: "directory", name, permission, parentHandle: parent,
        children: new Map(),
        async *entries() { for (const pair of handle.children) yield pair; },
        async *keys() { for (const key of handle.children.keys()) yield key; },
        async getDirectoryHandle(child) {
            const found = handle.children.get(child);
            if (!found) throw named("NotFoundError");
            if (found.kind !== "directory") throw named("TypeMismatchError");
            return found;
        },
        async getFileHandle(child) {
            const found = handle.children.get(child);
            if (!found) throw named("NotFoundError");
            if (found.kind !== "file") throw named("TypeMismatchError");
            return found;
        },
        async queryPermission() { return permission.state; },
        async requestPermission() {
            permission.asked += 1;
            permission.state = permission.after;
            return permission.state;
        },
        async isSameEntry(other) { return other === handle; },
        async resolve(other) {
            const trail = [];
            for (let at = other; at; at = at.parentHandle) {
                if (at === handle) return trail.reverse();
                trail.push(at.name);
            }
            return null;
        },
    };
    for (const [child, value] of Object.entries(tree)) {
        handle.children.set(child, typeof value === "string" ? memFile(child, value.length, undefined, value)
            : typeof value === "number" ? memFile(child, value)
            : memDir(child, value, permission, handle));
    }
    return handle;
}

/** A folder handle over a folder on disk, for the listing compared with Python's. */
function diskDir(path, name = basename(path)) {
    const child = (entry) => {
        const at = join(path, entry);
        return statSync(at).isDirectory() ? diskDir(at, entry) : {
            kind: "file", name: entry,
            async getFile() {
                const facts = statSync(at);
                return { name: entry, size: facts.size, lastModified: facts.mtimeMs };
            },
        };
    };
    const handle = {
        kind: "directory", name,
        async *entries() { for (const entry of readdirSync(path)) yield [entry, child(entry)]; },
        async *keys() { for (const entry of readdirSync(path)) yield entry; },
        async getDirectoryHandle(entry) {
            const at = join(path, entry);
            if (!existsSync(at)) throw named("NotFoundError");
            if (!statSync(at).isDirectory()) throw named("TypeMismatchError");
            return diskDir(at, entry);
        },
        async getFileHandle(entry) {
            const at = join(path, entry);
            if (!existsSync(at)) throw named("NotFoundError");
            if (statSync(at).isDirectory()) throw named("TypeMismatchError");
            return child(entry);
        },
        async queryPermission() { return "granted"; },
        async requestPermission() { return "granted"; },
        async isSameEntry(other) { return other === handle; },
        async resolve() { return null; },
    };
    return handle;
}

// -- IndexedDB, small enough to read ------------------------------------------------

function fakeIndexedDB() {
    const dbs = new Map();
    return {
        dbs,
        rows: (db, store) => Array.from(((dbs.get(db) || new Map()).get(store) || { rows: new Map() }).rows.values()),
        open(name) {
            const request = {};
            setTimeout(() => {
                const fresh = !dbs.has(name);
                if (fresh) dbs.set(name, new Map());
                const stores = dbs.get(name);
                request.result = {
                    createObjectStore(store, { keyPath }) { stores.set(store, { keyPath, rows: new Map() }); },
                    transaction(store) {
                        const table = stores.get(store);
                        const tx = {
                            objectStore: () => ({
                                put: (value) => { table.rows.set(value[table.keyPath], value); return {}; },
                                get: (key) => ({ result: table.rows.get(key) }),
                                getAll: () => ({ result: Array.from(table.rows.values()) }),
                                delete: (key) => { table.rows.delete(key); return {}; },
                            }),
                        };
                        setTimeout(() => tx.oncomplete && tx.oncomplete(), 0);
                        return tx;
                    },
                    close() {},
                };
                if (fresh && request.onupgradeneeded) request.onupgradeneeded();
                request.onsuccess();
            }, 0);
            return request;
        },
    };
}

// -- the host page ----------------------------------------------------------------------

/**
 * pickedFiles.js and localFiles.js in one window, as the site's host page
 * runs them. `folderPicker`: whether the browser has showDirectoryPicker.
 */
function bootHost({ folderPicker = true, filePicker = true } = {}) {
    const mounted = [];
    let counter = 0;
    const picked = { folder: null, files: null, folderOptions: [], fileOptions: [] };
    const db = fakeIndexedDB();
    const context = {
        console, Promise, Object, Array, Map, Set, JSON, Error, String, Number, Boolean, Uint8Array,
        setTimeout, URL, Response, Request, performance,
        crypto: { getRandomValues: (bytes) => { counter += 1; bytes.fill(counter % 250); return bytes; } },
        indexedDB: db,
        PlexoraPython: {
            mount: async (dir, files) => {
                mounted.push({ dir, names: Array.from(files, (f) => f.name) });
                return files.map((f) => `${dir}/${f.name}`);
            },
            mountTree: async (dir, blobs) => { mounted.push({ dir, names: Array.from(blobs, (b) => b.name) }); },
        },
    };
    if (folderPicker) {
        context.showDirectoryPicker = async (options = {}) => {
            picked.folderOptions.push(options);
            if (!picked.folder) throw named("AbortError");
            return typeof picked.folder === "function" ? picked.folder() : picked.folder;
        };
    }
    if (filePicker) {
        context.showOpenFilePicker = async (options = {}) => {
            picked.fileOptions.push(options);
            if (!picked.files) throw named("AbortError");
            return picked.files;
        };
    }
    context.window = context;
    createContext(context);
    runInContext(PICKS, context);
    runInContext(LOCAL, context);
    return { context, db, picks: context.PlexoraPicks, local: context.PlexoraLocalFiles, mounted, picked };
}

/** One request to the page's answer: {status, body}, or null where it declines. */
async function route(local, path, body = null, page = null) {
    const init = body === null ? {} : { method: "POST", body: JSON.stringify(body),
                                         headers: { "Content-Type": "application/json" } };
    const response = await local.answer(new Request(`http://site${path}`, init), page);
    return response ? { status: response.status, body: await response.json() } : null;
}

const labTree = () => ({
    "CRC_cohort": {
        "LSP11385.ome.tif": 2048, "LSP11385_mask.ome.tif": 512, "LSP11385_cells.csv": 64,
        "LSP11386.ome.zarr": { ".zattrs": 1, "0": { ".zarray": 1 } },
    },
    "Xenium_breast_run1": { "experiment.xenium": 1, "transcripts.parquet": 9, "cells.parquet": 3 },
    "Prostate_WSI_dicom": { "DCM_0001.dcm": 10, "DCM_0002.dcm": 11, "notes.txt": 1,
                            "series2": { "DCM_0003.dcm": 12 } },
    "slides": { "slide.mrxs": 5, "slide": { "Slidedat.ini": 1, "Data0000.dat": 7 } },
    "archive": { "old.txt": 1 },
    "panel_channels.csv": 30,
    ".DS_Store": 1,
});

/** A folder given to the site through Add folder…, and its `/local/<id>` path. */
async function give(host, handle) {
    host.picked.folder = handle;
    const answer = await host.local.dialog({ mode: "any" }).addFolder();
    host.picked.folder = null;
    return answer.show;
}

// -- the page's marks and listings, for tests/test_website_pick.py -------------------------

if (opt("--marks")) {
    const host = bootHost();
    const cases = JSON.parse(readFileSync(opt("--marks"), "utf8"));
    const marks = host.local.marks;
    const out = cases.map(({ name, children, dicom_below }) => ({
        by_name: marks.storeByName(name),
        by_names: marks.storeByNames(name, children),
        dataset: marks.datasetOf(name, children, dicom_below ? new Map([["sub", ["x.dcm"]]]) : null, () => true),
    }));
    console.log(`MARKS ${JSON.stringify(out)}`);
}

if (opt("--tree")) {
    const host = bootHost();
    const tree = opt("--tree");
    const at = await give(host, diskDir(tree));
    const listing = await route(host.local, "/list_dir", { path: at, show_hidden: false });
    const hidden = await route(host.local, "/list_dir", { path: at, show_hidden: true });
    console.log(`LISTING ${JSON.stringify({ shown: listing.body, hidden: hidden.body })}`);
}

// -- the explorer's routes (services/localFiles.js) -------------------------------------

await check("a browser that cannot give the site a folder answers none of the explorer's routes", async () => {
    const host = bootHost({ folderPicker: false });
    assert.equal(host.local.available(), false);
    assert.equal(await route(host.local, "/list_dir", { path: "" }), null);
});

await check("a node's listing, and a path in Python's own files, are not the page's to answer", async () => {
    const host = bootHost();
    assert.equal(await route(host.local, "/list_dir", { path: "", node: "hpc" }), null);
    assert.equal(await route(host.local, "/list_dir", { path: "/data/projects" }), null);
    assert.equal(await route(host.local, "/fs_places?node=hpc"), null);
    assert.equal(await route(host.local, "/serve_config"), null);
});

await check("with no folder given, the explorer opens on an empty list of places", async () => {
    const host = bootHost();
    const { status, body } = await route(host.local, "/list_dir", { path: "" });
    assert.equal(status, 200);
    assert.equal(body.path, "/local");
    assert.equal(body.parent, null);
    assert.equal(body.place_root, true);
    assert.deepEqual(body.entries, []);
    assert.deepEqual(body.crumbs, [{ label: "Folders", path: "/local" }]);
});

await check("Add folder keeps the folder, and the list of places shows it by its name", async () => {
    const host = bootHost();
    const lab = memDir("Lab data", labTree());
    const at = await give(host, lab);
    assert.match(at, /^\/local\/[0-9a-f]{8}$/);
    const { body } = await route(host.local, "/list_dir", { path: "/local" });
    assert.deepEqual(body.entries.map((e) => [e.name, e.path, e.is_dir, e.lapsed]), [["Lab data", at, true, false]]);
    assert.equal(host.db.rows("plexora-folders", "folders").length, 1);
    assert.equal(host.picked.folderOptions[0].mode, "read");
});

await check("the same folder given twice is one, and one inside it opens there", async () => {
    const host = bootHost();
    const lab = memDir("Lab data", labTree());
    const at = await give(host, lab);
    assert.equal(await give(host, lab), at);
    const inside = await give(host, lab.children.get("CRC_cohort"));
    assert.equal(inside, `${at}/CRC_cohort`);
    assert.equal(host.db.rows("plexora-folders", "folders").length, 1);
});

await check("inside a folder: the desktop's rows, crumbs and parent, folders first", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const { body } = await route(host.local, "/list_dir", { path: at });
    assert.equal(body.path, at);
    assert.equal(body.parent, "/local");
    assert.deepEqual(body.crumbs.map((c) => c.label), ["Folders", "Lab data"]);
    assert.deepEqual(body.entries.map((e) => e.name),
                     ["archive", "CRC_cohort", "Prostate_WSI_dicom", "slides", "Xenium_breast_run1",
                      "panel_channels.csv"]);
    const csv = body.entries.find((e) => e.name === "panel_channels.csv");
    assert.deepEqual([csv.size, csv.mtime, csv.kind, csv.path], [30, 1700000000, "file", `${at}/panel_channels.csv`]);
    assert.equal(body.partial, false);
    assert.equal(body.platform, "browser");
});

await check("...and each folder that is one dataset says which, by the names inside it", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const { body } = await route(host.local, "/list_dir", { path: at });
    const store = (name) => body.entries.find((e) => e.name === name).store;
    assert.deepEqual(store("Xenium_breast_run1"), { type: "xenium", label: "Xenium run" });
    assert.deepEqual(store("Prostate_WSI_dicom"), { type: "dicom", label: "DICOM slide" });
    assert.equal(store("archive"), null);
    assert.equal(store("CRC_cohort"), null);
    const crc = await route(host.local, "/list_dir", { path: `${at}/CRC_cohort` });
    assert.deepEqual(crc.body.entries.find((e) => e.name === "LSP11386.ome.zarr").store,
                     { type: "zarr", label: "OME-Zarr image" });
});

await check("hidden files only when asked for", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const shown = await route(host.local, "/list_dir", { path: at });
    const hidden = await route(host.local, "/list_dir", { path: at, show_hidden: true });
    assert.ok(!shown.body.entries.some((e) => e.name === ".DS_Store"));
    assert.ok(hidden.body.entries.some((e) => e.name === ".DS_Store"));
});

await check("a file's path opens the folder holding it", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const { body } = await route(host.local, "/list_dir", { path: `${at}/CRC_cohort/LSP11385.ome.tif` });
    assert.equal(body.path, `${at}/CRC_cohort`);
});

await check("the open folder that is a dataset says so, with this licence's word on it", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const page = (yes) => ({ PlexoraPro: { allows: () => yes } });
    const run = `${at}/Xenium_breast_run1`;
    assert.deepEqual((await route(host.local, "/list_dir", { path: run }, page(true))).body.dataset,
                     { type: "xenium", label: "Xenium run", licensed: true });
    assert.equal((await route(host.local, "/list_dir", { path: run }, page(false))).body.dataset.licensed, false);
    const dicom = await route(host.local, "/list_dir", { path: `${at}/Prostate_WSI_dicom` });
    assert.deepEqual(dicom.body.dataset, { type: "dicom", label: "DICOM slide", licensed: true });
});

await check("a folder the browser stopped granting answers Reconnect, and opens once it is granted", async () => {
    const host = bootHost();
    const permission = grant("prompt", "granted");
    const at = await give(host, memDir("Lab data", labTree(), permission));
    const lapsed = await route(host.local, "/list_dir", { path: `${at}/CRC_cohort` });
    assert.equal(lapsed.status, 403);
    assert.equal(lapsed.body.reconnect, at);
    assert.match(lapsed.body.error, /“Lab data”/);
    const root = await route(host.local, "/list_dir", { path: "/local" });
    assert.equal(root.body.entries[0].lapsed, true);
    assert.equal(await host.local.dialog({}).reconnect(at), true);
    assert.equal(permission.asked, 1);
    assert.equal((await route(host.local, "/list_dir", { path: `${at}/CRC_cohort` })).status, 200);
});

await check("stat_entries: sizes and dates for names in the folder, and never a path", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const { body } = await route(host.local, "/stat_entries", { path: at, names: ["panel_channels.csv", "archive"] });
    assert.deepEqual(body.entries["panel_channels.csv"], { size: 30, mtime: 1700000000, kind: "file", is_dir: false });
    assert.deepEqual(body.entries.archive, { size: null, mtime: null, kind: "dir", is_dir: true });
    assert.equal((await route(host.local, "/stat_entries", { path: at, names: ["../x"] })).status, 400);
    assert.equal((await route(host.local, "/stat_entries", { path: at, names: [".."] })).status, 400);
});

await check("the sidebar's places: the folders given, a lapsed one said so, no home, no search", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    await give(host, memDir("Downloads", { "tonsil_HE.svs": 9 }, grant("prompt")));
    const { body } = await route(host.local, "/fs_places");
    assert.equal(body.home, null);
    assert.equal(body.search, false);
    assert.deepEqual(body.folders.map((f) => [f.name, f.state]), [["Lab data", "granted"], ["Downloads", "prompt"]]);
    assert.equal(body.folders[0].path, at);
});

await check("Recent: newest first, once each, eight at most; pins add, go and keep their order", async () => {
    const host = bootHost();
    for (let i = 0; i < 10; i += 1) await route(host.local, "/picker_prefs", { add_recent: `/local/a/${i}` });
    await route(host.local, "/picker_prefs", { add_recent: "/local/a/5", last_dir: "/local/a/5" });
    let prefs = (await route(host.local, "/picker_prefs")).body;
    assert.deepEqual(prefs.recent, ["/local/a/5", "/local/a/9", "/local/a/8", "/local/a/7",
                                    "/local/a/6", "/local/a/4", "/local/a/3", "/local/a/2"]);
    assert.equal(prefs.last_dir, "/local/a/5");
    for (const pin of ["/local/p1", "/local/p2", "/local/p3"]) await route(host.local, "/picker_prefs", { pin });
    await route(host.local, "/picker_prefs", { unpin: "/local/p2" });
    prefs = (await route(host.local, "/picker_prefs", { pinned_order: ["/local/p3", "/local/gone"] })).body;
    assert.deepEqual(prefs.pinned, ["/local/p3", "/local/p1"]);
    prefs = (await route(host.local, "/picker_prefs", { view: { layout: "grid" } })).body;
    assert.equal(prefs.view.layout, "grid");
});

await check("Remove from Folders takes its Recent, its pins and its last place with it", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    await route(host.local, "/picker_prefs", { add_recent: `${at}/CRC_cohort`, last_dir: at, pin: at });
    await route(host.local, "/picker_prefs", { add_recent: "/local/other" });
    assert.equal(await host.local.dialog({}).forget(at), true);
    const prefs = (await route(host.local, "/picker_prefs")).body;
    assert.deepEqual([prefs.recent, prefs.pinned, prefs.last_dir], [["/local/other"], [], ""]);
    assert.deepEqual((await route(host.local, "/list_dir", { path: "/local" })).body.entries, []);
    assert.equal((await route(host.local, "/list_dir", { path: at })).status, 404);
});

await check("searching below a folder is not offered on the website yet", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const { body } = await route(host.local, "/search_dir", { path: at, query: "tif" });
    assert.equal(body.unsupported, true);
});

// -- what the explorer answered, mounted --------------------------------------------------

await check("what the explorer answers is mounted: a folder with its tree, a file as itself", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const zarr = await host.local.take(`${at}/CRC_cohort/LSP11386.ome.zarr`);
    assert.match(zarr, /^\/picked\/[0-9a-f]{12}\/LSP11386\.ome\.zarr$/);
    assert.deepEqual(host.mounted.at(-1).names.sort(), ["LSP11386.ome.zarr/.zattrs", "LSP11386.ome.zarr/0/.zarray"]);
    const tif = await host.local.take(`${at}/CRC_cohort/LSP11385.ome.tif`);
    assert.match(tif, /^\/picked\/[0-9a-f]{12}\/LSP11385\.ome\.tif$/);
    assert.equal(await host.local.take("/picked/abc/x.tif"), "/picked/abc/x.tif");
    const both = await host.local.take([`${at}/panel_channels.csv`, "/picked/abc/x.tif"]);
    assert.equal(both.length, 2);
    const records = host.db.rows("plexora-picks", "picks");
    assert.ok(records.some((r) => r.kind === "directory" && r.from === `${at}/CRC_cohort/LSP11386.ome.zarr`));
});

await check("a DICOM file brings its slide's instances, not the rest of its folder", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const path = await host.local.take(`${at}/Prostate_WSI_dicom/DCM_0001.dcm`);
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/Prostate_WSI_dicom\/DCM_0001\.dcm$/);
    assert.deepEqual(host.mounted.at(-1).names.sort(),
                     ["Prostate_WSI_dicom/DCM_0001.dcm", "Prostate_WSI_dicom/DCM_0002.dcm",
                      "Prostate_WSI_dicom/series2/DCM_0003.dcm"]);
    const record = host.db.rows("plexora-picks", "picks").at(-1);
    assert.deepEqual([record.kind, record.only, record.name], ["directory", "dicom", "Prostate_WSI_dicom"]);
});

await check("an MRXS brings the folder beside it, with no question", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    let asked = 0;
    const path = await host.local.take(`${at}/slides/slide.mrxs`, { ask: async () => { asked += 1; return true; } });
    assert.match(path, /\/slide\.mrxs$/);
    assert.equal(asked, 0);
    assert.deepEqual(host.mounted.at(-1).names.sort(), ["slide.mrxs", "slide/Data0000.dat", "slide/Slidedat.ini"]);
});

await check("a VSI brings its _name_ folder beside it, with no question", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", {
        "scans": { "kidney.vsi": 5, "_kidney_": { "stack1": { "frame_t.ets": 9 }, "stack10001": { "frame_t.ets": 3 } },
                   "other.vsi": 5 } }));
    let asked = 0;
    const path = await host.local.take(`${at}/scans/kidney.vsi`, { ask: async () => { asked += 1; return true; } });
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/kidney\.vsi$/);
    assert.equal(asked, 0);
    assert.deepEqual(host.mounted.at(-1).names.sort(),
                     ["_kidney_/stack1/frame_t.ets", "_kidney_/stack10001/frame_t.ets", "kidney.vsi"]);
    const record = host.db.rows("plexora-picks", "picks").at(-1);
    assert.deepEqual([record.kind, record.name], ["slide", "_kidney_"]);
});

await check("an NDPIS brings the .ndpi files it names, not the rest of its folder", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", {
        "nano": { "set.ndpis": NDPIS_TEXT, "set-DAPI (387) .ndpi": 9, "set-FITC (485).ndpi": 9,
                  "unrelated.ndpi": 9, "notes.txt": 1 } }));
    const path = await host.local.take(`${at}/nano/set.ndpis`);
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/nano\/set\.ndpis$/);
    assert.deepEqual(host.mounted.at(-1).names.sort(),
                     ["nano/set-DAPI (387) .ndpi", "nano/set-FITC (485).ndpi", "nano/set.ndpis"]);
    const record = host.db.rows("plexora-picks", "picks").at(-1);
    assert.deepEqual([record.kind, record.only, record.set, record.name], ["directory", "ndpis", "set.ndpis", "nano"]);
});

await check("an NDPIS picked alone asks for its folder; one missing a channel's file is refused", async () => {
    const host = bootHost();
    const folder = memDir("nano", { "set.ndpis": NDPIS_TEXT, "set-DAPI (387) .ndpi": 9, "set-FITC (485).ndpi": 9 });
    const file = folder.children.get("set.ndpis");
    host.picked.files = [file];
    host.picked.folder = folder;
    const asked = [];
    const path = await host.picks.pick({ mode: "file", ask: async (text) => { asked.push(text); return true; } });
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/nano\/set\.ndpis$/);
    assert.equal(asked.length, 1);
    assert.match(asked[0], /set\.ndpis is a set of 2 \.ndpi files/);
    assert.deepEqual(host.mounted.at(-1).names.sort(),
                     ["nano/set-DAPI (387) .ndpi", "nano/set-FITC (485).ndpi", "nano/set.ndpis"]);
    const partial = memDir("nano", { "set.ndpis": NDPIS_TEXT, "set-DAPI (387) .ndpi": 9 });
    host.picked.files = [partial.children.get("set.ndpis")];
    host.picked.folder = partial;
    await assert.rejects(host.picks.pick({ mode: "file", ask: async () => true }), /does not have them all/);
});

await check("an NDPIS folder remembered is mounted again as its set, not the rest", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", {
        "nano": { "set.ndpis": NDPIS_TEXT, "set-DAPI (387) .ndpi": 9, "set-FITC (485).ndpi": 9, "other.ndpi": 4 } }));
    await host.local.take(`${at}/nano/set.ndpis`);
    host.mounted.length = 0;
    const { mounted } = await host.picks.restore();
    assert.ok(mounted.length >= 1);
    const again = host.mounted.find((m) => m.names.includes("nano/set.ndpis"));
    assert.ok(again, JSON.stringify(host.mounted));
    assert.deepEqual(again.names.sort(), ["nano/set-DAPI (387) .ndpi", "nano/set-FITC (485).ndpi", "nano/set.ndpis"]);
});

await check("the gate is asked once, before anything is mounted, and a no mounts nothing", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    let gated = 0;
    const no = async () => { gated += 1; return false; };
    assert.equal(await host.local.take([`${at}/panel_channels.csv`, `${at}/archive`], { gate: no }), null);
    assert.equal(gated, 1);
    assert.deepEqual(host.mounted, []);
});

await check("the next Browse of the field opens where its pick was chosen", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    const tif = await host.local.take(`${at}/CRC_cohort/LSP11385.ome.tif`);
    assert.equal(host.local.startFor(tif), `${at}/CRC_cohort/LSP11385.ome.tif`);
    // A card's Browse is handed the folder of its image: the folder the image
    // was chosen in -- and for an OME-Zarr too, not the inside of the store.
    assert.equal(host.local.startFor(tif.split("/").slice(0, 3).join("/")), `${at}/CRC_cohort`);
    const zarr = await host.local.take(`${at}/CRC_cohort/LSP11386.ome.zarr`);
    assert.equal(host.local.startFor(zarr.split("/").slice(0, 3).join("/")), `${at}/CRC_cohort`);
    assert.equal(host.local.startFor(zarr), `${at}/CRC_cohort/LSP11386.ome.zarr`);
    const whole = await host.local.take(at);
    assert.equal(host.local.startFor(whole.split("/").slice(0, 3).join("/")), "/local");
    assert.equal(host.local.startFor(at), at);
    assert.equal(host.local.startFor(""), "");
    assert.equal(host.local.startFor("s3://bucket/key"), "");
    const listed = await route(host.local, "/list_dir", { path: tif });
    assert.equal(listed.body.path, `${at}/CRC_cohort`);
});

// -- the explorer's dialogs on the website ---------------------------------------------------

await check("the title bar's buttons follow what the field takes", async () => {
    const host = bootHost();
    const kinds = (options) => host.local.dialog(options).buttons.map((b) => b.kind).join("|");
    assert.equal(kinds({ mode: "any" }), "file|directory");
    assert.equal(kinds({ mode: "file" }), "file|directory");
    assert.equal(kinds({ mode: "directory" }), "directory");
    assert.deepEqual(plain(host.local.dialog({ mode: "any" }).buttons.map((b) => b.label)), ["Choose file…", "Choose folder…"]);
    assert.equal(host.local.dialog({}).inPage, true);
});

await check("Choose folder: a dataset is the answer at once, any other folder is kept and opened", async () => {
    const host = bootHost();
    const lab = memDir("Lab data", labTree());
    host.picked.folder = lab.children.get("Xenium_breast_run1");
    const run = await host.local.dialog({ mode: "any" }).open("directory");
    assert.match(run, /^\/picked\/[0-9a-f]{12}\/Xenium_breast_run1$/);
    assert.equal(host.db.rows("plexora-folders", "folders").length, 0);
    host.picked.folder = lab;
    const kept = await host.local.dialog({ mode: "any" }).open("directory");
    assert.match(kept.show, /^\/local\/[0-9a-f]{8}$/);
    assert.equal(host.db.rows("plexora-folders", "folders").length, 1);
    host.picked.folder = null;
    assert.equal(await host.local.dialog({ mode: "any" }).open("directory"), null);
});

await check("...and a field that takes a folder takes whichever folder was chosen", async () => {
    const host = bootHost();
    host.picked.folder = memDir("Lab data", labTree());
    const path = await host.local.dialog({ mode: "directory" }).open("directory");
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/Lab data$/);
});

await check("Choose file is the browser's own dialog, narrowed to the field and opened beside its file", async () => {
    const host = bootHost();
    const beside = memFile("LSP11385.ome.tif");
    host.picked.files = [memFile("LSP11385_mask.ome.tif")];
    const types = [{ description: "Images", accept: { "application/octet-stream": [".tif"] } }];
    const path = await host.local.dialog({ mode: "any", types, startIn: beside }).open("file");
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/LSP11385_mask\.ome\.tif$/);
    const options = host.picked.fileOptions.at(-1);
    assert.deepEqual([options.types, options.startIn, options.id], [types, beside, "plexora"]);
});

/** A drop's DataTransfer, asking `log` which of its parts were read when. */
function dataTransfer(items, log = []) {
    return {
        types: ["Files"],
        files: items.map((it) => ({ name: it.name })),
        items: items.map((it) => ({
            kind: "file",
            getAsFileSystemHandle: it.handle === undefined ? undefined
                : () => { log.push(`handle:${it.name}`); return Promise.resolve(it.handle); },
            webkitGetAsEntry: () => { log.push(`entry:${it.name}`); return it.entry || null; },
            getAsFile: () => it.file || null,
        })),
    };
}

await check("a drop on the explorer: a file is the answer, a plain folder is kept and opened", async () => {
    const host = bootHost();
    const lab = memDir("Lab data", labTree());
    const file = await host.local.dialog({ mode: "any" }).drop(dataTransfer([{ name: "x.csv", handle: memFile("x.csv") }]));
    assert.match(file, /^\/picked\/[0-9a-f]{12}\/x\.csv$/);
    const kept = await host.local.dialog({ mode: "any" }).drop(dataTransfer([{ name: "Lab data", handle: lab }]));
    assert.match(kept.show, /^\/local\//);
    const two = await host.local.dialog({ mode: "any" }).drop(dataTransfer([
        { name: "a.csv", handle: memFile("a.csv") }, { name: "b.csv", handle: memFile("b.csv") }]));
    assert.deepEqual(plain(two), { error: "This takes one file or folder." });
});

// -- pickedFiles.js: drops, DICOM, dialogs -----------------------------------------------------

await check("a drop is read in its own turn: every handle and entry asked for before anything waits", async () => {
    const host = bootHost();
    const log = [];
    const taken = host.picks.take(dataTransfer([
        { name: "a.csv", handle: memFile("a.csv") }, { name: "run", handle: memDir("run", { "x.tif": 1 }) }], log), {});
    assert.deepEqual(log, ["handle:a.csv", "entry:a.csv", "handle:run", "entry:run"]);
    const paths = await taken;
    assert.equal(paths.length, 2);
    const records = host.db.rows("plexora-picks", "picks");
    assert.deepEqual(plain(records.map((r) => r.kind).sort()), ["directory", "files"]);
    assert.ok(records.find((r) => r.kind === "directory").handle);
});

/** A Firefox entry tree: {name: size | subtree}, read in batches of two. */
function entryOf(name, value) {
    if (typeof value === "number") {
        return { name, isFile: true, isDirectory: false,
                 file: (ok) => ok({ name, size: value, lastModified: 1 }) };
    }
    const children = Object.entries(value).map(([n, v]) => entryOf(n, v));
    return { name, isFile: false, isDirectory: true,
             createReader() {
                 let at = 0;
                 return { readEntries(ok) { const batch = children.slice(at, at + 2); at += 2; ok(batch); } };
             } };
}

await check("Firefox and Safari: a dropped folder is read through its entry, every batch of it", async () => {
    const host = bootHost({ folderPicker: false, filePicker: false });
    const run = { "experiment.xenium": 1, "transcripts.parquet": 2, "cells.parquet": 3,
                  "morphology_focus": { "a.ome.tif": 4, "b.ome.tif": 5, "c.ome.tif": 6 } };
    const [path] = await host.picks.take(dataTransfer([{ name: "run", entry: entryOf("run", run) }]), {});
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/run$/);
    assert.equal(host.mounted.at(-1).names.length, 6);
    const record = host.db.rows("plexora-picks", "picks").at(-1);
    assert.deepEqual([record.kind, record.handle, record.files.length], ["directory", null, 6]);
});

await check("an MRXS dropped with its folder is one slide", async () => {
    const host = bootHost();
    const slide = memDir("slide", { "Slidedat.ini": 1, "Data0000.dat": 2 });
    const paths = await host.picks.take(dataTransfer([
        { name: "slide.mrxs", handle: memFile("slide.mrxs") }, { name: "slide", handle: slide }]), {});
    assert.equal(paths.length, 1);
    assert.match(paths[0], /\/slide\.mrxs$/);
    assert.deepEqual(host.mounted.at(-1).names.sort(), ["slide.mrxs", "slide/Data0000.dat", "slide/Slidedat.ini"]);
});

await check("a VSI dropped with its _name_ folder is one slide", async () => {
    const host = bootHost();
    const folder = memDir("_kidney_", { "stack1": { "frame_t.ets": 9 } });
    const paths = await host.picks.take(dataTransfer([
        { name: "kidney.vsi", handle: memFile("kidney.vsi") }, { name: "_kidney_", handle: folder }]), {});
    assert.equal(paths.length, 1);
    assert.match(paths[0], /\/kidney\.vsi$/);
    assert.deepEqual(host.mounted.at(-1).names.sort(), ["_kidney_/stack1/frame_t.ets", "kidney.vsi"]);
});

await check("an NDPIS dropped with its .ndpi files is one set, mounted side by side", async () => {
    const host = bootHost();
    const paths = await host.picks.take(dataTransfer([
        { name: "set.ndpis", handle: memFile("set.ndpis", NDPIS_TEXT.length, undefined, NDPIS_TEXT) },
        { name: "set-DAPI (387) .ndpi", handle: memFile("set-DAPI (387) .ndpi") },
        { name: "set-FITC (485).ndpi", handle: memFile("set-FITC (485).ndpi") }]), {});
    assert.equal(paths.length, 1);
    assert.match(paths[0], /^\/picked\/[0-9a-f]{12}\/set\.ndpis$/);
    assert.deepEqual(host.mounted.at(-1).names.sort(), ["set-DAPI (387) .ndpi", "set-FITC (485).ndpi", "set.ndpis"]);
});

await check("a drop's gate is asked once the drop is read, and a no takes nothing", async () => {
    const host = bootHost();
    const paths = await host.picks.take(dataTransfer([{ name: "a.csv", handle: memFile("a.csv") }]),
                                        { gate: async () => false });
    assert.deepEqual(plain(paths), []);
    assert.deepEqual(host.mounted, []);
});

await check("a DICOM file picked alone asks for its folder, which opens beside it", async () => {
    const host = bootHost();
    const folder = memDir("Prostate", { "DCM_0001.dcm": 1, "DCM_0002.dcm": 2, "readme.txt": 3 });
    const file = folder.children.get("DCM_0001.dcm");
    host.picked.files = [file];
    host.picked.folder = folder;
    const asked = [];
    const path = await host.picks.pick({ mode: "file", ask: async (text) => { asked.push(text); return true; } });
    assert.match(path, /^\/picked\/[0-9a-f]{12}\/Prostate\/DCM_0001\.dcm$/);
    assert.equal(asked.length, 1);
    assert.match(asked[0], /DCM_0001\.dcm/);
    assert.deepEqual([host.picked.folderOptions[0].startIn, host.picked.folderOptions[0].id], [file, "plexora-slide"]);
    assert.deepEqual(host.mounted.at(-1).names.sort(), ["Prostate/DCM_0001.dcm", "Prostate/DCM_0002.dcm"]);
});

await check("...and a no to that question is a cancelled pick", async () => {
    const host = bootHost();
    host.picked.files = [memFile("DCM_0001.dcm")];
    assert.equal(await host.picks.pick({ mode: "file", ask: async () => false }), null);
    assert.deepEqual(host.mounted, []);
});

await check("the file dialog is narrowed to the field's formats, and opens beside a handle", async () => {
    const host = bootHost();
    host.picked.files = [memFile("cells.csv")];
    const types = [{ description: "Tables", accept: { "application/octet-stream": [".csv"] } }];
    const beside = memFile("image.tif");
    await host.picks.pick({ mode: "file", types, startIn: beside });
    assert.deepEqual(host.picked.fileOptions.at(-1).types, types);
    assert.equal(host.picked.fileOptions.at(-1).startIn, beside);
    host.picked.folder = memDir("run", { "a.tif": 1 });
    await host.picks.pick({ mode: "directory", startIn: beside });
    assert.equal(host.picked.folderOptions.at(-1).startIn, beside);
});

await check("startHandle: beside a picked file, never inside a picked folder's tree", async () => {
    const host = bootHost();
    const image = memFile("LSP11385.ome.tif");
    host.picked.files = [image];
    const path = await host.picks.pick({ mode: "file" });
    assert.equal(host.picks.startHandle(path), image);
    assert.equal(host.picks.startHandle(path.split("/").slice(0, 3).join("/")), image);
    host.picked.folder = memDir("store.zarr", { "0": { ".zarray": 1 } });
    const store = await host.picks.pick({ mode: "directory" });
    assert.equal(host.picks.startHandle(store.split("/").slice(0, 3).join("/")), null);
    assert.equal(host.picks.startHandle("s3://bucket/a.tif"), null);
});

await check("a DICOM folder remembered is mounted again as its instances, not the rest", async () => {
    const host = bootHost();
    const at = await give(host, memDir("Lab data", labTree()));
    await host.local.take(`${at}/Prostate_WSI_dicom/DCM_0002.dcm`);
    const before = host.mounted.length;
    const again = bootHost();
    again.db.dbs.set("plexora-picks", host.db.dbs.get("plexora-picks"));
    const { mounted } = await again.picks.restore();
    assert.equal(mounted.length, 1);
    assert.deepEqual(again.mounted.at(-1).names.sort(), host.mounted[before - 1].names.sort());
    assert.ok(again.picks.originOf(`/picked/${mounted[0]}/x`).endsWith("/DCM_0002.dcm"));
});

// -- browsePicker.js on the website ---------------------------------------------------------------

/** browsePicker.js in an app page of the website, with stand-ins for what it calls. */
function bootPage({ local = null, explorer = null, backend = "browser", desktop = null } = {}) {
    const calls = { pick: [], chooseKind: [], take: [], explorer: [] };
    const listeners = new Map();
    const document = {
        activeElement: null,
        addEventListener(type, fn) { listeners.set(type, fn); },
        createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, classList: { add() {} } }),
    };
    const context = {
        console, Promise, Object, Array, Map, Set, JSON, Error, String, Number, Boolean, setTimeout, Intl, Date,
        document,
        PlexoraBackend: { kind: backend },
        PlexoraDesktop: desktop,
        PlexoraPicks: {
            pick: async (options) => { calls.pick.push(options); return "/picked/aa/x"; },
            take: () => Promise.resolve([]),
            startHandle: (path) => (path ? { kind: "file", beside: path } : null),
        },
        PlexoraLocalFiles: local,
        PlexoraFileExplorer: explorer,
    };
    context.window = context;
    createContext(context);
    runInContext(TYPES, context);
    runInContext(BROWSE, context);
    // The File | Folder menu itself is drawn elsewhere (file_explorer tests).
    context.chooseKind = async (anchor, examples) => { calls.chooseKind.push({ anchor, examples }); return "directory"; };
    return { context, calls, listeners };
}

await check("Firefox and Safari: a field that takes either asks File or Folder, under the button pressed", async () => {
    const page = bootPage();
    const button = { tagName: "BUTTON" };
    let got = null;
    await page.context.browseForPath({ mode: "any", filter: "image", examples: "mask", anchorEl: button,
                                       onPicked: (path) => { got = path; } });
    assert.deepEqual(page.calls.chooseKind, [{ anchor: button, examples: "mask" }]);
    assert.equal(page.calls.pick[0].mode, "directory");
    assert.equal(got, "/picked/aa/x");
});

await check("...and a caller that names no button gets the one just pressed", async () => {
    const page = bootPage();
    const pressed = { tagName: "BUTTON" };
    page.context.document.activeElement = pressed;
    await page.context.browseForPath({ mode: "any", filter: "any", onPicked() {} });
    assert.equal(page.calls.chooseKind[0].anchor, pressed);
});

await check("the browser's file dialog gets the field's formats, one extension each, no stores", async () => {
    const page = bootPage();
    const [image] = page.context.pickerTypes("image");
    const suffixes = image.accept["application/octet-stream"];
    for (const suffix of [".tif", ".svs", ".dcm", ".mrxs", ".ndpi"]) assert.ok(suffixes.includes(suffix), suffix);
    assert.ok(!suffixes.includes(".zarr") && !suffixes.some((s) => s.split(".").length > 2));
    assert.equal(image.description, "Images");
    assert.equal(page.context.pickerTypes("any"), null);
    await page.context.browseForPath({ mode: "file", filter: "csv", start: "/picked/bb/img.tif", onPicked() {} });
    assert.deepEqual(plain(page.calls.pick[0].types[0].accept["application/octet-stream"]), [".csv"]);
    assert.deepEqual(page.calls.pick[0].startIn, { kind: "file", beside: "/picked/bb/img.tif" });
});

await check("Chrome and Edge open the explorer over the folders given, and mount what it answers", async () => {
    const asked = {};
    const local = {
        available: () => true,
        startFor: (start) => `start-for:${start}`,
        dialog: (options) => { asked.dialog = options; return { inPage: true, marker: "website" }; },
        take: async (chosen, options) => { asked.take = { chosen, options }; return "/picked/cc/run"; },
    };
    const explorer = { pick: async (options) => { asked.explorer = options; return "/local/ab12cd34/run"; } };
    const page = bootPage({ local, explorer });
    let got = null;
    await page.context.browseForPath({ mode: "any", filter: "image", start: "/picked/bb/img.tif",
                                       onPicked: (path) => { got = path; } });
    assert.equal(asked.explorer.systemDialog.marker, "website");
    assert.equal(asked.explorer.start, "start-for:/picked/bb/img.tif");
    assert.equal(asked.explorer.mode, "any");
    assert.equal(asked.dialog.mode, "any");
    assert.ok(Array.isArray(asked.dialog.types));
    assert.equal(asked.take.chosen, "/local/ab12cd34/run");
    assert.equal(typeof asked.take.options.ask, "function");
    assert.equal(got, "/picked/cc/run");
    assert.deepEqual(page.calls.chooseKind, []);
});

await check("a box says what it will not take from a drop, by its names, before reading it", async () => {
    const page = bootPage();
    const refuse = page.context.webRefusal;
    assert.equal(refuse([{ name: "run", directory: true }], { mode: "file", filter: "channels" }),
                 "This box takes a file, not a folder.");
    assert.equal(refuse([{ name: "a.csv", directory: false }], { mode: "directory" }),
                 "This box takes a folder, not a file.");
    assert.match(refuse([{ name: "cells.csv", directory: false }], { mode: "any", filter: "image" }), /takes an image/);
    assert.equal(refuse([{ name: "tonsil.ome.zarr", directory: true }], { mode: "any", filter: "image" }), "");
});

await check("a file let go anywhere but a box that takes it is refused, not opened in place of the app", async () => {
    const page = bootPage();
    let prevented = false;
    const event = { defaultPrevented: false, dataTransfer: { types: ["Files"], dropEffect: "copy" },
                    preventDefault() { prevented = true; } };
    page.listeners.get("dragover")(event);
    assert.equal(prevented, true);
    assert.equal(event.dataTransfer.dropEffect, "none");
    prevented = false;
    page.listeners.get("dragover")({ ...event, dataTransfer: { types: ["text/plain"] }, preventDefault() { prevented = true; } });
    assert.equal(prevented, false);
});

await check("...in the local app opened in a browser tab too, but not in the desktop app", async () => {
    const files = () => ({ defaultPrevented: false, dataTransfer: { types: ["Files"], dropEffect: "copy" },
                           prevented: false, preventDefault() { this.prevented = true; } });
    const local = bootPage({ backend: "server" });
    const over = files();
    local.listeners.get("dragover")(over);
    assert.equal(over.prevented, true);
    assert.equal(over.dataTransfer.dropEffect, "none");
    const drop = files();
    local.listeners.get("drop")(drop);
    assert.equal(drop.prevented, true);
    // A zone that took the drag is left to it.
    const taken = { ...files(), defaultPrevented: true };
    local.listeners.get("dragover")(taken);
    assert.equal(taken.dataTransfer.dropEffect, "copy");
    // The desktop app's drops are paths; its window never opens a file.
    const desktop = bootPage({ backend: "server", desktop: { platform: "darwin" } });
    assert.equal(desktop.listeners.has("dragover"), false);
    assert.equal(desktop.listeners.has("drop"), false);
});

await check("...and a file input, which the browser fills from a drop itself, is left alone", async () => {
    for (const backend of ["browser", "server"]) {
        const page = bootPage({ backend });
        const input = { tagName: "INPUT", type: "file" };
        let prevented = false;
        const event = { target: input, defaultPrevented: false,
                        dataTransfer: { types: ["Files"], dropEffect: "copy" },
                        preventDefault() { prevented = true; } };
        page.listeners.get("dragover")(event);
        page.listeners.get("drop")(event);
        assert.equal(prevented, false, backend);
        assert.equal(event.dataTransfer.dropEffect, "copy", backend);
        // Any other input is not one.
        const text = { ...event, target: { tagName: "INPUT", type: "text" } };
        page.listeners.get("dragover")(text);
        assert.equal(prevented, true, backend);
    }
});

await settle();
console.log(`${checks} checks passed`);
