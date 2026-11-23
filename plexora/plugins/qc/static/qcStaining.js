/**
 * Low quality staining (server/stain.py): the QC panel's fold for the marker
 * channels a QC session read as weak or failed.
 *
 * A session shows each marker channel at several scales and beside a partner
 * marker and asks whether the stain worked; a first "failed" is read again by
 * a second reader who never saw the first. This fold lists what came out weak
 * or failed -- failed first -- with the readers' reason; a click on a line
 * shows the picture they judged, a click on the picture opens it full size.
 *
 * A flag for a person, never a removal: weak stains are sometimes read as
 * failed, so nothing here changes a channel, a cell or a gate. Read from the
 * QC state (`low_quality_staining`, `checks_result.low_quality_staining`);
 * runs only inside a QC session, so there is no Run button.
 */
class QcStaining {
    constructor(ctx, api, host) {
        this.ctx = ctx;
        this.api = api;
        this.host = host;
        this.data = null;
        this.open = new Set();
        this.el = (id) => document.getElementById(id);
    }

    /** The state's `low_quality_staining` (or nothing: no session read stains). */
    adopt(staining) {
        this.data = staining || null;
        this.render();
    }

    pictureUrl(id) {
        return this.ctx.url(`agent/v1/captures/${encodeURIComponent(String(id))}`);
    }

    render() {
        const list = this.el("qc_stain_list");
        const summary = this.el("qc_stain_summary");
        const note = this.el("qc_stain_note");
        if (!list) return;
        list.textContent = "";
        const data = this.data;
        const counts = (data && data.counts) || {};
        if (summary) {
            summary.textContent = !data ? ""
                : (counts.failed || counts.weak)
                    ? [counts.failed ? `${counts.failed} failed` : "", counts.weak ? `${counts.weak} weak` : ""]
                        .filter(Boolean).join(" · ")
                    : "none";
        }
        if (note) {
            note.hidden = false;
            note.textContent = !data
                ? "Read inside a QC session: each marker's stain is judged real, weak or failed."
                : `${data.n_read} channel${data.n_read === 1 ? "" : "s"} read: ${counts.real || 0} real`
                  + (counts.cannot_tell ? `, ${counts.cannot_tell} could not be told` : "")
                  + ". A flag for review: weak stains are sometimes read as failed, "
                  + "so nothing was changed.";
        }
        for (const entry of (data && data.channels) || []) {
            const row = document.createElement("div");
            row.className = "qc-line qc-stain-line";
            row.dataset.verdict = entry.verdict;
            row.setAttribute("role", "listitem");
            const dot = document.createElement("span");
            dot.className = "qc-line-dot qc-stain-dot";
            dot.setAttribute("aria-hidden", "true");
            const name = document.createElement("button");
            name.type = "button";
            name.className = "qc-line-name qc-stain-name";
            name.textContent = entry.channel;
            const readings = (entry.readings || []).join(" / ");
            name.title = `${entry.verdict}${readings ? ` (readings: ${readings})` : ""}`
                + (entry.why ? `: ${entry.why}` : "") + (entry.picture ? " — click for the picture" : "");
            const verdict = document.createElement("span");
            verdict.className = "qc-stain-verdict";
            verdict.textContent = entry.verdict;
            row.append(dot, name, verdict);
            const why = document.createElement("div");
            why.className = "qc-stain-why";
            why.textContent = entry.why || "";
            list.append(row, why);
            if (entry.picture) {
                name.addEventListener("click", () => {
                    if (this.open.has(entry.channel)) this.open.delete(entry.channel);
                    else this.open.add(entry.channel);
                    this.render();
                });
                if (this.open.has(entry.channel)) {
                    const img = document.createElement("img");
                    img.className = "qc-stain-picture";
                    img.alt = `${entry.channel}: the picture the readers judged`;
                    img.title = "Open full size";
                    img.loading = "lazy";
                    img.src = this.pictureUrl(entry.picture);
                    img.addEventListener("click", () => window.open(img.src, "_blank", "noopener"));
                    list.appendChild(img);
                }
            }
        }
    }
}

if (typeof window !== "undefined") window.QcStaining = QcStaining;
