/**
 * QcSidebarController - the Quality Control panel, and the plugin's registration.
 *
 * THREE TOOLS IN ONE CARD, each its own fold (`.qc-tool`, `data-open`):
 * Registration QC (qcRegistration.js), Segmentation QC (qcSegmentation.js)
 * and ROI QC, which is this file's. Every tool opens folded, and ROI QC's
 * regions open hidden (each category's eye off), on every load: the panel
 * opens as a list of what can be checked, drawing nothing until asked.
 *
 * ROI QC shows what QC says about this image's regions and puts it on the
 * tissue: the regions (drawn by qcLayers.js's overlay, in their class
 * colours), the flagged cells (QC's own cell layer, coloured by reason), and
 * every channel's status -- as one tree in the ROI panel's shape (qcTree.js).
 * Every row has an eye for what it draws and a kebab for what can be done to
 * it; a click on a region fits the viewer to it and highlights it, and a
 * click on a reason frames the cells it flagged (and turns QC's cell layer
 * on: until then only the regions are drawn). THE CHANNELS ON SCREEN ARE THE
 * USER'S: no click changes them. A region drawn by hand remembers what was on
 * screen, and its menu can put that back -- only when asked.
 *
 * A class's dot and a cell reason's dot are colour pickers (core's
 * ColorSwatchPicker, as the ROI panel's category dots are). A class's colour
 * is its ROI category's, so the ROI panel follows and the cells inside those
 * regions take it too; a reason's is kept in QC's store. The change is drawn
 * at once and saved a moment later, so dragging the custom colour does not
 * post once per pixel.
 *
 * ROI QC's header adds a region (a category, then Freehand), takes in the
 * ROI panel's edits, downloads the files, and says how to start an AI
 * session; the strictness under it re-derives every call on the server, no
 * agent involved. Each part appears only once it has something in it:
 * Regions with the first region, Cells once cells were called, Channels once
 * a session checked them, the strictness once a session decided regions it
 * applies to. A region says quietly where it came from -- drawn by hand, an
 * AI session, or derived from another QC -- and one drawn by hand can be
 * renamed or deleted here.
 *
 * Marking a region by hand never leaves this panel: picking a category (an
 * artifact class, or one the user names under "Custom") puts QC's own
 * Freehand in hand (qcDraw.js) and raises the drawing bar -- Select/Pan and
 * Draw, the category, Space to pan -- and each stroke is saved as an ordinary
 * ROI (the ROI plugin's `create_roi`, through `/regions/draw`) and taken into
 * QC at once, with the channels that were on screen kept beside it. The ROI
 * panel is never opened; opened later, it shows these regions like any other,
 * and edits made to them there are taken in as they are saved (watchRoi).
 *
 * ONE TOOL OPEN AT A TIME, as the sidebar keeps one card: unfolding a tool
 * folds the others. Each header ends in the whole tool's eye: Registration
 * QC's is the check itself (on/off, as the server keeps it); Segmentation
 * QC's and ROI QC's hide everything the tool draws while every row keeps its
 * own eye, and showing any one row turns its tool back on. Those two are not
 * remembered.
 *
 * Which rows are hidden, and which tools are folded, are per-viewer
 * conveniences kept in localStorage and wrapped in try/catch: a private
 * window or blocked storage must still get a working panel with everything
 * shown and every tool open.
 */
class QcSidebarController {

    constructor(ctx) {
        this.ctx = ctx;
        this.api = new QcApi(ctx);
        this.state = null;
        this.regionData = { regions: [] };
        this.cellData = null;
        this.vocabulary = null;
        this.busy = false;
        this.session = null;          // {state: "running"|"finished", phase}
        this.selectedRegion = null;
        this._messageTimer = null;
        this._reloading = null;
        this.hidden = this.loadHidden();
        this.folds = this.loadFolds();
        this.roiMuted = false;        // ROI QC's header eye, off
        this._channelsQueue = Promise.resolve();
        this._colorTimers = new Map();
        this._roiWatch = null;        // {store, unsubscribe, known, pending, signature}
        this._drawnTimer = null;

        this.overlay = new QcRegionOverlay(ctx);
        this.overlay.isVisible = (region) => this.regionVisible(region);
        this.freehand = new QcFreehand(ctx, this.overlay);
        this.freehand.onStroke = (points) => this.saveStroke(points);
        this.freehand.onModeChange = () => this.renderDrawbar();
        this.drawTarget = null;       // {target, label, words, color}: the category in hand
        this.cellLayer = new QcCellLayer(ctx, QcSidebarController.PLUGIN);
        this.cellLayer.isVisible = (group) => this.cellGroupVisible(group);
        // The free image checks (qcRegistration.js, qcBlur.js, qcSegmentation.js).
        this.registration = new QcRegistration(ctx, this.api, this);
        this.segmentation = new QcSegmentationQc(ctx, this.api, this);
        this.blur = new QcBlurQc(ctx, this.api, this);
        this.tree = new QcTree({
            listId: "qc_tree",
            // Clean channels are the uninteresting majority: folded until asked.
            collapsed: ["h:clean"],
            onActivate: (spec) => this.activate(spec),
            onEye: (spec) => this.toggleHidden(spec.key),
            onMenu: (spec) => this.menuFor(spec),
            onColor: (spec, hex) => this.setColor(spec, hex),
        });
    }

    static get PLUGIN() { return "qc"; }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        this.el("qc_strictness")?.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-preset]");
            if (button) this.setStrictness(button.dataset.preset);
        });
        this.el("qc_refresh")?.addEventListener("click", () => this.refresh());
        this.el("qc_draw")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openDrawMenu(event.currentTarget);
        });
        this.el("qc_start_ai")?.addEventListener("click", () => this.askForSession());
        for (const tool of document.querySelectorAll("#qc_panel_section .qc-tool[data-tool]")) {
            tool.querySelector(".qc-tool-fold")?.addEventListener("click",
                () => this.toggleFold(tool.dataset.tool));
        }
        this.renderFolds();
        this.el("qc_roi_eye")?.addEventListener("click", () => this.setRoiMuted(!this.roiMuted));
        this.el("qc_mode_pan")?.addEventListener("click", () => this.freehand.pan());
        this.el("qc_mode_draw")?.addEventListener("click", () => {
            if (this.drawTarget) this.freehand.start(this.drawTarget.color);
        });
        this.el("qc_drawbar_category")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openDrawMenu(event.currentTarget, { align: "left" });
        });
        this.el("qc_drawbar_close")?.addEventListener("click", () => this.stopDrawing());
        this.el("qc_download")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openDownloadMenu(event.currentTarget);
        });
        this.overlay.attach();
        this.registration.setup();
        this.segmentation.setup();
        this.blur.setup();
        this.ctx.onCleanup?.(() => this.destroy());
        this.loadVocabulary();
        this.reload();
    }

    loadVocabulary() {
        return this.api.vocabulary().then((answer) => {
            if (!answer.ok) return;
            this.vocabulary = answer.data;
            this.render();
        }).catch(() => {});
    }

    onShow() {
        this.overlay.attach();
        this.registration.onShow();
        this.blur.onShow();
        this.reload();
    }

    onHide() {
        QcTree.closePopup();
        // A pen that goes on drawing over another tool's session is the
        // failure roiTools' disarm exists to prevent.
        this.stopDrawing();
        // Z / X / F belong to whichever tool is on screen; flicker waits.
        this.registration.onHide();
    }

    /** The card's eye. Core shows and hides the cell layer itself (this
     *  plugin owns one); the regions are this plugin's own overlay. */
    onVisibilityChange(visible) {
        this.overlay.setEnabled(Boolean(visible));
        if (visible) this.reload();
    }

    destroy() {
        this.registration.destroy();
        this.segmentation.destroy();
        this.blur.destroy();
        this.freehand.stop();
        window.clearTimeout(this._messageTimer);
        window.clearTimeout(this._drawnTimer);
        this._roiWatch?.unsubscribe?.();
        this._roiWatch = null;
        this._colorTimers.forEach((timer) => window.clearTimeout(timer));
        QcTree.closePopup();
        this.overlay.destroy();
        this.cellLayer.destroy();
    }

    // -- talking to the server ---------------------------------------------------

    /** Everything the panel shows, in one pass. Coalesced: a session closing
     *  units in quick succession asks once, not once per event. */
    reload() {
        if (this._reloading) {
            this._reloadAgain = true;
            return this._reloading;
        }
        this._reloading = this._reload().finally(() => {
            this._reloading = null;
            if (this._reloadAgain) {
                this._reloadAgain = false;
                this.reload();
            }
        });
        return this._reloading;
    }

    async _reload() {
        try {
            const [state, regions, cells] = await Promise.all([
                this.api.state(), this.api.regions().catch(() => null),
                this.api.cells().catch(() => null)]);
            if (!state.ok) {
                this.status("error", (state.data.error && state.data.error.message)
                    || "QC could not be read");
                return;
            }
            this.watchRoi();
            this.state = state.data;
            this.regionData = regions && regions.ok ? regions.data : { regions: [] };
            if (!this._hidOnLoad && regions && regions.ok) {
                // Every region off on load, category by category, so any one
                // category's eye (or Show all) brings its regions back.
                this._hidOnLoad = true;
                for (const region of this.regionData.regions || []) {
                    this.hidden.add(`c:${QcSidebarController.groupOf(region)}`);
                }
            }
            this.cellData = cells && cells.ok ? cells.data : null;
            if (this.selectedRegion && !this.regionData.regions.some(
                (r) => r.roi_id === this.selectedRegion)) this.selectedRegion = null;
            this.overlay.selectedId = this.selectedRegion;
            this.overlay.setRegions(this.regionData.regions || []);
            this.setCellGroups();
            this.render();
            this.registration.adopt((this.state.checks || {}).registration);
            this.segmentation.adopt((this.state.checks || {}).segmentation);
            this.blur.adopt((this.state.checks || {}).blur);
        } catch (error) {
            this.status("error", "QC could not be read");
        }
    }

    /** A `qc.session` event from the agent bridge: what the status says. */
    noteSession(payload) {
        const event = payload && payload.event;
        if (!event) return;
        if (event === "finished") {
            this.session = { state: "finished" };
        } else if (event !== "needs_setup") {
            const paused = payload.control && payload.control.state === "paused";
            this.session = { state: paused ? "paused" : "running",
                             phase: payload.phase || (this.session && this.session.phase) };
        }
        this.renderStatus();
    }

    async act(label, fn) {
        if (this.busy) return null;
        this.busy = true;
        this.el("qc_panel_section")?.classList.add("is-busy");
        try {
            const answer = await fn();
            if (!answer.ok) {
                const error = answer.data.error || {};
                this.message(error.message || `${label} failed`);
                return null;
            }
            return answer.data;
        } catch (error) {
            this.message(`${label} failed`);
            return null;
        } finally {
            this.busy = false;
            this.el("qc_panel_section")?.classList.remove("is-busy");
        }
    }

    async setStrictness(preset) {
        const done = await this.act("Changing the strictness", () => this.api.setStrictness(preset));
        if (!done) return;
        const renamed = (done.renamed || []).length;
        this.message(renamed ? `${renamed} region${renamed === 1 ? "" : "s"} changed action`
            : "Strictness changed");
        await this.reload();
    }

    /** Take in the ROI panel's edits. `views` ({roi_id: channels}) is what
     *  was on screen when each new region was drawn; `quiet` is the automatic
     *  take-in after a drawing, which only speaks when a region was added. */
    async refresh({ views = null, quiet = false } = {}) {
        const done = await this.act("Applying your edits", () => this.api.refresh(views));
        if (!done) return false;
        const sync = done.sync || {};
        const changed = ["adopted", "edited", "deleted", "relabelled", "removed"]
            .reduce((n, key) => n + ((sync[key] || []).length), 0);
        const added = (sync.adopted || []).length;
        if (!quiet) {
            this.message(changed ? `Took in ${changed} change${changed === 1 ? "" : "s"}` : "Up to date");
        } else if (added) {
            this.message(`${added} region${added === 1 ? "" : "s"} added. Draw another, or `
                + "press Esc to stop drawing");
        }
        await this.reload();
        return true;
    }

    async approve(roiId, action) {
        const done = await this.act("Approving the region", () => this.api.approve(roiId, action));
        if (!done) return;
        this.message(`Approved as ${action} and locked`);
        await this.reload();
    }

    async traceRegion(region) {
        const theirs = Boolean(region.user_edited);
        if (theirs && !window.confirm("You reshaped this region. Trace the artifact inside "
            + "your outline and replace it? Undo puts it back.")) return;
        const done = await this.act("Tracing the outline",
            () => this.api.refineRegion({ roi_id: region.roi_id, force: theirs }));
        if (!done) return;
        const traced = (done.refined || [])[0];
        const skipped = (done.skipped || [])[0];
        this.message(traced
            ? `Traced: keeps ${Math.round(100 * (traced.kept_fraction ?? 1))}% of the outline`
            : `Left as it is: ${skipped ? skipped.why : "nothing to trace"}`);
        await this.reload();
    }

    async traceAll() {
        const done = await this.act("Tracing the outlines",
            () => this.api.refineRegion({ all: true }));
        if (!done) return;
        const traced = (done.refined || []).length;
        const left = (done.skipped || []).length;
        this.message(`Traced ${traced} region${traced === 1 ? "" : "s"}`
            + (left ? `, ${left} left as they are` : ""));
        await this.reload();
    }

    static tracedWords(region) {
        const refinement = region.refinement || {};
        if (refinement.status !== "refined") return "";
        const kept = typeof refinement.kept_fraction === "number"
            ? `, ${Math.round(refinement.kept_fraction * 100)}% of its outline` : "";
        return `traced${kept}`;
    }

    async renameRegion(region) {
        const before = QcSidebarController.regionName(region, this);
        const name = window.prompt("Rename this region", before);
        if (name === null) return;
        const words = name.replace(/\s+/g, " ").trim();
        if (!words || words === before) return;
        const done = await this.act("Renaming the region",
            () => this.api.renameRegion(region.roi_id, words));
        if (!done) return;
        this.message(`Renamed "${words}"`);
        await this.reload();
    }

    /** Where a region came from, in the words its row says quietly. */
    static originOf(region) {
        const by = String(region.created_by || "agent");
        if (by === "user") return { manual: true, words: "manual" };
        if (by.startsWith("registration")) return { manual: false, words: "Derived from Registration QC" };
        if (by.startsWith("segmentation")) return { manual: false, words: "Derived from Segmentation QC" };
        if (by.startsWith("blur")) return { manual: false, words: "Derived from Blur QC" };
        return { manual: false, words: "AI" };
    }

    static regionName(region, controller) {
        if (region.name) return region.name.replace(/^QC:\s*/, "");
        return QcSidebarController.capital(region.category_words
            || controller.classWords(region.class));
    }

    async deleteRegion(region) {
        const words = this.classWords(region.class);
        if (!window.confirm(`Delete this ${words} region? The ROI is removed and its cells `
            + "stop being flagged for it.")) return;
        const done = await this.act("Deleting the region", () => this.api.deleteRegion(region.roi_id));
        if (!done) return;
        this.message("Region deleted");
        await this.reload();
    }

    // -- marking a region by hand ------------------------------------------------

    /** The ROI plugin's controller, when its tool is loaded. */
    static roiController() {
        return window.__plexora?.plugins?.get("roi")?.sidebarController || null;
    }

    /** Make the QC category (`{class}` or `{label}`) and put QC's Freehand in
     *  hand for it. The ROI tool is not opened: the user stays here. */
    async startDrawing(target) {
        const made = await this.act("Preparing the category", () => this.api.addCategory(target));
        if (!made) return false;
        if (made.custom) this.loadVocabulary();
        const words = QcSidebarController.capital(made.words || this.classWords(made.key));
        const color = made.color || this.classColor(made.key);
        this.drawTarget = { target: made.custom ? { label: made.words } : { class: made.key },
                            label: made.label, words, color };
        if (!this.freehand.start(color)) {
            this.drawTarget = null;
            this.message("The image is not ready to draw on yet");
            return false;
        }
        this.renderDrawbar();
        this.message(`Draw round the ${words.toLowerCase()}: press and drag. Hold Space to pan`);
        return true;
    }

    stopDrawing() {
        this.freehand.stop();
        this.drawTarget = null;
        this.renderDrawbar();
    }

    /** A finished stroke: an ROI in the category in hand, taken into QC. */
    async saveStroke(points) {
        const target = this.drawTarget;
        if (!target) return;
        const done = await this.act("Saving the region", () => this.api.drawRegion(
            { ...target.target, points, views: QcSidebarController.currentView() }));
        if (!done) return;
        // Known to the ROI watch already, so it is not taken in twice.
        this._roiWatch?.known.add(done.roi?.id);
        this.message(`Added "${(done.roi && done.roi.name) || target.label}"`);
        await this.reload();
        // What was just drawn is on screen, whatever its category's eye said.
        const drawn = (this.regionData.regions || []).find((r) => r.roi_id === done.roi?.id);
        if (drawn && this.show("g:regions", `c:${QcSidebarController.groupOf(drawn)}`,
                               `r:${drawn.roi_id}`)) this.redraw();
    }

    renderDrawbar() {
        const bar = this.el("qc_drawbar");
        if (!bar) return;
        const target = this.drawTarget;
        bar.hidden = !target;
        if (!target) return;
        bar.style.setProperty("--qc-row-color", target.color);
        const label = this.el("qc_drawbar_label");
        if (label) label.textContent = target.words;
        const drawing = this.freehand.drawing;
        this.el("qc_mode_draw")?.setAttribute("aria-checked", drawing ? "true" : "false");
        this.el("qc_mode_pan")?.setAttribute("aria-checked", drawing ? "false" : "true");
    }

    /** What is on screen now, as a region drawn under it should remember it:
     *  every channel switched on, with its colour and window. The window in
     *  raw units, which is what the launch path that puts it back reads (a
     *  slot's own range is byte-domain outside HD mode). */
    static currentView() {
        const panel = window.__plexora?.viewerSidebar;
        const slots = (panel && panel.channelSlots) || [];
        return slots.filter((slot) => slot && slot.enabled && slot.visible !== false && slot.name)
            .slice(0, 8).map((slot) => {
                const entry = { name: slot.name };
                if (/^#[0-9a-f]{6}$/i.test(slot.colorHex || "")) entry.color = slot.colorHex;
                let range = null;
                try {
                    range = typeof panel.toRawRangeForSlot === "function"
                        ? panel.toRawRangeForSlot(slot) : null;
                } catch (error) {
                    range = null;
                }
                if (Array.isArray(range) && range.length === 2
                        && range.every((v) => Number.isFinite(Number(v)))) {
                    entry.range = [Number(range[0]), Number(range[1])];
                }
                return entry;
            });
    }

    static isQcCategory(id) {
        return String(id || "").startsWith("qc_");
    }

    /** What, of the ROI store's QC regions, a take-in would care about. */
    static qcSignature(store) {
        return (store.features || [])
            .filter((f) => QcSidebarController.isQcCategory(f.category_id))
            .map((f) => [f.id, f.category_id, f.name, f.locked ? 1 : 0,
                         JSON.stringify(f.geometry || null).length].join(":"))
            .sort().join("|");
    }

    /**
     * Follow the ROI store: a region drawn in a QC category is taken in the
     * moment it is saved -- with the channels on screen when it appeared --
     * and so is any reshape, move or deletion of a QC region. Once per store;
     * the regions already there when watching starts are not "new".
     */
    watchRoi(roi = QcSidebarController.roiController()) {
        const store = roi && roi.store;
        if (!store || typeof store.onChange !== "function") return;
        if (this._roiWatch && this._roiWatch.store === store) return;
        this._roiWatch?.unsubscribe?.();
        const watch = {
            store,
            known: new Set((store.features || []).map((f) => f.id)),
            pending: new Map(),
            signature: QcSidebarController.qcSignature(store),
        };
        watch.unsubscribe = store.onChange(() => this.onRoiChange(watch));
        this._roiWatch = watch;
    }

    /** Only the user's own edits count: they are queued before the store
     *  announces them, while a reload -- a session's regions arriving from
     *  the server -- lands with nothing queued, and is QC's already. */
    onRoiChange(watch) {
        const store = watch.store;
        const local = Boolean(store.hasUnsavedWork);
        if (local) watch.local = true;
        for (const feature of store.features || []) {
            if (watch.known.has(feature.id)) continue;
            watch.known.add(feature.id);
            if (local && QcSidebarController.isQcCategory(feature.category_id)) {
                watch.pending.set(feature.id, QcSidebarController.currentView());
            }
        }
        const saved = store.status === "saved" && !store.hasUnsavedWork && !store._flushing;
        if (!saved) return;
        const signature = QcSidebarController.qcSignature(store);
        const mine = watch.local;
        watch.local = false;
        if (signature === watch.signature && !watch.pending.size) return;
        watch.signature = signature;
        if (!mine && !watch.pending.size) return;
        window.clearTimeout(this._drawnTimer);
        this._drawnTimer = window.setTimeout(() => this.takeInDrawn(watch), 250);
    }

    async takeInDrawn(watch) {
        if (this._roiWatch !== watch) return;
        if (this.busy) {
            this._drawnTimer = window.setTimeout(() => this.takeInDrawn(watch), 400);
            return;
        }
        const views = {};
        for (const [id, view] of watch.pending) if (view.length) views[id] = view;
        const ids = [...watch.pending.keys()];
        const done = await this.refresh({ views: Object.keys(views).length ? views : null,
                                          quiet: true });
        if (done) ids.forEach((id) => watch.pending.delete(id));
    }

    /** "Run AI QC session": a session is run by the user's agent, so the
     *  words that start one are put where they can be pasted. */
    askForSession() {
        const words = "QC this image";
        const say = (copied) => this.message(`Ask your AI agent to "${words}"`
            + (copied ? " (copied)" : "") + ". It needs Plexora Paid.");
        try {
            if (navigator.clipboard?.writeText) {
                navigator.clipboard.writeText(words).then(() => say(true), () => say(false));
                return;
            }
        } catch (error) { /* said without the copy */ }
        say(false);
    }

    // -- what is hidden -----------------------------------------------------------

    storageKey() {
        return `plexora.qc.hidden.${this.ctx.datasource || ""}`;
    }

    loadHidden() {
        try {
            const raw = window.localStorage.getItem(this.storageKey());
            const list = raw ? JSON.parse(raw) : [];
            return new Set(Array.isArray(list) ? list : []);
        } catch (error) {
            return new Set();
        }
    }

    saveHidden() {
        try {
            window.localStorage.setItem(this.storageKey(), JSON.stringify([...this.hidden]));
        } catch (error) {
            // A viewer convenience: nothing is lost that matters.
        }
    }

    /** Which of the four tools are open: {reg, blur, seg, roi}. All folded on
     *  every load, and not remembered. */
    loadFolds() {
        return { reg: false, blur: false, seg: false, roi: false };
    }

    toggleFold(key) {
        if (!(key in this.folds)) return;
        const open = !this.folds[key];
        // One open at a time: opening a tool folds the others.
        if (open) for (const other of Object.keys(this.folds)) this.folds[other] = false;
        this.folds[key] = open;
        QcTree.closePopup();
        this.renderFolds();
    }

    renderFolds() {
        for (const tool of document.querySelectorAll("#qc_panel_section .qc-tool[data-tool]")) {
            const open = this.folds[tool.dataset.tool] !== false;
            tool.dataset.open = open ? "true" : "false";
            tool.querySelector(".qc-tool-fold")?.setAttribute("aria-expanded", open ? "true" : "false");
        }
    }

    toggleHidden(key) {
        // A row asked for while ROI QC is off is shown, and ROI QC with it.
        if (this.hidden.has(key) || this.roiMuted) this.hidden.delete(key);
        else this.hidden.add(key);
        this.roiMuted = false;
        this.saveHidden();
        this.redraw();
    }

    /** ROI QC's header eye: every region and QC cell off, the rows untouched. */
    setRoiMuted(muted) {
        this.roiMuted = Boolean(muted);
        this.redraw();
    }

    show(...keys) {
        let changed = this.roiMuted;
        this.roiMuted = false;
        for (const key of keys) changed = this.hidden.delete(key) || changed;
        if (changed) this.saveHidden();
        return changed;
    }

    /** What a region is grouped under: its class, or its custom category. */
    static groupOf(region) {
        return region.category || region.class;
    }

    regionVisible(region) {
        return !this.roiMuted
            && !this.hidden.has("g:regions")
            && !this.hidden.has(`c:${QcSidebarController.groupOf(region)}`)
            && !this.hidden.has(`r:${region.roi_id}`);
    }

    /** The root a cell group sits under: whole-cell reasons under Cells, one
     *  marker's flags under Markers. */
    static rootOf(group) {
        return group && group.level === "marker" ? "g:markers" : "g:cells";
    }

    /** The QC reason groups and Segmentation QC's two, in one cell table. */
    setCellGroups() {
        this.cellLayer.setGroups([...((this.cellData && this.cellData.groups) || []),
                                  ...this.segmentation.groups()]);
    }

    cellGroupVisible(group) {
        // Segmentation QC's Under / Over have their own toggles in their row.
        if (group && group.level === "segqc") return this.segmentation.groupVisible(group.key);
        return !this.roiMuted
            && !this.hidden.has(QcSidebarController.rootOf(group))
            && !this.hidden.has(`k:${group.key}`);
    }

    redraw() {
        this.overlay.selectedId = this.selectedRegion;
        this.overlay.schedule();
        this.cellLayer.recolor();
        this.renderTree();
    }

    // -- the viewer ---------------------------------------------------------------

    /** Frame [minX, minY, maxX, maxY] (full-resolution pixels), a tenth of its
     *  size spare on every side and never tighter than `least` pixels, through
     *  core's viewport helper -- what ROI's focus_roi does. */
    fit(box, least = 0) {
        if (!box || !window.PlexoraViewerScene || !this.ctx.viewer) return false;
        let [x0, y0, x1, y1] = box;
        const size = Math.max(x1 - x0, y1 - y0, least, 1);
        const cx = (x0 + x1) / 2;
        const cy = (y0 + y1) / 2;
        const half = size * 0.6;
        x0 = Math.min(x0, cx - half); x1 = Math.max(x1, cx + half);
        y0 = Math.min(y0, cy - half); y1 = Math.max(y1, cy + half);
        try {
            return window.PlexoraViewerScene.fitRegion(this.ctx.viewer, {
                x: x0, y: y0, width: x1 - x0, height: y1 - y0 }, { immediately: false });
        } catch (error) {
            return false;
        }
    }

    static union(boxes) {
        const live = boxes.filter(Boolean);
        if (!live.length) return null;
        return [Math.min(...live.map((b) => b[0])), Math.min(...live.map((b) => b[1])),
                Math.max(...live.map((b) => b[2])), Math.max(...live.map((b) => b[3]))];
    }

    /** What was asked to be seen is drawn: this tool's layer back on if the
     *  card's eye had it off. */
    ensureDrawn() {
        const loader = window.PlexoraToolLoader;
        if (loader?.isToolVisible && !loader.isToolVisible(QcSidebarController.PLUGIN)) {
            loader.setToolVisible?.(QcSidebarController.PLUGIN, true);
        }
    }

    /** The channels a finding was called on, put up the way the agent's
     *  evidence showed them: the nuclear stain in muted blue under the first
     *  channel in yellow, the next two in cyan and magenta, each at its
     *  calibrated window (auto-levelled where there is none). A finding with
     *  no channels of its own -- a region drawn by hand -- shows the nuclear
     *  stain alone.
     *
     *  Through the channel panel's launch path with its saves suspended, as
     *  the agent bridge's `set_channels` does: this is somebody looking, and
     *  the project's saved channel list does not change. Calls are queued so
     *  that two quick clicks end on the second finding's channels. */
    showChannels(names, view = null, { asked = false } = {}) {
        // The channels on screen are the user's: only an explicit ask moves them.
        if (!asked) return Promise.resolve(false);
        const run = () => this._applyChannels(names || [], view).catch(() => false);
        this._channelsQueue = this._channelsQueue.then(run, run);
        return this._channelsQueue;
    }

    async _applyChannels(names, view = null) {
        const panel = window.__plexora?.viewerSidebar;
        if (!panel || typeof panel.applyLaunchChannels !== "function") return false;
        const known = new Set(panel.columns || []);
        const display = this.regionData.display || {};
        const colors = display.colors || {};
        const windows = display.windows || {};
        const entries = [];
        // A region drawn by hand kept what was on screen when it was drawn:
        // put exactly that back, colours and windows as they were.
        const kept = (view || []).filter((entry) => entry && known.has(entry.name));
        if (kept.length) {
            for (const entry of kept) {
                const row = { name: entry.name };
                if (entry.color) row.color = entry.color;
                if (Array.isArray(entry.range)) row.range = entry.range;
                entries.push(row);
            }
        } else {
            const markers = [...new Set(names)].filter((name) => known.has(name)).slice(0, 3);
            const nuclear = known.has(display.nuclear) ? display.nuclear : null;
            if (nuclear && !markers.includes(nuclear)) {
                entries.push({ name: nuclear, color: colors.nuclear });
            }
            const palette = [colors.marker, ...(colors.references || [])];
            markers.forEach((name, i) => entries.push({ name, color: palette[i] }));
            for (const entry of entries) {
                if (Array.isArray(windows[entry.name])) entry.range = windows[entry.name];
                if (!entry.color) delete entry.color;
            }
        }
        if (!entries.length) return false;
        // Already showing exactly these: nothing to redo, and no flicker.
        const showing = (panel.channelSlots || [])
            .filter((slot) => slot && slot.enabled && slot.name).map((slot) => slot.name);
        if (showing.length === entries.length
                && entries.every((entry, i) => showing[i] === entry.name)) {
            return true;
        }
        panel.suspendPersistence?.();
        try {
            await panel.applyLaunchChannels(entries, { silent: true });
        } finally {
            panel.resumePersistence?.();
        }
        return true;
    }

    // -- colours -------------------------------------------------------------------

    /** What a row's colour belongs to: a class for a class row and for the
     *  cells inside its regions, a reason for any other cell reason. */
    static colorTarget(spec) {
        if (spec.kind === "class" && spec.ref && spec.ref[0]) return { class: spec.ref[0].class };
        if (spec.kind === "reason" && spec.ref) {
            const reason = spec.ref.reason || "";
            return reason.startsWith("region:") ? { class: reason.slice(7) } : { reason };
        }
        return null;
    }

    /** Draw `hex` on everything the target colours, now. */
    paintColor(target, hex) {
        if (target.class) {
            for (const region of this.regionData.regions || []) {
                if (QcSidebarController.groupOf(region) === target.class) region.color = hex;
            }
        }
        const reason = target.class ? `region:${target.class}` : target.reason;
        for (const group of (this.cellData && this.cellData.groups) || []) {
            if (group.reason === reason) group.color = hex;
        }
        this.redraw();
    }

    setColor(spec, hex) {
        const target = QcSidebarController.colorTarget(spec);
        if (!target || !hex) return;
        this.paintColor(target, hex);
        this.saveColor(target, hex);
    }

    /** Saved after the picker settles; a failure puts the stored colours back. */
    saveColor(target, hex) {
        const key = target.class ? `class:${target.class}` : `reason:${target.reason}`;
        window.clearTimeout(this._colorTimers.get(key));
        this._colorTimers.set(key, window.setTimeout(async () => {
            this._colorTimers.delete(key);
            try {
                const answer = await this.api.setColor(target, hex);
                if (!answer.ok) throw new Error("refused");
            } catch (error) {
                this.message("The colour could not be saved.");
                this.reload();
            }
        }, 300));
    }

    async resetColor(spec) {
        const target = QcSidebarController.colorTarget(spec);
        if (!target) return;
        try {
            const answer = await this.api.setColor(target, null);
            if (!answer.ok) throw new Error("refused");
        } catch (error) {
            this.message("The colour could not be reset.");
        }
        this.reload();
    }

    /** "Reset colour", on a row whose colour is not its default. */
    static resetItem(spec, controller) {
        const ref = spec.kind === "class" ? spec.ref && spec.ref[0] : spec.ref;
        const current = String((ref && ref.color) || "").toLowerCase();
        const standard = String((ref && ref.default_color) || "").toLowerCase();
        if (!standard || current === standard) return [];
        return [{ label: "Reset colour", color: standard, shape: "fill",
                  onSelect: () => controller.resetColor(spec) }];
    }

    focusRegion(region) {
        this.selectedRegion = region.roi_id;
        this.show("g:regions", `c:${QcSidebarController.groupOf(region)}`, `r:${region.roi_id}`);
        this.ensureDrawn();
        this.fit(region.bbox);
        this.redraw();
    }

    focusCells(group) {
        this.show("g:cells", `k:${group.key}`);
        // Asking to see these cells is the moment QC's cell layer turns on;
        // the Cells control then keeps None for turning it off again.
        this.ctx.layers?.showCells?.();
        this.ensureDrawn();
        // A lone cell framed edge to edge is a blur; 300 px is a neighbourhood.
        this.fit(group.bbox, 300);
        this.redraw();
    }

    activate(spec) {
        const ref = spec.ref;
        if (spec.kind === "region" && ref) this.focusRegion(ref);
        else if (spec.kind === "reason" && ref) this.focusCells(ref);
        else if (spec.kind === "channel" && ref) {
            this.showChannels([ref.name], null, { asked: true });
        } else if (spec.kind === "class" && ref) {
            this.show("g:regions", spec.key);
            this.ensureDrawn();
            this.fit(QcSidebarController.union(ref.map((r) => r.bbox)));
            this.redraw();
        }
    }

    // -- menus --------------------------------------------------------------------

    copy(text) {
        const done = () => this.message("Copied");
        try {
            if (navigator.clipboard?.writeText) {
                navigator.clipboard.writeText(text).then(done, () => this.message(text));
                return;
            }
        } catch (error) { /* fall through */ }
        this.message(text);
    }

    regionDetails(region) {
        const parts = [`QC ${region.action}: ${region.category_words || this.classWords(region.class)}`,
                       `channels: ${(region.channels || []).join(", ") || "all"}`];
        if ((region.view_channels || []).length) {
            parts.push(`drawn with: ${region.view_channels.map((v) => v.name).join(", ")}`);
        }
        if (region.severity) parts.push(`severity: ${region.severity}`);
        const share = typeof region.refined_fraction === "number"
            ? region.refined_fraction : region.tissue_fraction;
        if (typeof share === "number") {
            parts.push(`tissue: ${(share * 100).toFixed(1)}%`);
        }
        const traced = QcSidebarController.tracedWords(region);
        const refinement = region.refinement || {};
        if (traced) parts.push(`outline: ${traced} (${refinement.method})`);
        else if (refinement.status) parts.push(`outline: as drawn (${refinement.reason || refinement.status})`);
        parts.push(`made by: ${region.created_by}`, `roi: ${region.roi_id}`);
        if (region.bbox) parts.push(`bbox: ${region.bbox.map((v) => Math.round(v)).join(", ")}`);
        return parts.join("\n");
    }

    menuFor(spec) {
        const hideItem = (key) => ({
            label: this.hidden.has(key) ? "Show" : "Hide",
            onSelect: () => this.toggleHidden(key),
        });
        if (spec.kind === "region") {
            const region = spec.ref;
            const approved = region.approved;
            const drawnWith = region.view_channels || [];
            const manual = QcSidebarController.originOf(region).manual;
            return [
                { label: "Zoom to region", onSelect: () => this.focusRegion(region) },
                hideItem(spec.key),
                ...(manual ? [{ label: "Rename…", disabled: Boolean(region.locked),
                                hint: region.locked ? "Locked: unlock it in the ROI panel to rename it"
                                    : "Give this region its own name",
                                onSelect: () => this.renameRegion(region) }] : []),
                ...(drawnWith.length ? [{
                    label: "Show the channels it was drawn with",
                    hint: drawnWith.map((v) => v.name).join(", "),
                    onSelect: () => this.showChannels([], drawnWith, { asked: true }) }] : []),
                { label: "Trace outline", className: "is-sectioned",
                  disabled: Boolean(region.locked),
                  hint: region.locked ? "Locked: unlock it in the ROI panel to trace it"
                      : "Redraw it round the artifact's own pixels, keeping the tissue around them",
                  onSelect: () => this.traceRegion(region) },
                { label: approved && region.action === "exclude" ? "Approved as exclude"
                    : "Approve as exclude", className: "is-sectioned",
                  disabled: approved && region.action === "exclude",
                  hint: "Pin this action whatever the strictness, and lock the shape",
                  onSelect: () => this.approve(region.roi_id, "exclude") },
                { label: approved && region.action === "warn" ? "Approved as warn"
                    : "Approve as warn", disabled: approved && region.action === "warn",
                  hint: "Keep the cells, flagged: pin the action and lock the shape",
                  onSelect: () => this.approve(region.roi_id, "warn") },
                { label: "Copy details", className: "is-sectioned",
                  onSelect: () => this.copy(this.regionDetails(region)) },
                { label: "Delete region…", className: "is-sectioned is-destructive",
                  onSelect: () => this.deleteRegion(region) },
            ];
        }
        if (spec.kind === "class") {
            const first = (spec.ref || [])[0] || {};
            const target = first.custom ? { label: first.category_words }
                : { class: spec.key.slice(2) };
            return [
                { label: "Zoom to all", onSelect: () => this.activate(spec) },
                hideItem(spec.key),
                { label: `Mark another by hand`, className: "is-sectioned",
                  hint: "Draw another region in this category, with Freehand",
                  onSelect: () => this.startDrawing(target) },
                ...QcSidebarController.resetItem(spec, this),
            ];
        }
        if (spec.kind === "reason") {
            const group = spec.ref;
            const solo = () => {
                for (const other of (this.cellData?.groups || [])) {
                    const key = `k:${other.key}`;
                    if (other.key === group.key) this.hidden.delete(key);
                    else this.hidden.add(key);
                }
                this.hidden.delete(QcSidebarController.rootOf(group));
                this.saveHidden();
                this.focusCells(group);
            };
            const words = group.level === "marker"
                ? (group.status === "unreliable" ? "value unreliable" : "value flagged")
                : (group.status === "fail" ? "excluded" : "flagged");
            return [
                { label: "Frame these cells", onSelect: () => this.focusCells(group) },
                hideItem(spec.key),
                { label: "Show only these", onSelect: solo },
                ...QcSidebarController.resetItem(spec, this),
                { label: "Copy details", className: "is-sectioned",
                  onSelect: () => this.copy([`${group.label} (${words})`,
                      `${group.count} cells`, group.definition,
                      group.evidence_channels?.length
                          ? `channels: ${group.evidence_channels.join(", ")}` : ""]
                      .filter(Boolean).join("\n")) },
            ];
        }
        if (spec.kind === "channel") {
            const channel = spec.ref;
            return [{ label: "Copy details", onSelect: () => this.copy([
                channel.name, `status: ${this.channelWords(channel.status)}`,
                channel.reason ? `reason: ${channel.reason}` : "",
                channel.cycle !== undefined && channel.cycle !== null ? `cycle: ${channel.cycle}` : "",
            ].filter(Boolean).join("\n")) }];
        }
        if (spec.key === "g:regions" || spec.key === "g:cells" || spec.key === "g:markers") {
            const group = spec.key.slice(2);
            const keys = group === "regions"
                ? [...new Set((this.regionData.regions || []).flatMap(
                    (r) => [`c:${QcSidebarController.groupOf(r)}`, `r:${r.roi_id}`]))]
                : (this.cellData?.groups || [])
                    .filter((g) => QcSidebarController.rootOf(g) === spec.key)
                    .map((g) => `k:${g.key}`);
            return [
                { label: "Show all", onSelect: () => {
                    this.show(spec.key, ...keys);
                    this.redraw();
                } },
                { label: "Hide all", onSelect: () => {
                    this.hidden.add(spec.key);
                    this.saveHidden();
                    this.redraw();
                } },
                ...(group === "regions" ? [{
                    label: "Trace all outlines", className: "is-sectioned",
                    disabled: !(this.regionData.regions || []).length,
                    hint: "Redraw every region round its artifact's own pixels",
                    onSelect: () => this.traceAll() }] : []),
                group === "regions"
                    ? { label: "Download regions (GeoJSON)", className: "is-sectioned",
                        onSelect: () => this.download("regions.geojson") }
                    : { label: "Download cells (CSV)", className: "is-sectioned",
                        disabled: !this.cellData?.available,
                        onSelect: () => this.download("cells.csv") },
            ];
        }
        return [];
    }

    download(kind) {
        const link = document.createElement("a");
        link.href = QcApi.downloadUrl(this.ctx.url, this.ctx.datasource, kind);
        if (kind === "report.html") {
            link.target = "_blank";
            link.rel = "noopener";
        } else {
            link.download = "";
        }
        document.body.appendChild(link);
        link.click();
        link.remove();
    }

    openDownloadMenu(anchor) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const s = this.state || {};
        const hasResult = Boolean(s.summary);
        QcTree.menu(anchor, [
            { label: "Cells (CSV)", disabled: !this.cellData?.available,
              hint: "Every cell's pass / warn / exclude call and its reasons",
              onSelect: () => this.download("cells.csv") },
            { label: "Regions (GeoJSON)", disabled: !hasResult,
              hint: "The QC regions with their class and action",
              onSelect: () => this.download("regions.geojson") },
            { label: "Report", disabled: !(s.provenance && s.provenance.result_id
                                           && s.provenance.session_id),
              hint: "The session's report, in a new tab",
              onSelect: () => this.download("report.html") },
        ], { heading: "Download" });
    }

    /**
     * The category picker: "Custom" first -- a field, muted, that names a new
     * QC category -- then the categories named on this project, then every
     * artifact class. Choosing one starts drawing it (startDrawing); a name
     * typed that is already a category chooses that one.
     */
    openDrawMenu(anchor, options = {}) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const classes = ((this.vocabulary || {}).classes || [])
            .filter((item) => item.id !== "uncertain_manual_review");
        if (!classes.length) {
            this.message("The artifact classes have not loaded yet");
            return;
        }
        const custom = (this.vocabulary || {}).custom || [];

        const row = document.createElement("label");
        row.className = "qc-picker-custom";
        const icon = document.createElement("span");
        icon.className = "fas fa-plus";
        icon.setAttribute("aria-hidden", "true");
        const input = document.createElement("input");
        input.type = "text";
        input.className = "qc-picker-input";
        input.placeholder = "Custom";
        input.maxLength = 60;
        input.spellcheck = false;
        input.autocomplete = "off";
        input.setAttribute("aria-label", "Name a custom QC category, then press Enter");
        row.title = "Name your own QC category, then press Enter to draw it";
        row.append(icon, input);
        input.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            const words = input.value.replace(/\s+/g, " ").trim();
            if (!words) {
                row.classList.add("is-refused");
                return;
            }
            const folded = words.toLowerCase();
            const known = classes.find((item) => item.words.toLowerCase() === folded
                || item.id === folded.replace(/[^a-z0-9]+/g, "_"));
            QcTree.closePopup();
            this.startDrawing(known ? { class: known.id } : { label: words });
        });
        input.addEventListener("input", () => row.classList.remove("is-refused"));

        const items = [
            ...custom.map((item) => ({
                label: item.words, color: item.color,
                hint: `Draw a "${item.words}" region`,
                onSelect: () => this.startDrawing({ label: item.words }),
            })),
            ...classes.map((item, i) => ({
                label: QcSidebarController.capital(item.words), color: item.color,
                className: i === 0 && custom.length ? "is-sectioned" : "",
                hint: `Draw a region of ${item.words}`,
                onSelect: () => this.startDrawing({ class: item.id }),
            })),
        ];
        QcTree.menu(anchor, items, { heading: "Mark a region by hand", before: row,
                                     className: "qc-picker", align: options.align });
    }

    // -- drawing the panel ----------------------------------------------------------

    static capital(text) {
        const s = String(text || "");
        return s ? s[0].toUpperCase() + s.slice(1) : s;
    }

    classColor(id) {
        const found = ((this.vocabulary || {}).classes || []).find((item) => item.id === id);
        return found ? found.color : "#9ca3af";
    }

    classWords(id) {
        const found = ((this.vocabulary || {}).classes || []).find((item) => item.id === id);
        return found ? found.words : String(id || "").replace(/_/g, " ");
    }

    channelWords(status) {
        return String(status || "not reviewed").replace(/_/g, " ");
    }

    status(state, text) {
        // An error is said where the status is, whatever was showing.
        if (state === "error") {
            const section = this.el("qc_panel_section");
            if (section) section.dataset.mode = "result";
        }
        const row = this.el("qc_status_row");
        const node = this.el("qc_status");
        if (row) row.dataset.state = state;
        if (node) {
            node.textContent = text;
            node.title = text;
        }
    }

    message(text) {
        const node = this.el("qc_message");
        if (!node) return;
        node.textContent = text;
        node.hidden = false;
        window.clearTimeout(this._messageTimer);
        this._messageTimer = window.setTimeout(() => { node.hidden = true; }, 5000);
    }

    /** Before any QC, one line; once there is something, the result. */
    renderMode() {
        const section = this.el("qc_panel_section");
        if (!section || !this.state) return;
        const live = this.session && (this.session.state === "running"
            || this.session.state === "paused");
        const anything = Boolean(this.state.summary) || (this.regionData.regions || []).length > 0;
        const mode = anything || live ? "result" : "empty";
        if (section.dataset.mode !== mode) {
            section.dataset.mode = mode;
            QcTree.closePopup();
        }
    }

    render() {
        this.renderMode();
        this.renderStatus();
        this.renderToolbar();
        this.renderTree();
    }

    renderStatus() {
        const s = this.state;
        if (!s) return;
        this.renderMode();
        const preset = (s.strictness && s.strictness.preset) || "standard";
        // The strictness decides what a session's regions do; regions marked
        // by hand keep the action they were given, so until a session has
        // decided some there is nothing for it to change.
        const strictness = this.el("qc_strictness");
        if (strictness) {
            strictness.hidden = !((s.provenance && s.provenance.session_id)
                || (this.regionData.regions || []).some((r) => r.created_by !== "user"));
        }
        for (const button of document.querySelectorAll("#qc_strictness button[data-preset]")) {
            const on = button.dataset.preset === preset;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", on ? "true" : "false");
        }
        const provenance = s.provenance || {};
        const live = this.session && this.session.state;
        if (live === "running" || live === "paused") {
            const phase = this.session.phase ? ` · ${String(this.session.phase).replace(/_/g, " ")}` : "";
            this.status(live, `${live === "paused" ? "Session paused" : "Session running"}${phase}`);
        } else if (!s.summary) {
            this.status("none", "No QC yet");
        } else if (provenance.origin === "manual" && !provenance.session_id) {
            this.status("manual", "Marked by hand");
        } else {
            this.status("done", "QC complete");
        }
    }

    renderToolbar() {
        const node = this.el("qc_summary");
        if (!node) return;
        const summary = (this.state || {}).summary;
        const drawn = (this.regionData.regions || []).length;
        const download = this.el("qc_download");
        if (download) download.hidden = !summary;
        const refresh = this.el("qc_refresh");
        if (refresh) refresh.hidden = !summary && !drawn;
        if (!summary) {
            node.textContent = drawn ? String(drawn) : "";
            node.title = "";
            return;
        }
        const regions = summary.regions || {};
        const cells = summary.cells || {};
        const nRegions = (regions.exclude || 0) + (regions.warn || 0);
        // The header's bracket holds the count; the cells are in the title --
        // "ROI (12 regions · 1,204 cells out)" was more than the line could
        // hold beside + and the eye.
        node.textContent = `${nRegions} region${nRegions === 1 ? "" : "s"}`;
        node.title = `${regions.exclude || 0} excluding, ${regions.warn || 0} flagging`
            + (cells.n ? `; ${cells.n_fail || 0} cells excluded, ${cells.n_warn || 0} flagged`
                + `, ${cells.n_marker_flagged || 0} with a marker flagged` : "");
    }

    /** Every row, in order, for QcTree. */
    specs() {
        const out = [];
        const s = this.state || {};
        const regions = this.regionData.regions || [];
        const hid = (key) => this.hidden.has(key);

        // Regions, by category (an artifact class, or one the user named) --
        // only once there is one: an empty Regions is a container for nothing.
        if (regions.length) {
            out.push({ key: "g:regions", level: 0, kind: "group", icon: "draw-polygon",
                       label: "Regions", count: regions.length, expandable: true,
                       activatable: false, eye: true, menu: true,
                       hidden: hid("g:regions"), ownHidden: hid("g:regions") });
        }
        const byClass = new Map();
        for (const region of regions) {
            const group = QcSidebarController.groupOf(region);
            if (!byClass.has(group)) byClass.set(group, []);
            byClass.get(group).push(region);
        }
        const order = ((this.vocabulary || {}).classes || []).map((c) => c.id);
        const classes = [...byClass.keys()].sort((a, b) => {
            const ia = order.indexOf(a);
            const ib = order.indexOf(b);
            return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
        });
        for (const klass of classes) {
            const members = byClass.get(klass);
            const color = members[0].color || this.classColor(klass);
            const classKey = `c:${klass}`;
            const nExclude = members.filter((r) => r.action !== "warn").length;
            const words = QcSidebarController.capital(members[0].category_words
                || this.classWords(klass));
            out.push({ key: classKey, level: 1, kind: "class", ref: members, color,
                       shape: nExclude ? "fill" : "ring",
                       label: words,
                       title: `${words}${members[0].custom ? " (yours)" : ""}: `
                           + `${nExclude} excluding, ${members.length - nExclude} flagging`,
                       count: members.length, expandable: true, eye: true, menu: true,
                       colorable: true, colorTitle: "Colour of these regions",
                       hidden: hid("g:regions") || hid(classKey), ownHidden: hid(classKey) });
            members.forEach((region, index) => {
                const key = `r:${region.roi_id}`;
                const channels = region.channels || [];
                const label = channels.length ? channels.join(", ") : "All channels";
                const origin = QcSidebarController.originOf(region);
                const share = typeof region.refined_fraction === "number"
                    ? region.refined_fraction : region.tissue_fraction;
                const tissue = typeof share === "number"
                    ? ` · ${(share * 100).toFixed(1)}% of tissue` : "";
                const traced = QcSidebarController.tracedWords(region);
                const drawn = (region.view_channels || []).map((v) => v.name);
                out.push({
                    key, level: 2, kind: "region", ref: region, color,
                    shape: region.action === "warn" ? "ring" : "fill",
                    // Its own name, without the "QC: " every QC ROI carries in
                    // the ROI panel -- everything here is QC.
                    label: String(region.name || "").replace(/^QC:\s*/, "")
                        || `${index + 1}. ${label}`,
                    // Provenance, not a finding: muted, beside the name.
                    note: origin.words,
                    title: `${region.action === "warn" ? "Flags" : "Excludes"} cells · `
                        + `${label}${tissue}${traced ? ` · ${traced}` : ""}`
                        + `${origin.manual ? " · drawn by hand" : ` · ${origin.words}`}`
                        + `${drawn.length ? ` · drawn with ${drawn.join(", ")}` : ""}`
                        + `${region.approved ? " · approved" : ""}`,
                    // Only the exception is spelled out: a filled dot already
                    // says "excludes", and EXCLUDE on every row is a column of
                    // red that drowns the one WARN it exists to set apart.
                    tag: region.action === "warn" ? "warn" : "",
                    tagTone: "warn",
                    locked: region.approved, selected: region.roi_id === this.selectedRegion,
                    eye: true, menu: true,
                    hidden: !this.regionVisible(region), ownHidden: hid(key),
                });
            });
        }

        // Cells, by reason and status: the whole cell's calls.
        const cells = this.cellData;
        const every = (cells && cells.groups) || [];
        const groups = every.filter((g) => g.level !== "marker");
        const markerGroups = every.filter((g) => g.level === "marker");
        // Cells only once cells were called: before that there is nothing to
        // list, and "no cells checked yet" is a heading over an empty box.
        if (cells && cells.available) {
            out.push({ key: "g:cells", level: 0, kind: "group", icon: "braille", label: "Cells",
                       count: (cells.n_fail || 0) + (cells.n_warn || 0), expandable: true,
                       activatable: false,
                       title: `${(cells.n_fail || 0).toLocaleString()} excluded, `
                           + `${(cells.n_warn || 0).toLocaleString()} flagged, of `
                           + `${(cells.n || 0).toLocaleString()} cells`,
                       eye: groups.length > 0, menu: true,
                       hidden: hid("g:cells"), ownHidden: hid("g:cells") });
            if (!groups.length) {
                out.push({ key: "e:cells", level: 1, kind: "empty", label: "No cells flagged",
                           muted: true, activatable: false, shape: "none",
                           title: cells.note || "No cells flagged" });
            }
        }
        const reasonRow = (group) => {
            const key = `k:${group.key}`;
            const marker = group.level === "marker";
            const strong = marker ? group.status === "unreliable" : group.status === "fail";
            const words = marker ? (strong ? "with this marker unreliable" : "with this marker "
                + "flagged") : (strong ? "excluded" : "flagged");
            // Cells inside a region drawn by hand are there because of that
            // outline, not because QC found anything in them: said quietly.
            const from = (group.derived_from || []).map((r) => r.name).filter(Boolean);
            const note = from.length ? `Derived from: ${from.slice(0, 2).join(", ")}`
                + (from.length > 2 ? ` +${from.length - 2}` : "") : "";
            return {
                key, level: 1, kind: "reason", ref: group, color: group.color,
                shape: strong ? "fill" : "ring", label: group.label, note,
                title: `${group.label}: ${group.count.toLocaleString()} cells ${words}`
                    + `${group.definition ? ` — ${group.definition}` : ""}`
                    + `${group.truncated ? " (list truncated)" : ""}`,
                tag: strong ? "" : "warn", tagTone: "warn",
                count: group.count, eye: true, menu: true, colorable: true,
                colorTitle: group.reason.startsWith("region:")
                    ? "Colour of these regions and their cells" : "Colour of these cells",
                hidden: !this.cellGroupVisible(group), ownHidden: hid(key),
            };
        };
        for (const group of groups) out.push(reasonRow(group));

        // Markers: one channel's value unreliable in these cells, the cells
        // kept -- only when a marker was flagged in some cell.
        if (cells && cells.available && markerGroups.length) {
            out.push({ key: "g:markers", level: 0, kind: "group", icon: "vial",
                       label: "Markers", count: cells.n_marker_flagged || 0, expandable: true,
                       activatable: false,
                       title: `${(cells.n_marker_flagged || 0).toLocaleString()} cells with a `
                           + "marker whose value should not be read; the cells themselves are kept",
                       eye: markerGroups.length > 0, menu: true,
                       hidden: hid("g:markers"), ownHidden: hid("g:markers") });
            for (const group of markerGroups) out.push(reasonRow(group));
        }

        // Channels, flagged first.
        const channels = s.channels || [];
        const flaggedChannels = channels.filter((c) => c.status && c.status !== "clean");
        const clean = channels.filter((c) => c.status === "clean");
        // Channels only once a session checked them.
        if (channels.length) {
            out.push({ key: "g:channels", level: 0, kind: "group", icon: "layer-group",
                       label: "Channels", count: channels.length, expandable: true,
                       activatable: false, eye: false, menu: false,
                       title: `${flaggedChannels.length} flagged, ${clean.length} clean` });
        }
        const channelRows = (list, head, color, shape) => {
            if (!list.length) return;
            const headKey = `h:${head}`;
            out.push({ key: headKey, level: 1, kind: "status", color, shape,
                       label: head === "flagged" ? "Flagged" : "Clean", count: list.length,
                       expandable: true, activatable: false });
            for (const channel of list) {
                const reason = channel.reason ? ` — ${channel.reason}` : "";
                out.push({
                    key: `ch:${channel.name}`, level: 2, kind: "channel", ref: channel,
                    color, shape, label: channel.name,
                    title: `${channel.name}: ${this.channelWords(channel.status)}${reason}`,
                    tag: channel.cycle !== undefined && channel.cycle !== null
                        ? `c${channel.cycle}` : "",
                    tagTone: "plain", eye: false, menu: true,
                });
            }
        };
        channelRows(flaggedChannels, "flagged", "var(--accent-warning)", "ring");
        channelRows(clean, "clean", "var(--accent-success)", "fill");
        return out;
    }

    renderTree() {
        this.tree.render(this.specs());
        const eye = this.el("qc_roi_eye");
        if (eye) {
            eye.setAttribute("aria-pressed", this.roiMuted ? "false" : "true");
            eye.title = this.roiMuted ? "Show ROI QC" : "Hide everything ROI QC draws";
        }
        this.el("qc_tool_roi")?.classList.toggle("is-muted", this.roiMuted);
    }
}

window.QcSidebarController = QcSidebarController;

if (window.Plexora) {
    window.Plexora.registerPlugin({
        name: "qc",
        help: {
            summary: "Find imaging artifacts (folds, blur, aggregates, failed channels, lost "
                + "tissue) and flag the cells they affect. Regions are ROIs you can edit; "
                + "nothing is deleted from your data.",
            notes: [
                "Three tools, each folded by its header, one open at a time: "
                + "Registration QC (do the DNA channels line up?), Segmentation QC "
                + "(does the mask hold its nuclei?) and ROI QC (which regions are "
                + "artifacts?). Play runs a tool; the eye at the end of a header hides "
                + "everything that tool draws.",
                "Registration QC: < and > (or Z / X) step the comparison channel; "
                + "the flicker glyph (or F) stripes the misaligned areas, and any patch "
                + "where the two cycles plainly differ, on and off. The heatmap and "
                + "vector field glyphs beside it are both scaled to this image.",
                "Click a region to fit the view to it; click a cell reason to frame the "
                + "cells it flagged. Your channels are never changed by a click.",
                "Click a category's or a reason's dot to change its colour.",
                "A filled dot excludes; a ring only flags. The eyes hide what is drawn, "
                + "never what is counted.",
                "Cells lists what fails or flags the whole cell; Markers lists one "
                + "channel's value that should not be read in those cells, the cells kept.",
            ],
            docs: "plugins/qc",
        },
        /**
         * QC colours the cells it flagged, so it holds a cell layer of its own:
         * core's Cells control, the card's eye and opacity, the mask when there
         * is one and centroids when there is not. The colours are QC's, through
         * setCellColorLUT (see qcLayers.js).
         */
        ownsCellLayer: true,
        //: Outlines: a flagged cell drawn as a ring leaves the tissue it is
        //: flagged for visible, which is what a reviewer is looking at. Core
        //: falls back to centroids without a mask.
        preferredCellMode: "outlines",
        //: Only the regions are drawn until the user asks for the cells (a
        //: click on a Cells row, which calls ctx.layers.showCells), and None
        //: stays on the Cells control so they can be turned off again.
        cellLayerOnDemand: true,
        supportedCellModes: ["none", "centroids", "outlines", "filled"],
        /**
         * The selection-provider shape core expects from a cell layer's owner,
         * answering "nothing" on purpose, as Cell Explorer does: that interface
         * is about gating, and an empty gate would hide every cell. What colour
         * a cell is goes through setCellColorLUT instead.
         */
        createInstance() {
            return {
                getSelectedIds: async () => new Set(),
                supportsColorCoding: () => false,
                getColorCodedRanges: () => ({}),
            };
        },
        createSidebarController(ctx) {
            return new QcSidebarController(ctx);
        },
    });
}
