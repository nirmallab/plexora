/**
 * PlexoraStore -- where the website keeps Plexora's projects.
 *
 * Plexora keeps every project (its settings, gates, regions, figures, the
 * records of what was done) in one data folder, which its Python reads as
 * /data. On the website that is either this site's own storage in the browser
 * (OPFS plexora/state, needing no permission, kept in this browser only), or
 * Plexora's folder on the computer, where Chromium lets a page write (File
 * System Access): the projects are then files the user keeps, backs up, and
 * can open in the desktop app.
 *
 * That folder is Documents/Plexora, where the desktop app keeps its projects
 * too (plexora/paths.py `website_library`). A page can reach only a folder
 * the user chooses in the browser's dialog, and never Documents itself
 * (Chromium refuses it, though not the folders in it), so the dialog opens in
 * Documents for the user to choose Plexora's folder (made by the desktop app,
 * or with New Folder); a folder of another name gets Plexora's made inside
 * it. Chosen once; the browser then asks to allow editing. Inside it:
 *   projects/   the store Python opens as /data -- the browser reads all of it
 *               into memory when it opens it, so it holds state, never pixels;
 *   pyramids/   the pyramidized copies Plexora builds (services/
 *               browserPyramid.js), one folder per image, read on demand.
 *
 * The choice is asked once, on the first visit (services/shell.js:
 * `shouldAsk` / `useFolder` / `useBrowser`), and changed in Settings > Data
 * (`switchTo`, which reloads the page so Python starts on the new store).
 * On a later visit `prepare` gives the boot the folder to open, after one
 * click when the browser asks for permission again -- until the user answers
 * Chrome's "Allow on every visit" (122+), or always for an installed site,
 * after which the folder opens with no question.
 *
 *   prepare(ask)        -> at boot: the projects folder to mount, or null (the browser)
 *   current()           -> {kind: "browser"|"folder", label?, chosen}: label as
 *                          "Documents › Plexora"
 *   pyramids()          -> Plexora's pyramids folder, when Plexora's folder is open this visit
 *   reopen()            -> from a click: Plexora's folder, chosen on an earlier visit but
 *                          not opened on this one, opened for its pyramids (the projects
 *                          stay where this visit's Python has them)
 *   shouldAsk()         -> whether this visit should ask where (the first one)
 *   useFolder(startIn)  -> from a click: confirm the place, move the store there now
 *   useBrowser()        -> remember the browser's storage as the choice
 *   switchTo(kind)      -> from Settings: change, bring the projects along, reload
 *   (both moves merge: a project of the same name already there is kept)
 *
 * Classic script, `window.PlexoraStore`; needs PlexoraPython.
 */
(function (global) {
    "use strict";

    const DB = "plexora-store";
    const STORE = "store";
    const KEY = "data";
    //: Plexora's own folder, made inside the one the user confirms (Documents).
    const FOLDER = "Plexora";
    let current = { kind: "browser", chosen: false };
    //: Plexora's folder this visit, once granted: {home, projects, pyramids}.
    let opened = null;
    //: Plexora's folder as an earlier visit chose it, granted now or not: the
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
    const write = (record) => transact("readwrite", (store) => store.put(record, KEY));
    const canWriteFolders = () => typeof global.showDirectoryPicker === "function";
    //: How the place is named to the user: "Documents › Plexora".
    const labelOf = (parent) => (parent ? `${parent} › ${FOLDER}` : FOLDER);

    async function browserState() {
        const root = await global.navigator.storage.getDirectory();
        const plexora = await root.getDirectoryHandle("plexora", { create: true });
        return plexora.getDirectoryHandle("state", { create: true });
    }

    //: Plexora's folder's two parts, made if missing.
    async function parts(home) {
        return { home,
                 projects: await home.getDirectoryHandle("projects", { create: true }),
                 pyramids: await home.getDirectoryHandle("pyramids", { create: true }) };
    }

    async function isEmpty(directory) {
        return (await directory.keys().next()).done;
    }

    async function readJson(dir, name) {
        try {
            return JSON.parse(await (await (await dir.getFileHandle(name)).getFile()).text()) || {};
        } catch (error) {
            return {};
        }
    }

    /**
     * The projects in `from` that `to` does not hold, copied into it with
     * their registry entries; a project of the same name in `to` is kept as
     * it is. The other top-level files and folders are copied where `to` has
     * none. Returns {moved, kept}: the names copied, and those left alone.
     */
    async function merge(from, to) {
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
            const there = await (handle.kind === "directory" ? to.getDirectoryHandle(name) : to.getFileHandle(name))
                .catch(() => null);
            if (there) continue;
            if (handle.kind === "directory") {
                await copyTree(handle, await to.getDirectoryHandle(name, { create: true }));
            } else {
                const writable = await (await to.getFileHandle(name, { create: true })).createWritable();
                await writable.write(await handle.getFile());
                await writable.close();
            }
        }
        if (moved.length) {
            const writable = await (await to.getFileHandle("config.json", { create: true })).createWritable();
            await writable.write(JSON.stringify(ours, null, 4));
            await writable.close();
        }
        return { moved, kept };
    }

    //: Every file under `from` written under `to`, folders made as needed.
    async function copyTree(from, to) {
        for await (const [name, handle] of from.entries()) {
            if (handle.kind === "directory") {
                await copyTree(handle, await to.getDirectoryHandle(name, { create: true }));
            } else {
                const writable = await (await to.getFileHandle(name, { create: true })).createWritable();
                await writable.write(await handle.getFile());
                await writable.close();
            }
        }
    }

    /**
     * At boot, before Python starts: the projects folder to open as /data, or
     * null for the browser's own storage. `ask(record)` is shown when the
     * browser wants a click before granting the folder again; it resolves
     * true to open it, false to use the browser's storage this time.
     */
    async function prepare(ask) {
        const record = await read();
        remembered = (record && record.home) || null;
        if (!record) {
            current = { kind: "browser", chosen: false };
            return null;
        }
        if (record.kind !== "folder" || !record.home) {
            current = { kind: "browser", chosen: true };
            return null;
        }
        let state = await record.home.queryPermission({ mode: "readwrite" }).catch(() => "denied");
        if (state !== "granted" && ask && await ask(record)) {
            state = await record.home.requestPermission({ mode: "readwrite" }).catch(() => "denied");
            // The same click for the folders the pyramids beside their
            // originals are in (services/derivedIndex.js), so none of them
            // waits for another: Plexora's own pyramids come with this folder.
            if (state === "granted" && global.PlexoraDerived) {
                await global.PlexoraDerived.requestFolders().catch(() => 0);
            }
        }
        if (state !== "granted") {
            current = { kind: "browser", chosen: true, notOpened: labelOf(record.parent) };
            return null;
        }
        opened = await parts(record.home);
        current = { kind: "folder", chosen: true, label: labelOf(record.parent) };
        return opened.projects;
    }

    /**
     * The place the user confirms, and Plexora's folder in it -- or the place
     * itself, when it is that folder. A browser never lets a page have
     * Documents itself (Chromium refuses the home folder, Desktop, Documents
     * and Downloads, though not the folders in them), so the dialog opens in
     * Documents for the user to choose Plexora's folder there (the desktop app
     * makes it; New Folder where it has not), or in that folder when an
     * earlier visit chose it. Plexora's own `projects` folder, chosen by
     * mistake, is refused with what to choose instead: a page cannot reach
     * the folder above the one it was given.
     */
    async function chooseHome(startIn) {
        let picked;
        try {
            picked = await global.showDirectoryPicker({ id: "plexora-data", mode: "readwrite",
                                                        startIn: startIn || remembered || "documents" });
        } catch (error) {
            if (error && error.name === "AbortError") return null;
            throw error;
        }
        // In any case: macOS and Windows name `Documents/plexora` and
        // `Documents/Plexora` the same folder, the desktop app's library, and
        // a Plexora made inside it would be a second library.
        if (picked.name.toLowerCase() === FOLDER.toLowerCase()) return { parent: null, home: picked };
        if (picked.name === "projects" && await picked.getFileHandle("config.json").then(() => true, () => false)) {
            throw new Error(`That is the projects folder inside ${FOLDER}: choose the ${FOLDER} folder itself.`);
        }
        return { parent: picked.name, home: await picked.getDirectoryHandle(FOLDER, { create: true }) };
    }

    async function remember(chosen) {
        remembered = chosen.home;
        await write({ kind: "folder", home: chosen.home, name: FOLDER, parent: chosen.parent });
        current = { kind: "folder", chosen: true, label: labelOf(chosen.parent) };
    }

    /**
     * From the user's click: keep the projects in Plexora's folder from now
     * on. What this browser holds is merged in first (`merge`: a project the
     * folder already has, from the desktop app or another browser, is kept as
     * it is there). Null when cancelled.
     */
    async function useFolder(startIn) {
        const chosen = await chooseHome(startIn);
        if (!chosen) return null;
        await global.PlexoraPython.flush();
        const next = await parts(chosen.home);
        await merge(await browserState(), next.projects);
        await global.PlexoraPython.useStore(next.projects);
        opened = next;
        await remember(chosen);
        // The pyramids this browser holds for the site go there too, in the
        // background (services/derivedIndex.js).
        if (global.PlexoraDerived) global.PlexoraDerived.migrate().catch(() => null);
        return { ...current };
    }

    //: The browser's storage as the choice; Plexora's folder, where one was
    //: chosen, is kept for the dialog to open in (`prepare` opens only a
    //: "folder" record).
    const browserRecord = () => ({ kind: "browser", ...(remembered ? { home: remembered } : {}) });

    async function useBrowser() {
        await write(browserRecord());
        current = { kind: "browser", chosen: true };
        return current;
    }

    /**
     * From Settings: keep the projects in Plexora's folder ("folder", the
     * browser's dialog from the click) or in this browser ("browser"). The
     * projects come along when the new place holds none; then the page
     * reloads, so Python starts on it.
     */
    async function switchTo(kind, startIn = null) {
        await global.PlexoraPython.flush();
        if (kind === "folder") {
            const chosen = await chooseHome(startIn);
            if (!chosen) return null;
            const next = await parts(chosen.home);
            await merge(opened ? opened.projects : await browserState(), next.projects);
            await remember(chosen);
        } else {
            if (opened) await merge(opened.projects, await browserState());
            await write(browserRecord());
        }
        global.location.reload();
        return true;
    }

    /**
     * From a click, where the projects are kept in Plexora's folder but this
     * visit did not open it ("Use browser this time", or a permission the
     * browser let lapse): the folder opened again for the pyramids Plexora
     * keeps in it, so a copy goes where the user chose. The projects stay
     * where this visit's Python has them. True when it is open now.
     */
    async function reopen() {
        if (opened) return true;
        const record = await read();
        if (!record || record.kind !== "folder" || !record.home) return false;
        const state = await record.home.requestPermission({ mode: "readwrite" }).catch(() => "denied");
        if (state !== "granted") return false;
        opened = await parts(record.home);
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
        //: Open for the pyramids even when this visit's projects are in the
        //: browser (`reopen`): the copies go where the user chose.
        pyramids: () => (opened ? opened.pyramids : null),
        reopen,
        shouldAsk: () => canWriteFolders() && !current.chosen,
        //: Where projects can only be kept in the browser: said once, on the
        //: first visit (services/shell.js); every visit when nothing is kept.
        shouldWarn: () => !canWriteFolders() && (!current.chosen || storage === "memory"),
        canWriteFolders,
        useFolder,
        useBrowser,
        switchTo,
        FOLDER,
        labelOf,
    });
})(window);
