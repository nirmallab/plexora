/**
 * viewState.js - where the viewer is in a volume: which plane, which
 * projection, which frame.
 *
 * ONE STATE, owned here. The depth strip (views/depthStrip.js), the Depth
 * card (views/depthTools.js), the keyboard, the agent bridge and a launch
 * link all write it through `set`; the viewer, the badge and every plugin
 * read it. Nothing else holds a copy.
 *
 *     info  = what the image IS: depth, frames, spacing, levels (from
 *             /volume/<ds>/info, read once at boot)
 *     state = { t, z, mode: "plane"|"slab", slab: {z0, z1, op, q, thickness,
 *               follow}, section: {x, y}, layout, aux, volume }
 *
 * A PLANAR IMAGE HAS NO VIEW STATE. `enabled()` is false, `spec()` is "", no
 * DOM is built, no key is bound and no tile address changes -- which is what
 * keeps every 2-D project exactly as it was.
 *
 * HOW A CHANGE REACHES THE PICTURE. `set` updates the state and announces it
 * at once (`plexora:view-requested`), so the readout and the badge move under
 * the user's hand. The tiles follow through `applier` -- the ViewerManager's
 * `setView`, which rebuilds every channel at the new plane and hands over
 * only once it can draw. At most ONE swap is in flight; requests made while it
 * runs collapse into the newest, which is applied when it lands. A scrub
 * through forty planes is therefore a few swaps, and the picture is never a
 * plane the user has already left behind for long.
 *
 * Saved to `/view_state/<datasource>`, debounced, like the view transform.
 * URL `?z=`, `?t=` and `?view=` win over what was saved, and what was saved
 * wins over the default (the middle plane).
 *
 * Classic script, exposed as `window.PlexoraViewState`.
 */
(function (global) {
    "use strict";

    const SAVE_DELAY_MS = 400;
    const MODES = ["plane", "slab"];
    const OPS = ["max", "mean", "min", "std", "med", "pct"];
    const OP_LABELS = { max: "MIP", mean: "Mean", min: "Min", std: "Std. dev.",
                        med: "Median", pct: "Percentile" };

    function int(value, fallback) {
        const number = Math.round(Number(value));
        return Number.isFinite(number) ? number : fallback;
    }

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    function url(path) {
        return typeof global.plexoraUrl === "function" ? global.plexoraUrl(path) : `/${path}`;
    }

    /** A view token's parts, the same grammar as server/volume/viewspec.py. */
    function parseToken(token) {
        const text = String(token || "").trim();
        const match = /^(?:t(\d+))?(?:z(\d+)-(\d+)(max|min|mean|med|std|p\d+(?:\.\d+)?)|z(\d+))?$/.exec(text);
        if (!match || !text) return null;
        const out = { t: int(match[1], 0) };
        if (match[5] !== undefined) {
            out.mode = "plane";
            out.z = int(match[5], 0);
        } else if (match[2] !== undefined) {
            out.mode = "slab";
            let z0 = int(match[2], 0);
            let z1 = int(match[3], 0);
            if (z1 < z0) [z0, z1] = [z1, z0];
            const op = match[4];
            out.slab = op.startsWith("p") && op !== "pct"
                ? { z0, z1, op: "pct", q: Number(op.slice(1)) }
                : { z0, z1, op };
        }
        return out;
    }

    class ViewState {
        constructor(datasource, { fetchImpl = null, saveDelay = SAVE_DELAY_MS } = {}) {
            this.datasource = datasource;
            this.info = { volumetric: false, depth: 1, frames: 1 };
            this.state = null;
            this.applier = null;
            this._fetch = fetchImpl || ((...args) => global.fetch(...args));
            this._saveDelay = saveDelay;
            this._saveTimer = null;
            this._subscribers = new Set();
            this._applying = null;
            this._applied = "";
            this._pending = null;
            this._frame = 0;
            this._windowClass = "volume";
        }

        /** Read the image's extent and the saved position. Never rejects: a
         *  failure reads as a planar image, which is the safe answer. */
        async load({ search = global.location?.search || "" } = {}) {
            if (!this.datasource) return this;
            const ds = encodeURIComponent(this.datasource);
            const [info, saved] = await Promise.all([
                this._json(url(`volume/${ds}/info`)),
                this._json(url(`view_state/${ds}`)),
            ]);
            if (info && info.volumetric) this.info = info;
            if (!this.enabled()) return this;
            this.state = this._clean({ ...this._defaults(), ...(saved || {}) });
            this._applyUrl(search);
            this._applied = this.spec();
            this._windowClass = this.windowClass();
            return this;
        }

        async _json(address) {
            try {
                const response = await this._fetch(address);
                return response.ok ? await response.json() : null;
            } catch (error) {
                console.error("viewState: could not read", address, error);
                return null;
            }
        }

        enabled() {
            return Boolean(this.info?.volumetric)
                && (this.depth > 1 || this.frames > 1);
        }

        get depth() { return Math.max(1, int(this.info?.depth, 1)); }
        get frames() { return Math.max(1, int(this.info?.frames, 1)); }

        /** The z step in its unit, when the image states or was given one. */
        zStep() {
            const z = this.info?.spacing?.z;
            if (!z || !(Number(z.value) > 0)) return null;
            return { value: Number(z.value), unit: z.unit || "µm", source: z.source };
        }

        _defaults() {
            const depth = this.depth;
            const stated = /^(?:t\d+)?z(\d+)$/.exec(String(this.info?.default_view || ""));
            const z = stated ? int(stated[1], 0) : Math.floor(depth / 2);
            const thickness = Math.min(depth, Math.max(1, Math.round(depth / 4)) | 1);
            const half = Math.floor((thickness - 1) / 2);
            return {
                t: 0,
                z: clamp(Number.isFinite(z) ? z : Math.floor(depth / 2), 0, depth - 1),
                mode: "plane",
                slab: { z0: clamp(z - half, 0, depth - 1),
                        z1: clamp(z - half + thickness - 1, 0, depth - 1),
                        op: "max", thickness, follow: true },
                section: { x: Math.floor((this.info?.width || 0) / 2),
                           y: Math.floor((this.info?.height || 0) / 2) },
                layout: "single",
                aux: null,
                volume: { composite: "mip", opacity: {}, camera: {}, quality: "auto" },
            };
        }

        /** The state, repaired: every index inside the stack, the slab ordered,
         *  `follow` recentring the slab on z. */
        _clean(raw) {
            const top = this.depth - 1;
            const defaults = this._defaults();
            const out = { ...defaults, ...raw };
            out.t = clamp(int(out.t, 0), 0, this.frames - 1);
            out.z = clamp(int(out.z, defaults.z), 0, top);
            out.mode = MODES.includes(out.mode) ? out.mode : "plane";
            const slab = { ...defaults.slab, ...(raw.slab || {}) };
            slab.op = OPS.includes(slab.op) ? slab.op : "max";
            if (slab.op === "pct") {
                const q = Number(slab.q);
                slab.q = Number.isFinite(q) ? clamp(q, 0, 100) : 95;
            } else {
                delete slab.q;
            }
            let z0 = clamp(int(slab.z0, 0), 0, top);
            let z1 = clamp(int(slab.z1, top), 0, top);
            if (z1 < z0) [z0, z1] = [z1, z0];
            slab.thickness = clamp(int(slab.thickness, z1 - z0 + 1), 1, this.depth);
            slab.follow = slab.follow !== false;
            if (slab.follow && out.mode === "slab" && raw.__recentre) {
                const half = Math.floor((slab.thickness - 1) / 2);
                z0 = clamp(out.z - half, 0, Math.max(0, top - slab.thickness + 1));
                z1 = clamp(z0 + slab.thickness - 1, 0, top);
            }
            slab.z0 = z0;
            slab.z1 = z1;
            out.slab = slab;
            delete out.__recentre;
            return out;
        }

        _applyUrl(search) {
            let params;
            try {
                params = new URLSearchParams(search || "");
            } catch (error) {
                return;
            }
            const patch = {};
            const view = parseToken(params.get("view"));
            if (view) Object.assign(patch, view);
            if (params.has("z")) {
                patch.z = int(params.get("z"), this.state.z);
                if (!view) patch.mode = "plane";
            }
            if (params.has("t")) patch.t = int(params.get("t"), this.state.t);
            if (Object.keys(patch).length) {
                this.state = this._clean({ ...this.state, ...patch,
                                           slab: { ...this.state.slab, ...(patch.slab || {}) } });
            }
        }

        get() {
            return this.state ? JSON.parse(JSON.stringify(this.state)) : null;
        }

        /** The token the main pane's tiles are fetched at ("" when planar). */
        spec(state = this.state) {
            if (!this.enabled() || !state) return "";
            const t = this.frames > 1 && state.t ? `t${state.t}` : "";
            if (state.mode === "slab" && this.depth > 1) {
                const { z0, z1, op, q } = state.slab;
                const opToken = op === "pct" ? `p${Number(q).toString()}` : op;
                return `${t}z${z0}-${z1}${opToken}`;
            }
            return this.depth > 1 ? `${t}z${state.z}` : (t || "t0");
        }

        /** "volume" or "std": which quantization window the tiles use. */
        windowClass(state = this.state) {
            return state && state.mode === "slab" && state.slab.op === "std" ? "std" : "volume";
        }

        /** What the badge and every capture print, e.g. "z 12 / 40 · 24.0 µm". */
        label(state = this.state) {
            if (!this.enabled() || !state) return "";
            const step = this.zStep();
            const prefix = this.frames > 1
                ? `t ${state.t + 1}/${this.frames}${this._frameTime(state.t)} · ` : "";
            if (state.mode === "slab") {
                const { z0, z1, op, q } = state.slab;
                const name = op === "pct" ? `${q}th pct.` : OP_LABELS[op] || op;
                const planes = z1 - z0 + 1;
                const thick = step ? ` · ${this._length((planes) * step.value, step.unit)}` : "";
                return `${prefix}${name} z ${z0}–${z1} · ${planes} plane${planes === 1 ? "" : "s"}${thick}`;
            }
            if (this.depth <= 1) return prefix.replace(/ · $/, "");
            const where = step ? ` · ${this._length(state.z * step.value, step.unit)}` : "";
            return `${prefix}z ${state.z}/${this.depth}${where}`;
        }

        _frameTime(t) {
            const step = this.info?.spacing?.t;
            if (!step || !(Number(step.value) > 0)) return "";
            const seconds = t * Number(step.value);
            return ` · ${seconds.toFixed(seconds < 10 ? 1 : 0)} ${step.unit || "s"}`;
        }

        _length(value, unit) {
            return `${value.toFixed(value < 10 ? 1 : 0)} ${unit || "µm"}`;
        }

        /** True when the picture is not a plane the image holds. */
        isDerived(state = this.state) {
            return Boolean(state) && state.mode !== "plane";
        }

        /**
         * Change part of the state. Returns the new state.
         *
         * `save: false` for a change nobody chose (boot, an agent replaying a
         * capture); `recentre` recentres a following slab on the new z.
         */
        set(patch, { save = true } = {}) {
            if (!this.enabled() || !patch) return this.get();
            const before = this.state;
            const merged = {
                ...before, ...patch,
                slab: { ...before.slab, ...(patch.slab || {}) },
                section: { ...before.section, ...(patch.section || {}) },
                volume: { ...before.volume, ...(patch.volume || {}) },
            };
            const zMoved = patch.z !== undefined && patch.slab === undefined;
            if (zMoved && merged.mode === "slab" && merged.slab.follow) merged.__recentre = true;
            if (patch.slab && patch.slab.thickness !== undefined
                    && patch.slab.z0 === undefined && patch.slab.z1 === undefined) {
                merged.__recentre = true;
            }
            this.state = this._clean(merged);
            const fromClass = this._windowClass;
            const toClass = this.windowClass();
            this._announce("plexora:view-requested");
            this._notify();
            if (fromClass !== toClass) {
                this._windowClass = toClass;
                this._event("plexora:view-class-changed", { from: fromClass, to: toClass });
            }
            this._schedule();
            if (save) this._scheduleSave();
            return this.get();
        }

        /** Step z by `dz` planes (the keys, the wheel, the play button). */
        step(dz) {
            if (!this.enabled() || this.depth < 2) return this.get();
            const top = this.depth - 1;
            if (this.state.mode === "slab" && !this.state.slab.follow) {
                const { z0, z1 } = this.state.slab;
                const width = z1 - z0;
                const start = clamp(z0 + dz, 0, top - width);
                return this.set({ slab: { z0: start, z1: start + width } });
            }
            return this.set({ z: clamp(this.state.z + dz, 0, top) });
        }

        stepTime(dt) {
            if (!this.enabled() || this.frames < 2) return this.get();
            return this.set({ t: clamp(this.state.t + dt, 0, this.frames - 1) });
        }

        subscribe(fn) {
            this._subscribers.add(fn);
            return () => this._subscribers.delete(fn);
        }

        _notify() {
            const state = this.get();
            for (const fn of [...this._subscribers]) {
                try {
                    fn(state, this);
                } catch (error) {
                    console.error("viewState: subscriber failed", error);
                }
            }
        }

        _event(name, detail) {
            try {
                global.dispatchEvent?.(new CustomEvent(name, { detail }));
            } catch (error) {
                // No CustomEvent in a bare test runtime; subscribers were told.
            }
        }

        _announce(name) {
            this._event(name, { state: this.get(), spec: this.spec(), label: this.label(),
                                derived: this.isDerived(), windowClass: this.windowClass() });
        }

        /** One swap in flight; the newest request after it. */
        _schedule() {
            if (this._frame) return;
            const raf = global.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
            this._frame = raf(() => {
                this._frame = 0;
                this._pump();
            });
        }

        _pump() {
            const target = this.spec();
            if (this._applying) {
                this._pending = target;
                return;
            }
            if (target === this._applied || typeof this.applier !== "function") {
                if (target === this._applied) this._announce("plexora:view-changed");
                return;
            }
            this._pending = null;
            const run = Promise.resolve()
                .then(() => this.applier(target))
                .catch((error) => console.error("viewState: could not show", target, error))
                .then(() => {
                    this._applied = target;
                    this._applying = null;
                    if (this._pending !== null && this._pending !== this._applied) {
                        this._pending = null;
                        this._pump();
                    } else {
                        this._pending = null;
                        this._announce("plexora:view-changed");
                    }
                });
            this._applying = run;
        }

        /** Settles once the picture shows the current state (tests, agents). */
        settled() {
            const wait = () => (this._applying || this._frame
                ? new Promise((resolve) => setTimeout(resolve, 20)).then(wait)
                : Promise.resolve(this.get()));
            return wait();
        }

        /** The view token the tiles are at right now ("" when planar). */
        appliedSpec() {
            return this._applied;
        }

        /** Save the current state (debounced), for a change made with
         *  `save: false` that turned out to be worth keeping. */
        persist() {
            this._scheduleSave();
        }

        _scheduleSave() {
            if (!this.datasource) return;
            if (this._saveTimer !== null) clearTimeout(this._saveTimer);
            this._saveTimer = setTimeout(() => {
                this._saveTimer = null;
                this._save();
            }, this._saveDelay);
        }

        flush() {
            if (this._saveTimer === null) return Promise.resolve();
            clearTimeout(this._saveTimer);
            this._saveTimer = null;
            return this._save();
        }

        _save() {
            const address = url(`view_state/${encodeURIComponent(this.datasource)}`);
            return Promise.resolve(this._fetch(address, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(this.state),
            })).catch((error) => {
                // Never fatal: the plane is on screen; only the next load will
                // not reopen on it.
                console.error("viewState: could not save", error);
            });
        }
    }

    ViewState.parseToken = parseToken;
    ViewState.OP_LABELS = OP_LABELS;
    global.PlexoraViewState = ViewState;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = ViewState;
    }
})(typeof window !== "undefined" ? window : globalThis);
