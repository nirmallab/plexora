/**
 * savedAreas.js - areas of a stack kept to come back to.
 *
 * In 3-D, "Save area" keeps what is on screen -- the area drawn (`volume.area`,
 * the Area tool's; the whole image when there is none), the cut inside it
 * (`inspect`) and the camera -- under a name, in the view state's
 * `volume.saved` (saved with the project's view, at most
 * inspectGeometry.SAVED_MAX; services/inspectGeometry.js cleans it). A click
 * on one restores all three in one step, one undo entry with the cut
 * (services/inspectState.js).
 *
 * Each has a thumbnail, taken from the 3-D view when it is saved. Thumbnails
 * are this page's only (a session Map): never in the view state, which is
 * written on every change and would carry their bytes each time.
 *
 * Classic script, `window.PlexoraSavedAreas`.
 */
(function (global) {
    "use strict";

    const THUMB = { width: 160, height: 120 };

    function G() {
        return global.PlexoraInspectGeometry || null;
    }

    function copy(value) {
        return value === undefined || value === null ? value : JSON.parse(JSON.stringify(value));
    }

    function newId() {
        const random = Math.random().toString(36).slice(2, 8);
        return `a${Date.now().toString(36)}${random}`.slice(0, 32);
    }

    class SavedAreas {
        constructor({ viewState, volume = null, cutter = null } = {}) {
            this.viewState = viewState;
            this.volume = volume;
            this.cutter = cutter;
            //: id -> a JPEG data URL, this page only.
            this.thumbs = new Map();
        }

        get renderer() {
            return this.volume?.renderer || null;
        }

        /** The saved areas, oldest first: `[{id, name, area, inspect, camera}]`. */
        list() {
            return copy(this.viewState.get()?.volume?.saved || []);
        }

        get(id) {
            return this.list().find((entry) => entry.id === id) || null;
        }

        thumb(id) {
            return this.thumbs.get(id) || null;
        }

        _write(saved) {
            this.viewState.set({ volume: { saved } });
            try {
                global.dispatchEvent?.(new CustomEvent("plexora:saved-areas", { detail: { count: saved.length } }));
            } catch (error) {
                // No CustomEvent in a bare runtime.
            }
        }

        /** Keep what the 3-D view shows now, as `name` ("Area N" when none).
         *  The oldest goes past the limit. Returns the entry. */
        save(name = "") {
            const g = G();
            const vs = this.viewState;
            const state = vs.get();
            if (!g || !state) return null;
            const d = g.dims(vs.info, vs.depth);
            const area = g.cleanArea(state.volume?.area, d)
                || { x: 0, y: 0, width: d.width, height: d.height };
            const saved = this.list();
            const entry = {
                id: newId(),
                name: String(name || `Area ${saved.length + 1}`).slice(0, 64),
                area,
                inspect: copy(state.inspect || null),
                camera: this.renderer?.cameraState?.() || copy(state.volume?.camera?.q ? state.volume.camera : null),
            };
            saved.push(entry);
            while (saved.length > g.SAVED_MAX) {
                const gone = saved.shift();
                this.thumbs.delete(gone.id);
            }
            this.takeThumb(entry.id);
            this._write(saved);
            return entry;
        }

        /** The 3-D view now, small, as the thumbnail of `id`. */
        takeThumb(id) {
            const renderer = this.renderer;
            if (!renderer?.snapshot) return null;
            try {
                const canvas = renderer.snapshot(THUMB);
                const url = canvas?.toDataURL?.("image/jpeg", 0.8) || null;
                if (url) this.thumbs.set(id, url);
                return url;
            } catch (error) {
                console.error("savedAreas: no thumbnail", error);
                return null;
            }
        }

        /** Back to saved area `id`: its area, its cut and its camera, in 3-D,
         *  as one change (one undo entry). */
        restore(id) {
            const entry = this.get(id);
            if (!entry) return false;
            const vs = this.viewState;
            const volume = { area: entry.area, ...(entry.camera ? { camera: entry.camera } : {}) };
            const cutter = this.cutter;
            if (cutter?.enabled && entry.inspect) {
                cutter.set({ inspect: entry.inspect }, { extra: { mode: "volume", volume } });
            } else {
                vs.set({ mode: "volume", volume, ...(entry.inspect ? { inspect: entry.inspect } : {}) });
            }
            if (entry.camera) {
                const put = () => this.renderer?.restoreCamera?.(entry.camera);
                if (this.renderer) put();
                else global.addEventListener?.("plexora:volume-mounted", put, { once: true });
            }
            return true;
        }

        remove(id) {
            const saved = this.list();
            const kept = saved.filter((entry) => entry.id !== id);
            if (kept.length === saved.length) return false;
            this.thumbs.delete(id);
            this._write(kept);
            return true;
        }

        rename(id, name) {
            const saved = this.list();
            const entry = saved.find((item) => item.id === id);
            const text = String(name || "").trim().slice(0, 64);
            if (!entry || !text || entry.name === text) return false;
            entry.name = text;
            this._write(saved);
            return true;
        }
    }

    function attach(options = {}) {
        if (!options.viewState?.enabled?.()) return null;
        api.instance = new SavedAreas(options);
        return api.instance;
    }

    const api = { SavedAreas, attach, instance: null, THUMB };
    global.PlexoraSavedAreas = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
