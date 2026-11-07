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
 * That folder is Documents/Plexora: the browser's dialog opens in Documents
 * and Plexora makes its folder there (a page cannot make a folder unasked;
 * the user confirms the place once, and the browser asks to allow editing).
 * Inside it:
 *   projects/   the store Python opens as /data -- the browser reads all of it
 *               into memory when it opens it, so it holds state, never pixels;
 *   pyramids/   the pyramidized copies Plexora builds (services/
 *               browserPyramid.js), one folder per image, read on demand.
 *
 * The choice is asked once, on the first visit (services/shell.js:
 * `shouldAsk` / `useFolder` / `useBrowser`), and changed in Settings > Data
 * (`switchTo`, which reloads the page so Python starts on the new store).
 * On a later visit `prepare` gives the boot the folder to open, after one
 * click when the browser asks for permission again.
 *
 *   prepare(ask)        -> at boot: the projects folder to mount, or null (the browser)
 *   current()           -> {kind: "browser"|"folder", label?, chosen}: label as
 *                          "Documents › Plexora"
 *   pyramids()          -> Plexora's pyramids folder, when the projects are in Plexora's folder
 *   shouldAsk()         -> whether this visit should ask where (the first one)
 *   useFolder(startIn)  -> from a click: confirm the place, move the store there now
 *   useBrowser()        -> remember the browser's storage as the choice
 *   switchTo(kind)      -> from Settings: change, bring the projects along, reload
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
        }
        if (state !== "granted") {
            current = { kind: "browser", chosen: true, notOpened: labelOf(record.parent) };
            return null;
        }
        opened = await parts(record.home);
        current = { kind: "folder", chosen: true, label: labelOf(record.parent) };
        return opened.projects;
    }

    //: The place the user confirms (the dialog opens in Documents), and
    //: Plexora's folder in it -- or the place itself, when it is that folder.
    async function chooseHome(startIn) {
        try {
            const parent = await global.showDirectoryPicker({ id: "plexora-data", mode: "readwrite",
                                                              startIn: startIn || "documents" });
            const home = parent.name === FOLDER ? parent : await parent.getDirectoryHandle(FOLDER, { create: true });
            return { parent: parent.name === FOLDER ? null : parent.name, home };
        } catch (error) {
            if (error && error.name === "AbortError") return null;
            throw error;
        }
    }

    async function remember(chosen) {
        await write({ kind: "folder", home: chosen.home, name: FOLDER, parent: chosen.parent });
        current = { kind: "folder", chosen: true, label: labelOf(chosen.parent) };
    }

    /**
     * From the user's click: keep the projects in Plexora's folder from now
     * on. What this browser holds is copied there first when the folder holds
     * no projects; one that already does (from another browser, say) is used
     * as it is. Null when cancelled.
     */
    async function useFolder(startIn) {
        const chosen = await chooseHome(startIn);
        if (!chosen) return null;
        await global.PlexoraPython.flush();
        const next = await parts(chosen.home);
        if (await isEmpty(next.projects)) await copyTree(await browserState(), next.projects);
        await global.PlexoraPython.useStore(next.projects);
        opened = next;
        await remember(chosen);
        return { ...current };
    }

    async function useBrowser() {
        await write({ kind: "browser" });
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
            if (await isEmpty(next.projects)) {
                await copyTree(opened ? opened.projects : await browserState(), next.projects);
            }
            await remember(chosen);
        } else {
            const to = await browserState();
            if (opened && await isEmpty(to)) await copyTree(opened.projects, to);
            await write({ kind: "browser" });
        }
        global.location.reload();
        return true;
    }

    global.PlexoraStore = Object.freeze({
        prepare,
        current: () => ({ ...current }),
        pyramids: () => (opened && current.kind === "folder" ? opened.pyramids : null),
        shouldAsk: () => canWriteFolders() && !current.chosen,
        canWriteFolders,
        useFolder,
        useBrowser,
        switchTo,
        FOLDER,
        labelOf,
    });
})(window);
