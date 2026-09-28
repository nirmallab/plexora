/**
 * services/performanceTelemetry.js: tile timings without a single URL.
 *
 *   - classify() returns `{path, kind}` and nothing else, or null.
 *   - The project's name is in every tile URL; it never reaches a row.
 *   - Server-Timing phases become histograms by path.
 *   - The texture cache and decode timings fold into rows before a post.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js/services");

let checks = 0;
async function check(label, fn) {
    await fn();
    checks += 1;
    console.log("  ok  " + label);
}

function sandbox({ base = "", direct = [], mode = "anonymous" } = {}) {
    const ctx = {
        console,
        URL,
        crypto: { getRandomValues: (buffer) => buffer.fill(3) },
        setInterval: () => 1,
        clearInterval: () => {},
        location: { origin: "http://127.0.0.1:8000", href: "http://127.0.0.1:8000/viewer/patient_29384" },
        navigator: {
            userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            hardwareConcurrency: 10,
        },
        devicePixelRatio: 2,
        performance: { now: () => 1234 },
        requestAnimationFrame: () => 1,
        cancelAnimationFrame: () => {},
        document: { visibilityState: "visible", addEventListener: () => {} },
        addEventListener: () => {},
        plexoraUrl: (path) => "/" + path,
        PLEXORA_BASE_URL: base,
        PlexoraRouting: { isDirectOrigin: (origin) => direct.includes(origin) },
        fetch: async (url) => ({ ok: true, json: async () => ({ mode, enabled: true }) }),
    };
    ctx.window = ctx;
    createContext(ctx);
    runInContext(readFileSync(join(JS, "telemetry.js"), "utf8"), ctx);
    runInContext(readFileSync(join(JS, "performanceTelemetry.js"), "utf8"), ctx);
    return { ctx, P: ctx.PlexoraPerf, T: ctx.PlexoraTelemetry };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const TILE = "http://127.0.0.1:8000/generated/data/patient_29384/CD45_file_3/4/12_7.png";

await check("classify returns only {path, kind}", () => {
    const { P } = sandbox();
    const found = P.classify(TILE);
    assert.deepEqual(Object.keys(found).sort(), ["kind", "path"]);
    assert.equal(found.kind, "channel");
    assert.equal(found.path, "local");
});

await check("kinds come from the path's shape", () => {
    const { P } = sandbox();
    assert.equal(P.classify(TILE + "?q=hd").kind, "hd");
    assert.equal(P.classify("http://127.0.0.1:8000/generated/data/p/segmentation/2/0_0.png").kind, "label");
    assert.equal(P.classify("http://127.0.0.1:8000/generated/data/p/rgb/2/0_0.png").kind, "rgb");
    assert.equal(P.classify("http://127.0.0.1:8000/generated/layer/p/l/c_0/1/0_0.png").kind, "channel");
    assert.equal(P.classify("http://127.0.0.1:8000/get_centroid_tiles?datasource=p").kind, "points");
    assert.equal(P.classify("http://127.0.0.1:8000/config"), null);
    assert.equal(P.classify("https://elsewhere.example/generated/data/p/c_0/0/0_0.png"), null);
});

await check("a proxy prefix is `proxy`, a direct node is `direct`", () => {
    const proxied = sandbox({ base: "/rnode/node17/8000" });
    assert.equal(proxied.P.classify(TILE).path, "proxy");
    const direct = sandbox({ direct: ["http://node17:41000"] });
    const found = direct.P.classify("http://node17:41000/node/v1/image/img/tile/c_0/0/0_0.png?t=secret");
    assert.deepEqual({ ...found }, { path: "direct", kind: "channel" });
});

await check("a resource entry becomes histograms, and no URL survives", async () => {
    const { P, T } = sandbox();
    await settle();
    P._onResource({
        initiatorType: "xmlhttprequest", name: TILE + "?token=abc", startTime: 10,
        responseEnd: 52, transferSize: 60000,
        serverTiming: [{ name: "read", duration: 8 }, { name: "enc", duration: 21 },
                       { name: "total", duration: 31 }, { name: "cache", description: "miss" }],
    });
    P._fold();
    const rows = [...T._rows.values()];
    const text = JSON.stringify(rows);
    for (const leak of ["patient", "CD45", "http", "token", "?", "/"]) {
        assert.ok(!text.includes(leak), leak);
    }
    const tile = rows.find((row) => row.k === "tile_ms");
    assert.equal(tile.d.path, "local");
    assert.equal(tile.d.kind, "channel");
    assert.equal(tile.d.browser, "chrome");
    assert.equal(tile.d.os, "mac");
    assert.equal(tile.h[1], 1);  // 42 ms
    assert.ok(rows.find((row) => row.k === "server_read_ms"));
    assert.ok(rows.find((row) => row.k === "server_cache" && row.d.result === "miss"));
    assert.ok(rows.find((row) => row.k === "tile_bytes" && row.d.band === "10k"));
    assert.ok(rows.find((row) => row.k === "dpr" && row.d.band === "2"));
});

await check("the GPU family is attached only in diagnostics, as a family", async () => {
    const renderer = {
        gl: {
            getExtension: () => ({ UNMASKED_RENDERER_WEBGL: 1 }),
            getParameter: () => "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0)",
            canvas: { addEventListener: () => {} },
        },
        _tileTextureCache: { hits: 0, misses: 0 },
    };
    const anonymous = sandbox();
    await settle();
    anonymous.P.noteGl(renderer);
    anonymous.P.decoded("webp", "worker", 12);
    assert.ok(![...anonymous.T._rows.values()].some((row) => "gpu" in row.d));
    const diagnostics = sandbox({ mode: "diagnostics" });
    await settle();
    diagnostics.P.noteGl(renderer);
    diagnostics.P.decoded("webp", "worker", 12);
    const row = [...diagnostics.T._rows.values()].find((r) => r.k === "decode_ms");
    assert.equal(row.d.gpu, "nvidia");
    assert.ok(!JSON.stringify(row).includes("4090"));
});

await check("the texture cache's counters fold as deltas", async () => {
    const { P, T } = sandbox();
    await settle();
    const cache = { hits: 0, misses: 0 };
    P.noteGl({ gl: null, _tileTextureCache: cache });
    cache.hits = 5;
    cache.misses = 2;
    P._fold();
    cache.hits = 7;
    P._fold();
    const hits = [...T._rows.values()].find((row) => row.k === "texture_cache" && row.d.result === "hit");
    assert.equal(hits.n, 7);
});

console.log(`\nall checks passed (${checks})`);
