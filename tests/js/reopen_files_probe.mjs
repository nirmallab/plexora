/**
 * A project's files opened again on a later visit to the website
 * (services/reopenFiles.js), over the picks it keeps (services/pickedFiles.js).
 *
 * Both run here in one window, as the site's host page runs them, against a
 * DOM small enough to read (mini_dom.mjs), an IndexedDB holding the picks of
 * an "earlier visit", file handles whose permission behaves as Chrome's
 * (one prompt answers for every handle that shares it), and a stand-in for
 * Python's /project/<name>/files_here that says what is missing and records
 * what is found. What has to hold:
 *
 *   - **Ask only when the browser asks.** A handle it grants already opens
 *     with no dialog; one it asks about again needs one Allow access, and
 *     one refusal is not asked again for every file.
 *   - **No handle: describe, then check.** Each file is shown by what was
 *     recorded (name, size, date, what it is to the project, where the
 *     desktop app had it), found by the user, and taken only when it is the
 *     same one -- whatever its name -- unless they choose to use it anyway.
 *   - The X and Escape open the project without what is missing; names from
 *     the user's disk are text, never markup.
 *
 * Run directly:  node tests/js/reopen_files_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";
import { makeDocument } from "./mini_dom.mjs";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(REPO, path), "utf8");
const PICKS = read("plexora/client/src/js/services/pickedFiles.js");
const REOPEN = read("plexora/client/src/js/services/reopenFiles.js");

let checks = 0;
async function check(label, fn) {
    await fn();
    checks += 1;
    console.log("  ok  " + label);
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const settle = async () => { for (let i = 0; i < 20; i += 1) await tick(); };
const plain = (value) => JSON.parse(JSON.stringify(value));

function named(name) {
    const error = new Error(name);
    error.name = name;
    return error;
}

// -- files and handles ----------------------------------------------------------------

const DAY = 1730000000000;

function fileObject(name, size, lastModified = DAY) {
    return {
        name, size, lastModified,
        slice() { return { arrayBuffer: async () => new ArrayBuffer(0) }; },
    };
}

//: One permission, as the browser keeps it: every handle sharing it is
//: granted by one answer (Chrome's prompt lists every handle the site held).
function permission(state = "prompt", after = "granted") {
    return { state, after, asked: 0 };
}

function fileHandle(file, perm = permission("granted"), { gone = false } = {}) {
    return {
        kind: "file", name: file.name, perm, gone,
        async getFile() {
            if (this.gone) throw named("NotFoundError");
            return file;
        },
        async queryPermission() { return perm.state; },
        async requestPermission() {
            perm.asked += 1;
            perm.state = perm.after;
            return perm.state;
        },
    };
}

function dirHandle(name, files) {
    const children = files.map((file) => [file.name, fileHandle(file)]);
    return {
        kind: "directory", name,
        async *entries() { for (const pair of children) yield pair; },
        async queryPermission() { return "granted"; },
        async requestPermission() { return "granted"; },
    };
}

// -- IndexedDB, small enough to read ---------------------------------------------------

function fakeIndexedDB(picks = []) {
    const dbs = new Map();
    dbs.set("plexora-picks", new Map([["picks", { keyPath: "id", rows: new Map(picks.map((r) => [r.id, r])) }]]));
    return {
        dbs,
        rows: () => Array.from(dbs.get("plexora-picks").get("picks").rows.values()),
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

// -- Python's half --------------------------------------------------------------------

function respond(body) {
    return { ok: true, status: 200, json: async () => plain(body) };
}

/**
 * /project/<name>/files_here as server/utils/relink.py answers it: an item is
 * missing until a file is mounted where the website now has it (`here`); a
 * POST records a found path when the file there is the same one (its size and
 * date) or the user said `anyway`, and answers what is refused.
 */
function fakePython(given, title = "LSP cohort") {
    const items = given.map((item) => ({ ...item }));
    const files = new Map();
    const here = new Map(items.map((item) => [item.recorded, item.here || null]));
    const python = {
        posts: [],
        mounted: [],
        async mount(dir, list) {
            const paths = Array.from(list, (file) => `${dir}/${file.name}`);
            paths.forEach((path, i) => files.set(path, list[i]));
            python.mounted.push(...paths);
            return paths;
        },
        async mountTree(dir, blobs) {
            for (const blob of blobs) files.set(`${dir}/${blob.name}`, blob.data);
        },
        present: (recorded) => files.set(`/present${recorded}`, {}) && here.set(recorded, `/present${recorded}`),
        missing() {
            return items.filter((item) => !files.has(here.get(item.recorded)))
                .map((item) => ({ ...item, here: here.get(item.recorded) }));
        },
        async fetch(route, init = {}) {
            assert.equal(route, "/project/slide/files_here");
            if (init.method !== "POST") return respond({ missing: python.missing(), title });
            const body = JSON.parse(init.body);
            python.posts.push(body);
            let recorded = 0;
            for (const [original, path] of Object.entries(body.found)) {
                const item = items.find((one) => one.recorded === original);
                const file = files.get(path);
                if (!item || !file) continue;
                const same = file.size === item.size && Math.abs(file.lastModified - item.mtime_ms) <= 1;
                if (!same && !(body.anyway || []).includes(original)) continue;
                if (!same) Object.assign(item, { name: file.name, size: file.size, mtime_ms: file.lastModified });
                here.set(original, path);
                recorded += 1;
            }
            const missing = python.missing();
            const still = new Set(missing.map((item) => item.recorded));
            const refused = {};
            for (const [original, path] of Object.entries(body.found)) {
                const file = files.get(path);
                if (still.has(original) && file) refused[original] = { name: file.name, size: file.size, mtime_ms: file.lastModified };
            }
            return respond({ recorded, missing, refused });
        },
    };
    return python;
}

// -- the host page --------------------------------------------------------------------

/**
 * pickedFiles.js and reopenFiles.js in one window, the picks of an earlier
 * visit in its IndexedDB and restored as the site's start does. `chromium`:
 * whether the browser has the File System Access pickers.
 */
async function boot({ records = [], items = [], chromium = true, derived = null } = {}) {
    const document = makeDocument();
    const picked = { files: [], folders: [], inputs: [] };
    const create = document.createElement;
    // A file input: the next queued choice, or a cancel.
    document.createElement = (tag) => {
        const node = create(tag);
        if (String(tag).toLowerCase() === "input") {
            node.click = () => {
                const next = picked.inputs.shift();
                setTimeout(() => {
                    if (next) {
                        node.files = next;
                        node.dispatch("change");
                    } else {
                        node.dispatch("cancel");
                    }
                }, 0);
            };
        }
        return node;
    };
    const db = fakeIndexedDB(records);
    const python = fakePython(items);
    let counter = 0;
    const context = {
        console, Promise, Object, Array, Map, Set, JSON, Error, String, Number, Boolean, Math, Date,
        Uint8Array, ArrayBuffer, setTimeout, clearTimeout, document,
        crypto: { getRandomValues: (bytes) => { counter += 1; bytes.fill(counter % 250); return bytes; } },
        indexedDB: db,
        PlexoraPython: python,
    };
    if (chromium) {
        context.showOpenFilePicker = async () => {
            const next = picked.files.shift();
            if (!next) throw named("AbortError");
            return next;
        };
        context.showDirectoryPicker = async () => {
            const next = picked.folders.shift();
            if (!next) throw named("AbortError");
            return next;
        };
    }
    context.window = context;
    createContext(context);
    runInContext(PICKS, context);
    runInContext(REOPEN, context);
    const picks = context.PlexoraPicks;
    await picks.restore();
    const said = [];
    let revealed = 0;
    const t = {
        context, document, python, picks, picked, db, said,
        revealed: () => revealed,
        run: () => context.PlexoraReopen.run({ doc: document, project: "slide", python, picks, derived,
                                               reveal: () => { revealed += 1; }, status: (text) => said.push(text) }),
        dialog: () => document.body.querySelector("dialog.plx-reopen"),
        view: () => {
            const box = t.dialog();
            const body = box && box.querySelector(".plx-reopen-body");
            return body && !box.classList.contains("is-busy") ? body.dataset.view : null;
        },
        async until(name) {
            for (let i = 0; i < 400; i += 1) {
                if (t.view() === name) return;
                await tick();
            }
            throw new Error(`no ${name} view; it is ${t.view()}`);
        },
        text: (selector) => {
            const node = t.dialog().querySelector(selector);
            return node ? node.textContent : null;
        },
        all: (selector) => t.dialog().querySelectorAll(selector).map((node) => node.textContent),
        async press(key) {
            const button = t.dialog().querySelector(`[data-action="${key}"]`);
            assert.ok(button, `no ${key} on ${t.view()}`);
            button.click();
            await settle();
        },
    };
    return t;
}

const IMAGE = { recorded: "/picked/r1/slide.ome.tif", here: "/picked/r1/slide.ome.tif", name: "slide.ome.tif",
                size: 2048, mtime_ms: DAY, role: "image", made: false, kind: "file" };
const MASK = { recorded: "/picked/r2/slide_mask.tif", here: "/picked/r2/slide_mask.tif", name: "slide_mask.tif",
               size: 512, mtime_ms: DAY, role: "mask", made: false, kind: "file" };
const TABLE = { recorded: "/picked/r3/cells.csv", here: "/picked/r3/cells.csv", name: "cells.csv",
                size: 64, mtime_ms: DAY + 5, role: "table", made: false, kind: "file" };

const fileOf = (item, extra = {}) => fileObject(extra.name || item.name, extra.size ?? item.size,
                                                extra.lastModified ?? item.mtime_ms);
const recordOf = (id, item, handle) => ({ id, kind: "files", handles: handle ? [handle] : null, picked: DAY,
                                          files: [{ name: item.name, size: item.size, modified: item.mtime_ms }] });

// -- the picks ------------------------------------------------------------------------

await check("status says why a pick is not open: prompt, denied, moved, no handle, none", async () => {
    const records = [
        recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), permission("prompt"))),
        recordOf("r2", MASK, fileHandle(fileOf(MASK), permission("denied"))),
        recordOf("r3", TABLE, fileHandle(fileOf(TABLE), permission("granted"), { gone: true })),
        recordOf("r4", IMAGE, null),
        recordOf("r5", MASK, fileHandle(fileOf(MASK), permission("granted"))),
    ];
    const t = await boot({ records });
    const states = {};
    for (const id of ["r1", "r2", "r3", "r4", "r5", "zz"]) states[id] = (await t.picks.status(id)).state;
    assert.deepEqual(states, { r1: "prompt", r2: "denied", r3: "moved", r4: "nohandle", r5: "mounted", zz: "none" });
    assert.equal((await t.picks.status("r1")).record.picked, DAY);
    assert.equal(t.picks.idOf("/picked/r1/slide.ome.tif"), "r1");
});

await check("reconnect asks only for the ids given, and one refusal stops the asking", async () => {
    const no = permission("prompt", "denied");
    const yes = permission("prompt", "granted");
    const other = permission("prompt", "granted");
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), no)),
                                     recordOf("r2", MASK, fileHandle(fileOf(MASK), yes)),
                                     recordOf("r3", TABLE, fileHandle(fileOf(TABLE), other))] });
    const first = await t.picks.reconnect({ ids: ["r1", "r2"] });
    assert.deepEqual(plain(first.mounted), []);
    assert.equal(no.asked, 1);
    assert.equal(yes.asked, 0, "after a no, the next file is not asked about");
    assert.equal(other.asked, 0, "a pick not named is not asked about");
    const second = await t.picks.reconnect({ ids: ["r2"] });
    assert.deepEqual(plain(second.mounted), ["r2"]);
    assert.ok(t.python.mounted.includes("/picked/r2/slide_mask.tif"));
    assert.deepEqual(plain(t.picks.waiting().map((r) => r.id).sort()), ["r1", "r3"]);
});

await check("reconnect without asking mounts only what the browser grants already", async () => {
    const asks = permission("prompt");
    const granted = permission("prompt");
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), asks)),
                                     recordOf("r2", MASK, fileHandle(fileOf(MASK), granted))] });
    granted.state = "granted"; // another of the browser's prompts said yes
    const done = await t.picks.reconnect({ ask: false });
    assert.deepEqual(plain(done.mounted), ["r2"]);
    assert.equal(asks.asked + granted.asked, 0);
});

await check("two reconnects at once run one after the other, and status waits for them", async () => {
    const perm = permission("prompt");
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), perm))] });
    perm.state = "granted";
    const quiet = t.picks.reconnect({ ask: false });
    const again = t.picks.reconnect({ ask: false, ids: ["r1"] });
    const state = t.picks.status("r1");
    assert.deepEqual(plain((await quiet).mounted), ["r1"]);
    assert.deepEqual(plain((await again).mounted), []);
    assert.equal((await state).state, "mounted", "not taken for granted-but-gone while the first held it");
});

await check("find takes a renamed file by its size and date, never a different one", async () => {
    const t = await boot();
    t.picked.folders.push(dirHandle("Data", [
        fileObject("slide.ome.tif", 99, DAY), // its name, not its size
        fileObject("renamed.tif", 2048, DAY), // renamed since: its size and date
        fileObject("other.tif", 777, DAY + 9),
    ]));
    const lost = { recorded: "/picked/r9/gone.tif", name: "gone.tif", size: 777, mtime_ms: DAY };
    const hits = await t.picks.find([IMAGE, lost]);
    assert.deepEqual(Object.keys(hits), [IMAGE.recorded]);
    assert.match(hits[IMAGE.recorded], /^\/picked\/[0-9a-f]+\/renamed\.tif$/);
});

await check("find answers null when its dialog is cancelled", async () => {
    const t = await boot();
    assert.equal(await t.picks.find([IMAGE]), null);
    const firefox = await boot({ chromium: false });
    assert.equal(await firefox.picks.find([IMAGE, MASK]), null);
});

await check("chooseFile says whether the chosen file is the same one, and keep mounts it", async () => {
    const t = await boot();
    t.picked.files.push([fileHandle(fileOf(IMAGE, { name: "copy.tif" }))]);
    const same = await t.picks.chooseFile(IMAGE);
    assert.equal(same.same, true);
    const path = await t.picks.keep(same);
    assert.match(path, /^\/picked\/[0-9a-f]+\/copy\.tif$/);
    assert.ok(t.db.rows().some((row) => row.kind === "files" && row.handles && row.files[0].name === "copy.tif"));
    t.picked.files.push([fileHandle(fileOf(IMAGE, { size: 99 }))]);
    assert.equal((await t.picks.chooseFile(IMAGE)).same, false);
    assert.equal(await t.picks.chooseFile(IMAGE), null, "cancelled");
});

// -- the dialog -----------------------------------------------------------------------

await check("the browser grants the files already: they open with no dialog", async () => {
    const perm = permission("prompt");
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), perm))], items: [IMAGE] });
    perm.state = "granted"; // "Allow on every visit", or the home page's Reconnect
    assert.equal(await t.run(), true);
    assert.equal(t.dialog(), null);
    assert.equal(t.revealed(), 0);
    assert.equal(perm.asked, 0);
});

await check("it asks again: one Allow access, and every file the browser lists opens", async () => {
    const site = permission("prompt", "granted");
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), site)),
                                     recordOf("r2", MASK, fileHandle(fileOf(MASK), site))],
                           items: [IMAGE, MASK] });
    const running = t.run();
    await t.until("allow");
    assert.equal(t.revealed(), 1);
    assert.equal(t.text(".plx-reopen-title"), "Open your files from last time");
    assert.deepEqual(t.all(".plx-reopen-file-name"), ["slide.ome.tif", "slide_mask.tif"]);
    assert.match(t.all(".plx-reopen-file-meta")[0], /^Image · 2\.00 KB · modified /);
    assert.match(t.text(".plx-reopen-tip"), /Choose Allow on every visit and these open without asking next time\./);
    assert.ok(t.dialog().querySelector('[data-action="self"]'));
    await t.press("allow");
    await t.until("opened");
    assert.equal(t.text(".plx-reopen-title"), "Opening LSP cohort");
    assert.deepEqual(t.all(".plx-reopen-state"), ["Opened", "Opened"]);
    assert.equal(await running, true);
    assert.equal(site.asked, 1, "one prompt for both");
    assert.equal(t.dialog(), null);
    assert.deepEqual(t.python.missing(), []);
});

await check("Allow access refused: the files are found by the user instead, said why", async () => {
    const site = permission("prompt", "denied");
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), site)),
                                     recordOf("r2", MASK, fileHandle(fileOf(MASK), site))],
                           items: [IMAGE, MASK] });
    const running = t.run();
    await t.until("allow");
    await t.press("allow");
    await t.until("list");
    assert.equal(site.asked, 1, "a no is not asked again for the second file");
    assert.equal(t.text(".plx-reopen-title"), "Find the 2 files of this project");
    assert.match(t.text(".plx-reopen-lead"), /^Your browser has not allowed Plexora to open these files on this visit\. Choose the folder/);
    await t.press("skip");
    assert.equal(await running, false);
});

await check("Choose the files myself goes straight to finding them", async () => {
    const site = permission("prompt", "granted");
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), site))], items: [IMAGE] });
    const running = t.run();
    await t.until("allow");
    assert.equal(t.text('[data-action="self"]'), "Choose the file myself instead");
    await t.press("self");
    await t.until("locate");
    assert.equal(site.asked, 0);
    assert.equal(t.text(".plx-reopen-title"), "Plexora needs this file to open the project");
    await t.press("skip");
    assert.equal(await running, false);
});

await check("Firefox and Safari: one file described by what was recorded, found in the file dialog", async () => {
    const t = await boot({ chromium: false, records: [recordOf("r1", IMAGE, null)], items: [IMAGE] });
    const running = t.run();
    await t.until("locate");
    assert.equal(t.text(".plx-reopen-title"), "Find slide.ome.tif again");
    assert.match(t.text(".plx-reopen-lead"), /^This browser does not let a website keep access to a file between visits/);
    assert.equal(t.text(".plx-reopen-card-name"), "slide.ome.tif");
    assert.equal(t.text(".plx-reopen-card .plx-reopen-file-meta"), "Image in the project LSP cohort");
    const facts = t.all(".plx-reopen-fact");
    assert.equal(facts[0], "Size2.00 KB (2,048 bytes)");
    assert.match(facts[1], /^Last modified/);
    assert.equal(facts[2], "TypeOME-TIFF image");
    assert.match(facts[3], /^Chosen here/);
    assert.match(t.text(".plx-reopen-hint"), /checks its size and modified date, not its name/);
    assert.equal(t.text(".plx-reopen-foot"), "In Chrome or Edge, files you used here before reopen after one click.");
    t.picked.inputs.push([fileOf(IMAGE)]);
    await t.press("locate:0");
    await t.until("opened");
    assert.equal(await running, true);
    assert.equal(t.python.posts.length, 1);
    assert.deepEqual(t.python.posts[0].anyway, []);
    assert.match(t.python.posts[0].found[IMAGE.recorded], /^\/picked\/[0-9a-f]+\/slide\.ome\.tif$/);
});

await check("a renamed file is taken, a different one is shown beside the original", async () => {
    const moved = fileHandle(fileOf(IMAGE), permission("granted"), { gone: true });
    const t = await boot({ records: [recordOf("r1", IMAGE, moved)], items: [IMAGE] });
    const running = t.run();
    await t.until("locate");
    assert.equal(t.text(".plx-reopen-title"), "slide.ome.tif is not where it was");
    assert.ok(t.dialog().querySelector(".plx-reopen-icon.is-warn"));
    t.picked.files.push([fileHandle(fileOf(IMAGE, { size: 4096, lastModified: DAY + 86400000 }))]);
    await t.press("locate:0");
    await t.until("mismatch");
    assert.equal(t.text(".plx-reopen-title"), "That is a different file");
    assert.match(t.text(".plx-reopen-lead"), /^It has the same name, but not the size or the modified date of the image/);
    assert.equal(t.dialog().querySelectorAll(".plx-reopen-same").length, 1, "only the name is the same");
    assert.deepEqual(t.all(".plx-reopen-differs").slice(0, 1), ["4.00 KB"]);
    assert.equal(t.python.posts.length, 0, "a different file is not offered to Python unasked");
    t.picked.files.push([fileHandle(fileOf(IMAGE, { name: "slide renamed.tif" }))]);
    await t.press("another");
    await t.until("opened");
    assert.equal(await running, true);
    assert.match(t.python.posts[0].found[IMAGE.recorded], /\/slide renamed\.tif$/);
    assert.deepEqual(t.python.posts[0].anyway, []);
});

await check("Use this file anyway records it in the original's place", async () => {
    const t = await boot({ chromium: false, records: [recordOf("r1", IMAGE, null)], items: [IMAGE] });
    const running = t.run();
    await t.until("locate");
    t.picked.inputs.push([fileOf(IMAGE, { name: "export.tif", size: 3000 })]);
    await t.press("locate:0");
    await t.until("mismatch");
    assert.match(t.text(".plx-reopen-lead"), /^It is not the image this project was made with/);
    assert.equal(t.text('[data-action="anyway"]'), "Use this file anyway");
    await t.press("anyway");
    await t.until("opened");
    assert.equal(await running, true);
    assert.deepEqual(t.python.posts.map((post) => post.anyway), [[IMAGE.recorded]]);
    assert.deepEqual(t.python.missing(), []);
});

await check("several files: a folder search, then Locate for the one still missing", async () => {
    const items = [IMAGE, MASK, TABLE].map((item) => ({ ...item }));
    const t = await boot({ items });
    const running = t.run();
    await t.until("list");
    assert.equal(t.text(".plx-reopen-title"), "Find the 3 files of this project");
    assert.match(t.text(".plx-reopen-lead"), /chosen in another browser, or this browser’s data for the site was cleared/);
    assert.deepEqual(t.all(".plx-reopen-state"), ["Not found yet", "Not found yet", "Not found yet"]);
    assert.equal(t.text('[data-action="search"]'), "Choose folder…");
    t.picked.folders.push(dirHandle("Data", [fileOf(IMAGE), fileOf(MASK), fileObject("notes.txt", 5)]));
    await t.press("search");
    await t.until("list");
    assert.equal(t.text(".plx-reopen-title"), "1 file of this project is still missing");
    assert.deepEqual(t.all(".plx-reopen-state"), ["Found", "Found", "Not in that folder"]);
    assert.equal(t.text('[data-action="search"]'), "Search another folder…");
    assert.equal(t.text('[data-action="skip"]'), "Open without it");
    t.picked.files.push([fileHandle(fileOf(TABLE))]);
    await t.press("locate:2");
    await t.until("opened");
    assert.equal(await running, true);
    assert.equal(t.python.posts.length, 2);
});

await check("a desktop project's files show where the desktop app had them", async () => {
    const desk = (item, at) => ({ ...item, recorded: at, here: null });
    const items = [desk(IMAGE, "/Users/someone/Data/LSP/slide.ome.tif"), desk(MASK, "/Users/someone/Data/LSP/slide_mask.tif")];
    const t = await boot({ items });
    const running = t.run();
    await t.until("list");
    assert.match(t.text(".plx-reopen-lead"), /^This project was made in the Plexora desktop app, which recorded where its files were\./);
    assert.deepEqual(t.all(".plx-reopen-file-path"), ["Was at /Users/someone/Data/LSP/slide.ome.tif",
                                                       "Was at /Users/someone/Data/LSP/slide_mask.tif"]);
    await t.press("skip");
    assert.equal(await running, false);
    const one = await boot({ items: [desk(IMAGE, "/Users/someone/Data/LSP/slide.ome.tif")] });
    const single = one.run();
    await one.until("locate");
    assert.equal(one.text(".plx-reopen-title"), "Find slide.ome.tif");
    assert.equal(one.all(".plx-reopen-fact").at(-1), "Was at/Users/someone/Data/LSP/slide.ome.tif");
    await one.press("skip");
    assert.equal(await single, false);
});

await check("the X and Escape open the project without what is missing", async () => {
    const t = await boot({ records: [recordOf("r1", IMAGE, fileHandle(fileOf(IMAGE), permission("prompt")))],
                           items: [IMAGE] });
    const first = t.run();
    await t.until("allow");
    t.dialog().querySelector(".plx-reopen-close").click();
    assert.equal(await first, false);
    assert.equal(t.dialog(), null);
    const second = t.run();
    await t.until("allow");
    const escape = t.dialog().dispatch("cancel");
    assert.equal(escape.defaultPrevented, true);
    assert.equal(await second, false);
    assert.equal(t.dialog(), null);
    assert.equal(t.python.posts.length, 0);
});

await check("a pyramid's folder that waits is opened by the same Allow access", async () => {
    const pyramid = { recorded: "/derived/k1/slide.pyramid.ome.tiff", here: "/derived/k1/slide.pyramid.ome.tiff",
                      name: "slide.pyramid.ome.tiff", size: 10, mtime_ms: DAY, role: "image", made: true,
                      from_name: "slide.ome.tif", kind: "file", waiting: true, place: "Data" };
    const asked = [];
    let t = null;
    const derived = {
        async reconnect({ ask }) {
            asked.push(ask);
            if (ask) t.python.present(pyramid.recorded);
            return { mounted: ask ? [pyramid.recorded] : [] };
        },
    };
    t = await boot({ items: [pyramid], derived });
    const running = t.run();
    await t.until("allow");
    assert.deepEqual(asked, [false], "tried without a question first");
    assert.equal(t.all(".plx-reopen-file-meta")[0], "Pyramid Plexora made from slide.ome.tif · in the folder Data");
    assert.equal(t.dialog().querySelector('[data-action="self"]'), null, "a pyramid is not one to find by hand");
    await t.press("allow");
    await t.until("opened");
    assert.equal(await running, true);
    assert.deepEqual(asked, [false, true]);
});

await check("nothing is drawn when nothing is missing, and names are text, not markup", async () => {
    const none = await boot({ items: [] });
    assert.equal(await none.run(), false);
    assert.equal(none.dialog(), null);
    const evil = { ...IMAGE, name: '<img src=x onerror="alert(1)">.tif' };
    const t = await boot({ items: [evil] });
    const running = t.run();
    await t.until("locate");
    assert.equal(t.text(".plx-reopen-card-name"), evil.name);
    assert.equal(t.dialog().querySelectorAll("img").length, 0);
    for (const node of t.dialog().querySelectorAll("span, div, h2, p, dd")) {
        assert.ok(!String(node.innerHTML).includes("<img"), "no name set as markup");
    }
    await t.press("skip");
    assert.equal(await running, false);
});

await settle();
console.log(`${checks} checks passed`);
