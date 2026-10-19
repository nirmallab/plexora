/**
 * GateClassifierControl - the Thresholding panel's second mode: the Single
 * Cell Classifier.
 *
 * Owned by GatingSidebarController and switched by the cog in the card's
 * header. Thresholding calls a marker positive by one number; this calls it
 * from examples. The wand picks about ten clear positives and ten clear
 * negatives across the image itself and trains on them; + Positive /
 * - Negative arm a click on the image that labels the cell under it, and Train
 * (the play button) learns from exactly the examples there are. Either calls
 * every cell QC lets be called. The eraser takes the marker back to how it was
 * before its first run, keeping the examples. After a run, the pointer resting
 * on a cell shows its score (GateClassifierHover, below).
 *
 * Drawing reuses what the viewer already has, with no shader of its own:
 *
 * - The cells are the gating cell layer's colour table (`ctx.layers.
 *   setColorLUT("cells", ...)`). Before a run every cell is drawn, in the
 *   mask's own white, so there is something to click; after one, white for
 *   positive, as a gated cell is drawn, amber for uncertain, the not-called
 *   grey for cells not classified, and nothing for negatives. A labelled cell
 *   is green (positive) or red (negative) throughout. While this mode is on,
 *   the layer's provider reports no range gate
 *   (CSVGatingList.getColorCodedRanges), so the table decides.
 * - The examples are also an overlay (`ctx.layers.addOverlay`): a ring with a
 *   plus or a minus, sized in screen pixels so it reads at any zoom -- solid
 *   for a cell a person labelled, dashed for one the wand picked.
 *
 * Everything the server keeps (examples, runs, mode) is per project
 * (classifier/ on the server); this class holds only what is on screen.
 */

//: The colours a call is drawn in: [r, g, b, a].
const CLASSIFIER_COLORS = {
    // A gated cell's long-standing white (labelTile.js).
    1: [255, 255, 255, 220],
    // Not drawn: a negative is the tissue the positives stand out from.
    0: [0, 0, 0, 0],
    // The not-called grey (labelTile.js NOT_CALLED_RGB).
    2: [122, 122, 122, 200],
    // --accent-warning: look here.
    3: [243, 184, 69, 235],
};

//: The example rings and cells (--accent-success, --accent-danger).
const EXAMPLE_COLORS = { positive: "#34d399", negative: "#f87171" };
const EXAMPLE_RGBA = { positive: [52, 211, 153, 255], negative: [248, 113, 113, 255] };
//: Every cell before a run: the mask's own white (labelTile.js).
const UNRUN_RGBA = [255, 255, 255, 220];

//: Above this many ids a dense colour table is too big to build (4 bytes
//: per id); the sparse form is used instead.
const DENSE_LUT_MAX_ID = 1 << 25;

//: What the server's run stages are called on the status line.
const STAGE_WORDS = {
    pick: "Picking examples across the image",
    examples: "Checking the examples",
    features: "Reading each cell's pixels",
    fit: "Training",
    predict: "Classifying every cell",
    store: "Saving the calls",
};

//: How far from a cell (screen px) a click still picks it.
const PICK_SCREEN_PX = 8;

class GateClassifierControl {
    constructor(controller) {
        this.controller = controller;
        this.ctx = controller.ctx;
        this.api = controller.api;
        //: "threshold" | "classifier" -- the card's mode.
        this.mode = "threshold";
        this.marker = null;
        //: The server's answer for the marker on screen (panel.state).
        this.state = null;
        //: Markers the classifier decides, for the marker list's dots.
        this.classified = new Set();
        //: The calls on screen and the run they came from.
        this.calls = null;
        //: "positive" | "negative" | null: what a click on a cell does.
        this.label = null;
        //: {id, marker} of the run being followed.
        this.job = null;
        this.status = null;
        this.shown = false;
        this._ids = null;
        this._seq = 0;
        this._armed = false;
        this._onClick = (event) => this.onCanvasClick(event);
        this._onKey = (event) => this.onKey(event);
        this._onServer = (event) => this.onServerChange(event);
    }

    // -- lifecycle -----------------------------------------------------------

    init() {
        const byId = (id) => document.getElementById(id);
        this.section = byId("gate_marker_section");
        this.cog = byId("gating_mode_button");
        this.runButton = byId("clf_run_button");
        this.revertButton = byId("clf_revert_button");
        this.positiveButton = byId("clf_positive_button");
        this.negativeButton = byId("clf_negative_button");
        this.summary = byId("clf_summary");
        this.statusLine = byId("clf_status");
        this.loadInput = byId("clf_load_input");

        this.cog?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openModeMenu();
        });
        this.trainButton = byId("clf_train_button");
        this.runButton?.addEventListener("click", () => this.onRunClick({ auto: true }));
        this.trainButton?.addEventListener("click", () => this.onRunClick({ auto: false }));
        this.revertButton?.addEventListener("click", () => this.onRevertClick());
        this.positiveButton?.addEventListener("click", () => this.toggleLabel("positive"));
        this.negativeButton?.addEventListener("click", () => this.toggleLabel("negative"));
        byId("clf_load_button")?.addEventListener("click", () => this.loadInput?.click());
        this.loadInput?.addEventListener("change", () => this.onLoadFile());
        const download = byId("clf_download_button");
        download?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openDownloadMenu(download);
        });
        const more = byId("clf_more_button");
        more?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openMoreMenu(more);
        });
        window.addEventListener("plexora:agent-state-changed", this._onServer);
        this.hover = new GateClassifierHover(this);
        this.ctx.onCleanup?.(() => this.dispose());
        this.paint();

        // The mode is the project's: a reload comes back to the one left.
        this.api.classifierState().then((state) => {
            this.takeState(state);
            if (state.mode === "classifier") this.setMode("classifier", { persist: false });
        }).catch((error) => console.error("classifier: no state", error));
    }

    dispose() {
        this.disarm();
        this.hover?.destroy();
        document.removeEventListener("keydown", this._onKey);
        window.removeEventListener("plexora:agent-state-changed", this._onServer);
        this.ctx.layers?.removeOverlay?.("classifier-examples");
    }

    show() {
        this.shown = true;
        document.addEventListener("keydown", this._onKey);
        this.syncArming();
        this.hover?.sync();
    }

    hide() {
        this.shown = false;
        document.removeEventListener("keydown", this._onKey);
        this.syncArming();
        this.hover?.sync();
    }

    // -- mode ----------------------------------------------------------------

    openModeMenu() {
        window.PlexoraMenu?.open(this.cog, [
            {
                label: "Thresholding", checked: this.mode === "threshold",
                title: "Call a marker positive by a threshold on its value",
                onSelect: () => this.setMode("threshold"),
            },
            {
                label: "Single Cell Classifier", checked: this.mode === "classifier",
                title: "Call a marker positive from cells you label as examples",
                onSelect: () => this.setMode("classifier"),
            },
        ]);
    }

    setMode(mode, { persist = true } = {}) {
        const next = mode === "classifier" ? "classifier" : "threshold";
        const changed = next !== this.mode;
        this.mode = next;
        if (this.section) this.section.dataset.gateMode = next;
        if (this.cog) {
            this.cog.classList.toggle("is-classifier", next === "classifier");
            const where = next === "classifier" ? "Single Cell Classifier" : "Thresholding";
            this.cog.title = `${where} (change mode)`;
            this.cog.setAttribute("aria-label", `${where} (change mode)`);
        }
        const gatingList = this.controller.gatingList;
        if (gatingList) gatingList.classifierMode = next === "classifier";
        if (next === "classifier") {
            this.mountOverlay();
            this.follow(this.controller.gateMarker, { force: true });
        } else {
            this.setLabel(null);
            this.ctx.layers?.removeOverlay?.("classifier-examples");
            this.ctx.layers?.setColorLUT?.("cells", null);
            this.calls = null;
            this.scores = null;
        }
        this.hover?.sync();
        // The layer's gate again: none in this mode, the slider's in the other.
        this.controller.eventHandler?.trigger?.(CSVGatingList.events.SELECTION_CHANGED,
                                                gatingList?.selections || {});
        this.refreshMarkerDots();
        this.syncArming();
        this.paint();
        if (changed && persist) {
            this.ctx.telemetry?.feature?.(`classifier.mode.${next}`);
            this.api.setClassifierMode(next).catch((error) => {
                console.error("classifier: could not save the mode", error);
            });
        }
    }

    isOn() {
        return this.mode === "classifier";
    }

    // -- the marker on screen -------------------------------------------------

    follow(marker, { force = false } = {}) {
        if (!marker) return;
        if (marker === this.marker && !force) return;
        this.marker = marker;
        if (!this.isOn()) return;
        this.status = null;
        this.calls = null;
        this.state = null;
        this.paint();
        this.refresh();
    }

    async refresh() {
        if (!this.isOn() || !this.marker) return;
        const seq = ++this._seq;
        const marker = this.marker;
        let state;
        try {
            state = await this.api.classifierState(marker);
        } catch (error) {
            if (seq === this._seq) this.setStatus(String(error.message || error), "error");
            return;
        }
        if (seq !== this._seq || marker !== this.marker) return;
        this.takeState(state);
        const run = state.marker?.run;
        if (!run) {
            this.calls = null;
            this.scores = null;
        } else if (!this.calls || this.calls.run !== run.run_id) {
            try {
                const calls = await this.api.classifierCalls(marker);
                if (seq !== this._seq || marker !== this.marker) return;
                this.calls = calls;
                this.scores = null;
                this.loadScores(marker, calls.run);
            } catch (error) {
                this.setStatus(`The calls could not be read (${error.message || error})`, "error");
            }
        }
        await this.drawCalls(this.calls);
        this.hover?.sync();
        // A run this tab did not start (another tab, an agent) is followed too.
        const running = state.running;
        if (running && !this.job) {
            this.job = { id: running.job_id, marker: running.marker };
            this.followJob();
        }
        this.paint();
    }

    takeState(state) {
        if (!state) return;
        if (state.marker && state.marker.marker === this.marker) this.state = state.marker;
        this.classified = new Set(state.classified || []);
        this.minExamples = state.min_examples || 3;
        this.refreshMarkerDots();
    }

    refreshMarkerDots() {
        const select = this.controller.gateMarkerSelect;
        select?.setOptions?.(this.controller.getGateMarkerNames());
    }

    /** "Decided by the Single Cell Classifier", or null (the marker list's dot). */
    describeIndicator(name) {
        return this.classified.has(name) ? "Decided by the Single Cell Classifier" : null;
    }

    // -- drawing ---------------------------------------------------------------

    async cellIds() {
        if (this._ids) return this._ids;
        const numericData = this.ctx.viewer?.numericData;
        if (!numericData?.loadCells) return null;
        const { ids } = await numericData.loadCells();
        this._ids = ids || null;
        return this._ids;
    }

    /** The scores the hover card reads, fetched once per run. */
    async loadScores(marker, run) {
        try {
            const scores = await this.api.classifierScores(marker);
            if (marker === this.marker && this.calls && this.calls.run === run) this.scores = scores;
        } catch (error) {
            console.error("classifier: no scores", error);
        }
    }

    /** id -> table row, for the hover card (built once with the ids). */
    async rowOf(cellId) {
        const ids = await this.cellIds();
        if (!ids) return -1;
        if (!this._rowOf) {
            this._rowOf = new Map();
            for (let i = 0; i < ids.length; i += 1) this._rowOf.set(ids[i], i);
        }
        const row = this._rowOf.get(cellId);
        return row === undefined ? -1 : row;
    }

    /**
     * The layer's colour table: before a run every cell in the mask's
     * white; after one, the calls; and the labelled cells green or red over
     * either. Rebuilt whole -- a few bytes a cell, from arrays in hand.
     */
    async drawCalls(calls) {
        if (!this.isOn()) return;
        const layers = this.ctx.layers;
        const ids = await this.cellIds();
        if (!ids || !ids.length) {
            layers?.setColorLUT?.("cells", null);
            return;
        }
        const ready = calls && calls.state === "ready";
        if (ready && ids.length !== calls.codes.length) {
            this.setStatus("The table on screen is not the one the calls were made on; "
                           + "reopen the project", "warning");
        }
        const codes = ready && ids.length === calls.codes.length ? calls.codes : null;
        const examples = this.state?.examples || {};
        const labelled = new Map();
        for (const entry of examples.positive || []) labelled.set(entry.cell_id, EXAMPLE_RGBA.positive);
        for (const entry of examples.negative || []) labelled.set(entry.cell_id, EXAMPLE_RGBA.negative);
        const colorOf = (i) => {
            const own = labelled.get(ids[i]);
            if (own) return own;
            return codes ? CLASSIFIER_COLORS[codes[i]] : UNRUN_RGBA;
        };
        let maxId = 0;
        for (let i = 0; i < ids.length; i += 1) if (ids[i] > maxId) maxId = ids[i];
        if (maxId <= DENSE_LUT_MAX_ID) {
            const colors = new Uint8Array((maxId + 1) * 4);
            for (let i = 0; i < ids.length; i += 1) {
                const color = colorOf(i);
                if (color && color[3]) colors.set(color, ids[i] * 4);
            }
            layers?.setColorLUT?.("cells", { colors, maxId });
        } else {
            const map = new Map();
            for (let i = 0; i < ids.length; i += 1) {
                const color = colorOf(i);
                if (color && color[3]) map.set(ids[i], color);
            }
            layers?.setColorLUT?.("cells", { map });
        }
    }

    mountOverlay() {
        this.ctx.layers?.addOverlay?.({
            id: "classifier-examples",
            draw: (opts) => this.drawExamples(opts),
        });
    }

    repaintOverlay() {
        this.ctx.layers?.repaint?.();
    }

    drawExamples(opts) {
        if (!this.isOn() || !this.state) return;
        const context = opts.context;
        const px = opts.px || 1;
        const examples = this.state.examples || {};
        const ring = 9 * px;
        const arm = 4 * px;
        const draw = (points, color, plus) => {
            for (const point of points || []) {
                context.beginPath();
                context.arc(point.x, point.y, ring, 0, Math.PI * 2);
                // A dark rim first, so the ring reads on bright tissue too.
                context.setLineDash([]);
                context.lineWidth = 4 * px;
                context.strokeStyle = "rgba(0, 0, 0, 0.55)";
                context.stroke();
                // Dashed: the wand picked it, nobody has looked at it yet.
                context.setLineDash(point.auto ? [3 * px, 2.5 * px] : []);
                context.lineWidth = 2 * px;
                context.strokeStyle = color;
                context.stroke();
                context.setLineDash([]);
                context.beginPath();
                context.moveTo(point.x - arm, point.y);
                context.lineTo(point.x + arm, point.y);
                if (plus) {
                    context.moveTo(point.x, point.y - arm);
                    context.lineTo(point.x, point.y + arm);
                }
                context.stroke();
            }
        };
        draw(examples.negative, EXAMPLE_COLORS.negative, false);
        draw(examples.positive, EXAMPLE_COLORS.positive, true);
    }

    // -- labelling cells -----------------------------------------------------------

    toggleLabel(label) {
        this.setLabel(this.label === label ? null : label);
    }

    setLabel(label) {
        this.label = label === "positive" || label === "negative" ? label : null;
        this.positiveButton?.setAttribute("aria-pressed", String(this.label === "positive"));
        this.negativeButton?.setAttribute("aria-pressed", String(this.label === "negative"));
        this.syncArming();
        this.paint();
    }

    syncArming() {
        if (this.shown && this.isOn() && this.label) this.arm();
        else this.disarm();
    }

    arm() {
        const viewer = this.ctx.viewer?.viewer;
        if (this._armed || !viewer) return;
        this._armed = true;
        viewer.addHandler("canvas-click", this._onClick);
        if (viewer.canvas) viewer.canvas.style.cursor = "crosshair";
    }

    disarm() {
        const viewer = this.ctx.viewer?.viewer;
        if (!this._armed) return;
        this._armed = false;
        viewer?.removeHandler?.("canvas-click", this._onClick);
        if (viewer?.canvas) viewer.canvas.style.cursor = "";
    }

    /** Screen position -> full-resolution image pixel (roiTools.toImage). */
    toImage(position) {
        const viewer = this.ctx.viewer?.viewer;
        const item = viewer?.world?.getItemAt(0);
        if (!item) return null;
        if (item.source && typeof item.source.getImagePixel === "function") {
            const [x, y] = item.source.getImagePixel(item, position);
            return [x, y];
        }
        const viewportPoint = window.PlexoraViewTransform
            ? window.PlexoraViewTransform.pointFromPixel(viewer, position)
            : viewer.viewport.pointFromPixel(position);
        const imagePoint = item.viewportToImageCoordinates(viewportPoint);
        const scale = 2 ** (this.ctx.config?.extraZoomLevels || 0);
        return [imagePoint.x / scale, imagePoint.y / scale];
    }

    /** Screen pixels -> image pixels at the current zoom. */
    imageDistance(screenPixels) {
        try {
            const viewer = this.ctx.viewer.viewer;
            const item = viewer.world.getItemAt(0);
            const zoom = item.viewportToImageZoom(viewer.viewport.getZoom(true));
            return screenPixels / Math.max(zoom, 1e-6);
        } catch (error) {
            return screenPixels;
        }
    }

    async onCanvasClick(event) {
        if (!this._armed || !this.label || !this.marker) return;
        // A drag pans, as ever; only a click labels.
        if (event.quick === false) return;
        event.preventDefaultAction = true;
        const point = this.toImage(event.position);
        if (!point) return;
        const label = this.label;
        const marker = this.marker;
        const radius = Math.max(2, Math.min(64, this.imageDistance(PICK_SCREEN_PX)));
        try {
            const hit = await this.api.classifierCellAt(point[0], point[1], radius);
            if (!hit.cell_id && hit.cell_id !== 0) {
                this.setStatus("No cell there", "warning");
                return;
            }
            const answer = await this.api.classifierAnnotate(marker, hit.cell_id, label);
            if (marker !== this.marker) return;
            if (this.state) this.state.examples = answer.examples;
            this.status = null;
            this.ctx.telemetry?.feature?.("classifier.annotate");
            await this.drawCalls(this.calls);
            this.repaintOverlay();
            this.paint();
        } catch (error) {
            this.setStatus(String(error.message || error), "error");
        }
    }

    onKey(event) {
        if (event.key !== "Escape" || !this.label) return;
        if (!this.controller.acceptsKeys(event)) return;
        this.setLabel(null);
    }

    // -- run and revert ---------------------------------------------------------------

    /** The wand (`auto`: pick the examples, then train) or Train (the
     *  examples there are). */
    async onRunClick({ auto = false } = {}) {
        if (this.job || !this.marker) return;
        const marker = this.marker;
        this.setStatus(auto ? "Picking examples…" : "Starting…");
        let answer;
        try {
            answer = await this.api.classifierRun(marker, auto);
        } catch (error) {
            this.setStatus(String(error.message || error), "error");
            return;
        }
        const running = answer.running?.marker || marker;
        this.job = { id: answer.job_id, marker: running };
        if (answer.already_running && running !== marker) {
            this.setStatus(`A run on ${running} is in progress`, "warning");
        }
        this.ctx.telemetry?.feature?.(auto ? "classifier.wand" : "classifier.train");
        this.paint();
        await this.followJob();
    }

    async followJob() {
        const job = this.job;
        if (!job) return;
        let wait = 750;
        while (this.job === job) {
            await new Promise((resolve) => window.setTimeout(resolve, wait));
            wait = Math.min(2000, wait + 250);
            let record;
            try {
                record = await this.api.gatingJob(job.id);
            } catch (error) {
                this.job = null;
                this.setStatus("Lost track of the run; press the wand again", "error");
                break;
            }
            if (this.job !== job) return;
            if (record.status === "done") {
                this.job = null;
                this.status = null;
                this.calls = null;
                await this.refresh();
                break;
            }
            if (record.status === "failed" || record.status === "interrupted") {
                this.job = null;
                this.setStatus(record.error?.message || "The run failed; the previous run is kept",
                               "error");
                break;
            }
            if (record.status === "cancelled") {
                this.job = null;
                this.setStatus("The run was cancelled; the previous run is kept");
                break;
            }
            if (job.marker === this.marker) this.paintProgress(record.progress || {});
        }
        this.paint();
    }

    paintProgress(progress) {
        const words = STAGE_WORDS[progress.message] || "Working";
        const total = Number(progress.total) || 0;
        const done = Number(progress.done) || 0;
        // The pixel pass reports its own tiles: the one stage long enough to
        // need a number.
        const pct = progress.message === "features" && total
            ? ` ${Math.max(0, Math.min(100, Math.round((done / total) * 100)))}%` : "";
        this.setStatus(`${words}…${pct}`);
    }

    async onRevertClick() {
        if (!this.marker) return;
        const marker = this.marker;
        try {
            await this.api.classifierRevert(marker);
        } catch (error) {
            this.setStatus(String(error.message || error), "error");
            return;
        }
        if (this.job && this.job.marker === marker) this.job = null;
        this.calls = null;
        this.setStatus(`${marker} is back to Thresholding; the examples are kept`);
        this.ctx.telemetry?.feature?.("classifier.revert");
        await this.refresh();
    }

    // -- files -------------------------------------------------------------------------

    openDownloadMenu(anchor) {
        const hasRun = this.classified.size > 0;
        const why = hasRun ? "" : "No marker has a classifier run yet";
        const deliver = (kind, name) => () => this.download(kind, name);
        window.PlexoraMenu?.open(anchor, [
            { label: "Binary calls (CSV)", disabled: !hasRun,
              title: why || "One row per cell, one 1/0 column per classified marker",
              onSelect: deliver("binary", "classifier_binary.csv") },
            { label: "Scores (CSV)", disabled: !hasRun,
              title: why || "One row per cell, the model's score per classified marker",
              onSelect: deliver("scores", "classifier_scores.csv") },
            { label: "Training examples (JSON)",
              title: "Every marker's labelled cells, to load into another session",
              onSelect: deliver("annotations", "classifier_annotations.json") },
        ]);
    }

    async download(kind, name) {
        try {
            const blob = await this.api.classifierDownload(kind);
            const file = `${this.ctx.datasource}_${name}`;
            if (window.PlexoraFileLocation?.deliver) {
                await window.PlexoraFileLocation.deliver(blob, file);
            } else {
                const link = document.createElement("a");
                link.href = URL.createObjectURL(blob);
                link.download = file;
                link.click();
                window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
            }
            this.ctx.telemetry?.feature?.(`classifier.download.${kind}`);
        } catch (error) {
            this.setStatus(String(error.message || error), "error");
        }
    }

    async onLoadFile() {
        const file = this.loadInput?.files?.[0];
        if (this.loadInput) this.loadInput.value = "";
        if (!file) return;
        let report;
        try {
            report = await this.api.classifierPreview(file);
        } catch (error) {
            await window.PlexoraConfirm?.tell?.({ title: "Load Annotations",
                                                  body: String(error.message || error) });
            return;
        }
        const lines = Object.entries(report.markers || {}).map(([marker, counts]) => {
            const extra = [];
            if (counts.unknown_cells) extra.push(`${counts.unknown_cells} not in this table`);
            if (counts.conflicts) extra.push(`${counts.conflicts} labelled both ways, left out`);
            if (counts.duplicates) extra.push(`${counts.duplicates} repeated`);
            return `${marker}: ${counts.valid_positive} positive, ${counts.valid_negative} negative`
                + (extra.length ? ` (${extra.join("; ")})` : "");
        });
        const body = [
            ...lines,
            ...(report.warnings || []).map((text) => `Note: ${text}`),
            ...(report.problems || []).slice(0, 5).map((text) => `Skipped: ${text}`),
            "Merge adds these to the examples you have; Replace makes them the only "
                + "examples of the markers named in the file.",
        ];
        const mode = await window.PlexoraConfirm?.choose?.({
            title: "Load Annotations",
            body,
            choices: [
                { value: null, label: "Cancel", focus: true },
                { value: "replace", label: "Replace" },
                { value: "merge", label: "Merge", kind: "primary" },
            ],
        });
        if (!mode) return;
        try {
            await this.api.classifierApply(report.token, mode);
            this.ctx.telemetry?.feature?.(`classifier.import.${mode}`);
            this.setStatus("Examples loaded");
            await this.refresh();
            this.repaintOverlay();
        } catch (error) {
            this.setStatus(String(error.message || error), "error");
        }
    }

    openMoreMenu(anchor) {
        window.PlexoraMenu?.open(anchor, [
            { label: "Clear training examples…",
              title: "Forget labelled cells (runs already made are kept)",
              onSelect: () => this.onClearExamples() },
            { label: "Reset classifier…",
              title: "Undo every marker's run and forget every example",
              onSelect: () => this.onReset() },
        ]);
    }

    async onClearExamples() {
        const which = await window.PlexoraConfirm?.choose?.({
            title: "Clear training examples",
            body: ["Runs already made are kept; the wand needs new examples to run again."],
            choices: [
                { value: null, label: "Cancel", focus: true },
                { value: "all", label: "Every marker", kind: "danger" },
                { value: "this", label: `${this.marker || "This marker"} only`, kind: "danger" },
            ],
        });
        if (!which) return;
        try {
            await this.api.classifierClearExamples(which === "this" ? [this.marker] : null);
            await this.refresh();
            this.repaintOverlay();
        } catch (error) {
            this.setStatus(String(error.message || error), "error");
        }
    }

    async onReset() {
        const sure = await window.PlexoraConfirm?.ask?.({
            title: "Reset classifier",
            body: ["Every marker's classifier run is undone (its calls go back to "
                   + "Thresholding) and every training example is forgotten."],
            confirm: "Reset",
            danger: true,
        });
        if (!sure) return;
        try {
            await this.api.classifierReset();
            this.calls = null;
            this.job = null;
            await this.refresh();
            this.repaintOverlay();
        } catch (error) {
            this.setStatus(String(error.message || error), "error");
        }
    }

    // -- server changes --------------------------------------------------------------

    onServerChange(event) {
        const detail = event.detail || {};
        if (detail.plugin !== "gating" || detail.kind !== "classifier.changed") return;
        const marker = detail.payload?.marker;
        if (!this.isOn() || (marker && marker !== this.marker)) {
            // Another marker's run: only the dots move.
            this.api.classifierState().then((state) => this.takeState(state)).catch(() => {});
            return;
        }
        this.refresh().then(() => this.repaintOverlay());
    }

    // -- the panel's words ----------------------------------------------------------------

    setStatus(text, tone = null) {
        this.status = text ? { text, tone } : null;
        this.paint();
    }

    paint() {
        const state = this.state;
        const examples = state?.examples || {};
        const nPos = examples.n_positive || 0;
        const nNeg = examples.n_negative || 0;
        const setText = (id, value) => {
            const node = document.getElementById(id);
            if (node) node.textContent = String(value);
        };
        setText("clf_positive_count", nPos);
        setText("clf_negative_count", nNeg);
        const min = this.minExamples || 3;
        const busy = Boolean(this.job);
        if (this.runButton) {
            this.runButton.disabled = busy || !this.marker;
            this.runButton.classList.toggle("is-busy", busy);
            this.runButton.title = busy ? "Running…"
                : "Pick about 10 positive and 10 negative cells across the image, then train "
                  + "and classify every cell";
        }
        if (this.trainButton) {
            const short = nPos < min || nNeg < min;
            this.trainButton.disabled = busy || !state || short;
            this.trainButton.title = busy ? "Running…"
                : short ? `Train needs ${min} positive and ${min} negative examples`
                    : `Train on these ${nPos + nNeg} examples and classify every cell`;
        }
        if (this.revertButton) this.revertButton.disabled = !state?.run;
        this.paintSummary();
        this.paintStatus(nPos, nNeg, min);
        this.repaintOverlay();
    }

    paintSummary() {
        const node = this.summary;
        if (!node) return;
        const calls = this.calls;
        if (!this.isOn() || !calls || calls.state !== "ready") {
            node.hidden = true;
            return;
        }
        node.replaceChildren();
        const format = (n) => Number(n).toLocaleString();
        const strong = (text) => {
            const el = document.createElement("strong");
            el.textContent = text;
            return el;
        };
        node.append("Positive cells: ", strong(format(calls.positive)), ` / ${format(calls.eligible)}`);
        if (calls.uncertain) {
            const span = document.createElement("span");
            span.className = "clf-uncertain";
            span.textContent = `${format(calls.uncertain)} uncertain`;
            node.append(" · ", span);
        }
        if (calls.notClassified) node.append(` · ${format(calls.notClassified)} not classified`);
        const run = this.state?.run;
        if (run) {
            const what = run.score_name === "probability" ? "calibrated" : "uncalibrated scores";
            const auto = run.auto_pick ? "; examples picked by the wand" : "";
            const cv = run.cv_balanced_accuracy != null
                ? `, cross-validated ${Math.round(run.cv_balanced_accuracy * 100)}%` : "";
            node.title = `${run.model === "gradient_boosting" ? "Gradient boosting"
                : "Logistic regression"} on ${run.n_examples_positive} + `
                + `${run.n_examples_negative} examples (${(run.feature_groups || []).join(", ")}; `
                + `${what}${cv}${auto}). Uncertain: ${run.uncertain_rule || "near the boundary"}.`;
        }
        node.hidden = false;
    }

    paintStatus(nPos, nNeg, min) {
        const node = this.statusLine;
        if (!node) return;
        let text = null;
        let tone = null;
        if (this.status) {
            ({ text, tone } = this.status);
        } else if (this.state?.stale) {
            text = `Stale: ${this.state.stale}. Run the wand again to update.`;
            tone = "warning";
        } else if (this.state && (nPos < min || nNeg < min)) {
            text = this.label
                ? `Click cells to label them ${this.label}. Train needs ${min} positive and `
                  + `${min} negative.`
                : "Press the wand to pick examples automatically, or choose Positive or "
                  + `Negative and click cells, then \u25B6 (${min} of each).`;
        } else if (this.label) {
            text = `Click cells to label them ${this.label}; click again to remove. Esc stops.`;
        }
        node.hidden = !text || !this.isOn();
        node.textContent = text || "";
        if (tone) node.dataset.tone = tone;
        else delete node.dataset.tone;
    }
}


/**
 * GateClassifierHover - what a positive or uncertain cell says when the
 * pointer rests on it after a run (a negative says nothing): its call, the model's score (or probability, once calibrated), how many
 * of the models fitted to resampled examples agree, and whether a person or
 * the wand labelled it.
 *
 * The cell under the pointer is the mask's (`cell_at`, the route a click on a
 * cell uses), asked once per new spot -- one question in flight, always about
 * the newest point, and a small move over the same spot answered from the last
 * reply. The scores are in hand (`classifierScores`, one fetch per run), so
 * nothing else is asked. On only in this mode, with the panel on screen and a
 * run drawn; a press, a drag, a wheel or the pointer leaving clears it.
 */
class GateClassifierHover {
    static get GAP() { return 16; }
    static get PAD() { return 8; }

    constructor(control) {
        this.control = control;
        this.ctx = control.ctx;
        this.tracker = null;
        this.viewer = null;
        this.card = null;
        this.position = null;
        this.last = null;
        this.wanted = null;
        this.inflight = false;
        this._frame = 0;
        this._handlers = [];
    }

    wantsOn() {
        const control = this.control;
        return control.isOn() && control.shown && Boolean(control.calls)
            && control.calls.state === "ready";
    }

    sync() {
        if (this.wantsOn()) this.arm();
        else this.disarm();
    }

    arm() {
        if (this.tracker) return;
        const viewer = this.ctx.viewer?.viewer;
        if (!viewer?.canvas) return;
        this.viewer = viewer;
        // Plain pointer events rather than an OSD MouseTracker: a tracker
        // made while the pointer is already over the image (a run finishing
        // under a resting hand) hears nothing until the pointer leaves and
        // comes back.
        const element = viewer.canvas;
        const onMove = (event) => {
            const box = element.getBoundingClientRect();
            this.moved({ position: { x: event.clientX - box.left, y: event.clientY - box.top } });
        };
        const onLeave = () => this.clear();
        element.addEventListener("pointermove", onMove);
        element.addEventListener("pointerleave", onLeave);
        this.tracker = {
            destroy: () => {
                element.removeEventListener("pointermove", onMove);
                element.removeEventListener("pointerleave", onLeave);
            },
        };
        for (const name of ["canvas-press", "canvas-drag", "canvas-scroll"]) {
            const fn = () => this.clear();
            viewer.addHandler(name, fn);
            this._handlers.push([name, fn]);
        }
    }

    disarm() {
        this.clear();
        this.tracker?.destroy?.();
        this.tracker = null;
        for (const [name, fn] of this._handlers) this.viewer?.removeHandler?.(name, fn);
        this._handlers = [];
        this.viewer = null;
        this.last = null;
    }

    destroy() {
        this.disarm();
        this.card?.remove();
        this.card = null;
    }

    moved(event) {
        if (!event?.position) return;
        const OSD = window.OpenSeadragon;
        this.position = OSD?.Point ? new OSD.Point(event.position.x, event.position.y)
            : event.position;
        if (this._frame) return;
        this._frame = requestAnimationFrame(() => {
            this._frame = 0;
            this.resolve();
        });
    }

    resolve() {
        if (!this.tracker || !this.position || !this.wantsOn()) {
            this.clear();
            return;
        }
        const point = this.control.toImage(this.position);
        if (!point) return;
        const [x, y] = point;
        const scale = this.control.imageDistance(1);
        const last = this.last;
        if (last && Math.hypot(x - last.x, y - last.y) < scale * 3) {
            this.render(last.hit);
            return;
        }
        this.wanted = { x, y, scale };
        this.ask();
    }

    async ask() {
        if (this.inflight || !this.wanted) return;
        const { x, y, scale } = this.wanted;
        this.wanted = null;
        this.inflight = true;
        try {
            const hit = await this.control.api.classifierCellAt(
                x, y, Math.max(1, Math.min(32, scale * 3)));
            this.last = { x, y, hit };
            if (this.tracker) this.render(hit);
        } catch (error) {
            this.clear();
        } finally {
            this.inflight = false;
            if (this.wanted) this.ask();
        }
    }

    async render(hit) {
        const control = this.control;
        if (!hit || hit.cell_id === null || hit.cell_id === undefined) {
            this.hideCard();
            return;
        }
        const row = await control.rowOf(hit.cell_id);
        const calls = control.calls;
        if (row < 0 || !calls || row >= calls.codes.length) {
            this.hideCard();
            return;
        }
        const code = calls.codes[row];
        const examples = control.state?.examples || {};
        let label = null;
        for (const kind of ["positive", "negative"]) {
            const entry = (examples[kind] || []).find((e) => e.cell_id === hit.cell_id);
            if (entry) label = [kind, entry];
        }
        // Only the cells the run draws -- positive and uncertain. A negative
        // is most of the tissue and drawn as nothing, so a card on every one
        // would follow the pointer everywhere it goes.
        if (code === 0 || (code === 2 && !label)) {
            this.hideCard();
            return;
        }
        const marker = control.marker;
        const scores = control.scores;
        const score = scores && row < scores.scores.length ? scores.scores[row] : NaN;
        const agreement = scores && row < scores.agreement.length ? scores.agreement[row] : null;
        const call = code === 1 || (code === 3 && score >= 0.5) ? "positive"
            : code === 2 ? "not classified" : "negative";
        const title = code === 3 ? `${marker} ${call} · uncertain` : `${marker} ${call}`;
        const rows = [];
        if (Number.isFinite(score)) {
            rows.push([scores.scoreName === "probability" ? "Probability" : "Score",
                       score.toFixed(2)]);
        }
        if (agreement !== null && Number.isFinite(score)) {
            rows.push(["Agreement", `${agreement}% of resampled models`]);
        }
        if (label) {
            rows.push(["Example", label[1].auto ? `${label[0]}, picked by the wand`
                : `${label[0]}, labelled by you`]);
        }
        rows.push(["Cell", String(hit.cell_id)]);
        const tone = code === 3 ? "uncertain" : call === "positive" ? "positive" : "negative";
        this.showCard(title, tone, rows);
    }

    ensureCard() {
        if (this.card) return this.card;
        const card = document.createElement("div");
        card.className = "clf-hover-card";
        card.setAttribute("role", "tooltip");
        card.hidden = true;
        document.body.appendChild(card);
        this.card = card;
        return card;
    }

    showCard(title, tone, rows) {
        const card = this.ensureCard();
        card.replaceChildren();
        const head = document.createElement("div");
        head.className = "clf-hover-title";
        head.dataset.tone = tone;
        head.textContent = title;
        const list = document.createElement("dl");
        list.className = "clf-hover-rows";
        for (const [name, value] of rows) {
            const dt = document.createElement("dt");
            dt.textContent = name;
            const dd = document.createElement("dd");
            dd.textContent = value;
            list.append(dt, dd);
        }
        card.append(head, list);
        card.hidden = false;
        this.place();
    }

    /** Beside the pointer, flipped where the image ends. */
    place() {
        const card = this.card;
        const canvas = this.viewer?.canvas;
        if (!card || !canvas || !this.position) return;
        const bounds = canvas.getBoundingClientRect();
        const box = card.getBoundingClientRect();
        const anchor = { x: bounds.left + this.position.x, y: bounds.top + this.position.y };
        const gap = GateClassifierHover.GAP;
        const pad = GateClassifierHover.PAD;
        let left = anchor.x + gap;
        if (left + box.width > bounds.right - pad) left = anchor.x - gap - box.width;
        let top = anchor.y + gap;
        if (top + box.height > bounds.bottom - pad) top = anchor.y - gap - box.height;
        card.style.left = `${Math.round(Math.max(bounds.left + pad, left))}px`;
        card.style.top = `${Math.round(Math.max(bounds.top + pad, top))}px`;
    }

    hideCard() {
        if (this.card) this.card.hidden = true;
    }

    clear() {
        this.hideCard();
        this.wanted = null;
    }
}
