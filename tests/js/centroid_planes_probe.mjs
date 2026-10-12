/**
 * Centroid dots on a z-stack whose cell table places each cell in depth.
 *
 * Plane k has to show plane k's cells, not the whole stack's flattened onto
 * it, and stepping through z must not blink: the old plane's dots stay drawn
 * until the new plane's tiles land, then are replaced. A reply for planes no
 * longer on screen is dropped.
 *
 * setCentroidPlanes, centroidRequestFilter and updateVisibleCentroidTiles are
 * sliced from the real imageViewer.js; the tile grid, the decoder and the
 * request are stubbed.
 *
 * Run directly:  node tests/js/centroid_planes_probe.mjs   (prints JSON)
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const SOURCE = path.join(here, "..", "..", "plexora", "client", "src", "js", "views",
    "imageViewer.js");
const source = await readFile(SOURCE, "utf8");

function between(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    if (start < 0) throw new Error(`${startMarker.trim()} not found in imageViewer.js`);
    const end = source.indexOf(endMarker, start);
    if (end < 0) throw new Error(`could not find the end of ${startMarker.trim()}`);
    return source.slice(start, end);
}

const update = between("    async updateVisibleCentroidTiles(showSpinner = false) {",
    "    prepareLegacyCentroidCache() {");
const planes = between("    setCentroidPlanes(planes) {", "    /** Walk every loaded label tile.");

const ImageViewer = new Function(`
    return class ImageViewer {
${update}
${planes}
    };
`)();

function deferred() {
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    return { promise, resolve };
}

const viewer = Object.create(ImageViewer.prototype);
Object.assign(viewer, {
    centroidFilter: { CD8: [1, 2] },
    centroidPlanes: null,
    centroidTilesStale: false,
    centroidTiles: new Map(),
    centroidTileRequest: 0,
    centroidFirstLoad: false,
    centroidManifest: {},
    viewer: { viewport: {} },
    requests: [],
    shouldDrawCentroids: () => true,
    setLoading() {},
    refreshCentroidOverlay() {},
    scheduleCentroidTileUpdate() {},
    getVisibleCentroidTileState: () => ({
        level: 0, tiles: [{ key: "0/0/0", x: 0, y: 0 }], keepKeys: new Set(["0/0/0"]) }),
    decodeCentroidTileBuffer: (buffer) => new Map([["0/0/0", { ids: buffer.ids }]]),
});
viewer.dataLayer = {
    getCentroidTiles(level, tiles, filter) {
        const reply = deferred();
        viewer.requests.push({ tiles, filter, reply });
        return reply.promise;
    },
};
const ids = () => Array.from(viewer.centroidTiles.get("0/0/0")?.ids || []);

const report = {};

// Plane 7 on screen, nothing loaded yet: the request carries the plane.
viewer.setCentroidPlanes({ column: "Z", z0: 7, z1: 7 });
report.filter = viewer.centroidRequestFilter();
let pending = viewer.updateVisibleCentroidTiles();
viewer.requests.at(-1).reply.resolve({ ids: [1, 2] });
await pending;

// Step to plane 2: the cached tile is fetched again, and plane 7's dots stay
// drawn until plane 2's land.
viewer.setCentroidPlanes({ column: "Z", z0: 2, z1: 2 });
const before = viewer.requests.length;
pending = viewer.updateVisibleCentroidTiles();
report.refetched_cached_tile = viewer.requests.length === before + 1
    && JSON.stringify(viewer.requests.at(-1).filter.Z) === JSON.stringify([1.5, 2.5]);
report.while_loading = ids();
viewer.requests.at(-1).reply.resolve({ ids: [3] });
await pending;
report.after_loading = ids();

// Plane 4 then plane 5 before plane 4's reply: plane 4's is dropped.
viewer.setCentroidPlanes({ column: "Z", z0: 4, z1: 4 });
const first = viewer.updateVisibleCentroidTiles();
const firstReply = viewer.requests.at(-1).reply;
viewer.setCentroidPlanes({ column: "Z", z0: 5, z1: 5 });
const second = viewer.updateVisibleCentroidTiles();
const secondReply = viewer.requests.at(-1).reply;
secondReply.resolve({ ids: [5] });
await second;
firstReply.resolve({ ids: [4] });
await first;
report.superseded = ids();

// The same planes again cost nothing.
const counter = viewer.centroidTileRequest;
viewer.setCentroidPlanes({ column: "Z", z0: 5, z1: 5 });
report.same_planes_is_free = viewer.centroidTileRequest === counter && !viewer.centroidTilesStale;

// A slab's whole range, given either way round; a flat view has no z filter.
viewer.setCentroidPlanes({ column: "Z", z0: 4, z1: 2 });
report.slab_filter = viewer.centroidRequestFilter();
viewer.setCentroidPlanes(null);
report.flat_filter = viewer.centroidRequestFilter();

console.log(JSON.stringify(report));
