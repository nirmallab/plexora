/**
 * VisiumHdApi - this plugin's own HTTP client.
 *
 * Every `plugins/visium_hd/...` address the browser knows is in this file and
 * nowhere else, for the reason TranscriptsApi gives: core's DataLayer must
 * never learn a plugin's routes, and a fetch buried in a controller is one
 * nothing can exercise in isolation.
 *
 * The TILES are not here. A bin layer is drawn through core's
 * `generated/layer/...` route (see binLayer.js `tileSource`), because a
 * counted grid is a rendering primitive and not this plugin's format.
 *
 * Nothing here swallows an error into a plausible answer. A manifest that
 * cannot be read (null) and one that says `missing` are different states --
 * a broken server against a store that has not been built -- and the panel
 * offers a build only for the second.
 */
class VisiumHdApi {

    constructor(ctx) {
        this.url = ctx.url;
        this.datasource = ctx.datasource;
    }

    _query(extra = {}) {
        return new URLSearchParams({ datasource: this.datasource, ...extra });
    }

    async _get(path, extra) {
        const response = await fetch(this.url(
            `plugins/visium_hd/${path}?${this._query(extra)}`));
        return response.ok ? response.json() : null;
    }

    /** The gene vocabulary and the grid. Never inlined into `/config`: it is
     *  eighteen thousand names, and every other project would pay for them. */
    async manifest(layer) {
        return this._get("manifest", { layer });
    }

    /**
     * Each gene's automatic window at one pooling, for the legend.
     *
     * `bin` is the pooling in GRID SQUARES (4 = 8 micron squares on a 2
     * micron grid), the same number the tile url carries -- so the numbers
     * printed beside the colour bar are the ones the tiles were stretched by.
     */
    async stats(layer, genes, bin) {
        const names = (genes || []).filter(Boolean);
        if (!names.length) return {};
        return (await this._get("stats", {
            layer, genes: names.join(","), bin: String(bin || 1),
        })) || {};
    }

    /** The counts in the square under the cursor. `x`/`y` are GRID
     *  coordinates -- the caller inverts the layer transform. */
    async square(layer, x, y, genes, bin) {
        const extra = { layer, x: String(x), y: String(y), bin: String(bin || 1) };
        const names = (genes || []).filter(Boolean);
        if (names.length) extra.genes = names.join(",");
        return this._get("bin", extra);
    }

    /** How far along this layer's build is, in core's job vocabulary. */
    async status(layer) {
        return (await this._get("status", { layer })) || { status: "missing" };
    }

    /** Ask for the bin store. Idempotent: core's job registry holds one build
     *  per layer, so this joins an import's build rather than racing it. */
    async build(layer) {
        const response = await fetch(this.url("plugins/visium_hd/build"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ datasource: this.datasource, layer }),
        });
        return response.ok ? response.json() : null;
    }

    /**
     * Gene groups read out of a table the user has, for core's gene-group
     * dialog. The FILE is posted, not a parse of it -- see core's
     * `server/utils/gene_groups.py` -- and it is matched against this layer's
     * vocabulary. Throws with the route's own sentence, which the dialog
     * shows as it is.
     */
    async parseGroups(layer, { file = null, path = "" } = {}) {
        const form = new FormData();
        form.append("datasource", this.datasource);
        form.append("layer", layer);
        if (file) form.append("file", file);
        else form.append("path", path);
        const response = await fetch(this.url("plugins/visium_hd/groups"),
                                     { method: "POST", body: form });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(body.error || "That file could not be read.");
        }
        return body;
    }

    /** A standard Visium run's vocabulary, spot positions and UMIs. */
    async spots(layer) {
        return this._get("spots", { layer });
    }

    /** `{values: {gene: [count per spot]}, windows: {gene: window}}`. */
    async spotValues(layer, genes) {
        const names = (genes || []).filter(Boolean);
        return (await this._get("spot_values", {
            layer, genes: names.join(","),
        })) || { values: {}, windows: {} };
    }

    /** The panel's saved state for this project, or `{}`. */
    async getState() {
        return (await this._get("state")) || {};
    }

    async putState(state) {
        const response = await fetch(this.url("plugins/visium_hd/state"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ datasource: this.datasource, ...state }),
        });
        return response.ok;
    }
}

if (typeof window !== "undefined") window.VisiumHdApi = VisiumHdApi;
if (typeof globalThis !== "undefined") globalThis.VisiumHdApi = VisiumHdApi;
