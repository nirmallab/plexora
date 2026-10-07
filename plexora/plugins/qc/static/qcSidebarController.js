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
        this.lastTarget = null;       // the last category drawn in, for E with nothing in hand
        // Magic select (qcDraw.js QcMagic): click an artifact, its outline is a region.
        this.magic = new QcMagic(ctx, this.overlay, this.freehand);
        this.magic.selected = () => this.magicSelected();
        this.magic.canCreate = () => Boolean(this.drawTarget);
        this.magic.onCommit = (change) => this.saveGeometry(change);
        this.magic.onMessage = (text) => this.message(text);
        this.magic.onModeChange = () => this.renderDrawbar();
        this.magic.onEscape = () => this.setDrawMode("pan");
        this.magic.onClose = () => this.setDrawMode("pan");
        this.magic.onBusy = (busy) => {
            this.el("qc_mode_magic")?.setAttribute("aria-busy", busy ? "true" : "false");
        };
        this._onMagicKey = (event) => this.magicKey(event);
        this.cellLayer = new QcCellLayer(ctx, QcSidebarController.PLUGIN);
        this.cellLayer.isVisible = (group) => this.cellGroupVisible(group);
        this._cellIndex = null;       // Map<cell id, [group]>, built on a hover's demand
        // What a region or a flagged cell says under the pointer (qcHover.js).
        this.hover = new QcHoverProbe(ctx, {
            overlay: this.overlay,
            api: this.api,
            toImage: (position) => this.freehand.toImage(position),
            imagePerScreen: () => this.freehand.imagePerScreen(),
            isSuppressed: () => this.hoverSuppressed(),
            isCellLayerOn: () => this.cellLayerShown(),
            helpers: this.hoverHelpers(),
            isFindingVisible: (level, entry) => this.findingVisible(level, entry),
            cellGroupsFor: (id) => this.cellGroupsFor(id),
            onSelect: (region) => this.focusRegion(region),
            onSelectCell: (record, shape) => this.focusCell(record, shape),
            // The Artifact Detector's objects (qcArtifacts.js): one click owner.
            hitArtifacts: (x, y, opts) => this.artifacts?.hitTest(x, y, opts) || null,
            artifactModel: (hits) => this.artifacts?.cardModel(hits) || null,
            onSelectArtifacts: (hits, at) => this.artifacts?.onClick(hits, at),
        });
        // The free image checks (qcRegistration.js, qcBlur.js, qcSegmentation.js).
        this.registration = new QcRegistration(ctx, this.api, this);
        this.segmentation = new QcSegmentationQc(ctx, this.api, this);
        this.blur = new QcBlurQc(ctx, this.api, this);
        this.artifacts = new QcArtifactsQc(ctx, this.api, this);
        this.tree = new QcTree({
            listId: "qc_tree",
            onActivate: (spec) => this.activate(spec),
            onEye: (spec) => this.toggleHidden(spec.key),
            onMenu: (spec, anchor) => this.menuFor(spec, anchor),
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
        this.el("qc_magic")?.addEventListener("click", (event) => {
            event.stopPropagation();
            // The header wand puts magic select away when it is on (E goes
            // back to drawing instead, the draw bar's own toggle).
            if (this.magic.active) this.stopDrawing();
            else this.toggleMagic(event.currentTarget);
        });
        this.el("qc_start_ai")?.addEventListener("click", () => this.askForSession());
        for (const tool of document.querySelectorAll("#qc_panel_section .qc-tool[data-tool]")) {
            tool.querySelector(".qc-tool-fold")?.addEventListener("click",
                () => this.toggleFold(tool.dataset.tool));
        }
        this.renderFolds();
        this.el("qc_roi_eye")?.addEventListener("click", () => this.setRoiMuted(!this.roiMuted));
        this.el("qc_mode_pan")?.addEventListener("click", () => this.setDrawMode("pan"));
        this.el("qc_mode_draw")?.addEventListener("click", () => this.setDrawMode("draw"));
        this.el("qc_mode_magic")?.addEventListener("click", () => this.setDrawMode("magic"));
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
        this.hover.arm();
        this.registration.setup();
        this.segmentation.setup();
        this.blur.setup();
        this.artifacts.setup();
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
        this.hover.arm();
        document.removeEventListener("keydown", this._onMagicKey);
        document.addEventListener("keydown", this._onMagicKey);
        this.registration.onShow();
        this.blur.onShow();
        this.artifacts.onShow();
        this.reload();
    }

    onHide() {
        QcTree.closePopup();
        // A pen that goes on drawing over another tool's session is the
        // failure roiTools' disarm exists to prevent.
        this.stopDrawing();
        // E belongs to whichever tool is on screen.
        document.removeEventListener("keydown", this._onMagicKey);
        // The card speaks for this panel's regions; another tool's turn now.
        this.hover.disarm();
        // Z / X / F belong to whichever tool is on screen; flicker waits.
        this.registration.onHide();
    }

    /** The card's eye. Core shows and hides the cell layer itself (this
     *  plugin owns one); the regions are this plugin's own overlay. */
    onVisibilityChange(visible) {
        this.overlay.setEnabled(Boolean(visible));
        if (visible) {
            this.hover.arm();
            this.reload();
        } else {
            this.hover.disarm();
        }
    }

    destroy() {
        this.registration.destroy();
        this.segmentation.destroy();
        this.blur.destroy();
        this.artifacts.destroy();
        this.freehand.stop();
        this.magic.stop();
        document.removeEventListener("keydown", this._onMagicKey);
        window.clearTimeout(this._messageTimer);
        window.clearTimeout(this._drawnTimer);
        this._roiWatch?.unsubscribe?.();
        this._roiWatch = null;
        this._colorTimers.forEach((timer) => window.clearTimeout(timer));
        QcTree.closePopup();
        this.hover.destroy();
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
            // A project QC wrote before the five categories: one refresh moves
            // its regions into them (the read itself never writes).
            if (((state.data.sync || {}).legacy || []).length && !this._migrating) {
                this._migrating = true;
                await this.api.refresh(null).catch(() => null);
                this._migrating = false;
                this._reloadAgain = true;
            }
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
            this.hover.revalidate();
            this.render();
            this.registration.adopt((this.state.checks || {}).registration);
            this.segmentation.adopt((this.state.checks || {}).segmentation);
            this.blur.adopt((this.state.checks || {}).blur);
            this.artifacts.adopt((this.state.checks || {}).artifacts);
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
            const before = this.session || {};
            this.session = { state: paused ? "paused" : "running",
                             phase: payload.phase || before.phase,
                             jobId: payload.job_id || before.jobId,
                             job: before.job };
            // The first packet ends the scan: nothing more to follow.
            if (event === "issued") this.session.job = null;
        }
        this.renderStatus();
        if (this.session && this.session.jobId && event === "started") this.followBulk();
    }

    /** While the session's scan and checks run (minutes on a large image, and
     *  nothing else to see), show the job's step in the status line. */
    async followBulk() {
        if (this._followingBulk) return;
        this._followingBulk = true;
        try {
            while (this.session && this.session.state !== "finished" && this.session.jobId) {
                const answer = await this.api.job(this.session.jobId).catch(() => null);
                const job = answer && answer.ok ? answer.data.job : null;
                if (!job || !["queued", "running"].includes(job.status)) {
                    if (this.session) this.session.job = null;
                    this.renderStatus();
                    break;
                }
                this.session.job = job.progress || {};
                this.renderStatus();
                await new Promise((resolve) => setTimeout(resolve, 2000));
            }
        } finally {
            this._followingBulk = false;
        }
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
        const method = QcSidebarController.methodWords(region);
        if (by === "user") return { manual: true, words: method === "magic select" ? method : "manual" };
        if (by === "agent" && method === "magic select (automatic)") {
            return { manual: false, words: "AI, with magic select" };
        }
        if (by.startsWith("registration")) return { manual: false, words: "Derived from Registration QC" };
        if (by.startsWith("segmentation")) return { manual: false, words: "Derived from Segmentation QC" };
        if (by.startsWith("blur")) return { manual: false, words: "Derived from Blur QC" };
        if (by.startsWith("artifacts")) return { manual: false, words: "Derived from the Artifact Detector" };
        return { manual: false, words: "AI" };
    }

    /** How a region's outline was made, in the words a person reads. The
     *  technical values (`sam`, `sam_agent`) never reach the screen. */
    static methodWords(region) {
        switch (region && region.method) {
            case "sam": return "magic select";
            case "sam_agent": return "magic select (automatic)";
            case "freehand": case "polygon": case "rectangle": case "ellipse": return "drawn by hand";
            case "traced": return "traced to the artifact's pixels";
            case "map": return "the check's score map";
            case "grid": return "grid squares";
            case "envelope": return "the detector's outline";
            default: return null;
        }
    }

    /** A consolidated region's other findings, one clause each ("out of
     *  focus in DNA_1 (34%)"), or "" when it holds only its own. */
    static alsoWords(region) {
        const others = (region.findings || []).filter((f) => !f.primary);
        const seen = new Set();
        return others.map((f) => {
            const where = (f.channels || []).join(", ") || "all channels";
            const part = typeof f.share === "number" && f.share < 0.95
                ? ` (${Math.round(f.share * 100)}%)` : "";
            return `${f.words} in ${where}${part}`;
        }).filter((text) => !seen.has(text) && seen.add(text)).join("; ");
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

    /** `ids` deleted at once (deleteRegions), then the message and reload
     *  every destructive region action needs; `not_deleted` may refuse some
     *  (a locked one, say). */
    async deleteManyRegions(ids) {
        if (!ids.length) return;
        const done = await this.act(`Deleting ${ids.length} region${ids.length === 1 ? "" : "s"}`,
            () => this.api.deleteRegions(ids));
        if (!done) return;
        const failed = (done.not_deleted || []).length;
        const removed = ids.length - failed;
        this.message(failed ? `Deleted ${removed} region${removed === 1 ? "" : "s"}, `
            + `${failed} could not be removed` : `Deleted ${removed} region${removed === 1 ? "" : "s"}`);
        await this.reload();
    }

    /** The regions behind a "region:" cell reason -- delete them and their
     *  cells stop being flagged. */
    async deleteReasonRegions(regions) {
        const ids = regions.map((r) => r.roi_id);
        if (!ids.length) return;
        if (!window.confirm(`Delete ${ids.length} region${ids.length === 1 ? "" : "s"}? The `
            + "ROIs are removed and their cells stop being flagged for them.")) return;
        await this.deleteManyRegions(ids);
    }

    /** A category row's "Delete all": every member but the locked and
     *  approved ones, which are skipped and said so. */
    async deleteClassRegions(members) {
        const locked = members.filter((r) => r.locked || r.approved);
        const ids = members.filter((r) => !r.locked && !r.approved).map((r) => r.roi_id);
        if (!ids.length) return;
        if (!window.confirm(`Delete ${ids.length} region${ids.length === 1 ? "" : "s"}?`
            + (locked.length ? ` ${locked.length} locked region${locked.length === 1 ? "" : "s"} `
                + "will be skipped." : "")
            + " The ROIs are removed and their cells stop being flagged for them.")) return;
        await this.deleteManyRegions(ids);
    }

    /** `findings/dismiss` (or its restore), then the reload every act needs.
     *  `words` names what was set aside, for the message after. */
    async dismiss(body, words) {
        const done = await this.act(body.restore ? "Restoring the finding" : "Removing the finding",
            () => this.api.dismissFinding(body));
        if (!done) return null;
        this.message(body.restore ? `Restored: ${words}` : `Stopped flagging: ${words}`);
        await this.reload();
        return done;
    }

    /** "Remove this finding": a whole-cell reason or a marker's, judged wrong
     *  -- QC stops raising it here, restorable from "Set aside by you". Not
     *  for a "region:" reason: those cells follow the region (deleteReasonRegions). */
    async dismissReason(group) {
        const n = group.count || 0;
        if (!window.confirm(`Stop flagging ${n.toLocaleString()} cell${n === 1 ? "" : "s"} `
            + `for "${group.label}"? You can restore it later from Set aside by you.`)) return;
        const body = group.level === "marker"
            ? { finding: "marker", reason: group.reason, marker: group.marker }
            : { finding: "cell_reason", reason: group.reason };
        await this.dismiss(body, group.label);
    }

    // -- marking a region by hand ------------------------------------------------

    /** The ROI plugin's controller, when its tool is loaded. */
    static roiController() {
        return window.__plexora?.plugins?.get("roi")?.sidebarController || null;
    }

    /** Make the QC category -- one of the five (`{category}`), a subtype's
     *  (`{class}`, kept as the region's class), or one the user names
     *  (`{label}`) -- and put magic select (in Scribble) in hand for it. The
     *  ROI tool is not opened: the user stays here. `mode` "draw" puts QC's
     *  Freehand in hand instead; so does a server that cannot run the model. */
    async startDrawing(target, { mode = "magic" } = {}) {
        if (mode === "magic" && !(await QcSidebarController.magicUsable())) mode = "draw";
        const made = await this.act("Preparing the category", () => this.api.addCategory(target));
        if (!made) return false;
        if (made.custom) this.loadVocabulary();
        const words = QcSidebarController.capital(made.custom ? made.words
            : target.class ? this.classWords(target.class) : this.categoryWords(made.key));
        const color = made.color || this.categoryColor(made.key);
        const kept = made.custom ? { label: made.words }
            : target.class ? { class: target.class } : { category: made.key };
        this.drawTarget = { target: kept, label: made.label, words, color };
        this.lastTarget = target;
        this.hover.gesture();
        if (mode === "magic") {
            if (!this.freehand.start(color)) {
                this.drawTarget = null;
                this.message("The image is not ready to draw on yet");
                return false;
            }
            this.setDrawMode("magic");
            this.message(`Draw a line over the ${words.toLowerCase()} to outline it. The bar on `
                + "the image switches between Add, Remove, Box and Scribble");
            return true;
        }
        this.magic.stop();
        if (!this.freehand.start(color)) {
            this.drawTarget = null;
            this.message("The image is not ready to draw on yet");
            return false;
        }
        this.renderDrawbar();
        this.message(`Draw round the ${words.toLowerCase()}: press and drag. Hold Space to pan`);
        return true;
    }

    /** Whether this server can run magic select at all (it may still need
     *  its one-time setup, which arming it starts). */
    static async magicUsable() {
        const segment = window.PlexoraSegment;
        if (!segment) return false;
        try {
            const state = await segment.status();
            return !(state && (state.state === "not_installed_runtime" || state.state === "disabled"));
        } catch (error) {
            return true;
        }
    }

    stopDrawing() {
        this.magic.stop();
        this.freehand.stop();
        this.drawTarget = null;
        this.renderDrawbar();
    }

    /** The draw bar's three pointers: pan (the viewer's own drag), draw
     *  (freehand) and magic (click to outline). */
    setDrawMode(mode) {
        if (!this.drawTarget) return;
        if (mode === "magic") {
            this.freehand.pan();
            this.magic.start(this.drawTarget.color);
        } else if (mode === "draw") {
            this.magic.stop();
            this.freehand.start(this.drawTarget.color);
        } else {
            this.magic.stop();
            this.freehand.pan();
        }
        this.renderDrawbar();
    }

    /** E, while this panel is the tool on screen: magic select on, or back. */
    magicKey(event) {
        const key = (window.PlexoraSegment && window.PlexoraSegment.KEY) || "e";
        if (String(event.key || "").toLowerCase() !== key || event.repeat
            || event.ctrlKey || event.metaKey || event.altKey) return;
        if (QcFreehand.typing() || document.querySelector("dialog[open]")
            || window.PlexoraConfirm?.modalOpen?.()) return;
        const loader = window.PlexoraToolLoader;
        if (loader && typeof loader.activeTool === "function" && loader.activeTool() !== "qc") return;
        event.preventDefault();
        this.toggleMagic();
    }

    toggleMagic(anchor = null) {
        if (this.magic.active) {
            this.setDrawMode("draw");
            return;
        }
        if (this.drawTarget) {
            this.setDrawMode("magic");
            return;
        }
        if (this.lastTarget) {
            this.startDrawing(this.lastTarget, { mode: "magic" });
            return;
        }
        const at = anchor || this.el("qc_magic") || this.el("qc_draw");
        if (at) this.openDrawMenu(at, { mode: "magic" });
    }

    /** The selected QC region as magic select's planner wants it. */
    magicSelected() {
        const region = (this.regionData.regions || []).find((r) => r.roi_id === this.selectedRegion);
        if (!region || !region.bbox || !this.regionVisible(region)) return null;
        const [x0, y0, x1, y1] = region.bbox;
        return { id: region.roi_id, locked: Boolean(region.locked), geometry: region.geometry,
                 bbox: { x: x0, y: y0, width: x1 - x0, height: y1 - y0 } };
    }

    /** Magic select's outline: a new region in the category in hand, or the
     *  selected one reshaped. Resolves to the region's ROI id, or null. */
    async saveGeometry({ roiId, geometry }) {
        const target = this.drawTarget;
        if (roiId) {
            const done = await this.act("Refining the region",
                () => this.api.reshapeRegion(roiId, geometry, "sam"));
            if (!done) return null;
            await this.reload();
            this.selectedRegion = roiId;
            this.overlay.selectedId = roiId;
            this.redraw();
            return roiId;
        }
        if (!target) return null;
        const done = await this.act("Saving the region", () => this.api.drawRegion(
            { ...target.target, geometry, method: "sam", views: this.currentView() }));
        if (!done) return null;
        this._roiWatch?.known.add(done.roi?.id);
        this.message(`Added "${(done.roi && done.roi.name) || target.label}"`);
        await this.reload();
        const drawn = (this.regionData.regions || []).find((r) => r.roi_id === done.roi?.id);
        if (drawn && this.show("g:regions", `c:${QcSidebarController.groupOf(drawn)}`,
                               `r:${drawn.roi_id}`)) this.redraw();
        this.selectedRegion = done.roi?.id || null;
        this.overlay.selectedId = this.selectedRegion;
        this.redraw();
        return done.roi?.id || null;
    }

    /** What is on screen, as a region drawn now should remember it: the
     *  whole view (viewport, zoom, HD mode, channels) when the server takes
     *  one, else the channels alone. */
    currentView() {
        const format = Number((this.vocabulary || {}).views_format || 0);
        if (format >= 1 && window.PlexoraViewSnapshot) {
            const snapshot = window.PlexoraViewSnapshot.capture({ sample: this.ctx.datasource });
            const stored = window.PlexoraViewSnapshot.forStorage(snapshot);
            if (stored && (stored.channels.length || stored.viewport)) return stored;
        }
        return QcSidebarController.currentView();
    }

    /** Put back the view a region was drawn under: HD mode, then where the
     *  viewer was (animated, so the move says where the region is). */
    restoreView(view) {
        const snapshots = window.PlexoraViewSnapshot;
        if (!snapshots || !view || !view.viewport) return false;
        snapshots.restoreHdMode(view);
        return snapshots.restoreViewport(view, { immediately: false });
    }

    /** A finished stroke: an ROI in the category in hand, taken into QC. */
    async saveStroke(points) {
        const target = this.drawTarget;
        if (!target) return;
        const done = await this.act("Saving the region", () => this.api.drawRegion(
            { ...target.target, points, method: "freehand", views: this.currentView() }));
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
        this.el("qc_magic")?.setAttribute("aria-pressed", this.magic.active ? "true" : "false");
        // While a drag draws (the pen, or magic select's Box and Scribble),
        // say once, quietly, how to move the image.
        const hints = window.PlexoraCanvasHint;
        if (hints) {
            if (this.drawTarget && (this.magic.active || this.freehand.drawing)) {
                hints.show(this, { key: "Space", text: "Hold to pan" });
            } else {
                hints.hide(this);
            }
        }
        const bar = this.el("qc_drawbar");
        if (!bar) return;
        const target = this.drawTarget;
        bar.hidden = !target;
        if (!target) return;
        bar.style.setProperty("--qc-row-color", target.color);
        const label = this.el("qc_drawbar_label");
        if (label) label.textContent = target.words;
        const magic = this.magic.active;
        const drawing = this.freehand.drawing && !magic;
        this.el("qc_mode_magic")?.setAttribute("aria-checked", magic ? "true" : "false");
        this.el("qc_mode_draw")?.setAttribute("aria-checked", drawing ? "true" : "false");
        this.el("qc_mode_pan")?.setAttribute("aria-checked", drawing || magic ? "false" : "true");
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
                watch.pending.set(feature.id, this.currentView());
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
        // A channel list, or a whole view (QcSidebarController#currentView).
        for (const [id, view] of watch.pending) {
            if (Array.isArray(view) ? view.length : (view && (view.viewport
                || (view.channels || []).length))) views[id] = view;
        }
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

    /** Which of the five tools are open: {reg, blur, art, seg, roi}. All
     *  folded on every load, and not remembered. */
    loadFolds() {
        return { reg: false, blur: false, art: false, seg: false, roi: false };
    }

    /** The nuclear channel the viewer shows (a slot the Artifact Detector
     *  leaves alone). */
    nuclearChannel() {
        const checks = (this.state && this.state.checks) || {};
        return (checks.artifacts && checks.artifacts.nuclear)
            || (this.regionData.display || {}).nuclear || null;
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

    /** What a region is grouped under: one of the five categories, "review",
     *  or the custom category the user named. */
    static groupOf(region) {
        return region.category || region.class;
    }

    regionVisible(region) {
        return !this.roiMuted
            && !this.hidden.has("g:regions")
            && !this.hidden.has(`c:${QcSidebarController.groupOf(region)}`)
            && !this.hidden.has(`r:${region.roi_id}`);
    }

    /** The root a cell group sits under: every category's cells are listed
     *  under Regions. */
    static rootOf(group) {
        return "g:regions";
    }

    /** The cell groups the panel lists and colours: whole-cell reasons only.
     *  One marker's flags (the cell kept, that value not to be read) stay in
     *  the exports and on the hover card. */
    static panelGroups(groups) {
        return (groups || []).filter((g) => g && g.level !== "marker");
    }

    /** The QC reason groups and Segmentation QC's two, in one cell table. */
    setCellGroups() {
        this._cellIndex = null;
        this.cellLayer.setGroups([
            ...QcSidebarController.panelGroups(this.cellData && this.cellData.groups),
            ...this.segmentation.groups()]);
    }

    cellGroupVisible(group) {
        // Segmentation QC's Under / Over have their own toggles in their row.
        if (group && group.level === "segqc") return this.segmentation.groupVisible(group.key);
        return !this.roiMuted
            && !this.hidden.has(QcSidebarController.rootOf(group))
            && !(group.category && this.hidden.has(`q:${group.category}`))
            && !this.hidden.has(`k:${group.key}`);
    }

    redraw() {
        this.overlay.selectedId = this.selectedRegion;
        this.overlay.schedule();
        this.cellLayer.recolor();
        this.renderTree();
        // An eye clicked under a still pointer: a hidden region stops speaking.
        this.hover.revalidate();
    }

    // -- the hover card ---------------------------------------------------------------

    /** No card while the pointer is busy: a stroke in hand or being drawn,
     *  Space held to pan, the ROI tool drawing these shapes, or a QC act
     *  under way. */
    hoverSuppressed() {
        const pen = this.freehand;
        return Boolean(pen.active || pen.drawing || pen.spaceHeld || pen.stroke || this.busy
            || this.magic.active || this.magic.busy
            || window.PlexoraToolLoader?.isToolVisible?.("roi"));
    }

    /** Whether QC's own cell layer is drawn: its eye on, a mode chosen, and
     *  neither ROI QC's mute nor core's overlay key hiding it. */
    cellLayerShown() {
        const layer = this.ctx.viewer?.getCellLayer?.(QcSidebarController.PLUGIN);
        return Boolean(layer && layer.visible !== false && layer.mode && layer.mode !== "none"
            && !this.roiMuted && !this.ctx.viewer?.overlayMuted
            && ((this.cellData && (this.cellData.groups || []).length)
                || this.segmentation.groups().length));
    }

    /** Whether a cell record's reason (`level` "cell") or marker flag
     *  ("marker") belongs to a group the panel is showing. */
    findingVisible(level, entry) {
        if (level === "marker") {
            const raw = entry.status === "unreliable" ? "exclude"
                : entry.status === "flagged" ? "warn" : entry.status;
            return this.cellGroupVisible({ level: "marker", category: entry.category,
                                           key: `m:${entry.marker}|${entry.reason}|${raw}` });
        }
        return this.cellGroupVisible({ level: "cell", category: entry.category,
                                       key: `${entry.reason}|${entry.status}` });
    }

    /** The panel's words and colours, for a card built outside it. */
    hoverHelpers() {
        return {
            capital: (text) => QcSidebarController.capital(text),
            originOf: (region) => QcSidebarController.originOf(region),
            tracedWords: (region) => QcSidebarController.tracedWords(region),
            regionName: (region) => QcSidebarController.regionName(region, this),
            classWords: (id) => this.classWords(id),
            categoryWords: (key) => this.categoryWords(key),
            categoryColor: (key) => this.categoryColor(key),
            defaultClass: (key) => (this.categoryEntry(key) || {}).default_class || null,
        };
    }

    /** The cell layer's groups a cell is in: what a card says of a cell when
     *  the server has no current calls to read. */
    cellGroupsFor(id) {
        if (!this._cellIndex) {
            const index = new Map();
            const groups = [
                ...QcSidebarController.panelGroups(this.cellData && this.cellData.groups),
                ...this.segmentation.groups()];
            for (const group of groups) {
                for (const cell of group.ids || []) {
                    const key = Number(cell);
                    if (!index.has(key)) index.set(key, []);
                    index.get(key).push(group);
                }
            }
            this._cellIndex = index;
        }
        return (this._cellIndex.get(Number(id)) || []).filter((g) => this.cellGroupVisible(g));
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

    /** What a row's colour belongs to: a category for a category row and for
     *  the cells inside its regions, a reason for any other cell reason. */
    static colorTarget(spec) {
        if (spec.kind === "class" && spec.ref && spec.ref[0]) {
            return { category: QcSidebarController.groupOf(spec.ref[0]) };
        }
        if (spec.kind === "reason" && spec.ref) {
            const reason = spec.ref.reason || "";
            return reason.startsWith("region:") && spec.ref.category
                ? { category: spec.ref.category } : { reason };
        }
        return null;
    }

    /** Draw `hex` on everything the target colours, now. */
    paintColor(target, hex) {
        if (target.category) {
            for (const region of this.regionData.regions || []) {
                if (QcSidebarController.groupOf(region) === target.category) region.color = hex;
            }
        }
        for (const group of (this.cellData && this.cellData.groups) || []) {
            const inRegion = String(group.reason || "").startsWith("region:");
            if (target.category ? inRegion && group.category === target.category
                : group.reason === target.reason) group.color = hex;
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
        const key = target.category ? `category:${target.category}` : `reason:${target.reason}`;
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

    /** A click on a finding: go there, and show what it was judged on -- the
     *  channels behind the call (a registration region its two cycles, a
     *  hand-drawn one what was on screen when it was drawn), with the cells'
     *  outlines for a segmentation finding. Without that signal on screen a
     *  person cannot check the call. */
    focusRegion(region) {
        this.selectedRegion = region.roi_id;
        this.show("g:regions", `c:${QcSidebarController.groupOf(region)}`, `r:${region.roi_id}`);
        this.ensureDrawn();
        // A region that remembers the whole view it was drawn under gets that
        // view back -- where the viewer was, how far in, HD mode; the others
        // are framed by their box.
        if (!(region.view && region.view.viewport && this.restoreView(region.view))) {
            this.fit(region.bbox);
        }
        const drawnWith = (region.view && region.view.channels && region.view.channels.length)
            ? region.view.channels.filter((c) => c.visible !== false) : region.view_channels;
        this.showChannels(region.evidence_channels || region.channels || [],
                          drawnWith, { asked: true });
        if (region.category === "segmentation") this.ctx.layers?.showCells?.();
        this.redraw();
    }

    /** A click on a flagged cell on the tissue: its reason's row lit in the
     *  panel, the cell framed with its neighbourhood, and the channels its
     *  call was made on put up -- a reason's own, else the region's behind
     *  it, else the marker flagged. A cell with only a marker flagged has no
     *  row in the panel: its channels go up and no row is lit. */
    focusCell(record, shape) {
        const reason = (record.reasons || [])[0] || null;
        const marker = (record.markers || [])[0] || null;
        const groups = this.cellGroupsFor(record.cell_id);
        let channels = reason ? [...(reason.channels || [])] : [];
        let view = null;
        if (reason && !channels.length) {
            const via = (reason.via_regions || [])[0];
            const region = via && (this.regionData.regions || []).find((r) => r.roi_id === via.roi_id);
            if (region) {
                channels = region.evidence_channels || region.channels || [];
                view = region.view_channels;
            }
        }
        if (!channels.length && marker) channels = [marker.marker];
        if (!channels.length && groups.length) channels = groups[0].evidence_channels || [];
        const key = reason ? `${reason.reason}|${reason.status}`
            : groups.length ? groups[0].key : null;
        const category = reason ? reason.category : groups.length ? groups[0].category : null;
        if (key) {
            this.show("g:regions", ...(category ? [`q:${category}`] : []), `k:${key}`);
        }
        this.ctx.layers?.showCells?.();
        this.ensureDrawn();
        if (shape) this.fit([shape.x, shape.y, shape.x + shape.w, shape.y + shape.h], 300);
        if (channels.length) this.showChannels(channels, view, { asked: true });
        this.redraw();
    }

    focusCells(group) {
        this.show("g:regions", ...(group.category ? [`q:${group.category}`] : []),
                  `k:${group.key}`);
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
        else if (spec.kind === "class" && ref) {
            this.show("g:regions", spec.key);
            this.ensureDrawn();
            this.fit(QcSidebarController.union(ref.map((r) => r.bbox)));
            this.redraw();
        } else if (spec.kind === "cellcat" && ref) {
            this.show("g:regions", spec.key);
            this.ctx.layers?.showCells?.();
            this.ensureDrawn();
            this.fit(QcSidebarController.union(ref.map((g) => g.bbox)), 300);
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

    /** Every line of a region's provenance, as [label, value] -- what the
     *  Details popup lists and "Copy details" copies. */
    regionFacts(region) {
        const facts = [];
        const add = (label, value) => {
            if (value !== undefined && value !== null && value !== "") facts.push([label, String(value)]);
        };
        const number = (v) => (typeof v === "number" ? (Math.round(v * 1000) / 1000).toString() : v);
        add("Category", QcSidebarController.capital(region.category_words
            || this.categoryWords(region.category)));
        add("Subtype", QcSidebarController.capital(region.words || this.classWords(region.class)));
        const also = QcSidebarController.alsoWords(region);
        if (also) add("Also here", also);
        add("Action", region.action);
        const tool = region.tool || {};
        const origin = QcSidebarController.originOf(region);
        add("Found by", tool.name && tool.name !== "user"
            ? `${tool.name}${tool.version ? ` v${tool.version}` : ""}` : origin.words);
        if (region.score !== undefined && region.score !== null) {
            add("Score", `${number(region.score)}${region.score_kind
                ? ` (${String(region.score_kind).replace(/_/g, " ")})` : ""}`);
        }
        if (region.threshold !== undefined && region.threshold !== null) {
            const steps = region.offset_steps ? `, ${region.offset_steps > 0 ? "+" : ""}`
                + `${region.offset_steps} step${Math.abs(region.offset_steps) === 1 ? "" : "s"}` : "";
            add("Threshold", `${number(region.threshold)} (${String(region.threshold_source
                || "auto").replace(/_/g, " ")}${steps})`);
        }
        add("Channels", (region.channels || []).join(", ") || "all");
        if ((region.cycles || []).length) add("Cycle", region.cycles.join(", "));
        const ai = region.ai || {};
        if (ai.verdict) {
            add("Agent", `${ai.verdict}${ai.confidence ? ` (${String(ai.confidence).replace(/_/g, " ")})` : ""}`
                + `${ai.source ? ` · ${String(ai.source).replace(/_/g, " ")}` : ""}`);
        }
        add("Severity", region.severity);
        const traced = QcSidebarController.tracedWords(region);
        const refinement = region.refinement || {};
        if (traced) add("Outline", `${traced} (${refinement.method})`);
        else if (refinement.status === "map") add("Outline", `score map (${refinement.method})`);
        else if (refinement.status) add("Outline", `as drawn (${refinement.reason || refinement.status})`);
        const share = typeof region.refined_fraction === "number"
            ? region.refined_fraction : region.tissue_fraction;
        if (typeof share === "number") add("Tissue", `${(share * 100).toFixed(1)}%`);
        if (typeof region.n_cells === "number") add("Cells derived", region.n_cells.toLocaleString());
        if ((region.view_channels || []).length) {
            add("Drawn with", region.view_channels.map((v) => v.name).join(", "));
        }
        add("Made by", region.created_by);
        add("ROI", region.roi_id);
        return facts;
    }

    regionDetails(region) {
        const facts = this.regionFacts(region);
        if (region.bbox) facts.push(["bbox", region.bbox.map((v) => Math.round(v)).join(", ")]);
        return facts.map(([label, value]) => `${label.toLowerCase()}: ${value}`).join("\n");
    }

    /** A region's provenance, read-only, in a small popup at `anchor`. */
    showDetails(region, anchor) {
        const box = document.createElement("div");
        box.className = "qc-details";
        box.setAttribute("role", "dialog");
        box.setAttribute("aria-label", "Region details");
        box.addEventListener("click", (event) => event.stopPropagation());
        const title = document.createElement("div");
        title.className = "qc-details-title";
        title.textContent = QcSidebarController.regionName(region, this);
        const list = document.createElement("dl");
        list.className = "qc-details-list";
        for (const [label, value] of this.regionFacts(region)) {
            const term = document.createElement("dt");
            term.textContent = label;
            const detail = document.createElement("dd");
            detail.textContent = value;
            list.append(term, detail);
        }
        box.append(title, list);
        if (anchor) QcTree.popup(anchor, box);
    }

    menuFor(spec, anchor = null) {
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
                { label: region.view && region.view.viewport ? "Show it as it was drawn"
                    : "Zoom to region",
                  hint: region.view && region.view.viewport
                      ? "The view, zoom, HD mode and channels on screen when it was drawn" : undefined,
                  onSelect: () => this.focusRegion(region) },
                { label: "Details", hint: "Why this region: category, subtype, what found it, "
                    + "the score and threshold, the agent's judgment",
                  onSelect: () => this.showDetails(region, anchor) },
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
            const members = spec.ref || [];
            const first = members[0] || {};
            const target = first.custom ? { label: first.category_words }
                : { category: spec.key.slice(2) };
            return [
                { label: "Zoom to all", onSelect: () => this.activate(spec) },
                hideItem(spec.key),
                { label: `Mark another by hand`, className: "is-sectioned",
                  hint: "Draw another region in this category, with Freehand",
                  onSelect: () => this.startDrawing(target, { mode: "draw" }) },
                { label: "Mark another with magic select",
                  hint: "Click an artifact and its outline becomes a region in this category (E)",
                  onSelect: () => this.startDrawing(target, { mode: "magic" }) },
                ...QcSidebarController.resetItem(spec, this),
                { label: `Delete all ${members.length} region${members.length === 1 ? "" : "s"}…`,
                  className: "is-sectioned is-destructive",
                  disabled: !members.some((r) => !r.locked && !r.approved),
                  onSelect: () => this.deleteClassRegions(members) },
            ];
        }
        if (spec.kind === "reason") {
            const group = spec.ref;
            const solo = () => {
                for (const other of QcSidebarController.panelGroups(this.cellData?.groups)) {
                    const key = `k:${other.key}`;
                    if (other.key === group.key) this.hidden.delete(key);
                    else this.hidden.add(key);
                }
                this.hidden.delete(QcSidebarController.rootOf(group));
                if (group.category) this.hidden.delete(`q:${group.category}`);
                this.saveHidden();
                this.focusCells(group);
            };
            const words = group.status === "fail" ? "excluded"
                : group.status === "note" ? "noted" : "flagged";
            // Cells inside a region drawn by hand follow that region: only the
            // region can be deleted, not the finding itself.
            const regionBacked = String(group.reason || "").startsWith("region:");
            const backingRegions = regionBacked
                ? (this.regionData.regions || []).filter((r) => r.class === group.reason.slice(7)) : [];
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
                regionBacked
                    ? { label: "Delete its regions…", className: "is-sectioned is-destructive",
                        disabled: !backingRegions.length,
                        hint: "These cells are flagged only because a region covers them: "
                            + "delete the regions and they stop being flagged",
                        onSelect: () => this.deleteReasonRegions(backingRegions) }
                    : { label: "Remove this finding…", className: "is-sectioned is-destructive",
                        hint: "QC got this wrong: stop flagging these cells for it (you can "
                            + "restore it)",
                        onSelect: () => this.dismissReason(group) },
            ];
        }
        if (spec.kind === "cellcat") {
            const keys = (spec.ref || []).map((g) => `k:${g.key}`);
            return [
                { label: "Frame these cells", onSelect: () => this.activate(spec) },
                hideItem(spec.key),
                { label: "Show all", onSelect: () => {
                    this.show("g:regions", spec.key, ...keys);
                    this.redraw();
                } },
            ];
        }
        if (spec.kind === "dismissed") {
            const entry = spec.ref;
            return [{ label: "Restore", hint: "Flag these cells again",
                      onSelect: () => this.dismiss({ finding: entry.finding, reason: entry.reason,
                                                      marker: entry.marker, channel: entry.channel,
                                                      restore: true }, entry.label) }];
        }
        if (spec.key === "g:regions") {
            const cellGroups = QcSidebarController.panelGroups(this.cellData?.groups);
            const keys = [...new Set([
                ...(this.regionData.regions || []).flatMap(
                    (r) => [`c:${QcSidebarController.groupOf(r)}`, `r:${r.roi_id}`]),
                ...cellGroups.flatMap((g) => [`q:${g.category || "tissue_acquisition"}`,
                                               `k:${g.key}`])])];
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
                { label: "Trace all outlines", className: "is-sectioned",
                  disabled: !(this.regionData.regions || []).length,
                  hint: "Redraw every region round its artifact's own pixels",
                  onSelect: () => this.traceAll() },
                { label: "Download regions (GeoJSON)", className: "is-sectioned",
                  disabled: !(this.regionData.regions || []).length,
                  onSelect: () => this.download("regions.geojson") },
                { label: "Download cells (CSV)",
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
              hint: "The QC regions with their category, subtype, action and threshold",
              onSelect: () => this.download("regions.geojson") },
            { label: "Provenance (JSON)", disabled: !hasResult,
              hint: "Why every region and cell was flagged: tool, score, threshold and its "
                  + "source, the agent's judgment",
              onSelect: () => this.download("provenance.json") },
            { label: "Findings (CSV)", disabled: !hasResult,
              hint: "The same, one row per region, cell reason and marker reason",
              onSelect: () => this.download("findings.csv") },
            { label: "Report", disabled: !(s.provenance && s.provenance.result_id
                                           && s.provenance.session_id),
              hint: "The session's report, in a new tab",
              onSelect: () => this.download("report.html") },
        ], { heading: "Download" });
    }

    /**
     * The category picker: "Custom" first -- a field, muted, that names a new
     * QC category (or, typed, one of the subtypes: "tissue fold" draws a fold
     * in Tissue / acquisition) -- then the five categories, then the ones
     * named on this project. The heading's ? opens a short help: the five,
     * each unfolding to what it groups. Choosing one starts drawing it
     * (startDrawing).
     */
    openDrawMenu(anchor, options = {}) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const categories = this.categories();
        if (!categories.length) {
            this.message("The QC categories have not loaded yet");
            return;
        }
        const classes = ((this.vocabulary || {}).classes || [])
            .filter((item) => item.id !== "uncertain_manual_review");
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
        row.title = "Name your own QC category (or a subtype, like tissue fold), then press "
            + "Enter to draw it";
        row.append(icon, input);
        input.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            const words = input.value.replace(/\s+/g, " ").trim();
            if (!words) {
                row.classList.add("is-refused");
                return;
            }
            QcTree.closePopup();
            this.startDrawing(QcSidebarController.pickerTarget(words, categories, classes),
                              { mode: options.mode });
        });
        input.addEventListener("input", () => row.classList.remove("is-refused"));

        const items = [
            ...categories.map((item) => ({
                label: item.words, color: item.color,
                hint: item.help ? `${item.words}: ${item.help}` : item.words,
                onSelect: () => this.startDrawing({ category: item.id }, { mode: options.mode }),
            })),
            ...custom.map((item, i) => ({
                label: item.words, color: item.color,
                className: i === 0 ? "is-sectioned" : "",
                hint: `Draw a "${item.words}" region`,
                onSelect: () => this.startDrawing({ label: item.words }, { mode: options.mode }),
            })),
        ];
        const help = QcSidebarController.pickerHelp(categories);
        QcTree.menu(anchor, items, {
            heading: options.mode === "magic" ? "Mark a region with magic select"
                : "Mark a region by hand", before: row, help,
            headingAction: {
                icon: "circle-question", title: "What the categories mean",
                onClick: (button) => {
                    help.hidden = !help.hidden;
                    button.setAttribute("aria-expanded", help.hidden ? "false" : "true");
                },
            },
            className: "qc-picker qc-picker--categories", align: options.align });
    }

    /** What a name typed into Custom draws: a subtype's words (its class, in
     *  its category), a category's words (that category), else a custom one. */
    static pickerTarget(words, categories, classes) {
        const folded = words.toLowerCase();
        const slug = folded.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
        const klass = classes.find((item) => item.words.toLowerCase() === folded
            || item.id === slug);
        if (klass) return { class: klass.id };
        const category = categories.find((item) => item.words.toLowerCase() === folded
            || item.id === slug);
        if (category) return { category: category.id };
        return { label: words };
    }

    /** The picker's help, folded away until the ? opens it: the five, each a
     *  row that unfolds to the one line of what it groups. */
    static pickerHelp(categories) {
        const help = document.createElement("div");
        help.className = "qc-picker-help";
        help.hidden = true;
        for (const item of categories) {
            const entry = document.createElement("div");
            entry.className = "qc-picker-help-entry";
            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = "qc-picker-help-row";
            toggle.setAttribute("aria-expanded", "false");
            const dot = document.createElement("span");
            dot.className = "qc-menu-dot";
            dot.dataset.shape = "fill";
            dot.style.setProperty("--qc-row-color", item.color);
            const name = document.createElement("span");
            name.className = "qc-picker-help-name";
            name.textContent = item.words;
            const chevron = document.createElement("span");
            chevron.className = "fas fa-chevron-right qc-picker-help-chevron";
            chevron.setAttribute("aria-hidden", "true");
            toggle.append(dot, name, chevron);
            const groups = document.createElement("div");
            groups.className = "qc-picker-help-groups";
            groups.hidden = true;
            groups.textContent = item.help || (item.groups || []).join(", ");
            toggle.addEventListener("click", (event) => {
                event.stopPropagation();
                groups.hidden = !groups.hidden;
                toggle.setAttribute("aria-expanded", groups.hidden ? "false" : "true");
            });
            entry.append(toggle, groups);
            help.appendChild(entry);
        }
        return help;
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

    /** The five categories, in order (the vocabulary's). */
    categories() {
        return (this.vocabulary || {}).categories || [];
    }

    /** One of the five, "review", or a custom category, by key. */
    categoryEntry(key) {
        const vocabulary = this.vocabulary || {};
        if (vocabulary.review && vocabulary.review.id === key) return vocabulary.review;
        if (vocabulary.background && vocabulary.background.id === key) return vocabulary.background;
        return this.categories().find((item) => item.id === key)
            || (vocabulary.custom || []).find((item) => item.id === key) || null;
    }

    categoryColor(key) {
        const found = this.categoryEntry(key);
        return (found && found.color) || "#9ca3af";
    }

    categoryWords(key) {
        const found = this.categoryEntry(key);
        return (found && found.words) || String(key || "").replace(/_/g, " ");
    }

    /** Where a category is listed: the five, then review, then custom ones,
     *  then the background (outside the tissue: annotated, never a finding). */
    categoryRank(key) {
        const five = this.categories().map((item) => item.id);
        const at = five.indexOf(key);
        if (at >= 0) return at;
        if (key === ((this.vocabulary || {}).review || {}).id) return five.length;
        const custom = ((this.vocabulary || {}).custom || []).map((item) => item.id);
        if (key === "background" || key === ((this.vocabulary || {}).background || {}).id) {
            return five.length + 2 + custom.length;
        }
        const mine = custom.indexOf(key);
        return five.length + 1 + (mine >= 0 ? mine : custom.length);
    }

    classWords(id) {
        const found = ((this.vocabulary || {}).classes || []).find((item) => item.id === id);
        return found ? found.words : String(id || "").replace(/_/g, " ");
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
            const job = this.session.job;
            const step = job && job.message ? ` · ${job.message}`
                + (job.total > 1 ? ` (step ${Math.min(job.done + 1, job.total)} of ${job.total})` : "")
                : "";
            this.status(live, `${live === "paused" ? "Session paused" : "Session running"}`
                + (step || phase));
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
                : "");
    }

    /** Every row, in order, for QcTree. */
    specs() {
        const out = [];
        const regions = this.regionData.regions || [];
        const hid = (key) => this.hidden.has(key);

        // The cells flagged, one row per category, under Regions beside the
        // regions themselves. Only whole-cell reasons: a marker's flags stay
        // in the exports and on the hover card.
        const cells = this.cellData;
        const groups = QcSidebarController.panelGroups(cells && cells.groups);
        const byCategory = new Map();
        for (const group of groups) {
            const key = group.category || "tissue_acquisition";
            if (!byCategory.has(key)) byCategory.set(key, []);
            byCategory.get(key).push(group);
        }
        const cellCategories = [...byCategory.keys()].sort((a, b) => this.categoryRank(a)
            - this.categoryRank(b));
        const dismissedCells = ((cells && cells.dismissed) || [])
            .filter((entry) => entry.finding !== "channel");

        // Regions, by category (an artifact class, or one the user named) --
        // only once there is one: an empty Regions is a container for nothing.
        if (regions.length || cellCategories.length || dismissedCells.length) {
            out.push({ key: "g:regions", level: 0, kind: "group", icon: "draw-polygon",
                       label: "Regions", count: regions.length || null, expandable: true,
                       activatable: false, eye: true, menu: true,
                       title: cells && cells.available
                           ? `${regions.length} region${regions.length === 1 ? "" : "s"}; `
                               + `${(cells.n_fail || 0).toLocaleString()} cells excluded, `
                               + `${(cells.n_warn || 0).toLocaleString()} flagged, of `
                               + `${(cells.n || 0).toLocaleString()}`
                           : "",
                       hidden: hid("g:regions"), ownHidden: hid("g:regions") });
        }
        const byClass = new Map();
        for (const region of regions) {
            const group = QcSidebarController.groupOf(region);
            if (!byClass.has(group)) byClass.set(group, []);
            byClass.get(group).push(region);
        }
        const classes = [...byClass.keys()].sort((a, b) => this.categoryRank(a)
            - this.categoryRank(b));
        for (const klass of classes) {
            const members = byClass.get(klass);
            const color = members[0].color || this.categoryColor(klass);
            const classKey = `c:${klass}`;
            const nExclude = members.filter((r) => r.action !== "warn").length;
            const words = QcSidebarController.capital(members[0].category_words
                || this.categoryWords(klass));
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
                const also = QcSidebarController.alsoWords(region);
                // The subtype, when it says more than the category it sits in.
                const own = this.categoryEntry(klass);
                const subtype = region.words && !(own && own.default_class === region.class)
                    && !region.custom ? region.words : "";
                out.push({
                    key, level: 2, kind: "region", ref: region, color,
                    shape: region.action === "warn" ? "ring" : "fill",
                    // Its own name, without the "QC: " every QC ROI carries in
                    // the ROI panel -- everything here is QC.
                    label: String(region.name || "").replace(/^QC:\s*/, "")
                        || `${index + 1}. ${label}`,
                    // The subtype and where it came from: muted, beside the name.
                    note: [subtype, also ? `also ${also}` : "", origin.words]
                        .filter(Boolean).join(" · "),
                    title: `${region.action === "warn" ? "Flags" : "Excludes"} cells · `
                        + `${label}${tissue}${traced ? ` · ${traced}` : ""}`
                        + `${origin.manual ? " · drawn by hand" : ` · ${origin.words}`}`
                        + `${drawn.length ? ` · drawn with ${drawn.join(", ")}` : ""}`
                        + `${also ? ` · also here: ${also}` : ""}`
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

        const reasonRow = (group) => {
            const key = `k:${group.key}`;
            const strong = group.status === "fail";
            // A note keeps the cell and records why (outside the tissue, a
            // merge the mask may hold): a fainter ring than a flag.
            const noted = group.status === "note";
            const words = strong ? "excluded" : noted ? "noted (kept)" : "flagged";
            // Cells inside a region drawn by hand are there because of that
            // outline, not because QC found anything in them: said quietly.
            const from = (group.derived_from || []).map((r) => r.name).filter(Boolean);
            const note = from.length ? `Derived from: ${from.slice(0, 2).join(", ")}`
                + (from.length > 2 ? ` +${from.length - 2}` : "") : "";
            return {
                key, level: 2, kind: "reason", ref: group, color: group.color,
                shape: strong ? "fill" : noted ? "faint" : "ring", label: group.label, note,
                title: `${group.label}: ${(group.count || 0).toLocaleString()} cells ${words}`
                    + `${group.definition ? ` — ${group.definition}` : ""}`
                    + `${group.truncated ? " (list truncated)" : ""}`,
                tag: strong ? "" : noted ? "noted" : "warn", tagTone: noted ? "plain" : "warn",
                count: group.count, eye: true, menu: true, colorable: true,
                colorTitle: String(group.reason || "").startsWith("region:")
                    ? "Colour of these regions and their cells" : "Colour of these cells",
                hidden: !this.cellGroupVisible(group), ownHidden: hid(key),
            };
        };
        // Whole-cell reasons under their category (the five, then review,
        // then the background), folded until opened: the eye on a category
        // hides all of its reasons.
        for (const category of cellCategories) {
            const members = byCategory.get(category);
            const key = `q:${category}`;
            const failing = members.filter((g) => g.status === "fail");
            const warning = members.filter((g) => g.status === "warn");
            const count = members.reduce((n, g) => n + (g.count || 0), 0);
            const words = QcSidebarController.capital(members[0].category_words
                || this.categoryWords(category));
            const verb = failing.length ? "excluded" : warning.length ? "flagged" : "noted";
            out.push({ key, level: 1, kind: "cellcat", ref: members,
                       color: this.categoryEntry(category) ? this.categoryColor(category)
                           : members[0].color || this.categoryColor(category),
                       shape: failing.length ? "fill" : warning.length ? "ring" : "faint",
                       label: `${words}: ${count.toLocaleString()} cell${count === 1 ? "" : "s"}`,
                       expandable: true, eye: true, menu: true,
                       title: `${words}: ${count.toLocaleString()} cell flags (${verb}) over `
                           + `${members.length} reason${members.length === 1 ? "" : "s"}`,
                       hidden: hid("g:regions") || hid(key), ownHidden: hid(key) });
            for (const group of members) out.push(reasonRow(group));
        }

        // Findings judged wrong and set aside: kept out of the counts above,
        // listed quietly underneath so any one can be brought back. A
        // channel's own verdict is not listed (the report holds the channels).
        if (dismissedCells.length) {
            out.push({ key: "h:dismissed", level: 1, kind: "status", label: "Set aside by you",
                       shape: "none", count: dismissedCells.length, expandable: true,
                       activatable: false, muted: true });
            for (const entry of dismissedCells) {
                const key = `d:${entry.finding}:${entry.reason || ""}:${entry.marker || ""}`;
                out.push({ key, level: 2, kind: "dismissed", ref: entry, label: entry.label,
                           title: `${entry.label}: set aside, no longer flagged`,
                           muted: true, activatable: false, shape: "none", eye: false, menu: true });
            }
        }
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
                "Click a region to fit the view to it -- a region you drew comes back "
                + "the way it was on screen (view, zoom, HD mode, channels); click a cell "
                + "reason to frame the cells it flagged.",
                "Magic select (E, or the wand on the draw bar): click an artifact and its "
                + "outline becomes a region. The bar that floats on the image switches "
                + "between Add (click to include), Remove (click to take away), Box "
                + "(drag round a large object) and Scribble (draw a line over a long or "
                + "patchy one; Shift-drag over what to leave out); all four refine the "
                + "same outline, Esc "
                + "finishes it and × puts magic select away. It sets itself up the first "
                + "time you use it (a short one-time download).",
                "Click a category's or a reason's dot to change its colour.",
                "A filled dot excludes; a ring only flags. The eyes hide what is drawn, "
                + "never what is counted.",
                "Under Regions, each category also lists the cells it flagged, by "
                + "reason; a faint ring is only noted (outside the tissue, say), the "
                + "cell kept. A marker unreliable in some cells, and each channel's "
                + "verdict, are in the report and the downloads.",
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
