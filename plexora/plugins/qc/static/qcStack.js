/**
 * Stack QC (capabilities_stack.py): the QC panel's section for a z-stack.
 *
 * Runs the stack checks (a job, followed through `/plugins/qc/jobs/<id>`),
 * lists what they found -- one line per finding: its planes, its channel or
 * cycle, the reason -- with a click going to its first plane, and writes the
 * findings as QC regions limited to their planes. The plane line says which
 * plane every other QC check in the card reads, and sets it to the plane on
 * screen (or back to the default).
 *
 * Hidden on a flat image (`ctx.view.planes()` is null there), and not in
 * the panel at all without Plexora 3D (`volume:analysis`).
 */
class QcStack {
    static get POLL_MS() { return 800; }

    constructor(ctx, api, host) {
        this.ctx = ctx;
        this.api = api;
        this.host = host;
        this.result = null;
        this.plane = "";
        this.job = null;
        this.el = (id) => document.getElementById(id);
        const tool = this.el("qc_tool_stack");
        // No section without Plexora 3D (panel.html), and none on a flat image.
        this.enabled = Boolean(tool && this.ctx.view?.planes?.());
        if (tool) tool.hidden = !this.enabled;
        if (!this.enabled) return;
        this.el("qc_stack_run")?.addEventListener("click", () => this.run());
        this.el("qc_stack_write")?.addEventListener("click", () => this.write());
        this.el("qc_stack_use_plane")?.addEventListener("click", () => this.usePlane(true));
        this.el("qc_stack_default_plane")?.addEventListener("click", () => this.usePlane(false));
        this.refresh();
    }

    async refresh() {
        const answer = await this.api.stack().catch(() => null);
        if (answer?.ok) {
            this.result = answer.data.findings ? answer.data : null;
            this.plane = answer.data.plane || "";
        }
        this.render();
    }

    async run() {
        const answer = await this.api.stackRun({}).catch(() => null);
        if (!answer?.ok) {
            this.host.message?.(answer?.data?.error?.message || "Stack QC could not start");
            return;
        }
        this.follow(answer.data.job_id);
    }

    follow(jobId) {
        if (!jobId) return;
        this.job = { job_id: jobId, progress: { done: 0, total: null } };
        this.render();
        const tick = async () => {
            const answer = await this.api.job(jobId).catch(() => null);
            if (!this.job || this.job.job_id !== jobId) return;
            const job = answer?.ok ? answer.data.job : null;
            if (job && job.status === "done") {
                this.job = null;
                await this.refresh();
                return;
            }
            if (job && (job.status === "failed" || job.status === "cancelled")) {
                this.job = null;
                this.host.message?.((job.error && job.error.message) || "Stack QC failed");
                this.render();
                return;
            }
            if (job) this.job = job;
            this.render();
            window.setTimeout(tick, QcStack.POLL_MS);
        };
        window.setTimeout(tick, 150);
    }

    async write() {
        const answer = await this.api.stackWrite({}).catch(() => null);
        if (!answer?.ok) {
            this.host.message?.(answer?.data?.error?.message || "The findings could not be written");
            return;
        }
        const data = answer.data;
        const skipped = (data.skipped || []).length;
        this.host.message?.(`${(data.written || []).length} region${(data.written || []).length === 1 ? "" : "s"} written`
            + (skipped ? `; ${skipped} skipped (${data.skipped[0].why})` : ""));
        this.host.refresh?.();
    }

    async usePlane(onScreen) {
        const view = this.ctx.view;
        const body = onScreen ? { z: view?.planes?.()?.[0] ?? null, t: view?.frame?.() || 0 } : {};
        if (onScreen && body.z === null) return;
        const answer = await this.api.stackPlane(body).catch(() => null);
        if (answer?.ok) this.plane = answer.data.plane || "";
        this.render();
    }

    goTo(finding) {
        this.ctx.view?.setPlane?.(finding.z0);
    }

    render() {
        if (!this.enabled) return;
        const plane = this.el("qc_stack_plane");
        if (plane) {
            plane.textContent = this.plane
                ? `QC reads plane ${this.plane.replace(/^t\d+/, "").replace("z", "z ")}`
                : "QC reads the stack's default plane";
        }
        const reset = this.el("qc_stack_default_plane");
        if (reset) reset.hidden = !this.plane;
        const progress = this.el("qc_stack_progress");
        if (progress) progress.hidden = !this.job;
        if (this.job) {
            const p = this.job.progress || {};
            const share = p.total ? Math.round(100 * (p.done || 0) / p.total) : 5;
            const fill = this.el("qc_stack_fill");
            if (fill) fill.style.width = `${share}%`;
            const phase = this.el("qc_stack_phase");
            if (phase) phase.textContent = p.message || "Checking the stack…";
        }
        const list = this.el("qc_stack_list");
        const summary = this.el("qc_stack_summary");
        const write = this.el("qc_stack_write");
        if (!list) return;
        list.textContent = "";
        const findings = this.result?.findings || [];
        if (summary) {
            summary.textContent = this.result ? (findings.length ? String(findings.length) : "clean") : "";
        }
        if (write) write.hidden = !findings.some((f) => f.check !== "segmentation_3d");
        for (const finding of findings) {
            const row = document.createElement("div");
            row.className = "qc-stack-line";
            row.setAttribute("role", "listitem");
            const where = finding.check === "z_registration_drift" ? `cycle ${finding.cycle}`
                : finding.channel || (finding.check === "segmentation_3d" ? "cells" : "");
            const planes = finding.z0 === finding.z1 ? `z ${finding.z0}` : `z ${finding.z0}–${finding.z1}`;
            const head = document.createElement("button");
            head.type = "button";
            head.className = "qc-stack-where";
            head.textContent = `${planes}${where ? ` · ${where}` : ""}`;
            head.title = "Go to this plane";
            head.addEventListener("click", () => this.goTo(finding));
            const reason = document.createElement("span");
            reason.className = "qc-stack-reason";
            reason.textContent = finding.reason;
            row.append(head, reason);
            list.appendChild(row);
        }
    }
}

if (typeof window !== "undefined") window.QcStack = QcStack;
