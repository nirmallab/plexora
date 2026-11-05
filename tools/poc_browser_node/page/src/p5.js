/**
 * P5: an existing `plexora node serve` used from a static origin, with nothing
 * else running -- the node protocol as the contract (investigation §6).
 *
 * Every function here is one scenario the driver (bench/pocs.mjs P5) sets up;
 * each returns what it saw, and the driver compares the bytes against the
 * same requests made outside the browser.
 */
import { NodeClient } from "./nodeClient.js";
import { sha256 } from "./png.js";
import { createViewer, waitFull } from "./viewer.js";

const now = () => performance.now();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function hashOf(response) {
    return sha256(new Uint8Array(await response.arrayBuffer()));
}

/** (a) The protocol end to end: handshake, image, batch, table, statistics. */
export async function p5Protocol({ node, tiles, roles, fetchOptions = {} }) {
    const client = new NodeClient(node.endpoint, node.token, { fetchOptions });
    const out = { timings: {} };
    let s = now();
    const hello = await client.hello();
    out.timings.helloMs = now() - s;
    out.hello = { api_version: hello.api_version, capabilities: hello.capabilities.length,
                  resources: hello.resources.map((r) => `${r.kind}:${r.id}`) };
    const geometry = await client.imageGeometry(node.imageId);
    out.geometry = { width: geometry.width, height: geometry.height, levels: geometry.levels, channels: geometry.num_channels };

    // Tiles, HD and the 8-bit default, one request each.
    out.tiles = { hd: [], webp: [] };
    const exposed = {};
    for (const t of tiles) {
        for (const hd of [true, false]) {
            const s1 = now();
            const r = await fetch(client.tileUrl(node.imageId, t.c, t.l, t.x, t.y, { hd }), { credentials: "omit", ...fetchOptions });
            const entry = { ...t, status: r.status, type: r.headers.get("Content-Type") };
            for (const h of ["X-Plexora-Generation", "X-Plexora-Resource", "ETag", "Server-Timing", "X-Plexora-Api"]) {
                if (r.headers.get(h) !== null) exposed[h] = true;
            }
            entry.sha256 = await hashOf(r);
            entry.ms = now() - s1;
            out.tiles[hd ? "hd" : "webp"].push(entry);
        }
    }
    out.exposedHeaders = Object.keys(exposed);

    // The same HD tiles in one batch request (CORS-simple: text/plain).
    s = now();
    const batch = await client.batch(tiles.map((t) => client.tileUrl(node.imageId, t.c, t.l, t.x, t.y, { hd: true })));
    out.timings.batchMs = now() - s;
    out.batch = await Promise.all(batch.map(async (b) => ({ status: b.status, sha256: await sha256(b.body) })));
    out.batchMatchesSingles = out.batch.every((b, i) => b.sha256 === out.tiles.hd[i].sha256);

    // Statistics the viewer asks for on open.
    s = now();
    const [stats, gmm] = await Promise.all([client.imageStats(node.imageId, 0), client.imageGmm(node.imageId, 0)]);
    out.timings.statsMs = now() - s;
    out.stats = { vmin_hint: stats.vmin_hint, vmax_hint: stats.vmax_hint, vmin: gmm.vmin, vmax: gmm.vmax };

    // The table: the read spec pushed from the page (the page is the primary).
    if (node.tableId) {
        const inspected = await client.inspectTable(node.tableId);
        const spec = { ...(inspected.proposed || {}), type: inspected.data_type || "csv", src: "page",
                       roles: { ...(inspected.proposed?.roles || {}), ...roles } };
        s = now();
        const loaded = await client.loadTable(node.tableId, spec);
        out.timings.tableLoadMs = now() - s;
        out.tableLoaded = { generation: loaded.generation ?? null, rows: loaded.rows ?? loaded.count ?? null };
        s = now();
        const ids = await client.allCells(node.tableId, [roles.cell_id, roles.x, roles.y], true);
        out.timings.allCellsMs = now() - s;
        out.allCellsSha = await sha256(new Uint8Array(ids.buffer, ids.byteOffset, ids.byteLength));
        out.cells = ids.length / 3;
        // The describe answer's own bytes: the server's JSON is deterministic,
        // so the same bytes outside the browser are the identity check.
        const raw = new Uint8Array(await (await fetch(client.url(`table/${node.tableId}/describe`), { credentials: "omit", ...fetchOptions })).arrayBuffer());
        out.describeColumns = Object.keys(JSON.parse(new TextDecoder().decode(raw))).length;
        out.describeSha = await sha256(raw);
        const firstIds = [ids[0], ids[3], ids[6]];
        const rows = await client.rows(node.tableId, firstIds);
        out.rowsCount = rows.rows.length;
    }
    out.requests = client.log.length;
    return out;
}

/** (f) Can this page read the node at all (CORS for this exact origin)? */
export async function p5Reach({ node, fetchOptions = {} }) {
    const client = new NodeClient(node.endpoint, node.token, { fetchOptions });
    try {
        const hello = await client.hello();
        return { ok: true, origin: location.origin, api_version: hello.api_version };
    } catch (error) {
        return { ok: false, origin: location.origin, error: String(error).slice(0, 200) };
    }
}

/**
 * (b) Chrome's Local Network Access: this page asks a loopback node with and
 * without `targetAddressSpace: "loopback"`; the driver decides whether the
 * page counts as public (--ip-address-space-overrides) and whether the
 * permission was granted. What the permission API says is recorded beside it.
 */
export async function p5Lna({ node, connector = null }) {
    const out = { origin: location.origin, secure: self.isSecureContext };
    if (connector) {
        // The control: the connector stub answers the private-network preflight
        // (Access-Control-Allow-Private-Network: true); the node does not.
        for (const [name, options] of [["connectorPlain", {}], ["connectorLoopback", { targetAddressSpace: "loopback" }]]) {
            try {
                const r = await fetch(`${connector}/connector/v1/hello`, { credentials: "omit", ...options });
                out[name] = { ok: r.ok, status: r.status };
            } catch (error) {
                out[name] = { ok: false, error: String(error).slice(0, 160) };
            }
        }
    }
    out.permission = {};
    for (const name of ["local-network-access", "loopback-network", "local-network"]) {
        try {
            out.permission[name] = (await navigator.permissions.query({ name })).state;
        } catch (error) {
            out.permission[name] = "unknown";
        }
    }
    for (const [name, options] of [["plain", {}], ["loopback", { targetAddressSpace: "loopback" }],
                                   ["local", { targetAddressSpace: "local" }]]) {
        const s = now();
        try {
            const r = await fetch(`${node.endpoint}/node/v1/hello?t=${node.token}`, { credentials: "omit", ...options });
            out[name] = { ok: r.ok, status: r.status, ms: Math.round(now() - s) };
        } catch (error) {
            out[name] = { ok: false, error: String(error).slice(0, 160), ms: Math.round(now() - s) };
        }
    }
    return out;
}

/** (d) Under COOP/COEP: which ways of loading a node's tile still work. */
export async function p5Isolation({ node, tile }) {
    const client = new NodeClient(node.endpoint, node.token);
    const url = client.tileUrl(node.imageId, tile.c, tile.l, tile.x, tile.y, { hd: false });
    const out = { crossOriginIsolated: self.crossOriginIsolated, sharedArrayBuffer: typeof SharedArrayBuffer === "function" };
    try { const r = await fetch(url, { credentials: "omit" }); out.fetchCors = r.ok; } catch (e) { out.fetchCors = `blocked: ${e}`; }
    out.xhr = await new Promise((resolve) => {
        const x = new XMLHttpRequest();
        x.open("GET", url);
        x.responseType = "arraybuffer";
        x.onload = () => resolve(x.status === 200);
        x.onerror = () => resolve("blocked");
        x.send();
    });
    const img = (crossOrigin) => new Promise((resolve) => {
        const i = new Image();
        if (crossOrigin) i.crossOrigin = crossOrigin;
        i.onload = () => resolve(true);
        i.onerror = () => resolve("blocked");
        i.src = `${url}&_=${crossOrigin || "none"}`;
    });
    out.imgNoCors = await img(null);
    out.imgAnonymous = await img("anonymous");
    return out;
}

/** (e) Poll the node's health while the driver restarts it; the timeline. */
export async function p5Poll({ node, durationMs = 15000, everyMs = 100 }) {
    const client = new NodeClient(node.endpoint, node.token);
    const t0 = now();
    const timeline = [];
    let last = null;
    while (now() - t0 < durationMs) {
        let state;
        try {
            await client.health();
            state = "up";
        } catch (error) {
            state = error.status === 403 ? "refused" : "down";
        }
        if (state !== last) { timeline.push({ at: Math.round(now() - t0), state }); last = state; }
        await sleep(everyMs);
    }
    return { timeline };
}

/**
 * (g) Pairing with a connector: the link's fragment carries the connector's
 * address and token (a fragment is never sent to any server), the page keeps
 * it in IndexedDB and takes it out of the address bar, then asks the
 * connector to connect and uses the node it hands back.
 */
export async function p5Pair() {
    const out = { steps: [] };
    const step = (name, extra = {}) => out.steps.push({ name, at: Math.round(now()), ...extra });
    const match = /pair=([^:]+):(\d+):([0-9a-f]+)/.exec(location.hash);
    if (!match) return { error: "no pairing fragment", hash: location.hash };
    const [, host, port, token] = match;
    const connector = `http://${host}:${port}`;
    await new Promise((resolve, reject) => {
        const open = indexedDB.open("plexora-poc", 1);
        open.onupgradeneeded = () => open.result.createObjectStore("connectors");
        open.onsuccess = () => {
            const tx = open.result.transaction("connectors", "readwrite");
            tx.objectStore("connectors").put({ endpoint: connector, token, pairedAt: Date.now() }, "default");
            tx.oncomplete = () => { open.result.close(); resolve(); };
            tx.onerror = () => reject(tx.error);
        };
        open.onerror = () => reject(open.error);
    });
    history.replaceState(null, "", location.pathname + location.search);
    out.hashAfter = location.hash;
    step("stored");
    const ask = async (path, init = {}) => {
        const r = await fetch(`${connector}/connector/v1/${path}`, { credentials: "omit", ...init });
        return { status: r.status, body: await r.json().catch(() => null) };
    };
    out.helloUnpaired = await ask("hello");
    out.profilesWithoutToken = (await ask("profiles")).status;
    const headers = { "X-Plexora-Connector-Token": token };
    out.profiles = await ask("profiles", { headers });
    step("profiles");
    const t0 = now();
    const connected = await ask("connect", { method: "POST", headers: { ...headers, "Content-Type": "application/json" },
                                             body: JSON.stringify({ profile: "o2" }) });
    out.connectMs = now() - t0;
    out.connected = connected.status;
    step("connected");
    const client = new NodeClient(connected.body.endpoint, connected.body.token);
    const hello = await client.hello();
    out.nodeHello = { api_version: hello.api_version, resources: hello.resources.length };
    step("node");
    return out;
}

/**
 * (h) The node serving the app: this page was loaded from
 * <origin>/rnode/<host>/<port>/app/poc/index.html, so the node is this
 * origin's /rnode/<host>/<port>/ -- same origin, no CORS, no Local Network
 * Access question. A viewer is opened on it to show the product files load
 * from under the prefix too (base_url resolved at runtime).
 */
export async function p5Rnode({ token, imageId = "img", channels }) {
    const prefix = location.pathname.split("/app/")[0];
    const endpoint = location.origin + prefix;
    const client = new NodeClient(endpoint, token);
    const out = { endpoint, base: window.PLEXORA_BASE_URL };
    const hello = await client.hello();
    out.hello = { api_version: hello.api_version };
    const g = await client.imageGeometry(imageId);
    const geometry = { width: g.width, height: g.height, tileWidth: 1024, tileHeight: 1024,
                       levels: g.level_shapes.map(([h, w]) => ({ width: w, height: h })) };
    const viewer = await createViewer({
        container: "paneA", geometry, channels, feed: "node",
        nodeTileUrl: (c, level, x, y) => client.tileUrl(imageId, c, level, x, y),
    });
    const full = await waitFull(viewer.viewer, { timeoutMs: 60000 });
    out.viewerFull = full;
    const canvas = viewer.viewer.drawer.canvas;
    const px = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    let nonBlack = 0;
    for (let i = 0; i < px.length; i += 4) if (px[i] || px[i + 1] || px[i + 2]) nonBlack += 1;
    out.nonBlack = nonBlack;
    viewer.destroy();
    return out;
}
