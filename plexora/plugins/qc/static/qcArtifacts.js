/**
 * QcArtifactsQc - the Artifact Detector section of the QC panel.
 *
 * The analysis is the server's (`run_artifact_check`, a job): folds, tears,
 * debris and saturated pixels, found coarse to fine across every channel,
 * each a snug outline with a 0-1 score, the channels it shows in and the one
 * it shows most. This section starts it, follows the job, and shows the
 * objects.
 *
 * ONE LINE PER CATEGORY: its colour (core's swatch picker), its name, how
 * many objects it keeps, its own threshold slider and an eye. A slider is a
 * filter over the scores already computed: every object (with its outline)
 * is fetched once per result and filtered here as the thumb moves -- no
 * request, nothing re-detected; the release stores the threshold
 * (receipted, `set_artifact_check`), so an agent and the ROI write read the
 * same numbers. A threshold is global: one bar per category, across every
 * channel.
 *
 * THE CHANNELS only choose what is shown. "All channels" first: with no
 * channel listed its eye shows or hides everything; once channels are
 * listed (the + adds a line whose select opens at once, as the image
 * channels' + does) an object shows when any channel it is seen in is
 * listed with its eye on, and the All eye shows or hides every line. The
 * listed channels are the server's (so `get_state` and the panel agree);
 * the eyes are per-viewer conveniences in localStorage.
 *
 * A CLICK on an object (qcHover.js owns the click, and hands the objects
 * under the pointer here) selects it and puts its source channel on screen:
 * a slot already holding it is switched on, else one slot this section
 * keeps for itself (never Registration's pair, never the nuclear slot). The
 * overlay stays drawn. Several objects under the pointer: a small menu to
 * choose; a second click at the same spot steps to the next one.
 */
class QcArtifactsQc {

    constructor(ctx, api, host) {
        this.ctx = ctx;
        this.api = api;
        this.host = host;
        this.status = null;          // public_status from the server
        this.objects = [];           // every object, with its outline
        this.objectsFingerprint = null;
        this._objectsSeq = 0;
        this.paths = new Map();      // object id -> Path2D
        this.cats = new Map();       // category -> its row (see entryFor)
        this.lines = new Map();      // listed channel -> its line's nodes
        this.draft = null;           // the empty line + added
        this.job = null;
        this.error = null;
        this.overlay = null;
        this._pollTimer = null;
        this.view = this.loadView(); // {fill, all, hidden: [channel], hiddenCats: [key]}
        this.muted = false;
        this.selectedId = null;
        this.lastClick = null;       // {x, y, ids, index}
        this.artifactSlot = null;    // {index, name}: the slot this section placed a channel in
    }

    static get POLL_MS() { return 750; }
    static get FILL_ALPHA() { return 0.18; }
    static get STROKE() { return 1.6; }
    static get SELECTED_STROKE() { return 2.6; }
    static get MAX_LINES() { return 12; }
    /** A second click this close (screen pixels) is "the same spot". */
    static get SAME_SPOT_PX() { return 4; }
    static get LABELS() {
        return { fold: "Fold", tear: "Tear", debris: "Debris", saturation: "Saturation" };
    }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        this.el("qc_art_run")?.addEventListener("click", () => this.start());
        this.el("qc_art_cancel")?.addEventListener("click", () => this.cancel());
        this.el("qc_art_eye")?.addEventListener("click", () => this.setMuted(!this.muted));
        this.el("qc_art_add")?.addEventListener("click", () => this.addChannel());
        this.el("qc_art_all_eye")?.addEventListener("click", () => this.toggleAll());
        this.el("qc_art_edit")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openMenu(event.currentTarget);
        });
        this.overlay = this.ctx.layers?.addOverlay?.({
            id: "artifacts",
            kind: "shapes",
            draw: (opts) => this.draw(opts),
            hitTest: (x, y, opts) => this.hitTest(x, y, opts),
        }) || null;
    }

    destroy() {
        window.clearTimeout(this._pollTimer);
        this._pollTimer = null;
        for (const entry of this.cats.values()) this.dropEntry(entry);
        this.cats.clear();
        for (const line of this.lines.values()) this.dropLine(line);
        this.lines.clear();
        this.dropDraft();
        this.overlay?.remove?.();
        this.overlay = null;
        this._anchor?.remove?.();
        this._anchor = null;
    }

    onShow() {}

    invalidate() {
        this.overlay?.invalidate?.();
    }

    // -- per-viewer state --------------------------------------------------------

    viewKey() {
        return `plexora.qc.artifacts.view.${this.ctx.datasource}`;
    }

    loadView() {
        const view = { fill: true, all: true, hidden: [], hiddenCats: [] };
        try {
            const raw = JSON.parse(window.localStorage.getItem(this.viewKey()) || "{}");
            if (typeof raw.fill === "boolean") view.fill = raw.fill;
            if (typeof raw.all === "boolean") view.all = raw.all;
            if (Array.isArray(raw.hidden)) view.hidden = raw.hidden.map(String).slice(0, 50);
            if (Array.isArray(raw.hiddenCats)) view.hiddenCats = raw.hiddenCats.map(String).slice(0, 8);
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

    setMuted(muted) {
        this.muted = Boolean(muted);
        this.render();
        this.invalidate();
    }

    setFill() {
        this.view.fill = this.muted || !this.view.fill;
        this.muted = false;
        this.saveView();
        this.render();
        this.invalidate();
    }

    // -- what is shown -------------------------------------------------------------

    listed() {
        return (this.status && this.status.shown) || [];
    }

    /** An object's channels: the ones it is seen in, else its source. */
    static channelsOf(o) {
        return (o.channels && o.channels.length) ? o.channels
            : (o.source_channel ? [o.source_channel] : []);
    }

    threshold(key) {
        const entry = this.cats.get(key);
        if (entry && entry.value != null) return entry.value;
        return 0.5;
    }

    retained(o) {
        return Number(o.score) >= this.threshold(o.category);
    }

    channelsAllow(o) {
        const listed = this.listed();
        if (!listed.length) return Boolean(this.view.all);
        const hidden = new Set(this.view.hidden);
        return QcArtifactsQc.channelsOf(o).some((c) => listed.includes(c) && !hidden.has(c));
    }

    /** Whether an object is drawn: the section on, its category's eye on,
     *  at or above its category's bar, and seen in a channel shown. */
    visible(o) {
        return !this.muted && !this.view.hiddenCats.includes(o.category)
            && this.retained(o) && this.channelsAllow(o);
    }

    channelOn(channel) {
        return !this.view.hidden.includes(channel);
    }

    allOn() {
        const listed = this.listed();
        if (!listed.length) return Boolean(this.view.all);
        return listed.every((c) => this.channelOn(c));
    }

    toggleChannel(channel) {
        const hidden = new Set(this.view.hidden);
        if (hidden.has(channel) || this.muted) hidden.delete(channel);
        else hidden.add(channel);
        this.view.hidden = [...hidden];
        this.muted = false;
        this.saveView();
        this.render();
        this.invalidate();
    }

    /** "All channels"' eye: with no line, everything; with lines, every line. */
    toggleAll() {
        const listed = this.listed();
        if (!listed.length) {
            this.view.all = this.muted || !this.view.all;
        } else if (this.allOn() && !this.muted) {
            this.view.hidden = [...new Set([...this.view.hidden, ...listed])];
        } else {
            this.view.hidden = this.view.hidden.filter((c) => !listed.includes(c));
        }
        this.muted = false;
        this.saveView();
        this.render();
        this.invalidate();
    }

    toggleCategory(key) {
        const hidden = new Set(this.view.hiddenCats);
        if (hidden.has(key) || this.muted) hidden.delete(key);
        else hidden.add(key);
        this.view.hiddenCats = [...hidden];
        this.muted = false;
        this.saveView();
        this.render();
        this.invalidate();
    }

    /** Retained objects (thresholds only, never the eyes): total, per
     *  category and per channel. */
    counts() {
        const out = { total: 0, byCat: {}, byChannel: {} };
        for (const o of this.objects) {
            if (!this.retained(o)) continue;
            out.total += 1;
            out.byCat[o.category] = (out.byCat[o.category] || 0) + 1;
            for (const c of QcArtifactsQc.channelsOf(o)) out.byChannel[c] = (out.byChannel[c] || 0) + 1;
        }
        return out;
    }

    /** What `get_state` reports for this section. */
    summary() {
        if (!this.status) return this.job ? { running: true, progress: this.job.progress || null } : null;
        const results = this.status.results || null;
        const counts = this.counts();
        const categories = [...this.cats.values()].map((entry) => {
            const c = entry.cat || {};
            const t = c.threshold || {};
            return { key: entry.key, class: c.class || null, color: c.color || null,
                     threshold: entry.value, threshold_source: t.source || null,
                     auto_threshold: t.auto ?? null, n_retained: counts.byCat[entry.key] || 0,
                     n_total: c.n_total ?? null, area_um2: c.area_um2 ?? null,
                     shown: !this.view.hiddenCats.includes(entry.key) };
        });
        return { running: Boolean(this.job), status: results ? results.status : null,
                 fingerprint: results ? results.fingerprint : null,
                 stale: Boolean(results && results.stale), shown: this.listed(),
                 hidden_channels: this.view.hidden.filter((c) => this.listed().includes(c)),
                 all: Boolean(this.view.all), fill: Boolean(this.view.fill), muted: this.muted,
                 selected: this.selectedId, categories };
    }

    // -- the server ----------------------------------------------------------------

    /** A public_status from /state or /artifacts. */
    adopt(status) {
        if (!status || status.error) {
            this.render();
            return;
        }
        this.status = status;
        for (const cat of status.categories || []) {
            const entry = this.entryFor(cat.key);
            entry.cat = cat;
            // The server's threshold, unless a hand is on this slider.
            if (!entry.holding && cat.threshold) entry.value = Number(cat.threshold.value);
        }
        const results = status.results;
        if (!results) {
            this.objects = [];
            this.objectsFingerprint = null;
            this.paths.clear();
            this.selectedId = null;
        } else if (results.fingerprint !== this.objectsFingerprint) {
            this.loadObjects(results.fingerprint);
        }
        if (status.job && !this.job) this.follow(status.job.job_id);
        this.render();
        this.invalidate();
    }

    entryFor(key) {
        let entry = this.cats.get(key);
        if (!entry) {
            entry = { key, cat: null, value: null, holding: false, setSeq: 0, nodes: null,
                      slider: null, picker: null };
            this.cats.set(key, entry);
        }
        return entry;
    }

    dropEntry(entry) {
        entry.slider?.destroy?.();
        entry.picker?.destroy?.();
        entry.nodes?.row?.remove?.();
        entry.slider = null;
        entry.picker = null;
        entry.nodes = null;
    }

    async loadObjects(fingerprint) {
        const seq = ++this._objectsSeq;
        const answer = await this.api.artifactsObjects().catch(() => null);
        if (seq !== this._objectsSeq) return;
        if (!answer || !answer.ok || !answer.data.available) return;
        this.objects = answer.data.objects || [];
        this.objectsFingerprint = answer.data.fingerprint || fingerprint;
        this.paths.clear();
        if (this.selectedId && !this.objects.some((o) => o.id === this.selectedId)) {
            this.selectedId = null;
        }
        this.render();
        this.invalidate();
    }

    async refresh() {
        const answer = await this.api.artifacts().catch(() => null);
        if (answer && answer.ok) this.adopt(answer.data.artifacts_qc);
    }

    /** `qc.artifacts*` from the agent bridge: a run finished, a setting
     *  changed or the result was cleared, here or anywhere else. */
    onEvent() {
        this.refresh();
    }

    async start(options = {}) {
        if (this.job) return;
        if (this.status && this.status.available === false) {
            this.host.message(this.status.available_message || "The Artifact Detector cannot run here");
            return;
        }
        this.error = null;
        const body = {};
        if (options.force) body.force = true;
        const answer = await this.api.artifactsRun(body).catch(() => null);
        if (!answer || !answer.ok) {
            this.error = (answer && answer.data.error && answer.data.error.message)
                || "The Artifact Detector could not start";
            this.render();
            return;
        }
        this.follow(answer.data.job_id);
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
                this._pollTimer = window.setTimeout(tick, QcArtifactsQc.POLL_MS * 2);
                return;
            }
            const job = answer.data.job;
            this.job = { ...job, job_id: jobId };
            if (job.status === "done") {
                this.job = null;
                await this.refresh();
                return;
            }
            if (job.status === "failed" || job.status === "cancelled") {
                this.job = null;
                this.error = job.status === "cancelled" ? null
                    : ((job.error && job.error.message) || "The Artifact Detector failed");
                if (job.status === "cancelled") this.host.message("Artifact Detector cancelled");
                this.render();
                return;
            }
            this.render();
            this._pollTimer = window.setTimeout(tick, QcArtifactsQc.POLL_MS);
        };
        this._pollTimer = window.setTimeout(tick, 150);
    }

    async cancel() {
        if (!this.job) return;
        const answer = await this.api.jobCancel(this.job.job_id).catch(() => null);
        if (answer && !answer.ok) {
            this.host.message((answer.data.error && answer.data.error.message)
                || "The Artifact Detector could not be cancelled");
        }
    }

    // -- the thresholds ------------------------------------------------------------

    /**
     * The one path every threshold change takes -- the slider, its number
     * box, an agent. The objects are filtered here at once (no request);
     * a commit also stores the threshold (receipted).
     */
    setThreshold(entry, value, { commit = false, from = null } = {}) {
        const v = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(v)) return;
        entry.value = v;
        // Never echoed back into the control it came from.
        if (from !== "slider" && entry.slider && Math.abs(entry.slider.get() - v) > 1e-12) {
            entry.slider.set(v, { silent: true });
        }
        if (this.selectedId) {
            const selected = this.objects.find((o) => o.id === this.selectedId);
            if (selected && selected.category === entry.key && !this.retained(selected)) {
                this.selectedId = null;
            }
        }
        this.render();
        this.invalidate();
        if (commit) this.commit(entry, v);
    }

    async commit(entry, value) {
        const seq = ++entry.setSeq;
        const answer = await this.api.artifactsSet({ category: entry.key, threshold: value })
            .catch(() => null);
        if (seq !== entry.setSeq) return;
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The threshold could not be saved");
            return;
        }
        if (entry.cat) entry.cat.threshold = answer.data.threshold;
        this.render();
    }

    async resetThresholds() {
        for (const entry of this.cats.values()) {
            if (entry.cat?.threshold?.source && entry.cat.threshold.source !== "auto") {
                await this.resetThreshold(entry);
            }
        }
    }

    async resetThreshold(entry) {
        const seq = ++entry.setSeq;
        const answer = await this.api.artifactsSet({ category: entry.key, threshold: "auto" })
            .catch(() => null);
        if (seq !== entry.setSeq || !answer || !answer.ok) return;
        if (entry.cat) entry.cat.threshold = answer.data.threshold;
        this.setThreshold(entry, answer.data.threshold.value);
    }

    async pickColor(entry, hex) {
        if (entry.cat) entry.cat.color = hex;
        this.render();
        this.invalidate();
        const answer = await this.api.artifactsSet({ category: entry.key, color: hex })
            .catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The colour could not be saved");
        }
    }

    colorOf(key) {
        const entry = this.cats.get(key);
        return (entry && entry.cat && entry.cat.color) || "#ef4444";
    }

    // -- the channels listed -------------------------------------------------------

    unlisted() {
        const shown = new Set(this.listed());
        return ((this.status && this.status.channels) || []).filter((n) => !shown.has(n));
    }

    async setChannels(channels) {
        const answer = await this.api.artifactsSet({ channels }).catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The channels could not be changed");
            return;
        }
        await this.refresh();
    }

    addChannel() {
        if (!this.unlisted().length || this.listed().length >= QcArtifactsQc.MAX_LINES) return;
        if (!this.draft) this.buildDraft();
        this.render();
        this.draft.select?.open?.(true);
    }

    pickDraft(name) {
        if (!name) return;
        this.dropDraft();
        // A channel picked is shown, whatever its eye said before.
        this.view.hidden = this.view.hidden.filter((c) => c !== name);
        this.saveView();
        this.setChannels([...this.listed(), name]);
    }

    dropDraft() {
        if (!this.draft) return;
        this.draft.select?.destroy?.();
        this.draft.nodes.row.remove();
        this.draft = null;
    }

    /** The last line removed is every channel again. */
    removeChannel(channel) {
        const rest = this.listed().filter((n) => n !== channel);
        this.setChannels(rest.length ? rest : "default");
    }

    swapChannel(channel, other) {
        this.setChannels(this.listed().map((n) => (n === channel ? other : n)));
    }

    dropLine(line) {
        line.select?.destroy?.();
        line.row?.remove?.();
    }

    // -- the overlay -----------------------------------------------------------------

    pathFor(o) {
        const cached = this.paths.get(o.id);
        if (cached) return cached;
        const path = new Path2D();
        const geometry = o.geometry || {};
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
        this.paths.set(o.id, path);
        return path;
    }

    shownObjects() {
        if (this.muted) return [];
        return this.objects.filter((o) => this.visible(o));
    }

    draw(opts) {
        if (this.muted || typeof Path2D === "undefined") return;
        const context = opts.context;
        const zoom = opts.zoom || 1;
        const bounds = opts.bounds || null;
        const shown = this.shownObjects();
        let selected = null;
        for (const key of Object.keys(QcArtifactsQc.LABELS)) {
            const mine = shown.filter((o) => o.category === key);
            if (!mine.length) continue;
            const color = this.colorOf(key);
            context.save();
            context.fillStyle = color;
            context.strokeStyle = color;
            context.lineJoin = "round";
            context.lineWidth = QcArtifactsQc.STROKE / zoom;
            for (const o of mine) {
                if (o.id === this.selectedId) {
                    selected = o;
                    continue;
                }
                const b = o.bbox;
                if (bounds && b && (b[2] < bounds.x0 || b[0] > bounds.x1
                                    || b[3] < bounds.y0 || b[1] > bounds.y1)) continue;
                const path = this.pathFor(o);
                if (this.view.fill) {
                    context.globalAlpha = QcArtifactsQc.FILL_ALPHA;
                    context.fill(path, "evenodd");
                }
                context.globalAlpha = 1;
                context.stroke(path);
            }
            context.restore();
        }
        if (selected) {
            // The selected object last: a white ring under its own colour.
            const path = this.pathFor(selected);
            const color = this.colorOf(selected.category);
            context.save();
            context.lineJoin = "round";
            context.fillStyle = color;
            context.globalAlpha = QcArtifactsQc.FILL_ALPHA * 1.6;
            context.fill(path, "evenodd");
            context.globalAlpha = 1;
            context.strokeStyle = "#ffffff";
            context.lineWidth = (QcArtifactsQc.SELECTED_STROKE + 2) / zoom;
            context.stroke(path);
            context.strokeStyle = color;
            context.lineWidth = QcArtifactsQc.SELECTED_STROKE / zoom;
            context.stroke(path);
            context.restore();
        }
    }

    /**
     * Every shown object at image pixel (x, y), topmost first (the selected
     * one, then the others in drawing order reversed): `[{id, region, edge}]`
     * -- `edge` when the point is on the outline (within `tolerance` image
     * pixels) rather than inside -- or null.
     */
    hitTest(x, y, opts) {
        const shown = this.shownObjects();
        if (!shown.length) return null;
        const scratch = typeof QcRegionOverlay !== "undefined" ? QcRegionOverlay.scratch() : null;
        if (!scratch) return null;
        const tolerance = Math.max(0, Number(opts?.tolerance) || 0);
        const order = Object.keys(QcArtifactsQc.LABELS);
        const drawn = shown.slice().sort((a, b) => order.indexOf(a.category) - order.indexOf(b.category));
        const hits = [];
        for (let i = drawn.length - 1; i >= 0; i--) {
            const o = drawn[i];
            const b = o.bbox;
            if (b && (x < b[0] - tolerance || x > b[2] + tolerance
                      || y < b[1] - tolerance || y > b[3] + tolerance)) continue;
            const path = this.pathFor(o);
            if (scratch.isPointInPath(path, x, y, "evenodd")) {
                hits.push({ id: o.id, region: o, edge: false });
                continue;
            }
            if (tolerance > 0) {
                scratch.lineWidth = 2 * tolerance;
                if (scratch.isPointInStroke(path, x, y)) hits.push({ id: o.id, region: o, edge: true });
            }
        }
        if (!hits.length) return null;
        const first = hits.findIndex((h) => h.id === this.selectedId);
        if (first > 0) hits.unshift(...hits.splice(first, 1));
        return hits;
    }

    // -- a click on the image ------------------------------------------------------------

    static itemLabel(o) {
        const words = QcArtifactsQc.LABELS[o.category] || o.category;
        const channel = o.source_channel || QcArtifactsQc.channelsOf(o)[0] || "";
        return [words, channel, Number(o.score).toFixed(2)].filter(Boolean).join(" · ");
    }

    /** qcHover.js: the objects under a click (`hits`, topmost first) at `at`
     *  ({x, y, scale, anchor}). One: selected. The same spot again: the next
     *  one. Several: a menu to choose from. */
    onClick(hits, at) {
        if (!hits || !hits.length) return;
        const ids = hits.map((h) => h.id);
        const last = this.lastClick;
        const near = last && at && Math.hypot(at.x - last.x, at.y - last.y)
            <= QcArtifactsQc.SAME_SPOT_PX * (at.scale || 1);
        if (hits.length === 1) {
            this.lastClick = at ? { x: at.x, y: at.y, ids, index: 0 } : null;
            this.select(hits[0].region);
            return;
        }
        if (near && last.ids.join("|") === ids.join("|")) {
            const index = (last.index + 1) % hits.length;
            this.lastClick = { ...last, index };
            this.select(hits[index].region);
            return;
        }
        this.lastClick = at ? { x: at.x, y: at.y, ids, index: -1 } : null;
        this.chooseAmong(hits, at && at.anchor);
    }

    anchorAt(point) {
        if (!this._anchor) {
            const anchor = document.createElement("div");
            anchor.className = "qc-art-anchor";
            anchor.setAttribute("aria-hidden", "true");
            document.body.appendChild(anchor);
            this._anchor = anchor;
        }
        if (point) {
            this._anchor.style.left = `${Math.round(point.x)}px`;
            this._anchor.style.top = `${Math.round(point.y)}px`;
        }
        return this._anchor;
    }

    chooseAmong(hits, point) {
        if (typeof QcTree === "undefined" || !QcTree.menu) {
            this.select(hits[0].region);
            return;
        }
        const items = hits.map((h, index) => ({
            label: QcArtifactsQc.itemLabel(h.region),
            checked: h.id === this.selectedId,
            hint: `Area ${QcArtifactsQc.area(h.region.area_um2)}`,
            onSelect: () => {
                if (this.lastClick) this.lastClick.index = index;
                this.select(h.region);
            },
        }));
        QcTree.menu(this.anchorAt(point), items, { heading: "Artifacts here",
                                                   className: "qc-picker qc-art-hits" });
    }

    select(o) {
        if (!o) return;
        this.selectedId = o.id;
        this.activateChannel(o.source_channel || QcArtifactsQc.channelsOf(o)[0]);
        this.render();
        this.invalidate();
    }

    /**
     * Put `name` on screen in the image channels: a slot already holding it
     * is switched on (if it is off); otherwise one slot this section keeps
     * for itself -- chosen once: not Registration's pair while it is on, not
     * the nuclear channel's, an unused or switched-off slot first -- holds
     * it. A slot the user has since given another channel is theirs again.
     */
    activateChannel(name) {
        const panel = window.__plexora?.viewerSidebar;
        if (!name || !panel || !Array.isArray(panel.channelSlots)
                || typeof panel.setSlotMarker !== "function") return false;
        const slots = panel.channelSlots;
        const held = slots.findIndex((slot) => slot && slot.name === name && slot.visible !== false);
        if (held >= 0) {
            if (!slots[held].enabled) this.place(panel, held, name);
            return true;
        }
        if (this.artifactSlot) {
            const slot = slots[this.artifactSlot.index];
            if (!slot || slot.name !== this.artifactSlot.name) this.artifactSlot = null;
        }
        let index = this.artifactSlot ? this.artifactSlot.index : this.chooseSlot(panel);
        if (index == null || index < 0) return false;
        this.place(panel, index, name);
        this.artifactSlot = { index, name };
        return true;
    }

    chooseSlot(panel) {
        const slots = panel.channelSlots;
        const nuclear = this.host?.nuclearChannel?.() || (this.status && this.status.nuclear) || null;
        const reserved = new Set();
        if (this.host?.registration?.active) {
            reserved.add(0);
            reserved.add(1);
        }
        slots.forEach((slot, i) => {
            if (slot && nuclear && slot.name === nuclear && slot.enabled) reserved.add(i);
        });
        const free = slots.map((slot, i) => [slot, i]).filter(([slot, i]) => slot && !reserved.has(i));
        const unused = free.find(([slot]) => slot.visible === false || !slot.name);
        if (unused) return unused[1];
        const off = free.find(([slot]) => !slot.enabled);
        if (off) return off[1];
        if (typeof panel.createAdditionalSlot === "function") {
            const used = slots.filter((slot) => slot && slot.name).map((slot) => slot.name);
            const made = panel.createAdditionalSlot(used);
            if (made) return made.index;
        }
        const later = free.find(([, i]) => i > 0);
        if (later) return later[1];
        return free.length ? free[0][1] : 0;
    }

    place(panel, index, name) {
        panel.suspendPersistence?.();
        try {
            panel.setSlotMarker(index, name, { enable: true, keepColor: true, reveal: true });
        } finally {
            panel.resumePersistence?.();
        }
    }

    // -- the hover card --------------------------------------------------------------

    static area(um2) {
        const n = Number(um2) || 0;
        if (n >= 1e6) return `${(n / 1e6).toFixed(2)} mm²`;
        if (n >= 1000) return `${Math.round(n).toLocaleString("en-US")} µm²`;
        return `${n.toFixed(0)} µm²`;
    }

    /** The card for the objects under the pointer (topmost first). */
    cardModel(hits) {
        if (!hits || !hits.length) return null;
        const o = hits[0].region;
        const words = QcArtifactsQc.LABELS[o.category] || o.category;
        const channels = QcArtifactsQc.channelsOf(o);
        const source = o.source_channel || channels[0] || "";
        const others = hits.slice(1, 4).map((h) => `${QcArtifactsQc.LABELS[h.region.category]
            || h.region.category} ${Number(h.region.score).toFixed(2)}`);
        const rows = [];
        if (channels.length) {
            rows.push(["Channels", channels.slice(0, 6).join(", ")
                + (channels.length > 6 ? ` +${channels.length - 6}` : "")]);
        }
        rows.push(["Area", QcArtifactsQc.area(o.area_um2)]);
        rows.push(["Threshold", this.threshold(o.category).toFixed(2)]);
        const metrics = o.metrics || {};
        if (metrics.shape) rows.push(["Shape", metrics.shape]);
        if (!o.refined) rows.push(["Outline", "coarse"]);
        return {
            chip: { color: this.colorOf(o.category), words: "Artifact Detector" },
            title: words,
            status: { words: `Score ${Number(o.score).toFixed(2)}`, tone: "muted" },
            lead: [source ? `On ${source}` : "", others.length ? `also here: ${others.join(", ")}` : ""]
                .filter(Boolean).join(" · "),
            rows,
            footer: hits.length > 1 ? `Click to choose among ${hits.length} regions here`
                : "Click to show its channel",
        };
    }

    // -- the section -------------------------------------------------------------------

    render() {
        const tool = this.el("qc_tool_art");
        if (!tool) return;
        const s = this.status || {};
        const running = Boolean(this.job);
        const available = s.available !== false;
        const results = s.results || null;
        tool.dataset.state = running ? "running" : results ? "done" : "idle";
        tool.classList.toggle("is-muted", this.muted);
        const run = this.el("qc_art_run");
        if (run) {
            run.setAttribute("aria-disabled", available && !running ? "false" : "true");
            run.title = !available ? `The Artifact Detector ${s.available_message || "cannot run here"}`
                : running ? "The Artifact Detector is running"
                    : results && results.stale ? "The image changed: run the Artifact Detector again"
                        : results ? "Run the Artifact Detector again"
                            : "Find folds, tears, debris and saturation in every channel";
        }
        const eye = this.el("qc_art_eye");
        if (eye) {
            eye.setAttribute("aria-pressed", this.muted ? "false" : "true");
            eye.title = this.muted ? "Show the Artifact Detector"
                : "Hide everything the Artifact Detector draws";
        }
        const counts = this.counts();
        this.renderProgress(running);
        this.renderCategories(counts, Boolean(results));
        this.renderChannels(counts);
        this.renderAdd();
        this.renderSummary(running, counts, results);
        this.renderNote(running, results);
    }

    renderProgress(running) {
        const progress = this.el("qc_art_progress");
        if (!progress) return;
        progress.hidden = !running;
        if (!running) return;
        const p = this.job.progress || {};
        const fraction = p.total ? Math.min(1, (p.done || 0) / p.total) : 0;
        const fill = this.el("qc_art_fill");
        if (fill) fill.style.transform = `scaleX(${fraction.toFixed(3)})`;
        const phase = this.el("qc_art_phase");
        if (phase) {
            const words = p.message && !["queued", "running"].includes(p.message)
                ? p.message : "Starting";
            phase.textContent = `${Math.round(100 * fraction)}% · ${words}`;
            phase.title = phase.textContent;
        }
    }

    renderCategories(counts, measured) {
        const list = this.el("qc_art_cats");
        if (!list) return;
        let previous = null;
        for (const cat of (this.status && this.status.categories) || []) {
            const entry = this.cats.get(cat.key);
            if (!entry) continue;
            if (!entry.nodes) this.buildCategory(entry);
            const row = entry.nodes.row;
            const expected = previous ? previous.nextSibling : list.firstChild;
            if (row !== expected) list.insertBefore(row, expected);
            previous = row;
            this.renderCategory(entry, counts, measured);
        }
    }

    buildCategory(entry) {
        const row = document.createElement("div");
        row.className = "qc-line is-nested qc-art-row qc-art-cat";
        row.setAttribute("role", "listitem");
        row.dataset.key = entry.key;
        const swatch = document.createElement("span");
        swatch.className = "qc-line-swatch";
        const name = document.createElement("span");
        name.className = "qc-line-name is-static qc-art-cat-name";
        name.textContent = QcArtifactsQc.LABELS[entry.key] || entry.key;
        const count = document.createElement("span");
        count.className = "qc-art-count";
        const mount = document.createElement("div");
        mount.className = "qc-art-slider";
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = "qc-line-action qc-eye";
        eye.innerHTML = '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>';
        eye.addEventListener("click", () => this.toggleCategory(entry.key));
        row.append(swatch, name, count, mount, eye);
        entry.nodes = { row, swatch, name, count, mount, eye };
        const label = QcArtifactsQc.LABELS[entry.key] || entry.key;
        if (typeof PlexoraSlider !== "undefined") {
            entry.slider = new PlexoraSlider(mount, {
                min: 0, max: 1, step: 0.01, decimals: 2,
                display: (v) => Number(v).toFixed(2),
                value: entry.value ?? 0.5,
                ariaLabel: `${label} threshold`,
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
                value: this.colorOf(entry.key),
                title: `${label} colour`,
                onChange: (hex) => this.pickColor(entry, hex),
            });
            swatch.classList.add("has-picker");
        }
    }

    renderCategory(entry, counts, measured) {
        const { row, name, count, eye } = entry.nodes;
        const cat = entry.cat || {};
        const color = this.colorOf(entry.key);
        const label = QcArtifactsQc.LABELS[entry.key] || entry.key;
        const on = !this.view.hiddenCats.includes(entry.key) && !this.muted;
        const enabled = cat.enabled !== false;
        row.style.setProperty("--qc-row-color", color);
        row.classList.toggle("is-hidden", !on);
        if (entry.picker && String(entry.picker.value || "").toLowerCase() !== color.toLowerCase()) {
            entry.picker.setValue(color);
        }
        const kept = counts.byCat[entry.key] || 0;
        const total = this.objects.filter((o) => o.category === entry.key).length;
        count.textContent = measured && enabled ? String(kept) : "";
        count.title = measured && enabled ? `${kept} of ${total} kept at this threshold` : "";
        if (entry.slider) {
            entry.slider.setAccent?.(color);
            entry.slider.setDisabled?.(!measured || !enabled || !on);
            if (!entry.holding && entry.value != null
                    && Math.abs(entry.slider.get() - entry.value) > 1e-12) {
                entry.slider.set(entry.value, { silent: true });
            }
        }
        const t = cat.threshold || {};
        row.classList.toggle("is-flagged", measured && kept > 0);
        name.title = `${label}: ${cat.words || ""}`
            + (measured ? ` — kept at ${Number(entry.value ?? 0.5).toFixed(2)} (`
                + (t.source === "user" ? "set by hand" : t.source === "user_relative"
                    ? "moved in steps" : "automatic") + ")" : "")
            + (enabled ? "" : " — not looked for in this run");
        eye.setAttribute("aria-pressed", on ? "true" : "false");
        eye.title = on ? `Hide ${label.toLowerCase()} regions` : `Show ${label.toLowerCase()} regions`;
    }

    renderChannels(counts) {
        const list = this.el("qc_art_channels");
        const all = this.el("qc_art_all");
        if (!list || !all) return;
        const listed = this.listed();
        for (const [channel, line] of [...this.lines.entries()]) {
            if (!listed.includes(channel)) {
                this.dropLine(line);
                this.lines.delete(channel);
            }
        }
        let previous = all;
        for (const channel of listed) {
            let line = this.lines.get(channel);
            if (!line) {
                line = this.buildLine(channel);
                this.lines.set(channel, line);
            }
            if (previous.nextSibling !== line.row) list.insertBefore(line.row, previous.nextSibling);
            previous = line.row;
            this.renderLine(channel, line, counts);
        }
        if (this.draft) {
            const row = this.draft.nodes.row;
            if (previous.nextSibling !== row) list.insertBefore(row, previous.nextSibling);
            this.draft.select?.setOptions?.(this.unlisted());
        }
        const allOn = this.allOn() && !this.muted;
        all.classList.toggle("is-hidden", !allOn);
        const allCount = this.el("qc_art_all_count");
        if (allCount) {
            allCount.textContent = this.objects.length ? String(counts.total) : "";
            allCount.title = this.objects.length ? `${counts.total} artifacts kept` : "";
        }
        const allEye = this.el("qc_art_all_eye");
        if (allEye) {
            allEye.setAttribute("aria-pressed", allOn ? "true" : "false");
            allEye.title = allOn ? "Hide all" : "Show all";
            allEye.setAttribute("aria-label", allEye.title);
        }
    }

    makeSelect(mount, channel, onChange) {
        if (typeof SearchableSelect === "undefined") return null;
        return new SearchableSelect(mount, {
            trigger: "button", options: (channel ? [channel] : []).concat(this.unlisted()),
            value: channel || "", placeholder: "Search channels…", emptyLabel: "Select channel…",
            emptyText: "No channels match", ariaLabel: "Channel", onChange,
        });
    }

    buildDraft() {
        const row = document.createElement("div");
        row.className = "qc-line is-nested qc-art-row is-draft";
        row.setAttribute("role", "listitem");
        const dot = document.createElement("span");
        dot.className = "qc-line-dot";
        dot.setAttribute("aria-hidden", "true");
        const name = document.createElement("div");
        name.className = "qc-blur-name qc-art-name";
        const fill = document.createElement("span");
        fill.className = "qc-line-fill";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "qc-line-action qc-art-remove";
        remove.innerHTML = '<span class="fas fa-xmark"></span>';
        remove.title = "Remove this empty line";
        remove.setAttribute("aria-label", remove.title);
        remove.addEventListener("click", () => this.dropDraft());
        row.append(dot, name, fill, remove);
        this.draft = { nodes: { row, name, remove } };
        this.draft.select = this.makeSelect(name, null, (picked) => this.pickDraft(picked));
    }

    buildLine(channel) {
        const row = document.createElement("div");
        row.className = "qc-line is-nested qc-art-row qc-art-chan";
        row.setAttribute("role", "listitem");
        row.dataset.channel = channel;
        const dot = document.createElement("span");
        dot.className = "qc-line-dot";
        dot.setAttribute("aria-hidden", "true");
        const name = document.createElement("div");
        name.className = "qc-blur-name qc-art-name";
        const count = document.createElement("span");
        count.className = "qc-art-count";
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = "qc-line-action qc-eye";
        eye.innerHTML = '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>';
        eye.addEventListener("click", () => this.toggleChannel(channel));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "qc-line-action qc-art-remove";
        remove.innerHTML = '<span class="fas fa-xmark"></span>';
        remove.addEventListener("click", () => this.removeChannel(channel));
        row.append(dot, name, count, eye, remove);
        const line = { row, dot, name, count, eye, remove };
        line.select = this.makeSelect(name, channel, (picked) => {
            if (picked && picked !== channel) this.swapChannel(channel, picked);
        });
        if (!line.select) {
            name.classList.add("qc-line-name", "is-static");
            name.textContent = channel;
        }
        return line;
    }

    renderLine(channel, line, counts) {
        const on = this.channelOn(channel) && !this.muted;
        line.row.classList.toggle("is-hidden", !on);
        line.select?.setOptions?.([channel].concat(this.unlisted()));
        const n = counts.byChannel[channel] || 0;
        line.count.textContent = this.objects.length ? String(n) : "";
        line.count.title = this.objects.length ? `${n} artifacts seen in ${channel}` : "";
        line.eye.setAttribute("aria-pressed", on ? "true" : "false");
        line.eye.title = on ? `Hide ${channel}'s artifacts` : `Show ${channel}'s artifacts`;
        line.remove.title = this.listed().length <= 1 ? `Remove ${channel}: every channel again`
            : `Remove ${channel}`;
        line.remove.setAttribute("aria-label", `Remove ${channel}`);
    }

    renderAdd() {
        const add = this.el("qc_art_add");
        if (!add) return;
        const s = this.status || {};
        const next = this.unlisted()[0];
        add.disabled = !this.status || s.available === false || !next
            || this.listed().length >= QcArtifactsQc.MAX_LINES;
        add.title = next || !this.status ? "Show only the artifacts seen in a channel"
            : "Every channel is listed";
    }

    renderSummary(running, counts, results) {
        const node = this.el("qc_art_summary");
        if (!node) return;
        node.classList.remove("is-flagged");
        if (running) {
            const p = (this.job && this.job.progress) || {};
            node.textContent = p.total ? `${Math.round(100 * (p.done || 0) / p.total)}%` : "…";
            node.title = "The Artifact Detector is running";
            return;
        }
        if (!results) {
            node.textContent = "";
            node.title = "";
            return;
        }
        node.textContent = String(counts.total);
        node.classList.toggle("is-flagged", counts.total > 0);
        const parts = Object.keys(QcArtifactsQc.LABELS)
            .filter((k) => counts.byCat[k])
            .map((k) => `${counts.byCat[k]} ${QcArtifactsQc.LABELS[k].toLowerCase()}`);
        node.title = counts.total ? `Artifacts kept: ${parts.join(", ")}` : "No artifacts at these thresholds";
    }

    renderNote(running, results) {
        const note = this.el("qc_art_status");
        if (!note) return;
        const s = this.status || {};
        let text = "";
        let error = false;
        if (this.error && !running) {
            text = this.error;
            error = true;
        } else if (s.available === false && s.available_message) {
            text = `The Artifact Detector ${s.available_message}`;
        } else if (!running && results && results.stale) {
            text = "Stale: the image changed since the last run";
        } else if (!running && results && (results.warnings || []).length) {
            text = results.warnings[0];
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
        const kept = this.counts().total;
        const items = [
            { label: "Fill regions", checked: Boolean(this.view.fill),
              hint: "Shade each region as well as outlining it",
              onSelect: () => this.setFill() },
            { label: "Automatic thresholds",
              disabled: ![...this.cats.values()].some((e) => e.cat?.threshold?.source
                                                        && e.cat.threshold.source !== "auto"),
              hint: "Put back each category's automatic threshold",
              onSelect: () => this.resetThresholds() },
            { label: "Run again", className: "is-sectioned",
              disabled: Boolean(this.job) || s.available === false,
              hint: "Detect again from the pixels",
              onSelect: () => this.start({ force: true }) },
            { label: "All channels",
              disabled: !this.listed().length,
              hint: "Stop filtering by channel",
              onSelect: () => this.setChannels("default") },
            { label: "Add artifact regions to ROI QC…", className: "is-sectioned",
              disabled: !kept || Boolean(this.job),
              hint: kept ? `Write the ${kept} kept at these thresholds (every channel: `
                  + "the eyes do not change what is written)" : "Nothing is kept at these thresholds",
              onSelect: () => this.writeRegions(kept) },
        ];
        QcTree.menu(anchor, items, { heading: "Artifact Detector", className: "qc-picker" });
    }

    async writeRegions(kept) {
        if (!kept) return;
        const words = `${kept} region${kept === 1 ? "" : "s"}`;
        const confirm = window.PlexoraConfirm;
        if (confirm && typeof confirm.ask === "function") {
            const sure = await confirm.ask({
                title: "Add artifact regions to ROI QC",
                body: `Write ${words}, each category's at its own threshold, as QC: Tissue / `
                    + "acquisition artifact regions? Artifact regions written before are "
                    + "replaced; ones you edited or locked are kept.",
                confirm: "Add regions",
            });
            if (!sure) return;
        }
        const answer = await this.api.artifactsWriteRegions({}).catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "The regions could not be written");
            return;
        }
        const written = (answer.data.written || []).length;
        const held = (answer.data.kept || []).length;
        this.host.message(`${written} artifact region${written === 1 ? "" : "s"} added to ROI QC`
            + (held ? `; ${held} you edited kept` : ""));
        this.host.reload();
    }
}

window.QcArtifactsQc = QcArtifactsQc;
