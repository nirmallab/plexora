/**
 * inspectState.js - changing how the 3-D view is cut: preview, commit, undo.
 *
 * The cut lives in the view state (`inspect`, `z`, `section`;
 * services/viewState.js) and its rules in services/inspectGeometry.js. This
 * is the one way the dock, the depth strip, the handles, the keyboard and the
 * agent change it, so every one of them gets the same three things:
 *
 *   PREVIEW while a hand is on a control: `preview(patch)` lays the patch
 *   over the state for the 3-D view only -- it redraws from uniforms in the
 *   next frame, nothing is saved, no revision runs -- and says so
 *   (`plexora:inspect-preview`), so a readout can follow the hand.
 *   `commit()` writes what was previewed as one change; `cancel()` drops it.
 *
 *   ONE UNDO ENTRY PER GESTURE: `set()` and `commit()` push what the cut was
 *   before onto a history of HISTORY entries; edits within COALESCE_MS of each
 *   other with the same `gesture` key fold into one. `undo()` / `redo()`.
 *
 *   ONE ANSWER for what is drawn: `effective()` is the state with any preview
 *   laid over it, `resolved()` its resolution (inspectGeometry.resolve). The
 *   volume renderer reads it every frame (volumeRenderer.inspectNow).
 *
 * Section and slab positions are the view's own `z` and `section.x/y`: a
 * patch may carry them (`{z: 12}`, `{section: {x: 400}}`) beside `inspect`.
 *
 * Classic script, `window.PlexoraInspectState` (the class) and, once the
 * viewer has a z-stack, `window.PlexoraInspect` (main.js).
 */
(function (global) {
    "use strict";

    const HISTORY = 50;
    const COALESCE_MS = 600;

    function copy(value) {
        return value === undefined || value === null ? value : JSON.parse(JSON.stringify(value));
    }

    function event(name, detail) {
        try {
            global.dispatchEvent?.(new CustomEvent(name, { detail }));
        } catch (error) {
            // No CustomEvent in a bare runtime.
        }
    }

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }

    class InspectState {
        constructor(viewState, { geometry = null } = {}) {
            this.viewState = viewState;
            this.G = geometry || global.PlexoraInspectGeometry;
            this.previewPatch = null;
            this.past = [];
            this.future = [];
            this._last = null;           // {gesture, at}: the entry edits fold into
            this._listeners = new Set();
        }

        get enabled() {
            return Boolean(this.viewState?.enabled?.() && this.G && this.viewState.get()?.inspect);
        }

        dims() {
            return this.G.dims(this.viewState.info, this.viewState.depth);
        }

        spacing() {
            return this.G.spacing(this.viewState.info);
        }

        /** What the cut is made of now: `{inspect, z, section}`. */
        snapshot(state = this.viewState.get()) {
            return { inspect: copy(state?.inspect), z: state?.z, section: copy(state?.section) };
        }

        /** The state with a patch laid over it, as `viewState.set` would. */
        _over(state, patch) {
            if (!patch) return state;
            const out = { ...state };
            if (patch.inspect) {
                out.inspect = this.G.clean(this.G.merge(state.inspect, patch.inspect), this.dims(), this.spacing());
            }
            if (patch.z !== undefined) out.z = Math.max(0, Math.min(this.viewState.depth - 1, Math.round(patch.z)));
            if (patch.section) out.section = { ...(state.section || {}), ...patch.section };
            return out;
        }

        /** The state the 3-D view draws: the view state, any preview over it. */
        effective() {
            const state = this.viewState.get();
            return state ? this._over(state, this.previewPatch) : null;
        }

        /** What the 3-D view keeps now (inspectGeometry.resolve), or null. */
        resolved() {
            const state = this.effective();
            if (!state?.inspect) return null;
            const d = this.dims();
            return this.G.resolve(state.inspect, this.G.positions(state, d), d);
        }

        /** "crop X 100–600 µm" for what is drawn now ("" when whole). */
        describe() {
            const state = this.effective();
            if (!state?.inspect) return "";
            const d = this.dims();
            return this.G.describe(state.inspect, this.G.positions(state, d), d, this.spacing());
        }

        /** Call `fn(resolved, {preview})` on every change of what is drawn;
         *  returns the undo. */
        onChange(fn) {
            this._listeners.add(fn);
            return () => this._listeners.delete(fn);
        }

        _changed(preview) {
            const resolved = this.resolved();
            for (const fn of [...this._listeners]) {
                try {
                    fn(resolved, { preview });
                } catch (error) {
                    console.error("inspectState: a listener failed", error);
                }
            }
            event(preview ? "plexora:inspect-preview" : "plexora:inspect-changed",
                  { resolved, effective: this.effective() });
        }

        /** Remember the cut as it was before a change; a change of the same
         *  `gesture` soon after the last folds into its entry. */
        _remember(gesture) {
            const at = now();
            const last = this._last;
            if (gesture && last && last.gesture === gesture && at - last.at < COALESCE_MS) {
                last.at = at;
                return;
            }
            this.past.push(this.snapshot());
            if (this.past.length > HISTORY) this.past.shift();
            this.future = [];
            this._last = { gesture: gesture || null, at };
        }

        /**
         * Change the cut: `patch` = `{inspect?, z?, section?}`. `save: false`
         * for a change nobody chose (a capture put back, an agent's replay);
         * `history: false` to leave the history alone; `gesture` to fold
         * repeated edits of one control into one undo entry.
         */
        set(patch, { save = true, history = true, gesture = null } = {}) {
            if (!this.enabled || !patch) return this.viewState.get();
            this.previewPatch = null;
            if (history) this._remember(gesture);
            const out = this.viewState.set(this._patchFor(patch), { save });
            this._changed(false);
            return out;
        }

        /** A view-state patch: `inspect` repaired here, so what is saved is
         *  what is drawn. */
        _patchFor(patch) {
            const out = {};
            if (patch.inspect) out.inspect = patch.inspect;
            if (patch.z !== undefined) out.z = patch.z;
            if (patch.section) out.section = patch.section;
            return out;
        }

        /** Show `patch` over the state in the 3-D view only, until commit or
         *  cancel. Successive previews of one gesture replace each other. */
        preview(patch) {
            if (!this.enabled || !patch) return;
            const held = this.previewPatch || {};
            this.previewPatch = {
                ...(held.inspect || patch.inspect
                    ? { inspect: this.G.merge(held.inspect || {}, patch.inspect || {}) } : {}),
                ...(patch.z !== undefined ? { z: patch.z } : (held.z !== undefined ? { z: held.z } : {})),
                ...(held.section || patch.section
                    ? { section: { ...(held.section || {}), ...(patch.section || {}) } } : {}),
            };
            this._changed(true);
        }

        get previewing() {
            return Boolean(this.previewPatch);
        }

        /** Write what was previewed, as one change (one undo entry). */
        commit({ gesture = null } = {}) {
            const patch = this.previewPatch;
            if (!patch) return this.viewState.get();
            return this.set(patch, { gesture });
        }

        /** Drop the preview: the 3-D view goes back to the state. */
        cancel() {
            if (!this.previewPatch) return;
            this.previewPatch = null;
            this._changed(false);
        }

        /**
         * Back to defaults: `"axis"` (one axis of the current mode, `axis`),
         * `"mode"` (the current mode's settings), `"cut"` (every mode's) or
         * `"full"` (the whole stack, settings kept).
         */
        reset(scope = "full", { axis = null } = {}) {
            if (!this.enabled) return this.viewState.get();
            const state = this.viewState.get();
            const inspect = state.inspect;
            const base = this.G.defaults(this.dims(), this.spacing());
            if (scope === "full") return this.set({ inspect: { mode: "full" } });
            if (scope === "cut") {
                return this.set({ inspect: { ...base, mode: inspect.mode, axes: inspect.axes } });
            }
            const axes = scope === "axis" && axis ? [axis] : this.G.AXES;
            const patch = {};
            if (inspect.mode === "crop") {
                patch.inspect = { crop: Object.fromEntries(axes.map((a) => [a, base.crop[a]])) };
            } else if (inspect.mode === "cutaway") {
                patch.inspect = { cutaway: Object.fromEntries(axes.map((a) => [a, base.cutaway[a]])) };
            } else if (inspect.mode === "slab" || inspect.mode === "section") {
                // A plane back to the middle of the stack (or of the plane).
                const d = this.dims();
                const middle = { x: Math.floor(d.width / 2), y: Math.floor(d.height / 2) };
                const section = {};
                for (const a of axes) if (a !== "z") section[a] = middle[a];
                if (Object.keys(section).length) patch.section = section;
                if (axes.includes("z")) patch.z = Math.floor(d.depth / 2);
                if (inspect.mode === "slab" && scope === "mode") {
                    patch.inspect = { slab: { thickness: base.slab.thickness, composite: base.slab.composite } };
                }
            }
            return this.set(patch);
        }

        canUndo() { return this.past.length > 0; }

        canRedo() { return this.future.length > 0; }

        undo() {
            const entry = this.past.pop();
            if (!entry) return false;
            this.future.push(this.snapshot());
            this._last = null;
            this._apply(entry);
            return true;
        }

        redo() {
            const entry = this.future.pop();
            if (!entry) return false;
            this.past.push(this.snapshot());
            this._last = null;
            this._apply(entry);
            return true;
        }

        _apply(entry) {
            this.previewPatch = null;
            this.viewState.set({ inspect: entry.inspect, z: entry.z, section: entry.section });
            this._changed(false);
        }
    }

    InspectState.HISTORY = HISTORY;
    InspectState.COALESCE_MS = COALESCE_MS;
    global.PlexoraInspectState = InspectState;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = InspectState;
    }
})(typeof window !== "undefined" ? window : globalThis);
