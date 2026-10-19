/**
 * GatingApi - this plugin's own HTTP client.
 *
 * These nine methods lived on core's DataLayer, which meant core shipped the
 * URLs of a plugin's routes: `plugins/gating/save_gating_list` and friends were
 * written into plexora/client/src/js/services/dataLayer.js. A core-only build
 * carried them for a tool it did not have, and no third-party plugin could ever
 * be given the same treatment -- it would have to bring its own client. Gating
 * was privileged in exactly the way the plugin API exists to rule out.
 *
 * Nothing about the requests changed in the move. Only the two globals they
 * reached for did: `plexoraUrl(...)` and the bare `datasource` are now
 * `ctx.url(...)` and `ctx.datasource`, which is what makes these calls resolve
 * correctly under a mounted deployment (PLEXORA_BASE_URL, e.g. Jupyter's proxy)
 * instead of depending on core's script-scope names.
 *
 * Each method swallows its own errors, as it did on DataLayer. That is what hid
 * `saveGatingList` failing outright for every call after `lassos` stopped being
 * a parameter but stayed in the payload; tests/js/datalayer_globals_probe.mjs
 * exists to catch a repeat, and points here.
 */
class GatingApi {

    constructor(ctx) {
        this.url = ctx.url;
        this.datasource = ctx.datasource;
    }

    async getUploadedGatingCsvValues() {
        try {
            let response = await fetch(this.url('plugins/gating/get_uploaded_gating_csv_values') + '?' + new URLSearchParams({
                datasource: this.datasource
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Uploaded Gates", e);
        }
    }

    async getSavedGatingList() {
        try {
            let response = await fetch(this.url('plugins/gating/get_saved_gating_list') + '?' + new URLSearchParams({
                datasource: this.datasource
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Saved Gating List", e);
        }
    }

    /**
     * Where each gate came from: {marker: {method, status, confidence, ...}}.
     * An empty object when there is no provenance (or the request fails --
     * provenance only ever adds to what the panel shows).
     */
    async getGateProvenance() {
        try {
            const response = await fetch(this.url('plugins/gating/get_gate_provenance') + '?' + new URLSearchParams({
                datasource: this.datasource
            }));
            if (!response.ok) return {};
            return (await response.json()).provenance || {};
        } catch (e) {
            console.log("Error Getting Gate Provenance", e);
            return {};
        }
    }

    /** approved | locked | excluded, or unapproved | unlocked | included. */
    async setGateStatus(marker, status) {
        const response = await fetch(this.url('plugins/gating/set_gate_status'), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ datasource: this.datasource, marker, status }),
        });
        if (!response.ok) throw new Error(`could not set ${marker} to ${status} (HTTP ${response.status})`);
        return response.json();
    }

    /**
     * Put markers back to never gated -- full range, no provenance --
     * every marker when `markers` is null. Answers `{cleared, kept}`, `kept`
     * being `{marker: "locked"}` for the gates a lock kept.
     */
    async clearGates(markers = null) {
        const response = await fetch(this.url('plugins/gating/clear_gates'), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ datasource: this.datasource, markers }),
        });
        if (!response.ok) throw new Error(`could not clear the gates (HTTP ${response.status})`);
        return response.json();
    }

    /** pause | resume | take_over an agent's gating session. */
    async controlAgentSession(sessionId, action, marker) {
        const response = await fetch(this.url(`plugins/gating/agent_session/${encodeURIComponent(sessionId)}/control`), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ action, marker, datasource: this.datasource }),
        });
        if (!response.ok) throw new Error(`the session did not accept "${action}" (HTTP ${response.status})`);
        return response.json();
    }

    /**
     * The gates, as a CSV, wherever the user wants it.
     *
     * Two ways down, and the fork is not cosmetic. A form submitted with
     * `form.submit()` fires no event and cannot be intercepted, so it can only
     * ever land in Downloads -- which is the right answer, and the only one
     * needed, whenever there is just the one machine to save to. When there IS
     * somewhere else, the file has to become a Blob before it can be sent
     * there, so the same POST is made with fetch and handed to the shared
     * layer. See services/fileLocation.js.
     *
     * The filename is built here rather than read out of a text box: the
     * download panel that held that box also held a second download and an
     * encoding picker, and all three went when the per-cell export did.
     */
    async downloadGatingCSV(channels, selections) {
        const filename = `${this.datasource}_gated_channel_ranges`;
        const fields = {
            filename: filename,
            filter: JSON.stringify(selections),
            channels: JSON.stringify(channels),
            datasource: this.datasource,
        };

        if (!(window.PlexoraFileLocation
                && window.PlexoraFileLocation.remoteAvailable())) {
            this._downloadGatingCSVViaForm(fields);
            return;
        }

        // Caught rather than thrown on: the caller presses a button and walks
        // away, so a rejection here is an unhandled one in the console and
        // nothing on screen either way.
        try {
            const body = new FormData();
            Object.entries(fields).forEach(([name, value]) => body.append(name, value));
            const response = await fetch(
                this.url("plugins/gating/download_gating_csv"),
                { method: 'POST', body: body });
            if (!response.ok) {
                console.log("Error Downloading Gating CSV", response.status);
                return;
            }
            await window.PlexoraFileLocation.deliver(
                await response.blob(), `${filename}.csv`);
        } catch (e) {
            console.log("Error Downloading Gating CSV", e);
        }
    }

    /** The plain download: a hidden form, posted and gone. */
    _downloadGatingCSVViaForm(fields) {
        let form = document.createElement("form");
        form.action = this.url("plugins/gating/download_gating_csv");

        form.method = "post";

        Object.entries(fields).forEach(([name, value]) => {
            const element = document.createElement("input");
            element.type = "hidden";
            element.value = value;
            element.name = name;
            form.appendChild(element);
        });

        document.body.appendChild(form);
        form.submit()
    }

    async saveGatingList(channels, selections) {
        try {
            let response = await fetch(this.url('plugins/gating/save_gating_list'), {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(
                    {
                        datasource: this.datasource,
                        filter: selections,
                        channels: channels
                    }
                )
            });
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Saving Gating List", e);
        }
    }

    async saveGatesToAnndata(tableName) {
        let response = await fetch(this.url('plugins/gating/save_gates_to_anndata'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    datasource: this.datasource,
                    table_name: tableName
                }
            )
        });
        let response_data = await response.json();
        if (!response.ok || !response_data.success) {
            throw new Error(response_data.error || 'Failed to save gates to AnnData');
        }
        return response_data;
    }

    async getGatesFromAnndata(tableName = "gates") {
        try {
            let response = await fetch(this.url('plugins/gating/get_gates_from_anndata') + '?' + new URLSearchParams({
                datasource: this.datasource,
                table_name: tableName
            }));
            let response_data = await response.json();
            return response_data.success ? response_data.gates : {};
        } catch (e) {
            console.log("Error Getting Gates From AnnData", e);
            return {};
        }
    }

    async submitGatingUpload(formData) {
        try {
            formData.append('datasource', this.datasource);
            let response = await fetch(this.url('plugins/gating/upload_gates'), {
                method: "POST",
                body: formData
            })
            let cell = await response.json();
            return cell;
        } catch (e) {
            console.log("Error Getting Submitting Form Upload", e);
        }
    }

    async getGatedCellIds(filter, start_keys) {
        try {
            let response = await fetch(this.url('plugins/gating/get_gated_cell_ids') + '?' + new URLSearchParams({
                filter: JSON.stringify(filter),
                start_keys: start_keys,
                datasource: this.datasource
            }))
            let cellIds = await response.json();
            return cellIds;
        } catch (e) {
            console.log("Error Getting Gated Cell Ids", e);
        }
    }

    async getGatingGMM(channel, selection_ids) {
        try {
            let response = await fetch(this.url('plugins/gating/get_gating_gmm'), {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(
                    {
                        channel: channel,
                        datasource: this.datasource,
                        selection_ids: selection_ids
                    }
                )
            });
            let packet_gmm = await response.json();
            return packet_gmm;
        } catch (e) {
            console.log("Error Getting Gating GMM", e);
        }
    }

    // -- Single Cell Classifier (gatingClassifier.js) --------------------------
    //
    // Unlike the methods above these THROW on failure, with the server's own
    // sentence as the message: every one answers something the user just
    // pressed, and the panel says why it did not happen.

    async _classifierAnswer(response) {
        let body = null;
        try {
            body = await response.json();
        } catch (e) {
            body = null;
        }
        if (!response.ok || !body || body.ok === false) {
            const message = body?.error?.message || body?.error || `HTTP ${response.status}`;
            throw new Error(String(message));
        }
        return body;
    }

    _classifierPost(path, payload) {
        return fetch(this.url(path), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ datasource: this.datasource, ...payload }),
        });
    }

    /** Mode, example counts, and for `marker` its examples, run and staleness. */
    async classifierState(marker) {
        const query = new URLSearchParams({ datasource: this.datasource });
        if (marker) query.set('marker', marker);
        const response = await fetch(this.url('plugins/gating/classifier/state') + '?' + query);
        return this._classifierAnswer(response);
    }

    async setClassifierMode(mode) {
        return this._classifierAnswer(await this._classifierPost('plugins/gating/classifier/mode', { mode }));
    }

    /** The cell under an image point (level 0 pixels): {cell_id, method, x, y}. */
    async classifierCellAt(x, y, radius) {
        const query = new URLSearchParams({ datasource: this.datasource, x, y });
        if (radius) query.set('radius', radius);
        const response = await fetch(this.url('plugins/gating/classifier/cell_at') + '?' + query);
        return this._classifierAnswer(response);
    }

    /** label: "positive" | "negative" | null. Same label again removes it. */
    async classifierAnnotate(marker, cellId, label) {
        return this._classifierAnswer(await this._classifierPost(
            'plugins/gating/classifier/annotate', { marker, cell_id: cellId, label }));
    }

    async classifierClearExamples(markers) {
        return this._classifierAnswer(await this._classifierPost(
            'plugins/gating/classifier/examples/clear', markers ? { markers } : {}));
    }

    /** Start a run: {job_id}, or the job already running on this project.
     *  `auto`: the wand -- the server picks the examples first. */
    async classifierRun(marker, auto) {
        return this._classifierAnswer(await this._classifierPost(
            'plugins/gating/classifier/run', { marker, auto: auto === true }));
    }

    /** Per table row: the run's score (NaN where not classified) and the share
     *  of resampled models agreeing with its call (0-100). */
    async classifierScores(marker) {
        const response = await fetch(this.url('plugins/gating/classifier/scores') + '?'
            + new URLSearchParams({ datasource: this.datasource, marker }));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const body = await response.arrayBuffer();
        const rows = body.byteLength / 5;
        return {
            run: response.headers.get('X-Classifier-Run') || '',
            scoreName: response.headers.get('X-Classifier-Score-Name') || 'score',
            scores: new Float32Array(body.slice(0, rows * 4)),
            agreement: new Uint8Array(body.slice(rows * 4)),
        };
    }

    async classifierRevert(marker) {
        return this._classifierAnswer(await this._classifierPost(
            'plugins/gating/classifier/revert', { marker }));
    }

    async classifierReset() {
        return this._classifierAnswer(await this._classifierPost('plugins/gating/classifier/reset', {}));
    }

    /** One byte per table row (0 negative, 1 positive, 2 not classified,
     *  3 uncertain) and the counts from the headers. */
    async classifierCalls(marker) {
        const response = await fetch(this.url('plugins/gating/classifier/calls') + '?'
            + new URLSearchParams({ datasource: this.datasource, marker }));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const header = (name) => response.headers.get(`X-Classifier-${name}`);
        return {
            codes: new Uint8Array(await response.arrayBuffer()),
            state: header('State') || 'not_run',
            run: header('Run') || '',
            positive: Number(header('Positive') || 0),
            eligible: Number(header('Eligible') || 0),
            uncertain: Number(header('Uncertain') || 0),
            notClassified: Number(header('Not-Classified') || 0),
        };
    }

    /** kind: "binary" | "scores" | "annotations"; the file as a Blob. */
    async classifierDownload(kind) {
        const path = kind === 'annotations'
            ? 'plugins/gating/classifier/export/annotations.json'
            : `plugins/gating/classifier/export/${kind}.csv`;
        const response = await fetch(this.url(path) + '?'
            + new URLSearchParams({ datasource: this.datasource }));
        if (!response.ok) {
            await this._classifierAnswer(response);
        }
        return response.blob();
    }

    /** Check an annotations file; nothing is applied. {token, markers, problems, warnings}. */
    async classifierPreview(file) {
        const form = new FormData();
        form.append('datasource', this.datasource);
        form.append('file', file);
        const response = await fetch(this.url('plugins/gating/classifier/annotations/preview'), {
            method: 'POST', body: form,
        });
        return this._classifierAnswer(response);
    }

    /** mode: "merge" | "replace". */
    async classifierApply(token, mode) {
        return this._classifierAnswer(await this._classifierPost(
            'plugins/gating/classifier/annotations/apply', { token, mode }));
    }

    /** A gating job's record: {status, progress, error, result}. */
    async gatingJob(jobId) {
        const response = await fetch(this.url(`plugins/gating/jobs/${encodeURIComponent(jobId)}`));
        return (await this._classifierAnswer(response)).job;
    }

    async cancelGatingJob(jobId) {
        const response = await fetch(this.url(`plugins/gating/jobs/${encodeURIComponent(jobId)}/cancel`), {
            method: 'POST',
        });
        return this._classifierAnswer(response);
    }
}
