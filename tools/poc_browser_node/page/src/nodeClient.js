/**
 * A Plexora data node, spoken to straight from the page: the browser twin of
 * plexora/server/providers/node.py + providers/http.py + providers/wire.py,
 * for the routes the POC needs.
 *
 * The token rides as `?t=` on GETs (a CORS-simple request: no preflight) and
 * as X-Plexora-Node-Token on POSTs. Credentials are never sent: the node does
 * not use cookies, and `omit` keeps a page from leaking one to it.
 *
 * `fetchOptions` is merged into every request -- P5 passes
 * `{targetAddressSpace: "loopback"}` there for Chrome's Local Network Access.
 */
export class NodeClient {
    constructor(endpoint, token, { fetchOptions = {} } = {}) {
        this.endpoint = endpoint.replace(/\/+$/, "");
        this.token = token;
        this.fetchOptions = fetchOptions;
        this.log = [];
    }

    url(path, params = {}) {
        const query = new URLSearchParams({ ...params, t: this.token });
        return `${this.endpoint}/node/v1/${path.replace(/^\/+/, "")}?${query}`;
    }

    async request(path, { params = {}, method = "GET", body = null, as = "json" } = {}) {
        const t0 = performance.now();
        const init = { method, credentials: "omit", mode: "cors", ...this.fetchOptions };
        if (body !== null) {
            init.body = typeof body === "string" ? body : JSON.stringify(body);
            init.headers = { "Content-Type": "application/json", "X-Plexora-Node-Token": this.token };
        }
        const response = await fetch(this.url(path, params), init);
        const entry = { path, method, status: response.status, ms: 0 };
        this.log.push(entry);
        if (!response.ok) {
            const text = await response.text();
            entry.ms = performance.now() - t0;
            const error = new Error(`${method} ${path} -> ${response.status}: ${text.slice(0, 300)}`);
            error.status = response.status;
            throw error;
        }
        const out = as === "json" ? await response.json()
            : as === "bytes" ? new Uint8Array(await response.arrayBuffer())
            : response;
        entry.ms = performance.now() - t0;
        return out;
    }

    hello() { return this.request("hello"); }
    health() { return this.request("health"); }

    // -- images ------------------------------------------------------------

    imageGeometry(id) { return this.request(`image/${id}/geometry`); }

    tileUrl(id, channel, level, x, y, { hd = true, tw = 1024, th = 1024 } = {}) {
        const params = { tw, th };
        if (hd) params.q = "hd";
        return this.url(`image/${id}/tile/${id}_${channel}/${level}/${x}_${y}.png`, params);
    }

    async tileBytes(id, channel, level, x, y, options) {
        const response = await fetch(this.tileUrl(id, channel, level, x, y, options),
                                     { credentials: "omit", ...this.fetchOptions });
        if (!response.ok) throw new Error(`tile ${channel}/${level}/${x}_${y} -> ${response.status}`);
        return new Uint8Array(await response.arrayBuffer());
    }

    segTileUrl(id, level, x, y, { tw = 1024, th = 1024 } = {}) {
        return this.url(`seg/${id}/tile/${level}/${x}_${y}.png`, { tw, th });
    }

    imageStats(id, channel) { return this.request(`image/${id}/stats`, { params: { channel_index: channel } }); }
    imageGmm(id, channel) { return this.request(`image/${id}/gmm`, { params: { channel_index: channel } }); }
    quantization(id, channel) { return this.request(`image/${id}/quantization`, { params: { channel_index: channel } }); }

    // -- tables ------------------------------------------------------------

    inspectTable(id) { return this.request(`table/${id}/inspect`); }
    loadTable(id, spec, reload = false) {
        return this.request(`table/${id}/load`, { method: "POST", body: { spec, reload } });
    }
    describe(id) { return this.request(`table/${id}/describe`); }

    /** The /get_all_cells wire shape: one flat uint32 or float32 buffer. */
    async allCells(id, columns, integer) {
        const bytes = await this.request(`table/${id}/all_cells`, {
            params: { columns: columns.join(","), ...(integer ? { dtype: "integer" } : {}) },
            as: "bytes",
        });
        return integer ? new Uint32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4)
            : new Float32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4);
    }

    async columns(id, names) {
        const bytes = await this.request(`table/${id}/columns`, { params: { names: names.join(",") }, as: "bytes" });
        return unpackColumns(bytes);
    }

    rows(id, ids) { return this.request(`table/${id}/rows`, { params: { ids: ids.join(",") } }); }

    op(id, operation, payload) {
        return this.request(`table/${id}/op/${operation}`, { method: "POST", body: payload });
    }

    /**
     * The node's tile batch (server/tile_batch.py): `{"tiles": [path, ...]}`
     * sent as text/plain, so the POST is CORS-simple and needs no preflight;
     * each path carries its own `?t=`. Answered with a little-endian uint32
     * header length, a JSON header `{tiles: [{status, type, etag, bytes}]}`,
     * then every tile's bytes in order.
     */
    async batch(urls) {
        const paths = urls.map((u) => { const p = new URL(u); return p.pathname + p.search; });
        const response = await fetch(`${this.endpoint}/node/v1/batch?t=${encodeURIComponent(this.token)}`, {
            method: "POST", credentials: "omit", ...this.fetchOptions,
            headers: { "Content-Type": "text/plain" },
            body: JSON.stringify({ tiles: paths }),
        });
        if (!response.ok) throw new Error(`batch -> ${response.status}: ${(await response.text()).slice(0, 200)}`);
        const bytes = new Uint8Array(await response.arrayBuffer());
        const size = new DataView(bytes.buffer).getUint32(0, true);
        const header = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + size)));
        let at = 4 + size;
        return header.tiles.map((meta) => {
            const body = bytes.slice(at, at + meta.bytes);
            at += meta.bytes;
            return { ...meta, body };
        });
    }
}

/** providers/wire.py's frame: uint32 BE header length, JSON header, raw bytes. */
export function unpackFrame(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const size = view.getUint32(0, false);
    const meta = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + size)));
    return { meta, payload: bytes.subarray(4 + size) };
}

export function unpackColumns(bytes) {
    const { meta, payload } = unpackFrame(bytes);
    const names = meta.names || [];
    const length = Number(meta.length || 0);
    if (meta.dtype !== "<f4") throw new Error(`columns frame dtype ${meta.dtype}`);
    const copy = payload.slice();   // aligned
    const flat = new Float32Array(copy.buffer, 0, copy.byteLength / 4);
    const out = {};
    names.forEach((name, i) => { out[name] = flat.subarray(i * length, (i + 1) * length); });
    return out;
}
