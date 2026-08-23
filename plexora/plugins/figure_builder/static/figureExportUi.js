/**
 * FigureExportUi - the export dialog, and following the job it starts.
 *
 * Two things this does that a plain "download" button does not, and both are
 * the point:
 *
 * **It asks before it renders.** The preflight comes back with the effective
 * resolution of every panel and with anything the export cannot reproduce, and
 * that is shown while the dialog is still open. A warning attached to a file
 * that is already written is a warning nobody acts on.
 *
 * **It shows what it is doing.** An eighteen-panel figure at 600 DPI is
 * minutes. A spinner with no panel count is indistinguishable from a hang, and
 * a user who cannot tell those apart reloads the page.
 *
 * Cancelling is a real answer at any point: an export is a render, not an edit,
 * so stopping one changes nothing about the figure and leaves nothing
 * half-written behind.
 */
class FigureExportUi {

    /** How often the job is asked how it is getting on. Fast enough that the
     *  panel counter moves, slow enough not to be a request per frame. */
    static get POLL_MS() { return 700; }

    constructor(options) {
        this.api = options.api;
        this.figureId = options.figureId;
        this.state = options.state;
        this.jobId = null;
        this.timer = null;
    }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        this.el("fb_export_open")?.addEventListener("click", () => this.open());
        this.el("fb_export_cancel")?.addEventListener("click", () => this.close());
        this.el("fb_export_start")?.addEventListener("click", () => this.start());
        this.el("fb_export_stop")?.addEventListener("click", () => this.stop());
        for (const id of ["fb_export_format", "fb_export_dpi"]) {
            this.el(id)?.addEventListener("change", () => this.preflight());
        }
    }

    open() {
        const dialog = this.el("fb_export_dialog");
        if (!dialog) return;
        const dpi = this.el("fb_export_dpi");
        if (dpi && !dpi.value) dpi.value = String(this.state.document.settings.dpi_default);
        this.setPhase("idle");
        dialog.showModal?.();
        this.preflight();
    }

    close() {
        this.stopPolling();
        this.el("fb_export_dialog")?.close?.();
    }

    options() {
        return {
            format: this.el("fb_export_format")?.value || "pdf",
            dpi: Number(this.el("fb_export_dpi")?.value || 300),
            provenance: this.el("fb_export_provenance")?.checked !== false,
        };
    }

    /**
     * Ask what this export would be like, before rendering any of it.
     *
     * Re-run on every change of format or DPI, because the answer depends on
     * both: the same figure is fine at 150 and enlarged past its source at 600.
     */
    async preflight() {
        const target = this.el("fb_export_warnings");
        if (!target) return;
        target.innerHTML = `<div class="fb-muted">Checking sources…</div>`;

        const result = await this.api.preflightExport(this.figureId, this.options());
        if (!result.ok) {
            target.innerHTML = `<div class="fb-banner-detail">${FigureSchema.escapeHtml(
                result.data.error || "This figure could not be checked.")}</div>`;
            return;
        }
        const warnings = result.data.warnings || [];
        if (!warnings.length) {
            target.innerHTML = `<div class="fb-muted">${
                FigureSchema.countPhrase(result.data.panels, "panel")} ready at ${
                result.data.dpi} DPI.</div>`;
            return;
        }
        target.innerHTML = warnings.map((warning) =>
            `<div class="fb-banner fb-banner-warning">
                <span class="fas fa-triangle-exclamation"></span>
                <div><div class="fb-banner-detail">${FigureSchema.escapeHtml(warning.message)}</div></div>
            </div>`).join("");
    }

    async start() {
        this.setPhase("running", "Starting…");
        const result = await this.api.startExport(this.figureId, this.options());
        if (!result.ok) {
            this.setPhase("failed", result.data.error || "This figure could not be exported.");
            return;
        }
        this.jobId = result.data.job_id;
        this.poll();
    }

    async stop() {
        if (!this.jobId) return;
        await this.api.cancelExport(this.figureId, this.jobId);
        // Not marked cancelled here: the job reports its own state, and
        // claiming it stopped before it has would be a message that is
        // sometimes wrong.
    }

    stopPolling() {
        if (this.timer) window.clearTimeout(this.timer);
        this.timer = null;
    }

    async poll() {
        if (!this.jobId) return;
        const result = await this.api.exportStatus(this.figureId, this.jobId);
        if (result.status === 501) {
            this.setPhase("failed", result.data.detail
                || "This build cannot write that format.");
            return;
        }
        if (!result.ok) {
            this.setPhase("failed", "The export could not be followed.");
            return;
        }
        const job = result.data.job;
        if (job.status === "running") {
            this.setPhase("running", job.progress.message
                + (job.progress.total > 1
                    ? ` (${job.progress.done} of ${job.progress.total})` : ""));
            this.timer = window.setTimeout(() => this.poll(), FigureExportUi.POLL_MS);
            return;
        }
        this.stopPolling();
        if (job.status === "cancelled") {
            this.setPhase("idle", "Cancelled. Nothing was written.");
        } else if (job.status === "done") {
            this.setPhase("done", FigureSchema.countPhrase(job.result.panels, "panel")
                + " rendered.");
            const link = this.el("fb_export_download");
            if (link) {
                link.href = this.api.exportDownloadUrl(this.figureId, this.jobId);
                link.hidden = false;
            }
        } else {
            this.setPhase("failed", job.error || "The export failed.");
        }
    }

    setPhase(phase, message) {
        const status = this.el("fb_export_status");
        if (status) status.textContent = message || "";
        for (const [id, phases] of [
            ["fb_export_start", ["idle", "done", "failed"]],
            ["fb_export_stop", ["running"]],
            ["fb_export_download", ["done"]],
        ]) {
            const element = this.el(id);
            if (element) element.hidden = !phases.includes(phase);
        }
    }
}
