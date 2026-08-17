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

    downloadGatingCSV(channels, selections, selection_ids, fullCsv = false) {
        let form = document.createElement("form");
        form.action = this.url("plugins/gating/download_gating_csv");

        form.method = "post";

        let filename = '';
        if (!fullCsv) {
            filename = document.getElementById('download_input1').value;
        }else{
            filename = document.getElementById('download_input2').value;
        }
        let fileNameElemment = document.createElement("input");
        fileNameElemment.type = "hidden";
        fileNameElemment.value = _.toString(filename);
        fileNameElemment.name = "filename";
        form.appendChild(fileNameElemment);

        let fullCsvElemment = document.createElement("input");
        fullCsvElemment.type = "hidden";
        fullCsvElemment.value = _.toString(fullCsv);
        fullCsvElemment.name = "fullCsv";
        form.appendChild(fullCsvElemment);

        let encoding = document.getElementById('encoding').value;
        let encodingElement = document.createElement("input");
        encodingElement.type = "hidden";
        encodingElement.value = _.toString(encoding);
        encodingElement.name = "encoding";
        form.appendChild(encodingElement);

        let selectionsElement = document.createElement("input");
        selectionsElement.type = "hidden";
        selectionsElement.value = JSON.stringify(selections);
        selectionsElement.name = "filter";
        form.appendChild(selectionsElement);

        let channelsElement = document.createElement("input");
        channelsElement.type = "hidden";
        channelsElement.value = JSON.stringify(channels);
        channelsElement.name = "channels";
        form.appendChild(channelsElement);


        let idsElement = document.createElement("input");
        idsElement.type = "hidden";
        idsElement.value = JSON.stringify(selection_ids);
        idsElement.name = "selection_ids";
        form.appendChild(idsElement);

        let datasourceElement = document.createElement("input");
        datasourceElement.type = "hidden";
        datasourceElement.value = this.datasource;
        datasourceElement.name = "datasource";
        form.appendChild(datasourceElement);

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

    async saveGatesToAnndata(tableName, imageidColumn) {
        let response = await fetch(this.url('plugins/gating/save_gates_to_anndata'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    datasource: this.datasource,
                    table_name: tableName,
                    imageid_column: imageidColumn
                }
            )
        });
        let response_data = await response.json();
        if (!response.ok || !response_data.success) {
            throw new Error(response_data.error || 'Failed to save gates to AnnData');
        }
        return response_data;
    }

    async getGatesFromAnndata(tableName = "gates", imageidColumn = "imageid") {
        try {
            let response = await fetch(this.url('plugins/gating/get_gates_from_anndata') + '?' + new URLSearchParams({
                datasource: this.datasource,
                table_name: tableName,
                imageid_column: imageidColumn
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
