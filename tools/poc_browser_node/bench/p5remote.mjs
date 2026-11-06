/**
 * P5R: P5's protocol check against a node that is already running somewhere
 * else -- an O2 job through its ssh tunnel, an ERIS node behind the portal, a
 * GCP VM behind a TLS proxy -- for the manual records of the investigation's
 * section 8.4. Nothing is started here but the static page.
 *
 * The node must allow the page's origin, which is fixed by `port`:
 *   plexora node serve ... --allow-origin http://127.0.0.1:8650
 * The token is read from POC_NODE_TOKEN and never written to the record.
 *
 *   POC_NODE_TOKEN=... node bench/run.mjs --poc P5R --fixture none --label o2 --params \
 *     '{"endpoint":"http://127.0.0.1:8765","image":"img","table":"cells","port":8650,
 *       "roles":{"cell_id":"CellID","x":"X_centroid","y":"Y_centroid"}}'
 *
 * Records: the round trip (/health x 20), the page's protocol answers against
 * the same requests made outside the browser (tiles HD and WebP, batch,
 * all_cells, describe), and each tile's time as the page saw it.
 */
import { createHash } from "node:crypto";
import { openPage } from "./pocs.mjs";

export async function P5R(ctx) {
    const p = ctx.args.params || {};
    const token = process.env.POC_NODE_TOKEN;
    if (!p.endpoint || !token) throw new Error("P5R needs --params endpoint and POC_NODE_TOKEN");
    const roles = p.roles || { cell_id: "CellID", x: "X_centroid", y: "Y_centroid" };
    const imageId = p.image || "img";
    const tableId = p.table || null;
    const headers = { "X-Plexora-Node-Token": token };
    const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");

    // The round trip, outside the browser.
    const rtt = [];
    for (let i = 0; i < 20; i += 1) {
        const t0 = performance.now();
        await fetch(`${p.endpoint}/node/v1/health`, { headers });
        rtt.push(performance.now() - t0);
    }
    rtt.sort((a, b) => a - b);
    const hello = await (await fetch(`${p.endpoint}/node/v1/hello`, { headers })).json();
    const geometry = await (await fetch(`${p.endpoint}/node/v1/image/${imageId}/geometry`, { headers })).json();
    let seed = 17;
    const rand = () => ((seed = (seed * 1103515245 + 12345) >>> 0) / 4294967296);
    const tiles = Array.from({ length: 20 }, () => {
        const l = Math.floor(rand() * geometry.levels);
        const [h, w] = geometry.level_shapes[l];
        return { c: Math.floor(rand() * geometry.num_channels), l,
                 x: Math.floor(rand() * Math.ceil(w / 1024)), y: Math.floor(rand() * Math.ceil(h / 1024)) };
    });

    // A node's 8-bit tiles change once its background window scan of that
    // channel finishes (the first ones are drawn with a provisional window):
    // for every channel the tiles use, wait until one of its tiles reads the
    // same twice, a second apart, so both sides compare settled bytes.
    const t0 = performance.now();
    let settledMs = null;
    for (const t of [...new Map(tiles.map((x) => [x.c, x])).values()]) {
        const probe = `${p.endpoint}/node/v1/image/${imageId}/tile/${imageId}_${t.c}/${t.l}/${t.x}_${t.y}.png?tw=1024&th=1024`;
        let last = null;
        while (performance.now() - t0 < 120000) {
            const now = sha(Buffer.from(await (await fetch(probe, { headers })).arrayBuffer()));
            if (now === last) break;
            last = now;
            await new Promise((r) => setTimeout(r, 1000));
        }
    }
    settledMs = performance.now() - t0;

    const page = await openPage(ctx, { staticOptions: { port: p.port || 8650 } });
    const node = { endpoint: p.endpoint, token, imageId, tableId };
    const out = await page.page.evaluate((a) => window.POC.p5Protocol(a), { node, tiles, roles });
    const logs = page.browser.logs.filter((l) => !l.includes(token)).slice(-20);
    await page.browser.close();
    page.stat.proc.kill();

    const ref = { hd: [], webp: [] };
    for (const t of tiles) {
        for (const hd of [true, false]) {
            const r = await fetch(`${p.endpoint}/node/v1/image/${imageId}/tile/${imageId}_${t.c}/${t.l}/${t.x}_${t.y}.png?tw=1024&th=1024${hd ? "&q=hd" : ""}`,
                                  { headers });
            ref[hd ? "hd" : "webp"].push(sha(Buffer.from(await r.arrayBuffer())));
        }
    }
    const checks = {
        hdTilesIdentical: out.tiles.hd.every((t, i) => t.sha256 === ref.hd[i]),
        webpTilesIdentical: out.tiles.webp.every((t, i) => t.sha256 === ref.webp[i]),
        batchMatchesSingles: out.batchMatchesSingles,
    };
    if (tableId) {
        const ids = [roles.cell_id, roles.x, roles.y].join(",");
        const allCells = await fetch(`${p.endpoint}/node/v1/table/${tableId}/all_cells?columns=${ids}&dtype=integer`, { headers });
        const describe = await fetch(`${p.endpoint}/node/v1/table/${tableId}/describe`, { headers });
        checks.allCellsIdentical = out.allCellsSha === sha(Buffer.from(await allCells.arrayBuffer()));
        checks.describeIdentical = out.describeSha === sha(Buffer.from(await describe.arrayBuffer()));
    }
    // Nothing that carries the token goes into the record.
    const clean = JSON.parse(JSON.stringify(out).split(token).join("<token>"));
    return {
        browser: page.env, arm: "browser->remote node", state: `node at ${new URL(p.endpoint).host}, already running`,
        metrics: { rttMs: { p50: rtt[10], p95: rtt[19], min: rtt[0] }, windowsSettledMs: settledMs, hello: { api_version: hello.api_version,
                   capabilities: hello.capabilities }, geometry: { levels: geometry.levels, channels: geometry.num_channels },
                   a: clean },
        checks, logs,
    };
}
