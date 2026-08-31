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
     * The gated cells, as a CSV, wherever the user wants it.
     *
     * Two ways down, and the fork is not cosmetic. The hidden form below is a
     * streamed download: the browser writes the response straight to disk, so
     * a full CSV of two million cells never exists in the tab. That is worth
     * keeping, and it is what runs whenever there is only one machine to save
     * to -- which is every single-server install.
     *
     * When there IS somewhere else, the file has to become a Blob before it
     * can be sent anywhere but Downloads, so the same POST is made with fetch
     * and handed to the shared layer. A form submitted with `form.submit()`
     * fires no event and cannot be intercepted, which is why this asks rather
     * than being asked. See services/fileLocation.js.
     */
    async downloadGatingCSV(channels, selections, selection_ids, fullCsv = false) {
        let filename = '';
        if (!fullCsv) {
            filename = document.getElementById('download_input1').value;
        }else{
            filename = document.getElementById('download_input2').value;
        }
        const encoding = document.getElementById('encoding').value;
        const fields = {
            filename: _.toString(filename),
            fullCsv: _.toString(fullCsv),
            encoding: _.toString(encoding),
            filter: JSON.stringify(selections),
            channels: JSON.stringify(channels),
            selection_ids: JSON.stringify(selection_ids),
            datasource: this.datasource,
        };

        if (!(window.PlexoraFileLocation
                && window.PlexoraFileLocation.remoteAvailable())) {
            this._downloadGatingCSVViaForm(fields);
            return;
        }

        // Caught rather than thrown on: both callers press a button and walk
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

    /** The streaming download, unchanged: a hidden form, posted and gone. */
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
}
