/**
 * viewHistory.js - Back: the views a derived view was opened from.
 *
 * A square placed on a plane opens its own 3-D view (views/areaTool.js), and
 * Crop to area opens a tighter one (services/inspectState.js `cropToArea`).
 * Each of those first `push()`es where the user was, so `back()` -- the Back
 * pill, or B (views/backButton.js) -- returns there in one step.
 *
 * SPATIAL ONLY. An entry is where the view was and what it was cut to: the
 * mode, the plane, the projection's range, the frame, the area, the cut, the
 * 3-D camera and the 2-D viewport. Never the rendering -- channels, colours,
 * contrast, how voxels are composited, what is shown -- so a colour changed
 * in the child view is still that colour after Back. Bookmarks (persistent,
 * rendering included) are services/bookmarks.js; neither knows the other.
 *
 * THE TRAIL ENDS when the view goes back to 2-D any other way (the mode
 * buttons, V): there is no derived view left to come back from, and B is the
 * dataset chip's again (views/datasetNav.js). A new page is a new trail.
 *
 * Back moves smoothly where the view allows: the 3-D camera turns there
 * (volumeRenderer.turnTo, instant under reduced motion) and the 2-D view
 * glides (viewerScene.glideTo). Nothing is fetched again that is cached: 2-D
 * tiles are the image viewer's, a 3-D area stands in from the last drawn.
 *
 * Classic script, `window.PlexoraViewHistory`.
 */
(function (global) {
    "use strict";

    //: How many views back are kept; the oldest go first.
    const LIMIT = 20;
    //: How long the 2-D view glides back (ms); 0 under reduced motion.
    const GLIDE_MS = 320;

    function copy(value) {
        return value === undefined || value === null ? value : JSON.parse(JSON.stringify(value));
    }

    function smooth() {
        const prefs = global.PlexoraViewerPrefs;
        return prefs?.smoothMotion ? prefs.smoothMotion() : true;
    }

    class ViewHistory {
        constructor({ viewState, cutter = null, volume = null, imageViewer = null, config = null } = {}) {
            this.viewState = viewState;
            this.cutter = cutter;
            this.volume = volume;
            this.imageViewer = imageViewer;
            this.config = config;
            this.stack = [];
            this._restoring = false;
            this._listeners = new Set();
            this._off = [];
        }

        attach() {
            const vs = this.viewState;
            if (vs?.subscribe) {
                this._off.push(vs.subscribe((state) => {
                    if (this._restoring || !this.stack.length) return;
                    if (state?.mode !== "volume") this.clear();
                }));
            }
            const hidden = () => this.clear();
            global.addEventListener?.("plexora:viewer-hidden", hidden);
            this._off.push(() => global.removeEventListener?.("plexora:viewer-hidden", hidden));
            return this;
        }

        destroy() {
            for (const off of this._off.splice(0)) off?.();
            this.stack = [];
        }

        /** Where the view is now, as `back()` puts it back. */
        entry() {
            const state = this.viewState.get();
            if (!state) return null;
            const out = {
                mode: state.mode, z: state.z, t: state.t, slab: copy(state.slab),
                area: copy(state.volume?.area ?? null), inspect: copy(state.inspect ?? null),
                camera: null, view: null,
            };
            const renderer = this.volume?.renderer;
            if (state.mode === "volume" && renderer?.cameraState) out.camera = renderer.cameraState();
            const scene = global.PlexoraViewerScene;
            if (state.mode !== "volume" && scene?.viewOf && this.imageViewer) {
                out.view = scene.viewOf(this.imageViewer);
            }
            return out;
        }

        /** Remember the view as it is now, before a derived one opens. */
        push() {
            const entry = this.entry();
            if (!entry) return false;
            this.stack.push(entry);
            while (this.stack.length > LIMIT) this.stack.shift();
            this._changed();
            return true;
        }

        canGoBack() {
            return this.stack.length > 0;
        }

        get depth() {
            return this.stack.length;
        }

        clear() {
            if (!this.stack.length) return;
            this.stack = [];
            this._changed();
        }

        /**
         * Go back to the last view pushed: the mode, the plane, the area and
         * the cut in one change of the view state (one undo entry of the
         * cut, as any other change of it), then the camera or the viewport.
         * False with nowhere to go back to.
         */
        back() {
            const entry = this.stack.pop();
            if (!entry) return false;
            const vs = this.viewState;
            const extra = { mode: entry.mode, z: entry.z, t: entry.t, slab: entry.slab,
                            volume: { area: entry.area } };
            this._restoring = true;
            try {
                if (entry.inspect && this.cutter?.enabled) {
                    this.cutter.set({ inspect: entry.inspect }, { extra, exact: true });
                } else {
                    vs.set(entry.inspect ? { ...extra, inspect: entry.inspect } : extra);
                }
            } finally {
                this._restoring = false;
            }
            if (entry.mode === "volume" && entry.camera) {
                const renderer = this.volume?.renderer;
                const [panX, panY] = entry.camera.pan || [0, 0];
                renderer?.turnTo?.({ q: entry.camera.q, dist: entry.camera.dist, panX, panY });
            } else if (entry.view && this.imageViewer) {
                global.PlexoraViewerScene?.glideTo?.(this.imageViewer, entry.view,
                                                     { ms: smooth() ? GLIDE_MS : 0 });
            }
            this._changed();
            return true;
        }

        /** Call `fn({depth})` on every change; returns the undo. */
        onChange(fn) {
            this._listeners.add(fn);
            return () => this._listeners.delete(fn);
        }

        _changed() {
            const detail = { depth: this.stack.length };
            for (const fn of [...this._listeners]) {
                try {
                    fn(detail);
                } catch (error) {
                    console.error("viewHistory: a listener failed", error);
                }
            }
            try {
                global.dispatchEvent?.(new CustomEvent("plexora:view-history", { detail }));
            } catch (error) {
                // No CustomEvent in a bare runtime.
            }
        }
    }

    function attach(options = {}) {
        if (!options.viewState?.enabled?.()) return null;
        const history = new ViewHistory(options).attach();
        api.instance = history;
        return history;
    }

    const api = {
        LIMIT,
        ViewHistory,
        attach,
        instance: null,
        /** Remember where the view is, before a derived view opens. */
        push: () => Boolean(api.instance?.push()),
        canGoBack: () => Boolean(api.instance?.canGoBack()),
        back: () => Boolean(api.instance?.back()),
        clear: () => api.instance?.clear(),
    };
    global.PlexoraViewHistory = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
