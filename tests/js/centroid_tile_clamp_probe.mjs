/**
 * The server files a cell whose centroid lies past the image's right or
 * bottom edge (or on it) into the last tile (centroid_tiles._write_level
 * clips the tile key). The client's decoder must key it the same way, or the
 * cell lands under a tile the viewer never asked for and is dropped
 * (audit lead flow-coords-7).
 *
 * decodeCentroidTileBuffer is sliced from the real imageViewer.js.
 *
 * Run directly:  node tests/js/centroid_tile_clamp_probe.mjs   (prints JSON)
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(path.join(here, "..", "..", "plexora", "client", "src", "js", "views",
    "imageViewer.js"), "utf8");

function between(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    if (start < 0) throw new Error(`${startMarker.trim()} not found in imageViewer.js`);
    const end = source.indexOf(endMarker, start);
    if (end < 0) throw new Error(`could not find the end of ${startMarker.trim()}`);
    return source.slice(start, end);
}

const decode = between("    decodeCentroidTileBuffer(buffer, level) {",
    "    async ensureSegmentationReady(");
const ImageViewer = new Function(`
    return class ImageViewer {
    centroidTileKey(level, x, y) {
        return \`\${level}/\${x}/\${y}\`;
    }
${decode}
    };
`)();

const viewer = Object.create(ImageViewer.prototype);
// 1000 x 600 image, 512 px tiles: level 0 has tiles 0..1 by 0..1.
viewer.centroidManifest = { tile_size: 512, width: 1000, height: 600 };
viewer.config = {};
const cells = [[1, 10, 10], [2, 1000, 20], [3, 1100.5, 590], [4, 900, 700], [5, -3, -2]];
const buffer = new ArrayBuffer(cells.length * 12);
const view = new DataView(buffer);
cells.forEach(([id, x, y], i) => {
    view.setUint32(i * 12, id, true);
    view.setFloat32(i * 12 + 4, x, true);
    view.setFloat32(i * 12 + 8, y, true);
});
const groups = viewer.decodeCentroidTileBuffer(buffer, 0);
const report = {};
groups.forEach((group, key) => { report[key] = Array.from(group.ids); });
console.log(JSON.stringify(report));
