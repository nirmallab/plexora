/**
 * cellExplorerApi.js - the four server calls, and the value cache.
 *
 * Two things here are load-bearing beyond "fetch some JSON".
 *
 * **Stale responses can never win.** Selecting a column starts a request;
 * selecting another one starts a second. They can finish in either order, and
 * the first finishing last would repaint the image with the previous variable's
 * colours under the current variable's legend. Every request carries the
 * generation it was issued at, the caller compares it on arrival, and the
 * outgoing one is aborted besides. Both, not either: an abort is best-effort
 * and a response already in flight still resolves.
 *
 * **Values are cached, so the cheap interactions stay cheap.** Palette,
 * opacity, category colour, visibility and range never refetch -- they rebuild
 * the lookup table from the arrays already here. Switching back to a column
 * looked at a moment ago is likewise free. Bounded to eight columns, least
 * recently used first, because each entry is a value per cell and a browser tab
 * holding twenty of them on a five-million-cell table is a tab that stops.
 */
class CellExplorerApi {

    /** How many decoded columns to keep. Enough that going back and forth
     *  between the two or three variables somebody is comparing is instant. */
    static CACHE_LIMIT = 8;

    constructor(ctx) {
        this.ctx = ctx;
        this.datasource = ctx.datasource;
        this._cache = new Map();
    }

    url(path, params) {
        const query = new URLSearchParams({ datasource: this.datasource, ...params });
        return `${this.ctx.url(`plugins/cell_explorer/api/${path}`)}?${query}`;
    }

    async variables(signal) {
        const response = await fetch(this.url("variables"), { signal });
        if (!response.ok) throw new Error(`variables: ${response.status}`);
        return response.json();
    }

    async state(signal) {
        const response = await fetch(this.url("state"), { signal });
        if (response.status === 422) {
            // Written by a newer Plexora. Reported rather than thrown, because
            // the panel still works -- it just must not save over it.
            return { ...(await response.json()), unreadable: true };
        }
        if (!response.ok) throw new Error(`state: ${response.status}`);
        return response.json();
    }

    async save(revision, settings) {
        const response = await fetch(
            this.ctx.url("plugins/cell_explorer/api/state"),
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ datasource: this.datasource, revision, settings }),
            },
        );
        const payload = await response.json().catch(() => ({}));
        if (response.status === 409) {
            return { ...payload, conflict: true };
        }
        if (!response.ok) throw new Error(payload.error || `save: ${response.status}`);
        return payload;
    }

    cacheKey(column, kind) {
        return `${this.datasource}:${column}:${kind || "auto"}`;
    }

    /**
     * One column's values, from the cache or the network.
     *
     * @param column the metadata column
     * @param kind   "categorical" | "continuous" to override the server's
     *               inference, or null to take it
     * @param signal an AbortSignal for the caller's current generation
     */
    async values(column, kind, signal) {
        const key = this.cacheKey(column, kind);
        const cached = this._cache.get(key);
        if (cached) {
            // Re-inserted so it counts as recently used. A Map iterates in
            // insertion order, which is what makes the eviction below LRU
            // rather than first-in.
            this._cache.delete(key);
            this._cache.set(key, cached);
            return cached;
        }

        const params = kind ? { column, kind } : { column };
        const response = await fetch(this.url("values", params), { signal });
        if (!response.ok) {
            const detail = await response.json().catch(() => ({}));
            throw new Error(detail.error || `values: ${response.status}`);
        }

        const decoded = CellExplorerApi.decode(
            await response.arrayBuffer(),
            response.headers.get("X-Value-Kind") || "categorical",
            Number(response.headers.get("X-Cell-Count") || 0),
        );

        this._cache.set(key, decoded);
        while (this._cache.size > CellExplorerApi.CACHE_LIMIT) {
            this._cache.delete(this._cache.keys().next().value);
        }
        return decoded;
    }

    /** Drop everything. For when the table underneath has changed. */
    clearCache() {
        this._cache.clear();
    }

    /**
     * Unpack one of the two record layouts (see server/values.py).
     *
     *     categorical   id uint32, code uint16   -- 6 bytes, no alignment
     *     continuous    id uint32, value float32 -- 8 bytes, 4-byte aligned
     *
     * The continuous case is read as two interleaved typed-array views over the
     * same buffer, which is a handful of milliseconds for a million cells. The
     * categorical stride is 6, so no typed array can be laid over it and a
     * DataView loop is the honest way to read it.
     */
    static decode(buffer, kind, count) {
        if (kind === "continuous") {
            const stride = 8;
            const rows = count || Math.floor(buffer.byteLength / stride);
            const asUint = new Uint32Array(buffer, 0, rows * 2);
            const asFloat = new Float32Array(buffer, 0, rows * 2);
            const ids = new Uint32Array(rows);
            const values = new Float32Array(rows);
            for (let i = 0; i < rows; i += 1) {
                ids[i] = asUint[i * 2];
                values[i] = asFloat[i * 2 + 1];
            }
            return { kind, ids, values, count: rows };
        }

        const stride = 6;
        const rows = count || Math.floor(buffer.byteLength / stride);
        const view = new DataView(buffer);
        const ids = new Uint32Array(rows);
        const codes = new Uint16Array(rows);
        for (let i = 0; i < rows; i += 1) {
            ids[i] = view.getUint32(i * stride, true);
            codes[i] = view.getUint16(i * stride + 4, true);
        }
        return { kind: "categorical", ids, codes, count: rows };
    }
}
