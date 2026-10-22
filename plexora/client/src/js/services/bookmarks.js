/**
 * bookmarks.js - views kept to come back to, on any image, 2-D or 3-D.
 *
 * A BOOKMARK IS A CAPTURE. Core keeps no store of its own: a plugin that
 * keeps captures registers one (Figure Builder's captures bin, through its
 * static/figureBookmarkStore.js, loaded on every viewer page by
 * Plugin.viewer_scripts), and a bookmark is a capture in it with a flag and a
 * label. So a bookmark has the capture's reproducible scene -- the region,
 * the channels with their colours and windows, the overlays, and on a z-stack
 * the plane, the projection, or in 3-D the camera, the area, the cut and the
 * surfaces -- its preview raster, and its place in Figure Builder (badged,
 * filterable, usable in a figure). Without such a plugin there are no
 * bookmarks, and the toolbar icon is not shown.
 *
 * THE STORE, as registered (`registerStore`, or the `plexora:bookmarks-ready`
 * event for a store whose script ran first):
 *
 *     list(datasource)              -> Promise<[{id, label, created_at,
 *                                       preview_url, scene}]>, bookmarks only
 *     capture(ctx)                  -> scene, read synchronously
 *     restore(ctx, scene, {persist})-> Promise<report>
 *     add({id, datasource, scene, label})          -> Promise<bool>
 *     putPreview(id, blob, {width, height})        -> Promise<bool>
 *     update(id, {bookmarked?, label?})            -> Promise<bool>
 *     locateUrl(id)                 -> string, the capture in its own page
 *
 * `ctx` is the viewer as a scene is read from it: `{viewer, config,
 * dataLayer, datasource, url}`.
 *
 * Saving reads the scene at once -- before anything is awaited, so a pan in
 * the next frame is not in it -- and the picture of the screen with it
 * (services/viewRaster.js); the list shows the bookmark straight away, its
 * thumbnail as soon as the picture is made, and it is written behind. A
 * restore puts the scene back and KEEPS it (`persist`): going back to a
 * bookmark is going somewhere. Changes are announced as
 * `plexora:bookmarks-changed` (and to `onChange` listeners); the same event
 * from anyone else -- Figure Builder's own bin -- reads the list again.
 *
 * OPENING AT ONE from another page ("Go to view", Figure Builder's
 * Bookmarks): that page leaves a note in sessionStorage under OPEN_KEY,
 * `{datasource, id}`, and goes to the viewer; here it is read once -- when a
 * store registers, and whenever the viewer is shown again -- and, on that
 * image, restored as soon as the image is open.
 *
 * Classic script, `window.PlexoraBookmarks`.
 */
(function (global) {
    "use strict";

    const READY = "plexora:bookmarks-ready";
    const CHANGED = "plexora:bookmarks-changed";
    //: The note a page leaves to open the viewer at a bookmark.
    const OPEN_KEY = "plexora:bookmark-open";
    //: How long to wait for the image to open before giving up on a note (ms).
    const OPEN_WAIT_MS = 20000;
    //: After a restore, how long view changes are the restore's own (the
    //: camera's turn, a glide) before one means the user moved on (ms).
    const SETTLE_MS = 900;

    let store = null;
    let cache = null;
    let loading = null;
    let loadedFor = null;
    let activeId = null;
    let activeUntil = 0;
    const listeners = new Set();

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }

    function datasource() {
        return global.flaskVariables?.datasource || "";
    }

    /** The viewer as a scene is read from it and put back into. */
    function context() {
        const core = global.__plexora || {};
        const viewer = core.seaDragonViewer || null;
        return {
            viewer, config: viewer?.config || null, dataLayer: core.dataLayer || null,
            datasource: datasource(),
            url: typeof global.plexoraUrl === "function" ? global.plexoraUrl : (path) => `/${path}`,
        };
    }

    function newId() {
        const bytes = new Uint8Array(6);
        try {
            global.crypto.getRandomValues(bytes);
        } catch (error) {
            for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
        }
        return "cap_" + [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
    }

    function emit(detail) {
        for (const fn of [...listeners]) {
            try {
                fn(detail);
            } catch (error) {
                console.error("bookmarks: a listener failed", error);
            }
        }
        try {
            global.dispatchEvent?.(new CustomEvent(CHANGED, { detail: { ...detail, origin: "core" } }));
        } catch (error) {
            // No CustomEvent in a bare runtime.
        }
    }

    function warn(title) {
        global.PlexoraToast?.show?.({ title, tone: "warning", timeout: 6000 });
    }

    /** A store answers the whole interface, or is not one. */
    function isStore(candidate) {
        return Boolean(candidate) && ["list", "capture", "restore", "add", "putPreview", "update", "locateUrl"]
            .every((name) => typeof candidate[name] === "function");
    }

    function registerStore(candidate) {
        if (!isStore(candidate)) return false;
        store = candidate;
        cache = null;
        loadedFor = null;
        emit({ reason: "store" });
        openNoted();
        return true;
    }

    /** The note another page left (OPEN_KEY), when it is for this image:
     *  taken, so a reload does not open it again. */
    function takeNote() {
        try {
            const raw = global.sessionStorage?.getItem(OPEN_KEY);
            if (!raw) return null;
            const note = JSON.parse(raw);
            if (!note?.id || note.datasource !== datasource()) return null;
            global.sessionStorage.removeItem(OPEN_KEY);
            return note;
        } catch (error) {
            return null;
        }
    }

    /** Resolves true once the image is open in the viewer (false on timeout). */
    function imageOpen() {
        const open = () => (global.__plexora?.seaDragonViewer?.viewer?.world?.getItemCount?.() || 0) > 0;
        if (open()) return Promise.resolve(true);
        return new Promise((resolve) => {
            const started = now();
            const poll = () => {
                if (open()) resolve(true);
                else if (now() - started > OPEN_WAIT_MS) resolve(false);
                else setTimeout(poll, 100);
            };
            setTimeout(poll, 100);
        });
    }

    /** Open at the bookmark a note names, if there is one for this image. */
    async function openNoted() {
        if (!available()) return null;
        const note = takeNote();
        if (!note) return null;
        if (!await imageOpen()) return null;
        await list({ refresh: true });
        if (!find(note.id)) {
            warn("That saved view is no longer bookmarked.");
            return null;
        }
        return restore(note.id);
    }

    /** Whether bookmarks can be kept here: a store, and an image open. */
    function available() {
        return Boolean(store && datasource());
    }

    /** This image's bookmarks, newest first (read once, then kept). */
    function list({ refresh = false } = {}) {
        if (!available()) return Promise.resolve([]);
        const ds = datasource();
        if (cache && loadedFor === ds && !refresh) return Promise.resolve(cache);
        if (loading) return loading;
        loading = Promise.resolve(store.list(ds))
            .then((rows) => {
                // What is on its way to the store stays listed.
                const pending = (cache || []).filter((entry) => entry.pending);
                const known = new Set((rows || []).map((row) => row.id));
                cache = [...pending.filter((entry) => !known.has(entry.id)), ...(rows || [])];
                loadedFor = ds;
                return cache;
            })
            .catch(() => cache || [])
            .finally(() => {
                loading = null;
            });
        return loading;
    }

    /** The list as last read, without asking (for painting). */
    function cached() {
        return cache || [];
    }

    function find(id) {
        return cached().find((entry) => entry.id === id) || null;
    }

    function nextLabel() {
        const taken = new Set(cached().map((entry) => entry.label));
        let n = cached().length + 1;
        while (taken.has(`View ${n}`)) n += 1;
        return `View ${n}`;
    }

    /**
     * Bookmark what is on screen. Returns `{id, entry, preview}` at once --
     * `preview` resolves the picture (`{blob, width, height}` or null), for
     * the tray's animation -- or null when there is nowhere to keep it. The
     * write goes on behind; a failure takes the entry out again and says so.
     */
    function bookmark({ label = "" } = {}) {
        if (!available()) return null;
        const ctx = context();
        if (!ctx.viewer) return null;
        let scene;
        try {
            // Now, before anything is awaited: the view the user pressed K on.
            scene = store.capture(ctx);
        } catch (error) {
            console.error("bookmarks: the view could not be read", error);
            warn("This view could not be bookmarked.");
            return null;
        }
        const raster = global.PlexoraViewRaster;
        const osd = ctx.viewer.viewer;
        const preview = raster ? raster.blob(osd, raster.fullViewRect(osd)) : Promise.resolve(null);
        const id = newId();
        const entry = { id, label: String(label || "").trim().slice(0, 64) || nextLabel(),
                        created_at: new Date().toISOString(), preview_url: null, scene, pending: true };
        cache = [entry, ...cached()];
        loadedFor = loadedFor || datasource();
        emit({ reason: "added", id });
        const ds = datasource();
        const written = (async () => {
            const picture = await preview.catch(() => null);
            if (picture?.blob && typeof global.URL?.createObjectURL === "function") {
                entry.preview_url = global.URL.createObjectURL(picture.blob);
                entry.local = true;
                emit({ reason: "preview", id });
            }
            const stored = await Promise.resolve(store.add({ id, datasource: ds, scene, label: entry.label }))
                .catch(() => false);
            if (!stored) {
                cache = cached().filter((other) => other.id !== id);
                emit({ reason: "failed", id });
                warn("This view could not be bookmarked: the captures store did not take it.");
                return false;
            }
            if (picture?.blob) {
                await Promise.resolve(store.putPreview(id, picture.blob,
                                                       { width: picture.width, height: picture.height }))
                    .catch(() => false);
            }
            entry.pending = false;
            emit({ reason: "stored", id });
            return true;
        })();
        return { id, entry, preview, written };
    }

    /**
     * Go back to bookmark `id`: its scene put back and kept. Resolves the
     * restore's report (what could not be put back is in it), or null.
     */
    async function restore(id) {
        const entry = find(id);
        if (!store || !entry?.scene) return null;
        activeId = id;
        activeUntil = now() + SETTLE_MS;
        emit({ reason: "active", id });
        let report = null;
        try {
            report = await store.restore(context(), entry.scene, { persist: true });
        } catch (error) {
            console.error("bookmarks: the view could not be restored", error);
            warn(`${entry.label} could not be restored.`);
        }
        activeUntil = now() + SETTLE_MS;
        const missing = report?.missing_channels || [];
        if (missing.length) {
            global.PlexoraToast?.show?.({ title: `${entry.label}: some channels are not in this image`,
                                          lines: missing, timeout: 8000 });
        }
        return report;
    }

    /** Take the bookmark off: the capture stays in Figure Builder's bin,
     *  and any figure made from it is untouched. */
    async function unbookmark(id) {
        const entry = find(id);
        if (!store || !entry) return false;
        cache = cached().filter((other) => other.id !== id);
        if (activeId === id) activeId = null;
        emit({ reason: "removed", id });
        const ok = await Promise.resolve(store.update(id, { bookmarked: false })).catch(() => false);
        if (!ok) {
            cache = [entry, ...cached()].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
            emit({ reason: "failed", id });
            warn(`${entry.label} could not be removed from bookmarks.`);
        }
        return Boolean(ok);
    }

    async function rename(id, label) {
        const entry = find(id);
        const name = String(label || "").trim().slice(0, 64);
        if (!store || !entry || !name || name === entry.label) return false;
        const before = entry.label;
        entry.label = name;
        emit({ reason: "renamed", id });
        const ok = await Promise.resolve(store.update(id, { label: name })).catch(() => false);
        if (!ok) {
            entry.label = before;
            emit({ reason: "failed", id });
        }
        return Boolean(ok);
    }

    /** Where bookmark `id` is in Figure Builder (its captures page). */
    function locateUrl(id) {
        return store && find(id) ? store.locateUrl(id) : "";
    }

    /** The bookmark whose view is on screen, until the user moves on. */
    function active() {
        return activeId;
    }

    /** The view moved: past a restore's own motion, it is not the
     *  bookmark's any more. */
    function viewMoved() {
        if (!activeId || now() < activeUntil) return;
        const was = activeId;
        activeId = null;
        emit({ reason: "inactive", id: was });
    }

    function onChange(fn) {
        listeners.add(fn);
        return () => listeners.delete(fn);
    }

    const api = {
        READY, CHANGED, OPEN_KEY,
        registerStore, openNoted, available, list, cached, find, bookmark, restore, unbookmark, rename,
        locateUrl, active, viewMoved, onChange, context,
    };
    global.PlexoraBookmarks = api;

    // Someone else changed the captures (Figure Builder's bin, another page's
    // store): read the list again.
    global.addEventListener?.(CHANGED, (event) => {
        if (event?.detail?.origin === "core" || !available()) return;
        list({ refresh: true }).then(() => {
            for (const fn of [...listeners]) fn({ reason: "reloaded" });
        });
    });
    // Back in the viewer from another page that left a note.
    global.addEventListener?.("plexora:viewer-shown", () => { openNoted(); });
    // A store whose script ran before this one waits for this.
    try {
        global.dispatchEvent?.(new CustomEvent(READY, { detail: { registerStore } }));
    } catch (error) {
        // No CustomEvent in a bare runtime.
    }
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
