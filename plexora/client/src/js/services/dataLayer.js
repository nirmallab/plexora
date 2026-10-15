//todo add crossfilter stuff here... build some lensingFilters and sorters for individual and combined dimensions

class DataLayer {

    constructor(config, imageChannels) {
        var that = this;
        //vars and consts
        this.config = config;
        //all image channels
        this.imageChannels = imageChannels;

        this.imageBitRange = [0, 65536];
        //: A time series' object table (one row per cell per frame): the
        //: frame on screen, `{column, t}`, which every request for rows below
        //: carries -- the cells, centroids, nearest cell and gated ids of that
        //: frame only. Null for every other table (set by ImageViewer.setTableFrame).
        this.tableFrame = null;
        //: A z-stack's measured object table (one row per cell, with its plane
        //: extent): the planes on screen, `[first, last]` inclusive, which the
        //: nearest-cell pick carries so it keeps to the cells on them. Null for
        //: every other table, and in the 3-D view (set by main.js).
        this.tablePlanes = null;
        //selections
        this.currentSelection = new Map();
        // Roles, not literal column names -- see services/datasetContext.js.
        // Null for an image-only project, and also for one whose coordinate
        // columns nobody has identified yet; nothing needing real coordinates
        // should be reachable in either case (callers check `schema` first).
        this.schema = PlexoraDataset.resolveSchema(this.config);
        this.x = this.schema?.x;
        this.y = this.schema?.y;
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

    // There was a submitChannelUpload() here: one POST to /upload_channels
    // with a FormData the channel list built inline. The channel-name upload
    // is a conversation now rather than one request -- the server can come
    // back asking which column holds the names, or reporting a count that does
    // not match -- so it lives with the dialog that has to answer,
    // views/channelNamesUpload.js. Nothing about it needed the data layer's
    // cached state; it only wanted the datasource name.

    /** `{t}` for a request of rows while a frame is on screen (tableFrame). */
    frameQuery() {
        return this.tableFrame ? { t: String(this.tableFrame.t) } : {};
    }

    /** `{z0, z1}` for the nearest-cell pick while planes are on screen (tablePlanes). */
    planeQuery() {
        const planes = this.tablePlanes;
        return planes ? { z0: String(planes[0]), z1: String(planes[1]) } : {};
    }

    /** `filter` (column -> [low, high], low < value <= high) narrowed to the
     *  frame on screen, for the routes that take a range filter. */
    withFrame(filter) {
        if (!this.tableFrame) return filter || {};
        const t = Number(this.tableFrame.t);
        return { ...(filter || {}), [this.tableFrame.column]: [t - 0.5, t + 0.5] };
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
                datasource: datasource,
                ...this.frameQuery(),
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
                        filter: this.withFrame(filter),
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

    async getDatabaseDescription() {
        try {
            let response = await fetch(plexoraUrl('get_database_description') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            // The cells are still being read behind the viewer, so this came
            // back without them: views/loadProgress.js takes them on when
            // they land, however soon that is.
            if (response.headers.get('X-Plexora-Cells-Pending') === '1' && window.__plexora) {
                window.__plexora.cellsPending = true;
            }
            let description = await response.json();
            return description;
        } catch (e) {
            console.log("Error Getting DB Description", e);
        }
    }

    /**
     * Every way this sample's table, mask and image fail to describe the same
     * sample: `[{code, message}]`, and usually empty.
     *
     * Core's rather than any one plugin's, because nothing in the three files
     * says they belong together and every tool that draws per-cell results has
     * the question. Server-side in `models/consistency.py`, so the answer is
     * one answer.
     *
     * An empty array on failure, not undefined: the caller shows what comes
     * back, and a request that did not land is not evidence of a problem.
     */
    async getConsistencyReport() {
        try {
            let response = await fetch(plexoraUrl('get_consistency_report') + '?' + new URLSearchParams({
                datasource: datasource
            }))
            let findings = await response.json();
            return Array.isArray(findings) ? findings : [];
        } catch (e) {
            console.log("Error Getting Consistency Report", e);
            return [];
        }
    }

    /**
     * Correct how this project's table is read, for a finding marked
     * `fix: "table"` -- found by checking candidates against the mask, applied
     * on every read, and never written to the file (models/table_correction.py).
     *
     * `{success, applied, reloaded, message}`, or `{success: false, error}`
     * when the request itself failed. `applied: false` with a message is an
     * answer (the table already lines up; nothing tried does), not an error.
     */
    async fixTable() {
        return this.postTableFix('fix_table');
    }

    /** Read the table exactly as its file says again. Same reply shape. */
    async undoTableFix() {
        return this.postTableFix('undo_table_fix');
    }

    async postTableFix(route) {
        try {
            const response = await fetch(plexoraUrl(route), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ datasource: datasource }),
            });
            const reply = await response.json();
            return reply && typeof reply === 'object' ? reply : { success: false };
        } catch (e) {
            console.log("Error Correcting the Table", e);
            return { success: false, error: "The server could not be reached." };
        }
    }

    /**
     * What this sample is still preparing, as one document.
     *
     * `{layers: {id: {status, progress, stage, stage_label, message, error,
     * install}}, pending}` -- the mask, the table and every layer builder
     * composed by `layer_jobs.status`. One request whichever of them started
     * the work, which is the whole reason that module exists.
     *
     * Errors are swallowed and undefined returned, exactly as
     * `getSegmentationStatus` does: the caller is a poll loop, and a
     * transient failure is one missed tick rather than an abandoned job.
     */
    async getLayerStatus() {
        try {
            let response = await fetch(plexoraUrl('import/status') + '?' + new URLSearchParams({
                sample: datasource
            }))
            return await response.json();
        } catch (e) {
            console.log("Error Getting Layer Status", e);
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

    async getImageChannelStats(channel, view = "") {
        try {
            const query = { channel: channel, datasource: datasource };
            // A projection's own brightness (a z-stack's `view=` token).
            if (view) query.view = view;
            let response = await fetch(plexoraUrl('get_image_channel_stats') + '?' + new URLSearchParams(query))
            return await response.json();
        } catch (e) {
            console.log("Error Getting Image Channel Stats", e);
        }
    }

    async getChannelCellIds(sels) {
        try {
            let response = await fetch(plexoraUrl('get_channel_cell_ids') + '?' + new URLSearchParams({
                filter: JSON.stringify(this.withFrame(sels)),
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
                datasource: datasource,
                ...this.frameQuery(),
                ...this.planeQuery(),
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
        if (!this.schema) return;

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
        this.currentSelection.set(item[this.schema.cellId], item);

        // console.log('current selection size:', this.currentSelection.size);
        if (this.currentSelection.size > 0) {
            // console.log('id: ', this.currentSelection.values().next().value.id);
        }
    }


    addAllToCurrentSelection(items, allowDelete, clearPriors) {
        // console.log("update current selection")
        // No real per-cell id field for a quick-view (no feature data)
        // datasource -- nothing to key a selection by.
        if (!this.schema) return;
        var that = this;
        let idField = this.schema.cellId
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
     * dataset.isTransformed flag, which had to be set correctly
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

    /**
     * The precision one gate VALUE is kept at: the range's grid as a floor,
     * and the value's own decimals where it has more.
     *
     * `gateDecimals` is the grid a drag moves on, and nothing else. A gate
     * typed as 7.42 -- or read off a paper, or loaded from a CSV -- on a
     * marker whose span puts that grid at whole numbers is still 7.42; the
     * grid rounding it to 7 was the bug. Dust is stripped first (twelve
     * significant digits), so 0.1 + 0.2 counts as one decimal and not
     * seventeen. Capped at 8, which is past anything anybody gates at.
     * @param {number[]} range - [min, max] of the channel being gated
     * @param {number} value - the gate value
     * @returns {number}
     */
    gateValueDecimals(range, value) {
        const floor = this.gateDecimals(range);
        const number = Number(value);
        if (!Number.isFinite(number)) return floor;
        const [mantissa, exponent = "0"] = String(Number(number.toPrecision(12))).split("e");
        const own = (mantissa.split(".")[1] || "").length - Number(exponent);
        return Math.max(floor, Math.min(8, Math.max(0, own)));
    }


}
