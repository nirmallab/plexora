/**
 * Audit lead flow-website-3 (services/browserStore.js): work done on a visit
 * where the workspace folder was not opened is never lost on Reconnect, and
 * such a visit is not served copies older than the folder's.
 *
 * What went wrong before:
 *   1. `adoptWorkspace` copied the browser's projects (OPFS plexora/state)
 *      into the workspace and left them where they were.
 *   2. A later visit the browser did not grant the folder booted on that
 *      storage, so Python was served those pre-adoption copies.
 *   3. Reconnect merged it into the folder, and `merge` keeps the folder's
 *      copy of a project both hold: an edit made on that visit was dropped,
 *      and `reconnect` answered `{ok: true}`.
 *
 * What it does now (the user's choice, "keep both"):
 *   - after adoption (and after a Reconnect) the browser's copies of what the
 *     workspace holds are set aside to OPFS plexora/set-aside/<time>, so a
 *     visit that does not open the folder starts without them;
 *   - on Reconnect, a project both hold that this visit changed is copied by
 *     Python (`POST /projects/<name>/copy {name}`, server/models/bundles.py
 *     `copy_project`, which makes its rows, paths and id its own --
 *     tests/test_bundles.py `test_a_copy_under_a_name_of_its_own`) as
 *     "<name> (browser copy, <date>)", unique on both sides, merged in beside
 *     the folder's; `reconnect` returns the copies and leaves them in
 *     sessionStorage for the app frame's notice (views/webOnboarding.js
 *     `noticeCopies`).
 *
 * Run directly:  node tests/js/browser_store_reconnect_edits_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { webcrypto } from "node:crypto";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/services/browserStore.js"), "utf8");
const client = (rel) => readFileSync(join(REPO, "plexora/client/src/js", rel), "utf8");

let checks = 0;
let failed = 0;
async function check(label, fn) {
    try {
        await fn();
        checks += 1;
        console.log("  ok  " + label);
    } catch (error) {
        failed += 1;
        console.log("  FAIL " + label + "\n       " + String(error && error.message).split("\n").join("\n       "));
    }
}

// -- a File System Access tree (as tests/js/browser_store_probe.mjs) ---------

function domError(name) {
    const error = new Error(name);
    error.name = name;
    return error;
}

class FakeFile {
    constructor(name, data = "", lastModified = Date.now()) {
        this.kind = "file";
        this.name = name;
        this.data = data;
        this.lastModified = lastModified;
    }

    async getFile() {
        const data = this.data;
        return { name: this.name, size: data.length, lastModified: this.lastModified, async text() { return data; } };
    }

    async createWritable() {
        let buffer = "";
        const file = this;
        return {
            async write(chunk) {
                buffer += chunk && typeof chunk.text === "function" ? await chunk.text() : String(chunk);
            },
            async close() { file.data = buffer; file.lastModified = Date.now(); },
        };
    }

    async isSameEntry(other) { return other === this; }
}

class FakeDir {
    constructor(name) {
        this.kind = "directory";
        this.name = name;
        this.children = new Map();
        this.permission = "granted";
        this.answer = "granted";
    }

    async *entries() { for (const entry of [...this.children.entries()]) yield entry; }

    async *keys() { for (const name of [...this.children.keys()]) yield name; }

    async getDirectoryHandle(name, { create = false } = {}) {
        const found = this.children.get(name);
        if (found) {
            if (found.kind !== "directory") throw domError("TypeMismatchError");
            return found;
        }
        if (!create) throw domError("NotFoundError");
        const dir = new FakeDir(name);
        this.children.set(name, dir);
        return dir;
    }

    async getFileHandle(name, { create = false } = {}) {
        const found = this.children.get(name);
        if (found) {
            if (found.kind !== "file") throw domError("TypeMismatchError");
            return found;
        }
        if (!create) throw domError("NotFoundError");
        const file = new FakeFile(name, "");
        this.children.set(name, file);
        return file;
    }

    async removeEntry(name) {
        if (!this.children.delete(name)) throw domError("NotFoundError");
    }

    async queryPermission() { return this.permission; }

    async requestPermission() {
        this.permission = this.answer;
        return this.permission;
    }

    async isSameEntry(other) { return other === this; }

    file(path, data, lastModified = Date.now()) {
        const parts = path.split("/");
        const name = parts.pop();
        let at = this;
        for (const part of parts) {
            if (!at.children.has(part)) at.children.set(part, new FakeDir(part));
            at = at.children.get(part);
        }
        at.children.set(name, new FakeFile(name, data, lastModified));
        return this;
    }

    has(path) {
        let at = this;
        for (const part of path.split("/")) {
            at = at && at.children ? at.children.get(part) : null;
            if (!at) return false;
        }
        return true;
    }

    at(path) {
        let at = this;
        for (const part of path.split("/")) at = at.children.get(part);
        return at;
    }

    read(path) {
        return this.at(path).data;
    }

    json(path) {
        return JSON.parse(this.read(path));
    }
}

function fakeIndexedDB() {
    const data = new Map();
    return {
        data,
        open() {
            const request = {};
            setTimeout(() => {
                request.result = {
                    transaction() {
                        const tx = {};
                        tx.objectStore = () => ({
                            get: (key) => ({ result: data.get(key) }),
                            put: (value, key) => { data.set(key, value); return { result: key }; },
                        });
                        setTimeout(() => tx.oncomplete && tx.oncomplete(), 0);
                        return tx;
                    },
                    close() {},
                };
                if (request.onsuccess) request.onsuccess();
            }, 0);
            return request;
        },
    };
}

/**
 * Python's half, over the browser's storage it has open: "Make a copy" as
 * server/models/bundles.py runs it (here: the folder copied, its database
 * renamed, a new id), as a job polled at /projects/import/<job>.
 */
function fakePython(opfs, { fail = false } = {}) {
    const asked = [];
    const jobs = new Map();
    const reply = (status, body) => ({ ok: status < 300, status, json: async () => body });
    const python = {
        asked,
        moved: null,
        flush: async () => {},
        useStore: async (folder) => { python.moved = folder; return "folder"; },
        async fetch(url, init = {}) {
            asked.push([init.method || "GET", url, init.body ? JSON.parse(init.body) : null]);
            const copy = url.match(/^\/projects\/([^/]+)\/copy$/);
            if (copy && init.method === "POST") {
                if (fail) return reply(400, { error: "no room" });
                const name = decodeURIComponent(copy[1]);
                const wanted = JSON.parse(init.body).name;
                const state = opfs.at("plexora/state");
                const registry = JSON.parse(state.children.get("config.json").data);
                for (const [file, handle] of state.children.get(name).children) {
                    opfs.file(`plexora/state/${wanted}/${file === `${name}.db` ? `${wanted}.db` : file}`, handle.data);
                }
                registry[wanted] = { ...registry[name], id: `copy-of-${registry[name].id}` };
                opfs.file("plexora/state/config.json", JSON.stringify(registry));
                const job = `job${jobs.size + 1}`;
                jobs.set(job, { job, status: "ready", result: { projects: [{ target: wanted, action: "keep_both" }] } });
                return reply(202, { job, status: "pending" });
            }
            const polled = url.match(/^\/projects\/import\/(.+)$/);
            if (polled) return reply(200, jobs.get(decodeURIComponent(polled[1])));
            return reply(404, { error: "no route" });
        },
    };
    return python;
}

/** One visit of the page: the same browser (OPFS, IndexedDB, tab) each time. */
function visit(opfs, idb, python, session) {
    const context = {
        console, Promise, Object, Array, Map, Set, JSON, Error, String, Number, Boolean, Date, Math, Uint8Array,
        setTimeout, clearTimeout,
        indexedDB: idb,
        crypto: webcrypto,
        navigator: { storage: { getDirectory: async () => opfs, estimate: async () => ({ usage: 1, quota: 2 }) } },
        PlexoraPython: python,
        sessionStorage: session,
        location: { reload() {} },
    };
    context.window = context;
    createContext(context);
    runInContext(SOURCE, context);
    return context.PlexoraStore;
}

function fakeSession() {
    const data = new Map();
    return { data, getItem: (key) => (data.has(key) ? data.get(key) : null),
             setItem: (key, value) => data.set(key, String(value)), removeItem: (key) => data.delete(key) };
}

const later = () => new Promise((resolve) => setTimeout(resolve, 5));
const today = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
};

// -- the flow -------------------------------------------------------------------

const opfs = new FakeDir("");
const idb = fakeIndexedDB();
const session = fakeSession();
const home = new FakeDir("Plexora");
const STATE = "plexora/state";
const COPY = `tumour (browser copy, ${today()})`;

await check("visit 1: the browser's project is copied into the workspace on adoption", async () => {
    opfs.file(`${STATE}/config.json`, JSON.stringify({ tumour: { title: "Tumour", id: "t1" } }))
        .file(`${STATE}/tumour/gates.json`, "gates as first saved");
    const python = fakePython(opfs);
    const store = visit(opfs, idb, python, session);
    await store.prepare();
    await store.adoptWorkspace(home, { init: true });
    assert.equal(home.read("projects/tumour/gates.json"), "gates as first saved");
    assert.equal(python.moved, home.at("projects"));
});

await check("after adoption the browser's copy is set aside, not left to be served", async () => {
    assert.ok(!opfs.has(`${STATE}/tumour`));
    assert.ok(!("tumour" in opfs.json(`${STATE}/config.json`)));
    const [aside] = [...opfs.at("plexora/set-aside").children.values()];
    assert.equal(aside.children.get("tumour").children.get("gates.json").data, "gates as first saved");
    assert.equal(JSON.parse(aside.children.get("config.json").data).tumour.title, "Tumour");
});

await check("a visit that does not open the folder starts without the adopted copies", async () => {
    home.permission = "prompt";
    const store = visit(opfs, idb, fakePython(opfs), session);
    assert.equal(await store.prepare(), null); // null: the worker opens browserState()
    assert.equal(store.needsReconnect(), true);
    assert.deepEqual(Object.keys(opfs.json(`${STATE}/config.json`)), []);
    home.permission = "granted";
});

// A browser an older Plexora adopted from still holds its pre-adoption
// copies (written long before this visit): the case the lead found.
const LONG_AGO = Date.now() - 86400000;
let python2 = null;
let secondVisit = null;

await check("visit 2, folder not granted: an older version's left-over copies are what Python boots on", async () => {
    home.file("projects/tumour/gates.json", "gates refined in the folder");
    home.file("projects/config.json", JSON.stringify({ tumour: { title: "Tumour", id: "t1" },
                                                       lung: { title: "Lung", id: "l1" } }));
    home.file("projects/lung/gates.json", "lung in the folder");
    // A folder already named as the copy would be: the name must move on.
    home.file(`projects/${COPY}/note.txt`, "somebody's own");
    opfs.file(`${STATE}/config.json`, JSON.stringify({ tumour: { title: "Tumour", id: "t1" },
                                                      lung: { title: "Lung", id: "l1" } }), LONG_AGO)
        .file(`${STATE}/tumour/gates.json`, "gates as first saved", LONG_AGO)
        .file(`${STATE}/tumour/tumour.db`, "rows", LONG_AGO)
        .file(`${STATE}/lung/gates.json`, "lung as first saved", LONG_AGO);
    home.permission = "prompt";
    python2 = fakePython(opfs);
    secondVisit = visit(opfs, idb, python2, session);
    assert.equal(await secondVisit.prepare(), null);
    await later();
    // The user works on tumour; storeSync writes the edit back to OPFS. Lung is only looked at.
    opfs.file(`${STATE}/tumour/gates.json`, "gates edited while the folder was closed");
});

let done = null;
await check("Reconnect does not drop this visit's edit without a word", async () => {
    done = await secondVisit.reconnect();
    assert.equal(done.ok, true);
    const reached = Object.keys(home.json("projects/config.json"))
        .some((name) => home.has(`projects/${name}/gates.json`)
              && home.read(`projects/${name}/gates.json`) === "gates edited while the folder was closed");
    const told = JSON.stringify(done).includes("tumour");
    assert.ok(reached && told, JSON.stringify(done));
});

await check("both versions are kept: the folder's as it was, this visit's under a name of its own", async () => {
    const name = `${COPY} 2`;
    assert.deepEqual(JSON.parse(JSON.stringify(done.copies)), [{ from: "tumour", name }]);
    assert.equal(home.read("projects/tumour/gates.json"), "gates refined in the folder");
    assert.equal(home.read(`projects/${name}/gates.json`), "gates edited while the folder was closed");
    const registry = home.json("projects/config.json");
    assert.equal(registry[name].id, "copy-of-t1");
    assert.equal(registry.tumour.id, "t1");
    assert.equal(home.read(`projects/${COPY}/note.txt`), "somebody's own");
    assert.ok(!(COPY in registry));
});

await check("the copy is made by Python (its rows, paths and id its own), not by copying files", async () => {
    const posts = python2.asked.filter(([method]) => method === "POST");
    assert.deepEqual(JSON.parse(JSON.stringify(posts)),
                     [["POST", "/projects/tumour/copy", { name: `${COPY} 2` }]]);
    assert.ok(home.has(`projects/${COPY} 2/${COPY} 2.db`));
    assert.ok(python2.asked.some(([method, url]) => method === "GET" && url === "/projects/import/job1"));
});

await check("a project only looked at is not copied; what the folder holds is set aside", async () => {
    assert.equal(home.read("projects/lung/gates.json"), "lung in the folder");
    assert.ok(!Object.keys(home.json("projects/config.json")).some((name) => name.startsWith("lung (")));
    assert.deepEqual(Object.keys(opfs.json(`${STATE}/config.json`)), []);
    assert.ok(!opfs.has(`${STATE}/tumour`) && !opfs.has(`${STATE}/lung`));
    assert.equal(python2.moved, home.at("projects"));
});

await check("the copies are left for the app's notice, and told once", async () => {
    const left = JSON.parse(session.getItem("plexora-reconnect-copies"));
    assert.equal(left.label, "Plexora");
    assert.equal(left.copies[0].name, `${COPY} 2`);
    // The app frame's page, loaded with them waiting: one notice, then none.
    const shown = [];
    const frame = { console, setTimeout, clearTimeout, JSON, Promise, Object, Array, String, Map, Set, Date, Math,
                    Error, sessionStorage: session, PlexoraToast: { show: (notice) => shown.push(notice) },
                    location: { pathname: "/" } };
    frame.window = frame;
    createContext(frame);
    runInContext(client("views/webOnboarding.js"), frame);
    assert.equal(shown.length, 1);
    assert.equal(shown[0].title, "Kept both versions of “tumour”");
    assert.ok(shown[0].note.includes(`“${COPY} 2”`) && shown[0].note.includes("“Plexora” was closed"), shown[0].note);
    assert.equal(shown[0].timeout, 0);
    assert.equal(session.getItem("plexora-reconnect-copies"), null);
    assert.equal(frame.PlexoraOnboarding.noticeCopies(), false);
    assert.equal(shown.length, 1);
    const wired = (rel, pattern) => assert.ok(pattern.test(client(rel)), `${rel}: ${pattern}`);
    wired("views/webOnboarding.js", /const COPIES_KEY = "plexora-reconnect-copies"/);
    wired("views/webOnboarding.js", /function noticeCopies\(\)[\s\S]*sessionStorage\.removeItem\(COPIES_KEY\)/);
    wired("views/webOnboarding.js", /addEventListener\("DOMContentLoaded", noticeCopies/);
    wired("views/webOnboarding.js", /Object\.freeze\(\{[^}]*noticeCopies/);
    wired("views/webOnboarding.js", /if \(!noticeCopies\(\)\) global\.PlexoraToast/);
    wired("views/settingsPage.js", /store\.reconnect\(\)[\s\S]{0,400}noticeCopies/);
    wired("services/desktopHandoff.js", /store\.reconnect\(\)[\s\S]{0,400}noticeCopies/);
    // The home page's card reloads its frame after a Reconnect: told by the load.
    wired("views/quickViewLanding.js", /store\.reconnect\(\)[\s\S]{0,400}window\.location\.reload\(\)/);
});

await check("a copy that cannot be made stops the Reconnect: nothing moved, nothing set aside", async () => {
    const browser = new FakeDir("");
    const folder = new FakeDir("Plexora").file("projects/config.json", JSON.stringify({ a: { id: "a" } }))
        .file("projects/a/x", "folder");
    folder.permission = "prompt";
    const ownIdb = fakeIndexedDB();
    ownIdb.data.set("data", { kind: "folder", home: folder, name: "Plexora", label: "Plexora" });
    browser.file(`${STATE}/config.json`, JSON.stringify({ a: { id: "a" } }), LONG_AGO)
        .file(`${STATE}/a/x`, "browser", LONG_AGO);
    const python = fakePython(browser, { fail: true });
    const store = visit(browser, ownIdb, python, fakeSession());
    await store.prepare();
    await later();
    browser.file(`${STATE}/a/x`, "edited");
    const answer = await store.reconnect();
    assert.equal(answer.ok, false);
    assert.equal(answer.reason, "failed");
    assert.equal(python.moved, null);
    assert.equal(browser.read(`${STATE}/a/x`), "edited");
    assert.equal(folder.read("projects/a/x"), "folder");
});

console.log(`${checks} checks passed, ${failed} failed`);
if (failed) process.exit(1);
console.log("browser store reconnect edits probe OK");
