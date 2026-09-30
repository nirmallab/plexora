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

    /** The QC regions that count now, with outlines and bounding boxes. */
    async regions() {
        const response = await fetch(this.url("plugins/qc/regions") + "?"
            + new URLSearchParams({ datasource: this.datasource }));
        return this._read(response);
    }

    /** The flagged cells, grouped by reason and status, with their ids. */
    async cells() {
        const response = await fetch(this.url("plugins/qc/cells") + "?"
            + new URLSearchParams({ datasource: this.datasource }));
        return this._read(response);
    }

    deleteRegion(roiId) {
        return this._post("plugins/qc/regions/delete", { roi_id: roiId });
    }

    /** The ROI's own name, through the ROI plugin's update_roi. */
    renameRegion(roiId, name) {
        return this._post("plugins/qc/regions/rename", { roi_id: roiId, name });
    }

    /** The artifact classes, and the QC categories named on this project
     *  (`custom`). */
    async vocabulary() {
        return this._read(await fetch(this.url("plugins/qc/vocabulary") + "?"
            + new URLSearchParams({ datasource: this.datasource })));
    }

    setStrictness(preset) {
        return this._post("plugins/qc/strictness", { preset });
    }

    /** Take in the ROI panel's edits; `views` is {roi_id: [{name, color,
     *  range}]}, the channels on screen when each new region was drawn. */
    refresh(views) {
        return this._post("plugins/qc/refresh", views ? { views } : {});
    }

    /** Trace one region (`{roi_id, force}`) or every one (`{all: true}`) at
     *  pixel level inside its outline. */
    refineRegion(body) {
        return this._post("plugins/qc/regions/refine", body);
    }

    approve(roiId, action) {
        return this._post("plugins/qc/approve", { roi_id: roiId, action, lock: true });
    }

    /** `{class}` or `{reason}`, and a hex colour or null for the default. */
    setColor(target, color) {
        return this._post("plugins/qc/color", Object.assign({ color }, target));
    }

    /** A region drawn by hand in the QC panel: `{class | label, points,
     *  views}`, stored as an ROI and taken into QC in one request. */
    drawRegion(body) {
        return this._post("plugins/qc/regions/draw", body);
    }

    /** `{class}` an artifact class's QC category, or `{label}` one the user
     *  names; either is made when it does not exist yet. */
    addCategory(target) {
        return this._post("plugins/qc/categories", target);
    }

    async _get(path, params) {
        return this._read(await fetch(this.url(path) + "?"
            + new URLSearchParams(Object.assign({ datasource: this.datasource }, params || {}))));
    }

    // -- Registration Check (qc.registration_*) -------------------------------

    registration(includeOverlay = false) {
        return this._get("plugins/qc/registration", includeOverlay ? { include_overlay: 1, include_map: 1 } : {});
    }

    registrationChannels() {
        return this._get("plugins/qc/registration/channels");
    }

    /** `{active, reference, comparison, rule, params, overlay_visible,
     *  flicker, flicker_ms, colors, reset_colors}`, any subset. */
    registrationSet(body) {
        return this._post("plugins/qc/registration/set", body);
    }

    registrationStep(direction) {
        return this._post("plugins/qc/registration/step", { direction });
    }

    registrationCompute(body) {
        return this._post("plugins/qc/registration/compute", body || {});
    }

    // -- Segmentation QC (qc.segmentation_*) and its job ------------------------

    segmentation() {
        return this._get("plugins/qc/segmentation");
    }

    /** `{under, over}` re-threshold the stored scores for viewing, each side
     *  on its own (null or missing: the run's threshold). */
    segmentationCells(flags = {}) {
        const params = {};
        if (flags.under != null) params.flag_under = flags.under;
        if (flags.over != null) params.flag_over = flags.over;
        return this._get("plugins/qc/segmentation/cells", params);
    }

    /** Where the problems are concentrated over `{box, bins, flag_under,
     *  flag_over}`: one share-of-cells grid per category. */
    segmentationDensity(params) {
        return this._get("plugins/qc/segmentation/density", params);
    }

    /** `{dna_channel, force}`; answers `{job_id}` (or the cached summary). */
    segmentationRun(body) {
        return this._post("plugins/qc/segmentation/run", body || {});
    }

    // -- Blur QC (qc.blur_*) --------------------------------------------------

    blur() {
        return this._get("plugins/qc/blur");
    }

    /** `{channel}`: that channel's continuous score grid for the heatmap. */
    blurMap(params = {}) {
        return this._get("plugins/qc/blur/map", params);
    }

    /** `{channel, threshold, min_region_tiles}`: the channel's mask and
     *  regions there, stored nowhere (the slider's preview). */
    blurMask(params = {}) {
        return this._get("plugins/qc/blur/mask", params);
    }

    /** `{channel | channels, force}`; answers `{job_id}`. Default: every
     *  listed channel. */
    blurRun(body) {
        return this._post("plugins/qc/blur/run", body || {});
    }

    /** `{channel, threshold (0..1 or "auto"), color, channels (the listed
     *  ones, or "default"), min_region_tiles}`, receipted. */
    blurSet(body) {
        return this._post("plugins/qc/blur/set", body || {});
    }

    /** `{channel}`, or every channel's result. */
    blurClear(body) {
        return this._post("plugins/qc/blur/clear", body || {});
    }

    blurWriteRegions(body) {
        return this._post("plugins/qc/blur/regions/write", body || {});
    }

    job(jobId) {
        return this._get(`plugins/qc/jobs/${encodeURIComponent(jobId)}`);
    }

    jobCancel(jobId) {
        return this._post(`plugins/qc/jobs/${encodeURIComponent(jobId)}/cancel`, {});
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
