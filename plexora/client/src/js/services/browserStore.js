/**
 * PlexoraStore -- where the website keeps Plexora's projects.
 *
 * Plexora keeps every project (its settings, gates, regions, figures, the
 * records of what was done) in one data folder, which its Python reads as
 * /data. On the website that is either this site's own storage in the browser
 * (OPFS plexora/state, needing no permission, kept in this browser only), or
 * a workspace folder on the computer, where Chromium lets a page write (File
 * System Access): the projects are then files the user keeps, backs up, and
 * can open in the desktop app.
 *
 * A workspace is the folder the user picks, used as it is -- never a folder
 * made inside it (a pick of `Documents/Research` once became
 * `Research/Plexora`, and a pick of `Documents/Plexora` named otherwise became
 * `Plexora/Plexora`, which the desktop app never looked in). Inside it:
 *   workspace.json  {"schema": 1, "id", "createdAt", "createdBy", "name"}: the
 *                   workspace's identity, written only where there is none
 *                   (the desktop app writes the same file, plexora/paths.py);
 *   projects/       the store Python opens as /data -- the browser reads all
 *                   of it into memory when it opens it, so it holds state,
 *                   never pixels;
 *   pyramids/       the pyramidized copies Plexora builds (services/
 *                   browserPyramid.js), one folder per image, read on demand.
 * The desktop app's library is `Documents/Plexora` (plexora/paths.py
 * `website_library`), so that is the folder the onboarding steers to; a page
 * can never be given Documents itself, only a folder in it.
 *
 * Nothing is asked when the page loads. The first action that needs to keep
 * something (opening an image, saving a server, Open in Desktop App) runs the
 * onboarding in the app's frame (views/webOnboarding.js), which drives the
 * functions below; a later visit opens the folder with no question where the
 * browser still grants it (Chrome's "Allow on every visit", or an installed
 * site), and otherwise boots on the browser's storage and offers one click to
 * reconnect (`reconnect`: the home page's card, or the onboarding's
 * Reconnect screen).
 *
 *   prepare()               -> at boot: the projects folder to mount, or null (the browser)
 *   current()               -> {kind: "browser"|"folder", chosen, label?, notOpened?,
 *                               needsPermission?, workspaceId?, desktopFinds?, storage?}
 *   needsOnboarding()       -> nothing has been chosen yet
 *   needsReconnect()        -> a folder was chosen but is not open this visit
 *   pickFolder({startIn})   -> from a click: the browser's folder dialog; a handle or null
 *   inspectFolder(handle)   -> {kind: "workspace"|"legacy-nested"|"projects-inside"|"empty"|
 *                               "foreign", name, label, projects: [{name, title, id}], figures,
 *                               captures, nested, writable, workspaceId}
 *   adoptWorkspace(handle, {init, bring, look, onProgress})
 *                           -> the folder made the workspace: marker and folders where
 *                              missing, this browser's projects merged in, Python moved there
 *   prepareBrowser()        -> the browser's own storage made and written to once
 *   useBrowser()            -> remember the browser's storage as the choice
 *   continueInBrowser()     -> this visit only: the chosen folder left closed, nothing asked again
 *   reconnect()             -> from a click: the chosen folder opened again, Python moved there
 *   locations() / addLocation({startIn, init, handle}) / removeLocation(id) / setDefault(id)
 *                           -> the extra workspaces this browser keeps handles for (Settings)
 *   pyramids()              -> the workspace's pyramids folder, when it is open this visit
 *   reopen()                -> from a click: the chosen folder opened for its pyramids only
 *   useFolder(startIn)      -> from a click: pick, inspect, adopt (services/desktopHandoff.js)
 *   switchTo(kind, startIn, {reload}) -> from Settings: change, bring the projects along
 *   (every move merges: a project of the same name already there is kept)
 *
 * The choice is kept in IndexedDB plexora-store/store/data:
 *   {kind, home, workspaceId, name, label, chosen, onboarded, desktopFinds, locations: [{id, handle, label, name, workspaceId, addedAt}]}
 * Records written before this shape ({kind, home, name, parent}) keep working.
 *
 * Classic script, `window.PlexoraStore`; needs PlexoraPython.
 */
(function (global) {
    "use strict";

    const DB = "plexora-store";
    const STORE = "store";
    const KEY = "data";
    //: The desktop app's workspace is Documents/Plexora: the name the
    //: onboarding asks for, and how a folder the desktop app finds is told.
    const FOLDER = "Plexora";
    const WORKSPACE_FILE = "workspace.json";
    //: What a page never copies between stores (workers/storeSync.js
    //: `KEPT_OUT` and `KEPT_OUT_PREFIXES`, repeated here: this is a classic
    //: script): one machine's caches, the desktop's server token, project
    //: bundles' staging and exports, and write probes.
    const KEPT_OUT = new Set([".remote_cache", ".ims_index", ".models", ".telemetry", "server_token.json"]);
    const KEPT_OUT_PREFIXES = [".importing-", ".bundles"];
    const PROBE_PREFIX = ".plexora-write-probe";
    //: Files that do not make a folder anybody's: the operating systems' own.
    const SYSTEM_FILES = new Set([".ds_store", "desktop.ini", "thumbs.db", ".localized", ".directory"]);
    const RW = { mode: "readwrite" };

    let current = { kind: "browser", chosen: false };
    //: The record as read at boot or written since.
    let record = null;
    //: The workspace this visit, once granted: {home, projects, pyramids}.
    let opened = null;
    //: The workspace as an earlier visit chose it, granted now or not: the
    //: browser's dialog opens there, so choosing it again is one click.
    let remembered = null;
    //: Where the worker keeps a store in the browser (its boot says):
    //: "opfs", "indexeddb", or "memory" -- kept nowhere, lost with the tab.
    let storage = null;

    function database() {
        return new Promise((resolve, reject) => {
            const request = global.indexedDB.open(DB, 1);
            request.onupgradeneeded = () => request.result.createObjectStore(STORE);
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

    const read = () => transact("readonly", (store) => store.get(KEY)).catch(() => null);
    async function write(next) {
        await transact("readwrite", (store) => store.put(next, KEY));
        record = next;
        remembered = next.home || remembered;
    }
    const canWriteFolders = () => typeof global.showDirectoryPicker === "function";
    //: How a record of the old shape named its place: "Research › Plexora"
    //: for Plexora's folder made inside the one picked, else "Plexora".
    const labelOf = (parent) => (parent ? `${parent} › ${FOLDER}` : FOLDER);
    const labelFor = (rec) => (rec && rec.label) || labelOf(rec && rec.parent);
    const isPlexora = (name) => String(name || "").toLowerCase() === FOLDER.toLowerCase();
    //: Whether the desktop app finds this workspace by itself: Documents/
    //: Plexora (the folder the onboarding steers to; a page cannot see the
    //: path, so a folder named Plexora is taken for it) or the nested
    //: library an old version of this site made in it (plexora/paths.py
    //: `legacy_nested_library`). Elsewhere it is the website's until the user
    //: adds it in the desktop app's Settings › Data › Locations.
    function desktopFinds(rec) {
        if (!rec) return false;
        if (typeof rec.desktopFinds === "boolean") return rec.desktopFinds;
        return !rec.parent && isPlexora(rec.name || (rec.home && rec.home.name));
    }

    function failure(code, message, cause) {
        const error = new Error(message);
        error.code = code;
        if (cause) error.cause = cause;
        return error;
    }

    function newId(bytes = 16) {
        if (bytes === 16 && global.crypto && typeof global.crypto.randomUUID === "function") {
            return global.crypto.randomUUID().replaceAll("-", "");
        }
        const out = new Uint8Array(bytes);
        global.crypto.getRandomValues(out);
        return Array.from(out, (b) => b.toString(16).padStart(2, "0")).join("");
    }

    async function browserState() {
        const root = await global.navigator.storage.getDirectory();
        const plexora = await root.getDirectoryHandle("plexora", { create: true });
        return plexora.getDirectoryHandle("state", { create: true });
    }

    //: The workspace's two folders, made if missing.
    async function parts(home) {
        return { home,
                 projects: await home.getDirectoryHandle("projects", { create: true }),
                 pyramids: await home.getDirectoryHandle("pyramids", { create: true }) };
    }

    async function readJson(dir, name) {
        try {
            return JSON.parse(await (await (await dir.getFileHandle(name)).getFile()).text()) || {};
        } catch (error) {
            return {};
        }
    }

    //: A JSON object read from `dir/name`, or null when it is not there or
    //: is not an object.
    async function readObject(dir, name) {
        try {
            const value = JSON.parse(await (await (await dir.getFileHandle(name)).getFile()).text());
            return value && typeof value === "object" && !Array.isArray(value) ? value : null;
        } catch (error) {
            return null;
        }
    }

    async function writeFile(dir, name, data) {
        const writable = await (await dir.getFileHandle(name, { create: true })).createWritable();
        await writable.write(data);
        await writable.close();
    }

    const child = (dir, name, kind) => (kind === "directory" ? dir.getDirectoryHandle(name) : dir.getFileHandle(name))
        .catch(() => null);

    /**
     * The projects in `from` that `to` does not hold, copied into it with
     * their registry entries; a project of the same name in `to` is kept as
     * it is. The other top-level files and folders are copied where `to` has
     * none -- but never one machine's caches or a write probe (`KEPT_OUT`),
     * which the store never reads from a shared folder either. Returns
     * {moved, kept}: the names copied, and those left alone.
     */
    async function merge(from, to) {
        if (from && to && typeof from.isSameEntry === "function" && await from.isSameEntry(to).catch(() => false)) {
            return { moved: [], kept: [] };
        }
        const theirs = await readJson(from, "config.json");
        const ours = await readJson(to, "config.json");
        const moved = [];
        const kept = [];
        for (const [name, entry] of Object.entries(theirs)) {
            if (name in ours) { kept.push(name); continue; }
            try {
                const source = await from.getDirectoryHandle(name);
                await copyTree(source, await to.getDirectoryHandle(name, { create: true }));
            } catch (error) {
                // A project with no folder yet: its entry is all there is.
            }
            ours[name] = entry;
            moved.push(name);
        }
        for await (const [name, handle] of from.entries()) {
            if (name === "config.json" || name in theirs) continue;
            if (KEPT_OUT.has(name) || name.startsWith(PROBE_PREFIX)
                || KEPT_OUT_PREFIXES.some((prefix) => name.startsWith(prefix))) continue;
            if (await child(to, name, handle.kind)) continue;
            if (handle.kind === "directory") {
                await copyTree(handle, await to.getDirectoryHandle(name, { create: true }));
            } else {
                await writeFile(to, name, await handle.getFile());
            }
        }
        if (moved.length) await writeFile(to, "config.json", JSON.stringify(ours, null, 4));
        return { moved, kept };
    }

    //: Every file under `from` written under `to`, folders made as needed.
    async function copyTree(from, to) {
        for await (const [name, handle] of from.entries()) {
            if (handle.kind === "directory") {
                await copyTree(handle, await to.getDirectoryHandle(name, { create: true }));
            } else {
                await writeFile(to, name, await handle.getFile());
            }
        }
    }

    //: Whether Python has started: a pick before then is still mounted on
    //: the browser's storage, and a flush would wait for the start.
    async function pythonStarted() {
        const started = global.PlexoraShellStarted;
        if (!started) return true;
        return Promise.race([Promise.resolve(started).then(() => true, () => true),
                             new Promise((resolve) => global.setTimeout(() => resolve(false), 0))]);
    }

    /**
     * At boot, before Python starts: the projects folder to open as /data, or
     * null for the browser's own storage. Never asks: a folder the browser no
     * longer grants is left closed this visit (`needsReconnect`), and the
     * home page's card or the onboarding's Reconnect screen opens it from a
     * click. (An `ask` argument from an older caller is ignored.)
     */
    async function prepare() {
        const rec = await read();
        record = rec || null;
        remembered = (rec && rec.home) || null;
        if (!rec) {
            current = { kind: "browser", chosen: false };
            return null;
        }
        if (rec.kind !== "folder" || !rec.home) {
            current = { kind: "browser", chosen: true, onboarded: true };
            return null;
        }
        const label = labelFor(rec);
        const state = await rec.home.queryPermission(RW).catch(() => "denied");
        if (state !== "granted") {
            current = { kind: "browser", chosen: true, notOpened: label, needsPermission: true };
            return null;
        }
        try {
            opened = await parts(rec.home);
        } catch (error) {
            // Moved or deleted since: the browser still grants a handle to a
            // folder that is no longer there.
            current = { kind: "browser", chosen: true, notOpened: label, needsPermission: false, missing: true };
            return null;
        }
        current = { kind: "folder", chosen: true, onboarded: true, label, workspaceId: rec.workspaceId || null,
                    desktopFinds: desktopFinds(rec) };
        return opened.projects;
    }

    /**
     * From the user's click: the browser's folder dialog, and nothing else.
     * The dialog is the first thing awaited, so the click's activation is
     * still there for it (the frame's click counts for this page too). It
     * opens in Documents, or where an earlier visit chose. Null when
     * cancelled; a refusal throws (`NotAllowedError`, or `SecurityError`
     * when no click is behind it), and a browser with no folder dialog
     * throws `code: "unsupported"`.
     */
    async function pickFolder({ startIn = null } = {}) {
        if (!canWriteFolders()) throw failure("unsupported", "This browser cannot save into a folder.");
        try {
            return await global.showDirectoryPicker({ id: "plexora-data", mode: "readwrite",
                                                      startIn: startIn || remembered || "documents" });
        } catch (error) {
            if (error && error.name === "AbortError") return null;
            if (error && (error.name === "NotAllowedError" || error.name === "SecurityError")) {
                throw failure("denied", "The browser did not allow Plexora to use that folder.", error);
            }
            throw error;
        }
    }

    //: How many folders `dir/name` holds (a figure is a folder), 0 when absent.
    async function countFolders(dir, name) {
        const inner = await child(dir, name, "directory");
        if (!inner) return 0;
        let count = 0;
        for await (const [, handle] of inner.entries()) if (handle.kind === "directory") count += 1;
        return count;
    }

    //: The projects a registry lists, as cards: [{name, title, id}].
    const projectsOf = (registry) => Object.entries(registry || {})
        .filter(([, entry]) => entry && typeof entry === "object" && !Array.isArray(entry))
        .map(([name, entry]) => ({ name, title: typeof entry.title === "string" && entry.title ? entry.title : name,
                                   id: typeof entry.id === "string" ? entry.id : null }));

    /**
     * What a picked folder is, read without writing anything:
     *   workspace        it has workspace.json, or projects/config.json;
     *   legacy-nested    it holds none, but its Plexora folder does -- the
     *                    library an old version of this site made inside the
     *                    folder picked (`nested` is that folder);
     *   projects-inside  it is a workspace's projects folder itself (its own
     *                    config.json): a page cannot reach the folder above;
     *   empty            nothing in it but the system's own files;
     *   foreign          other things are in it (it may still be made a
     *                    workspace: nothing there is touched).
     * With the projects, figures and captures (the bin bookmarks are kept
     * in) the workspace's projects folder holds, and whether the browser
     * lets the page write there now.
     */
    async function inspectFolder(handle) {
        const entries = new Map();
        for await (const [name, entry] of handle.entries()) entries.set(name, entry);
        const marker = await readObject(handle, WORKSPACE_FILE);
        const projects = entries.get("projects");
        let registry = projects && projects.kind === "directory" ? await readObject(projects, "config.json") : null;
        let kind = null;
        let data = null;
        let nested = null;
        let inner = null;
        if (marker || registry) {
            kind = "workspace";
            data = projects && projects.kind === "directory" ? projects : null;
        } else {
            for (const [name, entry] of entries) {
                if (entry.kind !== "directory" || !isPlexora(name)) continue;
                const innerProjects = await child(entry, "projects", "directory");
                const innerRegistry = innerProjects ? await readObject(innerProjects, "config.json") : null;
                if (innerRegistry) {
                    kind = "legacy-nested";
                    nested = entry;
                    data = innerProjects;
                    registry = innerRegistry;
                    inner = await readObject(entry, WORKSPACE_FILE);
                    break;
                }
            }
        }
        if (!kind) {
            const own = await readObject(handle, "config.json");
            const looksLikeData = own && projectsOf(own).length > 0
                && (handle.name.toLowerCase() === "projects" || entries.has("datasets.json")
                    || entries.has(".figures") || projectsOf(own).some((p) => entries.has(p.name)));
            if (looksLikeData) {
                kind = "projects-inside";
                registry = own;
                data = handle;
            }
        }
        if (!kind) {
            const meaningful = [...entries.keys()].filter((name) => !SYSTEM_FILES.has(name.toLowerCase())
                                                                    && !name.startsWith("._"));
            kind = meaningful.length ? "foreign" : "empty";
        }
        const writable = await handle.queryPermission(RW).then((state) => state === "granted", () => false);
        const id = (inner || marker || {}).id;
        const bin = data ? await child(data, ".captures", "directory") : null;
        return {
            kind,
            name: handle.name,
            label: nested ? `${handle.name} › ${nested.name}` : handle.name,
            projects: projectsOf(registry),
            figures: data ? await countFolders(data, ".figures") : 0,
            captures: Boolean(bin && await child(bin, "captures.db", "file")),
            nested,
            writable,
            workspaceId: typeof id === "string" ? id : null,
        };
    }

    //: A file written and removed, so a folder the browser lets the page see
    //: but not change is found before anything is made in it.
    async function probe(dir) {
        const name = `${PROBE_PREFIX}-${newId(6)}`;
        const file = await dir.getFileHandle(name, { create: true });
        if (typeof file.createWritable === "function") {
            const writable = await file.createWritable();
            await writable.write("ok");
            await writable.close();
        }
        await dir.removeEntry(name).catch(() => null);
    }

    function writeFailure(error, label) {
        if (error && error.name === "QuotaExceededError") {
            return failure("quota", "There is no room left to save there.", error);
        }
        return failure("not-writable", `Plexora cannot write to “${label}”.`, error);
    }

    //: `UTC now`, as workspace.json has it: ISO 8601 to the second.
    const stamp = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

    //: The record for `home`, keeping what the old one had beside it.
    function folderRecord(home, fields) {
        const before = record || {};
        const locations = (before.locations || []).filter((loc) => loc && loc.handle);
        return { kind: "folder", home, chosen: true, onboarded: true, locations, ...fields };
    }

    /**
     * `handle` made the workspace, in an order that never leaves half a
     * workspace behind: a write probe before anything is made; then
     * workspace.json (only where there is none), projects/ and pyramids/
     * (only where missing); what this browser holds merged in (`bring`, the
     * default; a project of the same name there is kept); the choice
     * remembered; then, once Python has started (a first visit's pick waits
     * for it here, never for the file dialog), the store moved there.
     *
     *   init     a folder that is not yet a workspace may be made one
     *   look     inspectFolder's answer, when the caller already has it
     *   onProgress(step)  "probe", "folders", "merge", "remember", then "python"
     *            (waiting for Python to start, a first visit's) and "open",
     *            then "done", as each starts
     *
     * Throws with `code`: "projects-inside", "not-a-workspace", "denied",
     * "not-writable", "quota", or "partial" (with `step`: something was made
     * and the rest was not; running it again finishes it).
     */
    async function adoptWorkspace(handle, { init = true, bring = true, look = null, onProgress = null } = {}) {
        const say = (step) => { try { if (onProgress) onProgress(step); } catch (error) { /* the caller's */ } };
        const seen = look || await inspectFolder(handle);
        if (seen.kind === "projects-inside") {
            throw failure("projects-inside", `“${seen.name}” is the projects folder inside a workspace.`);
        }
        const existing = seen.kind === "workspace" || seen.kind === "legacy-nested";
        if (!existing && !init) throw failure("not-a-workspace", `“${seen.name}” is not a Plexora workspace.`);
        const root = seen.kind === "legacy-nested" && seen.nested ? seen.nested : handle;
        const label = seen.label || root.name;
        say("probe");
        let state = await root.queryPermission(RW).catch(() => "denied");
        if (state !== "granted") state = await root.requestPermission(RW).catch(() => "denied");
        if (state !== "granted") throw failure("denied", `The browser did not allow Plexora to change “${label}”.`);
        try {
            await probe(root);
        } catch (error) {
            throw writeFailure(error, label);
        }
        say("folders");
        let step = "marker";
        let workspaceId = null;
        let next;
        try {
            const marker = await readObject(root, WORKSPACE_FILE);
            if (marker && typeof marker.id === "string") {
                workspaceId = marker.id;
            } else if (!await child(root, WORKSPACE_FILE, "file")) {
                workspaceId = newId();
                await writeFile(root, WORKSPACE_FILE, JSON.stringify({
                    schema: 1, id: workspaceId, createdAt: stamp(), createdBy: "website", name: root.name,
                }, null, 4));
            }
            step = "folders";
            next = await parts(root);
        } catch (error) {
            const code = error && error.name === "QuotaExceededError" ? "quota" : "partial";
            const wrapped = failure(code, code === "quota" ? "There is no room left to save there."
                : `Plexora could not finish setting up “${label}”: ${error && error.message ? error.message : error}`, error);
            wrapped.step = step;
            throw wrapped;
        }
        const python = global.PlexoraPython;
        const up = await pythonStarted();
        const from = async () => (opened ? opened.projects : browserState());
        say("merge");
        let moved = { moved: [], kept: [] };
        try {
            if (up && python) await python.flush();
            if (bring) moved = await merge(await from(), next.projects);
        } catch (error) {
            const wrapped = failure(error && error.name === "QuotaExceededError" ? "quota" : "partial",
                                    `What this browser holds could not be copied to “${label}”: ${error && error.message ? error.message : error}`, error);
            wrapped.step = "merge";
            throw wrapped;
        }
        say("remember");
        const finds = seen.kind === "legacy-nested" || isPlexora(root.name);
        await write(folderRecord(root, { workspaceId, name: root.name, label, desktopFinds: finds }));
        // Python boots on the browser's storage until here on a first visit:
        // the store moves once it has started.
        say(up ? "open" : "python");
        try {
            if (global.PlexoraShellStarted) await global.PlexoraShellStarted;
            if (!up) say("open");
            if (python) {
                if (!up) {
                    await python.flush();
                    if (bring) {
                        const later = await merge(await from(), next.projects);
                        moved = { moved: [...moved.moved, ...later.moved], kept: [...moved.kept, ...later.kept] };
                    }
                }
                await python.useStore(next.projects);
            }
        } catch (error) {
            current = { kind: "browser", chosen: true, notOpened: label, needsPermission: false };
            const wrapped = failure("partial", `“${label}” is set up, but Plexora could not open it: `
                                    + `${error && error.message ? error.message : error}`, error);
            wrapped.step = "open";
            throw wrapped;
        }
        opened = next;
        current = { kind: "folder", chosen: true, onboarded: true, label, workspaceId, desktopFinds: finds };
        // The pyramids this browser holds for the site go there too, in the
        // background (services/derivedIndex.js).
        if (global.PlexoraDerived && global.PlexoraDerived.migrate) global.PlexoraDerived.migrate().catch(() => null);
        say("done");
        return { ...current, kind: "folder", look: seen.kind, created: seen.kind === "empty" || seen.kind === "foreign",
                 moved: moved.moved, kept: moved.kept };
    }

    /**
     * The browser's own storage, made and written to once: what the worker
     * opens at its next start, and the first sign of a browser that keeps
     * nothing for the site or has no room. {probe: "ok"|"quota"|"unavailable",
     * estimate: {usage, quota}|null}; "unavailable" is not a failure (the
     * worker keeps the projects in IndexedDB instead).
     */
    async function prepareBrowser() {
        let result = "ok";
        try {
            await probe(await browserState());
        } catch (error) {
            result = error && error.name === "QuotaExceededError" ? "quota" : "unavailable";
        }
        let estimate = null;
        try {
            if (global.navigator.storage && global.navigator.storage.estimate) {
                estimate = await global.navigator.storage.estimate();
            }
        } catch (error) {
            estimate = null;
        }
        return { probe: result, estimate };
    }

    //: The browser's storage as the choice; the workspace, where one was
    //: chosen, is kept for the dialog to open in (`prepare` opens only a
    //: "folder" record).
    const browserRecord = () => ({ kind: "browser", chosen: true, onboarded: true,
                                   ...(remembered ? { home: remembered } : {}),
                                   locations: ((record && record.locations) || []).filter((loc) => loc && loc.handle) });

    async function useBrowser() {
        await write(browserRecord());
        current = { kind: "browser", chosen: true, onboarded: true };
        return { ...current };
    }

    //: The chosen folder left closed for the rest of this visit: the projects
    //: stay in the browser's storage this time and nothing asks again until
    //: the next visit (the home page's Reconnect card stays).
    function continueInBrowser() {
        current = { ...current, declined: true };
        return { ...current };
    }

    /**
     * From a click, where a folder was chosen but this visit did not open it:
     * the browser asked for it again, then what this visit kept in the
     * browser merged into it and Python moved there. {ok: true, label} or
     * {ok: false, reason: "none"|"denied"|"missing"|"failed", error?}.
     */
    async function reconnect() {
        const rec = record && record.kind === "folder" && record.home ? record : null;
        if (!rec) return { ok: false, reason: "none" };
        if (opened && current.kind === "folder") return { ok: true, ...current };
        let state;
        try {
            state = await rec.home.requestPermission(RW);
        } catch (error) {
            state = "denied";
        }
        if (state !== "granted") return { ok: false, reason: "denied" };
        const label = labelFor(rec);
        const up = await pythonStarted();
        // The folders the pyramids beside their originals are in, from the
        // same click (services/derivedIndex.js), so none waits for another.
        const derived = global.PlexoraDerived;
        if (derived) {
            const ask = up && derived.reconnect ? derived.reconnect({ ask: true })
                : derived.requestFolders ? derived.requestFolders() : null;
            if (ask && ask.catch) ask.catch(() => 0);
        }
        // And the files picked on earlier visits: the browser's prompt just
        // answered listed every handle the site held, so the ones it granted
        // open now without another question, and a project opened next finds
        // them (services/reopenFiles.js). Before Python is up, its start
        // mounts them (PlexoraPicks.restore).
        const picks = global.PlexoraPicks;
        if (up && picks && typeof picks.reconnect === "function") picks.reconnect({ ask: false }).catch(() => null);
        let next;
        try {
            next = await parts(rec.home);
        } catch (error) {
            current = { ...current, missing: true };
            return { ok: false, reason: "missing", error: error && error.message };
        }
        try {
            const python = global.PlexoraPython;
            if (python) {
                await python.flush();
                await merge(await browserState(), next.projects);
                await python.useStore(next.projects);
            }
        } catch (error) {
            return { ok: false, reason: "failed", error: error && error.message ? error.message : String(error) };
        }
        opened = next;
        current = { kind: "folder", chosen: true, onboarded: true, label, workspaceId: rec.workspaceId || null,
                    desktopFinds: desktopFinds(rec) };
        if (derived && derived.migrate) derived.migrate().catch(() => null);
        return { ok: true, ...current };
    }

    /**
     * From a click, where the projects are kept in the workspace but this
     * visit did not open it: the folder opened again for the pyramids Plexora
     * keeps in it, so a copy goes where the user chose. The projects stay
     * where this visit's Python has them. True when it is open now.
     */
    async function reopen() {
        if (opened) return true;
        const rec = record && record.kind === "folder" ? record : await read();
        if (!rec || rec.kind !== "folder" || !rec.home) return false;
        const state = await rec.home.requestPermission(RW).catch(() => "denied");
        if (state !== "granted") return false;
        try {
            opened = await parts(rec.home);
        } catch (error) {
            return false;
        }
        return true;
    }

    // -- the extra workspaces (Settings › Data) --------------------------------
    //
    // The default workspace is the record's `home`; others the user adds are
    // kept by handle in `locations` (the worker mounts each at /library/<id>).
    // Their ids are stable for as long as the record keeps them.

    async function permissionOf(handle) {
        try {
            return await handle.queryPermission(RW);
        } catch (error) {
            return "denied";
        }
    }

    //: Every workspace this browser keeps, the default first:
    //: [{id, label, name, handle, isDefault, permission, workspaceId}].
    async function locations() {
        const rec = record || await read() || {};
        const out = [];
        if (rec.kind === "folder" && rec.home) {
            out.push({ id: rec.workspaceId || "default", label: labelFor(rec), name: rec.home.name, handle: rec.home,
                       isDefault: true, permission: await permissionOf(rec.home), workspaceId: rec.workspaceId || null });
        }
        for (const loc of rec.locations || []) {
            if (!loc || !loc.handle) continue;
            out.push({ id: loc.id, label: loc.label || loc.handle.name, name: loc.handle.name, handle: loc.handle,
                       isDefault: false, permission: await permissionOf(loc.handle),
                       workspaceId: loc.workspaceId || null, addedAt: loc.addedAt || null });
        }
        return out;
    }

    async function sameAs(a, b) {
        try {
            return Boolean(a && b && await a.isSameEntry(b));
        } catch (error) {
            return false;
        }
    }

    /**
     * From a click: another workspace kept beside the default one (`handle`
     * given: no dialog). A folder that is not a workspace yet is made one
     * only with `init`; otherwise {added: false, reason: "not-a-workspace",
     * look, handle}. The same folder twice is {added: false, reason:
     * "already"}. Null when the dialog is cancelled.
     */
    async function addLocation({ startIn = null, init = false, handle = null } = {}) {
        const picked = handle || await pickFolder({ startIn });
        if (!picked) return null;
        const look = await inspectFolder(picked);
        if (look.kind === "projects-inside") {
            throw failure("projects-inside", `“${look.name}” is the projects folder inside a workspace.`);
        }
        const existing = look.kind === "workspace" || look.kind === "legacy-nested";
        if (!existing && !init) return { added: false, reason: "not-a-workspace", look, handle: picked };
        const root = look.kind === "legacy-nested" && look.nested ? look.nested : picked;
        const rec = record || await read() || { kind: "browser" };
        const kept = (rec.locations || []).filter((loc) => loc && loc.handle);
        for (const loc of kept) {
            if (await sameAs(loc.handle, root)) return { added: false, reason: "already", location: loc };
        }
        if (rec.kind === "folder" && await sameAs(rec.home, root)) return { added: false, reason: "already", location: null };
        let state = await permissionOf(root);
        if (state !== "granted") state = await root.requestPermission(RW).catch(() => "denied");
        let workspaceId = look.workspaceId;
        if (state === "granted" && !await child(root, WORKSPACE_FILE, "file")) {
            try {
                await probe(root);
                workspaceId = newId();
                await writeFile(root, WORKSPACE_FILE, JSON.stringify({
                    schema: 1, id: workspaceId, createdAt: stamp(), createdBy: "website", name: root.name,
                }, null, 4));
                await root.getDirectoryHandle("projects", { create: true });
            } catch (error) {
                throw writeFailure(error, look.label);
            }
        }
        const location = { id: newId(6), handle: root, label: look.label, name: root.name,
                           workspaceId: workspaceId || null, addedAt: stamp() };
        await write({ ...rec, locations: [...kept, location] });
        return { added: true, location, look };
    }

    //: A kept workspace forgotten (its files are not touched). The default
    //: one cannot be: make another the default first.
    async function removeLocation(id) {
        const rec = record || await read();
        if (!rec) return [];
        if (rec.kind === "folder" && (rec.workspaceId || "default") === id) {
            throw failure("default", "That is where new work is saved: make another folder the default first.");
        }
        await write({ ...rec, locations: (rec.locations || []).filter((loc) => loc && loc.id !== id) });
        return locations();
    }

    /**
     * From a click: a kept workspace made the one new work is saved in, with
     * no reload (Python moved there, `adoptWorkspace` without bringing this
     * one's projects along); the old default stays kept beside it.
     */
    async function setDefault(id) {
        const rec = record || await read();
        const target = rec && (rec.locations || []).find((loc) => loc && loc.id === id);
        if (!target) throw failure("unknown", "That folder is no longer kept here.");
        const state = await target.handle.requestPermission(RW).catch(() => "denied");
        if (state !== "granted") throw failure("denied", `The browser did not allow Plexora to change “${target.label}”.`);
        const others = (rec.locations || []).filter((loc) => loc && loc.id !== id);
        if (rec.kind === "folder" && rec.home) {
            others.push({ id: rec.workspaceId || newId(6), handle: rec.home, label: labelFor(rec), name: rec.home.name,
                          workspaceId: rec.workspaceId || null, addedAt: stamp() });
        }
        const before = record;
        record = { ...rec, locations: others };
        try {
            return await adoptWorkspace(target.handle, { init: true, bring: false });
        } catch (error) {
            record = before;
            throw error;
        }
    }

    // -- the older entry points -------------------------------------------------

    /**
     * From the user's click: keep the projects in a workspace from now on
     * (services/desktopHandoff.js). What this browser holds is merged in.
     * Null when cancelled.
     */
    async function useFolder(startIn) {
        const handle = await pickFolder({ startIn });
        if (!handle) return null;
        return adoptWorkspace(handle, { init: true });
    }

    /**
     * From Settings: keep the projects in a workspace ("folder", the
     * browser's dialog from the click) or in this browser ("browser"). The
     * projects come along. A folder is used at once (`adoptWorkspace`); the
     * page then reloads unless `{reload: false}`, as it always has, so every
     * page shows the new place. Moving back to the browser always reloads:
     * Python starts on it.
     */
    async function switchTo(kind, startIn = null, { reload = true } = {}) {
        if (kind === "folder") {
            const handle = await pickFolder({ startIn });
            if (!handle) return null;
            const done = await adoptWorkspace(handle, { init: true });
            if (reload) global.location.reload();
            return done;
        }
        await global.PlexoraPython.flush();
        if (opened) await merge(opened.projects, await browserState());
        await write(browserRecord());
        global.location.reload();
        return true;
    }

    //: What the worker's boot said of the store (`mountState`): "folder",
    //: "opfs", "indexeddb", or "memory (why)".
    function booted(kind) {
        storage = String(kind || "").startsWith("memory") ? "memory" : kind || null;
    }

    global.PlexoraStore = Object.freeze({
        prepare,
        booted,
        current: () => ({ ...current, ...(current.kind === "browser" && storage ? { storage } : {}) }),
        //: Nothing chosen yet: the onboarding runs at the first save.
        needsOnboarding: () => !current.chosen,
        //: A folder chosen and not open this visit (and not set aside for it).
        needsReconnect: () => Boolean(current.notOpened) && current.kind !== "folder" && !current.declined,
        //: Open for the pyramids even when this visit's projects are in the
        //: browser (`reopen`): the copies go where the user chose.
        pyramids: () => (opened ? opened.pyramids : null),
        reopen,
        canWriteFolders,
        pickFolder,
        inspectFolder,
        adoptWorkspace,
        prepareBrowser,
        useBrowser,
        continueInBrowser,
        reconnect,
        locations,
        addLocation,
        removeLocation,
        setDefault,
        useFolder,
        switchTo,
        FOLDER,
        WORKSPACE_FILE,
        labelOf,
    });
})(window);
