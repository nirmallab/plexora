/**
 * gatingPlanes.js - thresholding a z-stack one plane at a time.
 *
 * In a z-stack the viewer draws only the cells on the plane on screen (a 3-D
 * mask's label tiles are one plane: server/volume/labels.py), so gating looks
 * like gating a 2-D image: step through the stack and judge each plane's
 * cells. Two things are added here, and neither changes what a gate is:
 *
 * - WHAT THE IMAGE SHOWS: the plane alone, or a SLAB around it -- the mean,
 *   max or median of the planes within half a thickness of it (5 um by
 *   default), cut where the stack ends -- for a stain too faint or too uneven
 *   to judge on one optical section. It is the viewer's own projection, kept
 *   centred on the plane as the user steps (viewState's following slab,
 *   `clip`), and the outlines stay the plane's own cells: a projection's
 *   picture is several planes, its outlines are always one plane's.
 * - WHAT THE PLANE'S CELLS ARE: under the histogram, those cells shaded and
 *   counted -- "z 12 . 41 of 312 positive" -- by the gate's own calls.
 *
 * A gate is always applied to each cell's whole-cell value (its mean over
 * every voxel, `objects_v1`), the same on every plane and whatever the image
 * shows: the slab is for looking, never for the call.
 *
 * Shown only for a z-stack with a 3-D mask, and only while a plane or a
 * projection is on screen (not the 3-D view). Core routes only
 * (`volume/<ds>/plane_cells`); the view goes through `ctx.view` /
 * `__plexora.viewState`.
 *
 * Classic script, loaded before gatingSidebarController.js, which builds one.
 */

const GATE_PLANE_CACHE = 48;
//: Planes read ahead of the hand, once it has been still this long.
const GATE_PLANE_PREFETCH_MS = 160;
//: How long a typed thickness waits before it is applied.
const GATE_SLAB_TYPE_MS = 450;
const GATE_SLAB_DEFAULT_UM = 5;
const GATE_SLAB_DEFAULT_PLANES = 5;
//: The panel's aggregations, as the viewer's projection spells them.
const GATE_SLAB_OPS = { mean: "mean", max: "max", median: "med" };
const GATE_VIEW_TITLES = {
    plane: "Show the plane on its own.",
    slab: "Show the planes around this one combined (mean, max or median), centred on it and "
        + "cut where the stack ends. The outlines stay this plane's cells, and gates still use "
        + "each cell's whole-cell value.",
};

class GatePlaneControl {
    constructor(controller) {
        this.controller = controller;
        this.ctx = controller.ctx;
        //: `${column}|${t}|${z}` -> {ids, values, header}.
        this.cache = new Map();
        this.inflight = new Map();
        //: The plane whose cells are counted: {z, t, column}, and its entry.
        this.plane = null;
        this.entry = null;
        this.direction = 1;
        this._typeTimer = 0;
        this._prefetchTimer = 0;
        this._request = 0;
        //: The slab the user last chose, so Plane -> Slab comes back as they
        //: left it, across markers, planes and reloads.
        this.lastSlab = this.loadLastSlab();
        this.root = document.getElementById("gate_planes");
        this.readout = document.getElementById("gate_plane_readout");
    }

    /** A z-stack whose mask is a 3-D label volume: whole cells to gate. */
    static applies(ctx) {
        return Boolean(ctx.view?.enabled && ctx.view.labelsAre3d?.());
    }

    init() {
        if (!this.root || !this.readout) return;
        this.build();
        this.ctx.view.onChange(() => this.follow());
        // The view moves under the hand before the picture does: the controls
        // follow the request, the counts the committed view.
        const onRequested = () => this.paint();
        const onGate = (event) => {
            if ((event.detail || {}).layer === this.controller.layerName()) this.paintReadout();
        };
        window.addEventListener("plexora:view-requested", onRequested);
        window.addEventListener("plexora:cell-gate-changed", onGate);
        this.ctx.onCleanup?.(() => {
            window.removeEventListener("plexora:view-requested", onRequested);
            window.removeEventListener("plexora:cell-gate-changed", onGate);
            clearTimeout(this._typeTimer);
            clearTimeout(this._prefetchTimer);
        });
        this.paint();
        this.follow();
    }

    // -- the view ---------------------------------------------------------------

    get viewState() {
        return window.__plexora?.viewState || null;
    }

    get state() {
        return this.ctx.view?.state || null;
    }

    /** Microns per plane, when the stack states it or somebody typed it. */
    zStepUm() {
        const step = this.viewState?.zStep?.();
        if (!step || !["metadata", "manual"].includes(step.source)) return null;
        return /^(µm|um|micron)/i.test(step.unit || "µm") ? Number(step.value) : null;
    }

    /** Planes each side of the centre for a slab of `thickness` microns,
     *  centre to centre: a plane is in when it is within half the thickness. */
    halfFor({ thickness_um: thickness, planes } = {}) {
        if (planes !== undefined) return Math.floor(Math.max(1, Number(planes)) / 2);
        const step = this.zStepUm();
        if (!(step > 0) || !(Number(thickness) > 0)) return null;
        return Math.floor(Number(thickness) / (2 * step) + 1e-9);
    }

    /** What the slab fields describe: the projection on screen when it is a
     *  following one, else the last slab chosen, else the default. */
    slabChoice() {
        const last = this.lastSlab || {};
        const zKnown = this.zStepUm() > 0;
        const base = zKnown
            ? { thickness_um: Number(last.thickness_um) || GATE_SLAB_DEFAULT_UM }
            : { planes: Number(last.planes) || GATE_SLAB_DEFAULT_PLANES };
        const agg = GATE_SLAB_OPS[last.agg] ? last.agg : "mean";
        const state = this.state;
        if (state?.mode === "slab" && state.slab.follow) {
            const onScreen = Object.keys(GATE_SLAB_OPS).find((key) => GATE_SLAB_OPS[key] === state.slab.op);
            const planes = state.slab.thickness;
            if (2 * (this.halfFor(base) ?? -1) + 1 !== planes) {
                // Set elsewhere (the Depth card): said in its own planes.
                return { planes, agg: onScreen || agg, foreign: !onScreen };
            }
            return { ...base, agg: onScreen || agg, foreign: !onScreen };
        }
        return { ...base, agg };
    }

    /** Show the plane, or the slab around it. */
    show(kind, choice = null) {
        const vs = this.viewState;
        if (!vs?.enabled?.()) return;
        if (kind === "plane") {
            vs.set({ mode: "plane" });
            return;
        }
        const slab = choice || this.slabChoice();
        const half = this.halfFor(slab);
        if (half === null) return;
        this.rememberSlab(slab);
        vs.set({ mode: "slab", slab: { thickness: 2 * half + 1, op: GATE_SLAB_OPS[slab.agg] || "mean",
                                       follow: true, clip: true } });
    }

    rememberSlab(slab) {
        this.lastSlab = { ...(this.lastSlab || {}), agg: slab.agg || "mean",
                          ...(slab.thickness_um !== undefined ? { thickness_um: slab.thickness_um } : {}),
                          ...(slab.planes !== undefined ? { planes: slab.planes } : {}) };
        try {
            window.localStorage?.setItem(this.storageKey(), JSON.stringify(this.lastSlab));
        } catch (error) { /* a remembered thickness is a convenience */ }
    }

    loadLastSlab() {
        try {
            return JSON.parse(window.localStorage?.getItem(this.storageKey()) || "null");
        } catch (error) {
            return null;
        }
    }

    storageKey() {
        return `plexora.gating.slab.${this.ctx.datasource}`;
    }

    // -- the plane on screen ----------------------------------------------------

    /** The plane whose outlines are drawn ({z, t}), or null in 3-D: a
     *  projection's is the plane a following slab follows, else its middle
     *  (viewState.labelSpec). */
    planeOnScreen() {
        const state = this.state;
        if (!state || !this.ctx.view?.enabled || state.mode === "volume") return null;
        const z = state.mode !== "slab" || state.slab.follow
            ? state.z : Math.floor((state.slab.z0 + state.slab.z1) / 2);
        return { z: Number(z) || 0, t: this.ctx.view.frame?.() || 0 };
    }

    column() {
        const marker = this.controller.gateMarker;
        return marker ? this.controller.dataLayer.getFullChannelName(marker) : null;
    }

    keyFor(z, t, column) {
        return `${column}|${t}|${z}`;
    }

    /** The view or the marker moved: read the plane on screen's cells. */
    follow() {
        this.paint();
        const plane = this.planeOnScreen();
        const column = this.column();
        if (!plane || !column) {
            this.plane = null;
            this.entry = null;
            this.paintReadout();
            this.controller.redrawPlaneOverlay?.();
            return;
        }
        const previousZ = this.plane?.z;
        if (previousZ !== undefined && previousZ !== plane.z) this.direction = Math.sign(plane.z - previousZ);
        this.plane = { ...plane, column };
        const request = ++this._request;
        this.read(plane.z, plane.t, column).then((entry) => {
            if (request !== this._request) return;
            this.entry = entry;
            this.paintReadout();
            this.controller.redrawPlaneOverlay?.();
        });
        clearTimeout(this._prefetchTimer);
        this._prefetchTimer = setTimeout(() => this.prefetch(), GATE_PLANE_PREFETCH_MS);
    }

    /** The planes the hand is heading to, read once it is still. */
    prefetch() {
        if (!this.plane) return;
        const depth = this.ctx.view.depth || 1;
        const dir = this.direction || 1;
        for (const dz of [dir, 2 * dir, -dir]) {
            const z = this.plane.z + dz;
            if (z >= 0 && z < depth) this.read(z, this.plane.t, this.plane.column);
        }
    }

    read(z, t, column) {
        const key = this.keyFor(z, t, column);
        if (this.cache.has(key)) {
            const hit = this.cache.get(key);
            this.cache.delete(key);
            this.cache.set(key, hit);
            return Promise.resolve(hit);
        }
        if (this.inflight.has(key)) return this.inflight.get(key);
        const params = new URLSearchParams({ z: String(z), t: String(t), column });
        const address = this.ctx.url(`volume/${encodeURIComponent(this.ctx.datasource)}/plane_cells`);
        const promise = fetch(`${address}?${params}`, { cache: "no-store" })
            .then(async (response) => {
                if (!response.ok) return null;
                const header = JSON.parse(response.headers.get("X-Plane-Cells") || "{}");
                const buffer = await response.arrayBuffer();
                const n = Number(header.n) || 0;
                const entry = { header, ids: new Uint32Array(buffer, 0, n),
                                values: new Float32Array(buffer, 4 * n, n) };
                this.cache.set(key, entry);
                while (this.cache.size > GATE_PLANE_CACHE) this.cache.delete(this.cache.keys().next().value);
                return entry;
            })
            .catch(() => null)
            .finally(() => this.inflight.delete(key));
        this.inflight.set(key, promise);
        return promise;
    }

    /**
     * The plane on screen's cells and how many the gate calls positive -- as
     * the viewer drew them (the layer's own gate table), else by their values
     * under the gate on screen (low < v <= high, labelGpu.evaluateGateMask's
     * rule).
     */
    counts() {
        const entry = this.entry;
        if (!entry) return null;
        const range = this.controller.gatingList.gating_channels[this.plane?.column];
        const [low, high] = range || [-Infinity, Infinity];
        const layer = this.ctx.viewer?.getCellLayer?.(this.controller.layerName());
        const mask = layer?.gateMask instanceof Uint8Array ? layer.gateMask : null;
        let positive = 0;
        for (let i = 0; i < entry.ids.length; i += 1) {
            const id = entry.ids[i];
            const called = mask && id < mask.length ? mask[id] === 1
                : entry.values[i] > low && entry.values[i] <= high;
            if (called) positive += 1;
        }
        return { cells: entry.ids.length, positive };
    }

    /**
     * The plane's cells under the histogram's line, on the same axis, by the
     * values their gate is applied to: shaded, smoothed over neighbouring bins
     * and scaled to its own peak -- a plane holds a few hundred of the stack's
     * cells, and on the stack's scale its shape would be a flat line.
     */
    drawOverlay(group, { xScale, innerHeight, line }) {
        const values = this.entry?.values;
        if (!values || !values.length || !this.plane) return;
        const [lo, hi] = xScale.domain();
        if (!(hi > lo)) return;
        const bins = 50;
        const counts = new Array(bins).fill(0);
        for (const value of values) {
            if (!Number.isFinite(value)) continue;
            const at = Math.floor(((value - lo) / (hi - lo)) * bins);
            counts[Math.max(0, Math.min(bins - 1, at))] += 1;
        }
        const smooth = smoothBins(counts);
        const peak = Math.max(...smooth);
        if (!(peak > 0)) return;
        const width = (hi - lo) / bins;
        const points = smooth.map((count, i) => ({ x: lo + (i + 0.5) * width, y: count / peak }));
        const area = d3.area()
            .x((d) => xScale(d.x))
            .y0(innerHeight)
            .y1((d) => innerHeight - d.y * innerHeight * 0.92)
            .curve(d3.curveMonotoneX);
        group.insert("path", line ? ".sidebar-distribution-line" : null)
            .datum(points)
            .attr("class", "gate-plane-distribution")
            .attr("clip-path", "url(#gate_distribution_clip)")
            .attr("d", area);
    }

    // -- painting -------------------------------------------------------------

    build() {
        this.root.innerHTML = `
            <div class="control-row gate-view-row">
                <span class="control-label gate-row-caption" id="gate_view_label">View</span>
                <div class="gate-view-control" role="radiogroup" aria-labelledby="gate_view_label">
                    <div class="cell-mode-options">
                        <button type="button" class="cell-mode-option" data-gate-view="plane" role="radio" aria-checked="false">Plane</button>
                        <button type="button" class="cell-mode-option" data-gate-view="slab" role="radio" aria-checked="false">Slab</button>
                    </div>
                </div>
            </div>
            <div class="gate-slab-row" hidden>
                <input class="depth-panel-number is-short gate-slab-thickness" type="number"
                       min="0.1" step="0.5" inputmode="decimal" aria-label="Slab thickness">
                <span class="depth-panel-unit gate-slab-unit">µm</span>
                <select class="sidebar-select depth-panel-select gate-slab-agg"
                        aria-label="How the slab's planes are combined">
                    <option value="mean">Mean</option>
                    <option value="max">Max</option>
                    <option value="median">Median</option>
                </select>
                <span class="depth-panel-unit gate-slab-planes"></span>
            </div>`;
        this.buttons = [...this.root.querySelectorAll("[data-gate-view]")];
        this.slabRow = this.root.querySelector(".gate-slab-row");
        this.thickness = this.root.querySelector(".gate-slab-thickness");
        this.unit = this.root.querySelector(".gate-slab-unit");
        this.agg = this.root.querySelector(".gate-slab-agg");
        this.planesText = this.root.querySelector(".gate-slab-planes");
        for (const button of this.buttons) {
            button.title = GATE_VIEW_TITLES[button.dataset.gateView];
            button.addEventListener("click", () => this.onView(button.dataset.gateView));
        }
        this.root.querySelector(".cell-mode-options").addEventListener("keydown", (event) => {
            const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
            if (!step) return;
            event.preventDefault();
            const at = this.buttons.findIndex((b) => b.getAttribute("aria-checked") === "true");
            const next = this.buttons[(at + step + this.buttons.length) % this.buttons.length];
            next?.focus();
            next?.click();
        });
        this.thickness.addEventListener("input", () => {
            clearTimeout(this._typeTimer);
            this._typeTimer = setTimeout(() => this.onSlabFields(), GATE_SLAB_TYPE_MS);
            this.paintPlanes();
        });
        this.thickness.addEventListener("change", () => {
            clearTimeout(this._typeTimer);
            this.onSlabFields();
        });
        this.agg.addEventListener("change", () => this.onSlabFields());
    }

    onView(kind) {
        const showing = this.state?.mode === "slab" ? "slab" : "plane";
        if (kind === showing && kind === "plane") return;
        this.show(kind);
    }

    onSlabFields() {
        const value = Number(this.thickness.value);
        if (!(value > 0)) {
            this.paintPlanes();
            return;
        }
        const agg = this.agg.value || "mean";
        // In the unit the field shows: planes for a projection set elsewhere.
        const choice = this.unit.textContent === "µm" && this.zStepUm() > 0
            ? { thickness_um: value, agg } : { planes: Math.round(value), agg };
        this.show("slab", choice);
    }

    paint() {
        if (!this.root) return;
        const state = this.state;
        const shown = Boolean(state && this.ctx.view?.enabled && state.mode !== "volume");
        this.root.hidden = !shown;
        if (!shown) {
            this.paintReadout();
            return;
        }
        const kind = state.mode === "slab" ? "slab" : "plane";
        for (const button of this.buttons) {
            const active = button.dataset.gateView === kind;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-checked", active ? "true" : "false");
            button.tabIndex = active ? 0 : -1;
        }
        this.slabRow.hidden = kind !== "slab";
        const zKnown = this.zStepUm() > 0;
        if (kind === "slab" && document.activeElement !== this.thickness
                && document.activeElement !== this.agg) {
            const choice = this.slabChoice();
            const inMicrons = zKnown && choice.thickness_um !== undefined;
            this.thickness.value = inMicrons ? String(choice.thickness_um) : String(choice.planes);
            this.unit.textContent = inMicrons ? "µm" : "planes";
            this.agg.value = choice.agg || "mean";
        }
        this.thickness.step = this.unit.textContent === "µm" ? "0.5" : "2";
        this.thickness.min = this.unit.textContent === "µm" ? "0.1" : "1";
        this.thickness.title = zKnown
            ? `Planes within half this thickness of the plane on screen are combined, centre to centre (${this.formatUm(this.zStepUm())} µm per plane).`
            : "This stack's z step is not known, so the thickness is in planes. Set it in the Depth card to type microns.";
        this.paintPlanes();
        this.paintReadout();
    }

    /** Beside the fields: the planes combined now ("z 10–14", which shows
     *  the cut at either end too), or while a thickness is being typed, how
     *  many planes it comes to. */
    paintPlanes() {
        if (!this.planesText) return;
        const state = this.state;
        const typing = document.activeElement === this.thickness;
        this.planesText.textContent = "";
        this.planesText.title = "";
        if (typing) {
            const typed = Number(this.thickness.value);
            const body = this.unit.textContent === "µm" ? { thickness_um: typed }
                : { planes: Math.round(typed) };
            const half = typed > 0 ? this.halfFor(body) : null;
            if (half === null) return;
            const count = 2 * half + 1;
            this.planesText.textContent = `${count} plane${count === 1 ? "" : "s"}`;
            this.planesText.title = count === 1
                ? "Thinner than two plane steps: the plane on its own." : "";
            return;
        }
        if (state?.mode !== "slab") return;
        const { z0, z1 } = state.slab;
        const count = z1 - z0 + 1;
        this.planesText.textContent = z1 > z0 ? `z ${z0}–${z1}` : `z ${z0}`;
        this.planesText.title = `${count} plane${count === 1 ? "" : "s"}, centred on plane ${state.z} `
            + "and cut where the stack ends, so a plane near either end combines fewer.";
    }

    paintReadout() {
        const node = this.readout;
        if (!node) return;
        const counts = this.root && !this.root.hidden && this.plane ? this.counts() : null;
        if (!counts) {
            node.hidden = true;
            node.replaceChildren();
            return;
        }
        const fmt = (n) => Number(n || 0).toLocaleString();
        node.replaceChildren();
        const swatch = document.createElement("span");
        swatch.className = "gate-plane-swatch";
        swatch.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "gate-plane-text";
        text.textContent = `z ${this.plane.z} · ${fmt(counts.positive)} of ${fmt(counts.cells)} positive`;
        node.append(swatch, text);
        const state = this.state;
        const image = state?.mode === "slab"
            ? ` The image shows the ${{ mean: "mean", max: "maximum", med: "median" }[state.slab.op] || state.slab.op} of planes ${state.slab.z0}–${state.slab.z1}.`
            : "";
        const approximate = this.entry.header?.source === "bbox"
            ? " (Counted by each cell's planes in depth: measure the cells again for an exact count.)"
            : "";
        node.title = `Shaded: the ${fmt(counts.cells)} cells with voxels on plane ${this.plane.z} -- `
            + "the outlines drawn -- by their whole-cell values. Line: every cell of the stack. "
            + "Gates use each cell's whole-cell value, the same on every plane." + image + approximate;
        node.hidden = false;
    }

    formatUm(value) {
        const number = Number(value);
        return Number.isFinite(number) ? String(Math.round(number * 1000) / 1000) : "?";
    }
}

/** Counts smoothed over neighbouring bins (1-4-6-4-1), so a plane's few
 *  hundred cells read as a shape rather than a comb; the weights used are
 *  what each bin is divided by, so the end bins are not dimmed. */
function smoothBins(counts) {
    const weights = [1, 4, 6, 4, 1];
    return counts.map((_, i) => {
        let sum = 0;
        let used = 0;
        for (let k = -2; k <= 2; k += 1) {
            const j = i + k;
            if (j < 0 || j >= counts.length) continue;
            sum += counts[j] * weights[k + 2];
            used += weights[k + 2];
        }
        return used ? sum / used : 0;
    });
}

window.GatePlaneControl = GatePlaneControl;
window.smoothBins = smoothBins;
