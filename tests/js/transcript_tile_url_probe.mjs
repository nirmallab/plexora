/**
 * The url the transcript layer asks for one molecule tile, per manifest.
 *
 * A point tile is served with a year-long `max-age`, so a browser that holds
 * one answers the next request for the same url out of its own cache and
 * never asks the server. Whatever tells two builds of the same layer apart
 * therefore has to be in the URL itself; an ETag that is never revalidated
 * reaches nobody.
 *
 * This probe runs the real client path -- `loadManifest` through
 * TranscriptsApi, then `fetchTile` -- once per manifest it is given, each on a
 * fresh layer the way a page load after a rebuild would, and prints the tile
 * url each one requested. The pytest driver
 * (plexora/plugins/transcripts/tests/test_transcripts_tile_cache_after_rebuild.py)
 * decides what those urls must satisfy.
 *
 * Run:  node tests/js/transcript_tile_url_probe.mjs <manifests.json>
 *       where the file holds {"manifests": [m1, m2, ...]}
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/transcripts/static");

const { manifests } = JSON.parse(readFileSync(process.argv[2], "utf8"));

let current = null;
const requested = [];

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, URLSearchParams,
    Uint8Array, Uint16Array, Float32Array, DataView, ArrayBuffer,
    encodeURIComponent,
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    fetch: async (url) => {
        const text = String(url);
        if (text.includes("plugins/transcripts/manifest")) {
            return { ok: true, json: async () => JSON.parse(JSON.stringify(current)) };
        }
        if (text.includes("plugins/transcripts/points")) {
            requested.push(text);
            return { ok: true, arrayBuffer: async () => new ArrayBuffer(0) };
        }
        return { ok: false };
    },
    document: { getElementById: () => null, querySelectorAll: () => [],
                createElement: () => ({ style: {}, classList: { add() {} },
                                        getContext: () => null, remove() {} }) },
    window: { devicePixelRatio: 2, addEventListener() {},
              setTimeout: () => 1, clearTimeout: () => {},
              requestAnimationFrame: () => 1 },
});

// base.html's order, as tests/js/transcript_points_probe.mjs loads them.
for (const name of ["views/slider.js", "views/gradientRange.js", "views/geneList.js"]) {
    runInContext(readFileSync(join(REPO, "plexora/client/src/js", name), "utf8"),
                 ctx, { filename: name });
}
for (const name of ["transcriptsApi.js", "transcriptPoints.js", "transcriptLayer.js"]) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

const { TranscriptLayer } = ctx;
const urls = [];
for (const manifest of manifests) {
    current = manifest;
    const layer = new TranscriptLayer({
        datasource: "xen",
        url: (path) => `/${path}`,
        layers: { viewport: () => null },
    }, "tx");
    layer.manifest = await layer.loadManifest();
    const before = requested.length;
    await layer.fetchTile(TranscriptLayer.tileKey("0", "0_0"), "0_0", 0, "0");
    urls.push(requested.length > before ? requested[requested.length - 1] : null);
}
console.log(`URLS ${JSON.stringify(urls)}`);
