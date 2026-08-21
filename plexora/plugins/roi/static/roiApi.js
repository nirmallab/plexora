/**
 * RoiApi - this plugin's own HTTP client.
 *
 * A plugin owns the addresses of its own routes. Core's DataLayer must never
 * learn them: that is what made gating privileged in the way the plugin API
 * exists to rule out, and it is why `plugins/roi/...` appears in this file and
 * nowhere in plexora/client/src/ (asserted by tests/test_datalayer_requests.py).
 *
 * Unlike GatingApi, nothing here swallows its own errors. That is deliberate.
 * Gating's methods each catch and `console.log`, which is exactly what hid
 * `saveGatingList` failing outright for every call it ever made -- and for
 * annotations the stakes are higher, because a save that quietly does nothing
 * means the user's regions exist only in a tab they are about to close. So a
 * transport failure propagates to RoiStore, which keeps the geometry, retries,
 * and says so on the panel.
 *
 * Requests return `{ok, status, data}` rather than throwing on a non-2xx: the
 * status IS the answer for two of them. 409 means somebody else saved first and
 * the user has work worth keeping; 422 means the image changed underneath. Both
 * need handling, not an exception.
 */
class RoiApi {

    constructor(ctx) {
        this.url = ctx.url;
        this.datasource = ctx.datasource;
    }

    /** Build the response shape every method returns. */
    async _read(response) {
        let data = {};
        try {
            data = await response.json();
        } catch (e) {
            // A body that is not JSON is a server fault, not an answer. Keep the
            // status, which is the part still worth acting on.
            data = { error: "the server sent a response that could not be read" };
        }
        return { ok: response.ok, status: response.status, data };
    }

    async getState() {
        const response = await fetch(this.url('plugins/roi/api/state') + '?' + new URLSearchParams({
            datasource: this.datasource
        }));
        return this._read(response);
    }

    async postOperations(baseRevision, operations) {
        const response = await fetch(this.url('plugins/roi/api/operations'), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                datasource: this.datasource,
                base_revision: baseRevision,
                operations: operations
            })
        });
        return this._read(response);
    }

    async importGeojson(document, baseRevision, acceptDimensionMismatch) {
        const response = await fetch(this.url('plugins/roi/api/import'), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                datasource: this.datasource,
                base_revision: baseRevision,
                document: document,
                accept_dimension_mismatch: Boolean(acceptDimensionMismatch)
            })
        });
        return this._read(response);
    }

    /**
     * Download the server's export.
     *
     * Fetched into a Blob rather than navigated to, so a failure is visible
     * here as a status rather than as the browser replacing the viewer with an
     * error page -- and so this method is one the request probe can actually
     * observe.
     */
    async downloadExport() {
        const response = await fetch(this.url('plugins/roi/api/export.geojson') + '?' + new URLSearchParams({
            datasource: this.datasource
        }));
        if (!response.ok) return this._read(response);
        const blob = await response.blob();
        RoiApi.saveBlob(blob, `${this.datasource}_rois.geojson`);
        return { ok: true, status: response.status, data: {} };
    }

    /**
     * Where this project's regions can be written, and under what name.
     *
     * One call rather than one per fact: the panel needs the kind, the default
     * name, the names already taken in the user's file and the one this project
     * last saved to, all at the same moment, and none of them is useful alone.
     */
    async destination() {
        const response = await fetch(this.url('plugins/roi/api/adapters/destination') + '?' + new URLSearchParams({
            datasource: this.datasource
        }));
        return this._read(response);
    }

    async saveToAnndata(key, replace) {
        const response = await fetch(this.url('plugins/roi/api/adapters/anndata'), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                datasource: this.datasource,
                key: key,
                // Never defaulted to true here. The server refuses an existing
                // key without it, which is the guard that stops one annotation
                // pass landing on another -- this flag is the user's answer to
                // being asked, not a convenience.
                replace: Boolean(replace)
            })
        });
        return this._read(response);
    }

    /**
     * Annotate the project's cells with the regions they fall inside.
     *
     * The other direction from the saves above: those put the polygons in the
     * user's file, this puts two columns on their rows. `name` is the same
     * destination name the saves take, because the columns are derived from it
     * -- one name to keep track of, not two.
     */
    async mapToCells(name, replace) {
        const response = await fetch(this.url('plugins/roi/api/map_to_cells'), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                datasource: this.datasource,
                name: name,
                // Never defaulted to true, for the same reason the AnnData save
                // does not: the server refuses an existing column without it,
                // and that refusal is the guard against one mapping pass
                // silently erasing another.
                replace: Boolean(replace)
            })
        });
        return this._read(response);
    }

    async saveToSpatialdata(elementName) {
        const response = await fetch(this.url('plugins/roi/api/adapters/spatialdata'), {
            method: 'POST',
            headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
            body: JSON.stringify({ datasource: this.datasource, element_name: elementName })
        });
        return this._read(response);
    }

    /**
     * Hand the browser a file to save.
     *
     * Also the emergency path: RoiStore builds the same document from local
     * state when the server is unreachable, so a user whose saves are failing
     * can still get their work out of the tab.
     */
    static saveBlob(blob, filename) {
        const href = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = href;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        // Deferred: revoking synchronously races the download in Safari.
        setTimeout(() => URL.revokeObjectURL(href), 10_000);
    }
}
