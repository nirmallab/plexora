/**
 * FigureBuilderApi - this plugin's own HTTP client.
 *
 * A plugin owns the addresses of its own routes. Core's DataLayer must never
 * learn them, which is why `plugins/figure_builder/...` appears in this file
 * and nowhere in plexora/client/src/ (asserted by
 * tests/test_datalayer_requests.py). It is also why the Figures tab on the Open
 * Project page is a server-rendered link rather than something core JavaScript
 * feature-detects: detection would mean core calling a plugin route.
 *
 * Nothing here swallows its own errors, for the reason RoiApi documents: a save
 * that quietly does nothing means the user's work exists only in a tab they are
 * about to close, and a figure can represent a day of it.
 *
 * Requests return `{ok, status, data}` rather than throwing on a non-2xx,
 * because for three of them the status IS the answer. 404 means the figure has
 * been deleted -- probably in the other tab. 409 means somebody else saved
 * first and there is work worth keeping on both sides. 422 means the stored
 * document cannot be read by this build, and nothing should be drawn or written
 * until the user has been told.
 */
class FigureBuilderApi {

    /**
     * @param {object} [options]
     * @param {function} [options.url] path -> URL. Supplied by the plugin
     *        context inside the viewer; on this plugin's own pages there is no
     *        context, so it falls back to core's global, which is the same
     *        function the viewer's context wraps.
     */
    constructor(options) {
        const provided = options && options.url;
        this.url = provided || ((path) => window.plexoraUrl(path));
    }

    _api(path) {
        return this.url("plugins/figure_builder/api/" + path);
    }

    /** Where a figure's own page lives -- for links and for navigation. */
    figureHref(figureId) {
        return this.url("plugins/figure_builder/figure/" + encodeURIComponent(figureId));
    }

    libraryHref() {
        return this.url("plugins/figure_builder/figures");
    }

    async _read(response) {
        let data = {};
        try {
            data = await response.json();
        } catch (e) {
            // A body that is not JSON is a server fault, not an answer. Keep
            // the status, which is the part still worth acting on.
            data = { error: "the server sent a response that could not be read" };
        }
        return { ok: response.ok, status: response.status, data };
    }

    async _json(path, method, body) {
        const response = await fetch(this._api(path), {
            method: method,
            headers: { "Accept": "application/json", "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
        return this._read(response);
    }

    async listFigures() {
        const response = await fetch(this._api("figures"));
        return this._read(response);
    }

    async createFigure(title) {
        return this._json("figures", "POST", { title: title || "" });
    }

    async getFigure(figureId) {
        const response = await fetch(this._api("figures/" + encodeURIComponent(figureId)));
        return this._read(response);
    }

    /**
     * Apply a batch of edits against the revision the client last read.
     *
     * One call is one undo step, and the batch is all-or-nothing. That is not
     * an optimisation: a five-panel split has to undo as one action, and half a
     * split landing is a figure in a state the user never asked for.
     */
    async patchFigure(figureId, baseRevision, operations) {
        return this._json("figures/" + encodeURIComponent(figureId), "PATCH", {
            base_revision: baseRevision,
            operations: operations,
        });
    }

    async replaceFigure(figureId, baseRevision, document) {
        return this._json("figures/" + encodeURIComponent(figureId), "PUT", {
            base_revision: baseRevision,
            document: document,
        });
    }

    async deleteFigure(figureId) {
        const response = await fetch(this._api("figures/" + encodeURIComponent(figureId)),
            { method: "DELETE" });
        return this._read(response);
    }

    async duplicateFigure(figureId, title) {
        return this._json("figures/" + encodeURIComponent(figureId) + "/duplicate", "POST",
            { title: title || "" });
    }

    /**
     * Store a panel's preview raster.
     *
     * Sent as bytes rather than as a base64 field on a JSON body: a WebP of a
     * canvas crop is tens of kilobytes and base64 would add a third to every
     * capture. `renderRevision` is what lets the server refuse a slow render
     * that lands after a newer one.
     */
    async putPreview(figureId, panelId, renderRevision, blob, size) {
        const query = new URLSearchParams({
            render_revision: String(renderRevision),
            width: String((size && size.width) || 0),
            height: String((size && size.height) || 0),
        });
        const response = await fetch(
            this._api(`figures/${encodeURIComponent(figureId)}/previews/${encodeURIComponent(panelId)}`)
            + "?" + query,
            { method: "POST", headers: { "Content-Type": blob.type || "image/webp" }, body: blob });
        return this._read(response);
    }

    /**
     * Where a panel's preview can be fetched from.
     *
     * `render_revision` rides along as a cache key rather than as something the
     * server reads: the route serves whatever is current, and the changing URL
     * is what stops the browser showing a panel the user has already edited.
     */
    previewUrl(figureId, panelId, renderRevision) {
        return this._api(`figures/${encodeURIComponent(figureId)}/previews/${encodeURIComponent(panelId)}`)
            + "?v=" + String(renderRevision || 0);
    }

    async putThumbnail(figureId, blob) {
        const response = await fetch(this._api(`figures/${encodeURIComponent(figureId)}/thumbnail`), {
            method: "PUT",
            headers: { "Content-Type": blob.type || "image/webp" },
            body: blob,
        });
        return this._read(response);
    }

    thumbnailUrl(figureId, revision) {
        return this._api(`figures/${encodeURIComponent(figureId)}/thumbnail`)
            + "?v=" + String(revision || 0);
    }

    /**
     * One channel of one region of a source, as raw numbers.
     *
     * Returned as a typed array rather than as an image, because the caller
     * composites it: Quick Edit's mini viewer keeps every channel's pixels and
     * recolours them in the browser, so changing a colour or dragging a
     * contrast slider costs no request at all.
     */
    async readPixels(figureId, sourceId, params) {
        const query = new URLSearchParams({
            channel: params.channel,
            x: String(Math.round(params.x)), y: String(Math.round(params.y)),
            w: String(Math.round(params.w)), h: String(Math.round(params.h)),
            out_w: String(Math.round(params.out_w)), out_h: String(Math.round(params.out_h)),
        });
        const response = await fetch(
            this._api(`figures/${encodeURIComponent(figureId)}`
                + `/sources/${encodeURIComponent(sourceId)}/pixels`) + "?" + query);
        if (!response.ok) return { ok: false, data: null };
        const buffer = await response.arrayBuffer();
        const [width, height] = (response.headers.get("X-Fb-Shape") || "0x0")
            .split("x").map((value) => parseInt(value, 10) || 0);
        return {
            ok: true,
            data: new Uint16Array(buffer),
            width: width,
            height: height,
            box: (response.headers.get("X-Fb-Box") || "").split(",").map(Number),
        };
    }

    /** A channel's intensity range, for a contrast slider's domain. */
    async pixelInfo(figureId, sourceId, channel) {
        const response = await fetch(
            this._api(`figures/${encodeURIComponent(figureId)}`
                + `/sources/${encodeURIComponent(sourceId)}/pixel_info`)
            + "?" + new URLSearchParams({ channel: channel }));
        return this._read(response);
    }

    async addAsset(figureId, filename, blob) {
        const response = await fetch(
            this._api(`figures/${encodeURIComponent(figureId)}/assets`)
            + "?" + new URLSearchParams({ filename: filename }),
            { method: "POST", headers: { "Content-Type": blob.type || "application/octet-stream" }, body: blob });
        return this._read(response);
    }

    assetUrl(figureId, assetId) {
        return this._api(`figures/${encodeURIComponent(figureId)}/assets/${encodeURIComponent(assetId)}`);
    }

    // -- export ----------------------------------------------------------
    //
    // Four calls rather than one download link. An eighteen-panel figure at 600
    // DPI is minutes of rendering, longer than any browser will hold a request
    // open -- so starting, following, cancelling and fetching are separate, and
    // the user can watch it and change their mind.

    /** What this export would be like, before any of it is rendered. */
    async preflightExport(figureId, options) {
        return this._json(`figures/${encodeURIComponent(figureId)}/export/preflight`,
            "POST", options);
    }

    async startExport(figureId, options) {
        return this._json(`figures/${encodeURIComponent(figureId)}/export`, "POST", options);
    }

    async exportStatus(figureId, jobId) {
        const response = await fetch(this._api(
            `figures/${encodeURIComponent(figureId)}/export/${encodeURIComponent(jobId)}`));
        return this._read(response);
    }

    async cancelExport(figureId, jobId) {
        const response = await fetch(this._api(
            `figures/${encodeURIComponent(figureId)}/export/${encodeURIComponent(jobId)}`),
            { method: "DELETE" });
        return this._read(response);
    }

    exportDownloadUrl(figureId, jobId) {
        return this._api(
            `figures/${encodeURIComponent(figureId)}/export/${encodeURIComponent(jobId)}/download`);
    }

    /** What a project image looks like right now -- for capture and relinking. */
    async describeSource(datasource) {
        const response = await fetch(this._api("sources/" + encodeURIComponent(datasource)));
        return this._read(response);
    }
}
