/**
 * cellSelection.js - the cells the user has picked, shared by every view.
 *
 * WHY. A lasso on an embedding, a region drawn on the tissue and a step of an
 * analysis notebook all answer the same question -- "these cells" -- and each
 * used to keep its own answer: the notebook a file of row numbers, Cell
 * Explorer nothing at all. One selection here, keyed the way the viewer keys
 * cells (project + mask label, the id every cell layer's colour table is
 * indexed by), lets each of them write it and the others follow.
 *
 * WHAT IT HOLDS.
 *
 *   selection  {projects: {name: Uint32Array of mask labels, sorted}, scope:
 *              [project names it speaks for], source, label, at, rev}. One
 *              selection for the whole app, spanning a dataset's projects: a
 *              lasso on a joint UMAP picks cells on several images at once.
 *              A project in `scope` with no entry has none of it selected.
 *   mode       how the tissue shows it: "highlight" (selected drawn bright
 *              over the rest), "dim" (the rest darkened) or "only" (the rest
 *              not drawn). One setting, remembered in this browser.
 *   region     a passing hint, not a selection: the cells of a region the
 *              pointer is resting on, which an embedding outlines in gold.
 *   history    every earlier selection, for Undo and Redo. One history for
 *              the app, as there is one selection: a lasso in the notebook,
 *              a brush in the viewer's window and a region used on the
 *              tissue are steps of it, and Undo walks back whichever made
 *              them. A clear is a step too, so Undo brings a cleared
 *              selection back. The stored arrays are never changed in place,
 *              so a step costs a reference, not a copy. Kept for the page's
 *              life (a full page load starts a new one).
 *
 * Events on window: `plexora:cells-selected` {rev, source, scope, count},
 * `plexora:cells-selection-mode` {mode}, `plexora:cells-region` {project,
 * name, count}. Listeners read the arrays from here, so a million ids are
 * never copied into an event.
 *
 * PERSISTENCE. Walking a dataset image by image is a full page load per
 * image, and the selection must still be there on the next one. It is kept
 * in IndexedDB -- the only browser store that takes a few megabytes of typed
 * arrays without serialising them -- and read back on load; until that read
 * lands `ready()` is pending and `get()` answers what is in memory. A browser
 * that refuses IndexedDB (a private window) keeps it for the page's life.
 *
 * Classic script, `window.PlexoraCellSelection`. Owns no DOM.
 */
(function (global) {
    "use strict";

    const DB = "plexora-cell-selection";
    const STORE = "state";
    const KEY = "current";
    const MODE_KEY = "plexora:cell-selection-mode";
    const MODES = ["highlight", "dim", "only"];

    let selection = null;
    let region = null;
    let mode = readMode();
    let rev = 0;
    let loaded = null;
    const HISTORY = 60;
    let past = [];
    let future = [];

    function readMode() {
        try {
            const value = global.localStorage?.getItem(MODE_KEY);
            return MODES.includes(value) ? value : "dim";
        } catch (error) {
            return "dim";
        }
    }

    function emit(name, detail) {
        try {
            global.dispatchEvent?.(new CustomEvent(name, { detail }));
        } catch (error) {
            console.warn("cellSelection: a listener failed", error);
        }
    }

    function openDb() {
        return new Promise((resolve, reject) => {
            if (!global.indexedDB) {
                reject(new Error("no IndexedDB"));
                return;
            }
            const request = global.indexedDB.open(DB, 1);
            request.onupgradeneeded = () => request.result.createObjectStore(STORE);
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    async function persist() {
        try {
            const db = await openDb();
            const tx = db.transaction(STORE, "readwrite");
            if (selection) tx.objectStore(STORE).put(selection, KEY);
            else tx.objectStore(STORE).delete(KEY);
            tx.oncomplete = () => db.close();
            tx.onerror = () => db.close();
        } catch (error) {
            /* kept for this page only */
        }
    }

    /** Ids as a sorted, de-duplicated Uint32Array. */
    function normalise(ids) {
        const out = ids instanceof Uint32Array ? ids.slice() : Uint32Array.from(ids || []);
        out.sort();
        let w = 0;
        for (let i = 0; i < out.length; i += 1) {
            if (out[i] === 0) continue; // 0 is background in every mask
            if (w === 0 || out[i] !== out[w - 1]) out[w++] = out[i];
        }
        return w === out.length ? out : out.slice(0, w);
    }

    function count(sel) {
        if (!sel) return 0;
        return Object.values(sel.projects || {}).reduce((sum, ids) => sum + (ids ? ids.length : 0), 0);
    }

    function announce(source) {
        emit("plexora:cells-selected", {
            rev, source: source !== undefined ? source : (selection?.source || null),
            scope: selection?.scope || [], count: count(selection),
            canUndo: past.length > 0, canRedo: future.length > 0,
        });
    }

    /** Record the selection as it is before a change. */
    function remember() {
        past.push(selection);
        if (past.length > HISTORY) past.shift();
        future = [];
    }

    function step(from, to, source) {
        if (!from.length) return rev;
        to.push(selection);
        selection = from.pop();
        rev += 1;
        announce(source);
        persist();
        return rev;
    }

    const api = {
        MODES,

        /** Resolves once the stored selection (if any) has been read back. */
        ready() {
            if (!loaded) {
                loaded = openDb().then((db) => new Promise((resolve) => {
                    const tx = db.transaction(STORE, "readonly");
                    const request = tx.objectStore(STORE).get(KEY);
                    request.onsuccess = () => {
                        // A selection made on this page before the read landed wins.
                        if (!selection && request.result && request.result.projects) {
                            selection = request.result;
                            rev += 1;
                            announce();
                        }
                        db.close();
                        resolve(selection);
                    };
                    request.onerror = () => { db.close(); resolve(selection); };
                })).catch(() => selection);
            }
            return loaded;
        },

        /** The whole selection, or null. Arrays are the stored ones: read only. */
        current() {
            return selection;
        },

        /** This project's selected mask labels: a Uint32Array, or null when
         *  the selection does not speak for this project at all. */
        get(project) {
            if (!selection || !project || !(selection.scope || []).includes(project)) return null;
            return selection.projects[project] || new Uint32Array(0);
        },

        /** Whether `id` of `project` is selected (binary search). */
        has(project, id) {
            const ids = api.get(project);
            if (!ids || !ids.length) return false;
            let lo = 0;
            let hi = ids.length - 1;
            while (lo <= hi) {
                const mid = (lo + hi) >> 1;
                if (ids[mid] === id) return true;
                if (ids[mid] < id) lo = mid + 1;
                else hi = mid - 1;
            }
            return false;
        },

        count(project) {
            if (project === undefined) return count(selection);
            const ids = api.get(project);
            return ids ? ids.length : 0;
        },

        rev() {
            return rev;
        },

        /**
         * Replace the selection. `projects` maps a project to its ids; `scope`
         * (default: those projects) is every project the selection speaks
         * for, so a lasso on a dataset's UMAP that caught nothing on one image
         * says so for that image rather than leaving an older pick there.
         */
        set(projects, { scope = null, source = null, label = null } = {}) {
            const clean = {};
            for (const [name, ids] of Object.entries(projects || {})) {
                const sorted = normalise(ids);
                if (sorted.length) clean[name] = sorted;
            }
            const names = Array.from(new Set([...(scope || []), ...Object.keys(projects || {})]));
            remember();
            selection = names.length
                ? { projects: clean, scope: names, source, label, at: Date.now() }
                : null;
            rev += 1;
            announce();
            persist();
            return rev;
        },

        clear({ source = null } = {}) {
            if (!selection) return rev;
            remember();
            selection = null;
            rev += 1;
            announce(source);
            persist();
            return rev;
        },

        /** Back to the selection before the last change (a clear included). */
        undo({ source = "undo" } = {}) {
            return step(past, future, source);
        },

        redo({ source = "redo" } = {}) {
            return step(future, past, source);
        },

        canUndo() {
            return past.length > 0;
        },

        canRedo() {
            return future.length > 0;
        },

        mode() {
            return mode;
        },

        setMode(next) {
            if (!MODES.includes(next) || next === mode) return;
            mode = next;
            try {
                global.localStorage?.setItem(MODE_KEY, mode);
            } catch (error) { /* this page only */ }
            emit("plexora:cells-selection-mode", { mode });
        },

        /** The region the pointer rests on, as a hint (null when it leaves). */
        region() {
            return region;
        },

        setRegion(project, ids, name) {
            region = project && ids ? { project, ids: normalise(ids), name: name || "Region" } : null;
            emit("plexora:cells-region", region
                ? { project, name: region.name, count: region.ids.length } : { project: null });
        },

        normalise,
    };

    global.PlexoraCellSelection = api;
    api.ready();
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
