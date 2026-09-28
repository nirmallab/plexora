/**
 * QcApi - the Quality Control plugin's own HTTP client.
 *
 * A plugin owns the addresses of its routes (core never learns them). Every
 * method returns `{ok, status, data}` rather than throwing on a non-2xx: a 409
 * (a QC session is open, or the results moved) is an answer the panel shows.
 */
class QcApi {

    constructor(ctx) {
        this.url = ctx.url;
        this.datasource = ctx.datasource;
    }

    async _read(response) {
        let data = {};
        try {
            data = await response.json();
        } catch (e) {
            data = { error: { message: "the server sent a response that could not be read" } };
        }
        return { ok: response.ok && data.ok !== false, status: response.status, data };
    }

    async _post(path, body) {
        const response = await fetch(this.url(path), {
            method: "POST",
            headers: { "Accept": "application/json", "Content-Type": "application/json" },
            body: JSON.stringify(Object.assign({ datasource: this.datasource }, body || {})),
        });
        return this._read(response);
    }

    async state() {
        const response = await fetch(this.url("plugins/qc/state") + "?"
            + new URLSearchParams({ datasource: this.datasource }));
        return this._read(response);
    }

    async vocabulary() {
        return this._read(await fetch(this.url("plugins/qc/vocabulary")));
    }

    setStrictness(preset) {
        return this._post("plugins/qc/strictness", { preset });
    }

    refresh() {
        return this._post("plugins/qc/refresh");
    }

    approve(roiId, action) {
        return this._post("plugins/qc/approve", { roi_id: roiId, action, lock: true });
    }

    addCategory(artifactClass) {
        return this._post("plugins/qc/categories", { class: artifactClass });
    }

    /** A download link, not a request (the browser follows it). */
    static downloadUrl(url, datasource, kind) {
        return url(`plugins/qc/download/${encodeURIComponent(datasource)}`) + "?"
            + new URLSearchParams({ kind });
    }

    control(sessionUrl, action, extra) {
        return fetch(this.url(sessionUrl), {
            method: "POST",
            headers: { "Accept": "application/json", "Content-Type": "application/json" },
            body: JSON.stringify(Object.assign({ action, datasource: this.datasource }, extra || {})),
        }).then((response) => this._read(response));
    }
}

window.QcApi = QcApi;
