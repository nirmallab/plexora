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
 * - TWO VIEWS, one at a time (the tabs on the body's first line), because
 *   they answer two different questions:
 *     Segmentation errors -- Under and Over, judged against the DNA stain:
 *       this label holds several nuclei, or this cut runs through one.
 *     Cell size -- Large, Small and Irregular, judged on the mask's shapes
 *       alone, against its own median: symptoms, not diagnoses (a large
 *       label may be two nuclei, or a giant cell).
 *   Only the chosen view's categories are drawn -- their cells in QC's cell
 *   layer and their share of the density map -- so the picture on screen is
 *   always one question's answer.
 * - A ROW PER CATEGORY: its colour (core's swatch), its name, its share of
 *   the cells, the threshold a cell must reach (core's PlexoraSlider), and
 *   its eye. Under's and Over's slider is the score; the size three's is how
 *   many robust SDs from the mask's median. The server re-thresholds what it
 *   stored; no pixel is read again. A hidden category's slider is disabled
 *   with it.
 * - THE DENSITY MAP (the flame): where the view's problems are concentrated,
 *   as a smooth field -- so the few cells worth a look can be found on a
 *   whole slide, and zoomed into. The server's `/segmentation/density`, asked
 *   for the view on screen on a grid ~8 screen pixels a cell. "Map opacity" is its
 *   opacity, shown while it is on.
 * - Options (the sliders glyph): the DNA channel, Run again, and putting the
 *   thresholds back. Download: every cell with a column per category, at the
 *   thresholds on screen, as CSV -- and, when the project was opened from a
 *   table file, Save writes those columns into it (after asking).
 *
 * THE HEADER'S EYE is the section's: off, nothing of it is drawn -- no
 * category, no map -- while every category keeps its own eye for when it
 * comes back on. Showing any one turns the section back on with it. Not
 * remembered: every load opens with it on.
 *
 * The view, which categories are hidden, their colours, the thresholds and
 * the map are per-viewer conveniences in localStorage, wrapped in try/catch.
 */
class QcSegmentationQc {

    constructor(ctx, api, host) {
        this.ctx = ctx;
        this.api = api;
        this.host = host;
        this.status = null;         // public_status from the server
        this.cells = null;          // {fingerprint, groups, max_id, sizes}
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
        this._saving = false;
        this.sliders = {};
        this.pickers = {};              // one of core's ColorSwatchPicker per category
        this.overlay = null;
        this.hidden = this.loadHidden();
        this.flags = this.loadFlags();  // per category; null: the default threshold
        this.view = this.loadView();    // {tab, heat, opacity, colors}
        this.muted = false;             // the header's eye, off
    }

    static get POLL_MS() { return 750; }
    static get FLAG_DEBOUNCE_MS() { return 150; }
    //: The two views, and the categories each one holds, in the panel's order.
    static get TABS() {
        return { errors: ["under", "over"], size: ["large", "small", "irregular"] };
    }
    static get CATEGORIES() {
        return [
            { key: "under", tab: "errors", word: "Under", long: "Under-segmented",
              what: "look like several nuclei in one label" },
            { key: "over", tab: "errors", word: "Over", long: "Over-segmented",
              what: "look like a fragment of a neighbour's nucleus" },
            { key: "large", tab: "size", word: "Large", long: "Large",
              what: "are far larger than the mask's median cell" },
            { key: "small", tab: "size", word: "Small", long: "Small",
              what: "are far smaller than the mask's median cell" },
            { key: "irregular", tab: "size", word: "Irregular", long: "Irregular",
              what: "have a far more ragged or elongated outline than the median cell" },
        ];
    }
    static get KEYS() { return QcSegmentationQc.CATEGORIES.map((c) => c.key); }
    //: Under / Over: a score from 0 to 1. The size three: robust SDs.
    static rangeOf(key) {
        return QcSegmentationQc.TABS.errors.includes(key)
            ? { min: 0.3, max: 0.95, step: 0.05, decimals: 2 }
            : { min: 1.5, max: 6, step: 0.25, decimals: 2 };
    }
    static category(key) {
        return QcSegmentationQc.CATEGORIES.find((c) => c.key === key) || null;
    }

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
        this.el("qc_seg_tabs")?.addEventListener("click", (event) => {
            const tab = event.target.closest("[data-tab]");
            if (tab) this.setTab(tab.dataset.tab);
        });
        this.el("qc_seg_tabs")?.addEventListener("keydown", (event) => this.tabKey(event));
        this.el("qc_seg_edit")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openMenu(event.currentTarget);
        });
        this.el("qc_seg_download")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openDownloadMenu(event.currentTarget);
        });
        for (const key of QcSegmentationQc.KEYS) {
            this.el(`qc_seg_${key}_eye`)?.addEventListener("click", () => this.toggle(`seg:${key}`));
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
        const flags = this.effectiveFlags();
        for (const category of QcSegmentationQc.CATEGORIES) {
            const key = category.key;
            const mount = this.el(`qc_seg_flag_${key}`);
            if (!mount || this.sliders[key]) continue;
            this.sliders[key] = new PlexoraSlider(mount, {
                ...QcSegmentationQc.rangeOf(key),
                display: (v) => Number(v).toFixed(2),
                value: flags[key],
                ariaLabel: `${category.long} threshold`,
                onInput: (value) => this.setFlag(key, value),
            });
        }
        const map = this.el("qc_seg_map_opacity");
        if (map && !this.sliders.map) {
            this.sliders.map = new PlexoraSlider(map, {
                min: 0.1, max: 1, step: 0.05, decimals: 2, value: this.view.opacity,
                ariaLabel: "Density map opacity",
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
        const view = { tab: "errors", heat: false, opacity: 0.7, colors: {} };
        for (const key of QcSegmentationQc.KEYS) view.colors[key] = null;
        try {
            const raw = JSON.parse(window.localStorage.getItem(this.viewKey()) || "{}");
            if (raw.tab in QcSegmentationQc.TABS) view.tab = raw.tab;
            if (typeof raw.heat === "boolean") view.heat = raw.heat;
            if (Number.isFinite(Number(raw.opacity))) {
                view.opacity = Math.max(0.1, Math.min(1, Number(raw.opacity)));
            }
            for (const key of QcSegmentationQc.KEYS) {
                const hex = raw.colors && raw.colors[key];
                if (/^#[0-9a-f]{6}$/i.test(hex || "")) view.colors[key] = hex;
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
        const flags = {};
        for (const key of QcSegmentationQc.KEYS) flags[key] = null;
        try {
            const raw = JSON.parse(window.localStorage.getItem(this.flagKey()) || "{}");
            for (const key of QcSegmentationQc.KEYS) {
                const value = Number(raw[key]);
                if (raw[key] != null && Number.isFinite(value)) flags[key] = value;
            }
        } catch (error) {
            // Unreadable or blocked storage: the default thresholds.
        }
        return flags;
    }

    saveFlags() {
        try {
            if (!this.tuned()) window.localStorage.removeItem(this.flagKey());
            else window.localStorage.setItem(this.flagKey(), JSON.stringify(this.flags));
        } catch (error) {
            // A private window: the sliders still work for this page.
        }
    }

    /** Whether any threshold is off its default (`keys`: only those). */
    tuned(keys = QcSegmentationQc.KEYS) {
        return keys.some((key) => this.flags[key] !== null);
    }

    /** The run's own score threshold (what the stored calls use). */
    storedFlag() {
        const params = this.status && this.status.summary && this.status.summary.params;
        return params && Number.isFinite(Number(params.flag)) ? Number(params.flag) : 0.6;
    }

    /** The size three's default: robust SDs from the mask's median. */
    outlierZ() {
        const z = (this.cells && this.cells.outlier_z) || (this.density && this.density.outlier_z);
        return Number.isFinite(Number(z)) ? Number(z) : 3;
    }

    defaultFlag(key) {
        return QcSegmentationQc.TABS.errors.includes(key) ? this.storedFlag() : this.outlierZ();
    }

    /** `value` null (or the default) puts that category back on its default. */
    setFlag(key, value) {
        this.flags[key] = value === null || Math.abs(value - this.defaultFlag(key)) < 1e-9
            ? null : value;
        this.saveFlags();
        this.render();
        window.clearTimeout(this._flagTimer);
        this._flagTimer = window.setTimeout(() => {
            this._flagTimer = null;
            this.loadCells();
            this.scheduleDensity(0);
        }, QcSegmentationQc.FLAG_DEBOUNCE_MS);
    }

    /** Every threshold of the view on screen back to its default. */
    resetFlags(keys = QcSegmentationQc.TABS[this.view.tab]) {
        for (const key of keys) this.flags[key] = null;
        this.saveFlags();
        this.render();
        this.loadCells();
        this.scheduleDensity(0);
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

    /** `seg:<category>`: drawn only in its own view, and only while shown. */
    groupVisible(key) {
        if (this.muted) return false;
        const category = QcSegmentationQc.category(String(key || "").slice(4));
        if (!category || category.tab !== this.view.tab) return false;
        return !this.hidden.has(key);
    }

    /** The header's eye: the whole section on or off, the rows untouched. */
    setMuted(muted) {
        this.muted = Boolean(muted);
        this.host.setCellGroups();
        this.render();
        this.overlay?.invalidate?.();
    }

    setTab(tab) {
        if (!(tab in QcSegmentationQc.TABS)) return;
        // Choosing a view is asking to see it.
        if (tab === this.view.tab && !this.muted) return;
        this.view.tab = tab;
        this.muted = false;
        this.saveView();
        this.host.setCellGroups();
        this.render();
        this.overlay?.invalidate?.();
    }

    /** The tabs' arrow keys: a tablist is one stop, its arrows move within. */
    tabKey(event) {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const tabs = Object.keys(QcSegmentationQc.TABS);
        const at = tabs.indexOf(this.view.tab);
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
            : (at + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
        event.preventDefault();
        this.setTab(tabs[next]);
        this.el(`qc_seg_tab_${tabs[next]}`)?.focus();
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
            const key = group.key && group.key.startsWith("seg:") ? group.key.slice(4) : null;
            const color = key && this.view.colors[key];
            return color ? { ...group, color } : group;
        });
    }

    /** A category's colour: the viewer's choice, else the run's. */
    colorOf(key, fallback) {
        return (this.view.colors && this.view.colors[key]) || fallback || "#9ca3af";
    }

    /** The server's colour for a category, from whichever answer has it. */
    serverColor(key) {
        const group = ((this.cells && this.cells.groups) || []).find((g) => g.key === `seg:${key}`);
        const layer = this.density && this.density.layers && this.density.layers[key];
        const colors = (this.status && this.status.summary && this.status.summary.colors) || {};
        return (group && group.color) || (layer && layer.color) || colors[key] || null;
    }

    /** Choosing a colour is asking to see it: the category comes back on,
     *  and the section with it. */
    setColor(key, hex) {
        this.view.colors[key] = hex;
        this.saveView();
        this._densityCanvases.delete(key);
        if (this.hidden.has(`seg:${key}`) || this.muted) {
            this.hidden.delete(`seg:${key}`);
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
        return { fingerprint: s.fingerprint, view: this.view.tab, pct_cells: shown.pct_cells,
                 pct_area: shown.pct_area, counts: shown.counts,
                 sizes: (this.cells && this.cells.sizes) || null, flags: this.effectiveFlags(),
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

    // -- download and save -------------------------------------------------------------

    /** What Save writes into, in words, or null when the project has no table
     *  file of a kind it can write. */
    tableWords() {
        const kind = this.status && this.status.table_kind;
        return { csv: "CSV", parquet: "Parquet", anndata: "AnnData",
                 spatialdata: "SpatialData" }[kind] || null;
    }

    openDownloadMenu(anchor) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const has = Boolean(this.status && this.status.summary);
        const words = this.tableWords();
        const items = [
            { label: "Cells (CSV)", disabled: !has,
              hint: "Every cell, with a column per category at the thresholds on screen",
              onSelect: () => this.download() },
        ];
        if (words) {
            items.push({ label: `Save into ${words} file`, className: "is-sectioned",
                         disabled: !has || this._saving,
                         hint: "Add the same columns to the table this project was opened from",
                         onSelect: () => this.save() });
        }
        QcTree.menu(anchor, items, { heading: "Segmentation QC", className: "qc-picker" });
    }

    download() {
        const link = document.createElement("a");
        link.href = QcApi.segmentationDownloadUrl(this.api.url, this.api.datasource, this.flags);
        link.download = "";
        document.body.appendChild(link);
        link.click();
        link.remove();
    }

    /** The thresholds in force, as a sentence for the Save dialog. */
    thresholdWords() {
        const flags = this.effectiveFlags();
        return `Under ${flags.under.toFixed(2)}, Over ${flags.over.toFixed(2)}; `
            + `Large, Small and Irregular at ${flags.large.toFixed(2)}, `
            + `${flags.small.toFixed(2)} and ${flags.irregular.toFixed(2)} robust SDs`;
    }

    /** Asked first, always: this writes into the user's own file. A conflict
     *  (an earlier save's columns) is asked about again, and only that answer
     *  sends `replace`. */
    async save() {
        const confirm = window.PlexoraConfirm;
        if (this._saving || !confirm) return;
        const words = this.tableWords();
        const go = await confirm.ask({
            title: `Save into your ${words} file?`,
            body: `Adds a column for each category (plexora_seg_qc_under_segmented, `
                + `_over_segmented, _large, _small, _irregular), the status and the scores `
                + `to the table this project was opened from.\n\nThresholds: `
                + `${this.thresholdWords()}.`,
            confirm: "Save", danger: false,
        });
        if (!go) return;
        this._saving = true;
        try {
            let answer = await this.api.segmentationWrite(this.flags).catch(() => null);
            if (answer && answer.status === 409) {
                const replace = await confirm.ask({
                    title: "Replace the earlier columns?",
                    body: "This file already holds Segmentation QC columns from an earlier "
                        + "save. Replace them? Nothing else in the file is touched.",
                    confirm: "Replace",
                });
                if (!replace) return;
                answer = await this.api.segmentationWrite(this.flags, { replace: true })
                    .catch(() => null);
            }
            if (!answer || !answer.ok) {
                this.host.message((answer && answer.data.error && answer.data.error.message)
                    || "Could not save into the file");
                return;
            }
            const written = answer.data.written || {};
            this.host.message(`Saved ${(written.columns || []).length} columns for `
                + `${(written.n_cells || 0).toLocaleString()} cells into your ${words} file`);
        } finally {
            this._saving = false;
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
        const answer = await this.api.segmentationDensity(params, this.flags).catch(() => null);
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

    /** The thresholds in force, per category: the slider's, else the default. */
    effectiveFlags() {
        const out = {};
        for (const key of QcSegmentationQc.KEYS) out[key] = this.flags[key] ?? this.defaultFlag(key);
        return out;
    }

    /** Under's and Over's numbers: the re-thresholded ones while a slider is
     *  off the run's threshold, else the stored summary's. */
    shownSummary() {
        const summary = (this.status && this.status.summary) || {};
        const cells = this.cells;
        if (this.tuned(QcSegmentationQc.TABS.errors) && cells && cells.pct_cells
                && cells.fingerprint === summary.fingerprint) {
            return Object.assign({}, summary, { counts: cells.counts, pct_cells: cells.pct_cells,
                                                pct_area: cells.pct_area });
        }
        return summary;
    }

    /** One category's {n, pct_cells, pct_area}, at the thresholds in force. */
    shareOf(key) {
        if (QcSegmentationQc.TABS.errors.includes(key)) {
            const shown = this.shownSummary();
            return { n: (shown.counts || {})[`${key}_segmented`] || 0,
                     pct_cells: (shown.pct_cells || {})[key],
                     pct_area: (shown.pct_area || {})[key], of: shown.n_cells || 0 };
        }
        const size = this.cells && this.cells.sizes && this.cells.sizes[key];
        if (!size) return null;
        const n = (this.status && this.status.summary && this.status.summary.n_cells) || 0;
        return { n: size.n, pct_cells: size.pct_cells, pct_area: size.pct_area, of: n };
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
        const download = this.el("qc_seg_download");
        if (download) download.disabled = !summary;
        const eye = this.el("qc_seg_eye");
        if (eye) {
            eye.setAttribute("aria-pressed", this.muted ? "false" : "true");
            eye.title = this.muted ? "Show Segmentation QC" : "Hide everything Segmentation QC draws";
        }
        tool.classList.toggle("is-muted", this.muted);
        this.renderProgress(running);
        this.renderTabs(summary, running);
        this.renderRows(summary, running);
        this.renderMapTune(summary, running);
        this.renderSummary(summary, running);
        this.renderNote(s, summary, mask, running);
        const edit = this.el("qc_seg_edit");
        if (edit) {
            edit.title = s.dna_channel ? `Options · DNA channel: ${s.dna_channel}` : "Options";
        }
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

    /** Segmentation errors | Cell size: which question the section answers. */
    renderTabs(summary, running) {
        const tabs = this.el("qc_seg_tabs");
        if (!tabs) return;
        tabs.hidden = running || !summary;
        for (const button of tabs.querySelectorAll("[data-tab]")) {
            const on = button.dataset.tab === this.view.tab;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-selected", on ? "true" : "false");
            button.tabIndex = on ? 0 : -1;
        }
    }

    /** The chosen view's rows: colour, name, share, threshold, eye. */
    renderRows(summary, running) {
        const flags = this.effectiveFlags();
        for (const [tab, keys] of Object.entries(QcSegmentationQc.TABS)) {
            const panel = this.el(`qc_seg_rows_${tab}`);
            if (!panel) continue;
            panel.hidden = running || !summary || tab !== this.view.tab;
            if (panel.hidden) continue;
            for (const key of keys) this.renderRow(key, flags[key]);
        }
    }

    renderRow(key, value) {
        const row = document.querySelector(`#qc_tool_seg .qc-seg-row[data-key="${key}"]`);
        if (!row) return;
        const category = QcSegmentationQc.category(key);
        const on = this.groupVisible(`seg:${key}`);
        const color = this.colorOf(key, this.serverColor(key));
        const share = this.shareOf(key);
        row.classList.toggle("is-hidden", !on);
        row.style.setProperty("--qc-row-color", color);
        const pct = this.el(`qc_seg_${key}_pct`);
        if (pct) pct.textContent = share ? QcSegmentationQc.pct(share.pct_cells) : "–";
        const errors = QcSegmentationQc.TABS.errors.includes(key);
        const bar = errors ? `a ${key} score of at least ${value.toFixed(2)}`
            : `at least ${value.toFixed(2)} robust SDs from the mask's median `
              + (key === "irregular" ? "circularity" : "area");
        row.title = !share ? category.long
            : `${share.n.toLocaleString()} of ${share.of.toLocaleString()} cells `
              + `(${QcSegmentationQc.pct(share.pct_cells)} of cells, `
              + `${QcSegmentationQc.pct(share.pct_area)} of segmented area) ${category.what}: `
              + `${bar}.` + (errors ? "" : " Judged on the mask's shapes, not the image.");
        const slider = this.sliders[key];
        if (slider && !slider.typing && Math.abs(slider.get() - value) > 1e-9) {
            slider.set(value, { silent: true });
        }
        slider?.setAccent?.(color);
        slider?.setDisabled?.(!on);
        const eye = this.el(`qc_seg_${key}_eye`);
        if (eye) {
            eye.setAttribute("aria-pressed", on ? "true" : "false");
            eye.title = `${on ? "Hide" : "Show"} ${category.long.toLowerCase()}`;
        }
        const mount = this.el(`qc_seg_${key}_color`);
        if (!mount || typeof ColorSwatchPicker === "undefined") return;
        if (!this.pickers[key]) {
            this.pickers[key] = new ColorSwatchPicker(mount, {
                value: color, title: `${category.long} colour`,
                onChange: (hex) => this.setColor(key, hex),
            });
        } else if (String(this.pickers[key].value || "").toLowerCase() !== color.toLowerCase()) {
            this.pickers[key].setValue(color);
        }
    }

    renderMapTune(summary, running) {
        const map = this.el("qc_seg_map_tune");
        if (map) map.hidden = running || !summary || !this.view.heat;
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
        } else if (summary && this.view.tab === "size" && this.cells && !this.cells.sizes) {
            text = "This result predates cell size: run Segmentation QC again";
        } else if (summary && this.view.heat && this.density && !this.density.available) {
            text = this.density.reason || "";
        } else if (!mask && s.pending) {
            text = "Mask being prepared";
        } else if (mask && !summary && !s.dna_channel) {
            text = "No DNA channel found: pick one in the options";
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
        const keys = QcSegmentationQc.TABS[this.view.tab];
        items.push({ label: "Reset thresholds", className: "is-sectioned",
                     disabled: !s.summary || !this.tuned(keys),
                     hint: this.view.tab === "errors"
                         ? `Under and Over back to the run's ${this.storedFlag().toFixed(2)}`
                         : `Large, Small and Irregular back to ${this.outlierZ()} robust SDs`,
                     onSelect: () => this.resetFlags(keys) });
        items.push({ label: "Run again",
                     disabled: !this.maskAvailable() || Boolean(this.job),
                     hint: "Measure again from the pixels",
                     onSelect: () => this.start(null, { force: true }) });
        QcTree.menu(anchor, items, { heading: "Segmentation QC", className: "qc-picker" });
    }
}

window.QcSegmentationQc = QcSegmentationQc;
