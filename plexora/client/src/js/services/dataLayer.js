//todo add crossfilter stuff here... build some lensingFilters and sorters for individual and combined dimensions

class DataLayer {

    constructor(config, imageChannels) {
        var that = this;
        //vars and consts
        this.config = config;
        //all image channels
        this.imageChannels = imageChannels;

        this.imageBitRange = [0, 65536];
        //selections
        this.currentSelection = new Map();
        //x,z coords -- undefined for a no-feature-data (quick-view) datasource,
        //whose featureData is an empty list; nothing that needs real coordinates
        //should be reachable in that case (see has_feature_data guards elsewhere).
        this.x = this.config["featureData"]?.[dataSrcIndex]?.["xCoordinate"];
        this.y = this.config["featureData"]?.[dataSrcIndex]?.["yCoordinate"];
        this.phenotypes = [];
    }

    async init() {
        try {
            await fetch(plexoraUrl('init_database') + '?' + new URLSearchParams({
                datasource: datasource
            }))

        } catch (e) {
            console.log("Error Initializing Dataset", e);
        }
    }

    async getRow(row) {
        try {
            let response = await fetch(plexoraUrl('get_database_row') + '?' + new URLSearchParams({
                row: row,
                datasource: datasource
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Row", e);
        }
    }

    async getUploadedGatingCsvValues() {
        try {
            let response = await fetch(plexoraUrl('get_uploaded_gating_csv_values') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Uploaded Gates", e);
        }
    }

    async getSavedGatingList() {
        try {
            let response = await fetch(plexoraUrl('get_saved_gating_list') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Saved Gating List", e);
        }
    }

    async getSavedChannelList() {
        try {
            let response = await fetch(plexoraUrl('get_saved_channel_list') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Saved Channel List", e);
        }
    }

    downloadGatingCSV(channels, selections, selection_ids, fullCsv = false) {
        let form = document.createElement("form");
        form.action = plexoraUrl("download_gating_csv");

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
        datasourceElement.value = datasource;
        datasourceElement.name = "datasource";
        form.appendChild(datasourceElement);

        document.body.appendChild(form);
        form.submit()
    }

    async saveGatingList(channels, selections) {
        const self = this;
        try {
            let response = await fetch(plexoraUrl('save_gating_list'), {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(
                    {
                        datasource: datasource,
                        filter: selections,
                        channels: channels,
                        lassos: lassos
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
        let response = await fetch(plexoraUrl('save_gates_to_anndata'), {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(
                {
                    datasource: datasource,
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
            let response = await fetch(plexoraUrl('get_gates_from_anndata') + '?' + new URLSearchParams({
                datasource: datasource,
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

    async saveChannelList(map_channels, active_channels, list_colors, list_ranges, list_channels) {
        const self = this;
        try {
            let response = await fetch(plexoraUrl('save_channel_list'), {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(
                    {
                        datasource: datasource,
                        map_channels: map_channels,
                        active_channels: active_channels,
                        list_colors: list_colors,
                        list_ranges: list_ranges,
                        list_channels: list_channels
                    }
                )
            });
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Saving Channel List", e);
        }
    }

    async getColumnDistributions(columns) {
        try {
            let response = await fetch(plexoraUrl('get_column_distributions') + '?' + new URLSearchParams({
                columns: columns,
                datasource: datasource
            }))
            let distributions = await response.json();
            return distributions;
        } catch (e) {
            console.log("Error Getting Nearest Cell", e);
        }
    }

    async submitGatingUpload(formData) {
        try {
            formData.append('datasource', datasource);
            let response = await fetch(plexoraUrl('upload_gates'), {
                method: "POST",
                body: formData
            })
            let cell = await response.json();
            return cell;
        } catch (e) {
            console.log("Error Getting Submitting Form Upload", e);
        }
    }

    async submitChannelUpload(formData) {
        try {
            formData.append('datasource', datasource);
            let response = await fetch(plexoraUrl('upload_channels'), {
                method: "POST",
                body: formData
            })
            let cell = await response.json();
            return cell;
        } catch (e) {
            console.log("Error Getting Submitting Form Upload", e);
        }
    }

    async getAllCells(start_keys, use_integer) {
        const dtype = use_integer ? 'integer' : 'float'
        const base_url = plexoraUrl(`get_all_cells/${dtype}/`) + '?'
        try {
            const headers = new Headers();
            headers.append("Content-Type","application/octet-stream");
            headers.append("Content-Encoding","gzip");
            const response = await fetch(base_url + new URLSearchParams({
                start_keys: start_keys,
                datasource: datasource
            }), {
                headers: headers
            })
            return response.arrayBuffer();
        } catch (e) {
            console.log("Error Getting Gated Cell Ids", e);
        }
    }

    async getCentroidManifest() {
        try {
            let response = await fetch(plexoraUrl('get_centroid_manifest') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            if (!response.ok) {
                throw new Error(`Centroid manifest request failed: ${response.status}`);
            }
            const text = await response.text();
            if (!text.trim().startsWith("{")) {
                throw new Error("Centroid manifest response was not JSON");
            }
            return JSON.parse(text);
        } catch (e) {
            console.log("Error Getting Centroid Manifest", e);
            return null;
        }
    }

    async getCentroidTiles(level, tiles, filter = {}, maxPoints = 50000) {
        try {
            let response = await fetch(plexoraUrl('get_centroid_tiles'), {
                method: 'POST',
                headers: {
                    'Accept': 'application/octet-stream',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(
                    {
                        datasource: datasource,
                        level: level,
                        tiles: tiles,
                        filter: filter || {},
                        max_points: maxPoints
                    }
                )
            });
            if (!response.ok) {
                throw new Error(`Centroid tile request failed: ${response.status}`);
            }
            const contentType = response.headers.get("Content-Type") || "";
            if (!contentType.includes("application/octet-stream")) {
                throw new Error("Centroid tile response was not binary");
            }
            return response.arrayBuffer();
        } catch (e) {
            console.log("Error Getting Centroid Tiles", e);
            return null;
        }
    }

    async getGatedCellIds(filter, start_keys) {
        try {
            let response = await fetch(plexoraUrl('get_gated_cell_ids') + '?' + new URLSearchParams({
                filter: JSON.stringify(filter),
                start_keys: start_keys,
                datasource: datasource
            }))
            let cellIds = await response.json();
            return cellIds;
        } catch (e) {
            console.log("Error Getting Gated Cell Ids", e);
        }
    }

    async getDatabaseDescription() {
        try {
            let response = await fetch(plexoraUrl('get_database_description') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            let description = await response.json();
            return description;
        } catch (e) {
            console.log("Error Getting DB Description", e);
        }
    }

    async getSegmentationStatus() {
        try {
            let response = await fetch(plexoraUrl('get_segmentation_status') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            return await response.json();
        } catch (e) {
            console.log("Error Getting Segmentation Status", e);
        }
    }

    async getChannelGMM(channel) {
        try {
            let response = await fetch(plexoraUrl('get_channel_gmm') + '?' + new URLSearchParams({
                channel: channel,
                datasource: datasource
            }))
            let packet_gmm = await response.json();
            return packet_gmm;
        } catch (e) {
            console.log("Error Getting Channel GMM", e);
        }
    }

    async getImageChannelStats(channel) {
        try {
            let response = await fetch(plexoraUrl('get_image_channel_stats') + '?' + new URLSearchParams({
                channel: channel,
                datasource: datasource
            }))
            return await response.json();
        } catch (e) {
            console.log("Error Getting Image Channel Stats", e);
        }
    }

    async getGatingGMM(channel, selection_ids) {
        try {
            let response = await fetch(plexoraUrl('get_gating_gmm'), {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(
                    {
                        channel: channel,
                        datasource: datasource,
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

    async getChannelCellIds(sels) {
        try {
            let response = await fetch(plexoraUrl('get_channel_cell_ids') + '?' + new URLSearchParams({
                filter: JSON.stringify(sels),
                datasource: datasource
            }))
            let cellIds = await response.json();
            return cellIds;
        } catch (e) {
            console.log("Error Getting Channel Cell Ids", e);
        }
    }

    async getChannelNames(shortNames = true) {
        try {
            let response = await fetch(plexoraUrl('get_channel_names') + '?' + new URLSearchParams({
                datasource: datasource,
                shortNames: shortNames
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Sample Row", e);
        }
    }

    async getNearestCell(point_x, point_y) {
        try {
            let response = await fetch(plexoraUrl('get_nearest_cell') + '?' + new URLSearchParams({
                point_x: point_x,
                point_y: point_y,
                datasource: datasource
            }))
            let cell = await response.json();
            return cell;
        } catch (e) {
            console.log("Error Getting Nearest Cell", e);
        }
    }


    getCurrentSelection() {
        return this.currentSelection;
    }

    clearCurrentSelection() {
        this.currentSelection.clear();
    }

    getImageBitRange(float = false) {
        const self = this;
        if (!float) {
            return self.imageBitRange;
        } else {
            return [0.0, 1.0];
        }
    }

    addToCurrentSelection(item, allowDelete, clearPriors) {
        // No real per-cell id field for a quick-view (no feature data)
        // datasource -- nothing to key a selection by.
        if (!this.config.featureData?.[0]) return;

        // delete item on second click
        if (allowDelete && this.currentSelection.has(item)) {
            this.currentSelection.delete(item);
            if (clearPriors) {
                this.currentSelection.clear();
            }

            // console.log('current selection size:', this.currentSelection.size);
            if (this.currentSelection.size > 0) {
                // console.log('id: ', this.currentSelection.values().next().value.id);
            }
            return;
        }

        // clear previous items
        if (clearPriors) {
            this.currentSelection.clear();
        }

        // add new item
        this.currentSelection.set(item[this.config.featureData[0].idField], item);

        // console.log('current selection size:', this.currentSelection.size);
        if (this.currentSelection.size > 0) {
            // console.log('id: ', this.currentSelection.values().next().value.id);
        }
    }


    addAllToCurrentSelection(items, allowDelete, clearPriors) {
        // console.log("update current selection")
        // No real per-cell id field for a quick-view (no feature data)
        // datasource -- nothing to key a selection by.
        if (!this.config.featureData?.[0]) return;
        var that = this;
        let idField = this.config.featureData[0].idField
        that.currentSelection = new Map(items.map(i => [(i[idField]), i]));
        // console.log("update current selection done")
    }

    isImageFeature(key) {
        if (this.imageChannels.hasOwnProperty(key)
            && key != 'CellId' && key != 'id' && key != 'CellID' && key != 'ID' && key != 'Area') {
            return true;
        }
        return false;
    }

    getShortChannelName(fullname) {
        var shortname = fullname;
        this.config["imageData"].forEach(function (channel) {
            if (channel.fullname == fullname) {
                shortname = channel.name;
            }
        });
        return shortname;
    }

    getFullChannelName(shortname) {
        var fullname = shortname;
        this.config["imageData"].forEach(function (channel) {
            if (channel.name == shortname) {
                fullname = channel.fullname;
            }
        });
        return fullname;
    }

    async getMetadata() {
        try {
            let response = await fetch(plexoraUrl('get_ome_metadata') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            let response_data = await response.json();
            return response_data;
        } catch (e) {
            console.log("Error Getting Metadata", e);
        }
    }

    /**
     * Decimal-place precision for gate slider rounding, derived from the
     * channel's own observed [min, max] span rather than a stored config
     * flag -- aims for ~200 distinguishable steps across the range. Wide
     * integer-scale channels (e.g. raw 0-65535 pixel intensities) land on 0
     * decimals (whole-number gates); narrow float-scale channels (e.g.
     * already log/arcsinh-transformed markers spanning ~1.7-2.2) get enough
     * decimal places to stay meaningful. Replaces the old
     * featureData.isTransformed config flag, which had to be set correctly
     * (and consistently across datasources built from the same source data)
     * at import time and silently broke gating whenever it didn't match the
     * data's actual scale -- this is instead computed fresh from the same
     * min/max every request already returns, so it can't drift out of sync.
     * @param {number[]} range - [min, max] of the channel being gated
     * @returns {number}
     */
    gateDecimals(range) {
        const span = Math.abs(((range && range[1]) || 0) - ((range && range[0]) || 0));
        if (!Number.isFinite(span) || span <= 0) return 0;
        const decimals = Math.ceil(Math.log10(200 / span));
        return Math.max(0, Math.min(6, decimals));
    }


}
