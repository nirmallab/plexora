/**
 * QcSegmentationQc - the Segmentation QC section of the QC panel.
 *
 * The analysis is the server's (`run_segmentation_qc`, a job): DNA peaks
 * against the mask's labels. This section starts it, follows the job, and
 * shows the answer:
 *
 * - Play runs it. Without a mask Play is shown muted and a click opens one
 *   small dialog ("Segmentation mask required"), whose button is core's own
 *   add-a-mask flow (`ctx.requirements.require`).
 * - While it runs: a thin progress bar between the header and the rows, the
 *   phase, and a cancel. The viewer stays free; the job is the server's, so a
 *   reload picks the bar back up.
 * - UNDER AND OVER, one line of two boxes: each a colour (core's swatch) and
 *   a word that shows or hides that category -- its cells in QC's cell layer,
 *   beside the QC reasons, and its share of the density map. The colour is
 *   the viewer's own over the run's; choosing one shows the category.
 * - WHILE THE DENSITY MAP IS ON: Large, Small and Irregular, outliers against
 *   the mask's own median, which exist only as the map -- a row each, with
 *   its share of cells and its eye.
 * - THE DENSITY MAP (the header's flame): where the problems are
 *   concentrated, as a smooth field rather than boxes -- the server's
 *   `/segmentation/density`, asked for the view on screen on a grid ~8 screen
 *   pixels a cell, so it is as fine as the zoom.
 * - The Under and Over sliders are the scores a cell must reach to be drawn
 *   Under, and Over (the server re-thresholds the stored scores; no pixel is
 *   read again). Each line opens with that category's share of the cells and
 *   wears its colour, and is disabled while its category is hidden. "Map" is
 *   the map's strength. All three are core's PlexoraSlider, adopting the
 *   range inputs in the template -- the same control as every slider in
 *   Plexora.
 *
 * THE HEADER'S EYE is the section's: off, nothing of it is drawn -- no
 * category, no map -- while every category keeps its own toggle for when it
 * comes back on. Showing any one turns the section back on with it. Not
 * remembered: every load opens with it on.
 *
 * Which categories are hidden, their colours, the thresholds and the map are
 * per-viewer conveniences in localStorage, wrapped in try/catch.
 */
class QcSegmentationQc {

    constructor(ctx, api, host) {
        this.ctx = ctx;
        this.api = api;
        this.host = host;
        this.status = null;         // public_status from the server
        this.cells = null;          // {fingerprint, groups, max_id}
        this.job = null;            // {job_id, status, progress}
        this.error = null;
        this.density = null;        // the last /segmentation/density answer
        this._densityCanvases = new Map();
        this._pollTimer = null;
        this._shownCells = false;
        this._cellsSeq = 0;
        this._densitySeq = 0;
        this._flagTimer = null;
        this._densityTimer = null;
        this.sliders = {};
        this.pickers = {};              // {under, over}: core's ColorSwatchPicker
        this.overlay = null;
        this.hidden = this.loadHidden();
        this.flags = this.loadFlags();  // {under, over}; null: the run's own threshold
        this.view = this.loadView();    // {heat, opacity, colors: {under, over}}
        this.muted = false;             // the header's eye, off
    }

    static get POLL_MS() { return 750; }
    static get FLAG_DEBOUNCE_MS() { return 150; }
    static get SIDES() { return ["under", "over"]; }
    //: Every category, in the panel's order; the last three are map-only.
    static get CATEGORIES() {
        return [
            { key: "under", word: "Under", what: "look like several nuclei in one label" },
            { key: "over", word: "Over", what: "look like a fragment of a neighbour's nucleus" },
            { key: "large", word: "Large", what: "are far larger than the mask's median cell" },
            { key: "small", word: "Small", what: "are far smaller than the mask's median cell" },
            { key: "irregular", word: "Irregular", what: "have a ragged or elongated outline" },
        ];
    }
    static get MAP_ONLY() { return ["large", "small", "irregular"]; }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        this.el("qc_seg_run")?.addEventListener("click", () => this.start());
        this.el("qc_seg_cancel")?.addEventListener("click", () => this.cancel());
        this.el("qc_seg_view_heat")?.addEventListener("click", () => {
            this.view.heat = !this.view.heat;
            this.saveView();
            if (this.view.heat) this.scheduleDensity(0);
            this.render();
            this.overlay?.invalidate?.();
        });
        this.el("qc_seg_eye")?.addEventListener("click", () => this.setMuted(!this.muted));
        this.el("qc_seg_list")?.addEventListener("click", (event) => {
            const row = event.target.closest(".qc-line[data-key]");
            if (row && event.target.closest("[data-action]")) this.toggle(`seg:${row.dataset.key}`);
        });
        this.el("qc_seg_edit")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openMenu(event.currentTarget);
        });
        for (const side of QcSegmentationQc.SIDES) {
            this.el(`qc_seg_flag_${side}_reset`)?.addEventListener("click",
                () => this.setFlag(side, null));
            this.el(`qc_seg_${side}_toggle`)?.addEventListener("click",
                () => this.toggle(`seg:${side}`));
        }
        this.buildSliders();
        this.overlay = this.ctx.layers?.addOverlay?.({
            id: "segmentation",
            kind: "shapes",
            draw: (opts) => this.draw(opts),
        }) || null;
        this.ctx.layers?.onViewportChange?.(() => this.scheduleDensity());
    }

    /** Core's slider, adopting each range input the template staged. */
    buildSliders() {
        if (typeof PlexoraSlider === "undefined") return;
        for (const side of QcSegmentationQc.SIDES) {
            const input = this.el(`qc_seg_flag_${side}`);
            if (!input || this.sliders[side]) continue;
            this.sliders[side] = new PlexoraSlider(input, {
                min: 0.3, max: 0.95, step: 0.05, decimals: 2,
                value: this.effectiveFlags()[side],
                ariaLabel: `${side === "under" ? "Under" : "Over"}-segmentation threshold`,
                onInput: (value) => this.setFlag(side, value),
            });
        }
        const map = this.el("qc_seg_map_opacity");
        if (map && !this.sliders.map) {
            this.sliders.map = new PlexoraSlider(map, {
                min: 0.1, max: 1, step: 0.05, decimals: 2, value: this.view.opacity,
                ariaLabel: "Density map strength",
                onInput: (value) => {
                    this.view.opacity = value;
                    this.overlay?.invalidate?.();
                },
                onChange: () => this.saveView(),
            });
        }
    }

    destroy() {
        window.clearTimeout(this._pollTimer);
        window.clearTimeout(this._flagTimer);
        window.clearTimeout(this._densityTimer);
        this._pollTimer = null;
        this._flagTimer = null;
        Object.values(this.sliders).forEach((slider) => slider?.destroy?.());
        this.sliders = {};
        Object.values(this.pickers).forEach((picker) => picker?.destroy?.());
        this.pickers = {};
        this.overlay?.remove?.();
        this.overlay = null;
    }

    // -- per-viewer state --------------------------------------------------------

    viewKey() {
        return `plexora.qc.segqc.view.${this.ctx.datasource}`;
    }

    loadView() {
        const view = { heat: false, opacity: 0.7, colors: { under: null, over: null } };
        try {
            const raw = JSON.parse(window.localStorage.getItem(this.viewKey()) || "{}");
            if (typeof raw.heat === "boolean") view.heat = raw.heat;
            if (Number.isFinite(Number(raw.opacity))) {
                view.opacity = Math.max(0.1, Math.min(1, Number(raw.opacity)));
            }
            for (const side of QcSegmentationQc.SIDES) {
                const hex = raw.colors && raw.colors[side];
                if (/^#[0-9a-f]{6}$/i.test(hex || "")) view.colors[side] = hex;
            }
        } catch (error) {
            // Unreadable or blocked storage: the defaults.
        }
        return view;
    }

    saveView() {
        try {
            window.localStorage.setItem(this.viewKey(), JSON.stringify(this.view));
        } catch (error) {
            // A private window: it still works for this page.
        }
    }

    flagKey() {
        return `plexora.qc.segqc.flags.${this.ctx.datasource}`;
    }

    loadFlags() {
        const flags = { under: null, over: null };
        try {
            const raw = JSON.parse(window.localStorage.getItem(this.flagKey()) || "{}");
            for (const side of QcSegmentationQc.SIDES) {
                const value = Number(raw[side]);
                if (raw[side] != null && Number.isFinite(value)) flags[side] = value;
            }
        } catch (error) {
            // Unreadable or blocked storage: the run's thresholds.
        }
        return flags;
    }

    tuned() {
        return QcSegmentationQc.SIDES.some((side) => this.flags[side] !== null);
    }

    /** The run's own threshold (what the stored calls use). */
    storedFlag() {
        const params = this.status && this.status.summary && this.status.summary.params;
        return params && Number.isFinite(Number(params.flag)) ? Number(params.flag) : 0.6;
    }

    /** `value` null (or the run's own) puts that side back on the stored calls. */
    setFlag(side, value) {
        const stored = this.storedFlag();
        this.flags[side] = value === null || Math.abs(value - stored) < 1e-9 ? null : value;
        try {
            if (!this.tuned()) window.localStorage.removeItem(this.flagKey());
            else window.localStorage.setItem(this.flagKey(), JSON.stringify(this.flags));
        } catch (error) {
            // A private window: the sliders still work for this page.
        }
        this.render();
        window.clearTimeout(this._flagTimer);
        this._flagTimer = window.setTimeout(() => {
            this._flagTimer = null;
            this.loadCells();
            this.scheduleDensity(0);
        }, QcSegmentationQc.FLAG_DEBOUNCE_MS);
    }

    storageKey() {
        return `plexora.qc.segqc.hidden.${this.ctx.datasource}`;
    }

    loadHidden() {
        try {
            const raw = window.localStorage.getItem(this.storageKey());
            return new Set(raw ? JSON.parse(raw) : []);
        } catch (error) {
            return new Set();
        }
    }

    saveHidden() {
        try {
            window.localStorage.setItem(this.storageKey(), JSON.stringify([...this.hidden]));
        } catch (error) {
            // A private window: the toggle still works for this page.
        }
    }

    /** `seg:<category>`. */
    groupVisible(key) {
        if (this.muted) return false;
        return !this.hidden.has(key);
    }

    /** The header's eye: the whole section on or off, the rows untouched. */
    setMuted(muted) {
        this.muted = Boolean(muted);
        this.host.setCellGroups();
        this.render();
        this.overlay?.invalidate?.();
    }

    toggle(key) {
        if (!key) return;
        // A row asked for while the section is off is shown, and the section with it.
        if (this.hidden.has(key) || this.muted) this.hidden.delete(key);
        else this.hidden.add(key);
        this.muted = false;
        this.saveHidden();
        this.host.setCellGroups();
        this.render();
        this.overlay?.invalidate?.();
    }

    /** The groups the QC cell layer colours, beside the QC reasons', in the
     *  colour chosen here where one was. */
    groups() {
        return ((this.cells && this.cells.groups) || []).map((group) => {
            const side = group.key && group.key.startsWith("seg:") ? group.key.slice(4) : null;
            const color = side && this.view.colors[side];
            return color ? { ...group, color } : group;
        });
    }

    /** A category's colour: the viewer's choice, else the run's. */
    colorOf(key, fallback) {
        return (this.view.colors && this.view.colors[key]) || fallback || "#9ca3af";
    }

    /** Choosing a colour is asking to see it: the category comes back on,
     *  and the section with it. */
    setColor(side, hex) {
        this.view.colors[side] = hex;
        this.saveView();
        this._densityCanvases.delete(side);
        if (this.hidden.has(`seg:${side}`) || this.muted) {
            this.hidden.delete(`seg:${side}`);
            this.muted = false;
            this.saveHidden();
        }
        this.host.setCellGroups();
        this.render();
        this.overlay?.invalidate?.();
    }

    /** What `get_state` reports for this section. */
    summary() {
        const s = this.status && this.status.summary;
        if (!s) return this.job ? { running: true, progress: this.job.progress || null } : null;
        const shown = this.shownSummary();
        return { fingerprint: s.fingerprint, pct_cells: shown.pct_cells, pct_area: shown.pct_area,
                 counts: shown.counts, flags: this.effectiveFlags(),
                 stored_flag: this.storedFlag(), stale: Boolean(this.status.stale),
                 notice: s.notice || null, hidden: [...this.hidden], muted: this.muted,
                 density_map: Boolean(this.view.heat) };
    }

    // -- the server ----------------------------------------------------------------

    /** A public_status from /state or /segmentation. */
    adopt(status) {
        if (!status || status.error) {
            this.render();
            return;
        }
        this.status = status;
        const fp = status.summary && status.summary.fingerprint;
        if (fp && (!this.cells || this.cells.fingerprint !== fp)) {
            this.loadCells();
            this.density = null;
            this.scheduleDensity(0);
        }
        if (!fp && this.cells) {
            this.cells = null;
            this.density = null;
            this.host.setCellGroups();
            this.overlay?.invalidate?.();
        }
        if (status.job && !this.job) this.follow(status.job.job_id);
        this.render();
    }

    async refresh() {
        const answer = await this.api.segmentation().catch(() => null);
        if (answer && answer.ok) this.adopt(answer.data.segmentation_qc);
    }

    async loadCells() {
        const seq = ++this._cellsSeq;
        const answer = await this.api.segmentationCells(this.flags).catch(() => null);
        // A slider dragged on: only the latest answer counts.
        if (seq !== this._cellsSeq) return;
        if (!answer || !answer.ok || !answer.data.available) return;
        this.cells = answer.data;
        this.host.setCellGroups();
        if (!this._shownCells && (this.cells.groups || []).some((g) => (g.ids || []).length)) {
            this._shownCells = true;
            this.ctx.layers?.showCells?.();
        }
        this.render();
    }

    /** `qc.segmentation` from the agent bridge: a run finished or was cleared,
     *  here or anywhere else. */
    onEvent(kind, payload) {
        if (kind === "qc.segmentation" || (payload && payload.after)) this.refresh();
    }

    maskAvailable() {
        const live = window.__plexora?.dataset?.segmentation;
        if (live && typeof live.available === "boolean") return live.available;
        return Boolean(this.status && this.status.available);
    }

    async start(dnaChannel = null, { force = false } = {}) {
        if (this.job) return;
        if (!this.maskAvailable()) {
            await this.askForMask();
            return;
        }
        this.error = null;
        const body = {};
        if (dnaChannel) body.dna_channel = dnaChannel;
        if (force) body.force = true;
        const answer = await this.api.segmentationRun(body).catch(() => null);
        if (!answer || !answer.ok) {
            this.error = (answer && answer.data.error && answer.data.error.message)
                || "Segmentation QC could not start";
            this.render();
            return;
        }
        this.follow(answer.data.job_id);
    }

    async askForMask() {
        const confirm = window.PlexoraConfirm;
        let add = true;
        if (confirm && typeof confirm.choose === "function") {
            add = await confirm.choose({
                title: "Segmentation mask required",
                body: "Add a segmentation mask to run Segmentation QC.",
                choices: [
                    { value: false, label: "Cancel" },
                    { value: true, label: "Add mask", kind: "primary", focus: true },
                ],
            }) === true;
        }
        if (!add || !this.ctx.requirements?.require) return;
        const done = await this.ctx.requirements.require(["segmentation"]).catch(() => false);
        if (!done) return;
        await window.__plexora?.refreshDataset?.();
        window.__plexora?.watchSegmentation?.();
        await this.refresh();
    }

    follow(jobId) {
        if (!jobId) return;
        this.job = { job_id: jobId, status: "queued", progress: { done: 0, total: null } };
        this.render();
        window.clearTimeout(this._pollTimer);
        const tick = async () => {
            this._pollTimer = null;
            const answer = await this.api.job(jobId).catch(() => null);
            if (!this.job || this.job.job_id !== jobId) return;
            if (!answer || !answer.ok) {
                this._pollTimer = window.setTimeout(tick, QcSegmentationQc.POLL_MS * 2);
                return;
            }
            const job = answer.data.job;
            this.job = job;
            if (job.status === "done") {
                this.job = null;
                await this.refresh();
                return;
            }
            if (job.status === "failed" || job.status === "cancelled") {
                this.job = null;
                this.error = job.status === "cancelled" ? null
                    : ((job.error && job.error.message) || "Segmentation QC failed");
                if (job.status === "cancelled") this.host.message("Segmentation QC cancelled");
                this.render();
                return;
            }
            this.render();
            this._pollTimer = window.setTimeout(tick, QcSegmentationQc.POLL_MS);
        };
        this._pollTimer = window.setTimeout(tick, 150);
    }

    async cancel() {
        if (!this.job) return;
        const answer = await this.api.jobCancel(this.job.job_id).catch(() => null);
        if (answer && !answer.ok) {
            this.host.message((answer.data.error && answer.data.error.message)
                || "Segmentation QC could not be cancelled");
        }
    }

    // -- the density map -------------------------------------------------------------

    wantsDensity() {
        return Boolean(this.view.heat && this.status && this.status.summary);
    }

    scheduleDensity(delay = 250) {
        if (!this.wantsDensity()) return;
        window.clearTimeout(this._densityTimer);
        this._densityTimer = window.setTimeout(() => this.loadDensity(), delay);
    }

    async loadDensity() {
        if (!this.wantsDensity()) return;
        const view = this.ctx.viewer?.viewportImageBounds?.(0);
        const image = window.__plexora?.dataset?.image || this.ctx.dataset?.image || {};
        let box;
        if (view) {
            const padX = (view.maxX - view.minX) * 0.1;
            const padY = (view.maxY - view.minY) * 0.1;
            box = [view.minX - padX, view.minY - padY, view.maxX + padX, view.maxY + padY];
        } else if (image.width && image.height) {
            box = [0, 0, Number(image.width), Number(image.height)];
        } else {
            return;
        }
        // About eight screen pixels a grid cell, whatever the zoom.
        const screen = QcRegistration.screenPixels ? QcRegistration.screenPixels() : 1200;
        const bins = Math.max(32, Math.min(256, Math.round(screen / 8)));
        const seq = ++this._densitySeq;
        const params = { box: box.map((v) => Math.round(v)).join(","), bins };
        if (this.flags.under != null) params.flag_under = this.flags.under;
        if (this.flags.over != null) params.flag_over = this.flags.over;
        const answer = await this.api.segmentationDensity(params).catch(() => null);
        if (seq !== this._densitySeq) return;
        if (!answer || !answer.ok) return;
        this.density = answer.data;
        this._densityCanvases.clear();
        this.render();
        this.overlay?.invalidate?.();
    }

    static bytes(base64) {
        const text = atob(base64 || "");
        const out = new Uint8Array(text.length);
        for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i);
        return out;
    }

    static rgb(hex) {
        const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
        const n = m ? parseInt(m[1], 16) : 0x9ca3af;
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    densityCanvas(name, layer) {
        const held = this._densityCanvases.get(name);
        if (held) return held;
        const { nx, ny } = this.density.grid;
        const values = QcSegmentationQc.bytes(layer.values);
        const cells = QcSegmentationQc.bytes(this.density.support);
        const canvas = document.createElement("canvas");
        canvas.width = nx;
        canvas.height = ny;
        const context = canvas.getContext("2d");
        const picture = context.createImageData(nx, ny);
        const [r, g, b] = QcSegmentationQc.rgb(this.colorOf(name, layer.color));
        for (let i = 0; i < nx * ny; i++) {
            const share = values[i] / 255;
            if (!share) continue;
            // Hottest at a third of the cells flagged; faded where a grid cell
            // holds under half the typical number of cells.
            const strength = Math.pow(Math.min(1, share / 0.33), 0.7)
                * Math.min(1, cells[i] / 128);
            const o = i * 4;
            picture.data[o] = r;
            picture.data[o + 1] = g;
            picture.data[o + 2] = b;
            picture.data[o + 3] = Math.round(255 * strength);
        }
        context.putImageData(picture, 0, 0);
        this._densityCanvases.set(name, canvas);
        return canvas;
    }

    draw(opts) {
        if (this.muted || !this.view.heat || !this.density || !this.density.available) return;
        if (typeof document === "undefined" || !document.createElement) return;
        const current = this.status && this.status.summary && this.status.summary.fingerprint;
        if (this.density.fingerprint !== current) return;
        const context = opts.context;
        const { x0, y0, step, nx, ny } = this.density.grid;
        context.save();
        context.globalAlpha = this.view.opacity;
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = "high";
        for (const { key } of QcSegmentationQc.CATEGORIES) {
            const layer = (this.density.layers || {})[key];
            if (!layer || !this.groupVisible(`seg:${key}`)) continue;
            context.drawImage(this.densityCanvas(key, layer), x0, y0, nx * step, ny * step);
        }
        context.restore();
    }

    // -- the section -------------------------------------------------------------------

    /** The thresholds in force, per side: the slider's, else the run's. */
    effectiveFlags() {
        const stored = this.storedFlag();
        return { under: this.flags.under ?? stored, over: this.flags.over ?? stored };
    }

    /** The numbers the rows show: the re-thresholded ones while a slider is
     *  off the run's threshold, else the stored summary's. */
    shownSummary() {
        const summary = (this.status && this.status.summary) || {};
        const cells = this.cells;
        if (this.tuned() && cells && cells.pct_cells
                && cells.fingerprint === summary.fingerprint) {
            return Object.assign({}, summary, { counts: cells.counts, pct_cells: cells.pct_cells,
                                                pct_area: cells.pct_area });
        }
        return summary;
    }

    static pct(value) {
        const n = Number(value) || 0;
        return n > 0 && n < 0.1 ? "<0.1%" : `${n.toFixed(1)}%`;
    }

    render() {
        const tool = this.el("qc_tool_seg");
        if (!tool) return;
        const s = this.status || {};
        const summary = s.summary;
        const mask = this.maskAvailable();
        const running = Boolean(this.job);
        tool.dataset.state = running ? "running" : summary ? "done" : "idle";
        const run = this.el("qc_seg_run");
        if (run) {
            run.setAttribute("aria-disabled", mask && !running ? "false" : "true");
            run.title = !mask ? "Segmentation QC needs a segmentation mask: click to add one"
                : running ? "Segmentation QC is running"
                    : summary && s.stale ? "The image, mask or DNA channel changed: run Segmentation QC again"
                        : summary ? "Run Segmentation QC again" : "Run Segmentation QC";
        }
        const heat = this.el("qc_seg_view_heat");
        if (heat) {
            heat.setAttribute("aria-pressed", this.view.heat ? "true" : "false");
            heat.disabled = !summary;
        }
        const eye = this.el("qc_seg_eye");
        if (eye) {
            eye.setAttribute("aria-pressed", this.muted ? "false" : "true");
            eye.title = this.muted ? "Show Segmentation QC" : "Hide everything Segmentation QC draws";
        }
        tool.classList.toggle("is-muted", this.muted);
        this.renderProgress(running);
        this.renderToggles(summary, running);
        this.renderList(summary, running);
        this.renderTune(summary, running);
        this.renderSummary(summary, running);
        this.renderNote(s, summary, mask, running);
        const edit = this.el("qc_seg_edit");
        if (edit) edit.title = s.dna_channel ? `DNA channel: ${s.dna_channel}` : "DNA channel";
    }

    renderProgress(running) {
        const progress = this.el("qc_seg_progress");
        if (!progress) return;
        progress.hidden = !running;
        if (!running) return;
        const p = this.job.progress || {};
        const fraction = p.total ? Math.min(1, (p.done || 0) / p.total) : 0;
        const fill = this.el("qc_seg_fill");
        if (fill) fill.style.transform = `scaleX(${fraction.toFixed(3)})`;
        const phase = this.el("qc_seg_phase");
        if (phase) {
            const words = p.message && !["queued", "running"].includes(p.message)
                ? p.message : "Starting";
            phase.textContent = `${Math.round(100 * fraction)}% · ${words}`;
            phase.title = phase.textContent;
        }
    }

    /** Under and Over: a colour and a word each, on one line. */
    renderToggles(summary, running) {
        const line = this.el("qc_seg_toggles");
        if (!line) return;
        line.hidden = running || !summary;
        if (line.hidden) return;
        const shown = this.shownSummary();
        const colors = summary.colors || {};
        for (const category of QcSegmentationQc.CATEGORIES) {
            const side = category.key;
            if (!QcSegmentationQc.SIDES.includes(side)) continue;
            const on = this.groupVisible(`seg:${side}`);
            const color = this.colorOf(side, colors[side]);
            line.querySelector(`.qc-seg-toggle[data-side="${side}"]`)
                ?.classList.toggle("is-off", !on);
            const word = this.el(`qc_seg_${side}_toggle`);
            if (word) {
                word.setAttribute("aria-pressed", on ? "true" : "false");
                word.title = `${on ? "Hide" : "Show"} ${category.word.toLowerCase()}: `
                    + this.describe(category, shown);
            }
            const mount = this.el(`qc_seg_${side}_color`);
            if (!mount || typeof ColorSwatchPicker === "undefined") continue;
            if (!this.pickers[side]) {
                this.pickers[side] = new ColorSwatchPicker(mount, {
                    value: color, title: `${category.word} colour`,
                    onChange: (value) => this.setColor(side, value),
                });
            } else if (String(this.pickers[side].value || "").toLowerCase() !== color.toLowerCase()) {
                this.pickers[side].setValue(color);
            }
        }
    }

    /** What an Under or Over count means, in a sentence. */
    describe(category, shown) {
        const pct = (shown.pct_cells || {})[category.key];
        const area = (shown.pct_area || {})[category.key];
        const count = (shown.counts || {})[category.key === "under" ? "under_segmented"
            : "over_segmented"];
        return `${(count || 0).toLocaleString()} of `
            + `${(shown.n_cells || 0).toLocaleString()} cells `
            + `(${QcSegmentationQc.pct(pct)} of cells, ${QcSegmentationQc.pct(area)} `
            + `of segmented area) ${category.what}.`;
    }

    /** The map's own three, a row each, while the map is on and has them.
     *  Under and Over are the toggle line's (renderToggles). */
    renderList(summary, running) {
        const list = this.el("qc_seg_list");
        if (!list) return;
        const shown = summary ? this.shownSummary() : null;
        const map = this.view.heat && this.density && this.density.available
            ? this.density.layers || {} : {};
        const colors = (summary && summary.colors) || {};
        const rows = [];
        if (shown && !running) {
            for (const category of QcSegmentationQc.CATEGORIES) {
                if (!QcSegmentationQc.MAP_ONLY.includes(category.key) || !map[category.key]) continue;
                rows.push({ ...category,
                            color: map[category.key].color || colors[category.key] || "#9ca3af" });
            }
        }
        const live = new Set(rows.map((r) => r.key));
        for (const row of [...list.children]) if (!live.has(row.dataset.key)) row.remove();
        let previous = null;
        for (const spec of rows) {
            let row = [...list.children].find((child) => child.dataset.key === spec.key);
            if (!row) row = QcSegmentationQc.buildRow(spec.key);
            const expected = previous ? previous.nextSibling : list.firstChild;
            if (row !== expected) list.insertBefore(row, expected);
            previous = row;
            const key = `seg:${spec.key}`;
            const on = this.groupVisible(key);
            row.style.setProperty("--qc-row-color", spec.color);
            row.classList.toggle("is-hidden", !on);
            row.querySelector(".qc-line-name").textContent = spec.word;
            const eye = row.querySelector(".qc-eye");
            eye.setAttribute("aria-pressed", on ? "true" : "false");
            eye.title = on ? `Hide ${spec.word.toLowerCase()}` : `Show ${spec.word.toLowerCase()}`;
            const layer = map[spec.key];
            row.querySelector(".qc-line-score").textContent = QcSegmentationQc.pct(layer.pct_cells);
            row.title = `${(layer.n || 0).toLocaleString()} of `
                + `${(this.density.n_cells || 0).toLocaleString()} cells `
                + `(${QcSegmentationQc.pct(layer.pct_cells)}) ${spec.what}: at least `
                + `${this.density.outlier_z} robust SDs from the median. On the map only.`;
        }
    }

    static buildRow(key) {
        const row = document.createElement("div");
        row.className = "qc-line is-nested";
        row.setAttribute("role", "listitem");
        row.dataset.key = key;
        const dot = document.createElement("span");
        dot.className = "qc-line-dot";
        dot.setAttribute("aria-hidden", "true");
        const label = document.createElement("span");
        label.className = "qc-line-name is-static";
        const fill = document.createElement("span");
        fill.className = "qc-line-fill";
        const score = document.createElement("span");
        score.className = "qc-line-score";
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = "qc-line-action qc-eye";
        eye.dataset.action = "eye";
        eye.innerHTML = '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>';
        row.append(dot, label, fill, score, eye);
        return row;
    }

    renderTune(summary, running) {
        const tune = this.el("qc_seg_tune");
        if (!tune) return;
        tune.hidden = running || !summary;
        if (tune.hidden) return;
        const stored = this.storedFlag();
        const flags = this.effectiveFlags();
        const shown = this.shownSummary();
        const colors = summary.colors || {};
        for (const side of QcSegmentationQc.SIDES) {
            const line = tune.querySelector(`.qc-tune[data-side="${side}"]`);
            if (!line) continue;
            const on = this.groupVisible(`seg:${side}`);
            const value = flags[side];
            const share = QcSegmentationQc.pct((shown.pct_cells || {})[side]);
            const slider = this.sliders[side];
            if (slider && !slider.typing && Math.abs(slider.get() - value) > 1e-9) {
                slider.set(value, { silent: true });
            }
            slider?.setAccent?.(this.colorOf(side, colors[side]));
            slider?.setDisabled?.(!on);
            line.classList.toggle("is-off", !on);
            const pct = this.el(`qc_seg_${side}_pct`);
            if (pct) pct.textContent = share;
            const reset = this.el(`qc_seg_flag_${side}_reset`);
            if (reset) {
                reset.hidden = this.flags[side] === null;
                reset.disabled = !on;
                reset.title = `Back to the run's threshold (${stored.toFixed(2)})`;
            }
            const word = side === "under" ? "Under" : "Over";
            line.title = !on ? `${word} is hidden: show it to adjust its threshold`
                : `${share} of cells are drawn ${word}: their ${side} score is at least `
                + `${value.toFixed(2)}; ${Math.max(0, value - 0.2).toFixed(2)}-${value.toFixed(2)} `
                + `is ambiguous. For viewing only: the stored calls and the export use `
                + `${stored.toFixed(2)}.`;
        }
        const map = this.el("qc_seg_map_tune");
        if (map) map.hidden = !this.view.heat;
    }

    renderSummary(summary, running) {
        const node = this.el("qc_seg_summary");
        if (!node) return;
        node.classList.remove("is-flagged");
        if (running) {
            const p = (this.job && this.job.progress) || {};
            node.textContent = p.total ? `${Math.round(100 * (p.done || 0) / p.total)}%` : "…";
            node.title = "Segmentation QC is running";
            return;
        }
        if (!summary) {
            node.textContent = this.maskAvailable() ? "" : "no mask";
            node.title = "";
            return;
        }
        const shown = this.shownSummary();
        const pct = shown.pct_cells || {};
        const flagged = (Number(pct.under) || 0) + (Number(pct.over) || 0);
        node.textContent = `${QcSegmentationQc.pct(flagged)}`;
        node.classList.toggle("is-flagged", flagged > 0);
        node.title = `Under ${QcSegmentationQc.pct(pct.under)}, over `
            + `${QcSegmentationQc.pct(pct.over)} of ${(shown.n_cells || 0).toLocaleString()} cells`
            + (this.status.stale ? " (stale: the inputs changed)" : "");
    }

    renderNote(s, summary, mask, running) {
        const note = this.el("qc_seg_status");
        if (!note) return;
        let text = "";
        let error = false;
        if (this.error && !running) {
            text = this.error;
            error = true;
        } else if (running) {
            text = "";
        } else if (summary && s.stale) {
            text = "Stale: the image, mask or DNA channel changed";
        } else if (summary && summary.notice) {
            text = summary.notice;
        } else if (summary && this.view.heat && this.density && !this.density.available) {
            text = this.density.reason || "";
        } else if (!mask && s.pending) {
            text = "Mask being prepared";
        } else if (mask && !summary && !s.dna_channel) {
            text = "No DNA channel found: pick one in the settings";
        }
        note.hidden = !text;
        note.textContent = text;
        note.title = text;
        note.classList.toggle("is-error", error);
    }

    openMenu(anchor) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const s = this.status || {};
        const names = window.__plexora?.dataset?.image?.channelNames
            || this.ctx.dataset?.image?.channelNames || [];
        const candidates = s.candidates || [];
        const others = names.filter((n) => !candidates.includes(n));
        const pick = (name) => ({
            label: name, checked: name === s.dna_channel,
            hint: `Check the mask against ${name}`,
            onSelect: () => this.start(name),
        });
        const items = [{ heading: "DNA channel" }, ...candidates.map(pick)];
        if (!candidates.length) items.push({ label: "None detected", disabled: true });
        if (others.length && others.length <= 60) {
            items.push({ heading: "Other channels" }, ...others.map(pick));
        }
        items.push({ label: "Run again", className: "is-sectioned",
                     disabled: !this.maskAvailable() || Boolean(this.job),
                     hint: "Measure again from the pixels",
                     onSelect: () => this.start(null, { force: true }) });
        QcTree.menu(anchor, items, { heading: "Segmentation QC", className: "qc-picker" });
    }
}

window.QcSegmentationQc = QcSegmentationQc;
