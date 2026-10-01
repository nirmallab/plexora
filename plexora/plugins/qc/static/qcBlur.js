/**
 * QcBlurQc - the Blur QC section of the QC panel.
 *
 * The analysis is the server's (`run_blur_check`, a job): multi-scale
 * gradient focus per tile of the tissue against the image's own sharpest
 * tiles, a 0-1 Blur Score per grid cell, one result per DNA channel. This
 * section starts it, follows the job, and shows the answer.
 *
 * ONE LINE PER DNA CHANNEL, the way the image channels are listed: its
 * colour (core's swatch picker), its channel (core's searchable select --
 * picking another swaps it), its own threshold slider, an eye and a remove.
 * The first three nuclear channels until someone picks. The header's + adds
 * an empty line, as the image channels' + does, whose select opens at once;
 * nothing is stored until a channel is picked in it (DNA channels first). The blurred share is the header's (the worst channel's)
 * and each line's tooltip. Which channels are listed, their thresholds
 * and their colours are the server's (`set_blur_check`), so the panel, an
 * agent and the ROI write read the same numbers.
 *
 * - Play runs every listed channel (one that has not changed is reused).
 *   While it runs: the progress bar, its phase and a cancel. The job is the
 *   server's, so a reload picks the bar back up.
 * - A SLIDER previews while dragged (`/blur/mask`, nothing stored) and
 *   commits on release (receipted). Its number reads two decimals at rest and
 *   the full value when clicked (core's slider `display`); a typed 0.4137 is
 *   held and sent as 0.4137. The value is never echoed back into the slider
 *   it came from.
 * - THE HEATMAP (the header's flame) and THE REGIONS (the settings menu) are
 *   toggled independently; each is drawn per shown channel in that
 *   channel's colour. A row's eye shows or hides that channel.
 *
 * Both overlays are drawn in full-resolution image pixels from the fixed
 * analysis grid, so they stay on the tissue at every zoom.
 *
 * THE HEADER'S EYE is the section's, as Segmentation QC's: off, nothing of it
 * is drawn; not remembered. The heatmap, the regions and the hidden channels
 * are per-viewer conveniences in localStorage, wrapped in try/catch.
 */
class QcBlurQc {

    constructor(ctx, api, host) {
        this.ctx = ctx;
        this.api = api;
        this.host = host;
        this.status = null;         // public_status from the server
        this.rows = new Map();      // channel -> the row's state (see entryFor)
        this.job = null;
        this.error = null;
        this.overlay = null;
        this._pollTimer = null;
        this.draft = null;          // the empty line + added: {nodes, select}
        this.view = this.loadView(); // {heat, mask, hidden: [channel]}
        this.muted = false;
    }

    static get POLL_MS() { return 750; }
    static get PREVIEW_MS() { return 100; }
    static get FILL_ALPHA() { return 0.18; }
    static get STROKE() { return 1.6; }
    static get HEAT_OPACITY() { return 0.7; }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        this.el("qc_blur_run")?.addEventListener("click", () => this.start());
        this.el("qc_blur_cancel")?.addEventListener("click", () => this.cancel());
        this.el("qc_blur_view_heat")?.addEventListener("click", () => this.setView("heat"));
        this.el("qc_blur_eye")?.addEventListener("click", () => this.setMuted(!this.muted));
        this.el("qc_blur_add")?.addEventListener("click", () => this.addChannel());
        this.el("qc_blur_edit")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openMenu(event.currentTarget);
        });
        this.overlay = this.ctx.layers?.addOverlay?.({
            id: "blur",
            kind: "shapes",
            draw: (opts) => this.draw(opts),
        }) || null;
    }

    destroy() {
        window.clearTimeout(this._pollTimer);
        this._pollTimer = null;
        for (const entry of this.rows.values()) this.dropEntry(entry);
        this.rows.clear();
        this.dropDraft();
        this.overlay?.remove?.();
        this.overlay = null;
    }

    onShow() {}

    // -- per-viewer state --------------------------------------------------------

    viewKey() {
        return `plexora.qc.blur.view.${this.ctx.datasource}`;
    }

    loadView() {
        const view = { heat: false, mask: true, hidden: [] };
        try {
            const raw = JSON.parse(window.localStorage.getItem(this.viewKey()) || "{}");
            if (typeof raw.heat === "boolean") view.heat = raw.heat;
            if (typeof raw.mask === "boolean") view.mask = raw.mask;
            if (Array.isArray(raw.hidden)) view.hidden = raw.hidden.map(String).slice(0, 50);
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

    /** The heatmap or the regions on or off, each on its own. Asking for one
     *  while the section is off turns the section back on with it. */
    setView(which) {
        this.view[which] = this.muted || !this.view[which];
        this.muted = false;
        this.saveView();
        if (which === "heat" && this.view.heat) {
            for (const entry of this.rows.values()) if (!entry.map) this.loadMap(entry);
        }
        this.render();
        this.overlay?.invalidate?.();
    }

    setMuted(muted) {
        this.muted = Boolean(muted);
        this.render();
        this.overlay?.invalidate?.();
    }

    visible(channel) {
        return !this.view.hidden.includes(channel);
    }

    toggleChannel(channel) {
        const hidden = new Set(this.view.hidden);
        if (hidden.has(channel)) hidden.delete(channel);
        else hidden.add(channel);
        this.view.hidden = [...hidden];
        this.muted = false;
        this.saveView();
        this.render();
        this.overlay?.invalidate?.();
    }

    /** What `get_state` reports for this section. */
    summary() {
        if (!this.status) return this.job ? { running: true, progress: this.job.progress || null } : null;
        const channels = [...this.rows.values()].map((entry) => {
            const r = entry.result || {};
            const s = r.summary || null;
            const e = entry.evaluation || r.evaluation || {};
            const t = r.threshold || {};
            return { channel: entry.channel, color: r.color || null,
                     fingerprint: s ? s.fingerprint : null, status: s ? s.status : null,
                     threshold: entry.value, threshold_source: t.source || null,
                     auto_threshold: s ? s.auto_threshold : null,
                     blurred_pct: s ? e.blurred_pct ?? null : null,
                     n_regions: s ? e.n_regions ?? null : null,
                     global_blur: s ? s.global_blur || null : null,
                     stale: Boolean(r.stale), shown: this.visible(entry.channel) };
        });
        return { channels, heat: Boolean(this.view.heat), mask: Boolean(this.view.mask),
                 muted: this.muted, running: Boolean(this.job) };
    }

    // -- the server ----------------------------------------------------------------

    /** A public_status from /state or /blur. */
    adopt(status) {
        if (!status || status.error) {
            this.render();
            return;
        }
        this.status = status;
        const results = status.results || [];
        const live = new Set(results.map((r) => r.channel));
        for (const [channel, entry] of [...this.rows.entries()]) {
            if (!live.has(channel)) {
                this.dropEntry(entry);
                this.rows.delete(channel);
            }
        }
        for (const result of results) {
            const entry = this.entryFor(result.channel);
            const colorChanged = entry.result && entry.result.color !== result.color;
            entry.result = result;
            if (colorChanged) entry.heat = null;
            // The server's threshold, unless a hand is on this slider.
            if (!entry.maskTimer && !entry.holding && result.threshold) {
                entry.value = Number(result.threshold.value);
            }
            const s = result.summary;
            if (s && s.status === "ok") {
                if (entry.map && entry.map.fingerprint !== s.fingerprint) {
                    entry.map = null;
                    entry.heat = null;
                }
                if (this.view.heat && !entry.map) this.loadMap(entry);
                if (!entry.evaluation || entry.evaluation.fingerprint !== s.fingerprint
                        || Math.abs(Number(entry.evaluation.threshold) - entry.value) > 1e-9) {
                    this.loadMask(entry, entry.value);
                }
            } else {
                entry.map = null;
                entry.heat = null;
                entry.evaluation = null;
                entry.paths = null;
            }
        }
        if (status.job && !this.job) this.follow(status.job.job_id, status.job.channels);
        this.render();
        this.overlay?.invalidate?.();
    }

    /** A row's state, made the first time its channel is listed. */
    entryFor(channel) {
        let entry = this.rows.get(channel);
        if (!entry) {
            entry = { channel, result: null, value: null, map: null, heat: null,
                      evaluation: null, paths: null, nodes: null, slider: null, picker: null,
                      maskTimer: null, maskSeq: 0, setSeq: 0, holding: false };
            this.rows.set(channel, entry);
        }
        return entry;
    }

    dropEntry(entry) {
        window.clearTimeout(entry.maskTimer);
        entry.maskTimer = null;
        entry.slider?.destroy?.();
        entry.picker?.destroy?.();
        entry.select?.destroy?.();
        entry.nodes?.row?.remove?.();
        entry.slider = null;
        entry.picker = null;
        entry.select = null;
        entry.nodes = null;
    }

    async refresh() {
        const answer = await this.api.blur().catch(() => null);
        if (answer && answer.ok) this.adopt(answer.data.blur_qc);
    }

    /** `qc.blur*` from the agent bridge: a run finished, a setting changed
     *  or a result was cleared, here or anywhere else. */
    onEvent() {
        this.refresh();
    }

    async loadMap(entry) {
        const answer = await this.api.blurMap({ channel: entry.channel }).catch(() => null);
        if (!answer || !answer.ok || !answer.data.available) return;
        entry.map = answer.data;
        entry.heat = null;
        this.overlay?.invalidate?.();
    }

    async loadMask(entry, threshold) {
        const seq = ++entry.maskSeq;
        const params = { channel: entry.channel };
        if (threshold != null) params.threshold = threshold;
        const answer = await this.api.blurMask(params).catch(() => null);
        // Dragged on: only the latest answer counts.
        if (seq !== entry.maskSeq) return;
        if (!answer || !answer.ok || !answer.data.available) return;
        entry.evaluation = answer.data;
        entry.paths = null;
        this.render();
        this.overlay?.invalidate?.();
    }

    async start(options = {}) {
        if (this.job) return;
        this.error = null;
        const body = {};
        if (options.channel) body.channel = options.channel;
        if (options.force) body.force = true;
        const answer = await this.api.blurRun(body).catch(() => null);
        if (!answer || !answer.ok) {
            this.error = (answer && answer.data.error && answer.data.error.message)
                || "Blur QC could not start";
            this.render();
            return;
        }
        this.follow(answer.data.job_id,
                    options.channel ? [options.channel] : this.listed());
    }

    follow(jobId, channels = null) {
        if (!jobId) return;
        this.job = { job_id: jobId, status: "queued", progress: { done: 0, total: null },
                     channels: channels || this.listed() };
        this.render();
        window.clearTimeout(this._pollTimer);
        const tick = async () => {
            this._pollTimer = null;
            const answer = await this.api.job(jobId).catch(() => null);
            if (!this.job || this.job.job_id !== jobId) return;
            if (!answer || !answer.ok) {
                this._pollTimer = window.setTimeout(tick, QcBlurQc.POLL_MS * 2);
                return;
            }
            const job = answer.data.job;
            this.job = { ...job, channels: this.job.channels };
            if (job.status === "done") {
                this.job = null;
                await this.refresh();
                return;
            }
            if (job.status === "failed" || job.status === "cancelled") {
                this.job = null;
                this.error = job.status === "cancelled" ? null
                    : ((job.error && job.error.message) || "Blur QC failed");
                if (job.status === "cancelled") this.host.message("Blur QC cancelled");
                this.render();
                return;
            }
            this.render();
            this._pollTimer = window.setTimeout(tick, QcBlurQc.POLL_MS);
        };
        this._pollTimer = window.setTimeout(tick, 150);
    }

    async cancel() {
        if (!this.job) return;
        const answer = await this.api.jobCancel(this.job.job_id).catch(() => null);
        if (answer && !answer.ok) {
            this.host.message((answer.data.error && answer.data.error.message)
                || "Blur QC could not be cancelled");
        }
    }

    // -- the listed channels -----------------------------------------------------------

    listed() {
        return (this.status && this.status.shown) || [];
    }

    /** Channels not listed yet, the DNA ones first, in channel order: what
     *  + adds from. */
    unlisted() {
        const shown = new Set(this.listed());
        const s = this.status || {};
        const dna = (s.candidates || []).filter((n) => !shown.has(n));
        const rest = (s.channels || []).filter((n) => !shown.has(n) && !dna.includes(n));
        return [...dna, ...rest];
    }

    anyResult() {
        return [...this.rows.values()].some((entry) => entry.result && entry.result.summary);
    }

    async setChannels(channels, { run = null } = {}) {
        const answer = await this.api.blurSet({ channels }).catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The channels could not be changed");
            return;
        }
        await this.refresh();
        // Once the others are measured, a channel added is measured too.
        if (run && this.anyResult() && !this.job) this.start({ channel: run });
    }

    /** +: an empty line under the others, its select open -- the image
     *  channels' +. A second press opens the same line's select again. */
    addChannel() {
        if (!this.unlisted().length || this.listed().length >= 12) return;
        if (!this.draft) this.buildDraft();
        this.render();
        this.draft.select?.open?.(true);
    }

    /** The empty line's select answered: listed, and measured once the
     *  others are. */
    pickDraft(name) {
        if (!name) return;
        this.dropDraft();
        this.setChannels([...this.listed(), name], { run: name });
    }

    dropDraft() {
        if (!this.draft) return;
        this.draft.select?.destroy?.();
        this.draft.nodes.row.remove();
        this.draft = null;
    }

    removeChannel(channel) {
        const rest = this.listed().filter((n) => n !== channel);
        if (!rest.length) return;
        this.setChannels(rest);
    }

    swapChannel(channel, other) {
        this.setChannels(this.listed().map((n) => (n === channel ? other : n)), { run: other });
    }

    // -- the thresholds ------------------------------------------------------------

    /**
     * The one path every threshold change takes -- the slider, its number
     * box, an agent: the thumb and the numbers move together. A preview asks
     * for the mask at `value` (debounced, nothing stored); a commit stores it
     * (receipted) and then shows it.
     */
    setThreshold(entry, value, { commit = false, from = null } = {}) {
        const v = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(v)) return;
        entry.value = v;
        // Never echoed back into the control it came from.
        if (from !== "slider" && entry.slider && Math.abs(entry.slider.get() - v) > 1e-12) {
            entry.slider.set(v, { silent: true });
        }
        window.clearTimeout(entry.maskTimer);
        entry.maskTimer = null;
        if (commit) {
            this.commit(entry, v);
            return;
        }
        entry.maskTimer = window.setTimeout(() => {
            entry.maskTimer = null;
            this.loadMask(entry, v);
        }, QcBlurQc.PREVIEW_MS);
    }

    async commit(entry, value) {
        const seq = ++entry.setSeq;
        this.loadMask(entry, value);
        const answer = await this.api.blurSet({ channel: entry.channel, threshold: value })
            .catch(() => null);
        if (seq !== entry.setSeq) return;
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The threshold could not be saved");
            return;
        }
        if (entry.result) entry.result.threshold = answer.data.threshold;
        this.render();
    }

    /** Every channel whose threshold was set by hand, back to its automatic one. */
    async resetThresholds() {
        for (const entry of this.rows.values()) {
            if (entry.result?.threshold?.source === "user") await this.resetThreshold(entry);
        }
    }

    async resetThreshold(entry) {
        const seq = ++entry.setSeq;
        const answer = await this.api.blurSet({ channel: entry.channel, threshold: "auto" })
            .catch(() => null);
        if (seq !== entry.setSeq || !answer || !answer.ok) return;
        if (entry.result) entry.result.threshold = answer.data.threshold;
        this.setThreshold(entry, answer.data.threshold.value);
        this.render();
    }

    async pickColor(entry, hex) {
        if (entry.result) entry.result.color = hex;
        entry.heat = null;
        this.render();
        this.overlay?.invalidate?.();
        const answer = await this.api.blurSet({ channel: entry.channel, color: hex })
            .catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The colour could not be saved");
        }
    }

    // -- the overlays ------------------------------------------------------------------

    static bytes(base64) {
        const text = atob(base64 || "");
        const out = new Uint8Array(text.length);
        for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i);
        return out;
    }

    static rgb(hex) {
        const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
        const n = m ? parseInt(m[1], 16) : 0xf97316;
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    /** The channel's colour, its alpha growing with the score: sharp tissue
     *  stays as it is. */
    heatCanvas(entry) {
        if (entry.heat) return entry.heat;
        const { nx, ny } = entry.map.grid;
        const values = QcBlurQc.bytes(entry.map.blur);
        const evaluable = QcBlurQc.bytes(entry.map.evaluable);
        const [r, g, b] = QcBlurQc.rgb(entry.result && entry.result.color);
        const canvas = document.createElement("canvas");
        canvas.width = nx;
        canvas.height = ny;
        const context = canvas.getContext("2d");
        const picture = context.createImageData(nx, ny);
        for (let i = 0; i < nx * ny; i++) {
            if (!evaluable[i] || !values[i]) continue;
            const o = i * 4;
            picture.data[o] = r;
            picture.data[o + 1] = g;
            picture.data[o + 2] = b;
            picture.data[o + 3] = Math.round(255 * Math.min(1, (values[i] / 255) * 1.25));
        }
        context.putImageData(picture, 0, 0);
        entry.heat = canvas;
        return canvas;
    }

    regionPaths(entry) {
        if (entry.paths) return entry.paths;
        entry.paths = ((entry.evaluation && entry.evaluation.regions) || []).map((region) => {
            const path = new Path2D();
            const geometry = region.geometry || {};
            const polygons = geometry.type === "Polygon" ? [geometry.coordinates]
                : geometry.type === "MultiPolygon" ? geometry.coordinates : [];
            for (const polygon of polygons || []) {
                for (const ring of polygon || []) {
                    if (!ring || !ring.length) continue;
                    path.moveTo(ring[0][0], ring[0][1]);
                    for (let i = 1; i < ring.length; i++) path.lineTo(ring[i][0], ring[i][1]);
                    path.closePath();
                }
            }
            return path;
        });
        return entry.paths;
    }

    draw(opts) {
        if (this.muted || typeof document === "undefined" || !document.createElement) return;
        const context = opts.context;
        const zoom = opts.zoom || 1;
        const shown = [...this.rows.values()].filter((entry) => {
            const s = entry.result && entry.result.summary;
            return s && s.status === "ok" && this.visible(entry.channel);
        });
        // Every map under every outline, so no channel's map hides another's regions.
        if (this.view.heat) {
            for (const entry of shown) {
                if (!entry.map || entry.map.fingerprint !== entry.result.summary.fingerprint) continue;
                const { x0, y0, step, nx, ny } = entry.map.grid;
                context.save();
                context.globalAlpha = QcBlurQc.HEAT_OPACITY;
                // Smoothing is for the eye only; the tiles' values are the server's.
                context.imageSmoothingEnabled = true;
                context.imageSmoothingQuality = "high";
                context.drawImage(this.heatCanvas(entry), x0, y0, nx * step, ny * step);
                context.restore();
            }
        }
        if (!this.view.mask) return;
        for (const entry of shown) {
            const e = entry.evaluation;
            if (!e || e.fingerprint !== entry.result.summary.fingerprint) continue;
            const color = entry.result.color || "#f97316";
            context.save();
            context.fillStyle = color;
            context.strokeStyle = color;
            context.lineJoin = "round";
            context.lineWidth = QcBlurQc.STROKE / zoom;
            for (const path of this.regionPaths(entry)) {
                context.globalAlpha = QcBlurQc.FILL_ALPHA;
                context.fill(path, "evenodd");
                context.globalAlpha = 1;
                context.stroke(path);
            }
            context.restore();
        }
    }

    // -- the section -------------------------------------------------------------------

    static pct(value) {
        const n = Number(value) || 0;
        return n > 0 && n < 0.1 ? "<0.1%" : `${n.toFixed(1)}%`;
    }

    static label(channel) {
        return channel === "brightfield" ? "Brightfield" : channel;
    }

    scored() {
        return [...this.rows.values()].some((entry) => {
            const s = entry.result && entry.result.summary;
            return s && s.status === "ok";
        });
    }

    pending(channel) {
        return Boolean(this.job && (this.job.channels || []).includes(channel));
    }

    render() {
        const tool = this.el("qc_tool_blur");
        if (!tool) return;
        const s = this.status || {};
        const running = Boolean(this.job);
        const available = s.available !== false;
        tool.dataset.state = running ? "running" : this.anyResult() ? "done" : "idle";
        tool.classList.toggle("is-muted", this.muted);
        const run = this.el("qc_blur_run");
        if (run) {
            const stale = [...this.rows.values()].some((entry) => entry.result && entry.result.stale);
            run.setAttribute("aria-disabled", available && !running ? "false" : "true");
            run.title = !available ? "Blur QC needs an image"
                : running ? "Blur QC is running"
                    : stale ? "The image changed: run Blur QC again"
                        : this.anyResult() ? "Run Blur QC again on the listed channels"
                            : "Run Blur QC on the listed channels";
        }
        const heat = this.el("qc_blur_view_heat");
        if (heat) {
            heat.setAttribute("aria-pressed", this.view.heat && !this.muted ? "true" : "false");
            heat.disabled = !this.scored();
            heat.title = this.view.heat ? "Hide the blur map"
                : "Blur map: how blurred each part of the tissue is";
        }
        const eye = this.el("qc_blur_eye");
        if (eye) {
            eye.setAttribute("aria-pressed", this.muted ? "false" : "true");
            eye.title = this.muted ? "Show Blur QC" : "Hide everything Blur QC draws";
        }
        this.renderProgress(running);
        this.renderList();
        this.renderAdd(running);
        this.renderSummary(running);
        this.renderNote(running);
    }

    renderProgress(running) {
        const progress = this.el("qc_blur_progress");
        if (!progress) return;
        progress.hidden = !running;
        if (!running) return;
        const p = this.job.progress || {};
        const fraction = p.total ? Math.min(1, (p.done || 0) / p.total) : 0;
        const fill = this.el("qc_blur_fill");
        if (fill) fill.style.transform = `scaleX(${fraction.toFixed(3)})`;
        const phase = this.el("qc_blur_phase");
        if (phase) {
            const words = p.message && !["queued", "running"].includes(p.message)
                ? p.message : "Starting";
            phase.textContent = `${Math.round(100 * fraction)}% · ${words}`;
            phase.title = phase.textContent;
        }
    }

    renderList() {
        const list = this.el("qc_blur_list");
        if (!list) return;
        let previous = null;
        for (const channel of this.listed()) {
            const entry = this.rows.get(channel);
            if (!entry) continue;
            if (!entry.nodes) this.buildRow(entry);
            const row = entry.nodes.row;
            const expected = previous ? previous.nextSibling : list.firstChild;
            if (row !== expected) list.insertBefore(row, expected);
            previous = row;
            this.renderRow(entry);
        }
        if (this.draft) {
            const row = this.draft.nodes.row;
            const expected = previous ? previous.nextSibling : list.firstChild;
            if (row !== expected) list.insertBefore(row, expected);
            this.draft.select?.setOptions?.(this.unlisted());
        }
    }

    /** What a line's select offers: its own channel, then every unlisted
     *  one, the DNA channels first. */
    choices(channel = null) {
        return (channel ? [channel] : []).concat(this.unlisted());
    }

    describeChoice(name) {
        return ((this.status && this.status.candidates) || []).includes(name) ? "DNA" : "";
    }

    makeSelect(mount, channel, onChange) {
        if (typeof SearchableSelect === "undefined") return null;
        return new SearchableSelect(mount, {
            trigger: "button", options: this.choices(channel), value: channel || "",
            placeholder: "Search channels…", emptyLabel: "Select channel…",
            emptyText: "No channels match", ariaLabel: "Channel",
            describeOption: (name) => this.describeChoice(name), onChange,
        });
    }

    /** The empty line: a greyed dot, the select, and a remove. */
    buildDraft() {
        const row = document.createElement("div");
        row.className = "qc-line is-nested qc-blur-row is-draft";
        row.setAttribute("role", "listitem");
        const dot = document.createElement("span");
        dot.className = "qc-line-dot";
        dot.setAttribute("aria-hidden", "true");
        const name = document.createElement("div");
        name.className = "qc-blur-name";
        const fill = document.createElement("span");
        fill.className = "qc-line-fill";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "qc-line-action qc-blur-remove";
        remove.innerHTML = '<span class="fas fa-xmark"></span>';
        remove.title = "Remove this empty line";
        remove.setAttribute("aria-label", remove.title);
        remove.addEventListener("click", () => this.dropDraft());
        row.append(dot, name, fill, remove);
        this.draft = { nodes: { row, name, remove } };
        this.draft.select = this.makeSelect(name, null, (picked) => this.pickDraft(picked));
    }

    /** One channel's line: swatch · name · slider · eye · remove. */
    buildRow(entry) {
        const row = document.createElement("div");
        row.className = "qc-line is-nested qc-blur-row";
        row.setAttribute("role", "listitem");
        row.dataset.channel = entry.channel;
        const swatch = document.createElement("span");
        swatch.className = "qc-line-swatch";
        const name = document.createElement("div");
        name.className = "qc-blur-name";
        const mount = document.createElement("div");
        mount.className = "qc-blur-slider";
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = "qc-line-action qc-eye";
        eye.innerHTML = '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>';
        eye.addEventListener("click", () => this.toggleChannel(entry.channel));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "qc-line-action qc-blur-remove";
        remove.innerHTML = '<span class="fas fa-xmark"></span>';
        remove.addEventListener("click", () => this.removeChannel(entry.channel));
        row.append(swatch, name, mount, eye, remove);
        entry.nodes = { row, swatch, name, mount, eye, remove };
        if (entry.channel === "brightfield") {
            name.classList.add("qc-line-name", "is-static");
            name.textContent = "Brightfield";
        } else {
            entry.select = this.makeSelect(name, entry.channel, (picked) => {
                if (picked && picked !== entry.channel) this.swapChannel(entry.channel, picked);
            });
        }
        if (typeof PlexoraSlider !== "undefined") {
            entry.slider = new PlexoraSlider(mount, {
                min: 0, max: 1, step: 0.01, decimals: 2,
                display: (v) => Number(v).toFixed(2),
                value: entry.value ?? 0.35,
                ariaLabel: `${QcBlurQc.label(entry.channel)} blur threshold`,
                onInput: (value) => {
                    entry.holding = true;
                    this.setThreshold(entry, value, { commit: false, from: "slider" });
                },
                onChange: (value) => {
                    entry.holding = false;
                    this.setThreshold(entry, value, { commit: true, from: "slider" });
                },
            });
        }
        if (typeof ColorSwatchPicker !== "undefined") {
            entry.picker = new ColorSwatchPicker(swatch, {
                value: (entry.result && entry.result.color) || "#f97316",
                title: `${QcBlurQc.label(entry.channel)} colour`,
                onChange: (hex) => this.pickColor(entry, hex),
            });
            swatch.classList.add("has-picker");
        }
        return row;
    }

    renderRow(entry) {
        const { row, name, eye, remove } = entry.nodes;
        const r = entry.result || {};
        const s = r.summary;
        const color = r.color || "#f97316";
        const label = QcBlurQc.label(entry.channel);
        const on = this.visible(entry.channel) && !this.muted;
        row.style.setProperty("--qc-row-color", color);
        row.classList.toggle("is-hidden", !on);
        const brightfield = entry.channel === "brightfield";
        entry.select?.setOptions?.(this.choices(entry.channel));
        if (entry.picker && String(entry.picker.value || "").toLowerCase() !== color.toLowerCase()) {
            entry.picker.setValue(color);
        }
        const ok = Boolean(s && s.status === "ok");
        if (entry.slider) {
            entry.slider.setAccent(color);
            entry.slider.setDisabled(!ok);
            if (!entry.holding && !entry.maskTimer && entry.value != null
                    && Math.abs(entry.slider.get() - entry.value) > 1e-12) {
                entry.slider.set(entry.value, { silent: true });
            }
        }
        const e = entry.evaluation || r.evaluation;
        const t = r.threshold || {};
        let title = "";
        let flagged = false;
        if (this.pending(entry.channel)) {
            title = "Measuring";
        } else if (!s) {
            title = "Not measured yet: Play measures it";
        } else if (s.status !== "ok") {
            title = "Not enough evaluable tissue to judge focus";
        } else if (e) {
            const regions = Number(e.n_regions) || 0;
            const text = QcBlurQc.pct(e.blurred_pct);
            flagged = Number(e.blurred_pct) > 0 || Boolean(s.global_blur && s.global_blur.possible);
            title = `Blurred: ${text} of the evaluable tissue, in ${regions} region`
                + `${regions === 1 ? "" : "s"}, at ${Number(entry.value).toFixed(2)} `
                + (t.source === "user" ? "(set by hand" : "(automatic")
                + (Number.isFinite(Number(t.auto)) ? `; automatic ${Number(t.auto).toFixed(2)})` : ")")
                + (r.stale ? ". Stale: the image changed" : "");
        }
        row.classList.toggle("is-flagged", flagged);
        name.title = brightfield ? `Brightfield: the darkness of the stain is analysed. ${title}`
            : `${label} — ${title}`;
        eye.setAttribute("aria-pressed", on ? "true" : "false");
        eye.title = on ? `Hide ${label}'s blur` : `Show ${label}'s blur`;
        const last = this.listed().length <= 1;
        remove.hidden = brightfield;
        remove.disabled = last;
        remove.title = last ? "The last channel stays listed" : `Remove ${label}`;
        remove.setAttribute("aria-label", `Remove ${label}`);
    }

    renderAdd(running) {
        const add = this.el("qc_blur_add");
        if (!add) return;
        const s = this.status || {};
        const next = this.unlisted()[0];
        add.hidden = Boolean(s.brightfield);
        add.disabled = !this.status || s.available === false || !next
            || this.listed().length >= 12;
        add.title = next || !this.status ? "Add a channel" : "Every channel is listed";
        add.setAttribute("aria-label", add.title);
    }

    renderSummary(running) {
        const node = this.el("qc_blur_summary");
        if (!node) return;
        node.classList.remove("is-flagged");
        if (running) {
            const p = (this.job && this.job.progress) || {};
            node.textContent = p.total ? `${Math.round(100 * (p.done || 0) / p.total)}%` : "…";
            node.title = "Blur QC is running";
            return;
        }
        const measured = [...this.rows.values()].map((entry) => {
            const s = entry.result && entry.result.summary;
            const e = entry.evaluation || (entry.result && entry.result.evaluation);
            return s && s.status === "ok" && e ? [entry, Number(e.blurred_pct) || 0] : null;
        }).filter(Boolean);
        if (!measured.length) {
            node.textContent = "";
            node.title = "";
            return;
        }
        const [worst, pct] = measured.sort((a, b) => b[1] - a[1])[0];
        const global = measured.some(([entry]) => entry.result.summary.global_blur?.possible);
        node.textContent = QcBlurQc.pct(pct);
        node.classList.toggle("is-flagged", pct > 0 || global);
        node.title = measured.length > 1
            ? `Most blurred: ${QcBlurQc.label(worst.channel)}, ${QcBlurQc.pct(pct)} of the evaluable tissue`
            : `Blurred: ${QcBlurQc.pct(pct)} of the evaluable tissue`;
    }

    renderNote(running) {
        const note = this.el("qc_blur_status");
        if (!note) return;
        let text = "";
        let error = false;
        const entries = [...this.rows.values()].filter((entry) => entry.result && entry.result.summary);
        const names = (pick) => entries.filter(pick).map((entry) => QcBlurQc.label(entry.channel));
        const global = names((entry) => entry.result.summary.global_blur?.possible
                                         && entry.result.summary.status === "ok");
        const stale = names((entry) => entry.result.stale);
        if (this.error && !running) {
            text = this.error;
            error = true;
        } else if (!running && stale.length) {
            text = `Stale: the image changed (${stale.join(", ")})`;
        } else if (!running && global.length) {
            text = `Possible global blur on ${global.join(", ")}: even the sharpest tissue `
                + "has little fine detail";
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
        const regions = [...this.rows.values()].reduce((n, entry) => {
            const e = entry.evaluation;
            return n + (entry.result && entry.result.summary && e ? Number(e.n_regions) || 0 : 0);
        }, 0);
        const items = [
            { label: "Outline blurred regions", checked: Boolean(this.view.mask),
              hint: "Draw each channel's regions at its threshold",
              onSelect: () => this.setView("mask") },
            { label: "Automatic thresholds",
              disabled: ![...this.rows.values()].some((e) => e.result?.threshold?.source === "user"),
              hint: "Put back each channel's automatic threshold",
              onSelect: () => this.resetThresholds() },
            { label: "Run again", className: "is-sectioned",
              disabled: Boolean(this.job) || s.available === false,
              hint: "Measure every listed channel again from the pixels",
              onSelect: () => this.start({ force: true }) },
            { label: "First three DNA channels",
              disabled: !s.shown || s.brightfield,
              hint: "List the first three nuclear channels again",
              onSelect: () => this.setChannels("default") },
            { label: "Add blurred regions to ROI QC…", className: "is-sectioned",
              disabled: !this.scored() || !regions || Boolean(this.job),
              hint: regions ? `Write ${regions} region${regions === 1 ? "" : "s"} as `
                  + "QC: Blur / focus issue" : "Nothing is blurred at these thresholds",
              onSelect: () => this.writeRegions(regions) },
        ];
        QcTree.menu(anchor, items, { heading: "Blur QC", className: "qc-picker" });
    }

    async writeRegions(regions) {
        if (!regions) return;
        const words = `${regions} region${regions === 1 ? "" : "s"}`;
        const confirm = window.PlexoraConfirm;
        if (confirm && typeof confirm.ask === "function") {
            const sure = await confirm.ask({
                title: "Add blurred regions to ROI QC",
                body: `Write ${words}, each channel's at its own threshold, as QC: Out of `
                    + "focus regions? Blur regions written before are replaced; ones you "
                    + "edited or locked are kept.",
                confirm: "Add regions",
            });
            if (!sure) return;
        }
        const answer = await this.api.blurWriteRegions({}).catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The regions could not be written");
            return;
        }
        const written = (answer.data.written || []).length;
        const kept = (answer.data.kept || []).length;
        this.host.message(`${written} blurred region${written === 1 ? "" : "s"} added to ROI QC`
            + (kept ? `; ${kept} you edited kept` : ""));
        this.host.reload();
    }
}

window.QcBlurQc = QcBlurQc;
