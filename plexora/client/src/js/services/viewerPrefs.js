/**
 * viewerPrefs.js - how this user likes the viewer, for every image.
 *
 * Matters of taste rather than of the image, so they are stored once for
 * the user (`/viewer_prefs`, in the settings file) and not per project the
 * way the plane and the projection are (viewState.js):
 *
 *     sections  the XZ and YZ panes' sizes, as a share of the plane's height
 *               and width; null = sized to fit, up to the default
 *     motion    "on" | "off": planes fade into each other and the 3-D view
 *               coasts after a drag, or neither
 *     grid      the 3-D view's reference grid (views/volumeRenderer.js):
 *               {on, faces: "walls" | "floor", step: "auto" | a number in
 *               x and y's units, opacity: 0.05..1, labels}
 *     rotate    how Play turns the stack in 3-D (views/volumeRenderer.js
 *               setAutoRotate): {speed: degrees a second, ROTATE_SPEED;
 *               axis: "screen" | "x" | "y" | "z"; direction: 1 | -1}. Whether
 *               it is turning is never stored: a page never opens mid-motion.
 *
 * `smoothMotion()` is what every animation asks. It is false when either the
 * user turned motion off here or the system asks for reduced motion
 * (`prefers-reduced-motion`), and the root element carries
 * `plx-reduced-motion` while it is false, so CSS transitions stop with it.
 *
 * Changes are announced as `plexora:viewer-prefs-changed` and saved debounced,
 * so a pane dragged through a hundred sizes is one write.
 *
 * Classic script, exposed as `window.PlexoraViewerPrefs`.
 */
(function (global) {
    "use strict";

    const SAVE_DELAY_MS = 400;
    const GRID = { on: false, faces: "walls", step: "auto", opacity: 0.18, labels: true };
    const ROTATE = { speed: 30, axis: "screen", direction: 1 };
    //: Degrees a second (server/routes/volume_routes.py ROTATE_SPEED).
    const ROTATE_SPEED = [5, 90];
    const ROTATE_AXES = ["screen", "x", "y", "z"];
    const DEFAULTS = { sections: { xz: null, yz: null }, motion: "on", grid: GRID,
                       rotate: ROTATE };

    /** A grid preference, repaired over `base`: what does not parse keeps
     *  the value it had (the server's `normalize_grid` keeps the same bounds). */
    function mergeGrid(base, patch) {
        const out = { ...base };
        if (!patch || typeof patch !== "object") return out;
        if (typeof patch.on === "boolean") out.on = patch.on;
        if (patch.faces === "walls" || patch.faces === "floor") out.faces = patch.faces;
        if (patch.step === "auto") out.step = "auto";
        else if (typeof patch.step === "number" && Number.isFinite(patch.step) && patch.step > 0) {
            out.step = Math.min(1e6, Math.max(1e-3, patch.step));
        }
        if (typeof patch.opacity === "number" && Number.isFinite(patch.opacity)) {
            out.opacity = Math.min(1, Math.max(0.05, patch.opacity));
        }
        if (typeof patch.labels === "boolean") out.labels = patch.labels;
        return out;
    }

    /** A rotate preference, repaired over `base`: what does not parse keeps
     *  the value it had (the server's `normalize_rotate` keeps the same bounds). */
    function mergeRotate(base, patch) {
        const out = { ...base };
        if (!patch || typeof patch !== "object") return out;
        if (typeof patch.speed === "number" && Number.isFinite(patch.speed)) {
            out.speed = Math.min(ROTATE_SPEED[1], Math.max(ROTATE_SPEED[0], patch.speed));
        }
        if (ROTATE_AXES.includes(patch.axis)) out.axis = patch.axis;
        if (patch.direction === 1 || patch.direction === -1) out.direction = patch.direction;
        return out;
    }

    function url(path) {
        return typeof global.plexoraUrl === "function" ? global.plexoraUrl(path) : `/${path}`;
    }

    function copy(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function systemReducedMotion() {
        try {
            return Boolean(global.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
        } catch (error) {
            return false;
        }
    }

    class ViewerPrefs {
        constructor({ fetchImpl = null, saveDelay = SAVE_DELAY_MS } = {}) {
            this._fetch = fetchImpl || ((...args) => global.fetch(...args));
            this._saveDelay = saveDelay;
            this._saveTimer = null;
            this._pending = {};
            this._loading = null;
            this.prefs = copy(DEFAULTS);
            this._media = null;
        }

        /** Read the stored preferences once. Never rejects: a failure keeps
         *  the defaults, which is how a first run looks anyway. */
        load() {
            if (this._loading) return this._loading;
            this._watchSystem();
            this._loading = (async () => {
                try {
                    const response = await this._fetch(url("viewer_prefs"));
                    if (response.ok) this._merge(await response.json());
                } catch (error) {
                    console.error("viewerPrefs: could not read", error);
                }
                this._paintRoot();
                this._announce();
                return this.get();
            })();
            return this._loading;
        }

        get() {
            return copy(this.prefs);
        }

        /** A pane's saved share, or null when it is sized to fit. */
        sectionShare(axis) {
            const share = this.prefs.sections?.[axis];
            return Number.isFinite(share) ? share : null;
        }

        /** The 3-D view's grid: `{on, faces, step, opacity, labels}`. */
        grid() {
            return { ...this.prefs.grid };
        }

        /** How Play turns the stack in 3-D: `{speed, axis, direction}`. */
        rotate() {
            return { ...this.prefs.rotate };
        }

        /** Whether planes may fade and the 3-D view may coast. */
        smoothMotion() {
            if (this._holds > 0) return false;
            return this.prefs.motion !== "off" && !systemReducedMotion();
        }

        /**
         * Hold motion off for a while without touching the saved preference:
         * `const release = prefs.hold()` ... `release()`. For a caller that
         * drives the viewer frame by frame (Video Studio's export), where a
         * plane fade or an eased zoom between "applied" and "captured" would
         * put a half-finished picture in a frame. Holds count, so two callers
         * can overlap; `set({motion: "off"}, {save: false})` would instead
         * queue the change for the next save and write it to disk.
         */
        hold() {
            this._holds = (this._holds || 0) + 1;
            this._announce();
            let released = false;
            return () => {
                if (released) return;
                released = true;
                this._holds = Math.max(0, (this._holds || 0) - 1);
                this._announce();
            };
        }

        systemReducedMotion() {
            return systemReducedMotion();
        }

        /**
         * Change some preferences: `{sections: {xz: 0.3}}`, `{motion: "off"}`,
         * `{grid: {on: true}}` (merged over the grid as it is).
         * `{rotate: {speed: 45}}` (merged over the rotation as it is).
         * `save: false` while a drag is still moving; the drag's end saves.
         */
        set(patch, { save = true } = {}) {
            if (!patch || typeof patch !== "object") return this.get();
            this._merge(patch);
            const grid = patch.grid || this._pending.grid
                ? { grid: { ...(this._pending.grid || {}), ...(patch.grid || {}) } } : {};
            const rotate = patch.rotate || this._pending.rotate
                ? { rotate: { ...(this._pending.rotate || {}), ...(patch.rotate || {}) } } : {};
            this._pending = {
                ...this._pending, ...patch,
                sections: { ...(this._pending.sections || {}), ...(patch.sections || {}) },
                ...grid,
                ...rotate,
            };
            this._paintRoot();
            this._announce();
            if (save) this._scheduleSave();
            return this.get();
        }

        _merge(patch) {
            const sections = { ...this.prefs.sections };
            for (const axis of ["xz", "yz"]) {
                if (!patch?.sections || !(axis in patch.sections)) continue;
                const share = patch.sections[axis];
                sections[axis] = Number.isFinite(share) ? share : null;
            }
            this.prefs = {
                sections,
                motion: patch?.motion === "off" || patch?.motion === "on"
                    ? patch.motion : this.prefs.motion,
                grid: mergeGrid(this.prefs.grid || GRID, patch?.grid),
                rotate: mergeRotate(this.prefs.rotate || ROTATE, patch?.rotate),
            };
        }

        _paintRoot() {
            global.document?.documentElement?.classList
                ?.toggle("plx-reduced-motion", !this.smoothMotion());
        }

        _announce() {
            try {
                global.dispatchEvent?.(new CustomEvent("plexora:viewer-prefs-changed",
                    { detail: this.get() }));
            } catch (error) {
                // No CustomEvent in a bare test runtime.
            }
        }

        /** The system setting can change while the page is open. */
        _watchSystem() {
            if (this._media) return;
            try {
                this._media = global.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
                this._media?.addEventListener?.("change", () => {
                    this._paintRoot();
                    this._announce();
                });
            } catch (error) {
                this._media = null;
            }
        }

        _scheduleSave() {
            if (this._saveTimer !== null) clearTimeout(this._saveTimer);
            this._saveTimer = setTimeout(() => {
                this._saveTimer = null;
                this.flush();
            }, this._saveDelay);
        }

        /** Write what changed since the last save, now. */
        flush() {
            if (this._saveTimer !== null) {
                clearTimeout(this._saveTimer);
                this._saveTimer = null;
            }
            const body = this._pending;
            this._pending = {};
            if (!Object.keys(body).length) return Promise.resolve();
            return Promise.resolve(this._fetch(url("viewer_prefs"), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            })).catch((error) => {
                // Never fatal: the layout on screen is right; only the next
                // session will not open with it.
                console.error("viewerPrefs: could not save", error);
            });
        }
    }

    ViewerPrefs.DEFAULTS = DEFAULTS;
    global.PlexoraViewerPrefsClass = ViewerPrefs;
    global.PlexoraViewerPrefs = new ViewerPrefs();
    if (typeof module !== "undefined" && module.exports) {
        module.exports = ViewerPrefs;
    }
})(typeof window !== "undefined" ? window : globalThis);
