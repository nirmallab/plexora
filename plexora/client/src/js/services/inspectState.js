/**
 * inspectState.js - changing how the 3-D view is cut: preview, commit, undo.
 *
 * The cut lives in the view state (`inspect`; services/viewState.js) and its
 * rules in services/inspectGeometry.js. This is the one way the dock, the
 * handles, the keyboard and the agent change it, so every one of them gets
 * the same four things:
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
 *   ONE TOOL AT A TIME, each with its own axes: a patch that picks the other
 *   tool (`{mode: "crop"}`) leaves the old tool's axes in `axesBy` and brings
 *   the new one's back (`withTool`). `toggle()` turns the tool last chosen
 *   off (the whole stack) and on again with everything it had.
 *
 *   ONE ANSWER for what is drawn: `effective()` is the state with any preview
 *   laid over it, `resolved()` its resolution (inspectGeometry.resolve). The
 *   volume renderer reads it every frame (volumeRenderer.inspectNow).
 *
 *   THE AREA GOES WITH THE CUT: an undo entry holds both (`snapshot`), so a
 *   slide of the area (`slideArea`, the renderer's Shift+drag) undoes in one
 *   step with the cut it carried, and "Crop to area" (`cropToArea`) is one.
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

        /** The stack's extent, inside `area` (the state's by default). */
        dims(area = this.viewState.get()?.volume?.area ?? null) {
            return this.G.dims(this.viewState.info, this.viewState.depth, area);
        }

        spacing() {
            return this.G.spacing(this.viewState.info);
        }

        /** What the cut is made of now: `{inspect, area}`. */
        snapshot(state = this.viewState.get()) {
            return { inspect: copy(state?.inspect), area: copy(state?.volume?.area ?? null) };
        }

        /** The state with a patch laid over it, as `viewState.set` would. */
        _over(state, patch) {
            if (!patch?.inspect) return state;
            return { ...state, inspect: this.G.clean(this.G.merge(state.inspect, patch.inspect),
                                                     this.dims(state.volume?.area ?? null)) };
        }

        /**
         * `patch` as it applies to `state`: when it picks the other tool, it
         * also leaves the tool it replaces its axes (`axesBy`) and takes up
         * the new tool's own -- a Cut on X and a Crop on X, Y and Z do not
         * trade axes. Axes the patch names itself win.
         */
        withTool(patch, state) {
            const next = patch?.inspect?.mode;
            const inspect = state?.inspect;
            if (!inspect || !this.G.TOOLS.includes(next) || next === inspect.tool) return patch;
            return { ...patch, inspect: {
                ...patch.inspect,
                axes: { ...inspect.axesBy[next], ...(patch.inspect.axes || {}) },
                axesBy: { ...(patch.inspect.axesBy || {}), [inspect.tool]: { ...inspect.axes } },
            } };
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
            return this.G.resolve(state.inspect, this.dims());
        }

        /** "crop X 100–600 µm" for what is drawn now ("" when whole). */
        describe() {
            const state = this.effective();
            if (!state?.inspect) return "";
            return this.G.describe(state.inspect, this.dims(), this.spacing());
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
         * Change the cut: `patch` = `{inspect}`. `save: false` for a change
         * nobody chose (a capture put back, an agent's replay);
         * `history: false` to leave the history alone; `gesture` to fold
         * repeated edits of one control into one undo entry; `extra`, more
         * of the view state changed in the same step (`{volume: {area}}`:
         * the cut then says where it is, and is not moved with the area);
         * `exact`, a whole cut put back as it was (Back,
         * services/viewHistory.js): no tool's axes traded (`withTool`).
         */
        set(patch, { save = true, history = true, gesture = null, extra = null, exact = false } = {}) {
            if (!this.enabled || !patch?.inspect) return this.viewState.get();
            const applied = exact ? patch : this.withTool(patch, this.viewState.get());
            this.previewPatch = null;
            if (history) this._remember(gesture);
            this._sliding = false;
            const out = this.viewState.set({ ...(extra || {}), inspect: applied.inspect }, { save });
            this._changed(false);
            return out;
        }

        /** Show `patch` over the state in the 3-D view only, until commit or
         *  cancel. Successive previews of one gesture replace each other. */
        preview(patch) {
            if (!this.enabled || !patch?.inspect) return;
            const held = this.previewPatch?.inspect || {};
            this.previewPatch = { inspect: this.G.merge(held, patch.inspect) };
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
         * Move the area the 3-D view draws to `area` (`{x, y, width,
         * height}`), the cut moving with it (viewState.set): the
         * renderer's Shift+drag, and the area's body grip. A slide is one
         * undo entry from its first move to its `final` one, saved once, at
         * the end.
         */
        slideArea(area, { final = true } = {}) {
            if (!this.viewState?.enabled?.()) return this.viewState?.get?.() || null;
            if (!this._sliding) {
                this._last = null;
                this._remember(null);
            }
            this._sliding = !final;
            this.previewPatch = null;
            const out = this.viewState.set({ volume: { area } }, { save: final });
            this._changed(false);
            return out;
        }

        /**
         * Resize the area the 3-D view draws to `area` (an area grip,
         * views/volumeHandles.js). Unlike a slide the cut does not move: it
         * stays where it is in the image, kept inside the new area -- from
         * `inspect`, the cut as the drag began, so a face pulled in and out
         * again gives the cut back. One undo entry from the first move to
         * the `final` one, saved once, at the end.
         */
        resizeArea(area, { final = true, inspect = null } = {}) {
            if (!this.viewState?.enabled?.()) return this.viewState?.get?.() || null;
            if (!this._sliding) {
                this._last = null;
                this._remember(null);
            }
            this._sliding = !final;
            this.previewPatch = null;
            const keep = inspect || this.viewState.get()?.inspect;
            const out = this.viewState.set({ volume: { area }, ...(keep ? { inspect: copy(keep) } : {}) },
                                           { save: final });
            this._changed(false);
            return out;
        }

        /** The crop's x and y ranges (whichever tool is on: a crop is kept
         *  while hidden), each the area's own where the crop is not along
         *  that axis: `{x: [lo, hi], y: [lo, hi]}`. */
        cropXY(state = this.viewState.get()) {
            const inspect = state?.inspect;
            if (!inspect) return null;
            const whole = this.G.whole(this.dims(state.volume?.area ?? null));
            const axes = inspect.tool === "crop" ? inspect.axes : inspect.axesBy?.crop;
            return { x: axes?.x ? inspect.crop.x.slice() : whole.x, y: axes?.y ? inspect.crop.y.slice() : whole.y };
        }

        /** Whether the crop is narrower than the area (or the image) in x
         *  or y: what "Crop to area" would tighten. */
        cropNarrower(state = this.viewState.get()) {
            const inspect = state?.inspect;
            if (!inspect || inspect.mode !== "crop") return false;
            const xy = this.cropXY(state);
            const whole = this.G.whole(this.dims(state.volume?.area ?? null));
            const narrower = (axis) => xy[axis][0] > whole[axis][0] + 0.5 || xy[axis][1] < whole[axis][1] - 0.5;
            return narrower("x") || narrower("y");
        }

        /**
         * The area becomes the crop's x and y: the stack is fetched again at
         * the finest level that box allows, and the camera frames it (the
         * box's longest side is always 1). One undo entry, area and crop.
         * A view of its own: Back returns to the one it was made from
         * (services/viewHistory.js).
         */
        cropToArea() {
            if (!this.enabled) return this.viewState.get();
            const xy = this.cropXY();
            if (!xy) return this.viewState.get();
            const area = this.G.cleanArea({ x: xy.x[0], y: xy.y[0], width: xy.x[1] - xy.x[0],
                                             height: xy.y[1] - xy.y[0] }, this.dims(null));
            if (!area) return this.viewState.get();
            global.PlexoraViewHistory?.push?.();
            const crop = { x: [area.x, area.x + area.width], y: [area.y, area.y + area.height] };
            return this.set({ inspect: { crop } }, { extra: { volume: { area } } });
        }

        /** Cut with `tool` ("cutaway" or "crop"), as it was last left. */
        pick(tool) {
            if (!this.G.TOOLS.includes(tool)) return this.viewState.get();
            return this.set({ inspect: { mode: tool } });
        }

        /** The tool last chosen off (the whole stack), or on again with its
         *  axes and its cuts as they were. */
        toggle() {
            const inspect = this.viewState.get()?.inspect;
            if (!this.enabled || !inspect) return this.viewState.get();
            return this.set({ inspect: { mode: inspect.mode === "full" ? inspect.tool : "full" } });
        }

        /**
         * Back to defaults: `"axis"` (one axis of the current tool, `axis`),
         * `"mode"` (every axis of the current tool, or of the tool last chosen
         * while the cut is hidden, which it shows again; the axes chosen stay) or
         * `"full"` (the whole stack, settings kept).
         */
        reset(scope = "full", { axis = null } = {}) {
            if (!this.enabled) return this.viewState.get();
            const inspect = this.viewState.get().inspect;
            if (scope === "full") return this.set({ inspect: { mode: "full" } });
            const base = this.G.defaults(this.dims());
            const axes = scope === "axis" && axis ? [axis] : this.G.AXES;
            // A hidden cut (the whole stack shown) is the tool last chosen's,
            // and its reset shows it again.
            const tool = inspect.mode === "full" ? inspect.tool : inspect.mode;
            if (tool === "crop") {
                return this.set({ inspect: { mode: tool,
                                             crop: Object.fromEntries(axes.map((a) => [a, base.crop[a]])) } });
            }
            if (tool === "cutaway") {
                return this.set({ inspect: { mode: tool,
                                             cutaway: Object.fromEntries(axes.map((a) => [a, base.cutaway[a]])) } });
            }
            return this.viewState.get();
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
            this._sliding = false;
            // The area and the cut in one step: the cut says where it is, so
            // it is not moved with the area (viewState.set).
            const area = entry.area === undefined ? {} : { volume: { area: entry.area } };
            this.viewState.set({ ...area, inspect: entry.inspect });
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
