/**
 * TranscriptsApi - this plugin's own HTTP client.
 *
 * A plugin owns the addresses of its own routes. Core's DataLayer must never
 * learn them: that is what made gating privileged in the way the plugin API
 * exists to rule out, and it is why `plugins/transcripts/...` appears in this
 * file and nowhere in plexora/client/src/ (asserted by
 * tests/test_datalayer_requests.py, which also exercises every method here).
 *
 * One class rather than fetches scattered through the layer and the panel,
 * for the reason RoiApi gives: a request buried in a controller is a request
 * nothing can test in isolation, and the probe that keeps core and plugin
 * apart can only check what it can construct.
 *
 * Nothing here swallows an error. A manifest that cannot be read and a
 * manifest that says "missing" are different states -- the first is a broken
 * server and the second is a layer that has not been built -- and collapsing
 * them would have the panel offer a Build button for a problem building
 * cannot fix.
 */
class TranscriptsApi {

    constructor(ctx) {
        this.url = ctx.url;
        this.datasource = ctx.datasource;
    }

    _query(extra = {}) {
        return new URLSearchParams({ datasource: this.datasource, ...extra });
    }

    /** What genes this layer has, and the grid its tiles are on. */
    async manifest(layer) {
        const response = await fetch(this.url(
            `plugins/transcripts/manifest?${this._query({ layer })}`));
        return response.ok ? response.json() : null;
    }

    /**
     * One tile's molecules, as the packed binary the renderer uploads.
     *
     * `level` 0 is the molecules themselves, and it asks for EVERY gene
     * deliberately: the shader picks which to draw, so a tile survives a
     * change of selection and toggling a gene costs no request. Above level
     * 0 a record is an aggregate of the molecules in one bin of one gene,
     * which the server can only compute for a stated selection and a stated
     * quality threshold -- so those ride the url there, and a tile is
     * immutable for the four of them together.
     */
    async points(layer, tile, { level = 0, genes = null, minq = null } = {}) {
        const extra = { layer, tile };
        if (level) extra.level = String(level);
        if (genes && genes.length) extra.genes = genes.join(",");
        if (minq !== null && minq !== undefined) {
            extra.minq = String(Math.round(minq));
        }
        const response = await fetch(this.url(
            `plugins/transcripts/points?${this._query(extra)}`));
        return response.ok ? response.arrayBuffer() : null;
    }

    /** How far along this layer's build is, in core's job vocabulary. */
    async status(layer) {
        const response = await fetch(this.url(
            `plugins/transcripts/status?${this._query({ layer })}`));
        return response.ok ? response.json() : { status: "missing" };
    }

    /** Ask for the tiles. Idempotent -- core's job registry holds one build
     *  per layer, so this joins an import's build rather than starting a
     *  second one over the same directory. */
    async build(layer) {
        const response = await fetch(this.url("plugins/transcripts/build"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ datasource: this.datasource, layer }),
        });
        return response.ok ? response.json() : null;
    }

    /**
     * Gene groups read out of a table the user has.
     *
     * The FILE is posted, not a parse of it: an .xlsx is a zip full of XML
     * and a CSV's delimiter has to be sniffed, and a browser that got either
     * subtly wrong would report a group with the wrong genes in it rather
     * than an error. Core already owns that reading and the route uses it.
     *
     * One of `file` (bytes from this browser, or relayed off a data node) and
     * `path` (a file the server can open itself) -- never both, because the
     * route prefers the upload and would quietly ignore a path sent beside
     * it.
     */
    async parseGroups(layer, { file = null, path = "" } = {}) {
        const form = new FormData();
        form.append("datasource", this.datasource);
        form.append("layer", layer);
        if (file) form.append("file", file);
        else form.append("path", path);
        const response = await fetch(this.url("plugins/transcripts/groups"),
                                     { method: "POST", body: form });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(body.error || "That file could not be read.");
        }
        return body;
    }

    /** The panel's saved state for this project, or `{}`. */
    async getState() {
        const response = await fetch(this.url(
            `plugins/transcripts/state?${this._query()}`));
        return response.ok ? response.json() : {};
    }

    async putState(state) {
        const response = await fetch(this.url("plugins/transcripts/state"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ datasource: this.datasource, ...state }),
        });
        return response.ok;
    }
}

if (typeof window !== "undefined") window.TranscriptsApi = TranscriptsApi;
if (typeof globalThis !== "undefined") globalThis.TranscriptsApi = TranscriptsApi;
