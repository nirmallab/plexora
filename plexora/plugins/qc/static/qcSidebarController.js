/**
 * QcSidebarController - the Quality Control panel.
 *
 * Shows what QC says about this image (the active result: regions by class
 * and action, channel statuses, cells excluded), lets the user change the
 * strictness (every call re-derived on the server, no agent involved), approve
 * a region, prepare a QC category to draw a region in by hand, take in the ROI
 * panel's edits, and download the files. The regions themselves are drawn by
 * the ROI plugin: QC never paints on the canvas.
 */
class QcSidebarController {

    constructor(ctx) {
        this.ctx = ctx;
        this.api = new QcApi(ctx);
        this.state = null;
        this.vocabulary = null;
        this.busy = false;
        this._messageTimer = null;
    }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        this.el("qc_strictness")?.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-preset]");
            if (button) this.setStrictness(button.dataset.preset);
        });
        this.el("qc_refresh")?.addEventListener("click", () => this.refresh());
        this.el("qc_draw")?.addEventListener("click", () => this.addCategory());
        this.el("qc_regions")?.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-approve]");
            if (button) this.approve(button.dataset.approve, button.dataset.action);
        });
        this.api.vocabulary().then((answer) => {
            if (!answer.ok) return;
            this.vocabulary = answer.data;
            this.renderClasses();
        }).catch(() => {});
        this.reload();
    }

    onShow() {
        this.reload();
    }

    onHide() {}

    onVisibilityChange(visible) {
        if (visible) this.reload();
    }

    destroy() {
        window.clearTimeout(this._messageTimer);
    }

    // -- talking to the server ---------------------------------------------------

    async reload() {
        try {
            const answer = await this.api.state();
            if (!answer.ok) {
                this.status((answer.data.error && answer.data.error.message) || "QC could not be read");
                return;
            }
            this.state = answer.data;
            this.render();
        } catch (error) {
            this.status("QC could not be read");
        }
    }

    async act(label, fn) {
        if (this.busy) return;
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

    async refresh() {
        const done = await this.act("Applying your edits", () => this.api.refresh());
        if (!done) return;
        const sync = done.sync || {};
        const changed = ["adopted", "edited", "deleted", "relabelled", "removed"]
            .reduce((n, key) => n + ((sync[key] || []).length), 0);
        this.message(changed ? `Took in ${changed} change${changed === 1 ? "" : "s"}` : "Up to date");
        await this.reload();
    }

    async approve(roiId, action) {
        const done = await this.act("Approving the region", () => this.api.approve(roiId, action));
        if (!done) return;
        this.message("Approved and locked");
        await this.reload();
    }

    async addCategory() {
        const select = this.el("qc_draw_class");
        if (!select || !select.value) return;
        const done = await this.act("Adding the category", () => this.api.addCategory(select.value));
        if (!done) return;
        this.message(`Draw the region in the ROI panel under "${done.label}", then press Apply`);
    }

    // -- drawing the panel ----------------------------------------------------------

    status(text) {
        const node = this.el("qc_status");
        if (node) node.textContent = text;
    }

    message(text) {
        const node = this.el("qc_message");
        if (!node) return;
        node.textContent = text;
        node.hidden = false;
        window.clearTimeout(this._messageTimer);
        this._messageTimer = window.setTimeout(() => { node.hidden = true; }, 5000);
    }

    renderClasses() {
        const select = this.el("qc_draw_class");
        if (!select || !this.vocabulary) return;
        select.textContent = "";
        for (const item of this.vocabulary.classes || []) {
            if (item.id === "uncertain_manual_review") continue;
            const option = document.createElement("option");
            option.value = item.id;
            option.textContent = item.words;
            select.appendChild(option);
        }
    }

    render() {
        const s = this.state || {};
        const summary = s.summary || null;
        const preset = (s.strictness && s.strictness.preset) || "standard";
        for (const button of document.querySelectorAll("#qc_strictness button[data-preset]")) {
            const on = button.dataset.preset === preset;
            button.classList.toggle("is-active", on);
            button.setAttribute("aria-checked", on ? "true" : "false");
        }
        if (!summary) {
            this.status("No QC for this image yet. Ask an agent to run a QC session, or mark a "
                + "region by hand below.");
        } else {
            const regions = summary.regions || {};
            const cells = summary.cells || {};
            const parts = [];
            parts.push(`${regions.exclude || 0} excluded, ${regions.warn || 0} flagged`);
            if (cells.n) parts.push(`${cells.n_fail || 0} of ${cells.n} cells excluded`);
            this.status(parts.join(" · "));
        }
        this.renderSummary();
        this.renderRegions();
        this.renderChannels();
        const cells = this.el("qc_download_cells");
        const regions = this.el("qc_download_regions");
        if (cells) cells.href = this.api.downloadUrl("cells.csv");
        if (regions) regions.href = this.api.downloadUrl("regions.geojson");
        const report = this.el("qc_download_report");
        if (report) {
            report.href = this.api.downloadUrl("report.html");
            report.hidden = !(s.provenance && s.provenance.result_id);
        }
    }

    renderSummary() {
        const node = this.el("qc_summary");
        if (!node) return;
        node.textContent = "";
        const byReason = ((this.state || {}).cells || {}).by_reason || {};
        const entries = Object.entries(byReason).sort((a, b) => b[1] - a[1]).slice(0, 6);
        for (const [reason, count] of entries) {
            const row = document.createElement("div");
            row.className = "qc-reason";
            row.textContent = `${reason.replace("region:", "").replace(/_/g, " ")}: ${count} cells`;
            node.appendChild(row);
        }
    }

    classColor(id) {
        const found = ((this.vocabulary || {}).classes || []).find((item) => item.id === id);
        return found ? found.color : "#9ca3af";
    }

    classWords(id) {
        const found = ((this.vocabulary || {}).classes || []).find((item) => item.id === id);
        return found ? found.words : String(id || "").replace(/_/g, " ");
    }

    renderRegions() {
        const list = this.el("qc_regions");
        if (!list) return;
        list.textContent = "";
        const regions = ((this.state || {}).regions || []).filter((r) => r.roi_id
            && !(r.user && (r.user.deleted || r.user.removed_from_qc)));
        if (!regions.length) {
            const empty = document.createElement("li");
            empty.className = "qc-empty";
            empty.textContent = "No QC regions";
            list.appendChild(empty);
            return;
        }
        for (const region of regions) {
            const item = document.createElement("li");
            item.className = "qc-item";
            const swatch = document.createElement("span");
            swatch.className = "qc-swatch";
            swatch.style.background = this.classColor(region.class);
            const text = document.createElement("span");
            text.className = "qc-item-text";
            const who = region.created_by === "user" ? " · yours" : "";
            text.textContent = `${this.classWords(region.class)} · ${region.action || "exclude"}`
                + `${(region.channels || []).length ? ` · ${region.channels.slice(0, 3).join(", ")}` : ""}${who}`;
            item.append(swatch, text);
            if (!(region.user && region.user.approved)) {
                const approve = document.createElement("button");
                approve.type = "button";
                approve.className = "qc-mini";
                approve.dataset.approve = region.roi_id;
                approve.dataset.action = region.action || "exclude";
                approve.textContent = "Approve";
                approve.title = "Pin this action whatever the strictness, and lock the shape";
                item.appendChild(approve);
            } else {
                const badge = document.createElement("span");
                badge.className = "qc-badge";
                badge.textContent = "approved";
                item.appendChild(badge);
            }
            list.appendChild(item);
        }
    }

    renderChannels() {
        const list = this.el("qc_channels");
        if (!list) return;
        list.textContent = "";
        for (const channel of (this.state || {}).channels || []) {
            const item = document.createElement("li");
            item.className = `qc-item qc-channel is-${channel.status || "unknown"}`;
            item.textContent = `${channel.name} · ${String(channel.status || "not reviewed").replace(/_/g, " ")}`;
            list.appendChild(item);
        }
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
            docs: "plugins/qc",
        },
        ownsCellLayer: false,
        createSidebarController(ctx) {
            return new QcSidebarController(ctx);
        },
    });
}
