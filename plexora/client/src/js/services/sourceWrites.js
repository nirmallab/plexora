/**
 * PlexoraSourceWrites -- saving into the user's own files, on the website.
 *
 * The desktop app writes gates, QC calls and ROI columns into the table or
 * .h5ad a project was made from, where it is (Save Gates to Source File, QC's
 * "Save into file", the ROI panel's cell columns). On the website that file
 * was picked (services/pickedFiles.js) and Python has it read-only, so Python
 * writes a staged copy (plexora/server/utils/source_writes.py), the worker
 * makes the copy the file Python reads from then on, and hands it here. This
 * saves it:
 *
 * - over the original, through the File System Access handle the picker gave
 *   (Chromium). The browser's own prompt asks once for write access to that
 *   file: at once when the click that saved is recent enough for the browser
 *   to ask, else after one question here ("Save into <name>?"), whose click
 *   is the one it asks from;
 * - as a download, with one short notice, where there is no handle (Firefox,
 *   Safari, a pick through `<input type=file>`) or write access was refused.
 *
 * Only when the user allowed it: Settings > Data > "Save into your files", off
 * by default, kept in this site's storage and answered to Python when it asks
 * before it writes (`ask("sourceWrites")`), and checked again here. The page
 * writes only through the handle it holds for the path Python names: a file
 * the user picked.
 *
 *   allowed() / setAllowed(on)  the setting
 *   save({source, file})        one staged file saved (PlexoraPython.saveWith;
 *                               saves run one after another)
 *   outcomes()                  [{source, name, bytes, how}], newest last; how:
 *                               "replaced", "downloaded", "declined" (the question
 *                               closed), "off" (the setting was turned off)
 *
 * Classic script, `window.PlexoraSourceWrites`, in the site's host page. Uses
 * PlexoraPython, PlexoraPicks, and the app frame's PlexoraConfirm and PlexoraToast.
 */
(function (global) {
    "use strict";

    const KEY = "plexora.sourceWrites";
    const MODE = { mode: "readwrite" };
    const KEEP = 20;
    const done = [];
    let chain = Promise.resolve();

    function allowed() {
        try {
            return global.localStorage.getItem(KEY) === "on";
        } catch (error) {
            return false; // storage blocked: never on
        }
    }

    function setAllowed(on) {
        try {
            if (on) global.localStorage.setItem(KEY, "on");
            else global.localStorage.removeItem(KEY);
        } catch (error) { /* storage blocked: stays off */ }
        return allowed();
    }

    //: The app's frame (services/shell.js), where questions and notices show.
    function app() {
        try {
            return global.document.getElementById("plexora-app")?.contentWindow || null;
        } catch (error) {
            return null;
        }
    }

    //: Write access to `handle`: "granted", "denied", or "ask" when the browser
    //: asks only from a click and the one that saved is too long ago.
    async function permission(handle) {
        if (typeof handle.queryPermission === "function"
            && await handle.queryPermission(MODE) === "granted") return "granted";
        if (typeof handle.requestPermission !== "function") return "granted"; // createWritable asks
        try {
            return await handle.requestPermission(MODE) === "granted" ? "granted" : "denied";
        } catch (error) {
            return "ask";
        }
    }

    async function writeInto(handle, file) {
        const writable = await handle.createWritable();
        try {
            await writable.write(file);
            await writable.close();
        } catch (error) {
            await writable.abort().catch(() => {});
            throw error;
        }
    }

    //: One question, whose click lets the browser ask: "save", "download", or
    //: null (closed, or the page went away).
    async function askToSave(name) {
        const win = app();
        const confirm = win && win.PlexoraConfirm;
        if (!confirm || typeof confirm.choose !== "function") return "download";
        const gone = new Promise((resolve) => win.addEventListener("pagehide", () => resolve(null), { once: true }));
        return Promise.race([confirm.choose({
            title: `Save into ${name}?`,
            choices: [{ value: "download", label: "Download instead" },
                      { value: "save", label: "Save", kind: "primary", focus: true }],
            inline: true, close: true,
        }), gone]);
    }

    function download(file) {
        const url = URL.createObjectURL(file);
        const link = global.document.createElement("a");
        link.href = url;
        link.download = file.name;
        link.style.display = "none";
        global.document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
    }

    function notice(title, note) {
        const toast = app()?.PlexoraToast;
        if (toast && typeof toast.show === "function") toast.show({ title, note });
    }

    async function saveOne({ source, file }) {
        const picks = global.PlexoraPicks;
        const name = file.name;
        const entry = { source, name, bytes: file.size, how: null };
        if (!allowed()) {
            entry.how = "off";
        } else {
            // What the page reads at that path from now on too (its own
            // readers: services/browserTable.js, browserNode.js).
            picks?.adopt?.(source, file);
            const handle = picks?.handleFor?.(source) || null;
            let refused = !handle;
            if (handle && typeof handle.createWritable === "function") {
                let state = await permission(handle);
                if (state === "ask") {
                    const answer = await askToSave(name);
                    state = answer === "save" ? await permission(handle) : answer === null ? "declined" : "denied";
                }
                if (state === "granted") {
                    try {
                        await writeInto(handle, file);
                        entry.how = "replaced";
                    } catch (error) {
                        console.error("[plexora] saving into", name, error);
                    }
                }
                if (state === "declined") entry.how = "declined";
                refused = entry.how !== "replaced";
            }
            if (!entry.how) {
                download(file);
                entry.how = "downloaded";
                notice(`${name} downloaded`, refused && handle
                    ? "Saving into the file was not allowed: use the downloaded copy in its place."
                    : "This browser cannot save into the file you opened: use the downloaded copy in its place.");
            }
        }
        done.push(entry);
        if (done.length > KEEP) done.shift();
        return entry;
    }

    function save(event) {
        const next = chain.then(() => saveOne(event));
        chain = next.catch(() => null);
        return next;
    }

    if (global.PlexoraPython) {
        global.PlexoraPython.answers("sourceWrites", () => ({ value: allowed() }));
        global.PlexoraPython.saveWith(save);
    }

    global.PlexoraSourceWrites = Object.freeze({
        allowed, setAllowed, save,
        outcomes: () => done.slice(),
    });
})(window);
